#!/usr/bin/env python3
"""Pairs Trading Backtest (z-score mean-reversion) на high-correlation FX пар.

Методология (Quant Decoded 2025, Lemishko 2024):
- Rolling β estimation (OLS) на last 30 days
- Spread = Y - β × X
- Z-score = (spread - mean_30d) / std_30d
- Entry: |z| > 2.0 (fade the divergence)
- Exit: |z| < 0.5 OR time stop
- SL: |z| > 3.5

Анти-overfit:
- Параметры зафиксированы ДО бэктеста.
- IS: первые 70% данных, OOS: последние 30%.
- Rolling β обновляется каждый день (walk-forward).
- Проверяем на 4 парах.

Запуск:
    PYTHONPATH=src python3 -m scripts.backtest_pairs_trading --timeframe H1
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy import stats
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

from fx_pro_bot.config.settings import pip_size

# ─────────────────── параметры (мировая практика) ───────────────────

ENTRY_Z = 2.0
EXIT_Z = 0.5
STOP_Z = 3.5
ROLLING_WINDOW_BARS = 7 * 24     # 7 дней × 24 H1 бара = 168 (стандарт для intraday FX pair trading)
TIME_STOP_BARS = 5 * 24          # 5 дней макс hold (HL 2.7-3.3d × 1.5)

# Пары для тестирования — топ-коррелированные из EDA
PAIRS_TO_TEST = [
    ("EURUSD=X", "GBPUSD=X"),   # corr 0.85
    ("EURUSD=X", "AUDUSD=X"),   # corr 0.76
    ("AUDUSD=X", "EURJPY=X"),   # marginal coint p=0.06
    ("AUDUSD=X", "GBPJPY=X"),   # marginal coint p=0.07
    ("EURJPY=X", "GBPJPY=X"),   # high-vol JPY crosses
    ("USDJPY=X", "GBPJPY=X"),   # JPY mechanics
]

# Cost R-T in pips (примерно sum комиссий двух ног)
# Торгуем 2 инструмента одновременно → double cost
PAIR_COSTS = {
    ("EURUSD=X", "GBPUSD=X"): 1.8 + 2.2,
    ("EURUSD=X", "AUDUSD=X"): 1.8 + 2.2,
    ("AUDUSD=X", "EURJPY=X"): 2.2 + 2.5,
    ("AUDUSD=X", "GBPJPY=X"): 2.2 + 3.0,
    ("EURJPY=X", "GBPJPY=X"): 2.5 + 3.0,
    ("USDJPY=X", "GBPJPY=X"): 1.8 + 3.0,
}

IS_FRACTION = 0.7


def _fname(sym: str) -> str:
    return sym.replace("=X", "").replace("=F", "_F") + "_M5.csv"


def load_csv(path: Path) -> np.ndarray:
    if not path.exists():
        return np.array([])
    rows = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append((int(r["timestamp"]) // 1000, float(r["close"])))
    dt = np.dtype([("ts", "i8"), ("close", "f8")])
    return np.array(rows, dtype=dt)


def resample(arr: np.ndarray, minutes: int) -> np.ndarray:
    if len(arr) == 0:
        return arr
    sec = minutes * 60
    block = (arr["ts"] // sec) * sec
    unique, idx_start = np.unique(block, return_index=True)
    idx_end = np.concatenate([idx_start[1:], [len(arr)]])
    out = np.zeros(len(unique), dtype=arr.dtype)
    for i, (s, e) in enumerate(zip(idx_start, idx_end)):
        out[i] = (unique[i], arr["close"][e - 1])
    return out


def align(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    common = np.intersect1d(a["ts"], b["ts"])
    if len(common) < 100:
        return np.array([]), np.array([]), np.array([])
    a_map = {t: i for i, t in enumerate(a["ts"])}
    b_map = {t: i for i, t in enumerate(b["ts"])}
    ai = np.array([a_map[t] for t in common])
    bi = np.array([b_map[t] for t in common])
    return common, a["close"][ai], b["close"][bi]


def rolling_hedge(y: np.ndarray, x: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Rolling OLS. Returns (alpha_series, beta_series)."""
    n = len(y)
    alphas = np.full(n, np.nan)
    betas = np.full(n, np.nan)
    for i in range(window, n):
        y_win = y[i - window:i]
        x_win = x[i - window:i]
        try:
            X = add_constant(x_win)
            m = OLS(y_win, X).fit()
            alphas[i] = float(m.params[0])
            betas[i] = float(m.params[1])
        except Exception:
            pass
    return alphas, betas


@dataclass
class PairTrade:
    entry_ts: int
    exit_ts: int
    entry_zscore: float
    exit_zscore: float
    direction: int            # +1 = long spread (y up, x down), -1 = short spread
    y_entry: float
    x_entry: float
    y_exit: float
    x_exit: float
    beta: float
    exit_reason: str
    y_pnl_pips: float
    x_pnl_pips: float
    net_pips: float           # total PnL across both legs (minus cost)


def backtest_pair(
    ts: np.ndarray, y: np.ndarray, x: np.ndarray,
    y_sym: str, x_sym: str,
    cost_rt: float,
    window: int = ROLLING_WINDOW_BARS,
) -> list[PairTrade]:
    """Backtest одной пары через z-score rules."""
    alphas, betas = rolling_hedge(y, x, window)
    spread = y - (alphas + betas * x)  # nan для первых window баров

    # Rolling mean/std спреда на том же window
    sp_mean = np.full(len(spread), np.nan)
    sp_std = np.full(len(spread), np.nan)
    for i in range(window, len(spread)):
        s_win = spread[i - window:i]
        if np.any(np.isnan(s_win)):
            continue
        sp_mean[i] = float(np.mean(s_win))
        sp_std[i] = float(np.std(s_win))

    z = np.where(sp_std > 0, (spread - sp_mean) / sp_std, 0)

    y_ps = pip_size(y_sym)
    x_ps = pip_size(x_sym)

    trades: list[PairTrade] = []
    in_position = False
    pos_dir = 0
    pos_entry_idx = 0
    pos_y_entry = 0.0
    pos_x_entry = 0.0
    pos_beta = 0.0
    pos_entry_z = 0.0

    for i in range(window + 1, len(z) - 1):
        if np.isnan(z[i]) or np.isnan(betas[i]):
            continue

        curr_z = z[i]

        if in_position:
            # Exit conditions
            exit_reason = ""
            # Revert to mean
            if pos_dir == 1 and curr_z >= -EXIT_Z:  # long spread, z recovered от -2 до -0.5
                exit_reason = "MEAN"
            elif pos_dir == -1 and curr_z <= EXIT_Z:
                exit_reason = "MEAN"
            # Stop loss
            elif pos_dir == 1 and curr_z <= -STOP_Z:
                exit_reason = "SL"
            elif pos_dir == -1 and curr_z >= STOP_Z:
                exit_reason = "SL"
            # Time stop
            elif i - pos_entry_idx >= TIME_STOP_BARS:
                exit_reason = "TIME"

            if exit_reason:
                y_exit = float(y[i])
                x_exit = float(x[i])
                # Long spread = long Y, short (beta·X)
                y_pnl = (y_exit - pos_y_entry) / y_ps * pos_dir
                # short (beta·X): PnL = -beta * (x_exit - x_entry) * dir
                x_pnl = -pos_beta * (x_exit - pos_x_entry) / x_ps * pos_dir
                net = y_pnl + x_pnl - cost_rt
                trades.append(PairTrade(
                    entry_ts=int(ts[pos_entry_idx]),
                    exit_ts=int(ts[i]),
                    entry_zscore=pos_entry_z,
                    exit_zscore=float(curr_z),
                    direction=pos_dir,
                    y_entry=pos_y_entry, x_entry=pos_x_entry,
                    y_exit=y_exit, x_exit=x_exit,
                    beta=pos_beta, exit_reason=exit_reason,
                    y_pnl_pips=y_pnl, x_pnl_pips=x_pnl,
                    net_pips=net,
                ))
                in_position = False
            continue

        # Entry: z крутой extreme
        if curr_z <= -ENTRY_Z:
            # spread низко → long spread (long Y, short X)
            in_position = True
            pos_dir = 1
            pos_entry_idx = i
            pos_y_entry = float(y[i])
            pos_x_entry = float(x[i])
            pos_beta = float(betas[i])
            pos_entry_z = float(curr_z)
        elif curr_z >= ENTRY_Z:
            in_position = True
            pos_dir = -1
            pos_entry_idx = i
            pos_y_entry = float(y[i])
            pos_x_entry = float(x[i])
            pos_beta = float(betas[i])
            pos_entry_z = float(curr_z)

    return trades


def summarize(trades: list[PairTrade], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0, "total": 0, "mean": 0, "wr": 0,
                "p": 1.0, "avg_win": 0, "avg_loss": 0, "pf": 0, "t": 0}
    pips = np.array([t.net_pips for t in trades])
    wins = pips[pips > 0]
    losses = pips[pips <= 0]
    pf = (wins.sum() / abs(losses.sum())) if len(losses) > 0 and abs(losses.sum()) > 0 else float("inf")
    t_stat, p = stats.ttest_1samp(pips, 0.0) if len(pips) > 1 else (0, 1)
    return {
        "label": label,
        "n": len(trades),
        "total": float(pips.sum()),
        "mean": float(pips.mean()),
        "wr": float(len(wins) / len(trades)),
        "p": float(p),
        "t": float(t_stat),
        "avg_win": float(wins.mean()) if len(wins) > 0 else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) > 0 else 0.0,
        "pf": float(pf) if pf != float("inf") else 999.0,
    }


def print_summary(s: dict) -> None:
    if s["n"] == 0:
        print(f"    [{s['label']:<5}] no trades")
        return
    print(
        f"    [{s['label']:<5}] n={s['n']:>3}  total={s['total']:+8.1f}  "
        f"mean={s['mean']:+6.2f}  WR={s['wr']*100:5.1f}%  "
        f"PF={s['pf']:5.2f}  avgW={s['avg_win']:+5.1f}  avgL={s['avg_loss']:+5.1f}  "
        f"t={s['t']:+5.2f}  p={s['p']:.4f}"
    )


def exit_breakdown(trades: list[PairTrade]) -> dict:
    out = {"MEAN": 0, "SL": 0, "TIME": 0}
    for t in trades:
        out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--timeframe", choices=["H1", "H4"], default="H1")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    minutes = {"H1": 60, "H4": 240}[args.timeframe]
    window = ROLLING_WINDOW_BARS if args.timeframe == "H1" else ROLLING_WINDOW_BARS // 4

    print("=" * 110)
    print(f"PAIRS TRADING BACKTEST | TF={args.timeframe} | "
          f"Entry |z|>{ENTRY_Z}, Exit |z|<{EXIT_Z}, SL |z|>{STOP_Z} | "
          f"rolling β window={window} bars")
    print("=" * 110)

    # Load
    all_data = {}
    for pair in PAIRS_TO_TEST:
        for sym in pair:
            if sym not in all_data:
                raw = load_csv(args.data_dir / _fname(sym))
                if len(raw) == 0:
                    print(f"[WARN] no data: {sym}")
                    continue
                all_data[sym] = resample(raw, minutes)

    overall_results = []

    for y_sym, x_sym in PAIRS_TO_TEST:
        if y_sym not in all_data or x_sym not in all_data:
            continue
        ts, y, x = align(all_data[y_sym], all_data[x_sym])
        if len(ts) < window + 200:
            print(f"\n[{y_sym} ~ {x_sym}] insufficient data: {len(ts)} bars")
            continue

        cost = PAIR_COSTS[(y_sym, x_sym)]
        print(f"\n{'=' * 110}")
        print(f"{y_sym.replace('=X','')} ~ {x_sym.replace('=X','')}  |  "
              f"bars={len(ts)}  window={window}  cost_RT={cost:.1f} pips")
        print(f"{'=' * 110}")

        # Debug: посчитаем z-score distribution на всей выборке
        alphas_full, betas_full = rolling_hedge(y, x, window)
        spread_full = y - (alphas_full + betas_full * x)
        sp_mean_full = np.full(len(spread_full), np.nan)
        sp_std_full = np.full(len(spread_full), np.nan)
        for i in range(window, len(spread_full)):
            s_win = spread_full[i - window:i]
            if not np.any(np.isnan(s_win)):
                sp_mean_full[i] = np.mean(s_win)
                sp_std_full[i] = np.std(s_win)
        z_full = np.where(sp_std_full > 0, (spread_full - sp_mean_full) / sp_std_full, np.nan)
        z_valid = z_full[~np.isnan(z_full)]
        if len(z_valid) > 0:
            pct_above_2 = float(np.mean(np.abs(z_valid) > 2.0) * 100)
            print(f"  z-distribution: min={z_valid.min():.2f} max={z_valid.max():.2f} "
                  f"|z|>2σ in {pct_above_2:.1f}% of bars "
                  f"(median β={np.nanmedian(betas_full):+.3f})")

        # IS / OOS split
        n_is = int(len(ts) * IS_FRACTION)
        is_trades = backtest_pair(
            ts[:n_is], y[:n_is], x[:n_is], y_sym, x_sym, cost, window,
        )
        oos_trades = backtest_pair(
            ts[n_is:], y[n_is:], x[n_is:], y_sym, x_sym, cost, window,
        )

        s_is = summarize(is_trades, "IS")
        s_oos = summarize(oos_trades, "OOS")
        print_summary(s_is)
        print_summary(s_oos)
        print(f"    IS exits: {exit_breakdown(is_trades)}    "
              f"OOS exits: {exit_breakdown(oos_trades)}")

        if args.verbose and oos_trades:
            print("    OOS trade detail:")
            for t in oos_trades[:10]:
                dt = datetime.fromtimestamp(t.entry_ts, UTC)
                print(f"      {dt} dir={'L' if t.direction==1 else 'S'} "
                      f"z_in={t.entry_zscore:+.2f} z_out={t.exit_zscore:+.2f} "
                      f"β={t.beta:+.2f} {t.exit_reason} "
                      f"y={t.y_pnl_pips:+.1f} x={t.x_pnl_pips:+.1f} "
                      f"net={t.net_pips:+.1f}")

        overall_results.append({
            "pair": f"{y_sym.replace('=X','')}~{x_sym.replace('=X','')}",
            "is": s_is, "oos": s_oos,
            "is_trades": is_trades, "oos_trades": oos_trades,
        })

    # FINAL VERDICT
    print("\n" + "=" * 110)
    print("FINAL VERDICT (net after cost)")
    print("=" * 110)
    print(f"{'Pair':<20}{'n_IS':>6}{'IS_tot':>10}{'IS_WR':>7}{'IS_p':>8}"
          f"{'n_OOS':>7}{'OOS_tot':>10}{'OOS_WR':>7}{'OOS_p':>8}  Verdict")
    for r in overall_results:
        is_ = r["is"]
        oos = r["oos"]
        if oos["n"] < 3:
            verdict = "~ too few OOS"
        elif oos["total"] > 0 and oos["p"] < 0.15:
            verdict = "✓ PASS"
        elif oos["total"] > 0:
            verdict = "~ +OOS low signif"
        else:
            verdict = "✗ FAIL"
        print(f"  {r['pair']:<18}{is_['n']:>6}{is_['total']:>+10.1f}{is_['wr']*100:>6.1f}%{is_['p']:>8.4f}"
              f"{oos['n']:>7}{oos['total']:>+10.1f}{oos['wr']*100:>6.1f}%{oos['p']:>8.4f}  {verdict}")

    total_oos = sum(r["oos"]["total"] for r in overall_results)
    total_oos_n = sum(r["oos"]["n"] for r in overall_results)
    print(f"\nTotal OOS: {total_oos_n} trades, {total_oos:+.0f} net pips")


if __name__ == "__main__":
    main()
