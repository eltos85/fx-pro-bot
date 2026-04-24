#!/usr/bin/env python3
"""Backtest Keltner Mean-Reversion Scalper (КАНДИДАТ №1).

Hypothesis: на M5 FX в активных сессиях, когда цена резко отклоняется
от краткосрочного среднего (касание Keltner границы), но старший тренд
H1 её НЕ ломает — происходит короткий mean-revert до EMA.

Параметры зафиксированы ДО запуска (см. STRATEGIES.md draft).
Изменения параметров после IS-прогона — ЗАПРЕЩЕНЫ (anti-overfitting).

Запуск:
    PYTHONPATH=src python3 -m scripts.backtest_keltner_mr
    PYTHONPATH=src python3 -m scripts.backtest_keltner_mr --split IS
    PYTHONPATH=src python3 -m scripts.backtest_keltner_mr --split OOS
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
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from fx_pro_bot.analysis.signals import _atr, _ema, compute_adx, _rsi
from fx_pro_bot.config.settings import (
    DISPLAY_NAMES, PIP_VALUES_USD, SPREAD_PIPS,
    pip_size, spread_cost_pips,
)
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.strategies.scalping.indicators import htf_ema_trend

from scripts.backtest_fxpro_all import Trade, load_bars

log = logging.getLogger("backtest_keltner_mr")

# ────────────────────── зафиксированные параметры ──────────────────────

LIVE_WINDOW_BARS = 4000            # ~14 дней M5 (~333 H1 баров — достаточно для EMA200 H1)
BAR_SEC = 5 * 60

# Keltner Channel
KC_EMA_PERIOD = 20
KC_ATR_PERIOD = 14
KC_MULT = 1.8

# RSI
RSI_PERIOD = 14
RSI_LONG_MAX = 35
RSI_SHORT_MIN = 65

# ADX
ADX_PERIOD = 14
ADX_MAX = 25.0

# HTF
HTF_EMA_PERIOD = 200

# SL/TP
SL_ATR_MULT = 1.5
TP_ATR_MULT = 1.5                  # R:R 1:1 — цель: mean-reversion к EMA

# Time stop
MAX_HOLD_BARS = 8                  # 40 минут на M5

# Sessions (UTC): London 08:00-12:00 + NY 13:00-17:00 (без overlap и без NY close)
LONDON_START = time(8, 0)
LONDON_END = time(12, 0)
NY_START = time(13, 0)
NY_END = time(17, 0)

# Макс одновременных позиций на инструмент (в симуляции одна — строго последовательно)
MAX_PER_INSTRUMENT = 1

# Комиссия FxPro (round-trip) в USD per side на 0.01 lot
FXPRO_COMMISSION_PER_SIDE_USD = 0.07
FXPRO_COMMISSION_ROUNDTRIP_USD = FXPRO_COMMISSION_PER_SIDE_USD * 2

# Pessimistic slippage (в pips) per side
SLIPPAGE_PIPS_PER_SIDE = 0.5

# ────────────────────── данные ──────────────────────

INSTRUMENTS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCAD=X", "USDCHF=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X",
    "GC=F", "CL=F", "BZ=F", "NG=F", "ES=F",
]


def commission_pips(symbol: str) -> float:
    """Комиссия round-trip в pips (на 0.01 lot)."""
    pv = PIP_VALUES_USD.get(symbol, 0.10)
    if pv <= 0:
        return 0.0
    return FXPRO_COMMISSION_ROUNDTRIP_USD / pv


def total_cost_pips(symbol: str) -> float:
    """Полная стоимость round-trip в pips (spread + commission + slippage)."""
    return (
        spread_cost_pips(symbol)
        + commission_pips(symbol)
        + SLIPPAGE_PIPS_PER_SIDE * 2
    )


# ────────────────────── session filter ──────────────────────

def _in_session(ts: datetime) -> bool:
    t = ts.time()
    if LONDON_START <= t < LONDON_END:
        return True
    if NY_START <= t < NY_END:
        return True
    return False


# ────────────────────── Keltner MR scan ──────────────────────

def _keltner_bands(window: list[Bar]) -> tuple[float, float, float] | None:
    """Возвращает (upper, mid, lower) или None если недостаточно данных."""
    if len(window) < max(KC_EMA_PERIOD, KC_ATR_PERIOD) + 1:
        return None
    closes = [b.close for b in window]
    ema_vals = _ema(closes, KC_EMA_PERIOD)
    if not ema_vals:
        return None
    mid = ema_vals[-1]
    atr_v = _atr(window, period=KC_ATR_PERIOD) if False else _atr(window)
    if atr_v <= 0:
        return None
    upper = mid + KC_MULT * atr_v
    lower = mid - KC_MULT * atr_v
    return upper, mid, lower


def scan_keltner_mr(
    window: list[Bar], price: float, ts: datetime,
) -> tuple[str, float] | None:
    """Возвращает (direction, atr) или None.

    Требования:
    - Session window (London/NY, без overlap/close)
    - ADX < ADX_MAX
    - Touch Keltner: low ≤ lower (long) или high ≥ upper (short) на последнем баре
    - RSI confirmation: < RSI_LONG_MAX (long), > RSI_SHORT_MIN (short)
    - HTF EMA200 H1 alignment: slope ≥ 0 (long), ≤ 0 (short)
    - close бара ВНУТРИ канала (reversal signal, не breakout) — если close остался
      за границей, это продолжение движения, пропускаем
    """
    if not _in_session(ts):
        return None
    if len(window) < LIVE_WINDOW_BARS // 2:
        return None
    atr_v = _atr(window)
    if atr_v <= 0:
        return None
    if compute_adx(window) > ADX_MAX:
        return None
    bands = _keltner_bands(window)
    if bands is None:
        return None
    upper, mid, lower = bands

    last = window[-1]
    closes = [b.close for b in window]
    rsi = _rsi(closes, RSI_PERIOD)
    htf_slope = htf_ema_trend(window, ema_period=HTF_EMA_PERIOD)
    if htf_slope is None:
        return None

    # LONG: touch lower + RSI oversold + HTF non-downtrend + close вернулся в канал
    if last.low <= lower and last.close > lower:
        if rsi < RSI_LONG_MAX and htf_slope >= 0:
            return ("long", atr_v)

    # SHORT: touch upper + RSI overbought + HTF non-uptrend + close вернулся в канал
    if last.high >= upper and last.close < upper:
        if rsi > RSI_SHORT_MIN and htf_slope <= 0:
            return ("short", atr_v)

    return None


# ────────────────────── симуляция ──────────────────────

def simulate_keltner(
    bars: list[Bar], entry_idx: int, direction: str,
    entry_price: float, atr_v: float,
    symbol: str,
) -> Trade | None:
    """Симулирует позицию с SL/TP от ATR, time-stop MAX_HOLD_BARS.

    Net pips = gross - (spread + commission + slippage × 2).
    """
    is_long = direction == "long"
    ps = pip_size(symbol)
    if ps <= 0:
        return None

    sl = entry_price - SL_ATR_MULT * atr_v if is_long else entry_price + SL_ATR_MULT * atr_v
    tp = entry_price + TP_ATR_MULT * atr_v if is_long else entry_price - TP_ATR_MULT * atr_v
    cost = total_cost_pips(symbol)

    for j in range(entry_idx + 1, min(entry_idx + 1 + MAX_HOLD_BARS, len(bars))):
        b = bars[j]
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
            strategy="keltner_mr", instrument=symbol,
            direction=direction, entry_ts=bars[entry_idx].ts,
            entry_price=entry_price, exit_ts=b.ts, exit_price=exit_price,
            sl=sl, tp=tp, reason=reason,
            bars_held=j - entry_idx,
            pnl_pips=round(gross, 2), net_pips=round(net, 2),
        )

    # time-stop
    end = min(entry_idx + MAX_HOLD_BARS, len(bars) - 1)
    b = bars[end]
    exit_price = b.close
    gross = (exit_price - entry_price) / ps
    if not is_long:
        gross = -gross
    net = gross - cost
    return Trade(
        strategy="keltner_mr", instrument=symbol,
        direction=direction, entry_ts=bars[entry_idx].ts,
        entry_price=entry_price, exit_ts=b.ts, exit_price=exit_price,
        sl=sl, tp=tp, reason="time",
        bars_held=end - entry_idx,
        pnl_pips=round(gross, 2), net_pips=round(net, 2),
    )


# ────────────────────── бэктест по инструменту ──────────────────────

def backtest_instrument(
    bars: list[Bar],
    symbol: str,
    *,
    split_start: datetime | None,
    split_end: datetime | None,
) -> list[Trade]:
    trades: list[Trade] = []
    n = len(bars)
    if n < LIVE_WINDOW_BARS + 1:
        return trades

    i = LIVE_WINDOW_BARS
    while i < n:
        ts = bars[i].ts
        if split_start is not None and ts < split_start:
            i += 1
            continue
        if split_end is not None and ts >= split_end:
            break

        window = bars[max(0, i - LIVE_WINDOW_BARS + 1): i + 1]
        price = bars[i].close
        sig = scan_keltner_mr(window, price, ts)
        if sig is None:
            i += 1
            continue

        direction, atr_v = sig
        tr = simulate_keltner(bars, i, direction, price, atr_v, symbol)
        if tr is None:
            i += 1
            continue
        trades.append(tr)
        # после входа — пропускаем до выхода (one-position-at-a-time per instrument)
        i += tr.bars_held + 1

    return trades


# ────────────────────── split по времени ──────────────────────

def compute_split_boundaries(
    data_dir: Path, is_fraction: float = 0.7,
) -> dict[str, datetime]:
    """Per-instrument split: у каждого инструмента свои 70/30 по его данным.

    Это честно: warmup ест первые ~14 дней у каждого инструмента,
    поэтому эффективный IS/OOS calendar-mapping рассчитывается
    на основе всего диапазона этого инструмента.
    """
    result: dict[str, datetime] = {}
    for sym in INSTRUMENTS:
        bars = load_bars(data_dir, sym)
        if not bars:
            continue
        span = bars[-1].ts - bars[0].ts
        boundary = bars[0].ts + span * is_fraction
        result[sym] = boundary
        log.info(
            "  %s: %s → %s (%.1f days) | boundary %s",
            sym, bars[0].ts, bars[-1].ts,
            span.total_seconds() / 86400, boundary,
        )
    if not result:
        raise SystemExit("Нет данных ни по одному инструменту")
    return result


# ────────────────────── отчёт ──────────────────────

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


def _per_instrument_report(trades: list[Trade]) -> list[InstrumentReport]:
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
        tpd = (
            len(ts) / len(by_day) if by_day else 0.0
        )
        reports.append(InstrumentReport(
            symbol=sym, trades=len(ts), wr=round(wr, 1),
            net_pips=round(net, 1), gross_pips=round(gross, 1),
            pf=round(pf, 2) if pf != float("inf") else 99.99,
            avg_win=round(aw, 2), avg_loss=round(al, 2),
            trades_per_day=round(tpd, 2),
        ))
    reports.sort(key=lambda r: r.net_pips, reverse=True)
    return reports


def _aggregate_report(trades: list[Trade], label: str) -> str:
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
    print(_aggregate_report(trades, label))
    print("-" * 110)
    print(f"{'Symbol':<12}{'n':>6}{'/day':>7}{'WR%':>7}{'Net':>9}{'Gross':>9}"
          f"{'PF':>7}{'AvgW':>8}{'AvgL':>8}")
    print("-" * 110)
    for r in _per_instrument_report(trades):
        print(f"{r.symbol:<12}{r.trades:>6}{r.trades_per_day:>7.2f}{r.wr:>7.1f}"
              f"{r.net_pips:>9.1f}{r.gross_pips:>9.1f}{r.pf:>7.2f}"
              f"{r.avg_win:>8.2f}{r.avg_loss:>8.2f}")


def write_trades_csv(trades: list[Trade], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "strategy", "instrument", "direction",
            "entry_ts", "entry_price", "exit_ts", "exit_price",
            "sl", "tp", "reason", "bars_held", "pnl_pips", "net_pips",
        ])
        for t in trades:
            w.writerow([
                t.strategy, t.instrument, t.direction,
                t.entry_ts.isoformat(), t.entry_price,
                t.exit_ts.isoformat(), t.exit_price,
                round(t.sl, 5), round(t.tp, 5), t.reason,
                t.bars_held, t.pnl_pips, t.net_pips,
            ])


# ────────────────────── main ──────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("data/fxpro_klines"))
    ap.add_argument("--split", choices=["IS", "OOS", "ALL"], default="IS")
    ap.add_argument("--is-fraction", type=float, default=0.7)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    boundaries = compute_split_boundaries(args.data_dir, args.is_fraction)

    if args.split == "IS":
        label = "IS (70% per instrument)"
    elif args.split == "OOS":
        label = "OOS (30% per instrument)"
    else:
        label = "ALL"

    # Печать cost model
    print()
    print("=" * 110)
    print("COST MODEL (round-trip, 0.01 lot, FxPro demo):")
    print(f"{'Symbol':<12}{'Spread':>8}{'Comm':>8}{'Slip':>8}{'Total':>8}")
    print("-" * 44)
    for sym in INSTRUMENTS:
        sp = spread_cost_pips(sym)
        cm = commission_pips(sym)
        sl = SLIPPAGE_PIPS_PER_SIDE * 2
        tot = sp + cm + sl
        print(f"{sym:<12}{sp:>8.2f}{cm:>8.2f}{sl:>8.2f}{tot:>8.2f}")

    # Прогон по всем инструментам
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

    print_report(all_trades, label=f"KELTNER_MR {label}")

    if args.out:
        write_trades_csv(all_trades, args.out)
        print(f"\nTrades CSV: {args.out}")


if __name__ == "__main__":
    main()
