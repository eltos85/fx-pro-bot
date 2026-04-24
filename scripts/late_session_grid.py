#!/usr/bin/env python3
"""Late Session Reversion — parameter grid + depth analysis.

Проблема v1: ACF(1) = -0.25 даёт expected edge ≈ 0.25σ ≈ 1.3 pips на EURGBP,
а cost = 2.3 pips. Edge < cost → убыточно.

Идеи:
1. GRID: k ∈ [1.0, 1.5, 2.0, 2.5], hold ∈ [1, 2, 3] — найти sweet spot
2. ACF глубина: посчитать ACF(2), ACF(3), ACF(4) в Late session —
   если ACF(2) тоже отрицательная, cumulative reverse больше
3. Joint: все 3 пары в одну стратегию → 3x sample size для статистики
4. Hour-specific: может только 22 UTC работает, не 21-23 в целом
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy import stats

from fx_pro_bot.config.settings import pip_size

TARGET_PAIRS = [("EURGBP=X", 2.3), ("EURJPY=X", 2.5), ("GBPJPY=X", 3.0)]


def _fname(sym):
    return sym.replace("=X", "").replace("=F", "_F") + "_M5.csv"


def load_csv(path):
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            rows.append((int(r["timestamp"]) // 1000, float(r["open"]),
                         float(r["high"]), float(r["low"]), float(r["close"])))
    dt = np.dtype([("ts", "i8"), ("open", "f8"), ("high", "f8"),
                   ("low", "f8"), ("close", "f8")])
    return np.array(rows, dtype=dt)


def resample_h1(arr):
    sec = 3600
    block = (arr["ts"] // sec) * sec
    unique, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        out[i] = (unique[i], arr["open"][s], arr["high"][s:e].max(),
                  arr["low"][s:e].min(), arr["close"][e - 1])
    return out


def acf_depth_analysis(bars_dict):
    """Какая ACF на разных лагах в Late session? Может ACF(2) усиливает reversion."""
    print("\n" + "=" * 90)
    print("ACF DEPTH ANALYSIS — Late Session (UTC 21-23)")
    print("=" * 90)
    print(f"{'Pair':<10}{'ACF(1)':>10}{'ACF(2)':>10}{'ACF(3)':>10}{'ACF(4)':>10}{'n':>8}")
    for sym, bars in bars_dict.items():
        close = bars["close"]
        ts = bars["ts"]
        log_ret = np.zeros(len(close))
        log_ret[1:] = np.log(close[1:] / close[:-1])
        hours = np.array([datetime.fromtimestamp(int(t), UTC).hour for t in ts])
        late = (hours >= 21) & (hours <= 23)
        r = log_ret[late]
        row = f"  {sym.replace('=X',''):<8}"
        for lag in [1, 2, 3, 4]:
            if len(r) > lag + 10:
                acf = np.corrcoef(r[:-lag], r[lag:])[0, 1]
                row += f"{acf:>+10.3f}"
            else:
                row += f"{'-':>10}"
        row += f"{len(r):>8}"
        print(row)


def hour_specific_acf(bars_dict):
    """Для каждого часа UTC в range 20-23, ACF(1)."""
    print("\n" + "=" * 90)
    print("HOUR-SPECIFIC ACF(1) — какой ЧАС даёт сильнейшую reversion?")
    print("=" * 90)
    print(f"{'Pair':<10}" + "".join(f"{h:>10}UTC" for h in [20, 21, 22, 23, 0, 1]))
    for sym, bars in bars_dict.items():
        close = bars["close"]
        ts = bars["ts"]
        log_ret = np.zeros(len(close))
        log_ret[1:] = np.log(close[1:] / close[:-1])
        hours = np.array([datetime.fromtimestamp(int(t), UTC).hour for t in ts])
        row = f"  {sym.replace('=X',''):<8}"
        for h in [20, 21, 22, 23, 0, 1]:
            mask = hours == h
            if np.sum(mask) < 30:
                row += f"{'-':>13}"
                continue
            # ACF: return при bar h vs return в bar h-1
            idx = np.where(mask)[0]
            idx = idx[idx > 0]
            if len(idx) < 30:
                row += f"{'-':>13}"
                continue
            curr_r = log_ret[idx]
            prev_r = log_ret[idx - 1]
            acf = np.corrcoef(prev_r, curr_r)[0, 1]
            row += f"{acf:>+13.3f}"
        print(row)


def grid_backtest(bars, sym, cost, k_grid, hold_grid, target_hours, lookback=30*24, is_frac=0.7):
    """Grid search k × hold. Returns table of (k, hold, n_is, is_tot, n_oos, oos_tot)."""
    ps = pip_size(sym)
    n = len(bars)
    close = bars["close"]
    open_ = bars["open"]
    high = bars["high"]
    low = bars["low"]
    ts = bars["ts"]
    log_ret = np.zeros(n)
    log_ret[1:] = np.log(close[1:] / close[:-1])
    sigma = np.full(n, np.nan)
    for i in range(lookback, n):
        w = log_ret[i - lookback:i]
        s = np.std(w)
        if s > 0:
            sigma[i] = s
    hours = np.array([datetime.fromtimestamp(int(t), UTC).hour for t in ts])
    n_is = int(n * is_frac)

    results = []
    for k in k_grid:
        for hold in hold_grid:
            is_pnls = []
            oos_pnls = []
            i = lookback + 2
            while i < n - hold - 1:
                if hours[i] not in target_hours or np.isnan(sigma[i]) or sigma[i] == 0:
                    i += 1
                    continue
                prev_sigma_ret = log_ret[i - 1] / sigma[i]
                if abs(prev_sigma_ret) < k:
                    i += 1
                    continue
                direction = -1 if prev_sigma_ret > 0 else 1
                entry_price = float(open_[i])
                sigma_price = sigma[i] * entry_price
                sl_long = entry_price - 3.0 * sigma_price
                sl_short = entry_price + 3.0 * sigma_price
                exit_price = float(close[i + hold - 1])
                for kk in range(hold):
                    bar_idx = i + kk
                    bar_high = float(high[bar_idx])
                    bar_low = float(low[bar_idx])
                    if direction == 1 and bar_low <= sl_long:
                        exit_price = sl_long
                        break
                    if direction == -1 and bar_high >= sl_short:
                        exit_price = sl_short
                        break
                pnl = (exit_price - entry_price) / ps * direction - cost
                if i < n_is:
                    is_pnls.append(pnl)
                else:
                    oos_pnls.append(pnl)
                i += hold
            results.append({
                "k": k, "hold": hold,
                "n_is": len(is_pnls),
                "is_tot": float(sum(is_pnls)) if is_pnls else 0.0,
                "is_mean": float(np.mean(is_pnls)) if is_pnls else 0.0,
                "n_oos": len(oos_pnls),
                "oos_tot": float(sum(oos_pnls)) if oos_pnls else 0.0,
                "oos_mean": float(np.mean(oos_pnls)) if oos_pnls else 0.0,
            })
    return results


def main():
    data_dir = Path("data/fxpro_klines")
    bars_dict = {}
    for sym, _ in TARGET_PAIRS:
        raw = load_csv(data_dir / _fname(sym))
        if len(raw) == 0:
            continue
        bars_dict[sym] = resample_h1(raw)

    acf_depth_analysis(bars_dict)
    hour_specific_acf(bars_dict)

    print("\n" + "=" * 120)
    print("GRID: Late Session (UTC 21-23) backtest | k_grid × hold_grid")
    print("=" * 120)
    k_grid = [1.0, 1.5, 2.0, 2.5]
    hold_grid = [1, 2, 3, 4]
    target_hours = {21, 22, 23}

    for sym, cost in TARGET_PAIRS:
        if sym not in bars_dict:
            continue
        bars = bars_dict[sym]
        print(f"\n{sym.replace('=X',''):<8}  cost_RT={cost}")
        results = grid_backtest(bars, sym, cost, k_grid, hold_grid, target_hours)
        # Table
        print(f"  {'k':>4}{'hold':>6}{'n_IS':>6}{'IS_tot':>10}{'IS_mean':>10}"
              f"{'n_OOS':>7}{'OOS_tot':>10}{'OOS_mean':>10}  Verdict")
        for r in results:
            v = ""
            if r["n_oos"] >= 3 and r["is_tot"] > 0 and r["oos_tot"] > 0:
                v = "✓ both +"
            elif r["is_tot"] > 0 and r["oos_tot"] > 0:
                v = "+ tiny"
            print(f"  {r['k']:>4.1f}{r['hold']:>6}{r['n_is']:>6}{r['is_tot']:>+10.1f}{r['is_mean']:>+10.2f}"
                  f"{r['n_oos']:>7}{r['oos_tot']:>+10.1f}{r['oos_mean']:>+10.2f}  {v}")

    # Joint strategy: объединить все 3 пары
    print("\n" + "=" * 120)
    print("JOINT: все 3 пары вместе (для статистики)")
    print("=" * 120)
    for k in k_grid:
        for hold in hold_grid:
            joint_is = []
            joint_oos = []
            for sym, cost in TARGET_PAIRS:
                if sym not in bars_dict:
                    continue
                bars = bars_dict[sym]
                r = grid_backtest(bars, sym, cost, [k], [hold], target_hours)[0]
                joint_is.append((r["n_is"], r["is_tot"]))
                joint_oos.append((r["n_oos"], r["oos_tot"]))
            n_is = sum(x[0] for x in joint_is)
            is_tot = sum(x[1] for x in joint_is)
            n_oos = sum(x[0] for x in joint_oos)
            oos_tot = sum(x[1] for x in joint_oos)
            v = ""
            if n_oos >= 10 and is_tot > 0 and oos_tot > 0:
                v = "✓✓ PASS"
            elif is_tot > 0 and oos_tot > 0:
                v = "~ +"
            print(f"  k={k:.1f} hold={hold}  n_IS={n_is:>3} IS_tot={is_tot:>+8.1f}  "
                  f"n_OOS={n_oos:>3} OOS_tot={oos_tot:>+8.1f}  {v}")


if __name__ == "__main__":
    main()
