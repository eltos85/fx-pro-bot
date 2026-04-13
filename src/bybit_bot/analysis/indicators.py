"""Технические индикаторы для V2 trend-following стратегии.

Все функции работают с list[float] или list[Bar] — без pandas.
"""

from __future__ import annotations

from bybit_bot.market_data.models import Bar


def ema(values: list[float], period: int) -> list[float]:
    """Exponential Moving Average. Возвращает список той же длины что values."""
    if not values or period <= 0:
        return []
    if len(values) < period:
        return values[:]

    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    result = [0.0] * (period - 1) + [seed]
    for i in range(period, len(values)):
        result.append(values[i] * k + result[-1] * (1 - k))
    return result


def atr(bars: list[Bar], period: int = 14) -> float:
    """Average True Range — последнее значение."""
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


def atr_series(bars: list[Bar], period: int = 14) -> list[float]:
    """ATR как серия значений (SMA TR)."""
    if len(bars) < period + 1:
        return []
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = bars[i].high
        low = bars[i].low
        prev_close = bars[i - 1].close
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    result: list[float] = []
    for i in range(period - 1, len(trs)):
        result.append(sum(trs[i - period + 1: i + 1]) / period)
    return result


def adx(bars: list[Bar], period: int = 14) -> float:
    """Average Directional Index — последнее значение.

    ADX > 20 = тренд, ADX < 20 = боковик.
    Wilder's smoothing: alpha = 1/period.
    """
    if len(bars) < period * 2 + 1:
        return 0.0

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr_list: list[float] = []

    for i in range(1, len(bars)):
        high = bars[i].high
        low = bars[i].low
        prev_high = bars[i - 1].high
        prev_low = bars[i - 1].low
        prev_close = bars[i - 1].close

        up = high - prev_high
        down = prev_low - low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr_list.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    if len(tr_list) < period:
        return 0.0

    smooth_tr = sum(tr_list[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])

    dx_values: list[float] = []

    for i in range(period, len(tr_list)):
        smooth_tr = smooth_tr - smooth_tr / period + tr_list[i]
        smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
        smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]

        if smooth_tr == 0:
            continue
        di_plus = 100 * smooth_plus / smooth_tr
        di_minus = 100 * smooth_minus / smooth_tr
        di_sum = di_plus + di_minus
        if di_sum == 0:
            dx_values.append(0.0)
        else:
            dx_values.append(100 * abs(di_plus - di_minus) / di_sum)

    if len(dx_values) < period:
        return 0.0

    adx_val = sum(dx_values[:period]) / period
    for i in range(period, len(dx_values)):
        adx_val = (adx_val * (period - 1) + dx_values[i]) / period

    return adx_val


def volume_avg(bars: list[Bar], period: int = 20) -> float:
    """Средний объём за последние period баров."""
    if len(bars) < period:
        return 0.0
    return sum(b.volume for b in bars[-period:]) / period
