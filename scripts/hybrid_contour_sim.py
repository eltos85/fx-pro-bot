"""Шаг 1 плана STRATEGY_HYBRID.md: контур против «просто держать ядро».

Живая выборка контура (n=6 фиксаций за 40 ч, один режим) до порога
`sample-size.mdc` не дойдёт никогда, поэтому единственный путь к гейту §8.1 —
история. Скрипт read-only, торговую логику не трогает, локальные БД не читает:
только публичные kline и funding с Bybit.

─── Что воспроизводится по факту, а не по интуиции ───────────────────────────

Ядро = `swing-bot` как он есть в коде (`src/horizon_bot`): правило
`sma20_50_4h` (SMA20 > SMA50 на 4h, long/flat), нотионал
`equity * SWING_POSITION_FRAC` (0.15, docker-compose.yml), лот = нотионал / цена,
плечо 1. Правило не тюнится (`no-data-fitting.mdc`, `strategy-guard.mdc`):
скрипт измеряет контур, а не ищет лучшие окна SMA.

Протокол исполнения как в `scripts/scalp_swing_research.py`: сигнал на close
бара, сделка на open следующего. Издержка taker 0.055% на ногу
(<https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate>), funding —
фактические ставки из `/v5/market/funding/history`, платит только открытая
позиция.

**Перезаход.** В `src/horizon_bot/app/main.py` вход происходит при
`want == 1 and ours is None`, а `ours` обнуляется по `broker_flat` сразу после
того как биржа закрыла общий лот. Поллинг 180 с, то есть ядро возвращается
через минуты. Замеренные live-гэпы «лок → перезаход» (§5 канона)
−0.024%/−0.235%/−0.022%/+0.281%/+0.062% — около нуля. Поэтому базовый режим
симулятора `--flat-hours 0` = перезаход по цене фиксации.

Отсюда следствие, которое проверяется численно: при мгновенном перезаходе
контур не меняет экспозицию, а только платит лишние ноги и пересчитывает лот от
новой цены. Значит выигрыш возможен только если фиксация даёт **паузу вне
рынка** (`--flat-hours > 0`). Ради этого в симуляторе и есть сетка пауз.

Триггеры здесь механические (случайный, трейлинг по ATR, трейлинг по %) —
ордерфлоу-сигнала на истории нет. Случайный триггер с той же частотой = нулевая
модель: он показывает, что делает сама механика фиксации, и задаёт планку,
которую ордерфлоу-триггер обязан перебить (шаг 2 плана).
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scalp_swing_research import fetch  # noqa: E402  протокол загрузки баров

TAKER = 0.00055
HOUR_MS = 3_600_000
CORE_FAST, CORE_SLOW = 20, 50


def _sma(vals: list[float], window: int) -> float | None:
    if len(vals) < window:
        return None
    return statistics.fmean(vals[-window:])


def core_schedule(bars4h: list[tuple]) -> dict[int, int]:
    """{ts открытия 4h-бара: желаемое состояние ядра с этого открытия}.

    Сигнал считается на close бара i, исполняется на open бара i+1 — тот же
    протокол, что в scalp_swing_research.py.
    """
    closes = [b[4] for b in bars4h]
    out: dict[int, int] = {}
    for i in range(len(bars4h) - 1):
        fast = _sma(closes[:i + 1], CORE_FAST)
        slow = _sma(closes[:i + 1], CORE_SLOW)
        if fast is None or slow is None:
            continue
        out[bars4h[i + 1][0]] = 1 if fast > slow else 0
    return out


def fetch_funding(sess, symbol: str, start_ms: int) -> list[tuple[int, float]]:
    """Фактические ставки funding. Long платит при rate > 0.

    https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
    """
    out: dict[int, float] = {}
    end = int(time.time() * 1000)
    while True:
        try:
            rows = sess.get_funding_rate_history(
                category="linear", symbol=symbol, startTime=start_ms,
                endTime=end, limit=200)["result"]["list"]
        except Exception:
            break
        if not rows:
            break
        oldest = end
        for r in rows:
            ts = int(r["fundingRateTimestamp"])
            out[ts] = float(r["fundingRate"])
            oldest = min(oldest, ts)
        if oldest <= start_ms or len(rows) < 200:
            break
        end = oldest - 1
    return [(ts, out[ts]) for ts in sorted(out)]


def _atr(bars: list[tuple], idx: int, length: int = 14) -> float | None:
    if idx < length:
        return None
    trs = []
    for j in range(idx - length + 1, idx + 1):
        _, _, hi, lo, cl = bars[j]
        prev = bars[j - 1][4]
        trs.append(max(hi - lo, abs(hi - prev), abs(lo - prev)))
    return statistics.fmean(trs)


class Trigger:
    """Механический генератор моментов фиксации.

    `random` — нулевая модель: та же частота, случайные моменты.
    `atr_trail` / `pct_trail` — практические baseline'ы для шага 2.
    """

    def __init__(self, kind: str, param: float, seed: int = 0):
        self.kind = kind
        self.param = param
        self.rng = random.Random(seed)
        self.peak = 0.0

    def on_entry(self, price: float) -> None:
        self.peak = price

    def fires(self, bars: list[tuple], i: int) -> bool:
        close = bars[i][4]
        self.peak = max(self.peak, close)
        if self.kind == "random":
            return self.rng.random() < self.param / 24.0
        if self.kind == "pct_trail":
            return close <= self.peak * (1.0 - self.param / 100.0)
        if self.kind == "atr_trail":
            atr = _atr(bars, i)
            return atr is not None and close <= self.peak - self.param * atr
        raise ValueError(f"неизвестный триггер {self.kind}")


def simulate(bars: list[tuple], sched: dict[int, int],
             funding: list[tuple[int, float]], *, equity: float, frac: float,
             trigger: Trigger | None, flat_hours: float) -> dict:
    """Один прогон по барам исполнения. `trigger=None` = ветвь холда.

    Equity держится постоянным намеренно: иначе результат по режимам зависит от
    порядка эпизодов (компаундинг), и вклады эпизодов нельзя складывать. Просадка
    лота в тренде при этом сохраняется — она берётся из роста ЦЕНЫ (лот =
    frac*equity/цена), а не из роста счёта.
    """
    fund_map = {ts: rate for ts, rate in funding}
    want = 0
    qty = 0.0
    anchor = 0.0
    flat_until = 0
    realized = 0.0
    fees = 0.0
    funding_paid = 0.0
    legs = 0
    fixations = 0
    episodes: list[dict] = []
    cur: dict | None = None

    def open_pos(price: float, ts: int) -> None:
        nonlocal qty, anchor, fees, legs
        qty = equity * frac / price
        anchor = price
        fee = qty * price * TAKER
        fees += fee
        legs += 1
        if trigger is not None:
            trigger.on_entry(price)
        if cur is not None:
            cur["fees"] += fee
            cur["legs"] += 1
        _ = ts

    def close_pos(price: float) -> float:
        nonlocal qty, realized, fees, legs
        gross = (price - anchor) * qty
        fee = qty * price * TAKER
        realized += gross
        fees += fee
        legs += 1
        if cur is not None:
            cur["gross"] += gross
            cur["fees"] += fee
            cur["legs"] += 1
        qty = 0.0
        return gross

    for i, (ts, op, _hi, _lo, cl) in enumerate(bars):
        # ── смена состояния ядра: на открытии 4h-бара исполнения ──────────
        if ts in sched:
            new_want = sched[ts]
            if new_want == 1 and want == 0:
                want = 1
                cur = {"ts_open": ts, "entry": op, "gross": 0.0, "fees": 0.0,
                       "funding": 0.0, "legs": 0, "fixations": 0}
                open_pos(op, ts)
            elif new_want == 0 and want == 1:
                if qty > 0:
                    close_pos(op)
                want = 0
                if cur is not None:
                    cur["ts_close"] = ts
                    cur["exit"] = op
                    episodes.append(cur)
                    cur = None
                flat_until = 0

        # ── funding: платит только открытая позиция ────────────────────────
        rate = fund_map.get(ts)
        if rate is not None and qty > 0:
            pay = rate * qty * cl
            funding_paid += pay
            if cur is not None:
                cur["funding"] += pay

        if want != 1:
            continue

        # ── фиксация и перезаход ──────────────────────────────────────────
        if qty > 0 and trigger is not None and trigger.fires(bars, i):
            close_pos(cl)
            fixations += 1
            if cur is not None:
                cur["fixations"] += 1
            flat_until = ts + int(flat_hours * HOUR_MS)
            if flat_hours <= 0:
                open_pos(cl, ts)
        elif qty == 0 and ts >= flat_until:
            open_pos(cl, ts)

    # ── незакрытый хвост: обе ветви закрываем по последней цене ───────────
    if qty > 0:
        close_pos(bars[-1][4])
        if cur is not None:
            cur["ts_close"] = bars[-1][0]
            cur["exit"] = bars[-1][4]
            episodes.append(cur)

    return {
        "gross": realized, "fees": fees, "funding": funding_paid,
        "net": realized - fees - funding_paid,
        "legs": legs, "fixations": fixations, "episodes": episodes,
    }


def regime_of(bars: list[tuple], ts: int, lookback_days: int = 30,
              index: dict[int, int] | None = None) -> str:
    """Режим ДО начала эпизода: доходность за предыдущие N дней.

    Классификация ex-ante, чтобы не подглядывать в исход эпизода. `index`
    (ts → номер бара) обязателен для скорости: без него функция линейно
    сканирует бары, а вызывается она на каждый эпизод каждого сида.
    """
    idx = index.get(ts) if index is not None else None
    if idx is None:
        for i, b in enumerate(bars):
            if b[0] >= ts:
                idx = i
                break
    if idx is None:
        return "n/a"
    back = idx - lookback_days * 24
    if back < 0:
        return "n/a"
    ret = bars[idx][4] / bars[back][4] - 1.0
    if ret > 0.05:
        return "тренд вверх"
    if ret < -0.05:
        return "тренд вниз"
    return "чоп"


def episode_delta(hold: dict, cont: dict, bars: list[tuple], *,
                  index: dict[int, int] | None = None,
                  reg_cache: dict[int, str] | None = None) -> dict:
    """Разница контур − холд по эпизодам ядра, с разбивкой по режимам.

    Границы эпизодов у ветвей совпадают (правило ядра одно и то же), поэтому
    сопоставление идёт по времени открытия. `reg_cache` переиспользуется между
    сидами: режим зависит только от даты старта эпизода.
    """
    h_by_ts = {e["ts_open"]: e for e in hold["episodes"]}
    buckets: dict[str, dict] = {}
    for ce in cont["episodes"]:
        he = h_by_ts.get(ce["ts_open"])
        if he is None:
            continue
        ts0 = ce["ts_open"]
        if reg_cache is not None and ts0 in reg_cache:
            reg = reg_cache[ts0]
        else:
            reg = regime_of(bars, ts0, index=index)
            if reg_cache is not None:
                reg_cache[ts0] = reg
        b = buckets.setdefault(reg, {"n": 0, "fix": 0, "hold": 0.0,
                                     "cont": 0.0, "deltas": []})
        h_net = he["gross"] - he["fees"] - he["funding"]
        c_net = ce["gross"] - ce["fees"] - ce["funding"]
        b["n"] += 1
        b["fix"] += ce["fixations"]
        b["hold"] += h_net
        b["cont"] += c_net
        b["deltas"].append(c_net - h_net)
    return buckets


def _fmt_bucket(name: str, b: dict) -> str:
    d = b["deltas"]
    mean = statistics.fmean(d) if d else 0.0
    win = 100 * sum(1 for x in d if x > 0) / len(d) if d else 0.0
    return (f"    {name:<12} n={b['n']:<4} фикс={b['fix']:<5} "
            f"холд ${b['hold']:>9.0f}  контур ${b['cont']:>9.0f}  "
            f"Δ ${b['cont'] - b['hold']:>+9.0f}  "
            f"Δ/эпизод ${mean:>+8.1f}  эпизодов в плюс {win:.0f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="ETHUSDT,BTCUSDT,SOLUSDT")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--frac", type=float, default=0.15)
    ap.add_argument("--lam", type=float, default=3.6,
                    help="частота случайных фиксаций в день (live: 6 за ~40ч)")
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--flat-hours", default="0,1,4,12,24")
    args = ap.parse_args()

    from pybit.unified_trading import HTTP
    sess = HTTP()
    start = int((time.time() - args.days * 86400) * 1000)
    flats = [float(x) for x in args.flat_hours.split(",") if x.strip()]

    # (триггер, параметр, пауза) → {символ: Δ}. Нужно чтобы не выхватить одну
    # удачную ячейку из тридцати: в сетке 2×3×5 всегда найдётся плюсовая.
    grid: dict[tuple, dict[str, float]] = {}

    print(f"H-HYBRID шаг 1 | ядро sma20_50_4h frac={args.frac} "
          f"equity=${args.equity:,.0f} | taker {TAKER*100:.3f}%/нога | "
          f"{args.days}д | случайный триггер λ={args.lam}/сут, "
          f"{args.seeds} сидов")
    print("метрика: net = gross − комиссии − funding; Δ = контур − холд\n")

    for symbol in [s.strip().upper() for s in args.symbols.split(",")
                   if s.strip()]:
        bars1h = fetch(sess, symbol, "60", start)
        bars4h = fetch(sess, symbol, "240", start)
        if len(bars4h) < CORE_SLOW + 5 or not bars1h:
            print(f"=== {symbol}: мало данных, пропуск ===\n")
            continue
        funding = fetch_funding(sess, symbol, start)
        sched = core_schedule(bars4h)
        idx = {b[0]: i for i, b in enumerate(bars1h)}
        reg_cache: dict[int, str] = {}

        hold = simulate(bars1h, sched, funding, equity=args.equity,
                        frac=args.frac, trigger=None, flat_hours=0.0)
        first = datetime.fromtimestamp(bars1h[0][0] / 1000, timezone.utc)
        last = datetime.fromtimestamp(bars1h[-1][0] / 1000, timezone.utc)
        print(f"=== {symbol} | {first:%Y-%m-%d} → {last:%Y-%m-%d} | "
              f"1h баров {len(bars1h)}, funding-начислений {len(funding)} ===")
        print(f"  ХОЛД ЯДРА: net ${hold['net']:,.0f} "
              f"(gross ${hold['gross']:,.0f}, комиссии ${hold['fees']:,.0f} "
              f"на {hold['legs']} ногах, funding ${hold['funding']:,.0f}), "
              f"эпизодов {len(hold['episodes'])}")

        for flat in flats:
            print(f"\n  ── пауза вне рынка после фиксации: {flat:g} ч ──")

            nets, deltas, fixes = [], [], []
            agg: dict[str, dict] = {}
            for seed in range(args.seeds):
                trig = Trigger("random", args.lam, seed=seed)
                cont = simulate(bars1h, sched, funding, equity=args.equity,
                                frac=args.frac, trigger=trig, flat_hours=flat)
                nets.append(cont["net"])
                deltas.append(cont["net"] - hold["net"])
                fixes.append(cont["fixations"])
                for name, b in episode_delta(hold, cont, bars1h, index=idx,
                                             reg_cache=reg_cache).items():
                    a = agg.setdefault(name, {"n": 0, "fix": 0, "hold": 0.0,
                                              "cont": 0.0, "deltas": []})
                    a["n"] += b["n"]
                    a["fix"] += b["fix"]
                    a["hold"] += b["hold"] / args.seeds
                    a["cont"] += b["cont"] / args.seeds
                    a["deltas"].extend(b["deltas"])
            nets.sort()
            deltas.sort()
            lo = deltas[int(0.05 * len(deltas))]
            hi = deltas[min(len(deltas) - 1, int(0.95 * len(deltas)))]
            beat = 100 * sum(1 for d in deltas if d > 0) / len(deltas)
            print(f"    СЛУЧАЙНЫЙ триггер: фиксаций "
                  f"{statistics.fmean(fixes):.0f} в среднем, "
                  f"контур net ${statistics.fmean(nets):,.0f}, "
                  f"Δ ${statistics.fmean(deltas):+,.0f} "
                  f"[5%..95%: {lo:+,.0f}..{hi:+,.0f}], "
                  f"сидов лучше холда {beat:.1f}%")
            for name in sorted(agg):
                print(_fmt_bucket(name, agg[name]))

            for kind, params in (("pct_trail", (1.0, 2.0, 5.0)),
                                 ("atr_trail", (1.0, 2.0, 3.0))):
                for p in params:
                    cont = simulate(bars1h, sched, funding, equity=args.equity,
                                    frac=args.frac,
                                    trigger=Trigger(kind, p),
                                    flat_hours=flat)
                    unit = "%" if kind == "pct_trail" else "ATR"
                    delta = cont["net"] - hold["net"]
                    grid.setdefault((kind, p, flat), {})[symbol] = delta
                    print(f"    {kind} {p:g}{unit}: фиксаций "
                          f"{cont['fixations']:<4} контур net "
                          f"${cont['net']:,.0f}, "
                          f"Δ ${delta:+,.0f}")
        print()

    syms = sorted({s for cell in grid.values() for s in cell})
    if len(syms) > 1:
        print("=" * 78)
        print("Согласованность по символам (Δ контур − холд). Ячейка полезна "
              "только если\nзнак одинаков везде: одиночный плюс в сетке 2×3×5 "
              "— это подгонка.\n")
        head = "  ".join(f"{s:>10}" for s in syms)
        print(f"  {'триггер':<20} {'пауза':>6}  {head}   вердикт")
        agree_pos = 0
        for (kind, p, flat), cell in sorted(grid.items()):
            vals = [cell.get(s) for s in syms]
            if any(v is None for v in vals):
                continue
            body = "  ".join(f"{v:>+10.0f}" for v in vals)
            if all(v > 0 for v in vals):
                verdict = "плюс везде"
                agree_pos += 1
            elif all(v < 0 for v in vals):
                verdict = "минус везде"
            else:
                verdict = "знак не сходится"
            print(f"  {kind + ' ' + format(p, 'g'):<20} {flat:>5g}ч  {body}"
                  f"   {verdict}")
        print(f"\n  ячеек с плюсом на ВСЕХ символах: {agree_pos} "
              f"из {len(grid)}")
        print()

    print("=" * 78)
    print("Гейт §8.1 считается пройденным только если контур обгоняет холд "
          "net-of-fees\nна ≥100 фиксациях, ≥2 недели, минимум в двух режимах.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
