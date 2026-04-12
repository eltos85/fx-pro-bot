"""Volume Spike Detection — обнаружение крупных сделок.

Альтернатива копи-трейдингу: вместо слежения за конкретными трейдерами
ловим крупные сделки "китов" по объёму на барах.

Если текущий бар имеет аномально высокий объём → входим по направлению движения.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.signals import Direction, atr, rsi, trend_direction
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import avg_volume

log = logging.getLogger(__name__)

# Volume spike 2.5-5x = значимый (Trader Dale, AlgoStorm Volume Profile guide).
# "First Test Rule": первый подход к зоне объёма = самый надёжный сигнал.
# OPT-5: снижено с 3.0 до 2.5 — больше сигналов при сохранении фильтрации.
VOLUME_SPIKE_MULT = 2.5  # было 3.0
PRICE_MOVE_ATR_MIN = 0.5
RSI_FILTER_LOW = 20
RSI_FILTER_HIGH = 80
# SL = 2.0 ATR — оптимальный по бэктесту 9,433 сделок (Quant Signals):
# profit factor 1.72 для BTC, drawdown 4.6%.
SL_ATR_MULT = 2.0
TP_ATR_MULT = 2.0
# Не торговать если за последние N баров уже был спайк (first test only).
COOLDOWN_BARS = 5


@dataclass(frozen=True, slots=True)
class VolumeSpikeSignal:
    symbol: str
    direction: Direction
    volume_ratio: float  # текущий / средний
    price_move_atr: float  # движение в ATR
    rsi_value: float
    atr_value: float
    entry_price: float


class VolumeSpikeStrategy:
    """Детекция крупных сделок по аномальному объёму.

    Ищет бары с объёмом >= VOLUME_SPIKE_MULT * avg_volume(20)
    и значимым ценовым движением в направлении close-open.
    Подтверждение: RSI не в экстремуме, тренд совпадает.
    """

    def __init__(self, *, max_signals_per_scan: int = 3) -> None:
        self._max_signals = max_signals_per_scan

    def scan(self, bars_map: dict[str, list[Bar]]) -> list[VolumeSpikeSignal]:
        signals: list[VolumeSpikeSignal] = []

        for symbol, bars in bars_map.items():
            if len(bars) < 30:
                continue

            avg_vol = avg_volume(bars[:-1], 20)
            if avg_vol <= 0:
                continue

            last_bar = bars[-1]
            if last_bar.volume <= 0:
                continue

            vol_ratio = last_bar.volume / avg_vol
            log.debug("%s: vol_ratio=%.1fx (нужно >=%.1f)", symbol, vol_ratio, VOLUME_SPIKE_MULT)
            if vol_ratio < VOLUME_SPIKE_MULT:
                continue

            # First Test: если спайк уже был недавно — пропустить (уровень ослаблен)
            recent = bars[-(COOLDOWN_BARS + 1):-1]
            if any(b.volume / avg_vol >= VOLUME_SPIKE_MULT for b in recent if avg_vol > 0):
                continue

            atr_val = atr(bars)
            if atr_val <= 0:
                continue

            price_move = last_bar.close - last_bar.open
            move_in_atr = abs(price_move) / atr_val

            if move_in_atr < PRICE_MOVE_ATR_MIN:
                continue

            closes = [b.close for b in bars]
            rsi_val = rsi(closes)

            if price_move > 0:
                direction = Direction.LONG
                if rsi_val > RSI_FILTER_HIGH:
                    continue
            else:
                direction = Direction.SHORT
                if rsi_val < RSI_FILTER_LOW:
                    continue

            trend = trend_direction(closes)
            if trend != Direction.FLAT and trend != direction:
                continue

            signals.append(VolumeSpikeSignal(
                symbol=symbol,
                direction=direction,
                volume_ratio=round(vol_ratio, 1),
                price_move_atr=round(move_in_atr, 2),
                rsi_value=round(rsi_val, 1),
                atr_value=atr_val,
                entry_price=last_bar.close,
            ))

            log.info(
                "VOLUME SPIKE: %s %s vol=%.1fx move=%.1f ATR RSI=%.0f",
                symbol, direction.value.upper(), vol_ratio, move_in_atr, rsi_val,
            )

        signals.sort(key=lambda s: s.volume_ratio, reverse=True)
        return signals[: self._max_signals]
