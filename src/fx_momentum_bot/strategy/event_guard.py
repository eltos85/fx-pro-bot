"""Event-guard: блок НОВЫХ входов вокруг HIGH-impact макро-релизов.

Контекст (BUILDLOG.md 2026-06-10): VP-шорт по золоту открыт в 12:22 UTC —
за 8 минут до US CPI (12:30 UTC). Релиз дал спайк +24 пункта за минуты,
полный стоп −$24.70; ре-вход в 13:28 на раздутом пост-релизном ATR →
ещё −$32.61. 90% дневного убытка — две сделки в окне одного релиза.

─── Research basis ───
- Andersen/Bollerslev/Diebold/Vega (2003, AER) «Micro Effects of Macro
  Announcements»: скачок цены и волатильности в первые минуты после
  макро-анонсов; то же исследование — основа news-фильтра ±4ч в
  OutsidersStrategy (STRATEGIES.md).
- Dalton «Mind Over Markets» (2007): запланированные экономические
  релизы «сбрасывают» аукцион — структура профиля (VA/POC) до релиза
  не описывает рынок после; день-таймфрейм трейдер стоит в стороне.
- Окно ±60 мин: пик реакции — первые минуты, повышенная волатильность
  и широкий спред держатся десятки минут (Andersen et al., fig. 1-2).
  Уже, чем ±4ч у Outsiders (mean-reversion чувствительнее к
  послерелизному тренду, чем breakout/momentum).

Блокируются только ВХОДЫ. Сопровождение (BE/partial/trailing),
sign-decay выход и SL продолжают работать: канон управления риском
важнее канона входа.

Календарь переиспользуется из fx_ai_trader.data.econ_calendar — это
shared-инфраструктура (фактические даты релизов, не торговый параметр),
аналогично тому, как fx_ai_trader переиспользует cTrader-движок
fx_pro_bot (см. strategy-guard.mdc, «Изоляция кодовых баз»).
Покрытие: US CPI (static 2026, BLS), FOMC decision (static 2026, Fed),
NFP (правило: первая пятница 08:30 ET). PPI/Retail Sales/GDP НЕ покрыты.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fx_ai_trader.data.econ_calendar import upcoming_events

log = logging.getLogger(__name__)


def high_impact_event_near(
    now_utc: datetime | None = None,
    *,
    before_min: int = 60,
    after_min: int = 60,
) -> str | None:
    """Описание HIGH-impact события в окне [now−after, now+before], либо None.

    None == входы разрешены. Строка == входы блокируются (текст для лога).
    Сбой календаря НЕ блокирует торговлю (guard — защита, не зависимость).
    """
    now = now_utc or datetime.now(timezone.utc)
    try:
        # ref сдвинут назад на after_min: upcoming_events отдаёт только
        # события >= ref, а нам нужны и недавно ВЫШЕДШИЕ релизы
        # (пост-релизная волатильность, кейс ре-входа 13:28).
        ref = now - timedelta(minutes=after_min)
        horizon_hours = (before_min + after_min) / 60.0 + 0.1
        events = upcoming_events(ref, ("*",), horizon_hours=horizon_hours)
    except Exception:
        log.exception("event_guard: calendar failed (входы НЕ блокирую)")
        return None
    for e in events:
        if e.impact != "HIGH":
            continue
        delta_min = (e.when_utc - now).total_seconds() / 60.0
        if -after_min <= delta_min <= before_min:
            when = (
                f"in {delta_min:.0f}min" if delta_min >= 0
                else f"{-delta_min:.0f}min ago"
            )
            return f"{e.name} {when}"
    return None
