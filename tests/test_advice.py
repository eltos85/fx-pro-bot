from datetime import UTC, datetime

from fx_pro_bot.advice.human import advice_for_signal
from fx_pro_bot.analysis.signals import Signal, TrendDirection
from fx_pro_bot.events.models import CalendarEvent


def test_advice_contains_plain_language() -> None:
    sig = Signal(direction=TrendDirection.LONG, strength=0.5, reasons=("ma_cross_up",))
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
    assert "вверх" in text.lower() or "покупки" in text.lower()
    assert "Тестовое событие" in text
