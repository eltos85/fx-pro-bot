#!/usr/bin/env python3
"""Walk-forward optimization для GBPJPY fade (anti-overfit).

Из гипотезы A получили IS +4953 pips / OOS +499 на GBPJPY.
Простое параметр-тюнинг на OOS = overfit. Используем walk-forward:

Разбиваем 2 года на 4 окна ~180 дней:
  W1: 2024-04-24 → 2024-10-22
  W2: 2024-10-22 → 2025-04-21
  W3: 2025-04-21 → 2025-10-20
  W4: 2025-10-20 → 2026-04-24

Протокол (expanding-window WFO):
  • W1: fit — пропускаем (warmup)
  • W2: fit params на W1, test на W2
  • W3: fit params на W1+W2, test на W3
  • W4: fit params на W1+W2+W3, test на W4

Варьируем:
  TRIGGER_SIGMA: {1.5, 2.0, 2.5, 3.0}
  ENTRY_DELAY_H: {1, 2, 4}
  HOLD_H: {12, 24, 36}
  VOL_FILTER: none / ATR_H1_high / ATR_D1_high
Selection criterion:  IS net_pips

Суммарный OOS = W2_test + W3_test + W4_test
Permutation test: OOS vs random signs.
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
    simulate_trade,
    Trade,
)


# ──────────────────── Конфиг ────────────────────

TRIGGER_SYMBOL = "GBPUSD=X"
RESPONSE_SYMBOL = "GBPJPY=X"

BARS_PER_HOUR = 12

# Пространство параметров
GRID_TRIGGER_SIGMA = [1.5, 2.0, 2.5, 3.0]
GRID_ENTRY_DELAY_H = [1, 2, 4]
GRID_HOLD_H = [12, 24, 36]
GRID_VOL_FILTER = ["none", "atr_high"]   # atr_high: ATR(H1, 14) > 50th percentile (rolling 30d)

STD_WINDOW_DAYS = 30
TRIGGER_LOOKBACK_H = 4
COOL_OFF_H = 4


# ──────────────────── Triggers & trade sim ────────────────────

def find_triggers(
    bars,
    trigger_sigma: float,
) -> list[tuple[int, int]]:
    n = len(bars.c)
    lookback = TRIGGER_LOOKBACK_H * BARS_PER_HOUR
    std_win = STD_WINDOW_DAYS * 24 * BARS_PER_HOUR
    cooloff = COOL_OFF_H * BARS_PER_HOUR

    log_c = np.log(bars.c)
    r = np.full(n, np.nan)
    r[lookback:] = log_c[lookback:] - log_c[:-lookback]

    triggers: list[tuple[int, int]] = []
    last = -10**9
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
        if abs(z) > trigger_sigma:
            triggers.append((i, int(np.sign(r[i]))))
            last = i
    return triggers


def nearest_idx(ts_arr: np.ndarray, target_ts: int) -> int:
    idx = int(np.searchsorted(ts_arr, target_ts, side="right") - 1)
    if idx < 0 or idx >= len(ts_arr):
        return -1
    return idx


def simulate_for_params(
    trig_bars,
    resp_bars,
    triggers: list[tuple[int, int]],
    entry_delay_h: int,
    hold_h: int,
    vol_filter: str,
    ts_start_ms: int,
    ts_end_ms: int,
) -> list[Trade]:
    """Симулирует trade для даних params и фильтра volatility на response-симбл."""
    trades: list[Trade] = []
    resp_ts = resp_bars.ts
    a_resp = atr(resp_bars.h, resp_bars.l, resp_bars.c, 14)

    # Rolling 30d percentile 50 ATR (per-H1 → per-M5 approximation)
    # Упрощение: используем percentile за N=30d = 8640 M5 bars; rolling
    med_atr = None
    if vol_filter == "atr_high":
        lookback_m5 = 30 * 24 * BARS_PER_HOUR
        med_atr = np.full(len(resp_bars.c), np.nan)
        # forward-pass rolling median (оптимизация: обновляется каждый 12-й бар, чтоб не был слишком медленно)
        for i in range(lookback_m5, len(resp_bars.c), 12):
            med_atr[i:i + 12] = np.nanmedian(a_resp[i - lookback_m5:i])

    for trig_idx, sign_a in triggers:
        trig_ts = int(trig_bars.ts[trig_idx])
        # Фильтр по времени: только триггеры в текущем окне
        if trig_ts < ts_start_ms or trig_ts >= ts_end_ms:
            continue
        entry_ts_target = trig_ts + entry_delay_h * 3600 * 1000
        entry_i = nearest_idx(resp_ts, entry_ts_target)
        if entry_i < 0 or entry_i >= len(resp_bars.c) - 1:
            continue
        if np.isnan(a_resp[entry_i]) or a_resp[entry_i] <= 0:
            continue
        # Vol filter
        if vol_filter == "atr_high":
            if med_atr is None or np.isnan(med_atr[entry_i]) or a_resp[entry_i] < med_atr[entry_i]:
                continue
        direction = -sign_a
        entry_price = resp_bars.o[entry_i + 1] if entry_i + 1 < len(resp_bars.c) else resp_bars.c[entry_i]
        # Time-only (без ATR stops)
        if direction == 1:
            sl = entry_price * 0.9
            tp = entry_price * 1.1
        else:
            sl = entry_price * 1.1
            tp = entry_price * 0.9
        max_bars = hold_h * BARS_PER_HOUR
        t = simulate_trade(resp_bars, entry_i, direction, sl, tp, max_bars, "wfo_gbpjpy")
        if t is not None:
            trades.append(t)
    return trades


def net_of(trades: list[Trade]) -> float:
    return sum(t.pnl_pips_net for t in trades)


# ──────────────────── Walk-forward ────────────────────

def main() -> None:
    print("=" * 100)
    print("Walk-Forward Optimization: GBPJPY fade (anti-overfit)")
    print("=" * 100)

    trig_bars = load(TRIGGER_SYMBOL)
    resp_bars = load(RESPONSE_SYMBOL)
    n_trig = len(trig_bars.c)
    t_start_ms = int(trig_bars.ts[0])
    t_end_ms = int(trig_bars.ts[-1])
    total_days = (t_end_ms - t_start_ms) / (86400 * 1000)
    print(f"  Data range: {datetime.fromtimestamp(t_start_ms/1000, tz=UTC).date()} → "
          f"{datetime.fromtimestamp(t_end_ms/1000, tz=UTC).date()} ({total_days:.0f} days)")

    # 4 окна
    win_duration_ms = int((t_end_ms - t_start_ms) / 4)
    windows = []
    for k in range(4):
        ws = t_start_ms + k * win_duration_ms
        we = ws + win_duration_ms if k < 3 else t_end_ms
        windows.append((ws, we))
        print(f"  W{k+1}: {datetime.fromtimestamp(ws/1000, tz=UTC).date()} → "
              f"{datetime.fromtimestamp(we/1000, tz=UTC).date()}")

    # Сетка параметров
    grid = []
    for sigma in GRID_TRIGGER_SIGMA:
        for delay in GRID_ENTRY_DELAY_H:
            for hold in GRID_HOLD_H:
                for vf in GRID_VOL_FILTER:
                    grid.append({"sigma": sigma, "delay": delay, "hold": hold, "vf": vf})
    print(f"\n  Grid size: {len(grid)} param combos\n")

    # Кэшируем triggers для каждой σ
    triggers_by_sigma: dict[float, list[tuple[int, int]]] = {}
    for sigma in GRID_TRIGGER_SIGMA:
        triggers_by_sigma[sigma] = find_triggers(trig_bars, sigma)
        print(f"  triggers σ={sigma}: {len(triggers_by_sigma[sigma])}")
    print()

    # WFO: W1 warmup, W2..W4 — fit→test
    best_per_walk = []
    all_oos_trades: list[Trade] = []
    print("─" * 100)
    print(f"  {'Walk':<6}{'Train':<24}{'Test':<24}{'BestParams':<32}{'ISnet':>10}{'OOSnet':>10}{'n':>5}")
    print("─" * 100)
    for k in range(1, 4):  # W2, W3, W4
        fit_start = t_start_ms
        fit_end = windows[k][0]
        test_start = windows[k][0]
        test_end = windows[k][1]

        # Grid search on fit window
        best_net = -float("inf")
        best_params = None
        best_is_trades = []
        for params in grid:
            trigs = triggers_by_sigma[params["sigma"]]
            fit_trades = simulate_for_params(
                trig_bars, resp_bars, trigs,
                params["delay"], params["hold"], params["vf"],
                fit_start, fit_end,
            )
            if len(fit_trades) < 30:
                continue
            net = net_of(fit_trades)
            if net > best_net:
                best_net = net
                best_params = params
                best_is_trades = fit_trades

        if best_params is None:
            print(f"  W{k+1}: NO params produced enough trades")
            continue

        # OOS test
        trigs = triggers_by_sigma[best_params["sigma"]]
        oos_trades = simulate_for_params(
            trig_bars, resp_bars, trigs,
            best_params["delay"], best_params["hold"], best_params["vf"],
            test_start, test_end,
        )
        oos_net = net_of(oos_trades)
        best_per_walk.append({
            "walk": k + 1,
            "train_days": (fit_end - fit_start) / (86400 * 1000),
            "test_days": (test_end - test_start) / (86400 * 1000),
            "best_params": best_params,
            "is_net": best_net,
            "is_n": len(best_is_trades),
            "oos_net": oos_net,
            "oos_n": len(oos_trades),
        })
        all_oos_trades.extend(oos_trades)
        fit_end_d = datetime.fromtimestamp(fit_end / 1000, tz=UTC).date()
        test_end_d = datetime.fromtimestamp(test_end / 1000, tz=UTC).date()
        pstr = f"σ{best_params['sigma']}_d{best_params['delay']}_h{best_params['hold']}_{best_params['vf']}"
        print(
            f"  W{k+1:<5}{'→'+str(fit_end_d):<24}{'→'+str(test_end_d):<24}{pstr:<32}"
            f"{best_net:>+10.1f}{oos_net:>+10.1f}{len(oos_trades):>5}"
        )

    print("─" * 100)
    # Суммарный OOS
    all_oos_net = net_of(all_oos_trades)
    if all_oos_trades:
        wins = [t.pnl_pips_net for t in all_oos_trades if t.pnl_pips_net > 0]
        losses = [t.pnl_pips_net for t in all_oos_trades if t.pnl_pips_net < 0]
        pf = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0)
        wr = len(wins) / len(all_oos_trades) * 100
        # Permutation test
        rng = np.random.default_rng(42)
        gross = np.asarray([t.pnl_pips_gross for t in all_oos_trades])
        costs = np.asarray([t.cost_pips for t in all_oos_trades])
        obs = float(all_oos_net)
        ge = 0
        for _ in range(1000):
            signs = rng.choice([-1, 1], size=len(all_oos_trades))
            if (gross * signs - costs).sum() >= obs:
                ge += 1
        p_oos = (ge + 1) / 1001
        print(f"\n  ИТОГ WFO (суммарный OOS по 3 walks):")
        print(f"    n = {len(all_oos_trades)} trades")
        print(f"    net = {all_oos_net:+.1f} pips")
        print(f"    avg = {all_oos_net/len(all_oos_trades):+.2f} pips/trade")
        print(f"    WR = {wr:.1f}%  PF = {pf:.2f}")
        print(f"    p_oos = {p_oos:.4f}")
    else:
        print("\n  No OOS trades")


if __name__ == "__main__":
    main()
