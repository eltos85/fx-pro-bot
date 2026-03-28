from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fx_pro_bot.market_data.models import Bar


class TrendDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class Signal:
    direction: TrendDirection
    strength: float  # 0..1
    reasons: tuple[str, ...]


def simple_ma_crossover(bars: list[Bar], fast: int = 10, slow: int = 30) -> Signal:
    """
    Минимальный пример стратегии: пересечение средних по close.
    На короткой истории может часто давать FLAT.
    """
    if len(bars) < slow + 1:
        return Signal(direction=TrendDirection.FLAT, strength=0.0, reasons=("insufficient_bars",))

    closes = [b.close for b in bars]
    ma_f = sum(closes[-fast:]) / fast
    ma_s = sum(closes[-slow:]) / slow
    prev_f = sum(closes[-fast - 1 : -1]) / fast
    prev_s = sum(closes[-slow - 1 : -1]) / slow

    crossed_up = prev_f <= prev_s and ma_f > ma_s
    crossed_down = prev_f >= prev_s and ma_f < ma_s

    if crossed_up:
        return Signal(direction=TrendDirection.LONG, strength=0.6, reasons=("ma_cross_up",))
    if crossed_down:
        return Signal(direction=TrendDirection.SHORT, strength=0.6, reasons=("ma_cross_down",))
    return Signal(direction=TrendDirection.FLAT, strength=0.2, reasons=("no_cross",))
