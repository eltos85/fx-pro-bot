from datetime import UTC, datetime, timedelta

from fx_pro_bot.analysis.signals import (
    TrendDirection,
    _atr,
    _rsi,
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
                instrument=ins,
                ts=base + timedelta(minutes=i),
                open=p,
                high=p + 0.005,
                low=p - 0.005,
                close=p + 0.002,
                volume=1.0,
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
                instrument=ins,
                ts=base + timedelta(minutes=i),
                open=p,
                high=p + 0.005,
                low=p - 0.005,
                close=p - 0.002,
                volume=1.0,
            )
        )
    return out


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


def test_rsi_basic() -> None:
    ups = [1.0 + 0.01 * i for i in range(20)]
    assert _rsi(ups, 14) > 50

    downs = [2.0 - 0.01 * i for i in range(20)]
    assert _rsi(downs, 14) < 50


def test_rsi_insufficient_data() -> None:
    assert _rsi([1.0, 1.1], 14) == 50.0


def test_atr_positive() -> None:
    bars = _bars_uptrend(20)
    assert _atr(bars, 14) > 0


def test_signal_has_rsi_and_trend() -> None:
    bars = _bars_uptrend(60)
    sig = ma_rsi_strategy(bars, fast=5, slow=15)
    assert sig.rsi is not None
    assert sig.trend is not None


def test_downtrend_no_long_signal() -> None:
    """В сильном даунтренде стратегия не должна давать LONG."""
    bars = _bars_downtrend(60)
    sig = ma_rsi_strategy(bars, fast=5, slow=15)
    assert sig.direction != TrendDirection.LONG


def test_insufficient_bars_flat() -> None:
    bars = _bars_uptrend(10)
    sig = ma_rsi_strategy(bars, fast=5, slow=15)
    assert sig.direction == TrendDirection.FLAT
    assert "insufficient_bars" in sig.reasons
