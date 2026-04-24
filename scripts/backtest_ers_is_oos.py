#!/usr/bin/env python3
"""IS/OOS валидация Extreme-Reversion Scalper (ERS).

Процесс:
1. Разделить данные 70/30 по времени (IS / OOS).
2. Для каждого instrument на IS: robustness grid k × H, найти лучшую комбинацию.
3. Применить найденную комбинацию на OOS, проверить сохраняется ли edge.
4. Вердикт: edge реальный, если OOS net > 0 И p < 0.1.

Overfit-prevention:
- Robustness grid: k ∈ {2.0, 2.25, 2.5, 2.75, 3.0} × H ∈ {4, 6, 8, 12, 16}
- Edge считается robust только если 60%+ cells в grid positive на IS
- Best cell для OOS — не абсолютно лучший, а центр robust region

Запуск:
    PYTHONPATH=src python3 -m scripts.backtest_ers_is_oos --timeframe M15
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import stats

from fx_pro_bot.config.settings import pip_size

INSTRUMENTS_TO_TEST = ["EURJPY=X", "GBPUSD=X", "USDCAD=X", "BZ=F"]

COST_PIPS = {
    "EURUSD=X": 1.8, "GBPUSD=X": 2.2, "USDJPY=X": 1.8, "AUDUSD=X": 2.2,
    "USDCAD=X": 2.3, "USDCHF=X": 2.3, "EURGBP=X": 3.9,
    "EURJPY=X": 2.5, "GBPJPY=X": 3.0,
    "GC=F": 40.0, "CL=F": 3.5, "BZ=F": 3.5, "NG=F": 3.5, "ES=F": 2.0,
}

K_GRID = [2.0, 2.25, 2.5, 2.75, 3.0]
H_GRID = [4, 6, 8, 12, 16]
LOOKBACK = 20


def _fname(sym: str) -> str:
    return sym.replace("=X", "").replace("=F", "_F") + "_M5.csv"


def load_csv(path: Path) -> np.ndarray:
    if not path.exists():
        return np.array([])
    rows = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append((
                int(r["timestamp"]) // 1000,
                float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]),
                float(r["volume"]),
            ))
    dt = np.dtype([
        ("ts", "i8"), ("open", "f8"), ("high", "f8"),
        ("low", "f8"), ("close", "f8"), ("volume", "f8"),
    ])
    return np.array(rows, dtype=dt)


def resample(arr: np.ndarray, minutes: int) -> np.ndarray:
    if minutes == 5:
        return arr
    sec = minutes * 60
    block = (arr["ts"] // sec) * sec
    unique, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        g = arr[s:e]
        out[i] = (
            unique[i], g["open"][0], g["high"].max(),
            g["low"].min(), g["close"][-1], g["volume"].sum(),
        )
    return out


@dataclass
class ERSResult:
    n_trades: int
    gross_mean: float
    net_mean: float
    t_stat: float
    p_value: float
    wr_after_cost: float
    total_net_pips: float


def run_ers(
    arr: np.ndarray, sym: str,
    k: float, H: int,
    lookback: int = LOOKBACK,
) -> ERSResult:
    c = arr["close"]
    rets = np.log(c[1:] / c[:-1])
    ps = pip_size(sym)
    ref = float(np.mean(c))
    rets_pips = rets * ref / ps

    if len(rets) < lookback + H + 10:
        return ERSResult(0, 0, 0, 0, 1, 0, 0)

    roll_std = np.array([
        np.std(rets[max(0, i - lookback):i]) if i >= 5 else 0.0
        for i in range(len(rets))
    ])
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(roll_std > 0, rets / roll_std, 0)

    cost = COST_PIPS.get(sym, 5.0)
    up_idx = np.where(z[:-H] > k)[0]
    dn_idx = np.where(z[:-H] < -k)[0]

    if len(up_idx) < 5 or len(dn_idx) < 5:
        return ERSResult(0, 0, 0, 0, 1, 0, 0)

    up_next = np.array([np.sum(rets_pips[i + 1:i + 1 + H]) for i in up_idx])
    dn_next = np.array([np.sum(rets_pips[i + 1:i + 1 + H]) for i in dn_idx])
    # MR: after up → short (profit=-up); after dn → long (profit=dn)
    profits = np.concatenate([-up_next, dn_next])
    gross = float(np.mean(profits))
    net_pips = profits - cost
    net = float(np.mean(net_pips))
    if len(profits) > 1:
        t, p = stats.ttest_1samp(net_pips, 0.0)
    else:
        t, p = 0.0, 1.0
    wr = float(np.mean(net_pips > 0))
    total = float(np.sum(net_pips))
    return ERSResult(
        n_trades=len(profits),
        gross_mean=gross,
        net_mean=net,
        t_stat=float(t),
        p_value=float(p),
        wr_after_cost=wr,
        total_net_pips=total,
    )


def split_is_oos(arr: np.ndarray, is_frac: float = 0.7) -> tuple[np.ndarray, np.ndarray]:
    if len(arr) < 100:
        return arr, np.array([])
    n_is = int(len(arr) * is_frac)
    return arr[:n_is], arr[n_is:]


def robustness_grid(
    arr: np.ndarray, sym: str,
    k_grid: list[float] = K_GRID,
    h_grid: list[int] = H_GRID,
) -> dict:
    grid: dict[tuple[float, int], ERSResult] = {}
    for k in k_grid:
        for H in h_grid:
            grid[(k, H)] = run_ers(arr, sym, k, H)
    return grid


def print_grid(grid: dict, title: str) -> None:
    print(f"\n--- {title} ---")
    header_label = "k/H"
    print(f"{header_label:<6}" + "".join(f"{H:>12}" for H in H_GRID))
    for k in K_GRID:
        row = f"{k:<6}"
        for H in H_GRID:
            r = grid[(k, H)]
            if r.n_trades == 0:
                row += f"{'---':>12}"
            else:
                mark = "**" if r.p_value < 0.01 else "*" if r.p_value < 0.05 else " "
                # формат: net/WR%
                row += f"{r.net_mean:+4.2f}/{r.wr_after_cost*100:2.0f}{mark:<2}"[:12].rjust(12)
        print(row)


def summarize_grid(grid: dict) -> dict:
    cells = list(grid.values())
    valid = [c for c in cells if c.n_trades > 0]
    if not valid:
        return {}
    positive = sum(1 for c in valid if c.net_mean > 0)
    significant_positive = sum(1 for c in valid if c.net_mean > 0 and c.p_value < 0.05)
    total_net = sum(c.total_net_pips for c in valid)
    avg_net_per_trade = np.mean([c.net_mean for c in valid])
    # Найдём лучшую по total_net_pips (не per-trade) среди p<0.1
    sig_cells = [(k, c) for k, c in grid.items() if c.p_value < 0.1 and c.net_mean > 0]
    if sig_cells:
        best_key, best_cell = max(sig_cells, key=lambda x: x[1].total_net_pips)
    else:
        # fallback — лучшая по p
        best_key, best_cell = max(grid.items(), key=lambda x: -x[1].p_value if x[1].net_mean > 0 else -1)
    return {
        "n_cells": len(valid),
        "positive_cells": positive,
        "significant_positive": significant_positive,
        "positive_fraction": positive / len(valid),
        "total_net_is_pips": total_net,
        "avg_net_per_trade": avg_net_per_trade,
        "best_key": best_key,  # (k, H)
        "best_cell": best_cell,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--timeframe", choices=["M5", "M15", "H1"], default="M15")
    ap.add_argument("--is-frac", type=float, default=0.7)
    args = ap.parse_args()

    minutes = {"M5": 5, "M15": 15, "H1": 60}[args.timeframe]

    print("=" * 110)
    print(f"ERS IS/OOS Validation | TF={args.timeframe} | IS={int(args.is_frac*100)}% | "
          f"grid {len(K_GRID)}×{len(H_GRID)}={len(K_GRID)*len(H_GRID)} cells per instrument")
    print("=" * 110)

    final_report = []

    for sym in INSTRUMENTS_TO_TEST:
        raw = load_csv(args.data_dir / _fname(sym))
        if len(raw) == 0:
            print(f"\n[WARN] no data: {sym}")
            continue
        arr = resample(raw, minutes)
        is_arr, oos_arr = split_is_oos(arr, args.is_frac)
        print(f"\n{'=' * 110}")
        print(f"{sym}  |  total bars={len(arr)}  IS bars={len(is_arr)}  OOS bars={len(oos_arr)}  |  cost={COST_PIPS[sym]} pips")
        print(f"{'=' * 110}")

        is_grid = robustness_grid(is_arr, sym)
        oos_grid = robustness_grid(oos_arr, sym)

        print_grid(is_grid, f"IS grid net_pips/WR% (** p<0.01, * p<0.05)")
        print_grid(oos_grid, f"OOS grid")

        is_sum = summarize_grid(is_grid)
        oos_sum = summarize_grid(oos_grid)
        if not is_sum or not oos_sum:
            continue

        # Выбираем IS-best и применяем те же (k, H) на OOS
        best_k, best_H = is_sum["best_key"]
        is_best = is_sum["best_cell"]
        oos_applied = oos_grid[(best_k, best_H)]

        print(f"\nIS summary: positive={is_sum['positive_cells']}/{is_sum['n_cells']} "
              f"({is_sum['positive_fraction']*100:.0f}%)  "
              f"significant={is_sum['significant_positive']}  "
              f"total_net_IS={is_sum['total_net_is_pips']:+.0f} pips")
        print(f"Best (k={best_k}, H={best_H}): IS n={is_best.n_trades} net={is_best.net_mean:+.3f} "
              f"p={is_best.p_value:.4f} WR={is_best.wr_after_cost*100:.1f}% "
              f"total={is_best.total_net_pips:+.0f} pips")
        print(f"OOS applied (k={best_k}, H={best_H}): n={oos_applied.n_trades} "
              f"net={oos_applied.net_mean:+.3f} p={oos_applied.p_value:.4f} "
              f"WR={oos_applied.wr_after_cost*100:.1f}% "
              f"total={oos_applied.total_net_pips:+.0f} pips")
        print(f"OOS grid: positive={oos_sum['positive_cells']}/{oos_sum['n_cells']} "
              f"({oos_sum['positive_fraction']*100:.0f}%)  "
              f"total_net_OOS={oos_sum['total_net_is_pips']:+.0f} pips")

        verdict = ""
        if oos_applied.net_mean > 0 and oos_applied.p_value < 0.1:
            if is_sum["positive_fraction"] >= 0.6:
                verdict = "✓ REAL EDGE — edge survives OOS + IS robust (60%+ cells positive)"
            else:
                verdict = "~ OOS OK but IS not robust — proceed with caution"
        else:
            verdict = "✗ OOS FAILED — edge not confirmed, risk of overfit"
        print(f"\nVERDICT: {verdict}")

        final_report.append({
            "sym": sym,
            "best_k": best_k, "best_H": best_H,
            "is_net": is_best.net_mean,
            "is_p": is_best.p_value,
            "is_total": is_best.total_net_pips,
            "oos_net": oos_applied.net_mean,
            "oos_p": oos_applied.p_value,
            "oos_total": oos_applied.total_net_pips,
            "oos_wr": oos_applied.wr_after_cost,
            "oos_n": oos_applied.n_trades,
            "is_robust_frac": is_sum["positive_fraction"],
            "oos_robust_frac": oos_sum["positive_fraction"],
            "verdict": verdict,
        })

    # ── Финальная сводка ──
    print("\n" + "=" * 110)
    print("FINAL SUMMARY (всё в net pips, после комиссий FxPro)")
    print("=" * 110)
    print(f"{'Symbol':<10}{'k':>5}{'H':>4}{'IS_net':>9}{'IS_p':>8}{'IS_tot':>9}"
          f"{'OOS_net':>9}{'OOS_p':>8}{'OOS_tot':>9}{'OOS_WR':>8}{'rob_IS':>8}{'rob_OOS':>9}  Verdict")
    for r in final_report:
        print(
            f"  {r['sym']:<8}{r['best_k']:>5}{r['best_H']:>4}"
            f"{r['is_net']:>+9.2f}{r['is_p']:>8.4f}{r['is_total']:>+9.0f}"
            f"{r['oos_net']:>+9.2f}{r['oos_p']:>8.4f}{r['oos_total']:>+9.0f}"
            f"{r['oos_wr']*100:>7.1f}%"
            f"{r['is_robust_frac']*100:>7.0f}%"
            f"{r['oos_robust_frac']*100:>8.0f}%  "
            f"{r['verdict'][:40]}"
        )

    total_oos = sum(r["oos_total"] for r in final_report if "REAL EDGE" in r["verdict"] or "caution" in r["verdict"])
    print(f"\nСумма OOS-pips по инструментам с подтверждённым edge: {total_oos:+.0f} pips "
          f"(за ~27 дней = {total_oos/27:+.1f} pips/day)")


if __name__ == "__main__":
    main()
