"""Ансамбль: голосование 5 индикаторов, сигнал при согласии N+."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.signals import (
    Direction,
    Signal,
    bollinger,
    ema_bounce,
    macd,
    ma_rsi_signal,
    rsi,
    stochastic,
    trend_direction,
)
from bybit_bot.market_data.models import Bar

log = logging.getLogger(__name__)

STRATEGY_NAMES = {
    "ma_rsi": "MA+RSI",
    "macd": "MACD",
    "stochastic": "Stochastic",
    "bollinger": "Bollinger",
    "ema_bounce": "EMA Bounce",
}


@dataclass(frozen=True, slots=True)
class Vote:
    strategy: str
    direction: Direction


def ensemble_signal(
    bars: list[Bar], *, fast: int = 10, slow: int = 30, min_votes: int = 3,
) -> Signal:
    """Запускает 5 стратегий и объединяет голоса."""
    if len(bars) < 51:
        return Signal(direction=Direction.FLAT, strength=0.0, reasons=("insufficient_bars",))

    closes = [b.close for b in bars]
    rsi_val = round(rsi(closes, 14), 1)
    trend_dir = trend_direction(closes, 50)

    ma_sig = ma_rsi_signal(bars, fast=fast, slow=slow)
    ma_vote = ma_sig.direction

    votes: list[Vote] = [
        Vote("ma_rsi", ma_vote),
        Vote("macd", macd(closes)),
        Vote("stochastic", stochastic(bars)),
        Vote("bollinger", bollinger(closes)),
        Vote("ema_bounce", ema_bounce(bars)),
    ]

    vote_str = " | ".join(f"{STRATEGY_NAMES[v.strategy]}={v.direction.value}" for v in votes)
    log.debug("Голоса: %s", vote_str)

    long_votes = [v for v in votes if v.direction == Direction.LONG]
    short_votes = [v for v in votes if v.direction == Direction.SHORT]

    if len(long_votes) >= min_votes:
        direction = Direction.LONG
        agreeing = long_votes
        strength = len(long_votes) / len(votes)
    elif len(short_votes) >= min_votes:
        direction = Direction.SHORT
        agreeing = short_votes
        strength = len(short_votes) / len(votes)
    else:
        reasons_parts = [f"{v.strategy}={v.direction.value}" for v in votes]
        return Signal(
            direction=Direction.FLAT,
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
