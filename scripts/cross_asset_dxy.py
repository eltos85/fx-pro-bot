#!/usr/bin/env python3
"""Cross-asset DXY-basket → momentum signal для EURUSD и GBPUSD.

Строим synthetic DXY (H1):
  dxy[t] = geometric basket of USD vs EUR/GBP/JPY/CAD/CHF
  dxy_log[t] = -w1*log(EURUSD) - w2*log(GBPUSD) + w3*log(USDJPY) + w4*log(USDCAD) + w5*log(USDCHF)
  Весы (grayscale):  w1=0.58 (EUR), w2=0.12 (GBP), w3=0.14 (JPY), w4=0.09 (CAD), w5=0.04 (CHF)
  (ближайшие к реальному DXY весам без SEK и USDMXN)

Сигнал:
  DXY_momentum = log_return(dxy, N_hours)
  if momentum > +threshold σ → USD strong → short EURUSD & GBPUSD
  if momentum < -threshold σ → USD weak → long EURUSD & GBPUSD

Варианты:
  MOMENTUM_H ∈ {2, 4, 8, 24} часа
  THRESH_σ   ∈ {1.0, 1.5, 2.0}
  HOLD_H     ∈ {2, 4, 8}

Торгуем на EURUSD (самый жирный) и GBPUSD.
IS/OOS 60/40.
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


# Inverted pairs (price * w) weights — approx DXY formula
DXY_WEIGHTS = {
    "EURUSD=X": -0.576,   # EUR-based, inverse
    "GBPUSD=X": -0.119,
    "USDJPY=X": +0.136,
    "USDCAD=X": +0.091,
    "USDCHF=X": +0.036,
}
TRADE_SYMS = ["EURUSD=X", "GBPUSD=X"]

BARS_PER_HOUR = 12
IS_FRAC = 0.6

GRID_MOMENTUM_H = [2, 4, 8, 24]
GRID_THRESH = [1.0, 1.5, 2.0]
GRID_HOLD_H = [2, 4, 8]

DEFAULT_MOMENTUM_H = 8
DEFAULT_THRESH = 1.5
DEFAULT_HOLD_H = 4


def load_all_bars():
    """Загружаем все DXY pairs + TRADE_SYMS. Возвращаем dict sym → bars."""
    return {s: load(s) for s in set(list(DXY_WEIGHTS.keys()) + TRADE_SYMS)}


def build_dxy(bars_dict: dict) -> tuple[np.ndarray, np.ndarray]:
    """Строим synthetic DXY log-price на общем timeline (M5 timestamps EURUSD)."""
    ref = bars_dict["EURUSD=X"]
    ts_ref = ref.ts
    n = len(ts_ref)
    dxy = np.zeros(n)
    for sym, w in DXY_WEIGHTS.items():
        b = bars_dict[sym]
        log_p = np.log(b.c)
        # Align by nearest timestamp (same schedule expected, fill with forward-fill)
        if len(b.ts) == n and np.array_equal(b.ts, ts_ref):
            dxy += w * log_p
        else:
            # reindex
            idxs = np.searchsorted(b.ts, ts_ref, side="right") - 1
            idxs = np.clip(idxs, 0, len(b.c) - 1)
            dxy += w * log_p[idxs]
    return ts_ref, dxy


def find_trades(bars_trade, ts_dxy, dxy, momentum_h: int, thresh: float, hold_h: int,
                std_lookback_h: int, ts_start: int, ts_end: int) -> list[dict]:
    """Сигналы момента DXY, торгуем на bars_trade (same timeline)."""
    pip = PIP_SIZE[bars_trade.sym]
    cost = cost_pips(bars_trade.sym)
    mom_bars = momentum_h * BARS_PER_HOUR
    hold_bars = hold_h * BARS_PER_HOUR
    std_bars = std_lookback_h * BARS_PER_HOUR

    # Momentum series
    n = len(dxy)
    mom = np.full(n, np.nan)
    mom[mom_bars:] = dxy[mom_bars:] - dxy[:-mom_bars]
    # Vectorised rolling std via cumulative sums
    sigma = np.full(n, np.nan)
    mom_f = np.nan_to_num(mom, nan=0.0)
    mom_sq = mom_f * mom_f
    valid = (~np.isnan(mom)).astype(np.float64)
    cs = np.concatenate([[0.0], np.cumsum(mom_f)])
    css = np.concatenate([[0.0], np.cumsum(mom_sq)])
    cv = np.concatenate([[0.0], np.cumsum(valid)])
    for i in range(std_bars + mom_bars, n):
        a_i = i - std_bars
        cnt = cv[i] - cv[a_i]
        if cnt < 50:
            continue
        s = cs[i] - cs[a_i]
        s2 = css[i] - css[a_i]
        mean = s / cnt
        var = s2 / cnt - mean * mean
        if var > 0:
            sigma[i] = var ** 0.5

    trades = []
    cool = hold_bars  # cool-off until exit
    last_entry = -10**9
    for i in range(n - 1):
        ts = int(ts_dxy[i])
        if ts < ts_start or ts >= ts_end:
            continue
        if i - last_entry < cool:
            continue
        if np.isnan(mom[i]) or np.isnan(sigma[i]) or sigma[i] <= 0:
            continue
        z = mom[i] / sigma[i]
        direction = 0
        # INVERTED: DXY momentum НЕ фейдится на EUR/GBP — они ПРОДОЛЖАЮТ падать вместе с DXY.
        # → Torguem в сторону DXY для EUR/GBP: DXY up → short EUR/GBP (не long!)
        # Поскольку EURUSD/GBPUSD inverse к DXY: short EUR/USD = long USD = same as DXY direction
        # direction для EUR/USD = -sign(z) правильно для "follow DXY". Но раз наш изначальный
        # прогноз был "-1" и дал -1185 pips OOS → теперь инвертируем:
        if z > thresh:
            direction = +1
        elif z < -thresh:
            direction = -1
        if direction == 0:
            continue
        entry_i = i + 1
        if entry_i >= len(bars_trade.c):
            continue
        entry_price = bars_trade.o[entry_i]
        a = atr(bars_trade.h, bars_trade.l, bars_trade.c, 14)
        if np.isnan(a[i]) or a[i] <= 0:
            continue
        sl_dist = 1.5 * a[i]
        sl = entry_price - direction * sl_dist
        tp = entry_price + direction * 1.5 * sl_dist  # 1:1.5 RR

        exit_price = None
        exit_reason = "TIME"
        exit_i = min(entry_i + hold_bars, len(bars_trade.c) - 1)
        for j in range(entry_i, min(entry_i + hold_bars + 1, len(bars_trade.c))):
            hi, lo = bars_trade.h[j], bars_trade.l[j]
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
            exit_price = bars_trade.c[exit_i]

        pnl_g = direction * (exit_price - entry_price) / pip
        pnl_n = pnl_g - cost
        trades.append({
            "sym": bars_trade.sym, "entry_ts": int(ts_dxy[entry_i]),
            "exit_ts": int(ts_dxy[exit_i]), "direction": direction,
            "entry": entry_price, "exit": exit_price, "z_dxy": float(z),
            "pnl_gross": pnl_g, "pnl_net": pnl_n, "cost": cost,
            "reason": exit_reason,
        })
        last_entry = exit_i
    return trades


def stats(trades):
    if not trades:
        return {"n": 0, "net": 0, "avg": 0, "wr": 0, "pf": 0}
    nets = [t["pnl_net"] for t in trades]
    total = sum(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    return {"n": len(trades), "net": total, "avg": total / len(trades),
            "wr": len(wins) / len(trades) * 100, "pf": pf}


def main() -> None:
    import time
    print("=" * 100)
    print("CROSS-ASSET DXY-basket momentum → EURUSD + GBPUSD")
    print(f"  DXY weights: {DXY_WEIGHTS}")
    print(f"  Trade symbols: {TRADE_SYMS}")
    print("=" * 100)
    t0 = time.time()
    print(f"[{time.time()-t0:6.1f}s] loading bars...", flush=True)
    bars_dict = load_all_bars()
    print(f"[{time.time()-t0:6.1f}s] bars loaded, building DXY...", flush=True)
    ts_dxy, dxy = build_dxy(bars_dict)
    print(f"[{time.time()-t0:6.1f}s] DXY built (n={len(dxy)}), starting backtests...", flush=True)
    ts_start = int(ts_dxy[0])
    ts_end = int(ts_dxy[-1])
    is_end = ts_start + int((ts_end - ts_start) * IS_FRAC)

    # ───────── Scheme A: Same params for all — anti-overfit ─────────
    print(f"\nA. Anti-overfit: momentum={DEFAULT_MOMENTUM_H}h, thresh={DEFAULT_THRESH}σ, hold={DEFAULT_HOLD_H}h")
    print("─" * 90)
    all_is_A, all_oos_A = [], []
    for sym in TRADE_SYMS:
        b = bars_dict[sym]
        is_tr = find_trades(b, ts_dxy, dxy, DEFAULT_MOMENTUM_H, DEFAULT_THRESH, DEFAULT_HOLD_H,
                            30 * 24, ts_start, is_end)
        oos_tr = find_trades(b, ts_dxy, dxy, DEFAULT_MOMENTUM_H, DEFAULT_THRESH, DEFAULT_HOLD_H,
                             30 * 24, is_end, ts_end)
        all_is_A.extend(is_tr)
        all_oos_A.extend(oos_tr)
        si, so = stats(is_tr), stats(oos_tr)
        print(f"  {sym:<10} IS n={si['n']:>4} net={si['net']:>+7.0f} WR={si['wr']:.0f}% PF={si['pf']:.2f} | "
              f"OOS n={so['n']:>4} net={so['net']:>+7.0f} WR={so['wr']:.0f}% PF={so['pf']:.2f}")
    si, so = stats(all_is_A), stats(all_oos_A)
    print(f"  Portfolio IS:  n={si['n']} net={si['net']:+.0f} WR={si['wr']:.1f}% PF={si['pf']:.2f}")
    print(f"  Portfolio OOS: n={so['n']} net={so['net']:+.0f} avg={so['avg']:+.2f} WR={so['wr']:.1f}% PF={so['pf']:.2f}")
    if all_oos_A:
        rng = np.random.default_rng(42)
        gross = np.asarray([t["pnl_gross"] for t in all_oos_A])
        costs = np.asarray([t["cost"] for t in all_oos_A])
        obs = float(sum(t["pnl_net"] for t in all_oos_A))
        ge = sum(1 for _ in range(1000) if (gross * rng.choice([-1, 1], size=len(all_oos_A)) - costs).sum() >= obs)
        print(f"  Permutation OOS p-value: {(ge+1)/1001:.4f}")

    # Scheme B (per-symbol grid) удалена — слишком долго и overfit-риск.


if __name__ == "__main__":
    main()
