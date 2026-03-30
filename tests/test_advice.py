from datetime import UTC, datetime

from fx_pro_bot.advice.human import advice_for_signal
from fx_pro_bot.analysis.signals import Signal, TrendDirection
from fx_pro_bot.events.models import CalendarEvent


def test_advice_contains_plain_language() -> None:
    sig = Signal(
        direction=TrendDirection.LONG,
        strength=0.5,
        reasons=("ma_cross_up", "rsi_confirms"),
        rsi=62.0,
        trend=TrendDirection.LONG,
    )
    ev = (
        CalendarEvent(
            title="Тестовое событие",
            at=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            importance="high",
        ),
    )
    text = advice_for_signal(
        display_name="EUR/USD",
        signal=sig,
        last_price=1.1,
        nearby_events=ev,
    )
    assert "EUR/USD" in text
    assert "вверх" in text.lower() or "покупк" in text.lower()
    assert "Тестовое событие" in text
    assert "RSI" in text
    assert "50%" in text


def test_advice_shows_strength() -> None:
    sig = Signal(
        direction=TrendDirection.SHORT,
        strength=0.7,
        reasons=("ma_cross_down", "trend_aligned"),
        rsi=35.0,
        trend=TrendDirection.SHORT,
    )
    text = advice_for_signal(
        display_name="GBP/USD",
        signal=sig,
        last_price=1.25,
        nearby_events=(),
    )
    assert "сильный" in text.lower()
    assert "70%" in text
    assert "вниз" in text.lower() or "продаж" in text.lower()
