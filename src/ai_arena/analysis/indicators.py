"""Индикаторы для AI Arena (Nof1 layout).

Nof1 Alpha Arena использует ровно эти индикаторы (см. gist
nof1-prompt.md, секция «Technical Indicators Provided»):

- EMA (20, 50)        — trend direction
- MACD (12, 26, 9)    — momentum
- RSI (7, 14)         — overbought/oversold (7-period для intraday,
                         14-period для trend; ≤25/≥75 extreme)
- ATR (3, 14)         — volatility regime (ATR(3) > ATR(14)×1.5 = expansion)
- Volume (current vs avg(20)) — participation
- Open Interest и Funding Rate — отдельные модули (trading/client.py),
  не indicators в строгом смысле.

Каноничные формулы:
- RSI:  J. Welles Wilder Jr. «New Concepts in Technical Trading Systems»
        (1978) — Wilder's smoothing (RMA), не EMA.
- MACD: Gerald Appel «Technical Analysis: Power Tools for Active Investors»
        (2005) — EMA-based, fast=12 / slow=26 / signal=9.
- ATR:  Wilder (1978) — RMA(True Range, period).
- EMA:  α = 2/(N+1).

Без внешних зависимостей (только Python stdlib).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IntradaySnapshot:
    """3-минутный snapshot для prompt'а Nof1 (×10 точек).

    Все массивы oldest → newest. None если данных не хватает.
    """

    prices: list[float]
    ema20: list[float | None]
    macd: list[float | None]
    rsi7: list[float | None]
    rsi14: list[float | None]


@dataclass
class LongerTermSnapshot:
    """4-часовой snapshot для prompt'а Nof1.

    EMA20 / EMA50 / ATR(3) / ATR(14) — единичные значения (последние).
    MACD / RSI(14) — массивы ×10 (oldest → newest).
    Volume current vs avg.
    """

    ema20: float | None
    ema50: float | None
    atr3: float | None
    atr14: float | None
    volume_current: float
    volume_avg: float
    macd: list[float | None]
    rsi14: list[float | None]


# ─── Базовые helpers ─────────────────────────────────────────────────────


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def _ema_series(values: list[float], period: int) -> list[float | None]:
    """Возвращает массив длины len(values), первые period-1 = None.

    EMA[i] = α·value[i] + (1-α)·EMA[i-1], α = 2/(period+1).
    Seed = SMA первых period значений.
    """
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    alpha = 2 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    e = seed
    for v in values[period:]:
        e = alpha * v + (1 - alpha) * e
        out.append(e)
    return out


def ema(values: list[float], period: int) -> float | None:
    s = _ema_series(values, period)
    last = s[-1] if s else None
    return last if isinstance(last, float) else None


def _rma_series(values: list[float], period: int) -> list[float | None]:
    """Wilder's RMA series, длина = len(values), первые period-1 = None.

    RMA[period-1] = SMA первых period.
    RMA[i] = (RMA[i-1]·(period-1) + value[i]) / period.
    """
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    out: list[float | None] = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    rma = seed
    for v in values[period:]:
        rma = (rma * (period - 1) + v) / period
        out.append(rma)
    return out


def _rma(values: list[float], period: int) -> float | None:
    s = _rma_series(values, period)
    last = s[-1] if s else None
    return last if isinstance(last, float) else None


# ─── RSI ─────────────────────────────────────────────────────────────────


def rsi(closes: list[float], period: int = 14) -> float | None:
    last = rsi_series(closes, period)[-1] if closes else None
    return last if isinstance(last, float) else None


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Возвращает RSI на каждом шаге (None для первых period значений)."""
    n = len(closes)
    if n < period + 1:
        return [None] * n
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains[i] = max(diff, 0.0)
        losses[i] = max(-diff, 0.0)
    avg_gain_series = _rma_series(gains[1:], period)
    avg_loss_series = _rma_series(losses[1:], period)
    out: list[float | None] = [None]  # нулевой индекс — нет diff
    for ag, al in zip(avg_gain_series, avg_loss_series):
        if ag is None or al is None:
            out.append(None)
            continue
        if al == 0:
            out.append(100.0)
            continue
        rs = ag / al
        out.append(100 - (100 / (1 + rs)))
    return out


# ─── MACD ────────────────────────────────────────────────────────────────


def macd_series(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Возвращает (macd_line, signal_line, histogram) — массивы длины len(closes).

    Стандартные параметры Appel (2005): fast=12, slow=26, signal=9.
    """
    n = len(closes)
    if n < slow + signal:
        return ([None] * n, [None] * n, [None] * n)
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line: list[float | None] = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None:
            macd_line.append(None)
        else:
            macd_line.append(f - s)
    valid_idx = [i for i, v in enumerate(macd_line) if v is not None]
    if len(valid_idx) < signal:
        return (macd_line, [None] * n, [None] * n)
    valid_vals = [macd_line[i] for i in valid_idx]
    sig_vals = _ema_series(valid_vals, signal)  # type: ignore[arg-type]
    sig_full: list[float | None] = [None] * n
    for offset, sv in enumerate(sig_vals):
        sig_full[valid_idx[offset]] = sv
    hist: list[float | None] = []
    for m, s in zip(macd_line, sig_full):
        if m is None or s is None:
            hist.append(None)
        else:
            hist.append(m - s)
    return (macd_line, sig_full, hist)


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    m, s, h = macd_series(closes, fast, slow, signal)
    return (m[-1] if m else None, s[-1] if s else None, h[-1] if h else None)


# ─── ATR ─────────────────────────────────────────────────────────────────


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """TR[i] = max(high-low, |high-prev_close|, |low-prev_close|).

    Длина = len(highs) - 1 (для первой свечи нет prev_close).
    """
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


# ─── Volume avg ──────────────────────────────────────────────────────────


def volume_avg(volumes: list[float], period: int = 20) -> float | None:
    if len(volumes) < period or period <= 0:
        return None
    return sum(volumes[-period:]) / period


# ─── Snapshot helpers (для prompt-сборщика) ──────────────────────────────


def _last_n(series: list[float | None], n: int) -> list[float | None]:
    if not series:
        return [None] * n
    if len(series) >= n:
        return list(series[-n:])
    return [None] * (n - len(series)) + list(series)


def build_intraday_snapshot(
    bars_closes: list[float],
    take_n: int = 10,
    display_prices: list[float] | None = None,
) -> IntradaySnapshot:
    """3-минутный layout Nof1: prices/EMA20/MACD/RSI(7)/RSI(14), последние 10.

    Параметр ``display_prices`` (опциональный) — массив цен для отображения
    в prompt (label «Mid prices» из gist L361). Если None — fallback к
    ``bars_closes`` (legacy / тесты). Это разделение нужно потому что:

    - Индикаторы (RSI/MACD/EMA) **канонически** считаются на close-prices —
      это инвариант финансовой математики и gist'а.
    - А «Mid prices» в gist — это intraday mid от Hyperliquid orderbook.
      У Bybit нет per-bar mid (klines дают только OHLC), поэтому делаем
      OHLC4 ≈ (O+H+L+C)/4 как самую близкую аппроксимацию mid за период.
      Этот mapping — расширение «lastPrice вместо mid-price» из правила
      ai-arena-sources.mdc § «Bybit ↔ Hyperliquid маппинг», на массив
      intraday цен. Подробности в BUILDLOG_AI_ARENA.md (v2.x bug-fix).

    Иначе (если бы передавали close в массив с label «Mid prices») LLM
    видел бы close-prices под label «Mid», что — semantic data integrity
    bug (close ≠ mid; close — это последняя сделка в баре, mid — середина
    bid/ask вне зависимости от направления тейкера).
    """
    ema20 = _ema_series(bars_closes, 20)
    macd_line, _, _ = macd_series(bars_closes)
    r7 = rsi_series(bars_closes, 7)
    r14 = rsi_series(bars_closes, 14)
    prices_source = display_prices if display_prices is not None else bars_closes
    prices_last = prices_source[-take_n:] if len(prices_source) >= take_n else (
        [0.0] * (take_n - len(prices_source)) + list(prices_source)
    )
    return IntradaySnapshot(
        prices=prices_last,
        ema20=_last_n(ema20, take_n),
        macd=_last_n(macd_line, take_n),
        rsi7=_last_n(r7, take_n),
        rsi14=_last_n(r14, take_n),
    )


def build_longer_term_snapshot(
    bars_highs: list[float],
    bars_lows: list[float],
    bars_closes: list[float],
    bars_volumes: list[float],
    take_n: int = 10,
) -> LongerTermSnapshot:
    """4-часовой layout Nof1: EMA20/50, ATR(3)/ATR(14), Volume vs avg(20),
    MACD ×10, RSI(14) ×10.
    """
    macd_line, _, _ = macd_series(bars_closes)
    r14 = rsi_series(bars_closes, 14)
    return LongerTermSnapshot(
        ema20=ema(bars_closes, 20),
        ema50=ema(bars_closes, 50),
        atr3=atr(bars_highs, bars_lows, bars_closes, 3),
        atr14=atr(bars_highs, bars_lows, bars_closes, 14),
        volume_current=bars_volumes[-1] if bars_volumes else 0.0,
        volume_avg=volume_avg(bars_volumes, 20) or 0.0,
        macd=_last_n(macd_line, take_n),
        rsi14=_last_n(r14, take_n),
    )
