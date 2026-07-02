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

``classify`` — МГНОВЕННЫЙ режим по накопленному per-SESSION профилю (§2, §6.1).
Решение: из объёма, принятого ВНЕ value area (хвосты выше VAH и ниже VAL), доля
≥ ``accept_frac`` на одной стороне → это направление аукциона.
``accept_frac`` = 0.68 — канон-автор Fabervaale: Value Area = 68% объёма (видео
«The Only Orderflow Guide» 28:50 *«68% of the volume»*; winkler-rulebook);
«acceptance» вне VA = направленное принятие той же грейд-доли (68%), reversible
через env (no-data-fitting.mdc).

ВАЖНО (A3): ``classify`` — чистая функция по ФОРМЕ профиля; она НЕ проверяет
«clear breakout of the previous level» (канон §2). breakout-гейт вынесен в
``auction.AuctionTracker.update`` — направление устанавливается/переворачивается
ТОЛЬКО при наличии swings-пробоя предыдущего уровня. На торговом пути (main)
всегда используется ``auction.update(classify(...))``, поэтому фактический
вход требует breakout+acceptance (канон). ``classify`` без AuctionTracker —
только для heartbeat-дисплея и юнит-тестов формы профиля.

ВАЖНО: мгновенный режим ФЛАПАЕТ при внутридневной миграции VA (отскок строит
встречный хвост). Канон держит направление аукциона (continuation) и меняет его
только по встречному структурному пробою — это ``auction.AuctionTracker``
(латч + «второе движение»). ``classify`` остаётся чистой функцией (формы
профиля), а латч-логика — поверх неё, чтобы обе части тестировались раздельно.
"""
from __future__ import annotations

from dataclasses import dataclass

from flowzone_bot.analysis.volume_profile import (
    VolumeProfile,
    find_hvn_lvn,
)

# Состояния контекста аукциона.
TREND_UP = "trend_up"
TREND_DOWN = "trend_down"
BALANCE = "balance"
UNKNOWN = "unknown"

# Форма профиля (D4, канон-нюансы — Dalton/Steidlmayer + «The Only Orderflow
# Guide»). НЕ гейтит вход (обогащение `ctx.shape`); вход гейтит бинарный
# trend/balance `state` как прежде (no-data-fitting.mdc / strategy-guard.mdc).
P_SHAPE_UP = "p_shape_up"            # тяжёлый верхний хвост + buy-delta → bullish next
P_SHAPE_DOWN = "p_shape_down"        # тяжёлый нижний хвост + sell-delta → bearish next
DOUBLE_DISTRIBUTION = "double_distribution"  # два HVN-кластера через LVN-перешеек
BALANCE_SHAPE = "balance_shape"      # симметричный, хвосты слабые
NORMAL = "normal"                    # колокол вокруг POC, хвостов нет


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
    # Форма профиля (D4) — обогащение, НЕ гейтит вход. UNKNOWN если профиля нет.
    shape: str = UNKNOWN

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


def classify_shape(profile: VolumeProfile | None, accept_above: float,
                   accept_below: float, *, accept_frac: float = 0.68) -> str:
    """Форма профиля (D4, Dalton «Mind Over Markets» + канон-автор).

    Паттерны:
    - **P-shape** — тяжёлый хвост в одну сторону + направленная дельта в нём →
      directional next period (канон *«P-shape… aggressive buyers… directional
      next day»*). `P_SHAPE_UP` = тяжёлый верхний хвост с buy-доминантой;
      `P_SHAPE_DOWN` = тяжёлый нижний хвост с sell-доминантой.
    - **Double distribution** — два HVN-кластера, разделённых LVN-перешейком →
      два dealing range, быстрый проход по LVN.
    - **Balance** — хвосты слабые (< accept_frac с обеих сторон), симметрия.
    - **Normal** — колокол вокруг POC, хвостов вне VA нет.

    НЕ гейтит вход (обогащение); вход определяется бинарным `classify` (trend vs
    balance по acceptance вне VA).
    """
    if profile is None:
        return UNKNOWN
    hvn, lvn = find_hvn_lvn(profile)
    # Double distribution: ≥2 HVN с LVN-перешейком между ними.
    if len(hvn) >= 2:
        lo_h, hi_h = min(hvn), max(hvn)
        if any(lo_h < lv < hi_h for lv in lvn):
            return DOUBLE_DISTRIBUTION
    # P-shape: доминирующий хвост + направленная дельта в хвосте.
    if accept_below >= accept_frac:
        tail_delta = sum(profile.bucket_delta(i) for i in profile.buckets
                         if i < profile.va_lo_idx)
        if tail_delta < 0:   # sell-доминанта в нижнем хвосте → bearish
            return P_SHAPE_DOWN
    if accept_above >= accept_frac:
        tail_delta = sum(profile.bucket_delta(i) for i in profile.buckets
                         if i > profile.va_hi_idx)
        if tail_delta > 0:   # buy-доминанта в верхнем хвосте → bullish
            return P_SHAPE_UP
    outside = accept_above + accept_below
    if outside <= 0:
        return NORMAL
    if accept_above < accept_frac and accept_below < accept_frac:
        return BALANCE_SHAPE
    return NORMAL


def classify(profile: VolumeProfile | None, last_price: float | None, *,
             accept_frac: float = 0.68,
             value_area_pct: float = 0.68) -> Context:
    """Определить контекст по ФОРМЕ профиля (Steidlmayer/Dalton, STRATEGY §2).

    Тренд = направленный acceptance ВНЕ value area: объём, принятый в ХВОСТАХ
    профиля (корзины ниже VAL / выше VAH), направленно перекошен. Из суммарного
    «вне-VA» объёма доля ≥ ``accept_frac`` на одной стороне → это направление
    аукциона; симметрично/слабо → BALANCE (не торгуем).

    Материальность acceptance: доминирующий хвост дополнительно обязан держать
    ≥ ``(1 − value_area_pct) / 2`` ОБЩЕГО объёма (при канон-VA 68% → 16%) —
    нейтральный симметричный колокол держит ровно столько вне VA НА ОДНУ
    сторону; «принятие» в направлении = хвост как минимум не меньше нейтральной
    одно-сторонней массы, но собранный на одной стороне. Иначе колокол с 1-2
    случайными принтами за VA давал бы «тренд» по шуму (VA-алгоритм к тому же
    overshoot-ит номинал → хвосты меньше 32%). Порог выведен из канон-константы
    68% VA + симметрии, без нового magic-number (no-data-fitting.mdc).

    Это МГНОВЕННЫЙ режим (форма дневного профиля); он флапает при миграции VA на
    откате. Удержание направления (канон «второе движение») — в
    ``auction.AuctionTracker`` поверх этой функции. ``accept_frac`` = 0.68 —
    канон-автор Fabervaale (Value Area = 68% объёма).
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
        return Context(BALANCE, vah=vah, val=val, poc=poc, last_price=last_price,
                       shape=classify_shape(profile, 0.0, 0.0, accept_frac=accept_frac))
    accept_above = vol_above / outside
    accept_below = vol_below / outside
    # Минимальная материальность acceptance: доминирующий хвост ≥ нейтральной
    # одно-сторонней вне-VA массы (1 − value_area_pct)/2 общего объёма.
    min_tail = (1.0 - value_area_pct) / 2.0 * profile.total_volume
    # Направленное принятие вне VA: доминирующий хвост ≥ accept_frac → тренд в его
    # сторону (профиль элонгирован туда). Иначе симметрия → баланс.
    if accept_below >= accept_frac and vol_below > vol_above and vol_below >= min_tail:
        state = TREND_DOWN
    elif accept_above >= accept_frac and vol_above > vol_below and vol_above >= min_tail:
        state = TREND_UP
    else:
        state = BALANCE
    return Context(state, vah=vah, val=val, poc=poc,
                   accept_above=accept_above, accept_below=accept_below,
                   last_price=last_price,
                   shape=classify_shape(profile, accept_above, accept_below,
                                        accept_frac=accept_frac))
