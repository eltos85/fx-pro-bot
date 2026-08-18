"""Дневной тренд: сделка на часы, не на недели. Канон, VIP 0, без подбора.

Зачем
─────
Минутный скальп на VIP 0 закрыт (комиссия 0.110% ≈ треть 0.3%-стопа).
Недельный Turtle/SMA200 жив, но лот висит неделями — пользователь это
отмёл. Остаётся средний слой, который форумы и гайды Bybit называют
«дневной торговлей»: уклон с дневного графика, вход на 15m/1h, выход
в тот же UTC-день или по 2R. Цель хода 1%+, комиссия становится мелочью.

Правила ЗАФИКСИРОВАНЫ до прогона (no-data-fitting.mdc):

  A. UTC ORB + дневной уклон
     Коробка — первые 15 или 60 минут UTC-дня (крипто-аналог 15-мин ORB:
     John Carter 2012 ch.7; Al Brooks 2012 ch.5). Уклон — вчерашний close
     выше вчерашнего open (простейший day-trend filter, Murphy 1999).
     Вход только по уклону, на закрытии 15m-бара ЗА коробкой, исполнение
     на открытии следующего. Стоп — противоположная сторона коробки.
     Выход: 2R (Carter) ИЛИ конец UTC-дня, что раньше. Одна сделка в день.

  B. NY-сессия ORB (13:30–14:30 UTC)
     Тот же ORB, но якорь — открытие американских площадок. ICT/SMC-крипто
     сообщество использует 9:30 ET как kill zone; здесь это не ICT-вход,
     а только сдвиг коробки на час наибольшей ликвидности.

  C. 4h Donchian 20/10 long/flat
     Короткий Turtle на 4h (Dennis/Eckhardt; Olanipekun 2026: 4h-тренд
     бьёт buy-and-hold). Печатаем срок удержания отдельно: если медиана
     уйдёт в дни — это уже не «дневной» кандидат.

Издержка: VIP 0 taker 0.055% × 2 = 0.110%
(https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate).
Без плеча. Сигнал на close, сделка на следующем open.

Критерий приёмки (до прогона), на BTC:
  1) n ≥ 100;
  2) средний PnL после комиссии > 0 и CI не накрывает ноль;
  3) знак среднего совпал в первой и второй половине выборки;
  4) медиана удержания ≤ 24 часов (иначе это не дневной тренд).
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

TAKER = 0.00055
RT = 2 * TAKER
UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
BAR_MIN = 15
BAR_MS = BAR_MIN * 60 * 1000
DAY_MS = 86400 * 1000


def fetch_klines(sess, symbol: str, interval: str, start_ms: int):
    out: dict[int, tuple[float, float, float, float]] = {}
    end = int(time.time() * 1000)
    while True:
        try:
            rows = sess.get_kline(
                category="linear", symbol=symbol, interval=interval,
                start=start_ms, end=end, limit=1000,
            )["result"]["list"]
        except Exception:
            break
        if not rows:
            break
        oldest = end
        for r in rows:
            ts = int(r[0])
            out[ts] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            oldest = min(oldest, ts)
        if len(rows) < 1000 or oldest <= start_ms:
            break
        end = oldest - 1
    return [(ts, *out[ts]) for ts in sorted(out)]


def _stats(pnls: list[float], holds_h: list[float], trades: int) -> dict:
    n = len(pnls)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(pnls)
    sd = statistics.stdev(pnls) if n > 1 else 0.0
    se = sd / math.sqrt(n)
    wr = 100 * sum(1 for x in pnls if x > 0) / n
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for x in pnls:
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return {
        "n": n, "mean_pct": mean * 100, "median_pct": statistics.median(pnls) * 100,
        "wr": wr, "total_pct": (eq - 1) * 100, "mdd_pct": mdd * 100,
        "ci_lo": (mean - 1.96 * se) * 100, "ci_hi": (mean + 1.96 * se) * 100,
        "hold_med_h": statistics.median(holds_h) if holds_h else 0.0,
        "hold_avg_h": statistics.mean(holds_h) if holds_h else 0.0,
        "trades": trades, "pnls": pnls,
    }


def orb_day(bars15, box_min: int, session_start_min: int, fee: float) -> dict:
    """ORB внутри UTC-дня. session_start_min — минуты от полуночи UTC."""
    by_day: dict[int, list] = {}
    for row in bars15:
        day = row[0] - (row[0] % DAY_MS)
        by_day.setdefault(day, []).append(row)
    days = sorted(by_day)
    pnls, holds = [], []
    for i, day in enumerate(days):
        if i == 0:
            continue
        prev = by_day[days[i - 1]]
        if not prev:
            continue
        # уклон: вчера close vs open (сумма дня)
        y_open, y_close = prev[0][1], prev[-1][4]
        bias = 1 if y_close > y_open else -1
        today = by_day[day]
        box_start = day + session_start_min * 60 * 1000
        box_end = box_start + box_min * 60 * 1000
        box = [r for r in today if box_start <= r[0] < box_end]
        if len(box) < max(1, box_min // BAR_MIN - 1):
            continue
        hi = max(r[2] for r in box)
        lo = min(r[3] for r in box)
        if hi <= lo:
            continue
        after = [r for r in today if r[0] >= box_end]
        if len(after) < 3:
            continue
        entry_side = entry_px = entry_ts = None
        sl = tp = None
        for j, r in enumerate(after[:-1]):
            ts, o, h, l, c = r
            if entry_side is None:
                if bias == 1 and c > hi:
                    entry_side, entry_px = 1, after[j + 1][1]
                    entry_ts = after[j + 1][0]
                    sl = lo
                    risk = entry_px - sl
                    if risk <= 0:
                        entry_side = None
                        continue
                    tp = entry_px + 2 * risk
                    break
                if bias == -1 and c < lo:
                    entry_side, entry_px = -1, after[j + 1][1]
                    entry_ts = after[j + 1][0]
                    sl = hi
                    risk = sl - entry_px
                    if risk <= 0:
                        entry_side = None
                        continue
                    tp = entry_px - 2 * risk
                    break
        if entry_side is None or entry_px is None:
            continue
        # путь после входа до конца дня
        path = [r for r in today if r[0] >= entry_ts]
        if len(path) < 2:
            continue
        exit_px, exit_ts = path[-1][4], path[-1][0]
        for r in path[1:]:
            ts, o, h, l, c = r
            if entry_side == 1:
                if l <= sl:
                    exit_px, exit_ts = sl, ts
                    break
                if h >= tp:
                    exit_px, exit_ts = tp, ts
                    break
            else:
                if h >= sl:
                    exit_px, exit_ts = sl, ts
                    break
                if l <= tp:
                    exit_px, exit_ts = tp, ts
                    break
        ret = entry_side * (exit_px / entry_px - 1) - fee
        pnls.append(ret)
        holds.append((exit_ts - entry_ts) / 3600000)
    return _stats(pnls, holds, len(pnls))


def donchian_4h(bars4, entry_n: int, exit_n: int, fee: float) -> dict:
    highs = [h for _, _, h, _, _ in bars4]
    lows = [lo for _, _, _, lo, _ in bars4]
    opens = [o for _, o, _, _, _ in bars4]
    closes = [c for _, _, _, _, c in bars4]
    ts = [t for t, _, _, _, _ in bars4]
    pos = 0
    entry_i = None
    pnls, holds = [], []
    i = max(entry_n, exit_n) + 1
    while i + 1 < len(bars4):
        eh = max(highs[i - entry_n:i])
        xl = min(lows[i - exit_n:i])
        want = pos
        if pos == 0 and closes[i] > eh:
            want = 1
        elif pos == 1 and closes[i] < xl:
            want = 0
        if want == 1 and pos == 0:
            entry_i = i + 1
        if want == 0 and pos == 1 and entry_i is not None:
            px0, px1 = opens[entry_i], opens[i + 1]
            pnls.append(px1 / px0 - 1 - fee)
            holds.append((ts[i + 1] - ts[entry_i]) / 3600000)
            entry_i = None
        pos = want
        i += 1
    return _stats(pnls, holds, len(pnls))


def split_agree(pnls: list[float]) -> tuple[str, float, float]:
    if len(pnls) < 20:
        return "нет", 0.0, 0.0
    mid = len(pnls) // 2
    a, b = statistics.mean(pnls[:mid]), statistics.mean(pnls[mid:])
    return ("ДА" if (a > 0) == (b > 0) else "нет"), a * 100, b * 100


def row(name, r) -> None:
    if r.get("n", 0) < 5:
        print(f"  {name:<28} n={r.get('n', 0)}  мало")
        return
    agree, a, b = split_agree(r["pnls"])
    ci = f"[{r['ci_lo']:+.3f}; {r['ci_hi']:+.3f}]"
    print(f"  {name:<28} n={r['n']:<5} ср={r['mean_pct']:+.3f}%  "
          f"мед={r['median_pct']:+.3f}%  WR={r['wr']:.0f}%  "
          f"итог={r['total_pct']:+.1f}%  DD={r['mdd_pct']:.1f}%  "
          f"держ.мед={r['hold_med_h']:.1f}ч ср={r['hold_avg_h']:.1f}ч  "
          f"CI{ci}  IS/OOS {agree} ({a:+.3f}/{b:+.3f})")


def accepted(r: dict) -> tuple[bool, str]:
    if r.get("n", 0) < 100:
        return False, "n<100"
    if r["mean_pct"] <= 0:
        return False, "средний ≤ 0"
    if r["ci_lo"] <= 0:
        return False, "ноль в CI"
    agree, _, _ = split_agree(r["pnls"])
    if agree != "ДА":
        return False, "IS/OOS разошлись"
    if r["hold_med_h"] > 24:
        return False, "медиана удержания > 24ч"
    return True, "принят"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    args = ap.parse_args()
    from pybit.unified_trading import HTTP
    sess = HTTP()
    start = int((time.time() - args.days * 86400) * 1000)
    print(f"VIP 0 taker RT {RT * 100:.3f}%, история {args.days}д, бар 15м / 4ч")
    print("критерий BTC: n≥100, ср>0, CI без нуля, IS/OOS один знак, медиана ≤24ч\n")

    btc_verdicts = []
    for sym in UNIVERSE:
        print(f"=== {sym} ===")
        b15 = fetch_klines(sess, sym, "15", start)
        b4 = fetch_klines(sess, sym, "240", start)
        print(f"  баров 15м={len(b15)}  4ч={len(b4)}")
        tests = [
            ("A UTC ORB 15м + уклон", orb_day(b15, 15, 0, RT)),
            ("A UTC ORB 60м + уклон", orb_day(b15, 60, 0, RT)),
            ("B NY ORB 60м 13:30", orb_day(b15, 60, 13 * 60 + 30, RT)),
            ("C 4h Turtle 20/10", donchian_4h(b4, 20, 10, RT)),
        ]
        for name, r in tests:
            row(name, r)
            if sym == "BTCUSDT":
                ok, why = accepted(r)
                btc_verdicts.append((name, ok, why))
        print()

    print("=" * 100)
    print("вердикт по BTC (критерий задан до прогона):")
    any_ok = False
    for name, ok, why in btc_verdicts:
        print(f"  {name:<28} {'ПРИНЯТ' if ok else 'закрыт'} — {why}")
        any_ok = any_ok or ok
    print(f"ИТОГ: {'есть дневной кандидат' if any_ok else 'дневного кандидата нет'}")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
