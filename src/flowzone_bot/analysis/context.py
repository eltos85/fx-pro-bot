"""Классификатор контекста аукциона flowzone_bot (тренд vs баланс).

Канон STRATEGY §2: прежде чем искать вход, классифицируем сценарий по форме
профиля и положению цены относительно Value Area:
- **Трендовый сценарий** — чистый пробой + **acceptance за границей VA**: цена
  торгуется и ПРИНИМАЕТСЯ ниже VAL (шорт-сценарий) или выше VAH (лонг-сценарий)
  → рынок ищет новый баланс, ждём направленное продолжение.
- **Балансовый сценарий** — цена внутри VA, акцепта за границами нет → входов
  по методике НЕ берём.

Research basis: Jim Dalton «Mind Over Markets» — «acceptance» = value торгуется
и принимается ВНЕ прошлой value area (не одиночный фитиль-проба). Контекст — это
РЕЖИМ (произошёл ли acceptance), а НЕ мгновенное положение цены: при откате цена
возвращается к зоне reload, но объём окна всё ещё печатается за прошлой границей
VA — направление аукциона сохраняется. Поэтому тренд определяем по тому, ГДЕ
торгуется объём: большинство (≥ accept_frac) объёма окна ниже VAL → аукцион вниз
(шорт-сценарий), выше VAH → вверх. 0.5 = нейтральная граница «большинства»
(reversible через env, валидация на форвард-тесте; no-data-fitting.mdc).
"""
from __future__ import annotations

from dataclasses import dataclass

from flowzone_bot.analysis.volume_profile import VolumeProfile
from flowzone_bot.data.aggregates import TradePrint

# Состояния контекста аукциона.
TREND_UP = "trend_up"
TREND_DOWN = "trend_down"
BALANCE = "balance"
UNKNOWN = "unknown"


@dataclass
class Context:
    state: str
    vah: float | None = None
    val: float | None = None
    poc: float | None = None
    accept_above: float = 0.0   # доля объёма окна, напечатанного выше VAH
    accept_below: float = 0.0   # доля объёма окна, напечатанного ниже VAL
    last_price: float | None = None

    @property
    def is_trend(self) -> bool:
        return self.state in (TREND_UP, TREND_DOWN)

    @property
    def trade_side(self) -> str | None:
        """Сторона continuation-входа по направлению аукциона (STRATEGY §1):
        тренд вверх → ищем лонг, вниз → шорт. Баланс → None (не торгуем)."""
        if self.state == TREND_UP:
            return "long"
        if self.state == TREND_DOWN:
            return "short"
        return None


def classify(profile: VolumeProfile | None, recent_trades: list[TradePrint],
             last_price: float | None, *, accept_frac: float = 0.5) -> Context:
    """Определить контекст по профилю VA + acceptance свежего потока.

    ``recent_trades`` — принты за окно acceptance (отфильтрованы вызывающим).
    ``accept_frac`` — минимальная доля объёма окна за границей VA для «принятия».
    """
    if profile is None or last_price is None:
        return Context(UNKNOWN, last_price=last_price)
    vah, val, poc = profile.vah, profile.val, profile.poc_price
    total = sum(t.size for t in recent_trades)
    if total <= 0:
        return Context(BALANCE, vah=vah, val=val, poc=poc, last_price=last_price)
    vol_above = sum(t.size for t in recent_trades if t.price > vah)
    vol_below = sum(t.size for t in recent_trades if t.price < val)
    accept_above = vol_above / total
    accept_below = vol_below / total
    # Тренд по расположению объёма окна относительно прошлой VA (не по последней
    # цене — она может быть на откате к зоне reload). При двусторонней неоднознач-
    # ности (оба ≥ frac) приоритет большей доле.
    if accept_above >= accept_frac and accept_above >= accept_below:
        state = TREND_UP
    elif accept_below >= accept_frac and accept_below > accept_above:
        state = TREND_DOWN
    else:
        state = BALANCE
    return Context(state, vah=vah, val=val, poc=poc,
                   accept_above=accept_above, accept_below=accept_below,
                   last_price=last_price)
