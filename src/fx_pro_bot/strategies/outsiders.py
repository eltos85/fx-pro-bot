"""Outsiders — extreme setups: RSI extreme, Bollinger 3σ, ATR spike, news proximity.

Аналог стратегии «Аутсайдеры» Polymarket-бота: вход на low-probability ситуациях.
Каждый сигнал порождает 4 paper exit-стратегии для сравнения.

Два режима:
- classic: немедленный вход при обнаружении экстрима (текущее поведение)
- confirmed: вход после подтверждения разворота + фильтр ликвидных сессий
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, time, timezone

from fx_pro_bot.analysis.signals import TrendDirection, _atr, _rsi, _sma
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.events.models import CalendarEvent
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.cost_model import estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.exits import create_paper_positions

log = logging.getLogger(__name__)

RSI_OVERSOLD = 10
RSI_OVERBOUGHT = 90
BB_SIGMA = 3.0
ATR_SPIKE_MULT = 4.0
NEWS_HOURS = 4.0

CONFIRMED_RSI_RECOVERY = 5
CONFIRMED_SL_ATR = 1.5
CLASSIC_SL_ATR = 3.0

OUTSIDERS_EXCLUDE_SYMBOLS: frozenset[str] = frozenset({"GC=F", "EURJPY=X"})

LONDON_START = time(7, 0)
LONDON_END = time(16, 0)
NY_START = time(12, 0)
NY_END = time(21, 0)


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
    *,
    mode: str = "classic",
) -> list[OutsiderSignal]:
    """Сканировать инструменты на экстремальные ситуации.

    mode="classic": вход при текущем экстриме (bars[-1]).
    mode="confirmed": вход когда bars[-2] был экстримом, а bars[-1] показывает разворот.
    """
    signals: list[OutsiderSignal] = []

    for symbol in symbols:
        bars = bars_map.get(symbol, [])
        if len(bars) < 52:
            continue

        closes = [b.close for b in bars]
        atr = _atr(bars)
        if atr <= 0:
            continue

        if mode == "confirmed":
            if not _is_liquid_session(bars[-1]):
                continue
            _scan_confirmed(symbol, bars, closes, atr, events, now, signals)
        else:
            _scan_classic(symbol, bars, closes, atr, events, now, signals)

    return signals


def _is_liquid_session(bar: Bar) -> bool:
    """Проверить, что бар попадает в ликвидную торговую сессию (London / NY)."""
    ts = bar.ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    t = ts.time()
    if ts.weekday() >= 5:
        return False
    return LONDON_START <= t <= LONDON_END or NY_START <= t <= NY_END


def _scan_classic(
    symbol: str,
    bars: list[Bar],
    closes: list[float],
    atr: float,
    events: tuple[CalendarEvent, ...],
    now: datetime | None,
    out: list[OutsiderSignal],
) -> None:
    sig = _check_rsi_extreme(symbol, closes, atr)
    if sig:
        out.append(sig)

    sig = _check_bollinger_extreme(symbol, closes, atr)
    if sig:
        out.append(sig)

    sig = _check_atr_spike(symbol, bars, atr)
    if sig:
        out.append(sig)

    sig = _check_news_proximity(symbol, bars, events, atr, now)
    if sig:
        out.append(sig)


def _scan_confirmed(
    symbol: str,
    bars: list[Bar],
    closes: list[float],
    atr: float,
    events: tuple[CalendarEvent, ...],
    now: datetime | None,
    out: list[OutsiderSignal],
) -> None:
    """Confirmed-режим: экстрим на bars[-2], разворот на bars[-1]."""
    prev_closes = closes[:-1]
    cur_close = closes[-1]

    sig = _check_rsi_confirmed(symbol, prev_closes, cur_close, closes, atr)
    if sig:
        out.append(sig)

    sig = _check_bb_confirmed(symbol, prev_closes, cur_close, atr)
    if sig:
        out.append(sig)

    sig = _check_atr_spike_confirmed(symbol, bars, atr)
    if sig:
        out.append(sig)

    sig = _check_news_confirmed(symbol, bars, events, atr, now)
    if sig:
        out.append(sig)


# ── Classic checks (unchanged) ─────────────────────────────────


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


# ── Confirmed checks ───────────────────────────────────────────


def _check_rsi_confirmed(
    symbol: str,
    prev_closes: list[float],
    cur_close: float,
    all_closes: list[float],
    atr: float,
) -> OutsiderSignal | None:
    """RSI был экстремальным на предыдущем баре, сейчас отскочил."""
    if len(prev_closes) < 15:
        return None

    prev_rsi = _rsi(prev_closes, 14)
    cur_rsi = _rsi(all_closes, 14)

    if prev_rsi <= RSI_OVERSOLD and cur_rsi > RSI_OVERSOLD + CONFIRMED_RSI_RECOVERY:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.LONG,
            source="extreme_rsi",
            detail=f"confirmed RSI recovery {prev_rsi:.1f}→{cur_rsi:.1f}",
            atr=atr,
        )
    if prev_rsi >= RSI_OVERBOUGHT and cur_rsi < RSI_OVERBOUGHT - CONFIRMED_RSI_RECOVERY:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.SHORT,
            source="extreme_rsi",
            detail=f"confirmed RSI reversal {prev_rsi:.1f}→{cur_rsi:.1f}",
            atr=atr,
        )
    return None


def _check_bb_confirmed(
    symbol: str,
    prev_closes: list[float],
    cur_close: float,
    atr: float,
) -> OutsiderSignal | None:
    """Цена была за BB 3σ на предыдущем баре, вернулась внутрь на текущем."""
    period = 20
    if len(prev_closes) < period + 1:
        return None

    mid = _sma(prev_closes, period)
    variance = sum((c - mid) ** 2 for c in prev_closes[-period:]) / period
    std = math.sqrt(variance)
    if std == 0:
        return None

    upper = mid + BB_SIGMA * std
    lower = mid - BB_SIGMA * std
    prev_close = prev_closes[-1]

    if prev_close <= lower and cur_close > lower:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.LONG,
            source="extreme_bb",
            detail=f"confirmed BB recovery: {prev_close:.5f}→{cur_close:.5f} (lower={lower:.5f})",
            atr=atr,
        )
    if prev_close >= upper and cur_close < upper:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.SHORT,
            source="extreme_bb",
            detail=f"confirmed BB reversal: {prev_close:.5f}→{cur_close:.5f} (upper={upper:.5f})",
            atr=atr,
        )
    return None


def _check_atr_spike_confirmed(
    symbol: str, bars: list[Bar], atr: float,
) -> OutsiderSignal | None:
    """ATR spike зафиксирован по bars[:-1], текущий бар ближе к середине."""
    if len(bars) < 14 or atr <= 0:
        return None

    prev_session = bars[-13:-1]
    session_high = max(b.high for b in prev_session)
    session_low = min(b.low for b in prev_session)
    session_range = session_high - session_low

    if session_range <= ATR_SPIKE_MULT * atr:
        return None

    mid = (session_high + session_low) / 2
    prev_close = bars[-2].close
    cur_close = bars[-1].close
    half_range = session_range / 2

    if prev_close < mid and abs(cur_close - mid) < 0.7 * half_range:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.LONG,
            source="atr_spike",
            detail=f"confirmed spike revert to mid (range={session_range:.5f} > {ATR_SPIKE_MULT}xATR)",
            atr=atr,
        )
    if prev_close > mid and abs(cur_close - mid) < 0.7 * half_range:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.SHORT,
            source="atr_spike",
            detail=f"confirmed spike revert to mid (range={session_range:.5f} > {ATR_SPIKE_MULT}xATR)",
            atr=atr,
        )
    return None


def _check_news_confirmed(
    symbol: str,
    bars: list[Bar],
    events: tuple[CalendarEvent, ...],
    atr: float,
    now: datetime | None = None,
) -> OutsiderSignal | None:
    """Новость прошла (0.5-4ч назад), видим разворот на текущем баре."""
    if not events or len(bars) < 3:
        return None

    ts = now or bars[-1].ts
    for ev in events:
        if ev.importance != "high":
            continue
        diff_sec = (ts - ev.at).total_seconds()
        if 1800 < diff_sec < NEWS_HOURS * 3600:
            move_prev = bars[-2].close - bars[-3].close
            move_cur = bars[-1].close - bars[-2].close
            if move_prev != 0 and (move_cur / abs(move_prev)) < -0.3:
                direction = TrendDirection.LONG if move_cur > 0 else TrendDirection.SHORT
                return OutsiderSignal(
                    instrument=symbol,
                    direction=direction,
                    source="news",
                    detail=f"confirmed post-news reversal ({ev.title}, {diff_sec/3600:.1f}ч назад)",
                    atr=atr,
                )
    return None


# ── Strategy ───────────────────────────────────────────────────


class OutsidersStrategy:
    """Стратегия Аутсайдеры: детектировать extreme setups, создать позиции + 4 paper."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 50,
        max_per_instrument: int = 3,
        mode: str = "classic",
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._max_per_instrument = max_per_instrument
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    def process_signals(
        self,
        signals: list[OutsiderSignal],
        prices: dict[str, float],
    ) -> int:
        """Открыть позиции + 4 paper для каждого extreme-сигнала."""
        opened = 0
        current_total = self._store.count_open_positions(strategy="outsiders")

        sl_mult = CONFIRMED_SL_ATR if self._mode == "confirmed" else CLASSIC_SL_ATR

        for sig in signals:
            if current_total >= self._max_positions:
                break

            if sig.instrument in OUTSIDERS_EXCLUDE_SYMBOLS:
                continue

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

            if self._mode == "confirmed":
                price = _limit_entry_price(price, sig.direction, sig.atr)

            if sig.direction == TrendDirection.LONG:
                sl = price - sl_mult * atr
            else:
                sl = price + sl_mult * atr

            pid = self._store.open_position(
                strategy="outsiders",
                source=sig.source,
                instrument=sig.instrument,
                direction=sig.direction.value,
                entry_price=price,
                stop_loss_price=sl,
            )

            cost = estimate_entry_cost(sig.instrument, sig.source, atr, ps)
            self._store.set_estimated_cost(pid, cost.round_trip_pips)

            create_paper_positions(self._store, pid, price, sig.direction, atr, ps)

            log.info(
                "  OUTSIDERS [%s] OPEN: %s %s @ %.5f (%s) + 4 paper, cost ~%.1f пипсов",
                self._mode.upper(),
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.detail, cost.round_trip_pips,
            )
            opened += 1
            current_total += 1

        return opened


def _limit_entry_price(market_price: float, direction: TrendDirection, atr: float) -> float:
    """Эмуляция лимитного ордера: вход на 30% ретрейсмента от текущей цены.

    В confirmed-режиме спред уже нормализовался, поэтому берём скромный
    ретрейсмент 0.3*ATR в направлении, благоприятном для входа.
    """
    offset = 0.3 * atr
    if direction == TrendDirection.LONG:
        return market_price - offset
    return market_price + offset
