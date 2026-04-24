#!/usr/bin/env python3
"""Late Session Reversion — ВАЛИДАЦИЯ.

Три теста:
1. Hour-specific: только 22UTC / только 23UTC
2. Walk-forward: 5 последовательных split-окон
3. Permutation test: 1000 shuffle → p-value против random

Если все 3 валидации пройдены — edge реален и готов для shadow mode.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy import stats

from fx_pro_bot.config.settings import pip_size

TARGET_PAIRS = [
    ("EURGBP=X", 2.3),
    ("EURJPY=X", 2.5),
    ("GBPJPY=X", 3.0),
]

LOOKBACK = 30 * 24  # 30 days H1
SL_SIGMA = 3.0


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


def generate_trades(bars, sym, cost, k, hold, target_hours):
    """Генерирует список trades: (entry_idx, direction, expected_pnl_pips)."""
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
    for i in range(LOOKBACK, n):
        w = log_ret[i - LOOKBACK:i]
        s = np.std(w)
        if s > 0:
            sigma[i] = s
    hours = np.array([datetime.fromtimestamp(int(t), UTC).hour for t in ts])

    trades = []
    i = LOOKBACK + 2
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
        sl_long = entry_price - SL_SIGMA * sigma_price
        sl_short = entry_price + SL_SIGMA * sigma_price
        exit_price = float(close[i + hold - 1])
        for kk in range(hold):
            bar_idx = i + kk
            if direction == 1 and float(low[bar_idx]) <= sl_long:
                exit_price = sl_long
                break
            if direction == -1 and float(high[bar_idx]) >= sl_short:
                exit_price = sl_short
                break
        pnl = (exit_price - entry_price) / ps * direction - cost
        trades.append({
            "i": i, "ts": int(ts[i]), "direction": direction,
            "prev_sigma_ret": float(prev_sigma_ret),
            "entry_price": entry_price, "exit_price": exit_price,
            "sigma": float(sigma[i]), "pnl": float(pnl),
        })
        i += hold
    return trades


def stats_line(pnls, label=""):
    if not pnls:
        return f"  [{label:<14}] n=0"
    arr = np.array(pnls)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 999
    t, p = stats.ttest_1samp(arr, 0) if len(arr) > 1 else (0, 1)
    return (f"  [{label:<14}] n={len(arr):>3}  total={arr.sum():+7.1f}  "
            f"mean={arr.mean():+6.2f}  WR={(len(wins)/len(arr))*100:5.1f}%  "
            f"PF={pf:5.2f}  t={t:+5.2f}  p={p:.4f}")


def test_1_hour_specific(bars_dict, k=1.0, hold=3):
    print("\n" + "=" * 110)
    print(f"TEST 1: HOUR-SPECIFIC (k={k}, hold={hold})")
    print("=" * 110)

    hour_sets = {
        "22 UTC only": {22},
        "23 UTC only": {23},
        "22+23 UTC": {22, 23},
        "21+22+23 UTC": {21, 22, 23},
    }

    for label, hours in hour_sets.items():
        print(f"\n  ▶ {label}")
        joint_pnls = []
        for sym, cost in TARGET_PAIRS:
            if sym not in bars_dict:
                continue
            trades = generate_trades(bars_dict[sym], sym, cost, k, hold, hours)
            joint_pnls.extend([t["pnl"] for t in trades])
            print(stats_line([t["pnl"] for t in trades], sym.replace("=X", "")))
        print(stats_line(joint_pnls, "JOINT"))


def test_2_walk_forward(bars_dict, k=1.0, hold=3, target_hours={22, 23}, n_splits=5):
    print("\n" + "=" * 110)
    print(f"TEST 2: WALK-FORWARD ({n_splits} splits, k={k}, hold={hold}, hours={sorted(target_hours)})")
    print("=" * 110)

    # Combine все trades с timestamps
    all_trades = []
    for sym, cost in TARGET_PAIRS:
        if sym not in bars_dict:
            continue
        trades = generate_trades(bars_dict[sym], sym, cost, k, hold, target_hours)
        for t in trades:
            t["sym"] = sym
        all_trades.extend(trades)
    all_trades.sort(key=lambda t: t["ts"])

    print(f"  Total trades: {len(all_trades)}")
    if len(all_trades) < 10:
        print("  ⚠ недостаточно trades для walk-forward")
        return

    # Split by equal chunks
    split_size = len(all_trades) // n_splits
    passes = 0
    for i in range(n_splits):
        start = i * split_size
        end = start + split_size if i < n_splits - 1 else len(all_trades)
        chunk = all_trades[start:end]
        pnls = [t["pnl"] for t in chunk]
        ts_start = datetime.fromtimestamp(chunk[0]["ts"], UTC)
        ts_end = datetime.fromtimestamp(chunk[-1]["ts"], UTC)
        total = sum(pnls)
        t_stat, p = stats.ttest_1samp(pnls, 0) if len(pnls) > 1 else (0, 1)
        status = "✓ profit" if total > 0 else "✗ loss"
        if total > 0:
            passes += 1
        print(f"  Split {i+1}/{n_splits}  "
              f"{ts_start:%Y-%m-%d} → {ts_end:%Y-%m-%d}  "
              f"n={len(chunk):>3}  total={total:+7.1f}  "
              f"mean={np.mean(pnls):+5.2f}  t={t_stat:+5.2f}  p={p:.4f}  {status}")

    print(f"\n  Passes (profit): {passes}/{n_splits}")
    if passes >= n_splits * 0.6:
        print("  ✓ ROBUST across time splits")
    else:
        print("  ⚠ NOT robust — edge inconsistent across time")


def test_3_permutation(bars_dict, k=1.0, hold=3, target_hours={22, 23}, n_perm=1000):
    """Сравнение observed total P&L с null-distribution (random direction)."""
    print("\n" + "=" * 110)
    print(f"TEST 3: PERMUTATION TEST (n={n_perm}, k={k}, hold={hold})")
    print("Shuffle направления сделок и пересчитываем PnL → null distribution")
    print("=" * 110)

    # Соберём все trades с их σ-predicted pnl
    all_trades = []
    for sym, cost in TARGET_PAIRS:
        if sym not in bars_dict:
            continue
        trades = generate_trades(bars_dict[sym], sym, cost, k, hold, target_hours)
        all_trades.extend(trades)

    if len(all_trades) < 10:
        print("  ⚠ too few trades for permutation")
        return

    observed_total = sum(t["pnl"] for t in all_trades)
    observed_mean = observed_total / len(all_trades)
    print(f"  Observed: n={len(all_trades)}, total={observed_total:+.1f}, mean={observed_mean:+.2f}")

    # Построим "сырой" PnL до применения direction: (exit - entry) / ps + cost_back
    # Потом permutation = random direction ±1
    # PnL_trade = direction * raw_signed_pnl - cost
    # raw = (exit_price - entry_price) / ps, cost это cost_RT
    # Но мы уже имеем trade['pnl'] = direction * (exit - entry)/ps - cost
    # permutation: сохраняем (exit - entry)/ps и cost, шуффлим direction

    # Reconstruct raw + cost per trade
    raw_and_cost = []
    for t in all_trades:
        # Get cost из TARGET_PAIRS
        # direction * raw_gross - cost = t['pnl']
        # но нам нужен отдельно raw_gross и cost
        # Найдём cost для этой пары
        sym = None
        # ... сделал без sym, используем pnl_gross = pnl + cost, но cost зависит от пары
        # Чтобы упростить: rebuild trades с cost
        pass

    # Переделаю: rebuild trades с sym, cost полями
    all_trades = []
    for sym, cost in TARGET_PAIRS:
        if sym not in bars_dict:
            continue
        trades = generate_trades(bars_dict[sym], sym, cost, k, hold, target_hours)
        for t in trades:
            t["sym"] = sym
            t["cost"] = cost
            # raw_gross до применения direction = direction * (pnl + cost)
            t["raw_gross"] = t["direction"] * (t["pnl"] + cost)
        all_trades.extend(trades)

    # Permutation: для каждой сделки — random direction ±1, PnL = rand_dir * raw_gross - cost
    rng = np.random.default_rng(42)
    null_totals = []
    null_means = []
    for _ in range(n_perm):
        perm_pnls = []
        for t in all_trades:
            rand_dir = 1 if rng.random() < 0.5 else -1
            perm_pnl = rand_dir * t["raw_gross"] - t["cost"]
            perm_pnls.append(perm_pnl)
        null_totals.append(sum(perm_pnls))
        null_means.append(np.mean(perm_pnls))

    null_totals = np.array(null_totals)
    null_means = np.array(null_means)

    p_total = float(np.mean(null_totals >= observed_total))
    p_mean = float(np.mean(null_means >= observed_mean))

    print(f"  Null distribution (n={n_perm}):")
    print(f"    total: mean={null_totals.mean():+.1f}, std={null_totals.std():.1f}, "
          f"95% CI=[{np.percentile(null_totals, 2.5):+.1f}, {np.percentile(null_totals, 97.5):+.1f}]")
    print(f"  p-value (observed ≥ null total):  {p_total:.4f}")
    print(f"  p-value (observed ≥ null mean):   {p_mean:.4f}")

    if p_total < 0.05:
        print("  ✓ ZNACHIMO: random direction даёт такой или лучший результат в < 5% случаев")
    elif p_total < 0.10:
        print("  ~ marginal: p < 0.10")
    else:
        print("  ✗ NOT significant: edge может быть random artifact")


def main():
    data_dir = Path("data/fxpro_klines")
    bars_dict = {}
    for sym, _ in TARGET_PAIRS:
        raw = load_csv(data_dir / _fname(sym))
        if len(raw) == 0:
            continue
        bars_dict[sym] = resample_h1(raw)

    # Фиксированные оптимальные параметры (из grid)
    k = 1.0
    hold = 3

    test_1_hour_specific(bars_dict, k, hold)
    test_2_walk_forward(bars_dict, k, hold, target_hours={22, 23}, n_splits=5)
    test_3_permutation(bars_dict, k, hold, target_hours={22, 23}, n_perm=1000)

    # Также с k=1.5
    print("\n\n" + "#" * 110)
    print("# ПОВТОР с k=1.5 (чуть более выборочно)")
    print("#" * 110)
    test_1_hour_specific(bars_dict, 1.5, 3)
    test_3_permutation(bars_dict, 1.5, 3, target_hours={22, 23}, n_perm=1000)


if __name__ == "__main__":
    main()
