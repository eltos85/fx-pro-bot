"""Свинг: удержание дни, не часы и не месяцы. Канон, VIP 0, без подбора.

Свинг в практике (HyprSwarm: медиана элитных свинг-трейдеров ~2.7 дня;
Coinquant: 4h–daily, несколько сделок в месяц). Комиссия VIP 0 0.110%
на таком ходе уже не треть риска.

Правила зафиксированы до прогона:
  1) 4h Donchian 20/10 long/flat — Turtle System 1 на 4h
     (Dennis/Eckhardt; Olanipekun 2026: 4h-тренд).
  2) 4h Donchian 10/5 — более короткий канал, тот же канон.
  3) 4h SMA 20/50 long/flat — классический свинг-кросс (Murphy 1999).
  4) Daily SMA50 long/flat — более быстрый режимный фильтр, чем SMA200.

Сигнал на close, сделка на следующем open. Издержка 0.110% taker RT.
https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate

Критерий приёмки на BTC (до прогона):
  n ≥ 30 (свинг редко стреляет; 100 за 2 года нереалистично),
  средний PnL > 0, знак совпал в IS/OOS,
  медиана удержания от 24ч до 10 суток (это и есть свинг).
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


def fetch(sess, symbol: str, interval: str, start_ms: int):
    out, end = {}, int(time.time() * 1000)
    while True:
        try:
            rows = sess.get_kline(category="linear", symbol=symbol,
                                  interval=interval, start=start_ms,
                                  end=end, limit=1000)["result"]["list"]
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


def _pack(pnls, holds_h):
    n = len(pnls)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(pnls)
    sd = statistics.stdev(pnls) if n > 1 else 0.0
    se = sd / math.sqrt(n)
    eq = peak = 1.0
    mdd = 0.0
    for x in pnls:
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    mid = n // 2
    a, b = statistics.mean(pnls[:mid]), statistics.mean(pnls[mid:])
    return {
        "n": n, "mean": mean * 100, "med": statistics.median(pnls) * 100,
        "wr": 100 * sum(1 for x in pnls if x > 0) / n,
        "tot": (eq - 1) * 100, "mdd": mdd * 100,
        "ci_lo": (mean - 1.96 * se) * 100, "ci_hi": (mean + 1.96 * se) * 100,
        "hmed": statistics.median(holds_h), "havg": statistics.mean(holds_h),
        "is": a * 100, "oos": b * 100,
        "agree": (a > 0) == (b > 0),
    }


def donchian(bars, en, ex, fee):
    hi = [h for _, _, h, _, _ in bars]
    lo = [l for _, _, _, l, _ in bars]
    op = [o for _, o, _, _, _ in bars]
    cl = [c for _, _, _, _, c in bars]
    ts = [t for t, _, _, _, _ in bars]
    pos, ei, pnls, holds = 0, None, [], []
    i = max(en, ex) + 1
    while i + 1 < len(bars):
        eh, xl = max(hi[i - en:i]), min(lo[i - ex:i])
        want = pos
        if pos == 0 and cl[i] > eh:
            want = 1
        elif pos == 1 and cl[i] < xl:
            want = 0
        if want == 1 and pos == 0:
            ei = i + 1
        if want == 0 and pos == 1 and ei is not None:
            pnls.append(op[i + 1] / op[ei] - 1 - fee)
            holds.append((ts[i + 1] - ts[ei]) / 3600000)
            ei = None
        pos = want
        i += 1
    return _pack(pnls, holds)


def sma_cross(bars, fast, slow, fee):
    cl = [c for _, _, _, _, c in bars]
    op = [o for _, o, _, _, _ in bars]
    ts = [t for t, _, _, _, _ in bars]
    pos, ei, pnls, holds = 0, None, [], []
    i = slow
    while i + 1 < len(bars):
        sf = statistics.mean(cl[i - fast:i])
        ss = statistics.mean(cl[i - slow:i])
        want = 1 if sf > ss else 0
        if want == 1 and pos == 0:
            ei = i + 1
        if want == 0 and pos == 1 and ei is not None:
            pnls.append(op[i + 1] / op[ei] - 1 - fee)
            holds.append((ts[i + 1] - ts[ei]) / 3600000)
            ei = None
        pos = want
        i += 1
    return _pack(pnls, holds)


def sma_price(bars, window, fee):
    cl = [c for _, _, _, _, c in bars]
    op = [o for _, o, _, _, _ in bars]
    ts = [t for t, _, _, _, _ in bars]
    pos, ei, pnls, holds = 0, None, [], []
    i = window
    while i + 1 < len(bars):
        sma = statistics.mean(cl[i - window:i])
        want = 1 if cl[i] > sma else 0
        if want == 1 and pos == 0:
            ei = i + 1
        if want == 0 and pos == 1 and ei is not None:
            pnls.append(op[i + 1] / op[ei] - 1 - fee)
            holds.append((ts[i + 1] - ts[ei]) / 3600000)
            ei = None
        pos = want
        i += 1
    return _pack(pnls, holds)


def show(name, r):
    if r.get("n", 0) < 5:
        print(f"  {name:<24} n={r.get('n', 0)} мало")
        return
    agr = "ДА" if r["agree"] else "нет"
    print(f"  {name:<24} n={r['n']:<4} ср={r['mean']:+.3f}% мед={r['med']:+.3f}% "
          f"WR={r['wr']:.0f}% итог={r['tot']:+.1f}% DD={r['mdd']:.1f}% "
          f"держ.мед={r['hmed']/24:.1f}д ср={r['havg']/24:.1f}д "
          f"CI[{r['ci_lo']:+.3f}; {r['ci_hi']:+.3f}] "
          f"IS/OOS {agr} ({r['is']:+.3f}/{r['oos']:+.3f})")


def ok(r) -> tuple[bool, str]:
    if r.get("n", 0) < 30:
        return False, "n<30"
    if r["mean"] <= 0:
        return False, "средний ≤0"
    if not r["agree"]:
        return False, "IS/OOS"
    if not (24 <= r["hmed"] <= 240):
        return False, f"медиана {r['hmed']/24:.1f}д не в 1–10 сутках"
    return True, "принят"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    args = ap.parse_args()
    from pybit.unified_trading import HTTP
    sess = HTTP()
    start = int((time.time() - args.days * 86400) * 1000)
    print(f"VIP 0 RT {RT*100:.3f}%, {args.days}д")
    print("критерий BTC: n≥30, ср>0, IS/OOS один знак, медиана 1–10 суток\n")
    verdicts = []
    for sym in UNIVERSE:
        print(f"=== {sym} ===")
        b4 = fetch(sess, sym, "240", start)
        bd = fetch(sess, sym, "D", start)
        tests = [
            ("4h Turtle 20/10", donchian(b4, 20, 10, RT)),
            ("4h Turtle 10/5", donchian(b4, 10, 5, RT)),
            ("4h SMA 20/50", sma_cross(b4, 20, 50, RT)),
            ("Daily SMA50", sma_price(bd, 50, RT)),
        ]
        for name, r in tests:
            show(name, r)
            if sym == "BTCUSDT":
                verdicts.append((name, *ok(r)))
        print()
    print("=" * 90)
    any_ok = False
    for name, accepted, why in verdicts:
        print(f"  {name:<24} {'ПРИНЯТ' if accepted else 'закрыт'} — {why}")
        any_ok = any_ok or accepted
    print(f"ИТОГ: {'есть свинг-кандидат' if any_ok else 'свингового кандидата нет'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
