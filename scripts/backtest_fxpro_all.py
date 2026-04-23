#!/usr/bin/env python3
"""Backtest 3 скальпинг-стратегий FxPro на 90-дневной M5-истории.

Прогоняются:
- vwap_reversion (VWAP mean-reversion, SL 2.0×ATR, TP 1.5×ATR)
- session_orb    (London/NY ORB + News Fade, SL 1.5×ATR, TP 3.0×ATR)
- stat_arb       (pairs-trading, z-entry 2.5 / exit 0.5, SL 2.0×ATR)

Каждая стратегия:
1. Скользящим окном 1440 баров (как live) пересчитывает `scan()`
2. На сигнал — симулирует позицию bar-walker: SL/TP от ATR; закрытие по
   первому касанию или по `max_hold_bars` (time stop).
3. P&L = (close_price - entry_price) * direction - spread_cost.
   Комиссия FxPro Raw+: $3.50/lot per side × 2 = $7.0 round-trip на 1.0 lot.

Изоляция: модуль НЕ импортирует live-инфраструктуру (store, executor,
calendar) — только «чистые» scan-функции стратегий + bar-модель.

Использование:
    python3 -m scripts.backtest_fxpro_all
    python3 -m scripts.backtest_fxpro_all --strategies vwap,orb
    python3 -m scripts.backtest_fxpro_all --data-dir data/fxpro_klines
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Iterator

from fx_pro_bot.analysis.signals import TrendDirection, _atr, _ema, compute_adx, _rsi
from fx_pro_bot.config.settings import (
    DISPLAY_NAMES, SPREAD_PIPS, pip_size, spread_cost_pips,
)
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.strategies.scalping.indicators import (
    ema_slope, htf_ema_trend, ols_hedge_ratio, rolling_z_score, spread_series,
    vwap, adf_test_stationary,
)

log = logging.getLogger("backtest_fxpro")

LIVE_WINDOW_BARS = 1440
BAR_SEC = 5 * 60
MAX_HOLD_BARS = 12 * 6  # 6 часов на M5 = time stop
FXPRO_ROUNDTRIP_COMMISSION_PER_LOT = 7.0  # $3.5 × 2 per 1.0 lot

# Пары для stat_arb — только внутри нашей вселенной
STATARB_PAIRS: list[tuple[str, str]] = [
    ("EURUSD=X", "GBPUSD=X"),
    ("USDJPY=X", "USDCAD=X"),
    ("AUDUSD=X", "USDCAD=X"),
]

# vwap_reversion constants (зеркально из strategy.py)
VWAP_DEV = 2.0
VWAP_RSI_LOW = 30
VWAP_RSI_HIGH = 70
VWAP_SL_ATR = 2.0
VWAP_TP_ATR = 1.5
VWAP_ADX_MAX = 25.0

# session_orb constants
ORB_BARS = 3
ORB_SL_ATR = 1.5
ORB_TP_ATR = 3.0
ORB_ADX_MAX = 25.0
LONDON_OPEN = time(8, 0)
LONDON_ORB_END = time(8, 15)
LONDON_CLOSE = time(12, 0)
NY_OPEN = time(14, 30)
NY_ORB_END = time(14, 45)
NY_CLOSE = time(17, 0)

# stat_arb constants
STATARB_Z_ENTRY = 2.5
STATARB_Z_EXIT = 0.5
STATARB_LOOKBACK = 100
STATARB_ZWIN = 50
STATARB_SL_ATR = 2.0
STATARB_ADF_CRIT = -2.86


# ────────────────────── данные ──────────────────────

def _filename_for(yf_symbol: str) -> str:
    return yf_symbol.replace("=X", "").replace("=F", "_F").replace("-", "_") + "_M5.csv"


def load_bars(data_dir: Path, yf_symbol: str) -> list[Bar]:
    path = data_dir / _filename_for(yf_symbol)
    if not path.exists():
        return []
    instr = InstrumentId(symbol=yf_symbol)
    bars: list[Bar] = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_ms = int(row["timestamp"])
            bars.append(Bar(
                instrument=instr,
                ts=datetime.fromtimestamp(ts_ms/1000, UTC),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    return bars


# ────────────────────── симуляция позиции ──────────────────────

@dataclass
class Trade:
    strategy: str
    instrument: str
    direction: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    sl: float
    tp: float
    reason: str           # tp | sl | time | endofdata | pair_exit
    bars_held: int
    pnl_pips: float        # gross (без spread)
    net_pips: float        # с вычетом spread (round-trip)


def _hit_sl_tp(bar: Bar, is_long: bool, sl: float, tp: float) -> tuple[bool, bool]:
    """Вернуть (hit_sl, hit_tp) для бара.

    Pessimistic: если high и low оба пересекли уровни на одном баре —
    предполагаем что SL сработал раньше (worst case).
    """
    hit_sl = (bar.low <= sl) if is_long else (bar.high >= sl)
    hit_tp = (bar.high >= tp) if is_long else (bar.low <= tp)
    return hit_sl, hit_tp


def simulate_position(
    bars: list[Bar], entry_idx: int, direction: str,
    entry_price: float, sl: float, tp: float,
    spread_pips: float, pip_sz: float,
    max_hold: int = MAX_HOLD_BARS,
) -> Trade | None:
    """Пройти по барам от entry_idx+1 до закрытия."""
    is_long = direction == "long"
    for j in range(entry_idx + 1, min(entry_idx + 1 + max_hold, len(bars))):
        b = bars[j]
        hit_sl, hit_tp = _hit_sl_tp(b, is_long, sl, tp)
        if hit_sl and hit_tp:
            exit_price = sl
            reason = "sl"
        elif hit_sl:
            exit_price = sl
            reason = "sl"
        elif hit_tp:
            exit_price = tp
            reason = "tp"
        else:
            continue
        gross = (exit_price - entry_price) / pip_sz
        if not is_long:
            gross = -gross
        net = gross - spread_pips
        return Trade(
            strategy="", instrument="",
            direction=direction,
            entry_ts=bars[entry_idx].ts,
            entry_price=entry_price,
            exit_ts=b.ts, exit_price=exit_price,
            sl=sl, tp=tp, reason=reason,
            bars_held=j - entry_idx,
            pnl_pips=round(gross, 2),
            net_pips=round(net, 2),
        )

    end = min(entry_idx + max_hold, len(bars) - 1)
    b = bars[end]
    exit_price = b.close
    gross = (exit_price - entry_price) / pip_sz
    if not is_long:
        gross = -gross
    net = gross - spread_pips
    return Trade(
        strategy="", instrument="",
        direction=direction, entry_ts=bars[entry_idx].ts,
        entry_price=entry_price, exit_ts=b.ts, exit_price=exit_price,
        sl=sl, tp=tp, reason="time",
        bars_held=end - entry_idx,
        pnl_pips=round(gross, 2),
        net_pips=round(net, 2),
    )


# ────────────────────── стратегии: чистые scan-функции ──────────────────────

def scan_vwap(window: list[Bar], price: float) -> tuple[str, float] | None:
    """Возвращает (direction, atr) или None."""
    if len(window) < 51:
        return None
    atr_v = _atr(window)
    if atr_v <= 0:
        return None
    if compute_adx(window) > VWAP_ADX_MAX:
        return None
    vwap_val = vwap(window[-50:])
    deviation = (price - vwap_val) / atr_v
    closes = [b.close for b in window]
    rsi = _rsi(closes, 14)
    ema_vals = _ema(closes, 50)
    slope = ema_slope(ema_vals, 5)
    htf_slope = htf_ema_trend(window)

    if deviation < -VWAP_DEV and rsi < VWAP_RSI_LOW:
        if slope < 0:
            return None
        return ("long", atr_v)
    if deviation > VWAP_DEV and rsi > VWAP_RSI_HIGH:
        if slope > 0:
            return None
        return ("short", atr_v)
    return None


def _is_london_orb_window(ts: datetime) -> bool:
    t = ts.time()
    return LONDON_ORB_END <= t < LONDON_CLOSE


def _is_ny_orb_window(ts: datetime) -> bool:
    t = ts.time()
    return NY_ORB_END <= t < NY_CLOSE


def _session_box(window: list[Bar], ts: datetime) -> tuple[float, float] | None:
    """Построить коробку первых 3 баров после открытия сессии текущего дня."""
    t = ts.time()
    if LONDON_ORB_END <= t < LONDON_CLOSE:
        start_t = LONDON_OPEN
        end_t = LONDON_ORB_END
    elif NY_ORB_END <= t < NY_CLOSE:
        start_t = NY_OPEN
        end_t = NY_ORB_END
    else:
        return None

    today = ts.date()
    box: list[Bar] = []
    for b in reversed(window):
        bt = b.ts
        if bt.date() != today:
            break
        if start_t <= bt.time() < end_t:
            box.append(b)
    if len(box) < ORB_BARS:
        return None
    return (max(b.high for b in box), min(b.low for b in box))


def scan_orb(window: list[Bar], price: float) -> tuple[str, float] | None:
    """Simplified ORB: пробой коробки + EMA-slope подтверждение, без volume."""
    if len(window) < 51:
        return None
    atr_v = _atr(window)
    if atr_v <= 0:
        return None
    if compute_adx(window) > ORB_ADX_MAX:
        return None
    last = window[-1]
    box = _session_box(window, last.ts)
    if box is None:
        return None
    box_high, box_low = box
    closes = [b.close for b in window]
    ema_vals = _ema(closes, 50)
    slope = ema_slope(ema_vals, 5)

    if last.close > box_high and slope > 0:
        return ("long", atr_v)
    if last.close < box_low and slope < 0:
        return ("short", atr_v)
    return None


def scan_statarb(bars_a: list[Bar], bars_b: list[Bar]) -> tuple[str, str, float, float] | None:
    """Возвращает (dir_a, dir_b, atr_a, atr_b) или None."""
    n = min(len(bars_a), len(bars_b))
    if n < STATARB_LOOKBACK + STATARB_ZWIN:
        return None
    ca = [b.close for b in bars_a][-n:]
    cb = [b.close for b in bars_b][-n:]
    beta = ols_hedge_ratio(ca[-STATARB_LOOKBACK:], cb[-STATARB_LOOKBACK:])
    sprd = spread_series(ca, cb, beta)
    adf = adf_test_stationary(sprd)
    if adf > STATARB_ADF_CRIT:
        return None
    z = rolling_z_score(sprd, STATARB_ZWIN)
    if abs(z) < STATARB_Z_ENTRY:
        return None
    atr_a = _atr(bars_a)
    atr_b = _atr(bars_b)
    if z > 0:
        return ("short", "long", atr_a, atr_b)
    return ("long", "short", atr_a, atr_b)


# ────────────────────── backtester loops ──────────────────────

def backtest_vwap(all_bars: dict[str, list[Bar]]) -> list[Trade]:
    trades: list[Trade] = []
    for sym, bars in all_bars.items():
        ps = pip_size(sym)
        spread = spread_cost_pips(sym) * _SPREAD_BT_MULT["vwap_deviation"]
        open_until_bar = -1
        for i in range(LIVE_WINDOW_BARS, len(bars)):
            if i <= open_until_bar:
                continue
            window = bars[i - LIVE_WINDOW_BARS: i]
            price = bars[i].close
            sig = scan_vwap(window, price)
            if sig is None:
                continue
            direction, atr_v = sig
            if direction == "long":
                sl = price - VWAP_SL_ATR * atr_v
                tp = price + VWAP_TP_ATR * atr_v
            else:
                sl = price + VWAP_SL_ATR * atr_v
                tp = price - VWAP_TP_ATR * atr_v
            tr = simulate_position(
                bars, i, direction, price, sl, tp, spread, ps,
            )
            if tr is None:
                continue
            tr.strategy = "vwap_reversion"
            tr.instrument = sym
            trades.append(tr)
            open_until_bar = i + tr.bars_held
    return trades


def backtest_orb(all_bars: dict[str, list[Bar]]) -> list[Trade]:
    trades: list[Trade] = []
    for sym, bars in all_bars.items():
        ps = pip_size(sym)
        spread = spread_cost_pips(sym) * _SPREAD_BT_MULT["orb_breakout"]
        open_until_bar = -1
        for i in range(LIVE_WINDOW_BARS, len(bars)):
            if i <= open_until_bar:
                continue
            window = bars[i - LIVE_WINDOW_BARS: i]
            price = bars[i].close
            sig = scan_orb(window, price)
            if sig is None:
                continue
            direction, atr_v = sig
            if direction == "long":
                sl = price - ORB_SL_ATR * atr_v
                tp = price + ORB_TP_ATR * atr_v
            else:
                sl = price + ORB_SL_ATR * atr_v
                tp = price - ORB_TP_ATR * atr_v
            tr = simulate_position(
                bars, i, direction, price, sl, tp, spread, ps,
            )
            if tr is None:
                continue
            tr.strategy = "session_orb"
            tr.instrument = sym
            trades.append(tr)
            open_until_bar = i + tr.bars_held
    return trades


def _align_bars_by_ts(a: list[Bar], b: list[Bar]) -> tuple[list[Bar], list[Bar]]:
    """Оставляем только пересечение timestamp'ов."""
    ts_b = {bar.ts: bar for bar in b}
    aligned_a: list[Bar] = []
    aligned_b: list[Bar] = []
    for bar in a:
        m = ts_b.get(bar.ts)
        if m is not None:
            aligned_a.append(bar)
            aligned_b.append(m)
    return aligned_a, aligned_b


def backtest_statarb(all_bars: dict[str, list[Bar]]) -> list[Trade]:
    """Pair trading: открываем 2 ноги одновременно, закрываем обе по |z|<Z_EXIT или SL."""
    trades: list[Trade] = []
    for sym_a, sym_b in STATARB_PAIRS:
        if sym_a not in all_bars or sym_b not in all_bars:
            continue
        bars_a, bars_b = _align_bars_by_ts(all_bars[sym_a], all_bars[sym_b])
        if len(bars_a) < STATARB_LOOKBACK + STATARB_ZWIN + 100:
            continue
        ps_a = pip_size(sym_a)
        ps_b = pip_size(sym_b)
        sp_a = spread_cost_pips(sym_a) * _SPREAD_BT_MULT["stat_arb"]
        sp_b = spread_cost_pips(sym_b) * _SPREAD_BT_MULT["stat_arb"]

        i = LIVE_WINDOW_BARS
        n = len(bars_a)
        while i < n:
            wa = bars_a[i - LIVE_WINDOW_BARS: i]
            wb = bars_b[i - LIVE_WINDOW_BARS: i]
            sig = scan_statarb(wa, wb)
            if sig is None:
                i += 1
                continue
            dir_a, dir_b, atr_a, atr_b = sig
            entry_a = bars_a[i].close
            entry_b = bars_b[i].close
            sl_a = entry_a - STATARB_SL_ATR * atr_a if dir_a == "long" else entry_a + STATARB_SL_ATR * atr_a
            sl_b = entry_b - STATARB_SL_ATR * atr_b if dir_b == "long" else entry_b + STATARB_SL_ATR * atr_b
            # Выход: фиксируем beta/mu/sigma от момента входа и смотрим,
            # когда mid-spread вернулся в |z| < Z_EXIT. Это даёт O(k) вместо
            # O(k × lookback). Точность: ≈ 1% разница vs полный пересчёт
            # beta в inner loop (проверено на Bybit-backtest).
            ca_entry = [b.close for b in wa][-STATARB_LOOKBACK:]
            cb_entry = [b.close for b in wb][-STATARB_LOOKBACK:]
            beta_ent = ols_hedge_ratio(ca_entry, cb_entry)
            sp_entry = [a - beta_ent * b for a, b in zip(ca_entry, cb_entry)]
            mu = statistics.mean(sp_entry[-STATARB_ZWIN:])
            sd = statistics.pstdev(sp_entry[-STATARB_ZWIN:]) or 1e-9

            exit_j = None
            exit_reason = "time"
            is_long_a = dir_a == "long"
            is_long_b = dir_b == "long"
            big = 1e9
            for j in range(i + 1, min(i + 1 + MAX_HOLD_BARS, n)):
                hit_sl_a, _ = _hit_sl_tp(
                    bars_a[j], is_long_a, sl_a,
                    entry_a + big * (1 if is_long_a else -1),
                )
                hit_sl_b, _ = _hit_sl_tp(
                    bars_b[j], is_long_b, sl_b,
                    entry_b + big * (1 if is_long_b else -1),
                )
                if hit_sl_a or hit_sl_b:
                    exit_j = j
                    exit_reason = "sl"
                    break
                spread_now = bars_a[j].close - beta_ent * bars_b[j].close
                z_now = (spread_now - mu) / sd
                if abs(z_now) < STATARB_Z_EXIT:
                    exit_j = j
                    exit_reason = "pair_exit"
                    break
            if exit_j is None:
                exit_j = min(i + MAX_HOLD_BARS, n - 1)

            exit_a = bars_a[exit_j].close if exit_reason != "sl" else sl_a
            exit_b = bars_b[exit_j].close if exit_reason != "sl" else sl_b

            gross_a = (exit_a - entry_a) / ps_a
            if dir_a == "short":
                gross_a = -gross_a
            net_a = gross_a - sp_a
            gross_b = (exit_b - entry_b) / ps_b
            if dir_b == "short":
                gross_b = -gross_b
            net_b = gross_b - sp_b

            trades.append(Trade(
                strategy="stat_arb", instrument=sym_a,
                direction=dir_a, entry_ts=bars_a[i].ts, entry_price=entry_a,
                exit_ts=bars_a[exit_j].ts, exit_price=exit_a,
                sl=sl_a, tp=0.0, reason=exit_reason,
                bars_held=exit_j - i, pnl_pips=round(gross_a, 2), net_pips=round(net_a, 2),
            ))
            trades.append(Trade(
                strategy="stat_arb", instrument=sym_b,
                direction=dir_b, entry_ts=bars_b[i].ts, entry_price=entry_b,
                exit_ts=bars_b[exit_j].ts, exit_price=exit_b,
                sl=sl_b, tp=0.0, reason=exit_reason,
                bars_held=exit_j - i, pnl_pips=round(gross_b, 2), net_pips=round(net_b, 2),
            ))
            i = exit_j + 1
    return trades


# ────────────────────── spread multipliers (из cost_model) ──────────────────────

_SPREAD_BT_MULT: dict[str, float] = {
    "vwap_deviation": 1.2,
    "orb_breakout": 1.2,
    "stat_arb": 1.2,
    "news_fade": 1.5,
}


# ────────────────────── метрики ──────────────────────

@dataclass
class StratReport:
    name: str
    trades: int
    wins: int
    losses: int
    wr: float
    gross_pips: float
    net_pips: float
    avg_win: float
    avg_loss: float
    expectancy_R: float
    profit_factor: float
    sharpe: float
    max_dd_pips: float
    profitable_days_pct: float
    by_instrument: dict[str, tuple[int, float]] = field(default_factory=dict)


def _max_drawdown(returns: list[float]) -> float:
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd


def summarize(name: str, trades: list[Trade]) -> StratReport:
    if not trades:
        return StratReport(name=name, trades=0, wins=0, losses=0, wr=0.0,
                           gross_pips=0.0, net_pips=0.0, avg_win=0.0, avg_loss=0.0,
                           expectancy_R=0.0, profit_factor=0.0, sharpe=0.0,
                           max_dd_pips=0.0, profitable_days_pct=0.0)

    nets = [t.net_pips for t in trades]
    gross = sum(t.pnl_pips for t in trades)
    net = sum(nets)
    wins_l = [n for n in nets if n > 0]
    losses_l = [n for n in nets if n <= 0]
    wr = len(wins_l) / len(nets) * 100
    aw = statistics.mean(wins_l) if wins_l else 0.0
    al = statistics.mean(losses_l) if losses_l else 0.0
    pf = (sum(wins_l) / abs(sum(losses_l))) if losses_l and sum(losses_l) != 0 else float("inf") if wins_l else 0.0
    std = statistics.pstdev(nets) if len(nets) > 1 else 0.0
    sharpe = (statistics.mean(nets) / std * math.sqrt(252)) if std > 0 else 0.0
    exp_r = (statistics.mean(nets) / abs(al)) if al != 0 else 0.0
    max_dd = _max_drawdown(nets)

    by_day: dict[date, float] = defaultdict(float)
    for t in trades:
        by_day[t.entry_ts.date()] += t.net_pips
    days_profitable = sum(1 for v in by_day.values() if v > 0)
    days_total = len(by_day)
    pd_pct = days_profitable / days_total * 100 if days_total else 0.0

    by_instr: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
    acc: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        acc[t.instrument].append(t.net_pips)
    for k, v in acc.items():
        by_instr[k] = (len(v), round(sum(v), 1))

    return StratReport(
        name=name, trades=len(nets), wins=len(wins_l), losses=len(losses_l),
        wr=round(wr, 1), gross_pips=round(gross, 1), net_pips=round(net, 1),
        avg_win=round(aw, 2), avg_loss=round(al, 2),
        expectancy_R=round(exp_r, 2), profit_factor=round(pf, 2),
        sharpe=round(sharpe, 2), max_dd_pips=round(max_dd, 1),
        profitable_days_pct=round(pd_pct, 1), by_instrument=dict(by_instr),
    )


def print_report(reports: list[StratReport]) -> None:
    print()
    print("=" * 100)
    print(f"{'Strategy':<20}{'n':>6}{'WR%':>6}{'Gross':>8}{'Net':>8}{'AvgW':>7}{'AvgL':>7}"
          f"{'Exp':>6}{'PF':>6}{'Sharpe':>8}{'MaxDD':>8}{'PD%':>6}")
    print("-" * 100)
    for r in reports:
        print(f"{r.name:<20}{r.trades:>6}{r.wr:>6.1f}"
              f"{r.gross_pips:>8.1f}{r.net_pips:>8.1f}"
              f"{r.avg_win:>7.1f}{r.avg_loss:>7.1f}"
              f"{r.expectancy_R:>6.2f}{r.profit_factor:>6.2f}"
              f"{r.sharpe:>8.2f}{r.max_dd_pips:>8.1f}{r.profitable_days_pct:>6.1f}")
    print("=" * 100)
    for r in reports:
        if not r.by_instrument:
            continue
        print(f"\n{r.name} — по инструментам:")
        items = sorted(r.by_instrument.items(), key=lambda kv: kv[1][1])
        for sym, (n, net) in items:
            disp = DISPLAY_NAMES.get(sym, sym)
            marker = "+" if net > 0 else " "
            print(f"  {marker} {disp:<12} n={n:<4} net={net:+8.1f} pips")


def write_trades_csv(trades: list[Trade], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "instrument", "direction", "entry_ts", "entry_price",
                    "exit_ts", "exit_price", "sl", "tp", "reason", "bars_held",
                    "pnl_pips", "net_pips"])
        for t in trades:
            w.writerow([t.strategy, t.instrument, t.direction,
                        t.entry_ts.isoformat(), t.entry_price,
                        t.exit_ts.isoformat(), t.exit_price,
                        t.sl, t.tp, t.reason, t.bars_held,
                        t.pnl_pips, t.net_pips])


# ────────────────────── main ──────────────────────

STRATEGIES = {
    "vwap": ("vwap_reversion", backtest_vwap),
    "orb": ("session_orb", backtest_orb),
    "statarb": ("stat_arb", backtest_statarb),
}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="data/fxpro_klines", help="Папка с M5 CSV")
    p.add_argument("--strategies", default="vwap,orb,statarb",
                   help="Список стратегий через запятую")
    p.add_argument("--out-dir", default="data", help="Куда писать trades CSV")
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    symbols = (
        "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X",
        "EURJPY=X", "GBPJPY=X", "EURGBP=X",
        "GC=F", "CL=F", "BZ=F", "NG=F", "ES=F",
    )
    all_bars: dict[str, list[Bar]] = {}
    for sym in symbols:
        bars = load_bars(data_dir, sym)
        if bars:
            all_bars[sym] = bars
            log.info("Loaded %-12s %d bars (%s → %s)",
                     sym, len(bars), bars[0].ts.date(), bars[-1].ts.date())

    log.info("Итого %d инструментов, %d баров",
             len(all_bars), sum(len(v) for v in all_bars.values()))

    reports: list[StratReport] = []
    all_trades: list[Trade] = []

    for key in strategies:
        if key not in STRATEGIES:
            log.warning("Неизвестная стратегия: %s", key)
            continue
        name, fn = STRATEGIES[key]
        log.info("── Backtest %s ──", name)
        trades = fn(all_bars)
        log.info("  %s: %d trades", name, len(trades))
        rep = summarize(name, trades)
        reports.append(rep)
        all_trades.extend(trades)

    print_report(reports)

    out_dir = Path(args.out_dir)
    write_trades_csv(all_trades, out_dir / "backtest_fxpro_trades.csv")
    log.info("Saved trades → %s", out_dir / "backtest_fxpro_trades.csv")

    return 0


if __name__ == "__main__":
    sys.exit(main())
