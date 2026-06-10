from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True, slots=True)
class MomentumSignal:
    direction: str  # "long" | "short" | "flat"
    momentum_value: float
    atr: float
    last_close: float


def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def build_signal(
    candles: pd.DataFrame, *, lookback_bars: int, atr_period: int, threshold: float
) -> MomentumSignal | None:
    if candles is None or candles.empty or len(candles) < max(lookback_bars + 1, atr_period + 1):
        return None

    close = candles["Close"]
    momentum = (close.iloc[-1] / close.iloc[-1 - lookback_bars]) - 1.0
    atr_series = _compute_atr(candles, atr_period)
    atr = float(atr_series.iloc[-1])
    last_close = float(close.iloc[-1])

    if atr <= 0 or last_close <= 0:
        return None

    if momentum > threshold:
        direction = "long"
    elif momentum < -threshold:
        direction = "short"
    else:
        direction = "flat"

    return MomentumSignal(
        direction=direction,
        momentum_value=float(momentum),
        atr=atr,
        last_close=last_close,
    )

