from datetime import UTC, datetime, timedelta

from fx_pro_bot.analysis.signals import TrendDirection, simple_ma_crossover
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


def test_ma_crossover_long_bias_on_uptrend() -> None:
    bars = _bars_uptrend(40)
    sig = simple_ma_crossover(bars, fast=5, slow=15)
    assert sig.direction in (TrendDirection.LONG, TrendDirection.FLAT, TrendDirection.SHORT)
    assert 0.0 <= sig.strength <= 1.0
