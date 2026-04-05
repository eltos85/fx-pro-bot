"""Индикаторы для скальпинг-стратегий.

VWAP, z-score, session range, EMA slope, OLS hedge ratio.
Индикаторы из analysis/signals.py (_rsi, _atr, _ema, _sma) импортируются — не дублируются.
"""

from __future__ import annotations

import math

from fx_pro_bot.market_data.models import Bar


def vwap(bars: list[Bar]) -> float:
    """Volume Weighted Average Price по списку баров.

    VWAP = sum(typical_price * volume) / sum(volume).
    Если volume == 0 у всех баров — fallback на простое среднее close.
    """
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
        if cum_vol > 0:
            result.append(cum_vp / cum_vol)
        else:
            result.append(b.close)
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
    """Полная серия z-score для каждого элемента (первые window-1 = 0)."""
    result: list[float] = []
    for i in range(len(values)):
        if i < window - 1:
            result.append(0.0)
            continue
        segment = values[i - window + 1: i + 1]
        mean = sum(segment) / window
        variance = sum((v - mean) ** 2 for v in segment) / (window - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
        result.append((values[i] - mean) / std if std > 0 else 0.0)
    return result


def session_range(bars: list[Bar], n: int) -> tuple[float, float]:
    """High/Low первых n баров (Opening Range Box).

    Returns (box_high, box_low). Если баров меньше n — использовать все.
    """
    subset = bars[:n] if len(bars) >= n else bars
    if not subset:
        return 0.0, 0.0
    return max(b.high for b in subset), min(b.low for b in subset)


def ema_slope(ema_values: list[float], lookback: int = 5) -> float:
    """Наклон EMA: (last - prev) / lookback. >0 = up, <0 = down."""
    if len(ema_values) < lookback + 1:
        return 0.0
    return (ema_values[-1] - ema_values[-1 - lookback]) / lookback


def ols_hedge_ratio(series_a: list[float], series_b: list[float]) -> float:
    """OLS регрессия: beta = cov(A, B) / var(B). Для stat-arb spread."""
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
    """Spread = A - beta * B для каждого элемента."""
    n = min(len(series_a), len(series_b))
    return [series_a[-n + i] - beta * series_b[-n + i] for i in range(n)]


def avg_volume(bars: list[Bar], window: int = 20) -> float:
    """Средний volume за последние window баров."""
    subset = bars[-window:] if len(bars) >= window else bars
    if not subset:
        return 0.0
    return sum(b.volume for b in subset) / len(subset)
