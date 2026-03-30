"""Простые формулировки на русском для людей без опыта на бирже."""

from __future__ import annotations

from fx_pro_bot.analysis.ensemble import STRATEGY_NAMES
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

    parts.append(_strategies_plain(signal))

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


def _strategies_plain(signal: Signal) -> str:
    """Человекочитаемое описание голосования стратегий."""
    lines: list[str] = []

    vote_summary = None
    strategies_agreed: list[str] = []

    for r in signal.reasons:
        if r in STRATEGY_NAMES:
            strategies_agreed.append(STRATEGY_NAMES[r])
        elif "/" in r and r[0].isdigit():
            vote_summary = r
        elif r == "no_consensus":
            lines.append("• Стратегии не пришли к согласию (нужно 3 из 5)")
        elif r == "insufficient_bars":
            lines.append("• Мало данных для анализа")
        elif r.startswith(("ma_rsi=", "macd=", "stochastic=", "bollinger=", "ema_bounce=")):
            name, val = r.split("=", 1)
            label = STRATEGY_NAMES.get(name, name)
            dir_text = {"long": "вверх", "short": "вниз", "flat": "нейтрально"}.get(val, val)
            lines.append(f"  {label}: {dir_text}")
        else:
            _fallback_reason(r, lines)

    if strategies_agreed:
        names = ", ".join(strategies_agreed)
        count = vote_summary or f"{len(strategies_agreed)}/5"
        lines.insert(0, f"• Стратегии подтвердили: {names} [{count}]")

    if signal.rsi is not None:
        lines.append(f"• RSI: {signal.rsi:.0f}")
    if signal.trend is not None:
        lines.append(f"• Тренд: {_TREND_LABELS.get(signal.trend, '?')}")

    return "Анализ стратегий:\n" + "\n".join(lines)


_REASON_MAP = {
    "ma_cross_up": "MA: пересечение вверх",
    "ma_cross_down": "MA: пересечение вниз",
    "rsi_confirms": "RSI подтверждает",
    "trend_aligned": "Совпадает с трендом",
    "no_cross": "Нет пересечения MA",
    "filtered": "Сигнал отфильтрован",
    "rsi_too_low": "RSI слишком низкий",
    "rsi_too_high": "RSI слишком высокий",
    "against_trend": "Против основного тренда",
}


def _fallback_reason(reason: str, lines: list[str]) -> None:
    text = _REASON_MAP.get(reason, reason)
    lines.append(f"• {text}")
