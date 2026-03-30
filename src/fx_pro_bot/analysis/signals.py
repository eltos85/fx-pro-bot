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


# ── Индикаторы ───────────────────────────────────────────────


def _sma(values: list[float], period: int) -> float:
    return sum(values[-period:]) / period


def _rsi(closes: list[float], period: int = 14) -> float:
    """Relative Strength Index (Wilder)."""
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
    """Average True Range."""
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


# ── Стратегия ────────────────────────────────────────────────


def simple_ma_crossover(bars: list[Bar], fast: int = 10, slow: int = 30) -> Signal:
    """Обратная совместимость: вызывает улучшенную стратегию."""
    return ma_rsi_strategy(bars, fast=fast, slow=slow)


def ma_rsi_strategy(
    bars: list[Bar],
    *,
    fast: int = 10,
    slow: int = 30,
    trend_period: int = 50,
    rsi_period: int = 14,
) -> Signal:
    """
    MA-кроссовер + RSI-фильтр + трендовый фильтр + динамическая сила.

    Сигнал LONG только если:
      1) Быстрая MA пересекла медленную снизу вверх
      2) RSI > 45 (подтверждение бычьего импульса)
      3) Цена выше трендовой MA (не против основного тренда)

    Сигнал SHORT — зеркально.
    """
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
            direction=TrendDirection.FLAT,
            strength=0.1,
            reasons=("no_cross",),
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

        rsi_ok = rsi_val > 45
        trend_ok = trend_dir != TrendDirection.SHORT

        if not rsi_ok:
            reject_reasons.append("rsi_too_low")
        if not trend_ok:
            reject_reasons.append("against_trend")

    else:
        raw_dir = TrendDirection.SHORT
        reasons.append("ma_cross_down")

        rsi_ok = rsi_val < 55
        trend_ok = trend_dir != TrendDirection.LONG

        if not rsi_ok:
            reject_reasons.append("rsi_too_high")
        if not trend_ok:
            reject_reasons.append("against_trend")

    if reject_reasons:
        return Signal(
            direction=TrendDirection.FLAT,
            strength=0.15,
            reasons=tuple(reasons + ["filtered"] + reject_reasons),
            rsi=round(rsi_val, 1),
            trend=trend_dir,
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

    return Signal(
        direction=raw_dir,
        strength=round(strength, 2),
        reasons=tuple(reasons),
        rsi=round(rsi_val, 1),
        trend=trend_dir,
    )


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
    ma_fast: float,
    ma_slow: float,
    rsi: float,
    direction: TrendDirection,
    trend: TrendDirection,
    atr: float,
) -> float:
    """0.0 .. 1.0 — чем больше подтверждений, тем сильнее."""
    score = 0.3  # базовый балл за пересечение

    # RSI: чем дальше от 50 в нужную сторону, тем лучше (до +0.3)
    if direction == TrendDirection.LONG:
        rsi_bonus = min((rsi - 50) / 50, 0.3) if rsi > 50 else 0.0
    else:
        rsi_bonus = min((50 - rsi) / 50, 0.3) if rsi < 50 else 0.0
    score += rsi_bonus

    # MA-разрыв нормализованный по ATR (до +0.2)
    if atr > 0:
        ma_gap = abs(ma_fast - ma_slow) / atr
        score += min(ma_gap * 0.1, 0.2)

    # Совпадение с трендом (+0.2)
    if trend == direction:
        score += 0.2

    return min(score, 1.0)
