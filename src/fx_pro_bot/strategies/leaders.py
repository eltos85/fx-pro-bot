"""Leaders — copy-trading: агрегация сигналов COT + Myfxbook sentiment + cTrader Copy.

Аналог стратегии «Лидеры» Polymarket-бота: вход когда 2+ источника согласны.
SL = 2 ATR, trailing = 0.7 ATR, exit by source reversal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fx_pro_bot.analysis.signals import TrendDirection, _atr
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.cost_model import estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.whales.cot import CotSignal
from fx_pro_bot.whales.sentiment import SentimentSignal

log = logging.getLogger(__name__)

MIN_SOURCES_AGREE = 2


@dataclass(frozen=True, slots=True)
class LeaderSignal:
    instrument: str
    direction: TrendDirection
    sources: tuple[str, ...]
    strength: float
    atr: float


def aggregate_leader_signals(
    cot_signals: list[CotSignal],
    sentiment_signals: list[SentimentSignal],
    bars_by_symbol: dict[str, list[Bar]],
) -> list[LeaderSignal]:
    """Агрегировать whale-источники: если 2+ согласны — сигнал."""
    votes: dict[str, list[tuple[str, TrendDirection]]] = {}

    for s in cot_signals:
        if s.direction != TrendDirection.FLAT:
            votes.setdefault(s.symbol, []).append(("cot", s.direction))

    for s in sentiment_signals:
        if s.direction != TrendDirection.FLAT:
            votes.setdefault(s.symbol, []).append(("sentiment", s.direction))

    signals: list[LeaderSignal] = []
    for symbol, vote_list in votes.items():
        long_sources = [src for src, d in vote_list if d == TrendDirection.LONG]
        short_sources = [src for src, d in vote_list if d == TrendDirection.SHORT]

        if len(long_sources) >= MIN_SOURCES_AGREE:
            direction = TrendDirection.LONG
            sources = tuple(long_sources)
        elif len(short_sources) >= MIN_SOURCES_AGREE:
            direction = TrendDirection.SHORT
            sources = tuple(short_sources)
        else:
            continue

        bars = bars_by_symbol.get(symbol, [])
        atr = _atr(bars) if len(bars) > 14 else 0.0
        strength = len(sources) / len(vote_list) if vote_list else 0.0

        signals.append(LeaderSignal(
            instrument=symbol,
            direction=direction,
            sources=sources,
            strength=round(strength, 2),
            atr=atr,
        ))

    return signals


class LeadersStrategy:
    """Стратегия Лидеры: open/manage positions на основе whale-сигналов."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 20,
        max_per_instrument: int = 3,
        sl_atr_mult: float = 2.0,
        trail_atr_mult: float = 0.7,
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._max_per_instrument = max_per_instrument
        self._sl_atr = sl_atr_mult
        self._trail_atr = trail_atr_mult

    def process_signals(
        self,
        signals: list[LeaderSignal],
        prices: dict[str, float],
    ) -> int:
        """Обработать leader-сигналы: открыть новые позиции. Возвращает кол-во открытых."""
        opened = 0
        current_total = self._store.count_open_positions(strategy="leaders")

        for sig in signals:
            if current_total >= self._max_positions:
                break

            instr_count = self._store.count_open_positions(
                strategy="leaders", instrument=sig.instrument,
            )
            if instr_count >= self._max_per_instrument:
                continue

            if sig.strength < 0.5:
                continue

            price = prices.get(sig.instrument)
            if price is None or price <= 0:
                continue

            atr = sig.atr if sig.atr > 0 else price * 0.005
            ps = pip_size(sig.instrument)

            if sig.direction == TrendDirection.LONG:
                sl = price - self._sl_atr * atr
            else:
                sl = price + self._sl_atr * atr

            trail = self._trail_atr * atr

            pid = self._store.open_position(
                strategy="leaders",
                source=",".join(sig.sources),
                instrument=sig.instrument,
                direction=sig.direction.value,
                entry_price=price,
                stop_loss_price=sl,
                trail_price=trail,
            )

            cost = estimate_entry_cost(sig.instrument, ",".join(sig.sources), atr, ps)
            self._store.set_estimated_cost(pid, cost.round_trip_pips)

            log.info(
                "  LEADERS OPEN: %s %s @ %.5f (SL=%.5f, trail=%.1f пипсов, src=%s)",
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sl, trail / ps,
                "+".join(sig.sources),
            )
            opened += 1
            current_total += 1

        return opened

    def check_source_reversals(
        self,
        cot_signals: list[CotSignal],
        sentiment_signals: list[SentimentSignal],
    ) -> int:
        """Закрыть позиции если источник развернулся (leader_exit)."""
        source_dirs: dict[str, TrendDirection] = {}
        for s in cot_signals:
            if s.direction != TrendDirection.FLAT:
                source_dirs[s.symbol] = s.direction
        for s in sentiment_signals:
            if s.direction != TrendDirection.FLAT:
                existing = source_dirs.get(s.symbol)
                if existing and existing != s.direction:
                    source_dirs[s.symbol] = TrendDirection.FLAT

        closed = 0
        for pos in self._store.get_open_positions(strategy="leaders"):
            src_dir = source_dirs.get(pos.instrument)
            if src_dir is None:
                continue
            if src_dir != TrendDirection.FLAT and src_dir.value != pos.direction:
                self._store.close_position(pos.id, "leader_exit")
                log.info(
                    "  LEADERS EXIT: %s %s (источник развернулся → %s)",
                    display_name(pos.instrument), pos.direction.upper(),
                    src_dir.value.upper(),
                )
                closed += 1

        return closed
