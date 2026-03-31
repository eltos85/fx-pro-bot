"""Ансамбль стратегий: голосование 5 индикаторов, сигнал при согласии 3+."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fx_pro_bot.analysis.signals import (
    Signal,
    TrendDirection,
    _bollinger,
    _ema_bounce,
    _macd,
    _rsi,
    _stochastic,
    _trend_direction,
    ma_rsi_strategy,
)
from fx_pro_bot.market_data.models import Bar

log = logging.getLogger(__name__)

STRATEGY_NAMES = {
    "ma_rsi": "MA+RSI",
    "macd": "MACD",
    "stochastic": "Stochastic",
    "bollinger": "Bollinger",
    "ema_bounce": "EMA Bounce",
}

MIN_VOTES = 2


@dataclass(frozen=True, slots=True)
class Vote:
    strategy: str
    direction: TrendDirection


def ensemble_signal(bars: list[Bar], *, fast: int = 10, slow: int = 30) -> Signal:
    """Запускает 5 стратегий и объединяет голоса."""
    if len(bars) < 51:
        return Signal(direction=TrendDirection.FLAT, strength=0.0, reasons=("insufficient_bars",))

    closes = [b.close for b in bars]
    rsi_val = round(_rsi(closes, 14), 1)
    trend_dir = _trend_direction(closes, 50)

    ma_sig = ma_rsi_strategy(bars, fast=fast, slow=slow)
    ma_vote = ma_sig.direction if ma_sig.direction != TrendDirection.FLAT else TrendDirection.FLAT

    macd_vote = _macd(closes)
    stoch_vote = _stochastic(bars)
    bb_vote = _bollinger(closes)
    ema_vote = _ema_bounce(bars)

    votes: list[Vote] = [
        Vote("ma_rsi", ma_vote),
        Vote("macd", macd_vote),
        Vote("stochastic", stoch_vote),
        Vote("bollinger", bb_vote),
        Vote("ema_bounce", ema_vote),
    ]

    vote_str = " | ".join(f"{STRATEGY_NAMES[v.strategy]}={v.direction.value}" for v in votes)
    log.debug("Голоса: %s", vote_str)

    long_votes = [v for v in votes if v.direction == TrendDirection.LONG]
    short_votes = [v for v in votes if v.direction == TrendDirection.SHORT]

    long_count = len(long_votes)
    short_count = len(short_votes)

    if long_count >= MIN_VOTES:
        direction = TrendDirection.LONG
        agreeing = long_votes
        strength = long_count / len(votes)
    elif short_count >= MIN_VOTES:
        direction = TrendDirection.SHORT
        agreeing = short_votes
        strength = short_count / len(votes)
    else:
        reasons_parts = [f"{v.strategy}={v.direction.value}" for v in votes]
        return Signal(
            direction=TrendDirection.FLAT,
            strength=0.1,
            reasons=("no_consensus", *reasons_parts),
            rsi=rsi_val,
            trend=trend_dir,
        )

    reasons = tuple(v.strategy for v in agreeing)
    vote_summary = f"{len(agreeing)}/{len(votes)}"

    return Signal(
        direction=direction,
        strength=round(strength, 2),
        reasons=(*reasons, vote_summary),
        rsi=rsi_val,
        trend=trend_dir,
    )
