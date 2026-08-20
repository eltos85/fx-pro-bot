"""Угадывает ли скальп момент закрытия нашей позиции? (read-only)

Шаг 2 плана STRATEGY_HYBRID.md. Скрипт ничего не меняет: только читает Bybit.

─── Зачем это считать ───────────────────────────────────────────────────────

Шаг 1 (§14 канона) показал: когда после закрытия мы покупаем обратно сразу,
позиция и цена те же, меняются только уплаченные комиссии и размер лота —
поэтому такой контур отстаёт от простого удержания в любом режиме рынка.

Остался один способ выиграть: после закрытия **не покупать сразу**, а подождать.
Это имеет смысл только если закрытия попадают в моменты, после которых цена
падает. Вопрос измеримый: сравнить, что было с ценой после наших закрытий, с
тем, что было бы после обычного часа рядом.

─── Как считается ───────────────────────────────────────────────────────────

Для каждого закрытия, сделанного скальпом или биржевым стопом/тейком (ядро
своим выходом лот не закрывает — §11 канона), берётся движение цены за 1, 4, 12
и 24 часа после. То же движение считается для каждого часа в окне ±48 часов
вокруг события — это база сравнения («обычный час рядом»).

Место события в этой базе и есть ответ. Доля часов, после которых цена вела
себя ХУЖЕ для нас (росла сильнее), чем после нашего закрытия:

    50%  — попадание случайное, угадывания нет;
    >50% — цена после закрытий падала чаще обычного, пауза имела бы смысл;
    <50% — закрытия попадают в моменты хуже случайных.

Знак читается так: для длинной позиции хорошее закрытие — то, после которого
цена упала (мы не сидели в просадке).

Источник данных — только биржа (`stats-collection.mdc`): исполнения из
/v5/execution/list и часовые свечи из /v5/market/kline. Локальные БД не
читаются.

Запуск (контейнер скальпа — там ключи и pybit):
    ssh root@204.168.149.140 \
      "docker exec -i fx-pro-bot-scalp-bot-1 python3 - --days 7" \
      < scripts/hybrid_trigger_skill.py
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

HOUR_MS = 3_600_000
HORIZONS_H = (1, 4, 12, 24)
NULL_WINDOW_H = 48

# Скрипт намеренно самодостаточен: он запускается передачей в контейнер через
# stdin (`python3 - < ...`), а там нет ни репозитория, ни соседних файлов, так
# что импортировать учёт из hybrid_contour_pnl.py нельзя. Ниже — те же правила
# атрибуции ног и та же пагинация, что в измерителе контура.

# Окно запроса у execution/list и closed-pnl ограничено 7 днями.
# https://bybit-exchange.github.io/docs/v5/order/execution
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000

# Клиентский троттлинг ниже лимитов API (5 req/s).
# https://bybit-exchange.github.io/docs/v5/rate-limit
THROTTLE_SEC = 0.2

# У Funding-записей execQty равен размеру позиции — в расчёт ног они не идут.
# https://bybit-exchange.github.io/docs/v5/enum#exectype
TRADE_EXEC_TYPES = {"Trade", "AdlTrade", "BustTrade", "Delivery"}

ACTOR_CORE = "core"
ACTOR_TACTIC = "tactic"
ACTOR_BRACKET = "bracket"
ACTOR_UNKNOWN = "unknown"


def _ms_to_utc(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _fnum(raw: object, default: float = 0.0) -> float:
    try:
        if raw in (None, ""):
            return default
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _actor(fill: dict) -> tuple[str, str]:
    """Кто инициировал ногу. `orderLinkId` — единственная метка владельца."""
    link = fill.get("orderLinkId") or ""
    if link.startswith("scalp_"):
        return ACTOR_TACTIC, link
    if link.startswith("swing_"):
        return ACTOR_CORE, "swing"
    if link.startswith("daytrend_"):
        return ACTOR_CORE, "daytrend"
    sot = fill.get("stopOrderType") or ""
    if sot and sot != "UNKNOWN":
        return ACTOR_BRACKET, sot
    return ACTOR_UNKNOWN, link or "-"


def _paginate(method, *, start_ms: int, end_ms: int, **params) -> list[dict]:
    """Полный обход cursor'ом по окнам <= 7 дней."""
    rows: list[dict] = []
    win_start = start_ms
    while win_start < end_ms:
        win_end = min(win_start + SEVEN_DAYS_MS, end_ms)
        cursor = ""
        while True:
            resp = method(startTime=win_start, endTime=win_end, limit=100,
                          cursor=cursor, **params)
            result = resp.get("result") or {}
            rows.extend(result.get("list") or [])
            cursor = result.get("nextPageCursor") or ""
            time.sleep(THROTTLE_SEC)
            if not cursor:
                break
        win_start = win_end
    return rows


def find_fixations(fills: list[dict]) -> list[dict]:
    """Закрытия лота, сделанные НЕ ядром: скальпом или биржевым брекетом.

    Позиция считается по ногам, чтобы не спутать закрытие с обычным входом:
    фиксация — это продажа при открытом лонге. Выход самого ядра (`swing_`,
    `daytrend_`) не считается фиксацией: это его собственное решение, а не
    внешний триггер.
    """
    pos = 0.0
    out: list[dict] = []
    for f in sorted(fills, key=lambda r: int(r.get("execTime") or 0)):
        if (f.get("execType") or "Trade") not in TRADE_EXEC_TYPES:
            continue
        qty = _fnum(f.get("execQty"))
        price = _fnum(f.get("execPrice"))
        if qty <= 0 or price <= 0:
            continue
        actor, detail = _actor(f)
        side = f.get("side")
        if side == "Sell" and pos > 0 and actor in (ACTOR_TACTIC,
                                                    ACTOR_BRACKET):
            out.append({
                "ts": int(f.get("execTime") or 0),
                "price": price,
                "qty": min(qty, pos),
                "actor": actor,
                "detail": detail,
            })
        pos += qty if side == "Buy" else -qty
        if abs(pos) < 1e-12:
            pos = 0.0
    return out


def fetch_hour_bars(sess, symbol: str, start_ms: int,
                    end_ms: int) -> dict[int, float]:
    """{начало часа: цена закрытия часа}. Пагинация по 1000 свечей."""
    out: dict[int, float] = {}
    end = end_ms
    while True:
        rows = sess.get_kline(category="linear", symbol=symbol, interval="60",
                              start=start_ms, end=end,
                              limit=1000)["result"]["list"]
        if not rows:
            break
        oldest = end
        for r in rows:
            ts = int(r[0])
            out[ts] = float(r[4])
            oldest = min(oldest, ts)
        if len(rows) < 1000 or oldest <= start_ms:
            break
        end = oldest - 1
    return out


def _floor_hour(ts: int) -> int:
    return ts - (ts % HOUR_MS)


def forward_move(bars: dict[int, float], ts: int,
                 hours: int) -> float | None:
    """Движение цены за `hours` от часа, в который попало событие."""
    h0 = _floor_hour(ts)
    now = bars.get(h0)
    later = bars.get(h0 + hours * HOUR_MS)
    if now is None or later is None or now <= 0:
        return None
    return later / now - 1.0


def rank_vs_neighbours(bars: dict[int, float], ts: int, hours: int,
                       window_h: int = NULL_WINDOW_H) -> tuple[float, int] | None:
    """Доля соседних часов, после которых цена росла сильнее, чем после события.

    Это и есть «угадал или нет»: 0.5 = как случайный час рядом, больше 0.5 =
    после нашего закрытия цена падала чаще обычного.
    """
    mine = forward_move(bars, ts, hours)
    if mine is None:
        return None
    h0 = _floor_hour(ts)
    worse = 0
    total = 0
    for k in range(-window_h, window_h + 1):
        if k == 0:
            continue
        other = forward_move(bars, h0 + k * HOUR_MS, hours)
        if other is None:
            continue
        total += 1
        if other > mine:
            worse += 1
    if total == 0:
        return None
    return worse / total, total


def sign_test_p(hits: int, n: int) -> float:
    """Насколько такой перекос вероятен при честной монете (в обе стороны).

    Берётся меньший хвост: перекос важен и когда попаданий подозрительно много
    (угадывание), и когда их подозрительно мало (систематически плохие моменты).
    """
    if n == 0:
        return 1.0
    upper = sum(math.comb(n, k) for k in range(hits, n + 1)) / 2 ** n
    lower = sum(math.comb(n, k) for k in range(0, hits + 1)) / 2 ** n
    return min(1.0, 2 * min(upper, lower))


def clustering(rows: list[dict], hours: int = 12) -> tuple[int, int]:
    """Сколько закрытий стоят ближе `hours` друг к другу.

    Такие события смотрят на почти одно и то же движение цены, поэтому
    считать их независимыми наблюдениями нельзя — иначе значимость завышена.
    """
    ts = sorted(r["ts"] for r in rows)
    close_pairs = sum(1 for a, b in zip(ts, ts[1:])
                      if b - a < hours * HOUR_MS)
    return close_pairs, len(ts)


def summarize(rows: list[dict], hours: int) -> dict | None:
    """Сводка по одному горизонту: где в среднем оказались наши закрытия."""
    ranks = [r["ranks"][hours] for r in rows if r["ranks"].get(hours)]
    if not ranks:
        return None
    shares = [x[0] for x in ranks]
    hits = sum(1 for s in shares if s > 0.5)
    moves = [r["moves"][hours] for r in rows if r["moves"].get(hours) is not None]
    saved = [r["saved"][hours] for r in rows if r["saved"].get(hours) is not None]
    return {
        "n": len(shares),
        "mean_share": statistics.fmean(shares),
        "hits": hits,
        "p": sign_test_p(hits, len(shares)),
        "mean_move": statistics.fmean(moves) * 100 if moves else 0.0,
        "saved": sum(saved),
    }


def report(symbol: str, fixations: list[dict],
           bars: dict[int, float]) -> None:
    rows = []
    for fx in fixations:
        moves: dict[int, float | None] = {}
        ranks: dict[int, tuple[float, int] | None] = {}
        saved: dict[int, float | None] = {}
        for h in HORIZONS_H:
            mv = forward_move(bars, fx["ts"], h)
            moves[h] = mv
            ranks[h] = rank_vs_neighbours(bars, fx["ts"], h)
            # Сколько денег дала бы пауза: цена ушла вниз на лоте, который мы
            # не держали. Плюс = пауза сберегла, минус = пауза стоила.
            saved[h] = None if mv is None else -mv * fx["price"] * fx["qty"]
        rows.append({**fx, "moves": moves, "ranks": ranks, "saved": saved})

    print(f"\n=== {symbol}: закрытий не ядром — {len(rows)} ===")
    if not rows:
        print("  нечего считать: за окном нет закрытий от скальпа или брекета")
        return

    print("  по каждому закрытию (движение цены после него):")
    for r in rows:
        when = _ms_to_utc(r["ts"])
        parts = []
        for h in HORIZONS_H:
            mv = r["moves"][h]
            parts.append(f"{h}ч " + ("н/д" if mv is None else f"{mv*100:+.2f}%"))
        print(f"    {when:%m-%d %H:%M} {r['actor']:<8} {r['qty']:.3f} @ "
              f"{r['price']:.2f} | " + "  ".join(parts))

    print("\n  сводка (сравнение с обычным часом рядом, окно ±48ч):")
    for h in HORIZONS_H:
        s = summarize(rows, h)
        if s is None:
            print(f"    {h:>2}ч: данных нет")
            continue
        if s["mean_share"] > 0.5:
            verdict = "похоже на угадывание"
        elif s["mean_share"] < 0.5:
            verdict = "моменты хуже обычного часа"
        else:
            verdict = "ровно как случайный час"
        print(f"    {h:>2}ч: n={s['n']:<3} лучше обычного часа в "
              f"{s['mean_share']*100:.0f}% случаев "
              f"({s['hits']}/{s['n']} закрытий выше середины, p={s['p']:.2f}), "
              f"средний ход цены {s['mean_move']:+.2f}%, "
              f"пауза дала бы ${s['saved']:+,.0f} — {verdict}")

    pairs, total = clustering(rows)
    if pairs:
        print(f"  внимание: {pairs} из {total - 1} промежутков между закрытиями "
              f"короче 12ч — такие события смотрят на одно и то же движение, "
              f"поэтому независимых наблюдений меньше, чем n, а p занижен")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="ETHUSDT,BTCUSDT")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--category", default="linear")
    args = ap.parse_args()

    key = os.environ.get("SCALP_BYBIT_API_KEY", "")
    secret = os.environ.get("SCALP_BYBIT_API_SECRET", "")
    if not key or not secret:
        raise SystemExit("нужны SCALP_BYBIT_API_KEY / SCALP_BYBIT_API_SECRET")
    demo = os.environ.get("SCALP_BYBIT_DEMO", "true").lower() in (
        "1", "true", "yes")

    from pybit.unified_trading import HTTP
    sess = HTTP(demo=demo, api_key=key, api_secret=secret, recv_window=20000)

    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)

    print("Угадывает ли скальп момент закрытия? | источник: Bybit "
          "(execution/list + kline 1h)")
    print(f"окно {_ms_to_utc(start_ms):%Y-%m-%d %H:%M} → "
          f"{now:%Y-%m-%d %H:%M} UTC | горизонты "
          f"{', '.join(str(h) + 'ч' for h in HORIZONS_H)}")
    print("50% = попадание случайное; больше 50% = после закрытий цена "
          "падала чаще обычного")

    for symbol in [s.strip().upper() for s in args.symbols.split(",")
                   if s.strip()]:
        fills = _paginate(sess.get_executions, start_ms=start_ms,
                                end_ms=end_ms, category=args.category,
                                symbol=symbol)
        fixations = find_fixations(fills)
        if fixations:
            lo = min(f["ts"] for f in fixations) - NULL_WINDOW_H * HOUR_MS
            hi = max(f["ts"] for f in fixations) + (
                NULL_WINDOW_H + max(HORIZONS_H)) * HOUR_MS
            bars = fetch_hour_bars(sess, symbol, lo, min(hi, end_ms))
        else:
            bars = {}
        report(symbol, fixations, bars)

    print("\n" + "=" * 78)
    print("Выборка меньше ~30 закрытий — это наблюдение, а не вывод "
          "(sample-size.mdc).\nСчитать доказательством можно только "
          "устойчивый перевес с p < 0.05.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
