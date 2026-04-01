"""Outsiders — extreme setups: RSI extreme, Bollinger 3σ, ATR spike, news proximity.

Аналог стратегии «Аутсайдеры» Polymarket-бота: вход на low-probability ситуациях.
Каждый сигнал порождает 4 paper exit-стратегии для сравнения.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime

from fx_pro_bot.analysis.signals import TrendDirection, _atr, _rsi, _sma
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.events.models import CalendarEvent
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.exits import create_paper_positions

log = logging.getLogger(__name__)

RSI_OVERSOLD = 15
RSI_OVERBOUGHT = 85
BB_SIGMA = 3.0
ATR_SPIKE_MULT = 2.5
NEWS_HOURS = 4.0


@dataclass(frozen=True, slots=True)
class OutsiderSignal:
    instrument: str
    direction: TrendDirection
    source: str
    detail: str
    atr: float


def detect_extreme_setups(
    symbols: tuple[str, ...],
    bars_map: dict[str, list[Bar]],
    events: tuple[CalendarEvent, ...] = (),
    now: datetime | None = None,
) -> list[OutsiderSignal]:
    """Сканировать инструменты на экстремальные ситуации."""
    signals: list[OutsiderSignal] = []

    for symbol in symbols:
        bars = bars_map.get(symbol, [])
        if len(bars) < 51:
            continue

        closes = [b.close for b in bars]
        atr = _atr(bars)
        if atr <= 0:
            continue

        sig = _check_rsi_extreme(symbol, closes, atr)
        if sig:
            signals.append(sig)

        sig = _check_bollinger_extreme(symbol, closes, atr)
        if sig:
            signals.append(sig)

        sig = _check_atr_spike(symbol, bars, atr)
        if sig:
            signals.append(sig)

        sig = _check_news_proximity(symbol, bars, events, atr, now)
        if sig:
            signals.append(sig)

    return signals


def _check_rsi_extreme(symbol: str, closes: list[float], atr: float) -> OutsiderSignal | None:
    rsi = _rsi(closes, 14)
    if rsi <= RSI_OVERSOLD:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.LONG,
            source="extreme_rsi",
            detail=f"RSI={rsi:.1f} (oversold < {RSI_OVERSOLD})",
            atr=atr,
        )
    if rsi >= RSI_OVERBOUGHT:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.SHORT,
            source="extreme_rsi",
            detail=f"RSI={rsi:.1f} (overbought > {RSI_OVERBOUGHT})",
            atr=atr,
        )
    return None


def _check_bollinger_extreme(
    symbol: str, closes: list[float], atr: float,
) -> OutsiderSignal | None:
    period = 20
    if len(closes) < period + 1:
        return None

    mid = _sma(closes, period)
    variance = sum((c - mid) ** 2 for c in closes[-period:]) / period
    std = math.sqrt(variance)
    if std == 0:
        return None

    upper = mid + BB_SIGMA * std
    lower = mid - BB_SIGMA * std
    cur = closes[-1]

    if cur <= lower:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.LONG,
            source="extreme_bb",
            detail=f"цена {cur:.5f} < BB lower {lower:.5f} ({BB_SIGMA}σ)",
            atr=atr,
        )
    if cur >= upper:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.SHORT,
            source="extreme_bb",
            detail=f"цена {cur:.5f} > BB upper {upper:.5f} ({BB_SIGMA}σ)",
            atr=atr,
        )
    return None


def _check_atr_spike(
    symbol: str, bars: list[Bar], atr: float,
) -> OutsiderSignal | None:
    if len(bars) < 2 or atr <= 0:
        return None

    session_bars = bars[-12:]
    session_high = max(b.high for b in session_bars)
    session_low = min(b.low for b in session_bars)
    session_range = session_high - session_low

    if session_range > ATR_SPIKE_MULT * atr:
        cur = bars[-1].close
        mid = (session_high + session_low) / 2
        if cur > mid:
            direction = TrendDirection.SHORT
        else:
            direction = TrendDirection.LONG

        return OutsiderSignal(
            instrument=symbol,
            direction=direction,
            source="atr_spike",
            detail=f"range {session_range:.5f} > {ATR_SPIKE_MULT}x ATR ({atr:.5f})",
            atr=atr,
        )
    return None


def _check_news_proximity(
    symbol: str,
    bars: list[Bar],
    events: tuple[CalendarEvent, ...],
    atr: float,
    now: datetime | None = None,
) -> OutsiderSignal | None:
    if not events:
        return None

    ts = now or (bars[-1].ts if bars else None)
    if ts is None:
        return None

    for ev in events:
        if ev.importance != "high":
            continue
        diff_hours = abs((ev.at - ts).total_seconds()) / 3600
        if diff_hours <= NEWS_HOURS:
            closes = [b.close for b in bars]
            rsi = _rsi(closes, 14)
            direction = TrendDirection.LONG if rsi < 50 else TrendDirection.SHORT

            return OutsiderSignal(
                instrument=symbol,
                direction=direction,
                source="news",
                detail=f"событие '{ev.title}' через {diff_hours:.1f}ч, RSI={rsi:.1f}",
                atr=atr,
            )
    return None


class OutsidersStrategy:
    """Стратегия Аутсайдеры: детектировать extreme setups, создать позиции + 4 paper."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 50,
        max_per_instrument: int = 3,
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._max_per_instrument = max_per_instrument

    def process_signals(
        self,
        signals: list[OutsiderSignal],
        prices: dict[str, float],
    ) -> int:
        """Открыть позиции + 4 paper для каждого extreme-сигнала."""
        opened = 0
        current_total = self._store.count_open_positions(strategy="outsiders")

        for sig in signals:
            if current_total >= self._max_positions:
                break

            instr_count = self._store.count_open_positions(
                strategy="outsiders", instrument=sig.instrument,
            )
            if instr_count >= self._max_per_instrument:
                continue

            price = prices.get(sig.instrument)
            if price is None or price <= 0:
                continue

            atr = sig.atr if sig.atr > 0 else price * 0.005
            ps = pip_size(sig.instrument)

            if sig.direction == TrendDirection.LONG:
                sl = price - 3.0 * atr
            else:
                sl = price + 3.0 * atr

            pid = self._store.open_position(
                strategy="outsiders",
                source=sig.source,
                instrument=sig.instrument,
                direction=sig.direction.value,
                entry_price=price,
                stop_loss_price=sl,
            )

            create_paper_positions(self._store, pid, price, sig.direction, atr, ps)

            log.info(
                "  OUTSIDERS OPEN: %s %s @ %.5f (%s) + 4 paper",
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.detail,
            )
            opened += 1
            current_total += 1

        return opened
