#!/usr/bin/env python3
"""Swing Daily RSI-2 mean-reversion (Larry Connors 2005).

Вариант A (classic Connors):
  • trend filter: close > SMA(200) — торгуем только LONG; < — только SHORT
  • entry: RSI(2) < 10  (long) или > 90 (short)
  • exit: close > SMA(5) (для long) / close < SMA(5) (для short)

Вариант B (agressive, no trend filter):
  • entry: RSI(2) < 5  → long;  RSI(2) > 95 → short
  • exit: same SMA(5)

Stop-loss: 2×ATR(14).  Max hold: 10 days.

Daily bars resampled from M5.
Тест: IS 60%, OOS 40%.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scalp_setups_m5 import (  # noqa: E402
    atr,
    cost_pips,
    load,
    PIP_SIZE,
)


SYMS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X",
    "AUDUSD=X", "USDCAD=X", "USDCHF=X",
    "EURGBP=X", "EURJPY=X", "GBPJPY=X",
    "GC=F", "CL=F", "BZ=F",
]

IS_FRAC = 0.6


# ──────────────────── Resample M5 → Daily ────────────────────

def resample_daily(ts_m5: np.ndarray, o, h, l, c):
    """M5 → Daily (UTC 00:00 boundaries)."""
    day_ms = 86400 * 1000
    ts0 = int(ts_m5[0] // day_ms) * day_ms
    ts_day = np.arange(ts0, int(ts_m5[-1]) + day_ms, day_ms, dtype=np.int64)
    nd = len(ts_day)
    od = np.full(nd, np.nan)
    hd = np.full(nd, np.nan)
    ld = np.full(nd, np.nan)
    cd = np.full(nd, np.nan)
    buckets = ((ts_m5 - ts0) // day_ms).astype(np.int64)
    buckets = np.clip(buckets, 0, nd - 1)
    prev_b = -1
    for i in range(len(ts_m5)):
        b = int(buckets[i])
        if b != prev_b:
            if np.isnan(od[b]):
                od[b] = o[i]
                hd[b] = h[i]
                ld[b] = l[i]
            prev_b = b
        hd[b] = max(hd[b], h[i]) if not np.isnan(hd[b]) else h[i]
        ld[b] = min(ld[b], l[i]) if not np.isnan(ld[b]) else l[i]
        cd[b] = c[i]
    mask = ~np.isnan(cd)
    return ts_day[mask], od[mask], hd[mask], ld[mask], cd[mask]


# ──────────────────── RSI ────────────────────

def rsi(c: np.ndarray, n: int = 2) -> np.ndarray:
    dc = np.diff(c, prepend=c[0])
    up = np.where(dc > 0, dc, 0.0)
    dn = np.where(dc < 0, -dc, 0.0)
    avg_up = np.full(len(c), np.nan)
    avg_dn = np.full(len(c), np.nan)
    if len(c) < n + 1:
        return avg_up
    avg_up[n] = up[1:n+1].mean()
    avg_dn[n] = dn[1:n+1].mean()
    for i in range(n + 1, len(c)):
        avg_up[i] = (avg_up[i-1] * (n-1) + up[i]) / n
        avg_dn[i] = (avg_dn[i-1] * (n-1) + dn[i]) / n
    rs = avg_up / np.where(avg_dn == 0, 1e-10, avg_dn)
    return 100 - 100 / (1 + rs)


def sma(x: np.ndarray, n: int) -> np.ndarray:
    s = np.full_like(x, np.nan, dtype=np.float64)
    if len(x) < n:
        return s
    cum = np.cumsum(x)
    s[n-1:] = (cum[n-1:] - np.concatenate(([0], cum[:-n]))) / n
    return s


# ──────────────────── Backtest ────────────────────

def backtest_rsi2(sym: str, mode: str, ts_start: int, ts_end: int) -> dict:
    """mode = 'classic' (trend+RSI<10/>90) or 'aggressive' (no trend+RSI<5/>95)."""
    bars = load(sym)
    ts_d, o_d, h_d, l_d, c_d = resample_daily(bars.ts, bars.o, bars.h, bars.l, bars.c)

    r2 = rsi(c_d, 2)
    sma5 = sma(c_d, 5)
    sma200 = sma(c_d, 200)
    a_d = atr(h_d, l_d, c_d, 14)
    pip = PIP_SIZE[sym]
    cost = cost_pips(sym)
    max_hold = 10

    trades = []
    in_trade = False
    direction = 0
    entry_i = 0
    entry_price = 0.0
    sl = 0.0

    for i in range(200, len(c_d) - 1):
        if ts_d[i] < ts_start or ts_d[i] >= ts_end:
            continue
        if in_trade:
            # Exit: SL hit intrabar
            hi, lo = h_d[i], l_d[i]
            exited = False
            if direction == 1 and lo <= sl:
                exit_price = sl
                exit_reason = "SL"
                exited = True
            elif direction == -1 and hi >= sl:
                exit_price = sl
                exit_reason = "SL"
                exited = True
            # Exit: SMA(5) cross at close
            elif direction == 1 and c_d[i] > sma5[i]:
                exit_price = c_d[i]
                exit_reason = "SMA5"
                exited = True
            elif direction == -1 and c_d[i] < sma5[i]:
                exit_price = c_d[i]
                exit_reason = "SMA5"
                exited = True
            elif i - entry_i >= max_hold:
                exit_price = c_d[i]
                exit_reason = "TIME"
                exited = True
            if exited:
                pnl_g = direction * (exit_price - entry_price) / pip
                pnl_n = pnl_g - cost
                trades.append({
                    "sym": sym,
                    "entry_ts": int(ts_d[entry_i]),
                    "exit_ts": int(ts_d[i]),
                    "direction": direction,
                    "entry": entry_price,
                    "exit": exit_price,
                    "pnl_gross": pnl_g,
                    "pnl_net": pnl_n,
                    "cost": cost,
                    "bars_held": i - entry_i,
                    "reason": exit_reason,
                })
                in_trade = False
            continue

        # Entry logic
        if np.isnan(r2[i]) or np.isnan(sma5[i]) or np.isnan(a_d[i]) or a_d[i] <= 0:
            continue
        go_long = False
        go_short = False
        if mode == "classic":
            if np.isnan(sma200[i]):
                continue
            if c_d[i] > sma200[i] and r2[i] < 10:
                go_long = True
            elif c_d[i] < sma200[i] and r2[i] > 90:
                go_short = True
        elif mode == "aggressive":
            if r2[i] < 5:
                go_long = True
            elif r2[i] > 95:
                go_short = True

        if go_long:
            entry_price = o_d[i+1]
            sl = entry_price - 2.0 * a_d[i]
            direction = 1
            in_trade = True
            entry_i = i + 1
        elif go_short:
            entry_price = o_d[i+1]
            sl = entry_price + 2.0 * a_d[i]
            direction = -1
            in_trade = True
            entry_i = i + 1

    if not trades:
        return {"sym": sym, "n": 0, "net": 0, "wr": 0, "pf": 0, "avg": 0, "trades": []}
    nets = [t["pnl_net"] for t in trades]
    total = sum(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0)
    return {
        "sym": sym, "n": len(trades), "net": total,
        "wr": len(wins) / len(trades) * 100,
        "pf": pf, "avg": total / len(trades),
        "avg_hold_days": np.mean([t["bars_held"] for t in trades]),
        "trades": trades,
    }


def main() -> None:
    sample = load("GBPJPY=X")
    ts_start = int(sample.ts[0])
    ts_end = int(sample.ts[-1])
    is_end = ts_start + int((ts_end - ts_start) * IS_FRAC)

    for mode in ("classic", "aggressive"):
        print("=" * 100)
        print(f"Swing Daily RSI-2 — mode={mode}")
        print(f"  IS 60% / OOS 40%. IS_end={datetime.fromtimestamp(is_end/1000, tz=UTC).date()}")
        print("=" * 100)
        header = f"  {'SYM':<10}{'IS_n':>6}{'IS_net':>9}{'IS_wr':>7}{'IS_pf':>7}  {'OOS_n':>6}{'OOS_net':>9}{'OOS_wr':>7}{'OOS_pf':>7}{'Avg':>8}"
        print(header)
        print("─" * len(header))
        all_oos = []
        all_is = []
        for sym in SYMS:
            is_r = backtest_rsi2(sym, mode, ts_start, is_end)
            oos_r = backtest_rsi2(sym, mode, is_end, ts_end)
            all_is.extend(is_r["trades"])
            all_oos.extend(oos_r["trades"])
            print(
                f"  {sym:<10}"
                f"{is_r['n']:>6}{is_r['net']:>+9.0f}{is_r['wr']:>6.1f}%{is_r['pf']:>7.2f}  "
                f"{oos_r['n']:>6}{oos_r['net']:>+9.0f}{oos_r['wr']:>6.1f}%{oos_r['pf']:>7.2f}"
                f"{oos_r['avg']:>+8.2f}"
            )
        print("─" * len(header))
        def p(trades, name):
            if not trades:
                print(f"  {name}: none")
                return
            nets = [t["pnl_net"] for t in trades]
            total = sum(nets)
            wins = [x for x in nets if x > 0]
            losses = [x for x in nets if x < 0]
            pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
            wr = len(wins) / len(trades) * 100
            print(f"  {name}: n={len(trades)} net={total:+.0f} avg={total/len(trades):+.2f} WR={wr:.1f}% PF={pf:.2f}")
        p(all_is, "IS")
        p(all_oos, "OOS")
        if all_oos:
            rng = np.random.default_rng(42)
            gross = np.asarray([t["pnl_gross"] for t in all_oos])
            costs = np.asarray([t["cost"] for t in all_oos])
            obs = float(sum(t["pnl_net"] for t in all_oos))
            ge = sum(1 for _ in range(1000) if (gross * rng.choice([-1, 1], size=len(all_oos)) - costs).sum() >= obs)
            p_val = (ge + 1) / 1001
            print(f"  Permutation OOS p-value: {p_val:.4f}")
        print()


if __name__ == "__main__":
    main()
