#!/usr/bin/env python3
"""Backtest всех 6 скальпинг-стратегий на одной и той же M5-истории.

Цель: получить сопоставимую сравнительную таблицу WR/Expectancy/PF/Sharpe/
ProfitableDays% для выбора рабочих страт. Все стратегии проходят через ту же
bar-by-bar симуляцию, одну и ту же fee-модель (Bybit taker 0.055% × 2).

Источник истины по параметрам каждой страты — её же модуль в
`bybit_bot.strategies.scalping.*`. Здесь адаптеры ТОЛЬКО вызывают `scan()`,
не переопределяют логику.

Особенности:
- `scalp_vwap`: 1h HTF-slope агрегируется на лету из M5-баров; без 1h-данных
  HTF-фильтр мягкий (htf_slope=None → фильтр не срабатывает). Даёт ~то же
  количество сигналов, что и live.
- `scalp_leadlag`: требует BTCUSDT в bars_map (добавлен автоматически как
  reference; торговли по BTCUSDT нет).
- `scalp_statarb`: pair-simulator (не общий ATR-exit). Exit по |z| < Z_EXIT
  одновременно на обоих символах; gross_pct = sum per-leg.
- `scalp_funding`: ПРОПУЩЕНА — зависит от `client.get_tickers()` (live
  fundingRate) и имеет time-based exit, а не ATR. Требует отдельного data
  pipeline (history `/v5/market/funding/history`). Запустим отдельно.

Использование:
    python3 -m scripts.backtest_all --days 90
    python3 -m scripts.backtest_all --days 90 --strategies vwap,volume,orb
    python3 -m scripts.backtest_all --days 90 --no-session-filter

Изоляция от FxPro: модуль импортирует только `bybit_bot.*`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Callable, Protocol

from pybit.unified_trading import HTTP

from bybit_bot.analysis.signals import Direction, atr, ema
from bybit_bot.config.settings import DEFAULT_SYMBOLS
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.btc_leadlag import BtcLeadLagStrategy
from bybit_bot.strategies.scalping.indicators import (
    ema_slope,
    ols_hedge_ratio,
    rolling_z_score,
    spread_series,
)
from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
from bybit_bot.strategies.scalping.stat_arb_crypto import (
    DEFAULT_PAIRS,
    LOOKBACK as STATARB_LOOKBACK,
    MIN_CORRELATION,
    StatArbCryptoStrategy,
    Z_ENTRY,
    Z_EXIT,
    ZSCORE_WINDOW,
)
from bybit_bot.strategies.scalping.turtle_soup import TurtleSoupStrategy
from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy

log = logging.getLogger("backtest_all")

# ── Общие параметры ─────────────────────────────────────────────
KLINE_INTERVAL = "5"
KLINE_MAX_LIMIT = 1000
KLINE_BAR_SEC = 5 * 60
TAKER_FEE_PCT = 0.055
ROUND_TRIP_FEE_PCT = TAKER_FEE_PCT * 2 / 100  # 0.0011

SESSION_START_UTC = 7
SESSION_END_UTC = 22
REFERENCE_SYMBOL = "BTCUSDT"  # leadlag requires this

# Live-бот (main.py + market_data.feed.fetch_bars_batch) передаёт в
# strategy.scan() окно yfinance_period=5d × yfinance_interval=5m = 1440 баров.
# Для честного backtest'а передаём страте ТОТ ЖЕ rolling window (а не всю
# историю 25K баров) — иначе индикаторы считаются на другом объёме данных
# чем в live. Плюс это делает симуляцию O(n), а не O(n²).
LIVE_WINDOW_BARS = 1440

CACHE_DIR = Path("data/backtest_klines")


# ── Fetch klines (переиспользуется между скриптами) ─────────────

def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol}_5m.csv"


def _load_cache(symbol: str) -> list[Bar]:
    path = _cache_path(symbol)
    if not path.exists():
        return []
    bars: list[Bar] = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bars.append(Bar(
                    symbol=symbol,
                    ts=datetime.fromisoformat(row["ts"]).replace(tzinfo=UTC),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                ))
            except (ValueError, KeyError):
                continue
    bars.sort(key=lambda b: b.ts)
    return bars


def _save_cache(symbol: str, bars: list[Bar]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([
                b.ts.replace(tzinfo=None).isoformat(),
                b.open, b.high, b.low, b.close, b.volume,
            ])


def _fetch_klines_window(
    session: HTTP, symbol: str, start_ms: int, end_ms: int,
) -> list[Bar]:
    out: list[Bar] = []
    cursor_end = end_ms
    while cursor_end > start_ms:
        resp = session.get_kline(
            category="linear", symbol=symbol, interval=KLINE_INTERVAL,
            start=start_ms, end=cursor_end, limit=KLINE_MAX_LIMIT,
        )
        rows = resp.get("result", {}).get("list", []) or []
        if not rows:
            break
        for r in rows:
            ts_ms = int(r[0])
            if ts_ms < start_ms:
                continue
            out.append(Bar(
                symbol=symbol,
                ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]),
            ))
        oldest_ts_ms = int(rows[-1][0])
        if oldest_ts_ms >= cursor_end - KLINE_BAR_SEC * 1000:
            break
        cursor_end = oldest_ts_ms - 1
        time.sleep(0.1)
    out.sort(key=lambda b: b.ts)
    return out


def fetch_klines(symbol: str, days: int, *, refetch: bool = False) -> list[Bar]:
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    start_ms = now_ms - days * 86400 * 1000

    cached: list[Bar] = []
    if not refetch:
        cached = _load_cache(symbol)

    if cached:
        cached = [b for b in cached if b.ts.timestamp() * 1000 >= start_ms]
        if cached and (cached[0].ts.timestamp() * 1000 - start_ms) < 86400 * 1000:
            last_ts_ms = int(cached[-1].ts.timestamp() * 1000)
            if now_ms - last_ts_ms > KLINE_BAR_SEC * 1000:
                session = HTTP()
                fresh = _fetch_klines_window(
                    session, symbol, last_ts_ms + 1, now_ms,
                )
                merged = {int(b.ts.timestamp() * 1000): b for b in cached}
                for b in fresh:
                    merged[int(b.ts.timestamp() * 1000)] = b
                bars = sorted(merged.values(), key=lambda b: b.ts)
                _save_cache(symbol, bars)
                log.info("%s: cache+refresh %d бар (+%d новых)",
                         symbol, len(bars), len(fresh))
                return bars
            log.info("%s: cache hit %d бар", symbol, len(cached))
            return cached

    session = HTTP()
    log.info("%s: fetch %d дней…", symbol, days)
    bars = _fetch_klines_window(session, symbol, start_ms, now_ms)
    _save_cache(symbol, bars)
    log.info("%s: %d бар", symbol, len(bars))
    return bars


# ── Агрегация M5 → 1h для VWAP HTF-slope ────────────────────────

def aggregate_to_1h(bars: list[Bar]) -> list[Bar]:
    """Собрать 1h-бары из M5. Границы по календарному часу UTC."""
    if not bars:
        return []
    out: list[Bar] = []
    cur_hour_ts: datetime | None = None
    o = h = l = c = 0.0
    v = 0.0
    for b in bars:
        hour_ts = b.ts.replace(minute=0, second=0, microsecond=0)
        if cur_hour_ts is None or hour_ts != cur_hour_ts:
            if cur_hour_ts is not None:
                out.append(Bar(
                    symbol=b.symbol, ts=cur_hour_ts,
                    open=o, high=h, low=l, close=c, volume=v,
                ))
            cur_hour_ts = hour_ts
            o = b.open
            h = b.high
            l = b.low
            c = b.close
            v = b.volume
        else:
            h = max(h, b.high)
            l = min(l, b.low)
            c = b.close
            v += b.volume
    if cur_hour_ts is not None:
        out.append(Bar(
            symbol=bars[-1].symbol, ts=cur_hour_ts,
            open=o, high=h, low=l, close=c, volume=v,
        ))
    return out


@dataclass(slots=True)
class HtfCache:
    """Pre-computed 1h EMA(50) slope per symbol per M5-bar index.

    Заранее считается для всей истории — O(n) вместо O(n²) пересчёта
    на каждом баре. Индекс M5-бара → slope: последнее известное 1h-EMA50
    slope доступное на момент ЗАКРЫТИЯ этого M5-бара.
    """

    slopes_by_symbol: dict[str, list[float | None]] = field(default_factory=dict)

    def get(self, symbol: str, m5_index: int) -> float | None:
        series = self.slopes_by_symbol.get(symbol)
        if series is None or m5_index >= len(series):
            return None
        return series[m5_index]


def precompute_htf_slopes(bars_map: dict[str, list[Bar]]) -> HtfCache:
    """Один раз собрать 1h, посчитать EMA(50) и slope, спроецировать на
    индексы M5-баров."""
    cache = HtfCache()
    for sym, bars in bars_map.items():
        if not bars:
            continue
        m5_slopes: list[float | None] = [None] * len(bars)
        h1: list[Bar] = []
        cur_hour_ts: datetime | None = None
        o = h = l = c = 0.0
        v = 0.0
        h1_closes: list[float] = []
        # EMA state
        k = 2.0 / (50 + 1)
        ema_val: float | None = None
        ema_history: list[float] = []

        def _finalize_h1(hour_ts: datetime, o_, h_, l_, c_, v_):
            nonlocal ema_val
            h1.append(Bar(symbol=sym, ts=hour_ts, open=o_, high=h_, low=l_,
                          close=c_, volume=v_))
            h1_closes.append(c_)
            if len(h1_closes) == 50:
                ema_val = sum(h1_closes) / 50.0
                ema_history.append(ema_val)
            elif len(h1_closes) > 50:
                assert ema_val is not None
                ema_val = c_ * k + ema_val * (1 - k)
                ema_history.append(ema_val)

        def _current_slope() -> float | None:
            if len(ema_history) < 6:
                return None
            return (ema_history[-1] - ema_history[-6]) / 5.0

        for idx, b in enumerate(bars):
            hour_ts = b.ts.replace(minute=0, second=0, microsecond=0)
            if cur_hour_ts is None:
                cur_hour_ts = hour_ts
                o = b.open; h = b.high; l = b.low; c = b.close; v = b.volume
            elif hour_ts != cur_hour_ts:
                _finalize_h1(cur_hour_ts, o, h, l, c, v)
                cur_hour_ts = hour_ts
                o = b.open; h = b.high; l = b.low; c = b.close; v = b.volume
            else:
                h = max(h, b.high)
                l = min(l, b.low)
                c = b.close
                v += b.volume
            # slope доступный НА ЗАКРЫТИИ этого M5-бара =
            # slope по предыдущим уже завершённым 1h-барам
            m5_slopes[idx] = _current_slope()
        cache.slopes_by_symbol[sym] = m5_slopes
    return cache


# ── Strategy Adapters ───────────────────────────────────────────

@dataclass(slots=True)
class SimSignal:
    symbol: str
    direction: str        # "long" / "short"
    entry_price: float
    atr_value: float
    sl_atr_mult: float
    tp_atr_mult: float
    pair_tag: str = ""
    extra: dict = field(default_factory=dict)


class SimAdapter(Protocol):
    name: str
    min_bars: int
    requires_reference: bool

    def scan_at(
        self, bars_map: dict[str, list[Bar]], i: int,
    ) -> list[SimSignal]: ...


class VwapAdapter:
    """scalp_vwap: VWAP mean-reversion, SL=2.0 ATR, TP=1.5 ATR (main.py:889)."""

    name = "scalp_vwap"
    min_bars = 60
    requires_reference = False

    def __init__(self, *, htf_cache: HtfCache | None = None) -> None:
        self._strategy = VwapCryptoStrategy()
        self._htf = htf_cache

    def scan_at(self, bars_map, i):
        cur_map: dict[str, list[Bar]] = {}
        lo = max(0, i + 1 - LIVE_WINDOW_BARS)
        for sym, bars in bars_map.items():
            if i >= len(bars) or sym == REFERENCE_SYMBOL:
                continue
            cur_map[sym] = bars[lo : i + 1]

        if self._htf is not None:
            slopes: dict[str, float] = {}
            for sym in cur_map:
                s = self._htf.get(sym, i)
                if s is not None:
                    slopes[sym] = s
            self._strategy.set_htf_slopes(slopes)
        else:
            self._strategy.set_htf_slopes({})

        sigs = self._strategy.scan(cur_map)
        out: list[SimSignal] = []
        for s in sigs:
            out.append(SimSignal(
                symbol=s.symbol,
                direction=s.direction.value,
                entry_price=s.entry_price,
                atr_value=s.atr_value,
                sl_atr_mult=2.0,
                tp_atr_mult=1.5,
                extra={"deviation_atr": s.deviation_atr, "rsi": s.rsi},
            ))
        return out


class VolumeAdapter:
    """scalp_volume: volume spike 2×, SL=2.0 ATR, TP=2.0 ATR (main.py:932)."""

    name = "scalp_volume"
    min_bars = 30
    requires_reference = False

    def __init__(self) -> None:
        self._strategy = VolumeSpikeStrategy(max_signals_per_scan=3)

    def scan_at(self, bars_map, i):
        lo = max(0, i + 1 - LIVE_WINDOW_BARS)
        cur_map = {
            sym: bars[lo : i + 1] for sym, bars in bars_map.items()
            if i < len(bars) and sym != REFERENCE_SYMBOL
        }
        sigs = self._strategy.scan(cur_map)
        out: list[SimSignal] = []
        for s in sigs:
            out.append(SimSignal(
                symbol=s.symbol,
                direction=s.direction.value,
                entry_price=s.entry_price,
                atr_value=s.atr_value,
                sl_atr_mult=2.0,
                tp_atr_mult=2.0,
                extra={"vol_ratio": s.volume_ratio},
            ))
        return out


class OrbAdapter:
    """scalp_orb: 15-мин коробка сессий, SL=2.0 ATR, TP = 2 × box_range / ATR."""

    name = "scalp_orb"
    min_bars = 100
    requires_reference = False

    def __init__(self) -> None:
        self._strategy = SessionOrbStrategy(max_signals_per_scan=3)

    def scan_at(self, bars_map, i):
        lo = max(0, i + 1 - LIVE_WINDOW_BARS)
        cur_map = {
            sym: bars[lo : i + 1] for sym, bars in bars_map.items()
            if i < len(bars) and sym != REFERENCE_SYMBOL
        }
        sigs = self._strategy.scan(cur_map)
        out: list[SimSignal] = []
        for s in sigs:
            tp_mult = (s.box_range * 2.0) / s.atr_value if s.atr_value > 0 else 2.0
            out.append(SimSignal(
                symbol=s.symbol,
                direction=s.direction.value,
                entry_price=s.breakout_price,
                atr_value=s.atr_value,
                sl_atr_mult=2.0,
                tp_atr_mult=tp_mult,
                extra={"session": s.session, "vol_ratio": s.volume_ratio},
            ))
        return out


class TurtleAdapter:
    """scalp_turtle: fade 20-bar breakout, SL=1.5 ATR, TP=2.5 ATR."""

    name = "scalp_turtle"
    min_bars = 80
    requires_reference = False

    def __init__(self) -> None:
        self._strategy = TurtleSoupStrategy(max_signals_per_scan=10)

    def scan_at(self, bars_map, i):
        out: list[SimSignal] = []
        lo = max(0, i + 1 - LIVE_WINDOW_BARS)
        for sym, bars in bars_map.items():
            if sym == REFERENCE_SYMBOL or i >= len(bars):
                continue
            sig = self._strategy._scan_symbol(sym, bars[lo : i + 1])
            if sig is None:
                continue
            out.append(SimSignal(
                symbol=sym,
                direction=sig.direction.value,
                entry_price=bars[i].close,
                atr_value=sig.atr_value,
                sl_atr_mult=1.5,
                tp_atr_mult=2.5,
                extra={"rsi": sig.rsi_at_break, "depth": sig.break_depth_atr},
            ))
        return out


class LeadlagAdapter:
    """scalp_leadlag: BTC impulse → alt lag, SL=1.5 ATR, TP=2.0 ATR."""

    name = "scalp_leadlag"
    min_bars = 60
    requires_reference = True

    def __init__(self) -> None:
        self._strategy = BtcLeadLagStrategy(max_signals_per_scan=3)

    def scan_at(self, bars_map, i):
        lo = max(0, i + 1 - LIVE_WINDOW_BARS)
        cur_map = {
            sym: bars[lo : i + 1] for sym, bars in bars_map.items()
            if i < len(bars)
        }
        sigs = self._strategy.scan(cur_map)
        out: list[SimSignal] = []
        for s in sigs:
            bars = bars_map[s.symbol]
            out.append(SimSignal(
                symbol=s.symbol,
                direction=s.direction.value,
                entry_price=bars[i].close,
                atr_value=s.atr_value,
                sl_atr_mult=1.5,
                tp_atr_mult=2.0,
                extra={"btc_move_pct": s.btc_move_pct, "corr": s.correlation},
            ))
        return out


# ── Generic ATR-exit simulator ──────────────────────────────────

@dataclass(slots=True)
class SimTrade:
    strategy: str
    symbol: str
    direction: str
    entry_ts: datetime
    entry_price: float
    atr_at_entry: float
    sl_price: float
    tp_price: float
    entry_idx: int
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    gross_pct: float = 0.0
    net_pct: float = 0.0
    hold_bars: int = 0
    pair_tag: str = ""


def _in_session(ts: datetime, enabled: bool) -> bool:
    if not enabled:
        return True
    return SESSION_START_UTC <= ts.hour < SESSION_END_UTC


def _try_exit_atr(
    bar: Bar, tr: SimTrade,
) -> tuple[float, str] | None:
    """Проверить SL/TP на этом баре. Возвращает (price, reason) или None."""
    sl_hit = False
    tp_hit = False
    if tr.direction == "long":
        if bar.low <= tr.sl_price:
            sl_hit = True
        if bar.high >= tr.tp_price:
            tp_hit = True
    else:
        if bar.high >= tr.sl_price:
            sl_hit = True
        if bar.low <= tr.tp_price:
            tp_hit = True

    if sl_hit and tp_hit:
        return (tr.sl_price, "sl_tp_same_bar")
    if sl_hit:
        return (tr.sl_price, "sl")
    if tp_hit:
        return (tr.tp_price, "tp")
    return None


def simulate_atr_exit(
    bars_map: dict[str, list[Bar]],
    adapter: SimAdapter,
    *,
    session_filter: bool,
    max_hold_bars: int,
) -> list[SimTrade]:
    """Симуляция страт с классическим ATR SL/TP exit (vwap, volume, orb,
    turtle, leadlag).

    Проход идёт по индексу i общего времени. Для каждой пары (i, sym) —
    проверяем exit открытой позиции, затем вызываем adapter.scan_at(i).
    Одна открытая позиция на символ максимум (как в live).
    """
    all_symbols = sorted(bars_map.keys())
    if not all_symbols:
        return []
    n_bars = min(len(bars_map[s]) for s in all_symbols)
    trades: list[SimTrade] = []
    open_by_symbol: dict[str, SimTrade] = {}

    start_i = adapter.min_bars
    for i in range(start_i, n_bars):
        for sym in all_symbols:
            if sym == REFERENCE_SYMBOL and not _symbol_is_tradable(sym, adapter):
                continue
            tr = open_by_symbol.get(sym)
            if tr is None:
                continue
            bar = bars_map[sym][i]
            result = _try_exit_atr(bar, tr)
            hold = i - tr.entry_idx
            if result is None and hold >= max_hold_bars:
                result = (bar.close, "time_stop")
            if result is not None:
                exit_price, reason = result
                tr.exit_ts = bar.ts
                tr.exit_price = exit_price
                tr.exit_reason = reason
                tr.hold_bars = hold
                if tr.direction == "long":
                    gross = (exit_price / tr.entry_price) - 1.0
                else:
                    gross = (tr.entry_price / exit_price) - 1.0
                tr.gross_pct = gross
                tr.net_pct = gross - ROUND_TRIP_FEE_PCT
                trades.append(tr)
                open_by_symbol.pop(sym, None)

        cur_bar = bars_map[all_symbols[0]][i]
        if not _in_session(cur_bar.ts, session_filter):
            continue

        sigs = adapter.scan_at(bars_map, i)
        for s in sigs:
            if s.symbol in open_by_symbol:
                continue
            if s.atr_value <= 0:
                continue
            if s.direction == "long":
                sl = s.entry_price - s.sl_atr_mult * s.atr_value
                tp = s.entry_price + s.tp_atr_mult * s.atr_value
            else:
                sl = s.entry_price + s.sl_atr_mult * s.atr_value
                tp = s.entry_price - s.tp_atr_mult * s.atr_value
            tr = SimTrade(
                strategy=adapter.name,
                symbol=s.symbol,
                direction=s.direction,
                entry_ts=bars_map[s.symbol][i].ts,
                entry_price=s.entry_price,
                atr_at_entry=s.atr_value,
                sl_price=sl,
                tp_price=tp,
                entry_idx=i,
            )
            open_by_symbol[s.symbol] = tr

    return trades


def _symbol_is_tradable(symbol: str, adapter: SimAdapter) -> bool:
    """leadlag ничего не открывает по BTCUSDT, но BTCUSDT нужен в bars_map
    как reference — не чистим openBy для него."""
    return symbol != REFERENCE_SYMBOL


# ── Stat-Arb Pair Simulator ─────────────────────────────────────

@dataclass(slots=True)
class StatArbOpenPair:
    pair_tag: str
    sym_a: str
    sym_b: str
    dir_a: str
    dir_b: str
    entry_price_a: float
    entry_price_b: float
    entry_idx: int
    entry_ts: datetime


def simulate_statarb(
    bars_map: dict[str, list[Bar]],
    *,
    session_filter: bool,
    max_hold_bars: int,
) -> list[SimTrade]:
    """Pair-симулятор для scalp_statarb.

    Использует `StatArbCryptoStrategy.scan()` для входа и `.check_exits()` для
    выхода. Exit: |z| < Z_EXIT или time-stop. Pair-TP / emergency-stop
    отключены — их логика сейчас ведётся в main.py через executor, а не в
    самой страте. Для первой оценки достаточно z-reversion + time.

    Каждая пара генерит ДВЕ записи SimTrade (leg A + leg B) с общим pair_tag.
    """
    strategy = StatArbCryptoStrategy()
    all_symbols = sorted(bars_map.keys())
    if not all_symbols:
        return []
    n_bars = min(len(bars_map[s]) for s in all_symbols)

    open_pairs: dict[str, StatArbOpenPair] = {}
    trades: list[SimTrade] = []

    min_bars = STATARB_LOOKBACK + ZSCORE_WINDOW + 10

    for i in range(min_bars, n_bars):
        lo = max(0, i + 1 - LIVE_WINDOW_BARS)
        cur_map = {
            sym: bars[lo : i + 1] for sym, bars in bars_map.items()
        }

        to_close: list[str] = []
        for tag, op in open_pairs.items():
            bars_a = cur_map[op.sym_a]
            bars_b = cur_map[op.sym_b]
            closes_a = [b.close for b in bars_a]
            closes_b = [b.close for b in bars_b]
            beta = ols_hedge_ratio(
                closes_a[-STATARB_LOOKBACK:], closes_b[-STATARB_LOOKBACK:],
            )
            sprd = spread_series(closes_a, closes_b, beta)
            z = rolling_z_score(sprd, ZSCORE_WINDOW)
            hold = i - op.entry_idx

            exit_reason = ""
            if abs(z) < Z_EXIT:
                exit_reason = "zscore"
            elif hold >= max_hold_bars:
                exit_reason = "time_stop"

            if exit_reason:
                cur_a = bars_a[-1]
                cur_b = bars_b[-1]
                if op.dir_a == "long":
                    gross_a = (cur_a.close / op.entry_price_a) - 1.0
                else:
                    gross_a = (op.entry_price_a / cur_a.close) - 1.0
                if op.dir_b == "long":
                    gross_b = (cur_b.close / op.entry_price_b) - 1.0
                else:
                    gross_b = (op.entry_price_b / cur_b.close) - 1.0

                trades.append(SimTrade(
                    strategy="scalp_statarb",
                    symbol=op.sym_a, direction=op.dir_a,
                    entry_ts=op.entry_ts, entry_price=op.entry_price_a,
                    atr_at_entry=0.0, sl_price=0.0, tp_price=0.0,
                    entry_idx=op.entry_idx,
                    exit_ts=cur_a.ts, exit_price=cur_a.close,
                    exit_reason=exit_reason,
                    gross_pct=gross_a,
                    net_pct=gross_a - ROUND_TRIP_FEE_PCT,
                    hold_bars=hold, pair_tag=tag,
                ))
                trades.append(SimTrade(
                    strategy="scalp_statarb",
                    symbol=op.sym_b, direction=op.dir_b,
                    entry_ts=op.entry_ts, entry_price=op.entry_price_b,
                    atr_at_entry=0.0, sl_price=0.0, tp_price=0.0,
                    entry_idx=op.entry_idx,
                    exit_ts=cur_b.ts, exit_price=cur_b.close,
                    exit_reason=exit_reason,
                    gross_pct=gross_b,
                    net_pct=gross_b - ROUND_TRIP_FEE_PCT,
                    hold_bars=hold, pair_tag=tag,
                ))
                to_close.append(tag)
        for tag in to_close:
            open_pairs.pop(tag, None)

        if not _in_session(cur_map[all_symbols[0]][-1].ts, session_filter):
            continue

        sigs = strategy.scan(cur_map)
        for sig in sigs:
            pair_key = f"{sig.symbol_a}|{sig.symbol_b}"
            if any(f"{op.sym_a}|{op.sym_b}" == pair_key for op in open_pairs.values()):
                continue
            cur_a = cur_map[sig.symbol_a][-1]
            cur_b = cur_map[sig.symbol_b][-1]
            tag = f"{i}_{sig.symbol_a}_{sig.symbol_b}"
            open_pairs[tag] = StatArbOpenPair(
                pair_tag=tag, sym_a=sig.symbol_a, sym_b=sig.symbol_b,
                dir_a=sig.direction_a.value, dir_b=sig.direction_b.value,
                entry_price_a=cur_a.close, entry_price_b=cur_b.close,
                entry_idx=i, entry_ts=cur_a.ts,
            )

    return trades


# ── Metrics ─────────────────────────────────────────────────────

@dataclass(slots=True)
class Metrics:
    n: int = 0
    wins: int = 0
    losses: int = 0
    win_pct_sum: float = 0.0
    loss_pct_sum: float = 0.0
    gross_pct_sum: float = 0.0
    net_pct_sum: float = 0.0
    max_dd_pct: float = 0.0
    reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    profitable_days: int = 0
    losing_days: int = 0
    flat_days: int = 0
    daily_pnl: dict[date, float] = field(default_factory=dict)

    @property
    def wr(self) -> float:
        return 100.0 * self.wins / self.n if self.n else 0.0

    @property
    def avg_win_pct(self) -> float:
        return 100.0 * self.win_pct_sum / self.wins if self.wins else 0.0

    @property
    def avg_loss_pct(self) -> float:
        return 100.0 * self.loss_pct_sum / self.losses if self.losses else 0.0

    @property
    def profit_factor(self) -> float:
        if self.loss_pct_sum >= 0:
            return 0.0
        return -self.win_pct_sum / self.loss_pct_sum

    @property
    def expectancy_pct(self) -> float:
        return 100.0 * self.net_pct_sum / self.n if self.n else 0.0

    @property
    def profitable_days_pct(self) -> float:
        total = self.profitable_days + self.losing_days + self.flat_days
        return 100.0 * self.profitable_days / total if total else 0.0


def compute_metrics(trades: list[SimTrade]) -> Metrics:
    m = Metrics()
    trades_sorted = sorted(trades, key=lambda t: t.exit_ts or t.entry_ts)
    equity = 0.0
    peak = 0.0
    for t in trades_sorted:
        m.n += 1
        m.gross_pct_sum += t.gross_pct
        m.net_pct_sum += t.net_pct
        if t.net_pct > 0.0001:
            m.wins += 1
            m.win_pct_sum += t.net_pct
        elif t.net_pct < -0.0001:
            m.losses += 1
            m.loss_pct_sum += t.net_pct
        m.reasons[t.exit_reason] += 1
        equity += t.net_pct
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > m.max_dd_pct:
            m.max_dd_pct = dd
        exit_date = (t.exit_ts or t.entry_ts).date()
        m.daily_pnl[exit_date] = m.daily_pnl.get(exit_date, 0.0) + t.net_pct

    for _, pnl in m.daily_pnl.items():
        if pnl > 0.0001:
            m.profitable_days += 1
        elif pnl < -0.0001:
            m.losing_days += 1
        else:
            m.flat_days += 1

    return m


def sharpe_annualized(trades: list[SimTrade], *, bars_per_year: int = 105120) -> float:
    if len(trades) < 2:
        return 0.0
    rets = [t.net_pct for t in trades]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    avg_bars = sum(t.hold_bars for t in trades) / max(len(trades), 1)
    if avg_bars <= 0:
        return 0.0
    trades_per_year = bars_per_year / avg_bars
    return (mean / std) * math.sqrt(trades_per_year)


# ── Report ──────────────────────────────────────────────────────

STRAT_ADAPTERS: dict[str, Callable[[HtfCache | None], SimAdapter]] = {
    "vwap": lambda htf: VwapAdapter(htf_cache=htf),
    "volume": lambda htf: VolumeAdapter(),
    "orb": lambda htf: OrbAdapter(),
    "turtle": lambda htf: TurtleAdapter(),
    "leadlag": lambda htf: LeadlagAdapter(),
}


def format_summary(results: dict[str, list[SimTrade]], days: int) -> str:
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append(f"BACKTEST ALL — {days} дней, 8 символов, fee round-trip 0.11%")
    lines.append("=" * 100)
    lines.append(f"{'strategy':14s} {'n':>5s} {'WR%':>6s} {'avgW%':>7s} "
                 f"{'avgL%':>7s} {'PF':>5s} {'exp%':>7s} {'Sharpe':>7s} "
                 f"{'PnL%':>8s} {'MaxDD%':>7s} {'Days+%':>7s}")
    lines.append("-" * 100)
    for name, trades in results.items():
        m = compute_metrics(trades)
        sh = sharpe_annualized(trades)
        lines.append(
            f"{name:14s} {m.n:5d} {m.wr:6.2f} {m.avg_win_pct:7.3f} "
            f"{m.avg_loss_pct:7.3f} {m.profit_factor:5.2f} "
            f"{m.expectancy_pct:7.3f} {sh:7.2f} "
            f"{m.net_pct_sum*100:8.2f} {m.max_dd_pct*100:7.2f} "
            f"{m.profitable_days_pct:7.2f}"
        )
    lines.append("")
    return "\n".join(lines)


def format_per_strategy_detail(name: str, trades: list[SimTrade]) -> str:
    m = compute_metrics(trades)
    lines: list[str] = []
    lines.append("")
    lines.append(f"── {name} detail ───────────────────────────────────")
    lines.append(f"n={m.n}  WR={m.wr:.2f}%  exp={m.expectancy_pct:+.3f}%  "
                 f"PnL={m.net_pct_sum*100:+.2f}%  MaxDD={m.max_dd_pct*100:.2f}%")
    lines.append(f"Profitable days: {m.profitable_days}/{m.profitable_days + m.losing_days + m.flat_days} "
                 f"({m.profitable_days_pct:.1f}%)")

    by_sym: dict[str, list[SimTrade]] = defaultdict(list)
    for t in trades:
        by_sym[t.symbol].append(t)
    if len(by_sym) > 1:
        lines.append("per symbol:")
        for sym in sorted(by_sym.keys()):
            sm = compute_metrics(by_sym[sym])
            lines.append(f"  {sym:10s} n={sm.n:4d} WR={sm.wr:5.2f}% "
                         f"exp={sm.expectancy_pct:+.3f}% PnL={sm.net_pct_sum*100:+.2f}%")

    lines.append("exits:")
    for r, c in sorted(m.reasons.items(), key=lambda x: -x[1]):
        lines.append(f"  {r:20s} {c}")

    if trades:
        longs = [t for t in trades if t.direction == "long"]
        shorts = [t for t in trades if t.direction == "short"]
        for lbl, tr in (("long", longs), ("short", shorts)):
            if not tr:
                continue
            sm = compute_metrics(tr)
            lines.append(f"  {lbl:6s} n={sm.n:4d} WR={sm.wr:5.2f}% "
                         f"exp={sm.expectancy_pct:+.3f}% PnL={sm.net_pct_sum*100:+.2f}%")

    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", type=str, default="")
    parser.add_argument("--strategies", type=str,
                        default="vwap,volume,orb,turtle,leadlag,statarb")
    parser.add_argument("--no-session-filter", action="store_true")
    parser.add_argument("--max-hold-bars", type=int, default=288)
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument("--output", type=str,
                        default="data/backtest_all_report.txt")
    parser.add_argument("--trades-csv", type=str,
                        default="data/backtest_all_trades.csv")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("bybit_bot.strategies").setLevel(logging.WARNING)

    if args.symbols:
        base_symbols = tuple(s.strip().upper() for s in args.symbols.split(","))
    else:
        base_symbols = DEFAULT_SYMBOLS
    symbols = tuple(dict.fromkeys((*base_symbols, REFERENCE_SYMBOL)))
    log.info("Символы: %s", ", ".join(symbols))

    strategies = [s.strip().lower() for s in args.strategies.split(",") if s.strip()]

    session_filter = not args.no_session_filter

    bars_map: dict[str, list[Bar]] = {}
    for sym in symbols:
        bars = fetch_klines(sym, args.days, refetch=args.refetch)
        if len(bars) < 200:
            log.warning("%s: мало баров (%d) — пропускаю", sym, len(bars))
            continue
        bars_map[sym] = bars

    log.info("Precompute HTF slopes (1h EMA50)…")
    t0 = time.time()
    htf_cache = precompute_htf_slopes(bars_map)
    log.info("HTF precompute: %.1fs, символов: %d",
             time.time() - t0, len(htf_cache.slopes_by_symbol))

    results: dict[str, list[SimTrade]] = {}
    all_trades: list[SimTrade] = []

    for strat in strategies:
        log.info("=== %s ===", strat)
        t0 = time.time()
        if strat == "statarb":
            trades = simulate_statarb(
                bars_map,
                session_filter=session_filter,
                max_hold_bars=args.max_hold_bars,
            )
        elif strat in STRAT_ADAPTERS:
            adapter = STRAT_ADAPTERS[strat](htf_cache)
            trades = simulate_atr_exit(
                bars_map, adapter,
                session_filter=session_filter,
                max_hold_bars=args.max_hold_bars,
            )
        else:
            log.warning("Неизвестная страта: %s — пропуск", strat)
            continue
        elapsed = time.time() - t0
        log.info("%s: %d сделок за %.1fs", strat, len(trades), elapsed)
        results[strat] = trades
        all_trades.extend(trades)

    summary = format_summary(results, args.days)
    print(summary)
    details: list[str] = [summary]
    for strat, trades in results.items():
        details.append(format_per_strategy_detail(strat, trades))

    full_report = "\n".join(details)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(full_report, encoding="utf-8")
    log.info("report → %s", out)

    trades_path = Path(args.trades_csv)
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    with trades_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "strategy", "symbol", "direction", "entry_ts", "entry_price",
            "atr", "sl", "tp", "exit_ts", "exit_price", "exit_reason",
            "gross_pct", "net_pct", "hold_bars", "pair_tag",
        ])
        for t in all_trades:
            writer.writerow([
                t.strategy, t.symbol, t.direction,
                t.entry_ts.isoformat(), f"{t.entry_price:.6f}",
                f"{t.atr_at_entry:.6f}",
                f"{t.sl_price:.6f}", f"{t.tp_price:.6f}",
                t.exit_ts.isoformat() if t.exit_ts else "",
                f"{t.exit_price:.6f}" if t.exit_price is not None else "",
                t.exit_reason, f"{t.gross_pct:.6f}", f"{t.net_pct:.6f}",
                t.hold_bars, t.pair_tag,
            ])
    log.info("trades → %s", trades_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
