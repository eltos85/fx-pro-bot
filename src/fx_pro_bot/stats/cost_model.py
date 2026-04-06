"""Модель реалистичных торговых издержек: спред × волатильность + проскальзывание.

Множители основаны на рыночных данных 2024-2025:
- EUR/USD нормальный спред ~1.5 пипсов, при волатильности до 5-10 пипсов
- Проскальзывание нормально 0.2-0.5 пипсов, при волатильности 2-5 пипсов
- ATR-scaled slippage (5% ATR) автоматически растёт в волатильных условиях
"""

from __future__ import annotations

from dataclasses import dataclass

from fx_pro_bot.config.settings import spread_cost_pips

_SPREAD_MULTIPLIERS: dict[str, float] = {
    "extreme_rsi": 2.5,
    "extreme_bb": 2.0,
    "atr_spike": 4.0,
    "news": 3.5,
    "cot": 1.0,
    "sentiment": 1.0,
    "cot,sentiment": 1.0,
    "sentiment,cot": 1.0,
    "vwap_deviation": 1.2,
    "stat_arb": 1.2,
    "orb_breakout": 1.2,
    "news_fade": 1.5,
}

_SLIPPAGE_ATR_PCT: dict[str, float] = {
    "extreme_rsi": 0.05,
    "extreme_bb": 0.04,
    "atr_spike": 0.08,
    "news": 0.07,
    "cot": 0.02,
    "sentiment": 0.02,
    "cot,sentiment": 0.02,
    "sentiment,cot": 0.02,
    "vwap_deviation": 0.03,
    "stat_arb": 0.03,
    "orb_breakout": 0.03,
    "news_fade": 0.05,
}


@dataclass(frozen=True, slots=True)
class CostEstimate:
    spread_pips: float
    slippage_pips: float

    @property
    def total_pips(self) -> float:
        return self.spread_pips + self.slippage_pips

    @property
    def round_trip_pips(self) -> float:
        return self.total_pips * 2


def estimate_entry_cost(
    symbol: str,
    source: str,
    atr: float,
    pip_sz: float,
) -> CostEstimate:
    """Оценка издержек входа на основе условий рынка.

    Args:
        symbol: yfinance-тикер инструмента
        source: источник сигнала (extreme_rsi, cot, vwap_deviation, ...)
        atr: текущий ATR инструмента (в ценовых единицах)
        pip_sz: размер пипса для инструмента

    Returns:
        CostEstimate с оценкой спреда и проскальзывания в пипсах
    """
    base_spread = spread_cost_pips(symbol)
    multiplier = _SPREAD_MULTIPLIERS.get(source, 1.5)
    spread = base_spread * multiplier

    slippage_atr_pct = _SLIPPAGE_ATR_PCT.get(source, 0.03)
    slippage = (atr / pip_sz * slippage_atr_pct) if pip_sz > 0 else 0.0

    return CostEstimate(spread_pips=round(spread, 2), slippage_pips=round(slippage, 2))
