"""Индикаторы для скальпинг-стратегий.

VWAP, z-score, session range, EMA slope, OLS hedge ratio.
Индикаторы из analysis/signals.py (_rsi, _atr, _ema, _sma) импортируются — не дублируются.
"""

from __future__ import annotations

import math
from datetime import time, timezone

from fx_pro_bot.market_data.models import Bar

# Границы ликвидных FX-сессий в UTC. End-интервалы exclusive — бары ровно
# в момент закрытия сессии исключаются, т.к. ликвидность схлопывается
# после NY close ([BIS Triennial FX Survey 2022](https://www.bis.org/publ/rpfx22.htm)).
LONDON_START = time(7, 0)
LONDON_END = time(16, 0)
NY_START = time(12, 0)
NY_END = time(21, 0)


def is_liquid_session(bar: Bar) -> bool:
    """Проверить, что бар попадает в ликвидную сессию (London / NY)."""
    ts = bar.ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    t = ts.time()
    if ts.weekday() >= 5:
        return False
    return LONDON_START <= t < LONDON_END or NY_START <= t < NY_END


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


def resample_m5_to_h1(bars: list[Bar]) -> list[Bar]:
    """Агрегирует M5 бары в H1 (по часу UTC)."""
    if not bars:
        return []
    hourly: dict[str, list[Bar]] = {}
    for b in bars:
        key = b.ts.strftime("%Y-%m-%d-%H")
        hourly.setdefault(key, []).append(b)

    result: list[Bar] = []
    for key in sorted(hourly):
        group = hourly[key]
        result.append(Bar(
            instrument=group[0].instrument,
            ts=group[0].ts,
            open=group[0].open,
            high=max(b.high for b in group),
            low=min(b.low for b in group),
            close=group[-1].close,
            volume=sum(b.volume for b in group),
        ))
    return result


def resample_m5_to_h4(bars: list[Bar]) -> list[Bar]:
    """Агрегирует M5 бары в H4 (границы 00/04/08/12/16/20 UTC)."""
    if not bars:
        return []
    buckets: dict[str, list[Bar]] = {}
    for b in bars:
        # H4 bucket = floor(hour / 4) * 4
        h4 = (b.ts.hour // 4) * 4
        key = f"{b.ts.strftime('%Y-%m-%d')}-{h4:02d}"
        buckets.setdefault(key, []).append(b)

    result: list[Bar] = []
    for key in sorted(buckets):
        group = buckets[key]
        result.append(Bar(
            instrument=group[0].instrument,
            ts=group[0].ts,
            open=group[0].open,
            high=max(b.high for b in group),
            low=min(b.low for b in group),
            close=group[-1].close,
            volume=sum(b.volume for b in group),
        ))
    return result


def htf_ema_trend(bars_m5: list[Bar], ema_period: int = 200) -> float | None:
    """EMA trend на H1 (ресемплированных из M5).

    Возвращает slope EMA: >0 = uptrend, <0 = downtrend, None = недостаточно данных.
    """
    h1 = resample_m5_to_h1(bars_m5)
    if len(h1) < ema_period + 5:
        return None
    closes = [b.close for b in h1]
    # Рассчитываем EMA вручную
    mult = 2.0 / (ema_period + 1)
    ema = closes[0]
    ema_vals = [ema]
    for c in closes[1:]:
        ema = c * mult + ema * (1 - mult)
        ema_vals.append(ema)
    return ema_slope(ema_vals, 5)


def adf_test_stationary(spread: list[float], max_lag: int = 1) -> float:
    """Simplified ADF test: returns t-statistic for stationarity.

    Более отрицательное значение = больше уверенности в стационарности.
    Критические значения: -3.43 (1%), -2.86 (5%), -2.57 (10%).
    Возвращает t-stat; если > -2.86 — коинтеграция сомнительна.
    """
    n = len(spread)
    if n < max_lag + 10:
        return 0.0
    dy = [spread[i] - spread[i - 1] for i in range(1, n)]
    y_lag = spread[:-1]

    n_obs = len(dy)
    mean_dy = sum(dy) / n_obs
    mean_y = sum(y_lag) / n_obs

    cov_xy = sum((y_lag[i] - mean_y) * (dy[i] - mean_dy) for i in range(n_obs)) / n_obs
    var_x = sum((y_lag[i] - mean_y) ** 2 for i in range(n_obs)) / n_obs

    if var_x == 0:
        return 0.0

    beta = cov_xy / var_x
    residuals = [dy[i] - beta * y_lag[i] for i in range(n_obs)]
    sse = sum(r * r for r in residuals)
    se_beta = (sse / (n_obs - 1) / (var_x * n_obs)) ** 0.5 if var_x * n_obs > 0 else 1.0

    if se_beta == 0:
        return 0.0
    return beta / se_beta
