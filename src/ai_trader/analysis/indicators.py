"""Технические индикаторы для AI-Trader.

Все формулы — каноничные, см. блок Research basis ниже.
Реализации без внешних зависимостей (только Python stdlib).

─── Research basis ───
- RSI: J. Welles Wilder Jr. «New Concepts in Technical Trading Systems» (1978).
  Период по умолчанию 14, использует Wilder's smoothing (RMA, не EMA).
- MACD: Gerald Appel «Technical Analysis: Power Tools for Active Investors»
  (2005). Стандартные параметры: fast=12, slow=26, signal=9 (EMA-based).
- ATR: тот же Wilder (1978). RMA(True Range, 14).
- EMA: канонический экспоненциально-взвешенный mean. α = 2/(N+1).
- Bollinger Bands: John Bollinger «Bollinger on Bollinger Bands» (2001).
  Middle = SMA(20), Upper/Lower = middle ± 2·std(20).
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass
class IndicatorSnapshot:
    """Полный набор индикаторов для одного символа."""

    last_close: float
    rsi14: float | None
    macd_line: float | None
    macd_signal: float | None
    macd_hist: float | None
    atr14: float | None
    atr14_pct: float | None  # ATR / last_close * 100, для нормализации
    ema20: float | None
    ema50: float | None
    bb_upper: float | None
    bb_middle: float | None
    bb_lower: float | None
    bb_position: float | None  # (close-lower)/(upper-lower) [0..1]; <0 / >1 = за пределами


# ─── Базовые helpers ─────────────────────────────────────────────────────


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    alpha = 2 / (period + 1)
    seed = sum(values[:period]) / period
    e = seed
    for v in values[period:]:
        e = alpha * v + (1 - alpha) * e
    return e


def _ema_series(values: list[float], period: int) -> list[float]:
    """Возвращает полный массив EMA (None для первых period-1 значений)."""
    if period <= 0 or len(values) < period:
        return [float("nan")] * len(values)
    alpha = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out = [float("nan")] * (period - 1) + [seed]
    e = seed
    for v in values[period:]:
        e = alpha * v + (1 - alpha) * e
        out.append(e)
    return out


def _rma(values: list[float], period: int) -> float | None:
    """Wilder's RMA (Running Moving Average), как в RSI/ATR.

    Первое значение = SMA первых period значений.
    Дальше: RMA[i] = (RMA[i-1] * (period - 1) + value[i]) / period.
    """
    if len(values) < period or period <= 0:
        return None
    seed = sum(values[:period]) / period
    rma = seed
    for v in values[period:]:
        rma = (rma * (period - 1) + v) / period
    return rma


# ─── Конкретные индикаторы ───────────────────────────────────────────────


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = _rma(gains, period)
    avg_loss = _rma(losses, period)
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    """Возвращает (macd_line, signal_line, histogram).

    macd_line = EMA(fast) - EMA(slow)
    signal = EMA(macd_line, signal)
    histogram = macd_line - signal
    """
    if len(closes) < slow + signal:
        return (None, None, None)
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line_series = [
        f - s if not (f != f or s != s) else float("nan")
        for f, s in zip(ema_fast, ema_slow)
    ]
    valid_macd = [v for v in macd_line_series if v == v]  # отбрасываем NaN
    if len(valid_macd) < signal:
        return (None, None, None)
    sig = ema(valid_macd, signal)
    macd_now = valid_macd[-1]
    if sig is None:
        return (macd_now, None, None)
    return (macd_now, sig, macd_now - sig)


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """TR = max(high-low, |high-prev_close|, |low-prev_close|)."""
    if len(highs) != len(lows) or len(highs) != len(closes) or len(highs) < 2:
        return []
    out: list[float] = []
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        out.append(max(hl, hc, lc))
    return out


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float | None:
    trs = true_ranges(highs, lows, closes)
    if len(trs) < period:
        return None
    return _rma(trs, period)


def bollinger(closes: list[float], period: int = 20, sigma: float = 2.0) -> tuple[float | None, float | None, float | None]:
    """Возвращает (upper, middle, lower). middle = SMA(period)."""
    if len(closes) < period or period <= 0:
        return (None, None, None)
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = sqrt(var)
    return (mid + sigma * sd, mid, mid - sigma * sd)


# ─── Сводка по всем индикаторам ──────────────────────────────────────────


def compute_snapshot(
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> IndicatorSnapshot:
    """Полный snapshot. Тихо возвращает None для тех индикаторов, для которых
    данных не хватило (например, при первом запуске когда только 5 свечей).
    """
    last_close = closes[-1] if closes else 0.0
    rsi_v = rsi(closes, 14)
    macd_line, macd_sig, macd_h = macd(closes)
    atr_v = atr(highs, lows, closes, 14)
    atr_pct = (atr_v / last_close * 100) if (atr_v is not None and last_close > 0) else None
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    bb_u, bb_m, bb_l = bollinger(closes, 20, 2.0)
    bb_pos: float | None = None
    if bb_u is not None and bb_l is not None and bb_u != bb_l:
        bb_pos = (last_close - bb_l) / (bb_u - bb_l)

    return IndicatorSnapshot(
        last_close=last_close,
        rsi14=rsi_v,
        macd_line=macd_line,
        macd_signal=macd_sig,
        macd_hist=macd_h,
        atr14=atr_v,
        atr14_pct=atr_pct,
        ema20=ema20,
        ema50=ema50,
        bb_upper=bb_u,
        bb_middle=bb_m,
        bb_lower=bb_l,
        bb_position=bb_pos,
    )


def format_snapshot(s: IndicatorSnapshot) -> str:
    """Компактная человекочитаемая строка для вкладывания в LLM-context."""

    def fmt(x: float | None, pattern: str) -> str:
        return pattern.format(x) if x is not None else "n/a"

    rsi_label = ""
    if s.rsi14 is not None:
        if s.rsi14 >= 70:
            rsi_label = " [OVERBOUGHT]"
        elif s.rsi14 <= 30:
            rsi_label = " [OVERSOLD]"

    macd_label = ""
    if s.macd_hist is not None:
        macd_label = " [bullish]" if s.macd_hist > 0 else " [bearish]"

    trend_label = ""
    if s.ema20 is not None and s.ema50 is not None:
        if s.ema20 > s.ema50 and s.last_close > s.ema20:
            trend_label = " [uptrend]"
        elif s.ema20 < s.ema50 and s.last_close < s.ema20:
            trend_label = " [downtrend]"
        else:
            trend_label = " [mixed]"

    bb_label = ""
    if s.bb_position is not None:
        if s.bb_position >= 1.0:
            bb_label = " [above upper BB]"
        elif s.bb_position <= 0.0:
            bb_label = " [below lower BB]"
        elif s.bb_position >= 0.8:
            bb_label = " [near upper BB]"
        elif s.bb_position <= 0.2:
            bb_label = " [near lower BB]"

    return (
        f"  RSI14={fmt(s.rsi14, '{:.1f}')}{rsi_label} "
        f"MACD={fmt(s.macd_line, '{:.4g}')}/sig={fmt(s.macd_signal, '{:.4g}')}/"
        f"hist={fmt(s.macd_hist, '{:+.4g}')}{macd_label}\n"
        f"  ATR14={fmt(s.atr14, '{:.4g}')} ({fmt(s.atr14_pct, '{:.2f}')}% of price)  "
        f"EMA20={fmt(s.ema20, '{:.4g}')} EMA50={fmt(s.ema50, '{:.4g}')}{trend_label}\n"
        f"  BB(20,2): upper={fmt(s.bb_upper, '{:.4g}')} mid={fmt(s.bb_middle, '{:.4g}')} "
        f"lower={fmt(s.bb_lower, '{:.4g}')} pos={fmt(s.bb_position, '{:.2f}')}{bb_label}"
    )
