#!/usr/bin/env python3
"""Carry trade JPY-basket: long USDJPY + GBPJPY + EURJPY с trend filter.

Идея 2024-2026:
  USD rate ~4-5%, GBP ~4-5%, EUR ~3-4%, JPY ~0-0.5%.
  Positive rate differential → long JPY crosses = positive daily swap.

Стратегия (Daily bars):
  Entry LONG: close > SMA(200) and RSI(14) < 70 (не на overbought)
  Exit     : close < SMA(50)  (trail через среднесрочный тренд)
  SL       : 3 × ATR(14) от entry
  Max hold : 90 days
  Swap     : conservative estimate — see SWAP_PIPS_DAY

Тест:
  IS 60% / OOS 40%
  Портфель = сумма по 3 парам
  В отчёте показываем gross (без свапа) и net (со свапом)
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
from swing_rsi2_daily import resample_daily, sma, rsi  # noqa: E402

JPY_PAIRS = ["USDJPY=X", "GBPJPY=X", "EURJPY=X"]

# Эмпирическая оценка средних swap long (pips/день) для 0.01 лота на FxPro demo
# (реальные значения надо подтвердить через API, это консервативная прикидка)
SWAP_PIPS_DAY = {
    "USDJPY=X": 1.2,   # USD 5% vs JPY 0.5% ~ 4.5% / 365 ≈ 0.012% → ~1-2 pips per 150
    "GBPJPY=X": 1.5,
    "EURJPY=X": 0.5,   # ECB rate lower
}

SMA_TREND = 200
SMA_EXIT = 50
ATR_STOP_MULT = 3.0
RSI_N = 14
RSI_UPPER = 70
MAX_HOLD_DAYS = 90
IS_FRAC = 0.6


def backtest_carry(sym: str, ts_start: int, ts_end: int) -> dict:
    bars = load(sym)
    ts_d, o_d, h_d, l_d, c_d = resample_daily(bars.ts, bars.o, bars.h, bars.l, bars.c)
    pip = PIP_SIZE[sym]
    cost = cost_pips(sym)
    swap_day = SWAP_PIPS_DAY[sym]
    ma_trend = sma(c_d, SMA_TREND)
    ma_exit = sma(c_d, SMA_EXIT)
    r = rsi(c_d, RSI_N)
    a = atr(h_d, l_d, c_d, 14)

    trades = []
    in_trade = False
    direction = 0
    entry_i = 0
    entry_price = 0.0
    sl = 0.0

    for i in range(SMA_TREND, len(c_d) - 1):
        if ts_d[i] < ts_start or ts_d[i] >= ts_end:
            continue
        if in_trade:
            hi, lo = h_d[i], l_d[i]
            exited = False
            exit_price = None
            exit_reason = ""
            if lo <= sl:
                exit_price = sl
                exit_reason = "SL"
                exited = True
            elif i > entry_i and c_d[i] < ma_exit[i]:
                exit_price = c_d[i]
                exit_reason = "SMA50"
                exited = True
            elif i - entry_i >= MAX_HOLD_DAYS:
                exit_price = c_d[i]
                exit_reason = "TIME"
                exited = True
            if exited:
                days_held = i - entry_i
                pnl_g = direction * (exit_price - entry_price) / pip
                swap_earned = swap_day * days_held
                pnl_n_noswap = pnl_g - cost
                pnl_n_swap = pnl_g - cost + swap_earned
                trades.append({
                    "sym": sym, "entry_ts": int(ts_d[entry_i]), "exit_ts": int(ts_d[i]),
                    "direction": direction, "entry": entry_price, "exit": exit_price,
                    "pnl_gross": pnl_g, "pnl_net_noswap": pnl_n_noswap,
                    "pnl_net_swap": pnl_n_swap, "swap_earned": swap_earned,
                    "cost": cost, "bars_held": days_held, "reason": exit_reason,
                })
                in_trade = False
            continue

        if np.isnan(ma_trend[i]) or np.isnan(a[i]) or a[i] <= 0 or np.isnan(r[i]):
            continue
        if c_d[i] > ma_trend[i] and r[i] < RSI_UPPER:
            entry_i = i + 1
            entry_price = o_d[entry_i]
            sl = entry_price - ATR_STOP_MULT * a[i]
            direction = 1
            in_trade = True

    return _make_stats(trades, sym)


def _make_stats(trades, sym):
    if not trades:
        return {"sym": sym, "n": 0, "trades": [], "net_gross": 0, "net_swap": 0, "wr": 0,
                "pf_noswap": 0, "pf_swap": 0, "avg_days": 0, "swap_total": 0}
    nets_ns = [t["pnl_net_noswap"] for t in trades]
    nets_s = [t["pnl_net_swap"] for t in trades]
    wins_ns = [x for x in nets_ns if x > 0]
    wins_s = [x for x in nets_s if x > 0]
    losses_ns = [x for x in nets_ns if x < 0]
    losses_s = [x for x in nets_s if x < 0]
    return {
        "sym": sym, "n": len(trades), "trades": trades,
        "net_noswap": sum(nets_ns),
        "net_swap": sum(nets_s),
        "wr_noswap": len(wins_ns) / len(trades) * 100,
        "wr_swap": len(wins_s) / len(trades) * 100,
        "pf_noswap": sum(wins_ns) / abs(sum(losses_ns)) if losses_ns else float("inf"),
        "pf_swap": sum(wins_s) / abs(sum(losses_s)) if losses_s else float("inf"),
        "avg_days": np.mean([t["bars_held"] for t in trades]),
        "swap_total": sum(t["swap_earned"] for t in trades),
    }


def main() -> None:
    sample = load("USDJPY=X")
    ts_start = int(sample.ts[0])
    ts_end = int(sample.ts[-1])
    is_end = ts_start + int((ts_end - ts_start) * IS_FRAC)

    print("=" * 100)
    print("CARRY TRADE JPY-BASKET: long USDJPY + GBPJPY + EURJPY, Daily")
    print("  entry: close > SMA200 & RSI14 < 70")
    print("  exit: close < SMA50 | SL 3×ATR | max_hold 90d")
    print(f"  swap (pips/day): USDJPY={SWAP_PIPS_DAY['USDJPY=X']}, "
          f"GBPJPY={SWAP_PIPS_DAY['GBPJPY=X']}, EURJPY={SWAP_PIPS_DAY['EURJPY=X']}")
    print(f"  IS 60% ({datetime.fromtimestamp(ts_start/1000, tz=UTC).date()} → "
          f"{datetime.fromtimestamp(is_end/1000, tz=UTC).date()})")
    print(f"  OOS 40% ({datetime.fromtimestamp(is_end/1000, tz=UTC).date()} → "
          f"{datetime.fromtimestamp(ts_end/1000, tz=UTC).date()})")
    print("=" * 100)

    header = f"  {'SYM':<10}{'IS_n':>5}{'IS_gross':>10}{'IS_swap':>9}{'IS_net':>9}{'WR':>6}{'PF':>6}  {'OOS_n':>5}{'OOS_gross':>10}{'OOS_swap':>9}{'OOS_net':>9}{'WR':>6}{'PF':>6}{'Days':>6}"
    print(header)
    print("─" * len(header))

    all_is, all_oos = [], []
    for sym in JPY_PAIRS:
        r_is = backtest_carry(sym, ts_start, is_end)
        r_oos = backtest_carry(sym, is_end, ts_end)
        all_is.extend(r_is["trades"])
        all_oos.extend(r_oos["trades"])
        print(
            f"  {sym:<10}"
            f"{r_is['n']:>5}{r_is['net_noswap']:>+10.0f}{r_is['swap_total']:>+9.0f}{r_is['net_swap']:>+9.0f}"
            f"{r_is['wr_swap']:>5.0f}%{r_is['pf_swap']:>6.2f}  "
            f"{r_oos['n']:>5}{r_oos['net_noswap']:>+10.0f}{r_oos['swap_total']:>+9.0f}{r_oos['net_swap']:>+9.0f}"
            f"{r_oos['wr_swap']:>5.0f}%{r_oos['pf_swap']:>6.2f}{r_oos['avg_days']:>6.0f}"
        )
    print("─" * len(header))

    def portfolio(trades, name):
        if not trades:
            print(f"  {name}: none")
            return
        g = sum(t["pnl_gross"] for t in trades)
        sw = sum(t["swap_earned"] for t in trades)
        c = sum(t["cost"] for t in trades)
        nns = sum(t["pnl_net_noswap"] for t in trades)
        ns = sum(t["pnl_net_swap"] for t in trades)
        wins = [t for t in trades if t["pnl_net_swap"] > 0]
        losses = [t for t in trades if t["pnl_net_swap"] < 0]
        pf = (sum(t["pnl_net_swap"] for t in wins)
              / abs(sum(t["pnl_net_swap"] for t in losses))) if losses else float("inf")
        print(f"  {name}: n={len(trades)} gross={g:+.0f} cost={-c:.0f} swap={sw:+.0f} "
              f"net_noswap={nns:+.0f} net_swap={ns:+.0f} WR={len(wins)/len(trades)*100:.1f}% PF={pf:.2f}")
    portfolio(all_is, "IS  portfolio")
    portfolio(all_oos, "OOS portfolio")
    if all_oos:
        rng = np.random.default_rng(42)
        # Permute: is it better than random directions + same timing + same swap?
        gross = np.asarray([t["pnl_gross"] for t in all_oos])
        swap = np.asarray([t["swap_earned"] for t in all_oos])
        costs = np.asarray([t["cost"] for t in all_oos])
        obs = float(sum(t["pnl_net_swap"] for t in all_oos))
        ge = 0
        for _ in range(1000):
            signs = rng.choice([-1, 1], size=len(all_oos))
            # Swap уходит вместе со sign (short = -swap)
            v = (gross * signs) + (swap * signs) - costs
            if v.sum() >= obs:
                ge += 1
        print(f"  Permutation OOS p-value (random signs, same swap mag): {(ge+1)/1001:.4f}")


if __name__ == "__main__":
    main()
