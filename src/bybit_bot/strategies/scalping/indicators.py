"""Индикаторы для скальпинг-стратегий Bybit-бота.

VWAP, z-score, OLS hedge ratio, EMA slope, средний объём.
"""

from __future__ import annotations

import math

from bybit_bot.market_data.models import Bar


def vwap(bars: list[Bar]) -> float:
    """Volume Weighted Average Price."""
    total_vp = 0.0
    total_vol = 0.0
    for b in bars:
        typical = (b.high + b.low + b.close) / 3.0
        total_vp += typical * b.volume
        total_vol += b.volume

    if total_vol == 0:
        return sum(b.close for b in bars) / len(bars) if bars else 0.0
    return total_vp / total_vol


def vwap_series(bars: list[Bar]) -> list[float]:
    """Кумулятивный VWAP — значение для каждого бара."""
    result: list[float] = []
    cum_vp = 0.0
    cum_vol = 0.0
    for b in bars:
        typical = (b.high + b.low + b.close) / 3.0
        cum_vp += typical * b.volume
        cum_vol += b.volume
        result.append(cum_vp / cum_vol if cum_vol > 0 else b.close)
    return result


def rolling_z_score(values: list[float], window: int) -> float:
    """Z-score последнего значения относительно скользящего окна."""
    if len(values) < window or window < 2:
        return 0.0
    segment = values[-window:]
    mean = sum(segment) / window
    variance = sum((v - mean) ** 2 for v in segment) / (window - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 0.0
    return (values[-1] - mean) / std


def z_score_series(values: list[float], window: int) -> list[float]:
    """Серия z-score для каждого элемента."""
    result: list[float] = []
    for i in range(len(values)):
        if i < window - 1:
            result.append(0.0)
            continue
        segment = values[i - window + 1 : i + 1]
        mean = sum(segment) / window
        variance = sum((v - mean) ** 2 for v in segment) / (window - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
        result.append((values[i] - mean) / std if std > 0 else 0.0)
    return result


def ema_slope(ema_values: list[float], lookback: int = 5) -> float:
    """Наклон EMA: (last - prev) / lookback. >0 = up, <0 = down."""
    if len(ema_values) < lookback + 1:
        return 0.0
    return (ema_values[-1] - ema_values[-1 - lookback]) / lookback


def ols_hedge_ratio(series_a: list[float], series_b: list[float]) -> float:
    """OLS регрессия: beta = cov(A, B) / var(B)."""
    n = min(len(series_a), len(series_b))
    if n < 10:
        return 1.0
    a = series_a[-n:]
    b = series_b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
    var_b = sum((b[i] - mean_b) ** 2 for i in range(n)) / n
    if var_b == 0:
        return 1.0
    return cov / var_b


def spread_series(
    series_a: list[float], series_b: list[float], beta: float,
) -> list[float]:
    """Spread = A - beta * B."""
    n = min(len(series_a), len(series_b))
    return [series_a[-n + i] - beta * series_b[-n + i] for i in range(n)]


def avg_volume(bars: list[Bar], window: int = 20) -> float:
    """Средний volume за последние window баров."""
    subset = bars[-window:] if len(bars) >= window else bars
    if not subset:
        return 0.0
    return sum(b.volume for b in subset) / len(subset)


def adf_pvalue(series: list[float]) -> float:
    """ADF-тест стационарности спреда. Возвращает p-value.

    p < 0.05 → спред стационарен (коинтеграция подтверждена).
    Источник: стандарт для Stat-Arb (Frontiers 2026, Springer 2025).
    """
    if len(series) < 20:
        return 1.0
    try:
        from statsmodels.tsa.stattools import adfuller
        result = adfuller(series, maxlag=1, autolag=None)
        return float(result[1])
    except Exception:
        return 1.0


def compute_adx(bars: list[Bar], period: int = 14) -> float:
    """ADX — сила тренда (0-100). ADX < 20 = боковик, ADX > 25 = сильный тренд."""
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

    adx_val = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return adx_val
