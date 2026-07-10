"""Решающий эксперимент: семантика sign-decay выхода — zero-cross vs full-flip.

Находка матчинга (_momentum_gap_match.py, BUILDLOG 2026-07-10): на 60
matched-входах live −12R vs реплика +15.5R — весь разрыв в выходах.

Live (src/fx_momentum_bot/app/main.py::_momentum_sign_direction +
_flip_close_targets): позиция закрывается, когда momentum ПЕРЕСЁК НОЛЬ против
неё (long закрывается при m < 0 — даже m = −0.0001).

Реплика/бэктест (scripts/momentum_exit_backtest.py::backtest_symbol):
sign-decay срабатывает только когда signal_dir стал ПРОТИВОПОЛОЖНЫМ, т.е.
|m| > threshold=0.0015 в другую сторону (полный флип). Между нулём и
−threshold бэктест ДЕРЖИТ позицию, live — уже вышел.

Здесь: одинаковые входы (edge-trigger, как в реплике), три варианта выхода:
  full-flip  — как в бэктесте (m < −threshold для long);
  zero-cross — как в live (m < 0 для long);
  no-decay   — без sign-decay вообще (только SL/BE/partial/trail).
Окно live: 2026-06-05 → 2026-07-10, yfinance 5m→1h mid.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
from momentum_exit_backtest import (  # noqa: E402
    ATR_PERIOD, ATR_STOP_MULT, LOOKBACK, PARTIAL_FRAC, PARTIAL_R, SYMBOLS,
    THRESHOLD, TRAIL_ATR, TRAIL_R, atr, load, to_1h,
)

WINDOW_START = pd.Timestamp("2026-06-05", tz="UTC")
WINDOW_END = pd.Timestamp("2026-07-10 23:59", tz="UTC")


def backtest(df5, df1h, decay_mode: str):
    """Как backtest_symbol (BE@1.0R), но sign-decay параметризован.

    decay_mode: 'full_flip' | 'zero_cross' | 'none'.
    """
    h1_close = df1h["Close"]
    h1_atr = atr(df1h, ATR_PERIOD)
    moms, atrs = {}, {}
    closes = list(h1_close.index)
    for i, ts in enumerate(closes):
        if i >= LOOKBACK:
            moms[ts] = float(h1_close.iloc[i] / h1_close.iloc[i - LOOKBACK] - 1.0)
        else:
            moms[ts] = 0.0
        atrs[ts] = float(h1_atr.iloc[i]) if not np.isnan(h1_atr.iloc[i]) else 0.0

    def sig_dir(m: float) -> str:
        if m > THRESHOLD:
            return "long"
        if m < -THRESHOLD:
            return "short"
        return "flat"

    h1_index = pd.DatetimeIndex(closes)
    trades = []
    last_direction = "flat"
    pos = None
    last_seen_h1 = None

    for ts, bar in df5.iterrows():
        loc = h1_index.searchsorted(ts, side="right") - 1
        if loc < 0:
            continue
        h1_ts = h1_index[loc]
        m = moms[h1_ts]
        cur_dir = sig_dir(m)
        cur_atr = atrs[h1_ts]
        hi, lo, close = float(bar["High"]), float(bar["Low"]), float(bar["Close"])

        if pos is not None:
            entry, side, risk = pos["entry"], pos["side"], pos["risk"]
            if side == "long":
                if lo <= pos["sl"]:
                    r_exit = (pos["sl"] - entry) / risk
                    trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit))
                    pos = None
                else:
                    r_now = (hi - entry) / risk
                    if not pos["partial"] and r_now >= PARTIAL_R:
                        pos["realizedR"] += PARTIAL_FRAC * PARTIAL_R
                        pos["size"] -= PARTIAL_FRAC
                        pos["partial"] = True
                    if not pos["be"] and r_now >= 1.0:
                        pos["sl"] = max(pos["sl"], entry)
                        pos["be"] = True
                    if r_now >= TRAIL_R and cur_atr > 0:
                        pos["sl"] = max(pos["sl"], close - TRAIL_ATR * cur_atr)
            else:
                if hi >= pos["sl"]:
                    r_exit = (entry - pos["sl"]) / risk
                    trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit))
                    pos = None
                else:
                    r_now = (entry - lo) / risk
                    if not pos["partial"] and r_now >= PARTIAL_R:
                        pos["realizedR"] += PARTIAL_FRAC * PARTIAL_R
                        pos["size"] -= PARTIAL_FRAC
                        pos["partial"] = True
                    if not pos["be"] and r_now >= 1.0:
                        pos["sl"] = min(pos["sl"], entry)
                        pos["be"] = True
                    if r_now >= TRAIL_R and cur_atr > 0:
                        pos["sl"] = min(pos["sl"], close + TRAIL_ATR * cur_atr)

        # sign-decay на новом 1h баре
        if pos is not None and h1_ts != last_seen_h1 and decay_mode != "none":
            side = pos["side"]
            if decay_mode == "full_flip":
                fire = (side == "long" and cur_dir == "short") or \
                       (side == "short" and cur_dir == "long")
            else:  # zero_cross — live-семантика (_momentum_sign_direction)
                fire = (side == "long" and m < 0) or (side == "short" and m > 0)
            if fire:
                if side == "long":
                    r_exit = (close - pos["entry"]) / pos["risk"]
                else:
                    r_exit = (pos["entry"] - close) / pos["risk"]
                trades.append((pos["t"], pos["realizedR"] + pos["size"] * r_exit))
                pos = None

        if h1_ts != last_seen_h1:
            if pos is None and cur_dir in ("long", "short") \
                    and cur_dir != last_direction and cur_atr > 0:
                risk = cur_atr * ATR_STOP_MULT
                sl = close - risk if cur_dir == "long" else close + risk
                pos = {"t": ts, "entry": close, "side": cur_dir, "risk": risk,
                       "sl": sl, "size": 1.0, "realizedR": 0.0,
                       "be": False, "partial": False}
            last_direction = cur_dir
            last_seen_h1 = h1_ts

    return trades


def stats(label, rs):
    if not rs:
        print(f"  {label:<12} n=0")
        return
    a = np.array(rs)
    wins, losses = a[a > 0], a[a < 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    print(f"  {label:<12} n={len(a):>3} netR={a.sum():+7.2f} "
          f"WR={len(wins)/len(a)*100:>3.0f}% avgR={a.mean():+5.2f} PF={pf:>5.2f}")


def main() -> int:
    data = {}
    for sym in SYMBOLS:
        df5 = load(sym)
        if df5 is None or len(df5) < 3000:
            continue
        data[sym] = (df5, to_1h(df5))

    for mode in ("full_flip", "zero_cross", "none"):
        rs = []
        for sym, (df5, df1h) in data.items():
            for t, r in backtest(df5, df1h, mode):
                if WINDOW_START <= t <= WINDOW_END:
                    rs.append(r)
        label = {"full_flip": "full-flip (бэктест)",
                 "zero_cross": "zero-cross (LIVE)",
                 "none": "no-decay"}[mode]
        print(f"=== sign-decay: {label} ===")
        stats("ALL", rs)
        print()
    print("LIVE факт (broker): n=122 netR=-19.41 WR=30% avgR=-0.16 PF=0.63")
    return 0


if __name__ == "__main__":
    sys.exit(main())
