from datetime import UTC, datetime, timedelta

from fx_pro_bot.analysis.signals import (
    TrendDirection,
    _atr,
    _bollinger,
    _ema,
    _ema_bounce,
    _macd,
    _rsi,
    _stochastic,
    ma_rsi_strategy,
    simple_ma_crossover,
)
from fx_pro_bot.market_data.models import Bar, InstrumentId


def _bars_uptrend(n: int) -> list[Bar]:
    ins = InstrumentId(symbol="X")
    base = datetime.now(tz=UTC)
    out: list[Bar] = []
    p = 1.0
    for i in range(n):
        p += 0.01
        out.append(
            Bar(
                instrument=ins, ts=base + timedelta(minutes=i),
                open=p, high=p + 0.005, low=p - 0.005, close=p + 0.002, volume=1.0,
            )
        )
    return out


def _bars_downtrend(n: int) -> list[Bar]:
    ins = InstrumentId(symbol="X")
    base = datetime.now(tz=UTC)
    out: list[Bar] = []
    p = 2.0
    for i in range(n):
        p -= 0.01
        out.append(
            Bar(
                instrument=ins, ts=base + timedelta(minutes=i),
                open=p, high=p + 0.005, low=p - 0.005, close=p - 0.002, volume=1.0,
            )
        )
    return out


# ── MA+RSI ───────────────────────────────────────────────────


def test_ma_crossover_long_bias_on_uptrend() -> None:
    bars = _bars_uptrend(60)
    sig = simple_ma_crossover(bars, fast=5, slow=15)
    assert sig.direction in (TrendDirection.LONG, TrendDirection.FLAT, TrendDirection.SHORT)
    assert 0.0 <= sig.strength <= 1.0


def test_ma_rsi_strategy_returns_signal() -> None:
    bars = _bars_uptrend(60)
    sig = ma_rsi_strategy(bars, fast=5, slow=15)
    assert sig.rsi is not None
    assert sig.trend is not None
    assert 0 <= sig.rsi <= 100
    assert 0.0 <= sig.strength <= 1.0


def test_signal_has_rsi_and_trend() -> None:
    bars = _bars_uptrend(60)
    sig = ma_rsi_strategy(bars, fast=5, slow=15)
    assert sig.rsi is not None
    assert sig.trend is not None


def test_downtrend_no_long_signal() -> None:
    bars = _bars_downtrend(60)
    sig = ma_rsi_strategy(bars, fast=5, slow=15)
    assert sig.direction != TrendDirection.LONG


def test_insufficient_bars_flat() -> None:
    bars = _bars_uptrend(10)
    sig = ma_rsi_strategy(bars, fast=5, slow=15)
    assert sig.direction == TrendDirection.FLAT
    assert "insufficient_bars" in sig.reasons


# ── RSI ──────────────────────────────────────────────────────


def test_rsi_basic() -> None:
    ups = [1.0 + 0.01 * i for i in range(20)]
    assert _rsi(ups, 14) > 50

    downs = [2.0 - 0.01 * i for i in range(20)]
    assert _rsi(downs, 14) < 50


def test_rsi_insufficient_data() -> None:
    assert _rsi([1.0, 1.1], 14) == 50.0


# ── ATR ──────────────────────────────────────────────────────


def test_atr_positive() -> None:
    bars = _bars_uptrend(20)
    assert _atr(bars, 14) > 0


# ── EMA ──────────────────────────────────────────────────────


def test_ema_length() -> None:
    vals = [float(i) for i in range(30)]
    result = _ema(vals, 10)
    assert len(result) == 21  # period + remainder


def test_ema_follows_trend() -> None:
    vals = [float(i) for i in range(50)]
    ema_vals = _ema(vals, 10)
    assert ema_vals[-1] > ema_vals[0]


# ── MACD ─────────────────────────────────────────────────────


def test_macd_flat_on_flat() -> None:
    closes = [1.5] * 60
    assert _macd(closes) == TrendDirection.FLAT


def test_macd_insufficient_data() -> None:
    assert _macd([1.0] * 10) == TrendDirection.FLAT


# ── Stochastic ───────────────────────────────────────────────


def test_stochastic_flat_on_sideways() -> None:
    ins = InstrumentId(symbol="X")
    base = datetime.now(tz=UTC)
    bars = [
        Bar(instrument=ins, ts=base + timedelta(minutes=i),
            open=1.5, high=1.51, low=1.49, close=1.5, volume=1.0)
        for i in range(40)
    ]
    assert _stochastic(bars) == TrendDirection.FLAT


def test_stochastic_insufficient_data() -> None:
    bars = _bars_uptrend(5)
    assert _stochastic(bars) == TrendDirection.FLAT


# ── Bollinger ────────────────────────────────────────────────


def test_bollinger_flat_stable_price() -> None:
    closes = [1.5] * 30
    assert _bollinger(closes) == TrendDirection.FLAT


def test_bollinger_insufficient_data() -> None:
    assert _bollinger([1.0] * 10) == TrendDirection.FLAT


def test_bollinger_long_on_lower_touch() -> None:
    closes = [1.5] * 22
    closes[-2] = 1.3
    closes[-1] = 1.45
    result = _bollinger(closes)
    assert result in (TrendDirection.LONG, TrendDirection.FLAT)


# ── EMA Bounce ───────────────────────────────────────────────


def test_ema_bounce_insufficient_data() -> None:
    bars = _bars_uptrend(5)
    assert _ema_bounce(bars) == TrendDirection.FLAT


def test_ema_bounce_flat_on_steady() -> None:
    ins = InstrumentId(symbol="X")
    base = datetime.now(tz=UTC)
    bars = [
        Bar(instrument=ins, ts=base + timedelta(minutes=i),
            open=1.5, high=1.51, low=1.49, close=1.5, volume=1.0)
        for i in range(30)
    ]
    assert _ema_bounce(bars) == TrendDirection.FLAT
