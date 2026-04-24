#!/usr/bin/env python3
"""Backtest EMA Pullback Trend-Following (КАНДИДАТ №2).

Hypothesis: в трендовом режиме (ADX >= 20 на M15, HTF H1 EMA200 slope
алигнут) цена делает pullback к M15 EMA50 и продолжает движение по
тренду. Edge = trend-following на middle timeframe, комиссии
разбавляются бóльшими TP.

Параметры зафиксированы ДО запуска. Менять после IS — запрещено.

Запуск:
    PYTHONPATH=src python3 -m scripts.backtest_ema_pullback --split IS
    PYTHONPATH=src python3 -m scripts.backtest_ema_pullback --split OOS
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from fx_pro_bot.analysis.signals import _atr, _ema, compute_adx
from fx_pro_bot.config.settings import (
    DISPLAY_NAMES, PIP_VALUES_USD, SPREAD_PIPS,
    pip_size, spread_cost_pips,
)
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.strategies.scalping.indicators import (
    htf_ema_trend, resample_m5_to_h1,
)

from scripts.backtest_fxpro_all import Trade, load_bars

log = logging.getLogger("backtest_ema_pullback")

# ────────────────────── зафиксированные параметры ──────────────────────

# Warmup — нужно для M15 EMA50, ADX14 и H1 EMA200
# 4000 M5 баров = 13.9 дней = ~333 H1 = достаточно
WARMUP_BARS_M5 = 4000

# EMAs на M15
FAST_EMA = 21
SLOW_EMA = 50

# HTF H1
HTF_EMA_PERIOD = 200

# ADX на M15
ADX_PERIOD = 14
ADX_MIN = 20.0

# Pullback detection
PULLBACK_LOOKBACK = 3  # M15 бары

# SL/TP
SL_ATR_MULT = 1.0
TP_ATR_MULT = 2.0

# Time stop
MAX_HOLD_BARS_M15 = 16  # 4 часа

# Sessions (UTC): activehours 08-17
ACTIVE_START = time(8, 0)
ACTIVE_END = time(17, 0)

# Weekend exclusion: Friday >= 16:00 UTC
FRIDAY_CUTOFF = time(16, 0)

# Комиссия FxPro (same as Keltner test)
FXPRO_COMMISSION_PER_SIDE_USD = 0.07
FXPRO_COMMISSION_ROUNDTRIP_USD = FXPRO_COMMISSION_PER_SIDE_USD * 2
SLIPPAGE_PIPS_PER_SIDE = 0.5


INSTRUMENTS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCAD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
    "GC=F", "CL=F", "BZ=F", "NG=F", "ES=F",
]


def commission_pips(symbol: str) -> float:
    pv = PIP_VALUES_USD.get(symbol, 0.10)
    if pv <= 0:
        return 0.0
    return FXPRO_COMMISSION_ROUNDTRIP_USD / pv


def total_cost_pips(symbol: str) -> float:
    return (
        spread_cost_pips(symbol)
        + commission_pips(symbol)
        + SLIPPAGE_PIPS_PER_SIDE * 2
    )


# ────────────────────── session filter ──────────────────────

def _in_active_session(ts: datetime) -> bool:
    # Sat/Sun — exclude
    if ts.weekday() >= 5:
        return False
    # Friday >= 16:00 UTC — exclude (weekend gap risk)
    if ts.weekday() == 4 and ts.time() >= FRIDAY_CUTOFF:
        return False
    t = ts.time()
    return ACTIVE_START <= t < ACTIVE_END


# ────────────────────── resample M5 -> M15 ──────────────────────

def resample_m5_to_m15(bars_m5: list[Bar]) -> list[Bar]:
    """Агрегирует 3 M5 бара в 1 M15 по 15-минутным блокам UTC (00, 15, 30, 45)."""
    if not bars_m5:
        return []
    groups: dict[tuple[int, int, int, int, int], list[Bar]] = {}
    for b in bars_m5:
        minute_block = (b.ts.minute // 15) * 15
        key = (b.ts.year, b.ts.month, b.ts.day, b.ts.hour, minute_block)
        groups.setdefault(key, []).append(b)
    result: list[Bar] = []
    for key in sorted(groups):
        grp = groups[key]
        start_ts = datetime(*key, tzinfo=UTC)
        result.append(Bar(
            instrument=grp[0].instrument,
            ts=start_ts,
            open=grp[0].open,
            high=max(b.high for b in grp),
            low=min(b.low for b in grp),
            close=grp[-1].close,
            volume=sum(b.volume for b in grp),
        ))
    return result


# ────────────────────── scan ──────────────────────

def scan_ema_pullback(
    bars_m15: list[Bar], bars_m5: list[Bar], ts: datetime,
) -> tuple[str, float] | None:
    """Возвращает (direction, atr_m15) или None.

    Требования для LONG (SHORT зеркально):
    - активная сессия
    - HTF H1 EMA200 slope > 0
    - ADX(14) на M15 >= 20
    - EMA21 > EMA50 на M15
    - в последних 3 барах low <= EMA50 (был pullback к EMA)
    - текущий бар close > EMA50 (retest прошёл)
    """
    if not _in_active_session(ts):
        return None
    if len(bars_m15) < max(SLOW_EMA, ADX_PERIOD) + 5:
        return None
    # используем M5 для HTF (он ресемплит сам)
    htf_slope = htf_ema_trend(bars_m5, ema_period=HTF_EMA_PERIOD)
    if htf_slope is None:
        return None

    atr_v = _atr(bars_m15)
    if atr_v <= 0:
        return None

    adx = compute_adx(bars_m15, period=ADX_PERIOD) if False else compute_adx(bars_m15)
    if adx < ADX_MIN:
        return None

    closes = [b.close for b in bars_m15]
    ema_fast = _ema(closes, FAST_EMA)
    ema_slow = _ema(closes, SLOW_EMA)
    if not ema_fast or not ema_slow:
        return None
    ef = ema_fast[-1]
    es = ema_slow[-1]
    last = bars_m15[-1]

    # Pullback lookback
    lookback = bars_m15[-PULLBACK_LOOKBACK:]
    lows = [b.low for b in lookback]
    highs = [b.high for b in lookback]
    min_low = min(lows)
    max_high = max(highs)

    # LONG
    if htf_slope > 0 and ef > es and min_low <= es and last.close > es:
        return ("long", atr_v)

    # SHORT
    if htf_slope < 0 and ef < es and max_high >= es and last.close < es:
        return ("short", atr_v)

    return None


# ────────────────────── simulate ──────────────────────

def simulate_pullback(
    bars_m15: list[Bar], entry_idx: int, direction: str,
    entry_price: float, atr_v: float,
    symbol: str,
) -> Trade | None:
    is_long = direction == "long"
    ps = pip_size(symbol)
    if ps <= 0:
        return None

    sl = entry_price - SL_ATR_MULT * atr_v if is_long else entry_price + SL_ATR_MULT * atr_v
    tp = entry_price + TP_ATR_MULT * atr_v if is_long else entry_price - TP_ATR_MULT * atr_v
    cost = total_cost_pips(symbol)

    for j in range(entry_idx + 1, min(entry_idx + 1 + MAX_HOLD_BARS_M15, len(bars_m15))):
        b = bars_m15[j]
        hit_sl = (b.low <= sl) if is_long else (b.high >= sl)
        hit_tp = (b.high >= tp) if is_long else (b.low <= tp)
        if hit_sl and hit_tp:
            exit_price, reason = sl, "sl"
        elif hit_sl:
            exit_price, reason = sl, "sl"
        elif hit_tp:
            exit_price, reason = tp, "tp"
        else:
            continue
        gross = (exit_price - entry_price) / ps
        if not is_long:
            gross = -gross
        net = gross - cost
        return Trade(
            strategy="ema_pullback", instrument=symbol,
            direction=direction, entry_ts=bars_m15[entry_idx].ts,
            entry_price=entry_price, exit_ts=b.ts, exit_price=exit_price,
            sl=sl, tp=tp, reason=reason,
            bars_held=j - entry_idx,
            pnl_pips=round(gross, 2), net_pips=round(net, 2),
        )

    end = min(entry_idx + MAX_HOLD_BARS_M15, len(bars_m15) - 1)
    b = bars_m15[end]
    exit_price = b.close
    gross = (exit_price - entry_price) / ps
    if not is_long:
        gross = -gross
    net = gross - cost
    return Trade(
        strategy="ema_pullback", instrument=symbol,
        direction=direction, entry_ts=bars_m15[entry_idx].ts,
        entry_price=entry_price, exit_ts=b.ts, exit_price=exit_price,
        sl=sl, tp=tp, reason="time",
        bars_held=end - entry_idx,
        pnl_pips=round(gross, 2), net_pips=round(net, 2),
    )


# ────────────────────── backtest ──────────────────────

def backtest_instrument(
    bars_m5: list[Bar],
    symbol: str,
    *,
    split_start: datetime | None,
    split_end: datetime | None,
) -> list[Trade]:
    trades: list[Trade] = []
    n_m5 = len(bars_m5)
    if n_m5 < WARMUP_BARS_M5 + 1:
        return trades

    # ресемплим весь массив M5 -> M15 один раз
    bars_m15_all = resample_m5_to_m15(bars_m5)
    if len(bars_m15_all) < 100:
        return trades

    # warmup index в M15: соответствует WARMUP_BARS_M5 в M5
    # M15 бар начинается в кратное 15 мин time. Найдём индекс M15 для
    # первой ts после WARMUP_BARS_M5[-1]
    warmup_ts = bars_m5[WARMUP_BARS_M5 - 1].ts
    m15_start_idx = 0
    for i, b in enumerate(bars_m15_all):
        if b.ts >= warmup_ts:
            m15_start_idx = i
            break

    i = m15_start_idx
    while i < len(bars_m15_all):
        ts = bars_m15_all[i].ts
        if split_start is not None and ts < split_start:
            i += 1
            continue
        if split_end is not None and ts >= split_end:
            break

        # Окно M15 и соответствующий кусок M5 для HTF
        window_m15 = bars_m15_all[max(0, i - 300): i + 1]
        # M5 окно: все до текущей ts
        m5_end_idx = None
        for k in range(len(bars_m5) - 1, -1, -1):
            if bars_m5[k].ts <= ts:
                m5_end_idx = k
                break
        if m5_end_idx is None or m5_end_idx < WARMUP_BARS_M5:
            i += 1
            continue
        window_m5 = bars_m5[max(0, m5_end_idx - WARMUP_BARS_M5 + 1): m5_end_idx + 1]

        sig = scan_ema_pullback(window_m15, window_m5, ts)
        if sig is None:
            i += 1
            continue

        direction, atr_v = sig
        entry_price = bars_m15_all[i].close
        tr = simulate_pullback(bars_m15_all, i, direction, entry_price, atr_v, symbol)
        if tr is None:
            i += 1
            continue
        trades.append(tr)
        i += tr.bars_held + 1

    return trades


# ────────────────────── split ──────────────────────

def compute_split_boundaries(
    data_dir: Path, is_fraction: float = 0.7,
) -> dict[str, datetime]:
    result: dict[str, datetime] = {}
    for sym in INSTRUMENTS:
        bars = load_bars(data_dir, sym)
        if not bars:
            continue
        span = bars[-1].ts - bars[0].ts
        boundary = bars[0].ts + span * is_fraction
        result[sym] = boundary
    return result


# ────────────────────── report ──────────────────────

@dataclass
class InstrumentReport:
    symbol: str
    trades: int
    wr: float
    net_pips: float
    gross_pips: float
    pf: float
    avg_win: float
    avg_loss: float
    trades_per_day: float


def _per_instrument(trades: list[Trade]) -> list[InstrumentReport]:
    by_sym: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_sym[t.instrument].append(t)
    reports: list[InstrumentReport] = []
    for sym, ts in by_sym.items():
        nets = [t.net_pips for t in ts]
        gross = sum(t.pnl_pips for t in ts)
        net = sum(nets)
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n <= 0]
        wr = len(wins) / len(nets) * 100 if nets else 0.0
        pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (
            float("inf") if wins else 0.0
        )
        aw = statistics.mean(wins) if wins else 0.0
        al = statistics.mean(losses) if losses else 0.0
        by_day: dict[date, int] = defaultdict(int)
        for t in ts:
            by_day[t.entry_ts.date()] += 1
        tpd = len(ts) / len(by_day) if by_day else 0.0
        reports.append(InstrumentReport(
            symbol=sym, trades=len(ts), wr=round(wr, 1),
            net_pips=round(net, 1), gross_pips=round(gross, 1),
            pf=round(pf, 2) if pf != float("inf") else 99.99,
            avg_win=round(aw, 2), avg_loss=round(al, 2),
            trades_per_day=round(tpd, 2),
        ))
    reports.sort(key=lambda r: r.net_pips, reverse=True)
    return reports


def _aggregate(trades: list[Trade], label: str) -> str:
    nets = [t.net_pips for t in trades]
    if not nets:
        return f"{label}: no trades"
    net = sum(nets)
    gross = sum(t.pnl_pips for t in trades)
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    wr = len(wins) / len(nets) * 100
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    by_day: dict[date, list[float]] = defaultdict(list)
    for t in trades:
        by_day[t.entry_ts.date()].append(t.net_pips)
    days = len(by_day)
    tpd = len(trades) / days if days else 0.0
    prof_days = sum(1 for v in by_day.values() if sum(v) > 0) / days * 100 if days else 0.0
    aw = statistics.mean(wins) if wins else 0.0
    al = statistics.mean(losses) if losses else 0.0
    std = statistics.pstdev(nets) if len(nets) > 1 else 0.0
    sharpe = (statistics.mean(nets) / std * math.sqrt(252)) if std > 0 else 0.0
    return (
        f"{label}: {len(trades)} trades | {days} days | {tpd:.1f}/day | "
        f"WR {wr:.1f}% | Net {net:+.1f} pips | Gross {gross:+.1f} | "
        f"PF {pf:.2f} | AvgW {aw:+.2f} / AvgL {al:+.2f} | "
        f"Prof.days {prof_days:.1f}% | Sharpe {sharpe:.2f}"
    )


def print_report(trades: list[Trade], label: str) -> None:
    print()
    print("=" * 110)
    print(_aggregate(trades, label))
    print("-" * 110)
    print(f"{'Symbol':<12}{'n':>6}{'/day':>7}{'WR%':>7}{'Net':>9}{'Gross':>9}"
          f"{'PF':>7}{'AvgW':>8}{'AvgL':>8}")
    print("-" * 110)
    for r in _per_instrument(trades):
        print(f"{r.symbol:<12}{r.trades:>6}{r.trades_per_day:>7.2f}{r.wr:>7.1f}"
              f"{r.net_pips:>9.1f}{r.gross_pips:>9.1f}{r.pf:>7.2f}"
              f"{r.avg_win:>8.2f}{r.avg_loss:>8.2f}")


# ────────────────────── main ──────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--split", choices=["IS", "OOS", "ALL"], default="IS")
    ap.add_argument("--is-fraction", type=float, default=0.7)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    boundaries = compute_split_boundaries(args.data_dir, args.is_fraction)

    if args.split == "IS":
        label = "IS (70% per instrument)"
    elif args.split == "OOS":
        label = "OOS (30% per instrument)"
    else:
        label = "ALL"

    print()
    print("=" * 110)
    print("COST MODEL (round-trip, 0.01 lot, FxPro demo):")
    print(f"{'Symbol':<12}{'Spread':>8}{'Comm':>8}{'Slip':>8}{'Total':>8}")
    for sym in INSTRUMENTS:
        sp = spread_cost_pips(sym)
        cm = commission_pips(sym)
        sl = SLIPPAGE_PIPS_PER_SIDE * 2
        print(f"{sym:<12}{sp:>8.2f}{cm:>8.2f}{sl:>8.2f}{(sp+cm+sl):>8.2f}")

    all_trades: list[Trade] = []
    for sym in INSTRUMENTS:
        bars = load_bars(args.data_dir, sym)
        if not bars:
            print(f"[WARN] no data: {sym}")
            continue
        boundary = boundaries.get(sym)
        if args.split == "IS":
            split_start, split_end = None, boundary
        elif args.split == "OOS":
            split_start, split_end = boundary, None
        else:
            split_start, split_end = None, None
        trades = backtest_instrument(
            bars, sym, split_start=split_start, split_end=split_end,
        )
        all_trades.extend(trades)

    print_report(all_trades, label=f"EMA_PULLBACK {label}")

    if args.out:
        with args.out.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "strategy", "instrument", "direction",
                "entry_ts", "entry_price", "exit_ts", "exit_price",
                "sl", "tp", "reason", "bars_held", "pnl_pips", "net_pips",
            ])
            for t in all_trades:
                w.writerow([
                    t.strategy, t.instrument, t.direction,
                    t.entry_ts.isoformat(), t.entry_price,
                    t.exit_ts.isoformat(), t.exit_price,
                    round(t.sl, 5), round(t.tp, 5), t.reason,
                    t.bars_held, t.pnl_pips, t.net_pips,
                ])
        print(f"\nTrades CSV: {args.out}")


if __name__ == "__main__":
    main()
