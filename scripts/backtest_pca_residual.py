#!/usr/bin/env python3
"""Backtest Hypothesis D: PCA Residual Mean-Reversion.

Из EDA (H4 block, 2 года H1):
  После вычитания 3 principal components — residuals нескольких пар
  показывают сильную отрицательную автокорреляцию:
    EURJPY  ACF=-0.101  (сильнее всех)
    EURGBP  ACF=-0.085
    USDJPY  ACF=-0.080
    USDCHF  ACF=-0.063
    GBPUSD  ACF=-0.055
    USDCAD  ACF=-0.052

Логика:
  • H1 timeframe (как в EDA!) — НЕ M5 как в гипотезе C.
  • Rolling PCA 30 дней: fit на 9 FX → получить PC1, PC2, PC3 loadings.
  • Для каждой пары считаем residual = return - Σ(loading_i * PC_i).
  • Z-score residual по rolling 30d.
  • Entry: |z| > 2 → trade в противоположную сторону от residual.
  • Exit: |z| < 0.5 или hold 4 часа.

На H1 — ATR тоже на H1, SL 2×ATR, TP 3×ATR.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scalp_setups_m5 import (  # noqa: E402
    PIP_SIZE,
    cost_pips,
    load,
)


# ──────────────────── Параметры ────────────────────
FX_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCAD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
]
N_PC = 3                          # сколько факторов убираем
ROLLING_FIT_H = 30 * 24           # 30 дней H1 = 720
ROLLING_ZSCORE_H = 30 * 24        # rolling std residual
ENTRY_Z = 2.0
EXIT_Z = 0.5
MAX_HOLD_H = 4                    # 4 часа
COOL_OFF_H = 1


# ──────────────────── Helpers ────────────────────

def to_h1(bars):
    """M5 → H1 close."""
    ts = bars.ts
    c = bars.c
    h_arr = bars.h
    l_arr = bars.l
    hour = (ts // (3600 * 1000)).astype(np.int64)
    idx0 = 0
    out_ts, out_c, out_h, out_l = [], [], [], []
    for i in range(1, len(ts) + 1):
        if i == len(ts) or hour[i] != hour[idx0]:
            out_ts.append(int(hour[idx0] * 3600 * 1000))
            out_c.append(float(c[i - 1]))
            out_h.append(float(h_arr[idx0:i].max()))
            out_l.append(float(l_arr[idx0:i].min()))
            idx0 = i
    return (
        np.asarray(out_ts, dtype=np.int64),
        np.asarray(out_c, dtype=np.float64),
        np.asarray(out_h, dtype=np.float64),
        np.asarray(out_l, dtype=np.float64),
    )


def align_many(ts_list: list[np.ndarray], c_list: list[np.ndarray]):
    """Пересечение всех timestamps."""
    common = set(ts_list[0].tolist())
    for ts in ts_list[1:]:
        common &= set(ts.tolist())
    common = sorted(common)
    common_arr = np.asarray(common, dtype=np.int64)
    out = []
    for ts, c in zip(ts_list, c_list):
        mp = {int(t): i for i, t in enumerate(ts)}
        idx = np.asarray([mp[t] for t in common], dtype=np.int64)
        out.append(c[idx])
    return common_arr, out


# ──────────────────── Simulator ────────────────────

def backtest_pca_residual() -> None:
    print("=" * 100)
    print("Hypothesis D: PCA Residual Mean-Reversion (H1, 3 factors)")
    print(f"  Rolling fit: {ROLLING_FIT_H}h  Z-entry: |{ENTRY_Z}|  Z-exit: |{EXIT_Z}|  MaxHold: {MAX_HOLD_H}h")
    print("=" * 100)

    # Загружаем M5 → H1 close, H, L
    h1_ts_list = []
    h1_c_list = []
    h1_h_list = []
    h1_l_list = []
    for sym in FX_PAIRS:
        b = load(sym)
        ts, c, hi, lo = to_h1(b)
        h1_ts_list.append(ts)
        h1_c_list.append(c)
        h1_h_list.append(hi)
        h1_l_list.append(lo)

    ts, closes = align_many(h1_ts_list, h1_c_list)
    _, highs = align_many(h1_ts_list, h1_h_list)
    _, lows = align_many(h1_ts_list, h1_l_list)
    n = len(ts)
    print(f"  Aligned H1 bars: {n}")

    # Log-returns (H1)
    rets = np.full((len(FX_PAIRS), n), np.nan)
    for j in range(len(FX_PAIRS)):
        lc = np.log(closes[j])
        rets[j, 1:] = lc[1:] - lc[:-1]

    # Для каждого бара i, fit PCA на [i-ROLLING_FIT_H : i] returns, посчитать residuals
    residuals = np.full((len(FX_PAIRS), n), np.nan)
    for i in range(ROLLING_FIT_H + 1, n):
        window = rets[:, i - ROLLING_FIT_H:i]  # (9, W)
        # NaN check
        if np.isnan(window).any():
            continue
        # Centre
        mean = window.mean(axis=1, keepdims=True)
        X = window - mean
        # Covariance
        cov = X @ X.T / (X.shape[1] - 1)
        # Eigen decomposition
        try:
            vals, vecs = np.linalg.eigh(cov)
        except np.linalg.LinAlgError:
            continue
        # Top N_PC eigenvectors (highest eigenvalues)
        top_vecs = vecs[:, -N_PC:]  # (9, N_PC)
        # Current returns (column vector)
        curr = rets[:, i] - mean.flatten()  # (9,)
        # Project curr onto top_vecs and reconstruct → pca_projection
        # proj = top_vecs @ (top_vecs.T @ curr)
        proj = top_vecs @ (top_vecs.T @ curr)
        # Residual = curr - proj
        resid = curr - proj
        residuals[:, i] = resid

    # Для каждой пары: z-score residual по rolling window ROLLING_ZSCORE_H
    # Потом симулируем trades: |z| > 2 → short residual (direction flip)
    print(f"  Residuals computed for {(~np.isnan(residuals[0])).sum()} bars")
    print()

    # Summary per pair
    all_trades = []
    rows = []
    for j, sym in enumerate(FX_PAIRS):
        r = residuals[j]
        # Rolling z-score
        z = np.full(n, np.nan)
        for i in range(ROLLING_ZSCORE_H, n):
            w = r[i - ROLLING_ZSCORE_H:i]
            w_clean = w[~np.isnan(w)]
            if len(w_clean) < 100:
                continue
            mu = w_clean.mean()
            sd = w_clean.std(ddof=0)
            if sd <= 0:
                continue
            z[i] = (r[i] - mu) / sd

        pip = PIP_SIZE[sym]
        cost = cost_pips(sym)

        # Simulate
        trades = []
        pos = 0
        entry_i = -1
        entry_price = 0.0
        cool_until = -10**9
        for i in range(ROLLING_ZSCORE_H, n - 1):
            if np.isnan(z[i]):
                continue
            if pos == 0:
                if i < cool_until:
                    continue
                # если z > +2: residual слишком большой → B двигалось сильнее чем PC predicts
                #   ожидаем возврат вниз → SHORT
                # если z < -2: SHORT residual → ожидаем возврат вверх → LONG
                if z[i] > ENTRY_Z:
                    pos = -1
                    entry_i = i
                    entry_price = closes[j][i]
                elif z[i] < -ENTRY_Z:
                    pos = +1
                    entry_i = i
                    entry_price = closes[j][i]
            else:
                hold = i - entry_i
                exit_now = False
                reason = ""
                if abs(z[i]) < EXIT_Z:
                    exit_now = True
                    reason = "zexit"
                elif hold >= MAX_HOLD_H:
                    exit_now = True
                    reason = "time"
                if exit_now:
                    px = closes[j][i]
                    pnl_gross = pos * (px - entry_price) / pip
                    pnl_net = pnl_gross - cost
                    trades.append({
                        "sym": sym,
                        "entry_ts": int(ts[entry_i]),
                        "exit_ts": int(ts[i]),
                        "direction": pos,
                        "pnl_gross": pnl_gross,
                        "pnl_net": pnl_net,
                        "cost": cost,
                        "reason": reason,
                        "z_entry": float(z[entry_i]),
                        "hold_h": hold,
                    })
                    pos = 0
                    cool_until = i + COOL_OFF_H

        if not trades:
            print(f"  {sym:10s} NO trades")
            continue

        # IS/OOS
        ts_sorted = sorted(trades, key=lambda t: t["entry_ts"])
        cutoff = ts_sorted[int(len(ts_sorted) * 0.6)]["entry_ts"]
        is_ = [t for t in ts_sorted if t["entry_ts"] < cutoff]
        oos = [t for t in ts_sorted if t["entry_ts"] >= cutoff]
        net = sum(t["pnl_net"] for t in trades)
        is_net = sum(t["pnl_net"] for t in is_)
        oos_net = sum(t["pnl_net"] for t in oos)
        wins = [t["pnl_net"] for t in trades if t["pnl_net"] > 0]
        losses = [t["pnl_net"] for t in trades if t["pnl_net"] < 0]
        pf = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0)

        # Permutation
        rng = np.random.default_rng(42)
        gross = np.asarray([t["pnl_gross"] for t in trades])
        costs = np.asarray([t["cost"] for t in trades])
        obs = float(net)
        ge = 0
        for _ in range(1000):
            signs = rng.choice([-1, 1], size=len(trades))
            if (gross * signs - costs).sum() >= obs:
                ge += 1
        p_all = (ge + 1) / 1001
        if oos:
            gross_o = np.asarray([t["pnl_gross"] for t in oos])
            costs_o = np.asarray([t["cost"] for t in oos])
            obs_o = sum(t["pnl_net"] for t in oos)
            ge_o = 0
            for _ in range(1000):
                signs = rng.choice([-1, 1], size=len(oos))
                if (gross_o * signs - costs_o).sum() >= obs_o:
                    ge_o += 1
            p_oos = (ge_o + 1) / 1001
        else:
            p_oos = 1.0

        print(
            f"  {sym:10s} n={len(trades):4d}  net={net:+8.1f}  "
            f"wr={len(wins)/len(trades)*100:5.1f}%  pf={pf:5.2f}  "
            f"avg={net/len(trades):+6.2f}  "
            f"IS={is_net:+7.1f}  OOS={oos_net:+7.1f}  "
            f"p={p_all:.4f}  p_oos={p_oos:.4f}"
        )
        rows.append({
            "sym": sym,
            "n": len(trades),
            "net": round(net, 1),
            "is_net": round(is_net, 1),
            "oos_net": round(oos_net, 1),
            "n_is": len(is_),
            "n_oos": len(oos),
            "wr": round(len(wins)/len(trades)*100, 1),
            "pf": round(pf, 2) if pf != float("inf") else 999.99,
            "avg": round(net/len(trades), 2),
            "p": round(p_all, 4),
            "p_oos": round(p_oos, 4),
        })
        all_trades.extend(trades)

    print()
    print("=" * 100)
    print("PORTFOLIO (9 FX pairs)")
    print("=" * 100)
    if all_trades:
        net = sum(t["pnl_net"] for t in all_trades)
        wins = [t["pnl_net"] for t in all_trades if t["pnl_net"] > 0]
        losses = [t["pnl_net"] for t in all_trades if t["pnl_net"] < 0]
        pf = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0)
        ts_sorted = sorted(all_trades, key=lambda t: t["entry_ts"])
        cutoff = ts_sorted[int(len(ts_sorted) * 0.6)]["entry_ts"]
        is_ = [t for t in ts_sorted if t["entry_ts"] < cutoff]
        oos = [t for t in ts_sorted if t["entry_ts"] >= cutoff]
        is_net = sum(t["pnl_net"] for t in is_)
        oos_net = sum(t["pnl_net"] for t in oos)

        rng = np.random.default_rng(42)
        gross = np.asarray([t["pnl_gross"] for t in all_trades])
        costs = np.asarray([t["cost"] for t in all_trades])
        obs = float(net)
        ge = 0
        for _ in range(1000):
            signs = rng.choice([-1, 1], size=len(all_trades))
            if (gross * signs - costs).sum() >= obs:
                ge += 1
        p_all = (ge + 1) / 1001
        gross_o = np.asarray([t["pnl_gross"] for t in oos])
        costs_o = np.asarray([t["cost"] for t in oos])
        obs_o = sum(t["pnl_net"] for t in oos)
        ge_o = 0
        for _ in range(1000):
            signs = rng.choice([-1, 1], size=len(oos))
            if (gross_o * signs - costs_o).sum() >= obs_o:
                ge_o += 1
        p_oos = (ge_o + 1) / 1001

        days = 2 * 365
        print(
            f"  n={len(all_trades)}  (~{len(all_trades)/days:.2f}/day over {days} days)\n"
            f"  NET  = {net:+.1f} pips\n"
            f"  IS   = {is_net:+.1f} (n={len(is_)})\n"
            f"  OOS  = {oos_net:+.1f} (n={len(oos)})\n"
            f"  WR   = {len(wins)/len(all_trades)*100:.1f}%  PF = {pf:.2f}\n"
            f"  avg  = {net/len(all_trades):+.2f}\n"
            f"  p_all= {p_all:.4f}  p_oos = {p_oos:.4f}"
        )
        rows.append({
            "sym": "PORTFOLIO",
            "n": len(all_trades),
            "net": round(net, 1),
            "is_net": round(is_net, 1),
            "oos_net": round(oos_net, 1),
            "n_is": len(is_),
            "n_oos": len(oos),
            "wr": round(len(wins)/len(all_trades)*100, 1),
            "pf": round(pf, 2) if pf != float("inf") else 999.99,
            "avg": round(net/len(all_trades), 2),
            "p": round(p_all, 4),
            "p_oos": round(p_oos, 4),
        })

    out = Path(__file__).resolve().parents[1] / "data" / "backtest_pca_residual.csv"
    with out.open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n  Сохранено: {out}")


if __name__ == "__main__":
    backtest_pca_residual()
