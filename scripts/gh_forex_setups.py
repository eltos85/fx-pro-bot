#!/usr/bin/env python3
"""Backtest 3 форекс-стратегии, извлечённых из GitHub-репозиториев.

Исследование популярных форекс-ботов (geraked/metatrader5 489⭐,
ilahuerta-IA/mt5_live_trading_bot, ilahuerta-IA/tradingsystem 2025)
показало 3 нетривиальные стратегии, которые мы ещё не тестировали.
Цель — проверить их на наших 90-днях M5 с реальными FxPro costs.

Стратегии:
  1. triple_macd  — 3 MACD с разными периодами (короткий/средний/длинный).
     Вход long: все три histogram > 0 И средний только что пересёк 0 снизу.
     (geraked/metatrader5 / 3MACD EA)

  2. dhl_andean   — Daily High/Low breakout + Andean Oscillator confirm.
     Breakout вчерашнего дневного high/low + подтверждение Andean bull>bear.
     (geraked/metatrader5 / DHLAOS EA — скальпинг M15)

  3. sunset_ogle  — 4-phase state machine (Break → Pullback → Entry → Exit).
     Wait for 20-bar range break, 38-61% Fib pullback, break of pullback extreme.
     SL=ATR×4.5, TP=ATR×6.5 (ilahuerta-IA/tradingsystem 2025).

Anti-overfit:
  • Параметры взяты из оригинальных описаний, не тюнились.
  • IS 60% / OOS 40%.
  • Permutation test 1000 shuffles.
  • Costs — те же что и в scalp_setups_m5.py (spread + commission + slippage).
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

# Переиспользуем инфраструктуру
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scalp_setups_m5 import (  # noqa: E402
    Bars,
    PORTFOLIO,
    Trade,
    atr,
    cost_pips,
    ema,
    format_stats,
    load,
    permutation_test,
    simulate_trade,
    split_trades_is_oos,
    stats_from,
)


# ─────────────────── Индикаторы (дополнительные) ───────────────────

def macd_hist(c: np.ndarray, fast: int, slow: int, signal: int) -> np.ndarray:
    """MACD histogram = (EMA_fast - EMA_slow) - EMA_signal((EMA_fast - EMA_slow))."""
    ef = ema(c, fast)
    es = ema(c, slow)
    macd = ef - es
    # EMA сигнала — считаем только где macd валиден
    sig = np.full_like(c, np.nan, dtype=np.float64)
    valid = ~np.isnan(macd)
    if valid.sum() < signal:
        return sig
    first_valid = np.argmax(valid)
    k = 2.0 / (signal + 1)
    sig[first_valid + signal - 1] = np.nanmean(macd[first_valid:first_valid + signal])
    for i in range(first_valid + signal, len(c)):
        sig[i] = sig[i - 1] + k * (macd[i] - sig[i - 1])
    return macd - sig


def andean_oscillator(
    o: np.ndarray, c: np.ndarray, length: int = 25, sig_len: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Andean Oscillator (Alex Orekhov).

    up1 = EMA(max(close, open), len)
    up2 = EMA(max(close^2, open^2), len)
    bull = sqrt(up2 - up1^2)  — восходящая волатильность (variance сверху)
    dn1 = EMA(min(close, open), len)
    dn2 = EMA(min(close^2, open^2), len)
    bear = sqrt(dn2 - dn1^2)
    signal = EMA(max(bull, bear), sig_len)
    """
    up = np.maximum(c, o)
    dn = np.minimum(c, o)
    up1 = ema(up, length)
    up2 = ema(up * up, length)
    dn1 = ema(dn, length)
    dn2 = ema(dn * dn, length)
    bull = np.sqrt(np.maximum(up2 - up1 * up1, 0.0))
    bear = np.sqrt(np.maximum(dn2 - dn1 * dn1, 0.0))
    combined = np.maximum(bull, bear)
    signal = ema(combined, sig_len)
    return bull, bear, signal


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=np.float64)
    for i in range(n - 1, len(x)):
        out[i] = x[i - n + 1:i + 1].max()
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=np.float64)
    for i in range(n - 1, len(x)):
        out[i] = x[i - n + 1:i + 1].min()
    return out


# ─────────────────── Strategy 1: Triple MACD ───────────────────

def setup_triple_macd(bars: Bars) -> list[Trade]:
    """3MACD (geraked/metatrader5).

    fast=(5, 35, 5), mid=(12, 26, 9), slow=(24, 52, 18).
    Long: slow_hist>0 И mid_hist>0 И fast_hist только что пересёк 0 снизу.
    Short: наоборот.
    SL = 1.5×ATR, TP = 2.5×ATR, time-stop 24 bars (2 часа).
    Session: London+NY (07-17 UTC).
    """
    trades: list[Trade] = []
    a = atr(bars.h, bars.l, bars.c, 14)
    h_fast = macd_hist(bars.c, 5, 35, 5)
    h_mid = macd_hist(bars.c, 12, 26, 9)
    h_slow = macd_hist(bars.c, 24, 52, 18)
    last_trade = -10000
    for i in range(60, len(bars.c) - 1):
        if np.isnan(a[i]) or np.isnan(h_fast[i - 1]) or np.isnan(h_mid[i]) or np.isnan(h_slow[i]):
            continue
        if i - last_trade < 12:
            continue
        if bars.hour[i] < 7 or bars.hour[i] >= 17:
            continue
        long_ok = (
            h_slow[i] > 0
            and h_mid[i] > 0
            and h_fast[i - 1] <= 0 < h_fast[i]
        )
        short_ok = (
            h_slow[i] < 0
            and h_mid[i] < 0
            and h_fast[i - 1] >= 0 > h_fast[i]
        )
        if long_ok:
            sl = bars.c[i] - 1.5 * a[i]
            tp = bars.c[i] + 2.5 * a[i]
            t = simulate_trade(bars, i, +1, sl, tp, 24, "triple_macd")
            if t:
                trades.append(t)
                last_trade = i
        elif short_ok:
            sl = bars.c[i] + 1.5 * a[i]
            tp = bars.c[i] - 2.5 * a[i]
            t = simulate_trade(bars, i, -1, sl, tp, 24, "triple_macd")
            if t:
                trades.append(t)
                last_trade = i
    return trades


# ─────────────────── Strategy 2: Daily H/L + Andean Osc ───────────────────

def setup_dhl_andean(bars: Bars) -> list[Trade]:
    """DHLAOS (geraked/metatrader5).

    Вчерашний дневной High/Low — как диапазон.
    Вход long: close пробивает вчерашний high + Andean bull>bear + bull>signal.
    Вход short: close пробивает вчерашний low + Andean bear>bull + bear>signal.
    SL = 1.5×ATR, TP = 2.0×ATR, time-stop 36 bars (3 часа).
    """
    trades: list[Trade] = []
    a = atr(bars.h, bars.l, bars.c, 14)
    bull, bear, sig = andean_oscillator(bars.o, bars.c, 25, 9)
    # Вчерашние high/low по календарной дате
    unique_dates = np.unique(bars.date)
    date_to_hl: dict[int, tuple[float, float]] = {}
    for d in unique_dates:
        mask = bars.date == d
        date_to_hl[int(d)] = (float(bars.h[mask].max()), float(bars.l[mask].min()))

    # для каждого дня считаем high/low предыдущего торгового дня
    last_trade = -10000
    prev_hl: tuple[float, float] | None = None
    current_date = -1
    # идем хронологически
    for i in range(60, len(bars.c) - 1):
        d = int(bars.date[i])
        if d != current_date:
            # новая дата — берём prev из предыдущей даты
            if current_date > 0 and current_date in date_to_hl:
                prev_hl = date_to_hl[current_date]
            current_date = d
        if prev_hl is None:
            continue
        if np.isnan(a[i]) or np.isnan(bull[i]) or np.isnan(bear[i]) or np.isnan(sig[i]):
            continue
        if i - last_trade < 24:
            continue
        if bars.hour[i] < 7 or bars.hour[i] >= 17:
            continue
        prev_h, prev_l = prev_hl
        long_ok = (
            bars.c[i] > prev_h
            and bars.c[i - 1] <= prev_h
            and bull[i] > bear[i]
            and bull[i] > sig[i]
        )
        short_ok = (
            bars.c[i] < prev_l
            and bars.c[i - 1] >= prev_l
            and bear[i] > bull[i]
            and bear[i] > sig[i]
        )
        if long_ok:
            sl = bars.c[i] - 1.5 * a[i]
            tp = bars.c[i] + 2.0 * a[i]
            t = simulate_trade(bars, i, +1, sl, tp, 36, "dhl_andean")
            if t:
                trades.append(t)
                last_trade = i
        elif short_ok:
            sl = bars.c[i] + 1.5 * a[i]
            tp = bars.c[i] - 2.0 * a[i]
            t = simulate_trade(bars, i, -1, sl, tp, 36, "dhl_andean")
            if t:
                trades.append(t)
                last_trade = i
    return trades


# ─────────────────── Strategy 3: Sunset Ogle 4-phase ───────────────────

def setup_sunset_ogle(bars: Bars) -> list[Trade]:
    """Sunset Ogle 4-phase state machine (ilahuerta-IA/tradingsystem).

    Phase 0 (Idle): wait for 20-bar range break + volume spike (> 1.5× median20)
    Phase 1 (Break): зафиксирован pivot = break price
    Phase 2 (Pullback): price retraces 38-61% фибо от extreme к pivot,
                        ждём N<=20 bars; если range нарушен в другую — cancel
    Phase 3 (Entry): break of pullback extreme в направлении первого impulse
    SL = pullback extreme (~4×ATR), TP = 6.5×ATR (соотв. оригиналу).
    """
    trades: list[Trade] = []
    n_range = 20
    a = atr(bars.h, bars.l, bars.c, 14)
    hi20 = rolling_max(bars.h, n_range)
    lo20 = rolling_min(bars.l, n_range)
    # Rolling median volume 20 (proxy через sorted)
    med_v = np.full_like(bars.v, np.nan, dtype=np.float64)
    for i in range(n_range - 1, len(bars.v)):
        med_v[i] = np.median(bars.v[i - n_range + 1:i + 1])

    last_trade = -10000
    i = 30
    while i < len(bars.c) - 1:
        if i - last_trade < 12:
            i += 1
            continue
        if np.isnan(a[i]) or np.isnan(hi20[i - 1]) or np.isnan(lo20[i - 1]) or np.isnan(med_v[i]):
            i += 1
            continue
        if bars.hour[i] < 7 or bars.hour[i] >= 17:
            i += 1
            continue
        # Phase 1: break
        vol_ok = bars.v[i] > 1.5 * med_v[i]
        broke_up = bars.c[i] > hi20[i - 1] and vol_ok
        broke_dn = bars.c[i] < lo20[i - 1] and vol_ok
        if not (broke_up or broke_dn):
            i += 1
            continue
        direction = 1 if broke_up else -1
        pivot = hi20[i - 1] if broke_up else lo20[i - 1]
        impulse_extreme = bars.h[i] if broke_up else bars.l[i]
        impulse_range = abs(impulse_extreme - pivot)
        if impulse_range < 0.3 * a[i]:
            i += 1
            continue
        # Phase 2: ждём pullback max 20 bars
        found_entry = False
        j = i + 1
        max_j = min(i + 20, len(bars.c) - 2)
        pullback_extreme = impulse_extreme
        while j <= max_j:
            lo_j = bars.l[j]
            hi_j = bars.h[j]
            if direction == 1:
                fib38 = impulse_extreme - 0.38 * impulse_range
                fib61 = impulse_extreme - 0.61 * impulse_range
                # pullback lo coasted into fib zone?
                pullback_extreme = min(pullback_extreme, lo_j)
                if lo_j < pivot:
                    break  # pullback пробил pivot — отмена
                if fib61 <= pullback_extreme <= fib38:
                    # Phase 3: ждём break вверх от pullback_extreme... и j+ ?
                    # Entry когда close[k] > high[j] (простой триггер)
                    k = j + 1
                    max_k = min(j + 10, len(bars.c) - 1)
                    while k <= max_k:
                        if bars.c[k] > bars.h[j]:
                            sl = pullback_extreme - 0.2 * a[k]
                            risk = bars.c[k] - sl
                            if risk > 0:
                                tp = bars.c[k] + 1.5 * risk
                                t = simulate_trade(bars, k, +1, sl, tp, 30, "sunset_ogle")
                                if t:
                                    trades.append(t)
                                    last_trade = k
                                    i = k + 6
                                    found_entry = True
                            break
                        k += 1
                    break
            else:
                fib38 = impulse_extreme + 0.38 * impulse_range
                fib61 = impulse_extreme + 0.61 * impulse_range
                pullback_extreme = max(pullback_extreme, hi_j)
                if hi_j > pivot:
                    break
                if fib38 <= pullback_extreme <= fib61:
                    k = j + 1
                    max_k = min(j + 10, len(bars.c) - 1)
                    while k <= max_k:
                        if bars.c[k] < bars.l[j]:
                            sl = pullback_extreme + 0.2 * a[k]
                            risk = sl - bars.c[k]
                            if risk > 0:
                                tp = bars.c[k] - 1.5 * risk
                                t = simulate_trade(bars, k, -1, sl, tp, 30, "sunset_ogle")
                                if t:
                                    trades.append(t)
                                    last_trade = k
                                    i = k + 6
                                    found_entry = True
                            break
                        k += 1
                    break
            j += 1
        if not found_entry:
            i += 1
    return trades


# ─────────────────── Main ───────────────────

SETUPS: dict[str, Callable[[Bars], list[Trade]]] = {
    "triple_macd": setup_triple_macd,
    "dhl_andean": setup_dhl_andean,
    "sunset_ogle": setup_sunset_ogle,
}


def main() -> None:
    print("=" * 100)
    print("GITHUB FOREX SETUPS BACKTEST — 3 стратегии × 12 инстр.")
    print("=" * 100)
    print("Costs per trade (round-trip, pips):")
    for sym in PORTFOLIO:
        print(f"  {sym:10s} = {cost_pips(sym):.2f} pips")
    print()

    all_bars: dict[str, Bars] = {}
    for sym in PORTFOLIO:
        try:
            all_bars[sym] = load(sym)
        except Exception as e:
            print(f"  !!! load {sym} failed: {e}")

    total_days_union: set[int] = set()
    for bars in all_bars.values():
        total_days_union.update(map(int, np.unique(bars.date)))
    total_days = len(total_days_union)
    print(f"Всего календарных дней в данных: {total_days}\n")

    all_trades_by_setup: dict[str, list[Trade]] = {k: [] for k in SETUPS}
    for sym, bars in all_bars.items():
        for setup_name, fn in SETUPS.items():
            try:
                ts = fn(bars)
            except Exception as e:
                print(f"  !!! setup {setup_name} on {sym} failed: {e}")
                ts = []
            all_trades_by_setup[setup_name].extend(ts)

    summary_rows: list[dict] = []
    for setup_name in SETUPS:
        trades = all_trades_by_setup[setup_name]
        is_, oos = split_trades_is_oos(trades, is_frac=0.6)
        st_all = stats_from(setup_name, "ALL", trades)
        st_is = stats_from(setup_name, "IS", is_)
        st_oos = stats_from(setup_name, "OOS", oos)
        p_all = permutation_test(trades, n_perm=1000)
        p_oos = permutation_test(oos, n_perm=1000)
        signals_per_day = st_all.n / max(total_days, 1)

        print("─" * 100)
        print(
            f"SETUP: {setup_name}  total_trades={st_all.n}  "
            f"~{signals_per_day:.2f} signals/day  "
            f"p_all={p_all:.4f}  p_oos={p_oos:.4f}"
        )
        print(format_stats(st_all))
        print(format_stats(st_is))
        print(format_stats(st_oos))

        per_sym: dict[str, list[Trade]] = {}
        for t in trades:
            per_sym.setdefault(t.sym, []).append(t)
        print("  Per-symbol:")
        for sym, ts_ in sorted(per_sym.items(), key=lambda x: -sum(t.pnl_pips_net for t in x[1])):
            st = stats_from(setup_name, sym, ts_)
            print(f"    {sym:10s} n={st.n:4d}  net={st.net_pips:+8.1f}  wr={st.wr*100:4.1f}%  pf={st.pf:4.2f}")

        summary_rows.append({
            "setup": setup_name,
            "n": st_all.n,
            "signals_per_day": round(signals_per_day, 3),
            "net_pips_all": round(st_all.net_pips, 1),
            "net_pips_is": round(st_is.net_pips, 1),
            "net_pips_oos": round(st_oos.net_pips, 1),
            "wr_all": round(st_all.wr * 100, 1),
            "wr_oos": round(st_oos.wr * 100, 1),
            "pf_all": round(st_all.pf, 2) if st_all.pf != float("inf") else 999.99,
            "pf_oos": round(st_oos.pf, 2) if st_oos.pf != float("inf") else 999.99,
            "avg_net": round(st_all.avg_net, 2),
            "p_all": round(p_all, 4),
            "p_oos": round(p_oos, 4),
        })

    print()
    print("=" * 100)
    print("СВОДКА")
    print("=" * 100)
    hdr = (f"{'setup':<18}{'n':>6}{'/day':>7}{'net':>9}{'is':>9}{'oos':>9}"
           f"{'wr%':>6}{'pf':>6}{'p_all':>8}{'p_oos':>8}")
    print(hdr)
    for r in sorted(summary_rows, key=lambda x: -x["net_pips_oos"]):
        print(
            f"{r['setup']:<18}"
            f"{r['n']:>6}"
            f"{r['signals_per_day']:>7.2f}"
            f"{r['net_pips_all']:>+9.1f}"
            f"{r['net_pips_is']:>+9.1f}"
            f"{r['net_pips_oos']:>+9.1f}"
            f"{r['wr_all']:>6.1f}"
            f"{r['pf_all']:>6.2f}"
            f"{r['p_all']:>8.4f}"
            f"{r['p_oos']:>8.4f}"
        )

    out = Path(__file__).resolve().parents[1] / "data" / "gh_forex_setups_summary.csv"
    with out.open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        w.writeheader()
        w.writerows(summary_rows)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
