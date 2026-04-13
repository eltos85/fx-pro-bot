"""[DEPRECATED V1] Momentum-стратегия для крипто-рынка.

Отключена в V2 (13.04.2026). Заменена на EmaTrendStrategy (strategies/trend_ema.py).
Не удалена для возможности отката.

Использует ансамбль индикаторов + дополнительные фильтры,
специфичные для крипто: объём, волатильность, RSI-зоны.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.ensemble import ensemble_signal
from bybit_bot.analysis.signals import Direction, Signal, atr, rsi
from bybit_bot.market_data.models import Bar

log = logging.getLogger(__name__)

RSI_OVERBOUGHT = 75
RSI_OVERSOLD = 25
MIN_VOLUME_RATIO = 0.8  # текущий объём >= 80% от среднего
MAX_ATR_RATIO = 3.0  # фильтр экстремальной волатильности


@dataclass(frozen=True, slots=True)
class TradeSignal:
    symbol: str
    direction: Direction
    strength: float
    entry_price: float
    atr_value: float
    rsi_value: float
    reasons: tuple[str, ...]


class MomentumStrategy:
    """Стратегия на основе ансамблевого сигнала + крипто-фильтры."""

    def __init__(self, *, min_votes: int = 3) -> None:
        self._min_votes = min_votes

    def evaluate(self, symbol: str, bars: list[Bar]) -> TradeSignal | None:
        if len(bars) < 51:
            return None

        signal = ensemble_signal(bars, min_votes=self._min_votes)
        if signal.direction == Direction.FLAT:
            return None

        closes = [b.close for b in bars]
        rsi_val = rsi(closes)
        atr_val = atr(bars)

        reject_reasons: list[str] = []

        if signal.direction == Direction.LONG and rsi_val > RSI_OVERBOUGHT:
            reject_reasons.append(f"rsi_overbought_{rsi_val:.0f}")
        if signal.direction == Direction.SHORT and rsi_val < RSI_OVERSOLD:
            reject_reasons.append(f"rsi_oversold_{rsi_val:.0f}")

        volumes = [b.volume for b in bars[-20:] if b.volume > 0]
        if volumes:
            avg_vol = sum(volumes) / len(volumes)
            cur_vol = bars[-1].volume
            if avg_vol > 0 and cur_vol / avg_vol < MIN_VOLUME_RATIO:
                reject_reasons.append("low_volume")

        if atr_val > 0:
            atr_20 = self._atr_long(bars, 50)
            if atr_20 > 0 and atr_val / atr_20 > MAX_ATR_RATIO:
                reject_reasons.append("extreme_volatility")

        if reject_reasons:
            log.debug(
                "%s: сигнал %s отфильтрован: %s",
                symbol, signal.direction.value, ", ".join(reject_reasons),
            )
            return None

        return TradeSignal(
            symbol=symbol,
            direction=signal.direction,
            strength=signal.strength,
            entry_price=bars[-1].close,
            atr_value=atr_val,
            rsi_value=rsi_val,
            reasons=signal.reasons,
        )

    @staticmethod
    def _atr_long(bars: list[Bar], period: int) -> float:
        return atr(bars, period) if len(bars) >= period + 1 else 0.0
