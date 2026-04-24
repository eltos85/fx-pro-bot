#!/usr/bin/env python3
"""Backtest Hypothesis C: Late Session (21-00 UTC) Mean-Reversion.

Из EDA 2 года (H3 block):
  EURGBP Late (21-00 UTC): ACF(1) = -0.255  ⚡ в 5× сильнее остального дня
  GBPJPY Late (21-00 UTC): ACF(1) = -0.086
  AUDUSD Late (21-00 UTC): ACF(1) = -0.086
  EURJPY Late (21-00 UTC): ACF(1) = -0.080
  USDCAD Late (21-00 UTC): ACF(1) = -0.058
  USDCHF Late (21-00 UTC): ACF(1) = -0.058

Сильная отрицательная автокорреляция → последовательные бары идут в противоход.

Логика (M5, не H1):
  • Только в часы 21,22,23 UTC.
  • Trigger: M5 bar закрытии > 1σ отклонение от session VWAP или > 1.5×ATR(14 M5).
  • Entry: в направлении, противоположном движению (fade).
  • Exit: 6 M5 bars (30 мин) time-stop или SL=0.8×ATR.

Тестируем все 6 символов из EDA + портфель.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Callable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scalp_setups_m5 import (  # noqa: E402
    Bars,
    PIP_SIZE,
    atr,
    cost_pips,
    load,
    permutation_test,
    simulate_trade,
    split_trades_is_oos,
    stats_from,
    Trade,
)


# ──────────────────── Параметры (консервативные, не оптимизировали) ────────────────────
LATE_HOURS = (21, 22, 23)                # UTC
LOOKBACK_BARS = 12                       # 1 час M5 — для session-local std
ENTRY_SIGMA = 1.0                        # |bar return| > 1σ rolling 12-bar std
ATR_N = 14
SL_ATR_MULT = 0.8                        # SL = 0.8×ATR
TP_ATR_MULT = 1.2                        # TP = 1.2×ATR (R:R = 1.5)
MAX_HOLD_BARS = 6                        # 30 min time-stop
COOL_OFF_BARS = 3                        # 15 мин между trades

SYMBOLS = ["EURGBP=X", "GBPJPY=X", "AUDUSD=X", "EURJPY=X", "USDCAD=X", "USDCHF=X"]


def late_session_mr(bars: Bars) -> list[Trade]:
    """Fade M5 bar returns > 1σ в часы 21-23 UTC."""
    a = atr(bars.h, bars.l, bars.c, ATR_N)
    log_c = np.log(bars.c)
    n = len(bars.c)
    bar_ret = np.full(n, np.nan)
    bar_ret[1:] = log_c[1:] - log_c[:-1]

    trades: list[Trade] = []
    last = -10**9
    for i in range(LOOKBACK_BARS + ATR_N, n - 1):
        if bars.hour[i] not in LATE_HOURS:
            continue
        if i - last < COOL_OFF_BARS:
            continue
        if np.isnan(a[i]) or a[i] <= 0:
            continue
        w = bar_ret[i - LOOKBACK_BARS:i]
        w = w[~np.isnan(w)]
        if len(w) < 8:
            continue
        s = float(np.std(w))
        if s <= 0:
            continue
        z = bar_ret[i] / s if not np.isnan(bar_ret[i]) else 0
        if abs(z) < ENTRY_SIGMA:
            continue
        # Fade: direction opposite to bar move
        direction = -int(np.sign(bar_ret[i]))
        if direction == 0:
            continue
        entry_price = bars.c[i]
        if direction == 1:
            sl = entry_price - SL_ATR_MULT * a[i]
            tp = entry_price + TP_ATR_MULT * a[i]
        else:
            sl = entry_price + SL_ATR_MULT * a[i]
            tp = entry_price - TP_ATR_MULT * a[i]
        t = simulate_trade(bars, i, direction, sl, tp, MAX_HOLD_BARS, "late_mr")
        if t is not None:
            trades.append(t)
            last = i
    return trades


def summarize(label: str, trades: list[Trade]) -> dict:
    is_, oos = split_trades_is_oos(trades, is_frac=0.6)
    st_all = stats_from("late_mr", label, trades)
    st_is = stats_from("late_mr", "IS", is_)
    st_oos = stats_from("late_mr", "OOS", oos)
    p_all = permutation_test(trades, n_perm=1000) if trades else 1.0
    p_oos = permutation_test(oos, n_perm=1000) if oos else 1.0
    return {
        "label": label,
        "n": st_all.n,
        "net": round(st_all.net_pips, 1),
        "is_net": round(st_is.net_pips, 1),
        "n_is": st_is.n,
        "oos_net": round(st_oos.net_pips, 1),
        "n_oos": st_oos.n,
        "wr": round(st_all.wr * 100, 1),
        "pf": round(st_all.pf, 2) if st_all.pf != float("inf") else 999.99,
        "avg": round(st_all.avg_net, 2),
        "p": round(p_all, 4),
        "p_oos": round(p_oos, 4),
    }


def main() -> None:
    print("=" * 100)
    print("Hypothesis C: Late Session (21-23 UTC) Mean-Reversion")
    print(f"  Trigger: |M5 bar return| > {ENTRY_SIGMA}σ (local 12-bar std)")
    print(f"  SL={SL_ATR_MULT}×ATR, TP={TP_ATR_MULT}×ATR, MaxHold={MAX_HOLD_BARS} bars (30min)")
    print("=" * 100)

    rows = []
    all_trades: list[Trade] = []
    for sym in SYMBOLS:
        bars = load(sym)
        trades = late_session_mr(bars)
        row = summarize(f"{sym}", trades)
        all_trades.extend(trades)
        print(
            f"  {sym:10s} n={row['n']:4d}  net={row['net']:+8.1f}  "
            f"wr={row['wr']:5.1f}%  pf={row['pf']:5.2f}  avg={row['avg']:+6.2f}  "
            f"IS={row['is_net']:+7.1f}  OOS={row['oos_net']:+7.1f}  "
            f"p={row['p']:.4f}  p_oos={row['p_oos']:.4f}"
        )
        rows.append(row)

    print()
    print("=" * 100)
    print("PORTFOLIO (6 pairs)")
    print("=" * 100)
    port = summarize("PORTFOLIO", all_trades)
    days = 2 * 365 * 3 / 24  # ~156 late session периодов
    print(
        f"  n={port['n']}  net={port['net']:+.1f} pips  wr={port['wr']}%  pf={port['pf']}\n"
        f"  IS = {port['is_net']:+.1f} (n={port['n_is']})\n"
        f"  OOS = {port['oos_net']:+.1f} (n={port['n_oos']})\n"
        f"  avg = {port['avg']:+.2f} pips/trade\n"
        f"  p_all = {port['p']}  p_oos = {port['p_oos']}"
    )
    rows.append(port)

    out = Path(__file__).resolve().parents[1] / "data" / "backtest_late_session_mr.csv"
    with out.open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n  Сохранено: {out}")


if __name__ == "__main__":
    main()
