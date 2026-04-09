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
    rsi: float | None = None
    trend: TrendDirection | None = None


# ── Базовые индикаторы ───────────────────────────────────────


def _sma(values: list[float], period: int) -> float:
    return sum(values[-period:]) / period


def _ema(values: list[float], period: int) -> list[float]:
    """Exponential Moving Average — возвращает массив значений."""
    if len(values) < period:
        return values[:]
    k = 2.0 / (period + 1)
    result = [_sma(values[:period], period)]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _rsi(closes: list[float], period: int = 14) -> float:
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


def _atr(bars: list[Bar], period: int = 14) -> float:
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


def compute_adx(bars: list[Bar], period: int = 14) -> float:
    """Average Directional Index — сила тренда (0-100).

    ADX < 20  → слабый/нет тренда (хорошо для mean reversion)
    ADX > 25  → сильный тренд (опасно для mean reversion)
    """
    n = len(bars)
    if n < period * 2 + 1:
        return 0.0

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_list: list[float] = []

    for i in range(1, n):
        high_diff = bars[i].high - bars[i - 1].high
        low_diff = bars[i - 1].low - bars[i].low
        plus_dm.append(high_diff if high_diff > low_diff and high_diff > 0 else 0.0)
        minus_dm.append(low_diff if low_diff > high_diff and low_diff > 0 else 0.0)
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        tr_list.append(tr)

    def _smooth(values: list[float], p: int) -> list[float]:
        result = [sum(values[:p])]
        for v in values[p:]:
            result.append(result[-1] - result[-1] / p + v)
        return result

    sm_tr = _smooth(tr_list, period)
    sm_plus = _smooth(plus_dm, period)
    sm_minus = _smooth(minus_dm, period)

    dx_values: list[float] = []
    for i in range(len(sm_tr)):
        if sm_tr[i] == 0:
            continue
        plus_di = 100 * sm_plus[i] / sm_tr[i]
        minus_di = 100 * sm_minus[i] / sm_tr[i]
        di_sum = plus_di + minus_di
        if di_sum == 0:
            continue
        dx_values.append(100 * abs(plus_di - minus_di) / di_sum)

    if len(dx_values) < period:
        return sum(dx_values) / len(dx_values) if dx_values else 0.0

    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


# ── MACD ─────────────────────────────────────────────────────


def _macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9
) -> TrendDirection:
    """MACD crossover: MACD-линия vs сигнальная линия."""
    if len(closes) < slow + signal_period:
        return TrendDirection.FLAT

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-(min_len - i)] - ema_slow[-(min_len - i)] for i in range(min_len)]

    if len(macd_line) < signal_period:
        return TrendDirection.FLAT

    signal_line = _ema(macd_line, signal_period)
    if len(signal_line) < 2:
        return TrendDirection.FLAT

    cur_macd = macd_line[-1]
    cur_signal = signal_line[-1]
    prev_macd = macd_line[-2]
    prev_signal = signal_line[-2] if len(signal_line) >= 2 else cur_signal

    if prev_macd <= prev_signal and cur_macd > cur_signal:
        return TrendDirection.LONG
    if prev_macd >= prev_signal and cur_macd < cur_signal:
        return TrendDirection.SHORT
    return TrendDirection.FLAT


# ── Stochastic ───────────────────────────────────────────────


def _stochastic(
    bars: list[Bar], k_period: int = 14, d_period: int = 3, slowing: int = 3
) -> TrendDirection:
    """Stochastic: перекупленность/перепроданность + пересечение %K/%D."""
    needed = k_period + d_period + slowing
    if len(bars) < needed:
        return TrendDirection.FLAT

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
        return TrendDirection.FLAT
    smooth_k: list[float] = []
    for i in range(slowing - 1, len(raw_k)):
        smooth_k.append(sum(raw_k[i - slowing + 1 : i + 1]) / slowing)

    if len(smooth_k) < d_period:
        return TrendDirection.FLAT
    d_line: list[float] = []
    for i in range(d_period - 1, len(smooth_k)):
        d_line.append(sum(smooth_k[i - d_period + 1 : i + 1]) / d_period)

    if len(d_line) < 2 or len(smooth_k) < 2:
        return TrendDirection.FLAT

    cur_k = smooth_k[-1]
    prev_k = smooth_k[-2]
    cur_d = d_line[-1]
    prev_d = d_line[-2]

    if cur_k < 25 and prev_k <= prev_d and cur_k > cur_d:
        return TrendDirection.LONG
    if cur_k > 75 and prev_k >= prev_d and cur_k < cur_d:
        return TrendDirection.SHORT
    return TrendDirection.FLAT


# ── Bollinger Bands ──────────────────────────────────────────


def _bollinger(closes: list[float], period: int = 20, num_std: float = 2.0) -> TrendDirection:
    """Отскок от границ Bollinger Bands."""
    if len(closes) < period + 1:
        return TrendDirection.FLAT

    mid = _sma(closes, period)
    variance = sum((c - mid) ** 2 for c in closes[-period:]) / period
    std = variance**0.5

    upper = mid + num_std * std
    lower = mid - num_std * std

    cur = closes[-1]
    prev = closes[-2]

    if prev <= lower and cur > lower:
        return TrendDirection.LONG
    if prev >= upper and cur < upper:
        return TrendDirection.SHORT
    return TrendDirection.FLAT


# ── EMA Bounce ───────────────────────────────────────────────


def _ema_bounce(bars: list[Bar], period: int = 20) -> TrendDirection:
    """Отскок цены от EMA как от поддержки/сопротивления."""
    if len(bars) < period + 2:
        return TrendDirection.FLAT

    closes = [b.close for b in bars]
    ema_vals = _ema(closes, period)
    if len(ema_vals) < 3:
        return TrendDirection.FLAT

    cur_close = closes[-1]
    prev_close = closes[-2]
    prev2_close = closes[-3]
    cur_ema = ema_vals[-1]
    prev_ema = ema_vals[-2]

    touched_from_above = prev_close <= prev_ema * 1.001 and prev2_close > prev_ema
    bounced_up = cur_close > cur_ema
    if touched_from_above and bounced_up:
        return TrendDirection.LONG

    touched_from_below = prev_close >= prev_ema * 0.999 and prev2_close < prev_ema
    bounced_down = cur_close < cur_ema
    if touched_from_below and bounced_down:
        return TrendDirection.SHORT

    return TrendDirection.FLAT


# ── Стратегия MA+RSI (оригинальная) ─────────────────────────


def simple_ma_crossover(bars: list[Bar], fast: int = 10, slow: int = 30) -> Signal:
    return ma_rsi_strategy(bars, fast=fast, slow=slow)


def ma_rsi_strategy(
    bars: list[Bar],
    *,
    fast: int = 10,
    slow: int = 30,
    trend_period: int = 50,
    rsi_period: int = 14,
) -> Signal:
    min_bars = max(slow, trend_period) + 1
    if len(bars) < min_bars:
        return Signal(direction=TrendDirection.FLAT, strength=0.0, reasons=("insufficient_bars",))

    closes = [b.close for b in bars]
    ma_f = _sma(closes, fast)
    ma_s = _sma(closes, slow)
    prev_closes = closes[:-1]
    prev_f = _sma(prev_closes, fast)
    prev_s = _sma(prev_closes, slow)

    crossed_up = prev_f <= prev_s and ma_f > ma_s
    crossed_down = prev_f >= prev_s and ma_f < ma_s

    if not crossed_up and not crossed_down:
        return Signal(
            direction=TrendDirection.FLAT, strength=0.1, reasons=("no_cross",),
            rsi=round(_rsi(closes, rsi_period), 1),
            trend=_trend_direction(closes, trend_period),
        )

    rsi_val = _rsi(closes, rsi_period)
    trend_dir = _trend_direction(closes, trend_period)
    atr_val = _atr(bars)
    reasons: list[str] = []
    reject_reasons: list[str] = []

    if crossed_up:
        raw_dir = TrendDirection.LONG
        reasons.append("ma_cross_up")
        if rsi_val <= 45:
            reject_reasons.append("rsi_too_low")
        if trend_dir == TrendDirection.SHORT:
            reject_reasons.append("against_trend")
    else:
        raw_dir = TrendDirection.SHORT
        reasons.append("ma_cross_down")
        if rsi_val >= 55:
            reject_reasons.append("rsi_too_high")
        if trend_dir == TrendDirection.LONG:
            reject_reasons.append("against_trend")

    if reject_reasons:
        return Signal(
            direction=TrendDirection.FLAT, strength=0.15,
            reasons=tuple(reasons + ["filtered"] + reject_reasons),
            rsi=round(rsi_val, 1), trend=trend_dir,
        )

    strength = _calc_strength(ma_f, ma_s, rsi_val, raw_dir, trend_dir, atr_val)
    if raw_dir == TrendDirection.LONG:
        if rsi_val > 55:
            reasons.append("rsi_confirms")
        if trend_dir == TrendDirection.LONG:
            reasons.append("trend_aligned")
    else:
        if rsi_val < 45:
            reasons.append("rsi_confirms")
        if trend_dir == TrendDirection.SHORT:
            reasons.append("trend_aligned")

    return Signal(direction=raw_dir, strength=round(strength, 2),
                  reasons=tuple(reasons), rsi=round(rsi_val, 1), trend=trend_dir)


def _trend_direction(closes: list[float], period: int) -> TrendDirection:
    if len(closes) < period:
        return TrendDirection.FLAT
    ma_trend = _sma(closes, period)
    current = closes[-1]
    if current > ma_trend:
        return TrendDirection.LONG
    elif current < ma_trend:
        return TrendDirection.SHORT
    return TrendDirection.FLAT


def _calc_strength(
    ma_fast: float, ma_slow: float, rsi: float,
    direction: TrendDirection, trend: TrendDirection, atr: float,
) -> float:
    score = 0.3
    if direction == TrendDirection.LONG:
        rsi_bonus = min((rsi - 50) / 50, 0.3) if rsi > 50 else 0.0
    else:
        rsi_bonus = min((50 - rsi) / 50, 0.3) if rsi < 50 else 0.0
    score += rsi_bonus
    if atr > 0:
        ma_gap = abs(ma_fast - ma_slow) / atr
        score += min(ma_gap * 0.1, 0.2)
    if trend == direction:
        score += 0.2
    return min(score, 1.0)
