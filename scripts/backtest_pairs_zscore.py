#!/usr/bin/env python3
"""Backtest Hypothesis B: Pairs Trading z-score mean-reversion.

Из EDA H1 (bottom_up_analysis 2 года) выделены 4 пары со стабильной высокой
корреляцией (std<0.12, |mean|>0.68) — кандидаты для pairs trading:

  EURJPY ~ GBPJPY    mean=+0.839  std=0.067    (100% |c|>0.5)
  EURUSD ~ GBPUSD    mean=+0.781  std=0.055    (100%)
  EURUSD ~ USDCHF    mean=-0.727  std=0.104    (97%)
  AUDUSD ~ USDCAD    mean=-0.686  std=0.057    (100%)

Логика:
  • На H1 считаем OLS hedge ratio β: P_B = α + β·P_A (rolling 60d)
  • spread = P_B - β·P_A (в лог-ценах для стабильности)
  • z = (spread - mean_60d) / std_60d
  • Entry LONG_SPREAD (long B, short A): z < -2
  • Entry SHORT_SPREAD (short B, long A): z > +2
  • Exit: |z| < 0.5 или time-stop 5 дней
  • Косты учтены по обеим ногам
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scalp_setups_m5 import (  # noqa: E402
    PIP_SIZE,
    cost_pips,
    load,
    permutation_test,
    split_trades_is_oos,
    stats_from,
    Trade,
)


# ──────────────────── Параметры ────────────────────
PAIRS = [
    ("EURJPY=X", "GBPJPY=X"),
    ("EURUSD=X", "GBPUSD=X"),
    ("EURUSD=X", "USDCHF=X"),
    ("AUDUSD=X", "USDCAD=X"),
]

RESAMPLE_H = 1                       # работаем на H1 (12× M5)
BARS_PER_HOUR = 12
ROLLING_WIN_H = 30 * 24              # 30 дней (в часах)
ENTRY_Z = 2.0
EXIT_Z = 0.5
MAX_HOLD_H = 5 * 24                  # 5 дней
COOL_OFF_H = 2                       # между сделками в пределах одной пары


def to_h1(bars) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """M5 → H1: timestamps align by floor(ts/3600s)."""
    ts = bars.ts
    o = bars.o
    h = bars.h
    l = bars.l
    c = bars.c
    # group_key = hour_idx
    hour = (ts // (3600 * 1000)).astype(np.int64)
    idx0 = 0
    out_ts, out_o, out_h, out_l, out_c = [], [], [], [], []
    for i in range(1, len(ts) + 1):
        if i == len(ts) or hour[i] != hour[idx0]:
            out_ts.append(int(hour[idx0] * 3600 * 1000))
            out_o.append(float(o[idx0]))
            out_h.append(float(h[idx0:i].max()))
            out_l.append(float(l[idx0:i].min()))
            out_c.append(float(c[i - 1]))
            idx0 = i
    return (
        np.asarray(out_ts, dtype=np.int64),
        np.asarray(out_o, dtype=np.float64),
        np.asarray(out_h, dtype=np.float64),
        np.asarray(out_l, dtype=np.float64),
        np.asarray(out_c, dtype=np.float64),
    )


def align_two(ts_a: np.ndarray, c_a: np.ndarray, ts_b: np.ndarray, c_b: np.ndarray):
    """Пересечение по timestamp. Returns (ts, cA, cB)."""
    set_a = set(ts_a.tolist())
    set_b = set(ts_b.tolist())
    common = sorted(set_a & set_b)
    common_arr = np.asarray(common, dtype=np.int64)
    map_a = {int(t): i for i, t in enumerate(ts_a)}
    map_b = {int(t): i for i, t in enumerate(ts_b)}
    idx_a = np.asarray([map_a[t] for t in common], dtype=np.int64)
    idx_b = np.asarray([map_b[t] for t in common], dtype=np.int64)
    return common_arr, c_a[idx_a], c_b[idx_b]


def rolling_ols_beta_alpha(
    x: np.ndarray, y: np.ndarray, win: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Rolling OLS y = α + β·x. Returns α[i], β[i]."""
    n = len(x)
    alpha = np.full(n, np.nan)
    beta = np.full(n, np.nan)
    for i in range(win, n):
        xw = x[i - win:i]
        yw = y[i - win:i]
        mean_x = xw.mean()
        mean_y = yw.mean()
        var_x = ((xw - mean_x) ** 2).sum()
        cov = ((xw - mean_x) * (yw - mean_y)).sum()
        if var_x <= 0:
            continue
        b = cov / var_x
        a = mean_y - b * mean_x
        alpha[i] = a
        beta[i] = b
    return alpha, beta


def rolling_stats(x: np.ndarray, win: int) -> tuple[np.ndarray, np.ndarray]:
    n = len(x)
    m = np.full(n, np.nan)
    s = np.full(n, np.nan)
    for i in range(win, n):
        w = x[i - win:i]
        m[i] = w.mean()
        s[i] = w.std(ddof=0)
    return m, s


@dataclass
class PairTrade:
    pair: str
    entry_ts: int
    exit_ts: int
    direction: int          # +1 long spread (long B, short A); -1 short spread
    pnl_pips_A: float
    pnl_pips_B: float
    cost_A: float
    cost_B: float
    pnl_net: float
    exit_reason: str
    z_entry: float


def simulate_pair(sym_a: str, sym_b: str) -> list[PairTrade]:
    bars_a = load(sym_a)
    bars_b = load(sym_b)
    ts_a, _, _, _, c_a = to_h1(bars_a)
    ts_b, _, _, _, c_b = to_h1(bars_b)
    ts, ca, cb = align_two(ts_a, c_a, ts_b, c_b)
    print(f"  {sym_a}~{sym_b}: H1 aligned {len(ts)} bars")

    # hedge ratio — в лог-ценах
    lcA = np.log(ca)
    lcB = np.log(cb)
    alpha, beta = rolling_ols_beta_alpha(lcA, lcB, ROLLING_WIN_H)

    # spread
    spread = lcB - beta * lcA - alpha
    m, s = rolling_stats(spread, ROLLING_WIN_H)
    z = (spread - m) / np.where(s > 0, s, np.nan)

    trades: list[PairTrade] = []
    pos = 0                  # 0 = flat, +1 long spread, -1 short spread
    entry_i = -1
    entry_z = 0.0
    cool_off_end = -10**9
    for i in range(ROLLING_WIN_H, len(ts) - 1):
        if np.isnan(z[i]) or np.isnan(beta[i]):
            continue
        if pos == 0:
            if i < cool_off_end:
                continue
            if z[i] > ENTRY_Z:
                pos = -1
                entry_i = i
                entry_z = float(z[i])
            elif z[i] < -ENTRY_Z:
                pos = +1
                entry_i = i
                entry_z = float(z[i])
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
            elif (pos > 0 and z[i] > entry_z + 2.0) or (pos < 0 and z[i] < entry_z - 2.0):
                # широкий stop-loss: z уходит ещё на 2σ против входа
                exit_now = True
                reason = "zstop"
            if exit_now:
                # PnL в пипсах для каждой ноги
                # B нога: если pos=+1 → long B → pnl = (cb[i] - cb[entry_i]) / pip_B
                # A нога: если pos=+1 → short A → pnl = (ca[entry_i] - ca[i]) / pip_A, weighted by beta
                pip_a = PIP_SIZE[sym_a]
                pip_b = PIP_SIZE[sym_b]
                b_dir = pos
                a_dir = -pos  # противоположно B
                pnl_b = b_dir * (cb[i] - cb[entry_i]) / pip_b
                pnl_a = a_dir * (ca[i] - ca[entry_i]) / pip_a * abs(beta[entry_i]) * (pip_b / pip_a) * (ca[entry_i] / cb[entry_i])
                # Проще: веса в долях notional, но для простоты считаем равные веса (hedge ratio = price neutral)
                # Альтернатива (упрощение): pnl_a = a_dir * (ca[i] - ca[entry_i]) / pip_a * abs(beta[entry_i])
                pnl_a_simple = a_dir * (ca[i] - ca[entry_i]) / pip_a * abs(beta[entry_i])
                cost_a = cost_pips(sym_a)
                cost_b = cost_pips(sym_b)
                net = pnl_b + pnl_a_simple - cost_a - cost_b
                trades.append(PairTrade(
                    pair=f"{sym_a}~{sym_b}",
                    entry_ts=int(ts[entry_i]),
                    exit_ts=int(ts[i]),
                    direction=pos,
                    pnl_pips_A=pnl_a_simple,
                    pnl_pips_B=pnl_b,
                    cost_A=cost_a,
                    cost_B=cost_b,
                    pnl_net=net,
                    exit_reason=reason,
                    z_entry=entry_z,
                ))
                pos = 0
                cool_off_end = i + COOL_OFF_H
    return trades


def stats_from_pair(trades: list[PairTrade], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0, "net": 0.0, "wr": 0.0, "pf": 0.0,
                "avg": 0.0, "is_net": 0.0, "oos_net": 0.0, "p": 1.0, "p_oos": 1.0}
    ts_sorted = sorted(trades, key=lambda t: t.entry_ts)
    cutoff = ts_sorted[int(len(ts_sorted) * 0.6)].entry_ts
    is_ = [t for t in ts_sorted if t.entry_ts < cutoff]
    oos = [t for t in ts_sorted if t.entry_ts >= cutoff]
    wins = [t.pnl_net for t in trades if t.pnl_net > 0]
    losses = [t.pnl_net for t in trades if t.pnl_net < 0]
    net = sum(t.pnl_net for t in trades)
    pf = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0)
    # permutation: shuffle signs
    rng = np.random.default_rng(42)
    gross = np.asarray([t.pnl_pips_A + t.pnl_pips_B for t in trades])
    costs = np.asarray([t.cost_A + t.cost_B for t in trades])
    obs = float(net)
    ge = 0
    n_perm = 1000
    for _ in range(n_perm):
        signs = rng.choice([-1, 1], size=len(trades))
        perm = (gross * signs - costs).sum()
        if perm >= obs:
            ge += 1
    p = (ge + 1) / (n_perm + 1)

    gross_o = np.asarray([t.pnl_pips_A + t.pnl_pips_B for t in oos])
    costs_o = np.asarray([t.cost_A + t.cost_B for t in oos])
    obs_o = sum(t.pnl_net for t in oos)
    ge_o = 0
    if oos:
        for _ in range(n_perm):
            signs = rng.choice([-1, 1], size=len(oos))
            perm = (gross_o * signs - costs_o).sum()
            if perm >= obs_o:
                ge_o += 1
    p_oos = (ge_o + 1) / (n_perm + 1)

    return {
        "label": label, "n": len(trades),
        "net": round(net, 1),
        "is_net": round(sum(t.pnl_net for t in is_), 1),
        "oos_net": round(sum(t.pnl_net for t in oos), 1),
        "n_is": len(is_), "n_oos": len(oos),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "pf": round(pf, 2) if pf != float("inf") else 999.99,
        "avg": round(net / len(trades), 2),
        "p": round(p, 4), "p_oos": round(p_oos, 4),
    }


def main() -> None:
    print("=" * 100)
    print("Hypothesis B: Pairs Trading z-score (mean-reversion)")
    print(f"  Rolling win: {ROLLING_WIN_H}h ({ROLLING_WIN_H//24}d), Entry |z|>{ENTRY_Z}, Exit |z|<{EXIT_Z}, MaxHold {MAX_HOLD_H}h")
    print("=" * 100)

    out_rows = []
    all_trades: list[PairTrade] = []
    for sym_a, sym_b in PAIRS:
        try:
            trades = simulate_pair(sym_a, sym_b)
        except Exception as e:
            print(f"  !!! {sym_a}~{sym_b} failed: {e}")
            continue
        if not trades:
            print(f"  {sym_a}~{sym_b}: NO trades")
            continue
        stats = stats_from_pair(trades, f"{sym_a}~{sym_b}")
        print(
            f"    n={stats['n']:4d}  net={stats['net']:+9.1f}  "
            f"wr={stats['wr']:5.1f}%  pf={stats['pf']:5.2f}  "
            f"avg={stats['avg']:+6.2f}  "
            f"IS={stats['is_net']:+8.1f}  OOS={stats['oos_net']:+8.1f}  "
            f"p={stats['p']:.4f}  p_oos={stats['p_oos']:.4f}"
        )
        out_rows.append(stats)
        all_trades.extend(trades)

    print()
    print("=" * 100)
    print("PORTFOLIO of 4 pairs")
    print("=" * 100)
    if all_trades:
        portfolio = stats_from_pair(all_trades, "PORTFOLIO")
        days = (max(t.exit_ts for t in all_trades) - min(t.entry_ts for t in all_trades)) / (86400 * 1000)
        print(
            f"  n={portfolio['n']}  (~{portfolio['n']/max(days,1):.2f}/day over {int(days)} days)\n"
            f"  NET  = {portfolio['net']:+.1f} pips\n"
            f"  IS   = {portfolio['is_net']:+.1f} (n={portfolio['n_is']})\n"
            f"  OOS  = {portfolio['oos_net']:+.1f} (n={portfolio['n_oos']})\n"
            f"  WR   = {portfolio['wr']}%  PF = {portfolio['pf']}\n"
            f"  p_all= {portfolio['p']}  p_oos = {portfolio['p_oos']}"
        )
        out_rows.append(portfolio)

    out = Path(__file__).resolve().parents[1] / "data" / "backtest_pairs_zscore.csv"
    with out.open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"\n  Сохранено: {out}")


if __name__ == "__main__":
    main()
