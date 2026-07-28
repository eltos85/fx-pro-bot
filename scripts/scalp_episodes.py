"""Схлопывание canon-теней в независимые эпизоды (read-only helper).

Зачем
-----
``canon_rejection_shadow`` до v0.18.47 писал кандидата на КАЖДОМ тике, пока
детектор держал сетап валидным, а не один раз на свип. Замер 2026-07-28:
3076 строк за 1.7 суток = 120 независимых эпизодов, 25.6 строки на эпизод,
77.7% соседних кандидатов в 1–5 секундах друг от друга.

Наблюдения внутри эпизода почти полностью скоррелированы (тот же символ, та же
сторона, тот же уровень, разница входа доли базисного пункта), но Wilson-CI и
пороги sample-size считают их независимыми. Итог — фиктивная уверенность:
WR по строкам 48.1% при n=2260, по эпизодам 33.7% при n=104. Разбивка по
возрасту уровня давала pdh 94.7% против 3.5% в соседнем ведре — невозможный
для настоящей закономерности разброс, типичная подпись дублей.

Эмиссия починена в v0.18.47, но уже накопленные строки останутся в БД, поэтому
отчёты обязаны схлопывать эпизоды сами. На данных после фикса схлопывание —
no-op: там уже одна строка на эпизод.

Правила
-------
* Эпизод = (symbol, side, level_type, level_price) + разрыв ≤ ``WINDOW_SEC``.
* ``WINDOW_SEC`` = 3600с — то же окно, что у эмиссии
  (``cfg.sl_cooldown_for('sweep_fade_canon')``, откалибровано в v0.18.14 на
  sweep n=829). Держим значения синхронными: разные окна дали бы разные n у
  живого сбора и у отчёта.
* Представитель эпизода — ПЕРВАЯ строка по ``ts_candidate``. Именно её взял бы
  бот; выбор «первой решённой» подтянул бы выборку вверх за счёт пропуска
  неразрешённых эпизодов, то есть внёс бы systematic bias.

Схлопывать нужно только canon-тени. У ``density_break_v2_shadow`` ключ содержит
``break_ts``, у ``density_bounce_persist_shadow`` — ``track_key``, у ``sl_widen``
и ``maker_nonfill`` — id исходной сделки: там дублей нет, и схлопывание по
уровню склеило бы РАЗНЫЕ пробои одной стены.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

WINDOW_SEC = 3600.0
DEDUPED_SETUP_TYPES = ("canon_rejection_shadow",)


def _key(row: Any) -> tuple:
    def get(name: str):
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return getattr(row, name, None)

    price = get("level_price")
    return (
        get("symbol"), get("side"),
        str(get("level_type") or "unknown"),
        f"{float(price):.10g}" if price is not None else "none",
    )


def collapse_episodes(rows: Iterable[Any],
                      window: float = WINDOW_SEC) -> list[Any]:
    """Оставить по одной строке на независимый эпизод.

    ``rows`` — записи ``counterfactual_setups`` с полями ``symbol``, ``side``,
    ``level_type``, ``level_price``, ``ts_candidate``. Порядок входа значения
    не имеет: сортируем сами. Возврат — представители эпизодов в хронологии.
    """
    ordered: Sequence[Any] = sorted(rows, key=lambda r: float(r["ts_candidate"]))
    last: dict[tuple, float] = {}
    out: list[Any] = []
    for row in ordered:
        ts = float(row["ts_candidate"])
        key = _key(row)
        prev = last.get(key)
        if prev is not None and ts - prev <= window:
            # Тот же свип, что и представитель: обновляем хвост эпизода, чтобы
            # серия тиков не рвалась на части, но строку не берём.
            last[key] = ts
            continue
        last[key] = ts
        out.append(row)
    return out


def episode_counts(rows: Iterable[Any], window: float = WINDOW_SEC
                   ) -> tuple[int, int]:
    """(строк, эпизодов) — для диагностики степени дублирования."""
    materialized = list(rows)
    return len(materialized), len(collapse_episodes(materialized, window))
