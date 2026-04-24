#!/usr/bin/env python3
"""Backtest Hypothesis A: GBPUSD extreme-move → JPY pairs fade (24h horizon).

Из EDA bottom_up_analysis на 2 годах M5/H1:
  GBPUSD ≥2σ (4h) → GBPJPY через 24h: mean=-12.5 bps, t=-4.59, p=5.3e-06, n=654
  GBPUSD ≥2σ (4h) → EURJPY через 24h: mean=-9.6  bps, t=-4.07, p=5.4e-05, n=654
  GBPUSD ≥2σ (4h) → USDJPY через 24h: mean=-11.3 bps, t=-3.98, p=7.5e-05, n=654

Логика (signed response был отрицательным → B движется в ПРОТИВОПОЛОЖНУЮ сторону от A):
  • Trigger: |4h_return GBPUSD| > 2σ (rolling 30d std), на H1 закрытии.
  • Через 1 час вход в JPY crosses в НАПРАВЛЕНИИ ПРОТИВ GBPUSD.
  • Hold 24h (time-stop) либо ATR-based SL/TP.
  • Cool-off 4h между триггерами (не каскадом).

IS/OOS split 60/40, permutation test 1000 shuffles.
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path

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


# ──────────────────── Параметры (из EDA) ────────────────────
TRIGGER_LOOKBACK_H = 4           # часы — 4h return GBPUSD
STD_WINDOW_DAYS = 30             # 30-дневный rolling std для нормализации
TRIGGER_SIGMA = 2.0              # |z| > 2
ENTRY_DELAY_H = 1                # через 1 час после trigger
HOLD_H = 24                      # 24h time-stop
COOL_OFF_H = 4                   # не более 1 триггера за 4h
USE_ATR_STOPS = False            # True: SL=1.5×ATR TP=2.0×ATR; False: только time-stop

TRIGGER_SYMBOL = "GBPUSD=X"
RESPONSE_SYMBOLS = ["GBPJPY=X", "USDJPY=X", "EURJPY=X"]

BARS_PER_HOUR = 12               # M5


# ──────────────────── Core ────────────────────

def find_triggers(trigger_bars: Bars) -> list[tuple[int, int]]:
    """Returns list of (bar_idx, sign). Triggered when |4h return| > 2σ (rolling 30d std)."""
    n = len(trigger_bars.c)
    lookback = TRIGGER_LOOKBACK_H * BARS_PER_HOUR
    std_win = STD_WINDOW_DAYS * 24 * BARS_PER_HOUR
    cooloff = COOL_OFF_H * BARS_PER_HOUR

    log_c = np.log(trigger_bars.c)
    # 4h log-return
    r = np.full(n, np.nan)
    r[lookback:] = log_c[lookback:] - log_c[:-lookback]

    triggers: list[tuple[int, int]] = []
    last = -10**9
    # Rolling std — скользящее окно, обновляется пошагово (для ускорения)
    # но тут n ~148k, std_win=8640 — допустимо через numpy per-step
    for i in range(std_win, n):
        if i - last < cooloff:
            continue
        if np.isnan(r[i]):
            continue
        w = r[i - std_win:i]
        w = w[~np.isnan(w)]
        if len(w) < 100:
            continue
        s = float(np.std(w))
        if s <= 0:
            continue
        z = r[i] / s
        if abs(z) > TRIGGER_SIGMA:
            triggers.append((i, int(np.sign(r[i]))))
            last = i
    return triggers


def nearest_idx(ts_arr: np.ndarray, target_ts: int) -> int:
    """Индекс с ts ≤ target_ts (nearest previous). Returns -1 if out of range."""
    idx = int(np.searchsorted(ts_arr, target_ts, side="right") - 1)
    if idx < 0 or idx >= len(ts_arr):
        return -1
    return idx


def backtest() -> None:
    print("=" * 100)
    print("Hypothesis A: GBPUSD extreme → JPY pairs fade (24h horizon)")
    print("=" * 100)
    print(f"  Trigger: |4h return {TRIGGER_SYMBOL}| > {TRIGGER_SIGMA}σ (rolling {STD_WINDOW_DAYS}d)")
    print(f"  Entry: +{ENTRY_DELAY_H}h, Hold: {HOLD_H}h, Cool-off: {COOL_OFF_H}h")
    print(f"  Response symbols: {RESPONSE_SYMBOLS}")
    print(f"  Stops: {'ATR 1.5×/2.0×' if USE_ATR_STOPS else 'TIME ONLY'}")
    print()

    trigger_bars = load(TRIGGER_SYMBOL)
    print(f"  Loaded {TRIGGER_SYMBOL}: {len(trigger_bars.c)} bars")

    triggers = find_triggers(trigger_bars)
    print(f"  Found {len(triggers)} triggers "
          f"(~{len(triggers) / (len(trigger_bars.c) / BARS_PER_HOUR / 24):.2f}/day)")

    # Для каждого response symbol — симулируем торги
    all_trades: list[Trade] = []
    per_sym_trades: dict[str, list[Trade]] = {}
    for resp_sym in RESPONSE_SYMBOLS:
        resp_bars = load(resp_sym)
        resp_ts = resp_bars.ts
        trades_sym: list[Trade] = []
        for trig_idx, sign_a in triggers:
            trig_ts = int(trigger_bars.ts[trig_idx])
            # вход через +ENTRY_DELAY_H часов
            entry_ts_target = trig_ts + ENTRY_DELAY_H * 3600 * 1000
            entry_i = nearest_idx(resp_ts, entry_ts_target)
            if entry_i < 0 or entry_i >= len(resp_bars.c) - 1:
                continue
            # direction: OPPOSITE to GBPUSD move
            direction = -sign_a
            entry_price = resp_bars.o[entry_i + 1] if entry_i + 1 < len(resp_bars.c) else resp_bars.c[entry_i]
            if USE_ATR_STOPS:
                a = atr(resp_bars.h, resp_bars.l, resp_bars.c, 14)
                if np.isnan(a[entry_i]):
                    continue
                if direction == 1:
                    sl = entry_price - 1.5 * a[entry_i]
                    tp = entry_price + 2.0 * a[entry_i]
                else:
                    sl = entry_price + 1.5 * a[entry_i]
                    tp = entry_price - 2.0 * a[entry_i]
            else:
                # time-only: ставим SL/TP далеко, чтобы не срабатывали
                if direction == 1:
                    sl = entry_price * 0.9
                    tp = entry_price * 1.1
                else:
                    sl = entry_price * 1.1
                    tp = entry_price * 0.9
            max_bars = HOLD_H * BARS_PER_HOUR
            t = simulate_trade(resp_bars, entry_i, direction, sl, tp, max_bars, f"gbpusd_jpy_fade_{resp_sym}")
            if t is not None:
                trades_sym.append(t)
        per_sym_trades[resp_sym] = trades_sym
        all_trades.extend(trades_sym)
        print(f"  {resp_sym}: {len(trades_sym)} trades")

    print()
    # Статистика
    print("=" * 100)
    print("PER-SYMBOL RESULTS")
    print("=" * 100)
    rows_out: list[dict] = []
    for sym, ts_ in per_sym_trades.items():
        is_, oos = split_trades_is_oos(ts_, is_frac=0.6)
        st_all = stats_from("gbpusd_jpy_fade", f"ALL/{sym}", ts_)
        st_is = stats_from("gbpusd_jpy_fade", "IS", is_)
        st_oos = stats_from("gbpusd_jpy_fade", "OOS", oos)
        p_all = permutation_test(ts_, n_perm=1000)
        p_oos = permutation_test(oos, n_perm=1000)
        print(
            f"  {sym}: n={st_all.n:3d}  net={st_all.net_pips:+8.1f}  "
            f"wr={st_all.wr*100:4.1f}%  pf={st_all.pf:4.2f}  "
            f"avg={st_all.avg_net:+5.2f}  "
            f"IS={st_is.net_pips:+7.1f}  OOS={st_oos.net_pips:+7.1f}  "
            f"p_all={p_all:.4f}  p_oos={p_oos:.4f}"
        )
        rows_out.append({
            "sym": sym,
            "n": st_all.n,
            "net_all": round(st_all.net_pips, 1),
            "net_is": round(st_is.net_pips, 1),
            "net_oos": round(st_oos.net_pips, 1),
            "wr": round(st_all.wr * 100, 1),
            "pf": round(st_all.pf, 2) if st_all.pf != float("inf") else 999.99,
            "avg": round(st_all.avg_net, 2),
            "p_all": round(p_all, 4),
            "p_oos": round(p_oos, 4),
        })

    # Портфель
    print()
    print("=" * 100)
    print("PORTFOLIO (все 3 инструмента одновременно)")
    print("=" * 100)
    is_, oos = split_trades_is_oos(all_trades, is_frac=0.6)
    st_all = stats_from("gbpusd_jpy_fade", "ALL/PORTFOLIO", all_trades)
    st_is = stats_from("gbpusd_jpy_fade", "IS/PORTFOLIO", is_)
    st_oos = stats_from("gbpusd_jpy_fade", "OOS/PORTFOLIO", oos)
    p_all = permutation_test(all_trades, n_perm=1000)
    p_oos = permutation_test(oos, n_perm=1000)
    signals_per_day = st_all.n / (len(trigger_bars.c) / BARS_PER_HOUR / 24)
    print(
        f"  n={st_all.n}  (~{signals_per_day:.2f}/day)  net={st_all.net_pips:+.1f} pips  "
        f"wr={st_all.wr*100:.1f}%  pf={st_all.pf:.2f}  avg={st_all.avg_net:+.2f}"
    )
    print(f"  IS = {st_is.net_pips:+.1f} pips (n={st_is.n})")
    print(f"  OOS = {st_oos.net_pips:+.1f} pips (n={st_oos.n})")
    print(f"  p_all = {p_all:.4f}  p_oos = {p_oos:.4f}")
    print(f"  exits: {st_all.exits}")

    out = Path(__file__).resolve().parents[1] / "data" / "backtest_gbpusd_jpy_fade.csv"
    with out.open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)
    print(f"\n  Сохранено: {out}")


if __name__ == "__main__":
    backtest()
