#!/usr/bin/env python3
"""Swing H4 Turtle-style breakout (классический Turtle 1983, упрощённый).

Вход:
  LONG  — пробой max(High, 20 дней) вверх
  SHORT — пробой min(Low, 20 дней) вниз

Выход:
  • Stop-loss: 2 × ATR(20) от entry
  • Trailing: противоположный breakout 10 дней
  • Time-stop: N дней (default 30)

Нет pyramiding (по одному entry за breakout).
Cool-off: после SL/exit ждём минимум 1 bar H4.

Тестируем на всех FX + commodities.
IS: первые 60% данных.  OOS: последние 40%.
Permutation test для OOS.
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

BARS_PER_HOUR = 12       # M5 → 1h
M5_PER_H4 = 48           # 4h * 12 = 48 M5 bars

ENTRY_LOOKBACK_DAYS = 20  # донской канал 20-дневный
EXIT_LOOKBACK_DAYS = 10   # trailing 10-дневный
ATR_STOP_MULT = 2.0
MAX_HOLD_DAYS = 30
COOL_OFF_BARS_H4 = 1      # после закрытия

SYMS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X",
    "AUDUSD=X", "USDCAD=X", "USDCHF=X",
    "EURGBP=X", "EURJPY=X", "GBPJPY=X",
    "GC=F", "CL=F", "BZ=F",
]

IS_FRAC = 0.6


# ──────────────────── M5 → H4 resample ────────────────────

def resample_h4(ts_m5: np.ndarray, o, h, l, c):
    """Простой resample M5 → H4 группировкой по (ts % 4h)."""
    h4_ms = 4 * 3600 * 1000
    # Каждые 48 M5 bars = 1 H4 bar
    n = len(ts_m5)
    # Выравниваем по H4 boundary
    ts0 = int(ts_m5[0] // h4_ms) * h4_ms
    ts4 = np.arange(ts0, int(ts_m5[-1]) + h4_ms, h4_ms, dtype=np.int64)
    nh = len(ts4)
    o4 = np.full(nh, np.nan)
    h4 = np.full(nh, np.nan)
    l4 = np.full(nh, np.nan)
    c4 = np.full(nh, np.nan)
    # Индексы bucket
    buckets = ((ts_m5 - ts0) // h4_ms).astype(np.int64)
    buckets = np.clip(buckets, 0, nh - 1)
    # Проход
    prev_b = -1
    for i in range(n):
        b = int(buckets[i])
        if b != prev_b:
            if np.isnan(o4[b]):
                o4[b] = o[i]
                h4[b] = h[i]
                l4[b] = l[i]
            prev_b = b
        h4[b] = max(h4[b], h[i]) if not np.isnan(h4[b]) else h[i]
        l4[b] = min(l4[b], l[i]) if not np.isnan(l4[b]) else l[i]
        c4[b] = c[i]
    # Убираем пустые bars (выходные, пробелы)
    mask = ~np.isnan(c4)
    return ts4[mask], o4[mask], h4[mask], l4[mask], c4[mask]


# ──────────────────── Rolling max/min ────────────────────

def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    r = np.full_like(x, np.nan, dtype=np.float64)
    for i in range(n - 1, len(x)):
        r[i] = x[i - n + 1:i + 1].max()
    return r


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    r = np.full_like(x, np.nan, dtype=np.float64)
    for i in range(n - 1, len(x)):
        r[i] = x[i - n + 1:i + 1].min()
    return r


# ──────────────────── Backtest ────────────────────

def backtest_turtle_h4(sym: str, start_ts: int, end_ts: int) -> dict:
    bars = load(sym)
    ts4, o4, h4, l4, c4 = resample_h4(bars.ts, bars.o, bars.h, bars.l, bars.c)
    pip = PIP_SIZE[sym]
    # Отфильтровать H4 bars в окне
    mask = (ts4 >= start_ts) & (ts4 < end_ts)
    # Но для lookback нам нужны и предыдущие bars — считаем индикаторы на всём
    bars_per_day = 6  # H4
    entry_n = ENTRY_LOOKBACK_DAYS * bars_per_day
    exit_n = EXIT_LOOKBACK_DAYS * bars_per_day
    atr_n = 14
    max_hold = MAX_HOLD_DAYS * bars_per_day

    upper = rolling_max(h4, entry_n)
    lower = rolling_min(l4, entry_n)
    exit_up = rolling_max(h4, exit_n)
    exit_dn = rolling_min(l4, exit_n)
    a = atr(h4, l4, c4, atr_n)

    trades = []
    cost = cost_pips(sym)
    n = len(c4)

    i = entry_n
    while i < n - 1:
        if ts4[i] < start_ts or ts4[i] >= end_ts:
            i += 1
            continue
        if np.isnan(upper[i-1]) or np.isnan(a[i-1]) or a[i-1] <= 0:
            i += 1
            continue
        # Breakout detection: high[i] пробивает upper[i-1]
        direction = 0
        entry_price = None
        if h4[i] > upper[i-1]:
            direction = 1
            entry_price = upper[i-1]  # заходим по уровню breakout
        elif l4[i] < lower[i-1]:
            direction = -1
            entry_price = lower[i-1]
        if direction == 0:
            i += 1
            continue

        stop_dist = ATR_STOP_MULT * a[i-1]
        if direction == 1:
            sl = entry_price - stop_dist
        else:
            sl = entry_price + stop_dist
        entry_i = i
        entry_ts = int(ts4[i])
        exit_price = None
        exit_reason = "TIME"
        exit_i = min(entry_i + max_hold, n - 1)

        for j in range(entry_i, min(entry_i + max_hold + 1, n)):
            if direction == 1:
                if l4[j] <= sl:
                    exit_price = sl
                    exit_reason = "SL"
                    exit_i = j
                    break
                # Trailing: если закрылись ниже exit_dn[j-1] → выход
                if j > entry_i and not np.isnan(exit_dn[j-1]) and l4[j] < exit_dn[j-1]:
                    exit_price = exit_dn[j-1]
                    exit_reason = "TRAIL"
                    exit_i = j
                    break
            else:
                if h4[j] >= sl:
                    exit_price = sl
                    exit_reason = "SL"
                    exit_i = j
                    break
                if j > entry_i and not np.isnan(exit_up[j-1]) and h4[j] > exit_up[j-1]:
                    exit_price = exit_up[j-1]
                    exit_reason = "TRAIL"
                    exit_i = j
                    break
        if exit_price is None:
            exit_price = c4[exit_i]
            exit_reason = "TIME"

        pnl_gross = direction * (exit_price - entry_price) / pip
        pnl_net = pnl_gross - cost
        trades.append({
            "sym": sym,
            "entry_ts": entry_ts,
            "exit_ts": int(ts4[exit_i]),
            "direction": direction,
            "entry": entry_price,
            "exit": exit_price,
            "pnl_gross": pnl_gross,
            "pnl_net": pnl_net,
            "cost": cost,
            "bars_held": exit_i - entry_i,
            "reason": exit_reason,
        })
        i = exit_i + COOL_OFF_BARS_H4 + 1

    # Stats
    if not trades:
        return {"sym": sym, "n": 0, "net": 0, "wr": 0, "pf": 0, "avg": 0, "avg_hold_days": 0, "trades": []}
    nets = [t["pnl_net"] for t in trades]
    net_total = sum(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    pf = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0)
    wr = len(wins) / len(trades) * 100
    avg = net_total / len(trades)
    avg_hold = np.mean([t["bars_held"] for t in trades]) / bars_per_day
    return {
        "sym": sym, "n": len(trades), "net": net_total, "wr": wr, "pf": pf,
        "avg": avg, "avg_hold_days": avg_hold, "trades": trades,
    }


# ──────────────────── Main ────────────────────

def main() -> None:
    print("=" * 100)
    print("SWING H4 TURTLE BREAKOUT — 12 инструментов, 2 года")
    print(f"  entry=max/min(20d)  stop=2×ATR(14)  trail=10d  max_hold=30d")
    print("=" * 100)

    # Determine global date range from GBPJPY (proxy)
    sample = load("GBPJPY=X")
    ts_start = int(sample.ts[0])
    ts_end = int(sample.ts[-1])
    total = ts_end - ts_start
    is_end = ts_start + int(total * IS_FRAC)
    print(f"  IS:  {datetime.fromtimestamp(ts_start/1000, tz=UTC).date()} → "
          f"{datetime.fromtimestamp(is_end/1000, tz=UTC).date()}")
    print(f"  OOS: {datetime.fromtimestamp(is_end/1000, tz=UTC).date()} → "
          f"{datetime.fromtimestamp(ts_end/1000, tz=UTC).date()}")
    print()

    all_is, all_oos = [], []
    header = f"  {'SYM':<10}{'IS_n':>6}{'IS_net':>10}{'IS_wr':>7}{'IS_pf':>7}  {'OOS_n':>6}{'OOS_net':>10}{'OOS_wr':>7}{'OOS_pf':>7}{'Avg_pips':>10}{'Hold_d':>8}"
    print(header)
    print("─" * len(header))

    for sym in SYMS:
        is_r = backtest_turtle_h4(sym, ts_start, is_end)
        oos_r = backtest_turtle_h4(sym, is_end, ts_end)
        all_is.extend(is_r["trades"])
        all_oos.extend(oos_r["trades"])
        print(
            f"  {sym:<10}"
            f"{is_r['n']:>6}{is_r['net']:>+10.0f}{is_r['wr']:>6.1f}%{is_r['pf']:>7.2f}  "
            f"{oos_r['n']:>6}{oos_r['net']:>+10.0f}{oos_r['wr']:>6.1f}%{oos_r['pf']:>7.2f}"
            f"{oos_r['avg']:>+10.2f}{oos_r['avg_hold_days']:>8.1f}"
        )
    print("─" * len(header))

    def stats(trades, name):
        if not trades:
            print(f"  {name}: no trades")
            return
        nets = [t["pnl_net"] for t in trades]
        total = sum(nets)
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x < 0]
        pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
        wr = len(wins) / len(trades) * 100
        avg = total / len(trades)
        print(f"  {name}: n={len(trades)}  net={total:+.0f} pips  avg={avg:+.2f}  WR={wr:.1f}%  PF={pf:.2f}")

    print()
    stats(all_is, "ALL IS")
    stats(all_oos, "ALL OOS")

    # Permutation test OOS
    if all_oos:
        rng = np.random.default_rng(42)
        gross = np.asarray([t["pnl_gross"] for t in all_oos])
        costs = np.asarray([t["cost"] for t in all_oos])
        obs = float(sum(t["pnl_net"] for t in all_oos))
        ge = sum(1 for _ in range(1000) if (gross * rng.choice([-1, 1], size=len(all_oos)) - costs).sum() >= obs)
        p = (ge + 1) / 1001
        print(f"  Permutation OOS p-value: {p:.4f}")


if __name__ == "__main__":
    main()
