"""Простые формулировки на русском для людей без опыта на бирже."""

from __future__ import annotations

from fx_pro_bot.analysis.signals import Signal, TrendDirection
from fx_pro_bot.events.models import CalendarEvent


def advice_for_signal(
    *,
    display_name: str,
    signal: Signal,
    last_price: float | None,
    nearby_events: tuple[CalendarEvent, ...],
) -> str:
    """Один связный текст: что видит бот, осторожность, контекст событий."""
    parts: list[str] = []

    if signal.direction == TrendDirection.LONG:
        parts.append(
            f"По {display_name} сейчас робот видит признаки, которые часто связывают с возможным "
            "движением цены вверх (в сторону покупки). Это не гарантия — рынок может развернуться."
        )
    elif signal.direction == TrendDirection.SHORT:
        parts.append(
            f"По {display_name} сейчас робот видит признаки, которые часто связывают с возможным "
            "движением цены вниз (в сторону продажи). Это не гарантия — рынок может развернуться."
        )
    else:
        parts.append(
            f"По {display_name} явного направления сейчас нет — сигнал нейтральный. "
            "Так бывает, когда картина смешанная; поспешные ставки обычно рискованнее."
        )

    if last_price is not None:
        parts.append(f"Ориентир цены (последнее значение в данных): {last_price:.5f}.")

    parts.append(_reasons_plain(signal))

    if nearby_events:
        ev_lines = [f"• {e.title} ({e.at.strftime('%d.%m %H:%M')} UTC)" for e in nearby_events[:5]]
        parts.append(
            "Рядом по времени есть важные события из вашего календаря — перед ними волатильность "
            "часто выше:\n" + "\n".join(ev_lines)
        )

    parts.append(
        "Это совет для обучения и тестов, не индивидуальная инвестиционная рекомендация."
    )
    return "\n\n".join(parts)


def _reasons_plain(signal: Signal) -> str:
    mapping = {
        "ma_cross_up": "Короткая средняя цена пересекла длинную снизу вверх — часто смотрят как на бычий намёк.",
        "ma_cross_down": "Короткая средняя пересекла длинную сверху вниз — часто смотрят как на медвежий намёк.",
        "no_cross": "Пересечения средних пока не было — тренд по этому правилу не выделен.",
        "insufficient_bars": "Истории цен мало, чтобы уверенно считать средние — лучше подождать больше данных.",
    }
    lines = [mapping.get(r, f"Техническая отметка: {r}.") for r in signal.reasons]
    return "Детали (простыми словами):\n" + "\n".join(f"• {line}" for line in lines)
