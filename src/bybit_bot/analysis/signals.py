"""Технические индикаторы и сигналы для крипто-бота."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from bybit_bot.market_data.models import Bar


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class Signal:
    direction: Direction
    strength: float  # 0..1
    reasons: tuple[str, ...]
    rsi: float | None = None
    trend: Direction | None = None
    sl_atr_mult: float | None = None
    tp_atr_mult: float | None = None
    pair_tag: str | None = None
    strategy_name: str = ""


# ── Базовые индикаторы ──────────────────────────────────────


def sma(values: list[float], period: int) -> float:
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return values[:]
    k = 2.0 / (period + 1)
    result = [sma(values[:period], period)]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(len(bars) - period, len(bars)):
        high = bars[i].high
        low = bars[i].low
        prev_close = bars[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9,
) -> Direction:
    if len(closes) < slow + signal_period:
        return Direction.FLAT

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-(min_len - i)] - ema_slow[-(min_len - i)] for i in range(min_len)]

    if len(macd_line) < signal_period:
        return Direction.FLAT

    signal_line = ema(macd_line, signal_period)
    if len(signal_line) < 2:
        return Direction.FLAT

    cur_macd = macd_line[-1]
    cur_signal = signal_line[-1]
    prev_macd = macd_line[-2]
    prev_signal = signal_line[-2] if len(signal_line) >= 2 else cur_signal

    if prev_macd <= prev_signal and cur_macd > cur_signal:
        return Direction.LONG
    if prev_macd >= prev_signal and cur_macd < cur_signal:
        return Direction.SHORT
    return Direction.FLAT


def stochastic(
    bars: list[Bar], k_period: int = 14, d_period: int = 3, slowing: int = 3,
) -> Direction:
    needed = k_period + d_period + slowing
    if len(bars) < needed:
        return Direction.FLAT

    raw_k: list[float] = []
    for i in range(k_period, len(bars) + 1):
        window = bars[i - k_period : i]
        high = max(b.high for b in window)
        low = min(b.low for b in window)
        close = window[-1].close
        if high == low:
            raw_k.append(50.0)
        else:
            raw_k.append(100.0 * (close - low) / (high - low))

    if len(raw_k) < slowing:
        return Direction.FLAT
    smooth_k: list[float] = []
    for i in range(slowing - 1, len(raw_k)):
        smooth_k.append(sum(raw_k[i - slowing + 1 : i + 1]) / slowing)

    if len(smooth_k) < d_period:
        return Direction.FLAT
    d_line: list[float] = []
    for i in range(d_period - 1, len(smooth_k)):
        d_line.append(sum(smooth_k[i - d_period + 1 : i + 1]) / d_period)

    if len(d_line) < 2 or len(smooth_k) < 2:
        return Direction.FLAT

    cur_k = smooth_k[-1]
    prev_k = smooth_k[-2]
    cur_d = d_line[-1]
    prev_d = d_line[-2]

    if cur_k < 25 and prev_k <= prev_d and cur_k > cur_d:
        return Direction.LONG
    if cur_k > 75 and prev_k >= prev_d and cur_k < cur_d:
        return Direction.SHORT
    return Direction.FLAT


def bollinger(closes: list[float], period: int = 20, num_std: float = 2.0) -> Direction:
    if len(closes) < period + 1:
        return Direction.FLAT

    mid = sma(closes, period)
    variance = sum((c - mid) ** 2 for c in closes[-period:]) / period
    std = variance**0.5

    upper = mid + num_std * std
    lower = mid - num_std * std

    cur = closes[-1]
    prev = closes[-2]

    if prev <= lower and cur > lower:
        return Direction.LONG
    if prev >= upper and cur < upper:
        return Direction.SHORT
    return Direction.FLAT


def ema_bounce(bars: list[Bar], period: int = 20) -> Direction:
    if len(bars) < period + 2:
        return Direction.FLAT

    closes = [b.close for b in bars]
    ema_vals = ema(closes, period)
    if len(ema_vals) < 3:
        return Direction.FLAT

    cur_close = closes[-1]
    prev_close = closes[-2]
    prev2_close = closes[-3]
    cur_ema = ema_vals[-1]
    prev_ema = ema_vals[-2]

    touched_from_above = prev_close <= prev_ema * 1.001 and prev2_close > prev_ema
    bounced_up = cur_close > cur_ema
    if touched_from_above and bounced_up:
        return Direction.LONG

    touched_from_below = prev_close >= prev_ema * 0.999 and prev2_close < prev_ema
    bounced_down = cur_close < cur_ema
    if touched_from_below and bounced_down:
        return Direction.SHORT

    return Direction.FLAT


def trend_direction(closes: list[float], period: int = 50) -> Direction:
    if len(closes) < period:
        return Direction.FLAT
    ma_trend = sma(closes, period)
    current = closes[-1]
    if current > ma_trend:
        return Direction.LONG
    elif current < ma_trend:
        return Direction.SHORT
    return Direction.FLAT


def ma_rsi_signal(
    bars: list[Bar],
    *,
    fast: int = 10,
    slow: int = 30,
    rsi_period: int = 14,
) -> Signal:
    """MA crossover + RSI фильтр."""
    min_bars = max(slow, 50) + 1
    if len(bars) < min_bars:
        return Signal(direction=Direction.FLAT, strength=0.0, reasons=("insufficient_bars",))

    closes = [b.close for b in bars]
    ma_f = sma(closes, fast)
    ma_s = sma(closes, slow)
    prev_closes = closes[:-1]
    prev_f = sma(prev_closes, fast)
    prev_s = sma(prev_closes, slow)

    crossed_up = prev_f <= prev_s and ma_f > ma_s
    crossed_down = prev_f >= prev_s and ma_f < ma_s

    rsi_val = rsi(closes, rsi_period)
    trend_dir = trend_direction(closes, 50)

    if not crossed_up and not crossed_down:
        return Signal(
            direction=Direction.FLAT, strength=0.1, reasons=("no_cross",),
            rsi=round(rsi_val, 1), trend=trend_dir,
        )

    if crossed_up:
        raw_dir = Direction.LONG
        reasons = ["ma_cross_up"]
        if rsi_val <= 45 or trend_dir == Direction.SHORT:
            return Signal(
                direction=Direction.FLAT, strength=0.15,
                reasons=tuple(reasons + ["filtered"]),
                rsi=round(rsi_val, 1), trend=trend_dir,
            )
    else:
        raw_dir = Direction.SHORT
        reasons = ["ma_cross_down"]
        if rsi_val >= 55 or trend_dir == Direction.LONG:
            return Signal(
                direction=Direction.FLAT, strength=0.15,
                reasons=tuple(reasons + ["filtered"]),
                rsi=round(rsi_val, 1), trend=trend_dir,
            )

    atr_val = atr(bars)
    strength = 0.3
    if raw_dir == Direction.LONG:
        strength += min((rsi_val - 50) / 50, 0.3) if rsi_val > 50 else 0.0
    else:
        strength += min((50 - rsi_val) / 50, 0.3) if rsi_val < 50 else 0.0
    if atr_val > 0:
        strength += min(abs(ma_f - ma_s) / atr_val * 0.1, 0.2)
    if trend_dir == raw_dir:
        strength += 0.2

    return Signal(
        direction=raw_dir, strength=round(min(strength, 1.0), 2),
        reasons=tuple(reasons), rsi=round(rsi_val, 1), trend=trend_dir,
    )
