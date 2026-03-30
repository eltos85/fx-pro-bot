from datetime import UTC, datetime, timedelta

from fx_pro_bot.analysis.ensemble import ensemble_signal
from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.market_data.models import Bar, InstrumentId


def _bars_flat(n: int) -> list[Bar]:
    ins = InstrumentId(symbol="X")
    base = datetime.now(tz=UTC)
    return [
        Bar(instrument=ins, ts=base + timedelta(minutes=i),
            open=1.5, high=1.505, low=1.495, close=1.5, volume=1.0)
        for i in range(n)
    ]


def _bars_uptrend(n: int) -> list[Bar]:
    ins = InstrumentId(symbol="X")
    base = datetime.now(tz=UTC)
    out: list[Bar] = []
    p = 1.0
    for i in range(n):
        p += 0.01
        out.append(
            Bar(instrument=ins, ts=base + timedelta(minutes=i),
                open=p, high=p + 0.005, low=p - 0.005, close=p + 0.002, volume=1.0)
        )
    return out


def test_ensemble_insufficient_bars() -> None:
    bars = _bars_flat(20)
    sig = ensemble_signal(bars)
    assert sig.direction == TrendDirection.FLAT
    assert "insufficient_bars" in sig.reasons


def test_ensemble_flat_on_sideways() -> None:
    bars = _bars_flat(100)
    sig = ensemble_signal(bars)
    assert sig.direction == TrendDirection.FLAT
    assert sig.rsi is not None
    assert sig.trend is not None


def test_ensemble_strength_range() -> None:
    bars = _bars_uptrend(100)
    sig = ensemble_signal(bars)
    assert 0.0 <= sig.strength <= 1.0


def test_ensemble_has_vote_info() -> None:
    bars = _bars_flat(100)
    sig = ensemble_signal(bars)
    assert len(sig.reasons) > 0


def test_ensemble_valid_direction() -> None:
    bars = _bars_uptrend(100)
    sig = ensemble_signal(bars)
    assert sig.direction in (TrendDirection.LONG, TrendDirection.SHORT, TrendDirection.FLAT)
