"""Классификатор контекста аукциона flowzone_bot (тренд vs баланс).

Канон STRATEGY §2 (ролик 00:33): прежде чем искать вход, классифицируем сценарий
по ФОРМЕ объёмного профиля:
- **Трендовый сценарий** — *«clear breakout of the previous level… they accepted
  after the breakout below the value area low… we are seeking new balance… what
  we can expect here is direction»*. То есть объём ПРИНЯТ (accepted) ВНЕ value
  area, и профиль ЭЛОНГИРОВАН в сторону принятия → ждём направленное продолжение.
- **Балансовый сценарий** — объём симметрично внутри/вокруг value area, акцепта
  за границами нет → входов по методике НЕ берём.

Research basis: Steidlmayer «Markets & Market Logic» (1989) / Jim Dalton «Mind
Over Markets» — чтение ФОРМЫ профиля: трендовый («elongated») профиль вытянут в
сторону, куда уходит value; балансовый («bell/normal») симметричен вокруг POC.
«Acceptance» = value торгуется и принимается ВНЕ прошлой value area (не одиночный
фитиль-проба), что проявляется как объём в ХВОСТЕ профиля за границей VA.

Контекст — это РЕЖИМ (куда мигрировала value), а НЕ мгновенная цена: измеряем по
самому накопленному профилю (дневной footprint, §6.3 «Dly Vol. Profile»), а не по
последним N секундам — поэтому режим СТАБИЛЕН на откате к зоне reload (канон:
вошёл во ВТОРОЕ движение, не в первое; первое движение = пробой+acceptance уже
сформировало хвост). Решение: из объёма, принятого ВНЕ value area (хвосты выше
VAH и ниже VAL), доля ≥ ``accept_frac`` на одной стороне → это направление
аукциона. ``accept_frac`` = 0.70 — каноничная Value-Area-константа (Steidlmayer/
Dalton: value area ≈70% принятого объёма; «acceptance» вне VA — это направленное
принятие той же грейд-доли), reversible через env (no-data-fitting.mdc).
"""
from __future__ import annotations

from dataclasses import dataclass

from flowzone_bot.analysis.volume_profile import VolumeProfile

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
    # доли объёма, ПРИНЯТОГО ВНЕ value area (от суммы обоих хвостов профиля):
    accept_above: float = 0.0   # доля хвоста выше VAH
    accept_below: float = 0.0   # доля хвоста ниже VAL
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


def classify(profile: VolumeProfile | None, last_price: float | None, *,
             accept_frac: float = 0.70) -> Context:
    """Определить контекст по ФОРМЕ профиля (Steidlmayer/Dalton, STRATEGY §2).

    Тренд = направленный acceptance ВНЕ value area: объём, принятый в ХВОСТАХ
    профиля (корзины ниже VAL / выше VAH), направленно перекошен. Из суммарного
    «вне-VA» объёма доля ≥ ``accept_frac`` на одной стороне → это направление
    аукциона; симметрично/слабо → BALANCE (не торгуем).

    Режим определяется по самому профилю (а не по последним N секундам), поэтому
    устойчив к откату цены к зоне reload (канон «второе движение»). ``accept_frac``
    = 0.70 — каноничная Value-Area-константа.
    """
    if profile is None or last_price is None:
        return Context(UNKNOWN, last_price=last_price)
    vah, val, poc = profile.vah, profile.val, profile.poc_price
    # Хвосты профиля = объём, принятый ВНЕ value area (корзины за её границами).
    # va_lo_idx/va_hi_idx — границы Value Area в индексах корзин (включительно).
    vol_below = sum(profile.bucket_volume(i) for i in profile.buckets
                    if i < profile.va_lo_idx)
    vol_above = sum(profile.bucket_volume(i) for i in profile.buckets
                    if i > profile.va_hi_idx)
    outside = vol_below + vol_above
    if outside <= 0:
        return Context(BALANCE, vah=vah, val=val, poc=poc, last_price=last_price)
    accept_above = vol_above / outside
    accept_below = vol_below / outside
    # Направленное принятие вне VA: доминирующий хвост ≥ accept_frac → тренд в его
    # сторону (профиль элонгирован туда). Иначе симметрия → баланс.
    if accept_below >= accept_frac and vol_below > vol_above:
        state = TREND_DOWN
    elif accept_above >= accept_frac and vol_above > vol_below:
        state = TREND_UP
    else:
        state = BALANCE
    return Context(state, vah=vah, val=val, poc=poc,
                   accept_above=accept_above, accept_below=accept_below,
                   last_price=last_price)
