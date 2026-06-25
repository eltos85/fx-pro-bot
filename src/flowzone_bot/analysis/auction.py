"""Sticky-трекер направления аукциона flowzone_bot (STRATEGY §2, §5.4).

─── Research basis / канон ───
Ролик (Fabervaale ENG «How To Find The BEST Entry Zones», 00:33–06:00):
- *«London session start with **clear breakout of the previous level**… they
  **accepted after the breakout** below the value area low… what we can expect
  here is **direction**.»* — направление аукциона задаётся ПОДТВЕРЖДЁННЫМ
  ПРОБОЕМ предыдущего уровня + acceptance вне value area.
- *«I **didn't take the first movement**… but the **second movement was so
  clear**.»* — встречное направление берём не на первом откате, а лишь когда
  структура реально сломана.
- Оба примера ролика — continuation-ШОРТЫ в нисходящем аукционе: автор
  перезаряжает шорт по новым dealing range и НИ РАЗУ не переворачивается в лонг
  внутри down-аукциона.

─── Зачем нужен ───
Мгновенный ``context.classify`` читает форму ДНЕВНОГО footprint-профиля. При
внутридневном откате value area мигрирует, встречный хвост перевешивает →
classify флапает в противоположный тренд → бот берёт continuation в контртренд
(подтверждено forward-статистикой: лонги-перевороты системно убыточны, шорты по
тренду — нет). Канон этого НЕ делает: направление держится (continuation), смена
— только при встречном СТРУКТУРНОМ пробое.

─── Логика (детерминированная, без новых magic-порогов) ───
Направление ЛАТЧИТСЯ на символ (якорь — UTC-день, как и сам профиль §6.3) и
адоптируется/переворачивается ТОЛЬКО когда выполнено ОБА канон-условия в ту
сторону:
  (1) acceptance вне VA в эту сторону (мгновенный ``classify`` = trend в неё), И
  (2) **структурный пробой предыдущего уровня** — цена пробила последний
      подтверждённый swing-экстремум (Williams-фрактал, §5.3) в эту сторону.
Пока оба не выполнены — держим прежнее направление (sticky: откат/баланс/
неподтверждённый встречный хвост направление НЕ сбрасывают). Это и есть «второе
ясное движение», а не «первое».
"""
from __future__ import annotations

import time

from flowzone_bot.analysis.context import (BALANCE, TREND_DOWN, TREND_UP,
                                           UNKNOWN, Context)
from flowzone_bot.analysis.swings import Swing


def _recent_extreme(swings: list[Swing], kind: str) -> float | None:
    """Цена последнего подтверждённого swing-экстремума заданного типа
    ('high'|'low') = «предыдущий уровень» канона. None если такого нет."""
    cands = [s for s in swings if s.kind == kind]
    if not cands:
        return None
    return max(cands, key=lambda s: s.idx).price


class AuctionTracker:
    """Латчит направление аукциона по символу; переворот — только по канон-
    условиям (acceptance + структурный пробой предыдущего уровня)."""

    def __init__(self, *, wall_now=time.time) -> None:
        self._wall_now = wall_now
        # symbol → (utc_day, latched_direction)
        self._dir: dict[str, str] = {}
        self._day: dict[str, int] = {}

    def peek(self, symbol: str) -> str | None:
        """Текущее залатченное направление (для heartbeat). None если не задано."""
        d = self._dir.get(symbol)
        return d if d in (TREND_UP, TREND_DOWN) else None

    def update(self, symbol: str, inst: Context, last_price: float | None,
               swings: list[Swing], *, now: float | None = None) -> Context:
        """Обновить латч по мгновенному контексту ``inst`` и структуре ``swings``.
        Возвращает Context с залатченным ``state`` (поля VA/acc — из ``inst``)."""
        wall = now if now is not None else self._wall_now()
        day = int(wall // 86400)
        if self._day.get(symbol) != day:  # новый UTC-день → профиль сброшен, латч тоже
            self._day[symbol] = day
            self._dir[symbol] = UNKNOWN

        cur = self._dir.get(symbol, UNKNOWN)
        want = inst.state  # мгновенный режим по форме дневного профиля

        # структурный пробой предыдущего уровня (канон «clear breakout»)
        broke_up = broke_down = False
        if last_price is not None:
            hi = _recent_extreme(swings, "high")
            lo = _recent_extreme(swings, "low")
            broke_up = hi is not None and last_price > hi
            broke_down = lo is not None and last_price < lo

        new_dir = cur
        if cur in (TREND_UP, TREND_DOWN):
            # держим направление; переворот ТОЛЬКО при встречном пробое+acceptance
            if cur == TREND_DOWN and want == TREND_UP and broke_up:
                new_dir = TREND_UP
            elif cur == TREND_UP and want == TREND_DOWN and broke_down:
                new_dir = TREND_DOWN
            # иначе (откат/баланс/неподтверждённый встречный хвост) — sticky
        else:  # UNKNOWN/BALANCE → устанавливаем первое направление дня по пробою
            if want == TREND_UP and broke_up:
                new_dir = TREND_UP
            elif want == TREND_DOWN and broke_down:
                new_dir = TREND_DOWN
            else:
                new_dir = BALANCE  # нет подтверждённого пробоя — не торгуем

        self._dir[symbol] = new_dir
        return Context(state=new_dir, vah=inst.vah, val=inst.val, poc=inst.poc,
                       accept_above=inst.accept_above,
                       accept_below=inst.accept_below, last_price=last_price)
