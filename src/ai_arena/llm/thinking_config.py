"""Маппинг env-параметра AI_ARENA_DEEPSEEK_REASONING → Anthropic-compat extra_body.

Вынесено в отдельный модуль (без зависимости от ``anthropic``-SDK), чтобы
unit-тесты могли импортировать функцию в окружении без ``anthropic``
(тестовый env только pure-Python deps).

Источник правды (правило api-docs.mdc):
- https://api-docs.deepseek.com/guides/thinking_mode
- https://api-docs.deepseek.com/guides/anthropic_api

Ключевое:
- **DeepSeek V4 thinking enabled by default** для всех V4-моделей.
  Чтобы получить Nof1-поведение (без CoT-блока) — нужно ЯВНО передать
  ``extra_body={"thinking": {"type": "disabled"}}``.
- Anthropic-compat НЕ поддерживает top-level ``reasoning_effort`` (это
  OpenAI-format поле). Effort задаётся через ``output_config.effort``.

Баг до 2026-05-18 (см. BUILDLOG_AI_ARENA.md): мы передавали
``extra_body={"reasoning_effort": "..."}``, что DeepSeek silent-игнорил.
Соответственно `AI_ARENA_DEEPSEEK_REASONING=off` 4+ дня работал как
**no-op**, и thinking всё время был активен с default effort=high.
Это противоречило инварианту ai-arena-sources.mdc «Nof1 не использует
reasoning» и могло влиять на качество решений.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def build_thinking_extra_body(reasoning_effort: str | None) -> dict:
    """Возвращает ``extra_body`` для ``anthropic.messages.create``.

    Поддерживаемые значения env (см. settings.py):
    - ``off``       → ``{"thinking": {"type": "disabled"}}`` (Nof1-режим,
      инвариант ai-arena-sources.mdc)
    - ``high`` / ``low`` / ``medium`` → enabled + ``effort=high``
      (low/medium DeepSeek сама мапит в high)
    - ``max`` / ``xhigh`` → enabled + ``effort=max``
    - неизвестное / пустое → off (безопасный дефолт)
    """
    mode = (reasoning_effort or "off").strip().lower()
    if mode == "off":
        return {"thinking": {"type": "disabled"}}
    if mode in ("high", "low", "medium"):
        return {
            "thinking": {"type": "enabled"},
            "output_config": {"effort": "high"},
        }
    if mode in ("max", "xhigh"):
        return {
            "thinking": {"type": "enabled"},
            "output_config": {"effort": "max"},
        }
    log.warning(
        "Unknown reasoning_effort=%r, falling back to disabled thinking",
        reasoning_effort,
    )
    return {"thinking": {"type": "disabled"}}
