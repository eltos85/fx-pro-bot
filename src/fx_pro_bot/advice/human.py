"""Простые формулировки на русском для людей без опыта на бирже."""

from __future__ import annotations

from fx_pro_bot.analysis.signals import Signal, TrendDirection
from fx_pro_bot.events.models import CalendarEvent

_STRENGTH_LABELS = {
    (0.0, 0.35): "слабый",
    (0.35, 0.55): "средний",
    (0.55, 0.75): "сильный",
    (0.75, 1.01): "очень сильный",
}

_TREND_LABELS = {
    TrendDirection.LONG: "восходящий",
    TrendDirection.SHORT: "нисходящий",
    TrendDirection.FLAT: "боковой",
}


def _strength_label(strength: float) -> str:
    for (lo, hi), label in _STRENGTH_LABELS.items():
        if lo <= strength < hi:
            return label
    return "неопределённый"


def advice_for_signal(
    *,
    display_name: str,
    signal: Signal,
    last_price: float | None,
    nearby_events: tuple[CalendarEvent, ...],
) -> str:
    """Один связный текст: что видит бот, осторожность, контекст событий."""
    parts: list[str] = []

    strength_text = _strength_label(signal.strength)

    if signal.direction == TrendDirection.LONG:
        parts.append(
            f"По {display_name} робот видит признаки возможного движения вверх (покупка). "
            f"Сигнал {strength_text} ({signal.strength:.0%}). Это не гарантия — рынок может развернуться."
        )
    elif signal.direction == TrendDirection.SHORT:
        parts.append(
            f"По {display_name} робот видит признаки возможного движения вниз (продажа). "
            f"Сигнал {strength_text} ({signal.strength:.0%}). Это не гарантия — рынок может развернуться."
        )
    else:
        parts.append(
            f"По {display_name} явного направления нет — сигнал нейтральный. "
            "Поспешные ставки обычно рискованнее."
        )

    if last_price is not None:
        parts.append(f"Цена: {last_price:.5f}")

    parts.append(_indicators_plain(signal))

    if nearby_events:
        ev_lines = [f"• {e.title} ({e.at.strftime('%d.%m %H:%M')} UTC)" for e in nearby_events[:5]]
        parts.append(
            "Рядом по времени есть важные события — перед ними волатильность часто выше:\n"
            + "\n".join(ev_lines)
        )

    parts.append(
        "Это совет для обучения и тестов, не индивидуальная инвестиционная рекомендация."
    )
    return "\n\n".join(parts)


def _indicators_plain(signal: Signal) -> str:
    lines: list[str] = []

    reason_map = {
        "ma_cross_up": "Быстрая средняя пересекла медленную снизу вверх (бычий сигнал)",
        "ma_cross_down": "Быстрая средняя пересекла медленную сверху вниз (медвежий сигнал)",
        "rsi_confirms": "RSI подтверждает направление",
        "trend_aligned": "Направление совпадает с основным трендом",
        "no_cross": "Пересечения средних не было",
        "filtered": "Сигнал отфильтрован (недостаточно подтверждений)",
        "rsi_too_low": "RSI слишком низкий для покупки",
        "rsi_too_high": "RSI слишком высокий для продажи",
        "against_trend": "Сигнал против основного тренда",
        "insufficient_bars": "Мало данных для анализа",
    }

    for r in signal.reasons:
        text = reason_map.get(r, f"Техническая отметка: {r}")
        lines.append(f"• {text}")

    if signal.rsi is not None:
        lines.append(f"• RSI: {signal.rsi:.0f}")

    if signal.trend is not None:
        lines.append(f"• Тренд: {_TREND_LABELS.get(signal.trend, '?')}")

    return "Индикаторы:\n" + "\n".join(lines)
