"""Парсинг поля ``reasons`` (детерминированный аналог «llm_reason»).

У обоих ботов причина входа закодирована в ``score`` + ``reasons`` + ``strategy``
(TASKSPEC §3.1). Токены различаются по боту:

- **scalp_bot** (``analysis/signals.py``): плоский список
  ``["sweep","cvd_div","reclaim","mom","ob_imb","key_<level>"]``.
- **hybrid_bot** (``app/main.py``): причина закрытия одним токеном
  (``fix_threshold`` / ``trend_flat`` / ``broker_flat``) — у стратегии нет
  набора факторов входа (STRATEGY_HYBRID.md §17.4).

Нормализуем в плоский список «факторных» токенов для детектора factor_noise:
структурные токены раскладываем на атомарные факторы (``zone=a+b`` → ``zone:a``,
``zone:b``; ``ctx=down`` → ``ctx:down``). Это **наблюдение над данными**, не
влияет на торговлю.
"""
from __future__ import annotations


def parse_reasons(raw: str | None) -> list[str]:
    """Сырая строка ``reasons`` → список токенов как они записаны ботом.

    Бот пишет ``",".join(reasons)`` (см. оба state/db.py — поле TEXT). Пустую
    строку / None трактуем как отсутствие факторов.
    """
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def factor_tokens(raw: str | None) -> list[str]:
    """Атомарные факторные токены для аудита factor_noise.

    Раскладывает структурные токены (``k=v1+v2``) на ``k:v1``, ``k:v2``.
    Плоские токены остаются как есть. Дубликаты убираются (сохраняя порядок) —
    каждый фактор учитывается как присутствующий один раз.
    """
    out: list[str] = []
    seen: set[str] = set()
    for tok in parse_reasons(raw):
        for atom in _atomize(tok):
            if atom not in seen:
                seen.add(atom)
                out.append(atom)
    return out


def _atomize(tok: str) -> list[str]:
    if "=" in tok:
        key, _, val = tok.partition("=")
        key = key.strip()
        parts = [p.strip() for p in val.split("+") if p.strip()]
        if not parts:
            return [key]
        return [f"{key}:{p}" for p in parts]
    return [tok]
