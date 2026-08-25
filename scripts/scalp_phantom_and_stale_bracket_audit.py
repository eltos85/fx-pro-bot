#!/usr/bin/env python3
"""Аудит двух искажений общего one-way счёта: фантомные сделки и забытые брекеты.

Только чтение. Ни БД, ни ордера не трогает.

Оба искажения родом из одного факта: Bybit linear one-way держит ОДИН лот на
символ, а счёт делят несколько ботов
(https://bybit-exchange.github.io/docs/v5/position/position-mode).

**Фантом.** Наш вход против чужой стороны не открывает позицию, а режет чужую.
Бот до `c9effac` (24.08) этого не проверял: строка в БД появлялась, позиции не
существовало, и «сделка» сопровождалась как настоящая, пока её не закрывали по
mark-price. Пример — #4325 (ETH 20.08): в БД шорт 1.45 @ 2298.24 и −$289.18, на
бирже в ту же секунду закрылся ЧУЖОЙ лонг 1.45 @ 2280.50 с +$21.96. Такая
строка не просто мусор: −28.5R это 30% всего минуса 20-дневной выборки.

**Забытый брекет.** На общем лоте брекеты стоят в режиме Partial, поэтому наш
дискреционный выход не закрывает позицию символа целиком — остаётся чужая нога,
а наш TP/SL остаётся живым и может сработать по ЧУЖОМУ объёму. Пример — #4501
(ETH 25.08): в 00:40 наш `flow_exit` закрыл 1.34, в 00:46 по цене нашего тейка
2499.79 закрылись ещё 1.34.

Две части, потому что два разных источника правды:

* **Часть A — по БД, вся история.** Отпечаток не зависит от API: при живом
  биржевом брекете результат сделки не может уехать далеко за номинальные
  −1R/+3.5R. Если уехал — брекета на бирже не было, то есть не было и позиции
  (либо ордер брекета не встал). Это кандидаты, а не доказательства.
* **Часть B — по бирже, последние 7 суток.** `get_closed_pnl` отдаёт максимум
  7-дневное окно (офдок close-pnl: «endTime − startTime <= 7 days»), зато даёт
  доказательство. Проверяем два отпечатка на реальных записях.

Семантика `side` в ответе close-pnl — сторона ЗАКРЫВАЮЩЕГО ордера, а не
позиции. Это видно из примера в самом офдоке
(https://bybit-exchange.github.io/docs/v5/position/close-pnl): `side=Sell`,
`avgEntryPrice=1194.98`, `avgExitPrice=1180.60`, `closedPnl=-47.41` — шорт от
1194.98 на выходе 1180.60 дал бы плюс, значит закрывали ЛОНГ ордером Sell.
Отпечаток фантома опирается ровно на это: наш вход стороной X появляется в
истории как закрытие чужой позиции ордером стороны X.

Запуск (внутри контейнера, ключи берутся из его окружения):

    docker exec -i fx-pro-bot-scalp-bot-1 python3 - /data/scalp_bot.sqlite \\
        < scripts/scalp_phantom_and_stale_bracket_audit.py

Часть B пропускается, если ключей в окружении нет (`--no-api` форсирует).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime

# Технические закрытия: позиции не было по построению, к аудиту не относятся.
_NON_TRADE = ("restart_flat", "entry_Cancelled", "entry_Rejected",
              "entry_Deactivated", "entry_timeout", "open")

# Насколько далеко за СВОЙ уровень может уехать результат при живом биржевом
# брекете: проскальзывание плюс комиссия (одна комиссия съедает до 0.42R, замер
# 24.08). Порог сознательно относительный к геометрии КОНКРЕТНОЙ сделки, а не
# фиксированный коридор −1R/+3.5R: у удалённой `sweep_fade_run` тейк стоял на
# ~11R, и фиксированный порог записал 22 её честных тейка в кандидаты.
_BEYOND_LEVEL_R = 0.6

# Допуски отпечатков. Цена входа/выхода сверяется относительно (тики разные:
# XRP 0.0001, BTC 0.1), объём — с тем же 2% допуском на шаг лота, что в
# executor._MIXED_LOT_TOL.
_PRICE_TOL = 5e-4
_QTY_TOL = 0.02
# Окно поиска записи вокруг нашего события. Наш вход и биржевое закрытие чужой
# ноги — одно и то же исполнение, расходятся только на задержку публикации.
_MATCH_WINDOW_SEC = 90.0
# Насколько позже нашего выхода должен сработать брекет, чтобы считаться
# забытым (а не нашим же закрытием, чьи филлы дошли с задержкой).
_STALE_MIN_LAG_SEC = 30.0


def _ts(v: float) -> str:
    return datetime.fromtimestamp(v, UTC).strftime("%m-%d %H:%M:%S")


def _r_of(row: sqlite3.Row) -> float | None:
    risk = abs(row["entry"] - row["sl"]) * row["qty"]
    if risk <= 0 or row["exit"] is None:
        return None
    sign = 1.0 if row["side"] == "long" else -1.0
    net = (row["exit"] - row["entry"]) * row["qty"] * sign - (row["fees_usd"] or 0.0)
    return net / risk


def _close(a: float, b: float, tol: float) -> bool:
    ref = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / ref <= tol


def _tp_in_r(row: sqlite3.Row) -> float | None:
    """Где стоит тейк этой сделки, в её собственных R. Стоп по определению −1R."""
    risk = abs(row["entry"] - row["sl"])
    if risk <= 0 or row["tp"] is None:
        return None
    return abs(row["tp"] - row["entry"]) / risk


def part_a(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Кандидаты по БД: результат вне коридора живого брекета."""
    ph = ",".join("?" for _ in _NON_TRADE)
    rows = db.execute(
        f"""SELECT id, ts_open, ts_close, symbol, side, qty, entry, sl, tp, exit,
                   pnl_usd, fees_usd, close_reason, strategy, pnl_verified
            FROM trades
            WHERE status='closed' AND mode='live' AND exit IS NOT NULL
              AND (close_reason IS NULL OR close_reason NOT IN ({ph}))
            ORDER BY ts_open""", _NON_TRADE).fetchall()

    print("=== ЧАСТЬ A: результат ушёл за СВОЙ уровень (вся история) ===")
    print(f"порог: дальше {_BEYOND_LEVEL_R}R за собственный SL или TP сделки — "
          "при живом биржевом брекете так уехать нельзя")
    head = (f"{'id':>6} {'открыт':<15}{'символ':<13}{'страт':<17}"
            f"{'причина':<14}{'часов':>7}{'R':>8}{'TP в R':>8}"
            f"{'net$':>10}{'ver':>4}")
    print(head)
    print("-" * len(head))
    hits: list[sqlite3.Row] = []
    reasons: Counter[str] = Counter()
    total_r = 0.0
    for r in rows:
        rr = _r_of(r)
        tp_r = _tp_in_r(r)
        if rr is None:
            continue
        past_sl = rr < -1.0 - _BEYOND_LEVEL_R
        past_tp = tp_r is not None and rr > tp_r + _BEYOND_LEVEL_R
        if not (past_sl or past_tp):
            continue
        hits.append(r)
        reasons[r["close_reason"] or "?"] += 1
        total_r += rr
        hold = ((r["ts_close"] or r["ts_open"]) - r["ts_open"]) / 3600.0
        print(f"{r['id']:>6} {_ts(r['ts_open']):<15}{r['symbol']:<13}"
              f"{(r['strategy'] or '?'):<17}{(r['close_reason'] or '?'):<14}"
              f"{hold:>7.1f}{rr:>8.2f}"
              f"{(tp_r if tp_r is not None else float('nan')):>8.1f}"
              f"{(r['pnl_usd'] or 0.0):>10.2f}{r['pnl_verified']:>4}")
    print(f"\nвсего сделок в истории: {len(rows)}; кандидатов: {len(hits)}; "
          f"их суммарный вклад: {total_r:+.1f}R")
    if reasons:
        print("причины: " + ", ".join(f"{k}={v}" for k, v in reasons.most_common()))
    return rows


def _fetch_closed_pnl(symbols: set[str], since_ms: int,
                      until_ms: int) -> dict[str, list[dict]]:
    """Записи close-pnl по символам с ОБЯЗАТЕЛЬНОЙ пагинацией (stats-collection.mdc:
    без `while cursor` первая страница — неполные данные)."""
    from pybit.unified_trading import HTTP
    sess = HTTP(api_key=os.environ["SCALP_BYBIT_API_KEY"],
                api_secret=os.environ["SCALP_BYBIT_API_SECRET"],
                demo=os.environ.get("SCALP_BYBIT_DEMO", "true").lower() != "false",
                recv_window=10000)
    out: dict[str, list[dict]] = {}
    for sym in sorted(symbols):
        recs: list[dict] = []
        cursor: str | None = None
        while True:
            kw = dict(category="linear", symbol=sym, startTime=since_ms,
                      endTime=until_ms, limit=100)
            if cursor:
                kw["cursor"] = cursor
            res = sess.get_closed_pnl(**kw)["result"]
            recs += res.get("list", []) or []
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
        out[sym] = recs
    return out


def part_b(db: sqlite3.Connection) -> None:
    """Доказательства по бирже за доступное 7-дневное окно."""
    now = time.time()
    since = now - 6.5 * 86400          # запас к лимиту окна 7 суток
    ph = ",".join("?" for _ in _NON_TRADE)
    rows = db.execute(
        f"""SELECT id, ts_open, ts_close, symbol, side, qty, entry, sl, tp, exit,
                   pnl_usd, fees_usd, close_reason, strategy
            FROM trades
            WHERE status='closed' AND mode='live' AND ts_open >= ?
              AND (close_reason IS NULL OR close_reason NOT IN ({ph}))
            ORDER BY ts_open""", (since, *_NON_TRADE)).fetchall()
    if not rows:
        print("\n=== ЧАСТЬ B: сделок в 7-дневном окне нет ===")
        return
    recs = _fetch_closed_pnl({r["symbol"] for r in rows},
                             int(since * 1000), int(now * 1000))
    n_rec = sum(len(v) for v in recs.values())
    print(f"\n=== ЧАСТЬ B: сверка с биржей, {_ts(since)} … {_ts(now)} UTC ===")
    print(f"наших сделок в окне: {len(rows)}; записей close-pnl: {n_rec}")

    print("\n--- B1: наш вход закрыл ЧУЖУЮ позицию (фантом) ---")
    print("отпечаток: сторона закрывающего ордера = сторона нашего входа, "
          "объём = наш, цена выхода = наша цена входа, время = наш вход")
    head = (f"{'id':>6} {'символ':<13}{'наш вход':<16}{'в БД, R':>9}"
            f"{'чужая ср.вход':>15}{'closedPnl':>11}")
    print(head)
    print("-" * len(head))
    phantoms = 0
    for r in rows:
        our_close_side = "Buy" if r["side"] == "long" else "Sell"
        for x in recs.get(r["symbol"], []):
            if x.get("side") != our_close_side:
                continue
            t = int(x["updatedTime"]) / 1000.0
            if abs(t - r["ts_open"]) > _MATCH_WINDOW_SEC:
                continue
            if not _close(float(x["closedSize"]), r["qty"], _QTY_TOL):
                continue
            if not _close(float(x["avgExitPrice"]), r["entry"], _PRICE_TOL):
                continue
            phantoms += 1
            rr = _r_of(r)
            print(f"{r['id']:>6} {r['symbol']:<13}{_ts(r['ts_open']):<16}"
                  f"{(rr if rr is not None else float('nan')):>9.2f}"
                  f"{float(x['avgEntryPrice']):>15.2f}"
                  f"{float(x['closedPnl']):>11.2f}")
            break
    print(f"найдено: {phantoms}")

    print("\n--- B2: наш забытый брекет закрыл ЧУЖОЙ объём ---")
    print("отпечаток: цена выхода = наш TP или SL, объём = наш, "
          f"время позже нашего выхода на >{_STALE_MIN_LAG_SEC:.0f}с")
    head2 = (f"{'id':>6} {'символ':<13}{'наш выход':<16}{'сработал':<16}"
             f"{'уровень':<8}{'цена':>10}{'closedPnl':>11}")
    print(head2)
    print("-" * len(head2))
    # Уровни повторяются (XRP ходит по тем же ценам сутками), поэтому запись
    # надо сначала исключить как «наша другая сделка»: иначе каждый более
    # поздний свой стоп на совпавшей цене выглядит как забытый брекет. Первый
    # прогон дал так 6 ложных из 7.
    own_closes: dict[str, list[float]] = {}
    for r in rows:
        if r["ts_close"] is not None:
            own_closes.setdefault(r["symbol"], []).append(r["ts_close"])

    stale = 0
    stale_pnl = 0.0
    for r in rows:
        if r["ts_close"] is None:
            continue
        for x in recs.get(r["symbol"], []):
            t = int(x["updatedTime"]) / 1000.0
            if t - r["ts_close"] < _STALE_MIN_LAG_SEC:
                continue
            px = float(x["avgExitPrice"])
            level = ("TP" if _close(px, r["tp"], _PRICE_TOL)
                     else "SL" if _close(px, r["sl"], _PRICE_TOL) else None)
            if level is None:
                continue
            if not _close(float(x["closedSize"]), r["qty"], _QTY_TOL):
                continue
            if any(abs(t - c) <= _MATCH_WINDOW_SEC
                   for c in own_closes.get(r["symbol"], [])):
                continue        # это закрытие другой НАШЕЙ сделки, не чужой лот
            stale += 1
            stale_pnl += float(x["closedPnl"])
            print(f"{r['id']:>6} {r['symbol']:<13}{_ts(r['ts_close']):<16}"
                  f"{_ts(t):<16}{level:<8}{px:>10.4f}"
                  f"{float(x['closedPnl']):>11.2f}")
    print(f"найдено: {stale}; чужого P&L закрыто на ${stale_pnl:+.2f} "
          "(в нашу стату не попадает, но лот соседа мы двигаем)")


def part_c(db: sqlite3.Connection) -> None:
    """Насколько записанный в БД P&L расходится со своей же геометрией.

    Это мера доверия ко ВСЕЙ прошлой статистике: `pnl_usd` брался с биржи, а на
    общем лоте биржевые цифры считаются от чужой средней входа. Расхождение
    двустороннее — чужая нога могла войти и лучше нашей.
    """
    ph = ",".join("?" for _ in _NON_TRADE)
    rows = db.execute(
        f"""SELECT id, ts_open, symbol, side, qty, entry, sl, exit, pnl_usd,
                   fees_usd, close_reason, strategy
            FROM trades
            WHERE status='closed' AND mode='live' AND exit IS NOT NULL
              AND pnl_usd IS NOT NULL
              AND (close_reason IS NULL OR close_reason NOT IN ({ph}))
            ORDER BY ts_open""", _NON_TRADE).fetchall()
    print("\n=== ЧАСТЬ C: записанный P&L против своей геометрии (вся история) ===")
    head = (f"{'id':>6} {'открыт':<15}{'символ':<13}{'страт':<17}"
            f"{'причина':<14}{'геометрия':>11}{'в БД':>11}{'дельта':>11}")
    print(head)
    print("-" * len(head))
    by_month: dict[str, list[float]] = {}
    total = 0.0
    hits = 0
    for r in rows:
        sign = 1.0 if r["side"] == "long" else -1.0
        geo = (r["exit"] - r["entry"]) * r["qty"] * sign - (r["fees_usd"] or 0.0)
        delta = r["pnl_usd"] - geo
        if abs(delta) <= 1.0:
            continue
        hits += 1
        total += delta
        month = datetime.fromtimestamp(r["ts_open"], UTC).strftime("%Y-%m")
        by_month.setdefault(month, []).append(delta)
        if abs(delta) >= 10.0:      # печатаем только материальные
            print(f"{r['id']:>6} {_ts(r['ts_open']):<15}{r['symbol']:<13}"
                  f"{(r['strategy'] or '?'):<17}{(r['close_reason'] or '?'):<14}"
                  f"{geo:>11.2f}{r['pnl_usd']:>11.2f}{delta:>11.2f}")
    print(f"\nсделок проверено: {len(rows)}; расходятся >$1: {hits}; "
          f"суммарный сдвиг статистики: ${total:+.2f}")
    if by_month:
        print("по месяцам:")
        for m in sorted(by_month):
            v = by_month[m]
            print(f"  {m}: сделок {len(v):<4} сдвиг ${sum(v):+.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--no-api", action="store_true",
                    help="только часть A (без обращения к бирже)")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    part_a(db)
    part_c(db)
    if args.no_api:
        print("\n(часть B пропущена: --no-api)")
    elif not os.environ.get("SCALP_BYBIT_API_KEY"):
        print("\n(часть B пропущена: в окружении нет SCALP_BYBIT_API_KEY)")
    else:
        part_b(db)
    db.close()


if __name__ == "__main__":
    main()
