#!/usr/bin/env python3
"""Backtest 3 кандидатов FX-стратегий на 90d M5-истории.

Кандидаты:
1. gold_orb_iso    — изолированный Gold ORB с confirm-bar (London + NY open)
2. asia_breakout   — Asian Range Breakout на London open (FX major)
3. bb_reversion_h1 — Bollinger Bands mean-reversion на H1 (FX major)

baseline: session_orb на всех инструментах (из backtest_fxpro_all.py).

Использует те же загрузчики и метрики. Запуск:
    PYTHONPATH=src python3 -m scripts.backtest_fxpro_candidates
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

from fx_pro_bot.analysis.signals import _atr, _ema, compute_adx, _rsi
from fx_pro_bot.config.settings import DISPLAY_NAMES, pip_size, spread_cost_pips
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.strategies.scalping.indicators import ema_slope

# Переиспользуем из главного backtest
from scripts.backtest_fxpro_all import (
    Trade, _hit_sl_tp, load_bars, simulate_position,
    summarize, print_report, write_trades_csv,
    LIVE_WINDOW_BARS, MAX_HOLD_BARS,
)

log = logging.getLogger("backtest_fxpro_candidates")


# ────────────────────── Gold ORB Isolated ──────────────────────

GOLD_ORB_SL_ATR = 1.5
GOLD_ORB_TP_ATR = 3.0
GOLD_ORB_ADX_MAX = 100.0   # v2: без ADX-фильтра — Gold торгуется на news/trending

LONDON_OPEN = time(8, 0)
LONDON_ORB_END = time(8, 15)
LONDON_CLOSE = time(12, 0)
NY_OPEN = time(14, 30)
NY_ORB_END = time(14, 45)
NY_CLOSE = time(17, 0)
ORB_BARS = 3


def _session_box_gold(window: list[Bar], ts: datetime) -> tuple[float, float, time] | None:
    """Возвращает (box_high, box_low, session_end) или None."""
    t = ts.time()
    if LONDON_ORB_END <= t < LONDON_CLOSE:
        start_t, end_t, close_t = LONDON_OPEN, LONDON_ORB_END, LONDON_CLOSE
    elif NY_ORB_END <= t < NY_CLOSE:
        start_t, end_t, close_t = NY_OPEN, NY_ORB_END, NY_CLOSE
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
    return (max(b.high for b in box), min(b.low for b in box), close_t)


def backtest_gold_orb(all_bars: dict[str, list[Bar]]) -> list[Trade]:
    """Gold-only ORB с confirm-bar. Вход по close за коробкой."""
    trades: list[Trade] = []
    sym = "GC=F"
    if sym not in all_bars:
        return trades
    bars = all_bars[sym]
    ps = pip_size(sym)
    spread = spread_cost_pips(sym) * 1.2

    open_until = -1
    traded_session: set[tuple[date, str]] = set()  # (день, "london"/"ny") — 1 вход в сессию

    for i in range(LIVE_WINDOW_BARS, len(bars)):
        if i <= open_until:
            continue
        last = bars[i]
        t = last.ts.time()

        if LONDON_ORB_END <= t < LONDON_CLOSE:
            session_tag = "london"
        elif NY_ORB_END <= t < NY_CLOSE:
            session_tag = "ny"
        else:
            continue
        key = (last.ts.date(), session_tag)
        if key in traded_session:
            continue

        window = bars[i - LIVE_WINDOW_BARS: i + 1]
        atr_v = _atr(window)
        if atr_v <= 0:
            continue
        if compute_adx(window) > GOLD_ORB_ADX_MAX:
            continue

        box = _session_box_gold(window, last.ts)
        if box is None:
            continue
        box_high, box_low, _ = box

        # v2: touch-break (high>box_high или low<box_low), без wait confirm
        direction: str | None = None
        if last.high > box_high:
            direction = "long"
            entry_break = box_high
        elif last.low < box_low:
            direction = "short"
            entry_break = box_low
        else:
            continue

        closes = [b.close for b in window]
        ema_vals = _ema(closes, 50)
        slope = ema_slope(ema_vals, 5)
        if direction == "long" and slope < 0:
            continue
        if direction == "short" and slope > 0:
            continue

        entry_price = entry_break  # вход на level, не close бара
        if direction == "long":
            sl = entry_price - GOLD_ORB_SL_ATR * atr_v
            tp = entry_price + GOLD_ORB_TP_ATR * atr_v
        else:
            sl = entry_price + GOLD_ORB_SL_ATR * atr_v
            tp = entry_price - GOLD_ORB_TP_ATR * atr_v

        tr = simulate_position(bars, i, direction, entry_price, sl, tp, spread, ps)
        if tr is None:
            continue
        tr.strategy = "gold_orb_iso"
        tr.instrument = sym
        trades.append(tr)
        traded_session.add(key)
        open_until = i + tr.bars_held

    return trades


# ────────────────────── Asian Range Breakout ──────────────────────

ASIA_START = time(0, 0)
ASIA_END = time(7, 0)
BREAKOUT_START = time(7, 0)
BREAKOUT_END = time(9, 30)  # 2.5 часа окно пробоя

ASIA_BT_INSTRUMENTS = ("EURUSD=X", "GBPUSD=X", "USDJPY=X", "EURJPY=X",
                       "GBPJPY=X", "AUDUSD=X", "USDCAD=X")
ASIA_BT_ADX_MAX = 100.0   # v2: убираем ADX — это entry по времени, не по trend
ASIA_BT_MIN_RANGE_ATR = 0.3   # v2: снижено
ASIA_BT_MAX_RANGE_ATR = 3.0   # v2: расширено


def _asia_range_today(window: list[Bar], today: date) -> tuple[float, float] | None:
    """Asia range за указанный день (high/low среди баров 00:00-07:00 UTC)."""
    highs: list[float] = []
    lows: list[float] = []
    for b in reversed(window):
        if b.ts.date() != today:
            if highs:
                break
            continue
        t = b.ts.time()
        if ASIA_START <= t < ASIA_END:
            highs.append(b.high)
            lows.append(b.low)
    if len(highs) < 10:  # >= 10 баров Asia (50 мин из 7h)
        return None
    return (max(highs), min(lows))


def backtest_asia_breakout(all_bars: dict[str, list[Bar]]) -> list[Trade]:
    """Asian range breakout: London-open пробой Asia hi/lo с SL на противоположной границе."""
    trades: list[Trade] = []
    for sym in ASIA_BT_INSTRUMENTS:
        if sym not in all_bars:
            continue
        bars = all_bars[sym]
        ps = pip_size(sym)
        spread = spread_cost_pips(sym) * 1.2
        open_until = -1
        traded_days: set[date] = set()

        for i in range(LIVE_WINDOW_BARS, len(bars)):
            if i <= open_until:
                continue
            last = bars[i]
            t = last.ts.time()
            today = last.ts.date()

            if not (BREAKOUT_START <= t < BREAKOUT_END):
                continue
            if today in traded_days:
                continue

            window = bars[i - LIVE_WINDOW_BARS: i + 1]
            atr_v = _atr(window)
            if atr_v <= 0:
                continue
            if compute_adx(window) > ASIA_BT_ADX_MAX:
                continue

            asia = _asia_range_today(window, today)
            if asia is None:
                continue
            asia_high, asia_low = asia
            asia_range = asia_high - asia_low
            if asia_range < ASIA_BT_MIN_RANGE_ATR * atr_v:
                continue
            if asia_range > ASIA_BT_MAX_RANGE_ATR * atr_v:
                continue

            # v2: touch-break (high>asia_high или low<asia_low), вход на level
            direction: str | None = None
            if last.high > asia_high:
                direction = "long"
                entry_price = asia_high
            elif last.low < asia_low:
                direction = "short"
                entry_price = asia_low
            else:
                continue

            if direction == "long":
                sl = asia_low
                tp = entry_price + 2 * asia_range
            else:
                sl = asia_high
                tp = entry_price - 2 * asia_range

            # Санити: SL-дистанция не должна превышать 3×ATR (иначе range слишком широкий)
            sl_dist = abs(entry_price - sl)
            if sl_dist > 3 * atr_v or sl_dist < 0.3 * atr_v:
                continue

            tr = simulate_position(bars, i, direction, entry_price, sl, tp, spread, ps)
            if tr is None:
                continue
            tr.strategy = "asia_breakout"
            tr.instrument = sym
            trades.append(tr)
            traded_days.add(today)
            open_until = i + tr.bars_held

    return trades


# ────────────────────── Bollinger Reversion H1 ──────────────────────

BB_PERIOD = 20
BB_SIGMA = 2.0
BB_RSI_LOW = 30
BB_RSI_HIGH = 70
BB_H1_ADX_MAX = 22.0   # v2: строже, только flat рынок

# v2: отбор range-bound пар (из первой итерации: положительные >43 pips net)
BB_REV_INSTRUMENTS = (
    "EURUSD=X", "USDJPY=X", "USDCHF=X", "GBPJPY=X",
)


def _resample_h1(bars: list[Bar]) -> list[Bar]:
    """Склеить M5 в H1 (12 баров → 1)."""
    if not bars:
        return []
    out: list[Bar] = []
    buf: list[Bar] = []
    cur_hour: datetime | None = None
    for b in bars:
        h = b.ts.replace(minute=0, second=0, microsecond=0)
        if cur_hour is None or h != cur_hour:
            if buf:
                out.append(Bar(
                    instrument=buf[0].instrument,
                    ts=cur_hour,
                    open=buf[0].open,
                    high=max(x.high for x in buf),
                    low=min(x.low for x in buf),
                    close=buf[-1].close,
                    volume=sum(x.volume for x in buf),
                ))
            buf = [b]
            cur_hour = h
        else:
            buf.append(b)
    if buf:
        out.append(Bar(
            instrument=buf[0].instrument,
            ts=cur_hour,
            open=buf[0].open,
            high=max(x.high for x in buf),
            low=min(x.low for x in buf),
            close=buf[-1].close,
            volume=sum(x.volume for x in buf),
        ))
    return out


def _bb(prices: list[float], period: int, sigma: float) -> tuple[float, float, float]:
    """Upper, middle, lower по последним period элементам."""
    if len(prices) < period:
        return 0.0, 0.0, 0.0
    w = prices[-period:]
    mid = sum(w) / period
    var = sum((p - mid) ** 2 for p in w) / period
    sd = math.sqrt(var)
    return mid + sigma * sd, mid, mid - sigma * sd


def backtest_bb_reversion_h1(all_bars: dict[str, list[Bar]]) -> list[Trade]:
    """BB mean-reversion на H1. Выход — возврат к SMA20 или SL ±2×ATR."""
    trades: list[Trade] = []
    for sym in BB_REV_INSTRUMENTS:
        if sym not in all_bars:
            continue
        m5_bars = all_bars[sym]
        h1_bars = _resample_h1(m5_bars)
        if len(h1_bars) < BB_PERIOD + 60:
            continue
        ps = pip_size(sym)
        spread = spread_cost_pips(sym) * 1.2

        # Маппинг: для каждого H1-бара находим индекс в M5 (для SL/TP walker)
        ts_to_m5: dict[datetime, int] = {}
        for idx, b in enumerate(m5_bars):
            h = b.ts.replace(minute=0, second=0, microsecond=0)
            if h not in ts_to_m5:
                ts_to_m5[h] = idx

        open_until = -1
        for hi in range(BB_PERIOD + 30, len(h1_bars) - 1):
            h_bar = h1_bars[hi]
            if h_bar.ts not in ts_to_m5:
                continue
            m5_entry_idx = ts_to_m5[h_bar.ts] + 11  # close H1 = ~12-й M5 бар
            if m5_entry_idx >= len(m5_bars):
                continue
            if m5_entry_idx <= open_until:
                continue

            h1_closes = [b.close for b in h1_bars[:hi + 1]]
            atr_h1 = _atr(h1_bars[max(0, hi - 30): hi + 1])
            if atr_h1 <= 0:
                continue
            if compute_adx(h1_bars[max(0, hi - 30): hi + 1]) > BB_H1_ADX_MAX:
                continue

            upper, mid, lower = _bb(h1_closes, BB_PERIOD, BB_SIGMA)
            if upper == 0:
                continue
            rsi = _rsi(h1_closes, 14)
            last_close = h_bar.close

            direction: str | None = None
            if last_close > upper and rsi > BB_RSI_HIGH:
                direction = "short"
                tp = mid
                sl = last_close + 2 * atr_h1
            elif last_close < lower and rsi < BB_RSI_LOW:
                direction = "long"
                tp = mid
                sl = last_close - 2 * atr_h1
            else:
                continue

            entry_price = last_close
            # Санити: mid должен быть в 0.3-3×ATR от entry
            tp_dist = abs(entry_price - tp)
            if tp_dist < 0.3 * atr_h1 or tp_dist > 3 * atr_h1:
                continue

            # walker на M5 до касания SL или TP, максимум 24 H1 = 288 M5
            tr = simulate_position(
                m5_bars, m5_entry_idx, direction, entry_price, sl, tp,
                spread, ps, max_hold=288,
            )
            if tr is None:
                continue
            tr.strategy = "bb_reversion_h1"
            tr.instrument = sym
            trades.append(tr)
            open_until = m5_entry_idx + tr.bars_held

    return trades


# ────────────────────── main ──────────────────────

CANDIDATES = {
    "gold_orb": ("gold_orb_iso", backtest_gold_orb),
    "asia_bo": ("asia_breakout", backtest_asia_breakout),
    "bb_rev_h1": ("bb_reversion_h1", backtest_bb_reversion_h1),
}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="data/fxpro_klines")
    p.add_argument("--candidates", default="gold_orb,asia_bo,bb_rev_h1")
    p.add_argument("--out-dir", default="data")
    p.add_argument("--split", choices=["none", "half", "third"], default="none",
                   help="walk-forward: разбить 90d на 2 (half) или 3 (third) части")
    args = p.parse_args()

    data_dir = Path(args.data_dir)

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
    log.info("Loaded %d инструментов, %d баров",
             len(all_bars), sum(len(v) for v in all_bars.values()))

    out_dir = Path(args.out_dir)

    def _run(name: str, fn, bars_subset: dict[str, list[Bar]], tag: str) -> tuple[list[Trade], object]:
        log.info("── Backtest %s [%s] ──", name, tag)
        trades = fn(bars_subset)
        log.info("  %s [%s]: %d trades", name, tag, len(trades))
        return trades, summarize(f"{name}[{tag}]", trades)

    def _slice_bars(all_b: dict[str, list[Bar]], frac_start: float, frac_end: float) -> dict[str, list[Bar]]:
        out = {}
        for sym, bars in all_b.items():
            n = len(bars)
            i0 = int(n * frac_start)
            i1 = int(n * frac_end)
            # важно: оставляем начало для warm-up (LIVE_WINDOW_BARS=1440)
            # если i0 > 0, всё равно отдаём сырой диапазон — backtest сам скипнет первые LIVE_WINDOW_BARS
            out[sym] = bars[max(0, i0 - LIVE_WINDOW_BARS):i1]
        return out

    all_trades: list[Trade] = []
    reports = []

    if args.split == "none":
        splits = [(0.0, 1.0, "full")]
    elif args.split == "half":
        splits = [(0.0, 0.5, "H1"), (0.5, 1.0, "H2")]
    else:
        splits = [(0.0, 1/3, "T1"), (1/3, 2/3, "T2"), (2/3, 1.0, "T3")]

    for key in [s.strip() for s in args.candidates.split(",") if s.strip()]:
        if key not in CANDIDATES:
            log.warning("Неизвестный кандидат: %s", key)
            continue
        name, fn = CANDIDATES[key]
        for frac_start, frac_end, tag in splits:
            bars_subset = _slice_bars(all_bars, frac_start, frac_end) if args.split != "none" else all_bars
            trades, rep = _run(name, fn, bars_subset, tag)
            for t in trades:
                t.strategy = f"{name}[{tag}]"
            all_trades.extend(trades)
            reports.append(rep)

    print_report(reports)
    write_trades_csv(all_trades, out_dir / f"backtest_fxpro_candidates_trades_{args.split}.csv")
    log.info("Saved → %s", out_dir / f"backtest_fxpro_candidates_trades_{args.split}.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
