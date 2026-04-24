#!/usr/bin/env python3
"""Swing H4 Bollinger Squeeze + Trend filter (John Carter / TTM Squeeze).

Идея:
  • Squeeze ON: BB(20, 2σ) полностью внутри KC(20, 1.5×ATR) — сжатие волы
  • Signal: squeeze released = BB выходит из KC (upper-BB > upper-KC либо lower-BB < lower-KC)
  • Direction: close > SMA(50)  → long; < SMA(50) → short
  • Entry: на баре после release
  • SL:  2 × ATR(14)
  • Exit: SMA(50) cross ИЛИ time-stop 10 дней (60 H4 bars)

Тест: IS 60% / OOS 40%.  Permutation OOS.
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
from swing_turtle_h4 import resample_h4  # noqa: E402


SYMS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X",
    "AUDUSD=X", "USDCAD=X", "USDCHF=X",
    "EURGBP=X", "EURJPY=X", "GBPJPY=X",
    "GC=F", "CL=F", "BZ=F",
]

BB_N = 20
BB_K = 2.0
KC_N = 20
KC_MULT = 1.5
SMA_N = 50
ATR_STOP_MULT = 2.0
MAX_HOLD_BARS_H4 = 60   # 10 days × 6 H4
IS_FRAC = 0.6
MIN_SQUEEZE_BARS = 3     # сжатие должно держаться хотя бы 3 bars перед release


# ──────────────────── Indicators ────────────────────

def sma(x: np.ndarray, n: int) -> np.ndarray:
    s = np.full_like(x, np.nan, dtype=np.float64)
    if len(x) < n:
        return s
    cum = np.cumsum(x)
    s[n-1:] = (cum[n-1:] - np.concatenate(([0], cum[:-n]))) / n
    return s


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    s = np.full_like(x, np.nan, dtype=np.float64)
    for i in range(n - 1, len(x)):
        s[i] = x[i - n + 1:i + 1].std(ddof=0)
    return s


# ──────────────────── Backtest ────────────────────

def backtest_squeeze(sym: str, ts_start: int, ts_end: int) -> dict:
    bars = load(sym)
    ts4, o4, h4, l4, c4 = resample_h4(bars.ts, bars.o, bars.h, bars.l, bars.c)
    pip = PIP_SIZE[sym]
    cost = cost_pips(sym)

    mid = sma(c4, BB_N)
    std = rolling_std(c4, BB_N)
    bb_u = mid + BB_K * std
    bb_l = mid - BB_K * std
    a = atr(h4, l4, c4, 14)
    kc_mid = sma(c4, KC_N)
    kc_u = kc_mid + KC_MULT * a
    kc_l = kc_mid - KC_MULT * a
    ma50 = sma(c4, SMA_N)

    # squeeze mask: BB внутри KC
    squeeze = (bb_u < kc_u) & (bb_l > kc_l)
    # накопленное число bars подряд в squeeze
    squeeze_count = np.zeros(len(c4), dtype=np.int32)
    for i in range(1, len(c4)):
        squeeze_count[i] = squeeze_count[i-1] + 1 if squeeze[i] else 0

    trades = []
    i = SMA_N + 2
    while i < len(c4) - 1:
        if ts4[i] < ts_start or ts4[i] >= ts_end:
            i += 1
            continue
        # Release: squeeze был ON >= MIN_SQUEEZE_BARS на i-1, сейчас OFF
        if not squeeze[i] and squeeze_count[i-1] >= MIN_SQUEEZE_BARS:
            if np.isnan(ma50[i]) or np.isnan(a[i]) or a[i] <= 0:
                i += 1
                continue
            direction = 0
            if c4[i] > ma50[i] and c4[i] > bb_u[i-1]:
                direction = 1
            elif c4[i] < ma50[i] and c4[i] < bb_l[i-1]:
                direction = -1
            if direction == 0:
                i += 1
                continue
            entry_i = i + 1
            if entry_i >= len(c4):
                break
            entry_price = o4[entry_i]
            sl = entry_price - ATR_STOP_MULT * a[i] if direction == 1 else entry_price + ATR_STOP_MULT * a[i]

            exit_i = min(entry_i + MAX_HOLD_BARS_H4, len(c4) - 1)
            exit_price = c4[exit_i]
            exit_reason = "TIME"
            for j in range(entry_i, min(entry_i + MAX_HOLD_BARS_H4 + 1, len(c4))):
                hi, lo = h4[j], l4[j]
                if direction == 1 and lo <= sl:
                    exit_price, exit_reason, exit_i = sl, "SL", j
                    break
                if direction == -1 and hi >= sl:
                    exit_price, exit_reason, exit_i = sl, "SL", j
                    break
                # Exit: SMA50 cross at close
                if direction == 1 and j > entry_i and c4[j] < ma50[j]:
                    exit_price, exit_reason, exit_i = c4[j], "MA50", j
                    break
                if direction == -1 and j > entry_i and c4[j] > ma50[j]:
                    exit_price, exit_reason, exit_i = c4[j], "MA50", j
                    break

            pnl_g = direction * (exit_price - entry_price) / pip
            pnl_n = pnl_g - cost
            trades.append({
                "sym": sym, "entry_ts": int(ts4[entry_i]), "exit_ts": int(ts4[exit_i]),
                "direction": direction, "entry": entry_price, "exit": exit_price,
                "pnl_gross": pnl_g, "pnl_net": pnl_n, "cost": cost,
                "bars_held": exit_i - entry_i, "reason": exit_reason,
            })
            i = exit_i + 1
            continue
        i += 1

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
        "trades": trades,
    }


def main() -> None:
    sample = load("GBPJPY=X")
    ts_start = int(sample.ts[0])
    ts_end = int(sample.ts[-1])
    is_end = ts_start + int((ts_end - ts_start) * IS_FRAC)

    print("=" * 100)
    print(f"Swing H4 Bollinger Squeeze + Trend filter (SMA50)")
    print(f"  BB(20,2σ) in KC(20,1.5×ATR) ≥ 3 bars → release + trend align")
    print(f"  IS/OOS split 60/40. IS_end={datetime.fromtimestamp(is_end/1000, tz=UTC).date()}")
    print("=" * 100)
    header = f"  {'SYM':<10}{'IS_n':>6}{'IS_net':>9}{'IS_wr':>7}{'IS_pf':>7}  {'OOS_n':>6}{'OOS_net':>9}{'OOS_wr':>7}{'OOS_pf':>7}{'Avg':>8}"
    print(header)
    print("─" * len(header))

    all_is, all_oos = [], []
    for sym in SYMS:
        is_r = backtest_squeeze(sym, ts_start, is_end)
        oos_r = backtest_squeeze(sym, is_end, ts_end)
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
        print(f"  Permutation OOS p-value: {(ge+1)/1001:.4f}")


if __name__ == "__main__":
    main()
