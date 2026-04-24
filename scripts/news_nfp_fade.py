#!/usr/bin/env python3
"""NFP post-event: fade OR momentum.

Режим задаётся переменной MODE:
  MODE='fade'     — fade reaction (trade против initial reaction direction)
  MODE='momentum' — trade В СТОРОНУ reaction direction (trend-follow)

В обоих режимах:
  1. Измеряем reaction в окне [12:30, 12:30+REACT_MIN]
  2. Enter в reaction_end+1 bar
  3. SL: extreme reaction ± 0 (for fade) или SL = ref_price (for momentum)
  4. TP:
       fade     → ref_price (return to pre-event)
       momentum → 2 × reaction_pips от entry (trail expected continuation)
  5. Time-stop: HOLD_H часов

Варьируем:
  REACT_MIN ∈ {15, 30, 45, 60} — сколько ждать initial reaction
  HOLD_H    ∈ {2, 4, 8}         — макс держание
  MIN_MOVE_PIPS ∈ {10, 20, 30}   — минимальное реакции чтобы входить

Тест: IS 60% / OOS 40%, permutation test.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scalp_setups_m5 import (  # noqa: E402
    atr,
    cost_pips,
    load,
    PIP_SIZE,
)


USD_SYMS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X"]

# Режим: 'fade' или 'momentum'
MODE = "momentum"

# Параметры сетки
GRID_REACT_MIN = [15, 30, 45, 60]
GRID_HOLD_H = [2, 4, 8]
GRID_MIN_MOVE_PIPS = [10, 20, 30]

IS_FRAC = 0.6
BARS_PER_HOUR = 12


# ──────────────────── NFP calendar ────────────────────

def generate_nfp_dates(ts_start_ms: int, ts_end_ms: int) -> list[int]:
    """Первая пятница каждого месяца в 12:30 UTC за период."""
    start = datetime.fromtimestamp(ts_start_ms / 1000, tz=UTC).replace(day=1)
    end = datetime.fromtimestamp(ts_end_ms / 1000, tz=UTC)
    dates = []
    d = start
    while d <= end:
        # Найти первую пятницу месяца
        m_start = d.replace(day=1, hour=12, minute=30, second=0, microsecond=0)
        dow = m_start.weekday()  # 0=mon, 4=fri
        if dow <= 4:
            first_friday = m_start + timedelta(days=(4 - dow))
        else:
            first_friday = m_start + timedelta(days=(11 - dow))
        ts_ms = int(first_friday.timestamp() * 1000)
        if ts_start_ms <= ts_ms <= ts_end_ms:
            dates.append(ts_ms)
        # next month
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 1)
    return dates


# ──────────────────── Helper ────────────────────

def find_bar_at(ts_arr: np.ndarray, target_ts: int) -> int:
    idx = int(np.searchsorted(ts_arr, target_ts, side="left"))
    if idx >= len(ts_arr):
        return -1
    # Bar может быть слегка позже target если события не попадают на 5-мин границу
    if ts_arr[idx] - target_ts > 5 * 60 * 1000:
        return -1
    return idx


# ──────────────────── Backtest one combo ────────────────────

def backtest_nfp(sym: str, react_min: int, hold_h: int, min_move_pips: float,
                 nfp_dates: list[int], ts_start: int, ts_end: int) -> list[dict]:
    bars = load(sym)
    pip = PIP_SIZE[sym]
    cost = cost_pips(sym)
    react_bars = react_min // 5    # M5 bars
    hold_bars = hold_h * BARS_PER_HOUR

    trades = []
    for nfp_ts in nfp_dates:
        if nfp_ts < ts_start or nfp_ts >= ts_end:
            continue
        i_nfp = find_bar_at(bars.ts, nfp_ts)
        if i_nfp < 0 or i_nfp + react_bars + hold_bars >= len(bars.c):
            continue
        ref_price = bars.o[i_nfp]    # open @ 12:30 before event hits
        # Initial reaction extreme within react window
        w_end = i_nfp + react_bars
        w_h = bars.h[i_nfp:w_end + 1]
        w_l = bars.l[i_nfp:w_end + 1]
        peak_high = float(np.max(w_h))
        trough_low = float(np.min(w_l))
        up_move = (peak_high - ref_price) / pip
        dn_move = (ref_price - trough_low) / pip

        # Берём больший move
        if up_move > dn_move:
            reaction_dir = 1
            extreme = peak_high
            reaction_pips = up_move
        else:
            reaction_dir = -1
            extreme = trough_low
            reaction_pips = dn_move

        if reaction_pips < min_move_pips:
            continue

        entry_i = w_end + 1
        if entry_i >= len(bars.c):
            continue
        entry_price = bars.o[entry_i]

        a = atr(bars.h, bars.l, bars.c, 14)
        if np.isnan(a[i_nfp]) or a[i_nfp] <= 0:
            continue

        if MODE == "fade":
            # Fade: торгуем противоположно
            direction = -reaction_dir
            if direction == 1:
                sl = trough_low
                tp = ref_price
            else:
                sl = peak_high
                tp = ref_price
        else:
            # Momentum: торгуем в сторону reaction
            direction = reaction_dir
            move_pip_abs = reaction_pips * pip
            if direction == 1:
                sl = ref_price
                tp = entry_price + 2.0 * move_pip_abs
            else:
                sl = ref_price
                tp = entry_price - 2.0 * move_pip_abs

        # Simulate
        exit_price = None
        exit_reason = "TIME"
        exit_i = min(entry_i + hold_bars, len(bars.c) - 1)
        for j in range(entry_i, min(entry_i + hold_bars + 1, len(bars.c))):
            hi, lo = bars.h[j], bars.l[j]
            if direction == 1:
                if lo <= sl:
                    exit_price, exit_reason, exit_i = sl, "SL", j
                    break
                if hi >= tp:
                    exit_price, exit_reason, exit_i = tp, "TP", j
                    break
            else:
                if hi >= sl:
                    exit_price, exit_reason, exit_i = sl, "SL", j
                    break
                if lo <= tp:
                    exit_price, exit_reason, exit_i = tp, "TP", j
                    break
        if exit_price is None:
            exit_price = bars.c[exit_i]

        pnl_g = direction * (exit_price - entry_price) / pip
        pnl_n = pnl_g - cost
        trades.append({
            "sym": sym, "nfp_ts": nfp_ts,
            "entry_ts": int(bars.ts[entry_i]), "exit_ts": int(bars.ts[exit_i]),
            "direction": direction, "entry": entry_price, "exit": exit_price,
            "reaction_pips": reaction_pips, "reaction_dir": reaction_dir,
            "pnl_gross": pnl_g, "pnl_net": pnl_n, "cost": cost,
            "reason": exit_reason, "bars_held": exit_i - entry_i,
        })
    return trades


def stats(trades):
    if not trades:
        return {"n": 0, "net": 0, "avg": 0, "wr": 0, "pf": 0}
    nets = [t["pnl_net"] for t in trades]
    total = sum(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    return {
        "n": len(trades), "net": total, "avg": total / len(trades),
        "wr": len(wins) / len(trades) * 100, "pf": pf,
    }


# ──────────────────── Main ────────────────────

def main() -> None:
    sample = load("EURUSD=X")
    ts_start = int(sample.ts[0])
    ts_end = int(sample.ts[-1])
    is_end = ts_start + int((ts_end - ts_start) * IS_FRAC)

    nfp_dates = generate_nfp_dates(ts_start, ts_end)
    print("=" * 100)
    print(f"NFP post-event {MODE.upper()} — 1st Friday of month 12:30 UTC")
    print(f"  Total NFP events in 2y: {len(nfp_dates)}")
    for d in nfp_dates:
        print(f"    {datetime.fromtimestamp(d/1000, tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 100)

    # Full grid: find best IS params per symbol, test on OOS
    results = []
    print(f"\n  {'SYM':<10}{'BestParams':<24}{'IS_n':>5}{'IS_net':>9}{'IS_PF':>7}  {'OOS_n':>5}{'OOS_net':>9}{'OOS_PF':>7}")
    print("─" * 90)

    all_oos_trades = []
    for sym in USD_SYMS:
        best_is_net = -float("inf")
        best_p = None
        best_is_s = None
        for rm in GRID_REACT_MIN:
            for hh in GRID_HOLD_H:
                for mm in GRID_MIN_MOVE_PIPS:
                    is_trades = backtest_nfp(sym, rm, hh, mm, nfp_dates, ts_start, is_end)
                    s = stats(is_trades)
                    if s["n"] < 5:
                        continue
                    if s["net"] > best_is_net:
                        best_is_net = s["net"]
                        best_p = (rm, hh, mm)
                        best_is_s = s
        if best_p is None:
            print(f"  {sym:<10}  no valid params")
            continue
        oos_trades = backtest_nfp(sym, *best_p, nfp_dates, is_end, ts_end)
        oos_s = stats(oos_trades)
        all_oos_trades.extend(oos_trades)
        pstr = f"rm{best_p[0]}_h{best_p[1]}_m{best_p[2]}"
        print(
            f"  {sym:<10}{pstr:<24}"
            f"{best_is_s['n']:>5}{best_is_s['net']:>+9.0f}{best_is_s['pf']:>7.2f}  "
            f"{oos_s['n']:>5}{oos_s['net']:>+9.0f}{oos_s['pf']:>7.2f}"
        )
        results.append((sym, best_p, best_is_s, oos_s))

    print("─" * 90)
    s_all = stats(all_oos_trades)
    print(f"\n  Portfolio OOS: n={s_all['n']} net={s_all['net']:+.0f} avg={s_all['avg']:+.2f} "
          f"WR={s_all['wr']:.1f}% PF={s_all['pf']:.2f}")
    if all_oos_trades:
        rng = np.random.default_rng(42)
        gross = np.asarray([t["pnl_gross"] for t in all_oos_trades])
        costs = np.asarray([t["cost"] for t in all_oos_trades])
        obs = float(sum(t["pnl_net"] for t in all_oos_trades))
        ge = sum(1 for _ in range(1000) if (gross * rng.choice([-1, 1], size=len(all_oos_trades)) - costs).sum() >= obs)
        print(f"  Permutation OOS p-value: {(ge+1)/1001:.4f}")

    # Non-optimised: same params for all symbols
    print("\n" + "=" * 100)
    print("  Same-params anti-overfit: (react=30, hold=4h, min_move=20pips) for all symbols")
    print("─" * 90)
    ALL_IS_TRADES, ALL_OOS_TRADES = [], []
    for sym in USD_SYMS:
        tr_is = backtest_nfp(sym, 30, 4, 20, nfp_dates, ts_start, is_end)
        tr_oos = backtest_nfp(sym, 30, 4, 20, nfp_dates, is_end, ts_end)
        ALL_IS_TRADES.extend(tr_is)
        ALL_OOS_TRADES.extend(tr_oos)
        si, so = stats(tr_is), stats(tr_oos)
        print(f"  {sym:<10} IS n={si['n']:>3} net={si['net']:>+6.0f} PF={si['pf']:.2f} | "
              f"OOS n={so['n']:>3} net={so['net']:>+6.0f} PF={so['pf']:.2f}")
    si, so = stats(ALL_IS_TRADES), stats(ALL_OOS_TRADES)
    print(f"\n  Portfolio IS:  n={si['n']} net={si['net']:+.0f} avg={si['avg']:+.2f} WR={si['wr']:.1f}% PF={si['pf']:.2f}")
    print(f"  Portfolio OOS: n={so['n']} net={so['net']:+.0f} avg={so['avg']:+.2f} WR={so['wr']:.1f}% PF={so['pf']:.2f}")
    if ALL_OOS_TRADES:
        rng = np.random.default_rng(42)
        gross = np.asarray([t["pnl_gross"] for t in ALL_OOS_TRADES])
        costs = np.asarray([t["cost"] for t in ALL_OOS_TRADES])
        obs = float(sum(t["pnl_net"] for t in ALL_OOS_TRADES))
        ge = sum(1 for _ in range(1000) if (gross * rng.choice([-1, 1], size=len(ALL_OOS_TRADES)) - costs).sum() >= obs)
        print(f"  Permutation OOS p-value: {(ge+1)/1001:.4f}")


if __name__ == "__main__":
    main()
