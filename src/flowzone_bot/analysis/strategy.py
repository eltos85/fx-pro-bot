"""Пайплайн входа flowzone_bot: контекст → зона → absorption → Signal.

Канон STRATEGY §7 (детерминированный чеклист входа):
1. Контекст (§2): трендовый сценарий по ФОРМЕ per-SESSION профиля (acceptance
   вне value area). Нет → не торгуем.
2. Зона (§3): confluence факторов VP по направлению аукциона, построенных из
   per-SWING профиля (профиль ПРЕДЫДУЩЕЙ swing-точки, §3.4 «super strong
   area» = ≥3 факторов).
3. Алерт на зону, ждём подхода цены.
4. Подтверждение потоком в зоне: absorption контр-стороны (deep trades в теле
   свечи, «failed» контр-сторона).
5. Вход: лимитка в зоне; стоп ЗА зоной (масштаб 1-2-3/1-2-4/1-2-5 =
   far_edge + N × ширина зоны, §5.2); цель — ближайшая swing-точка (§5.3);
   R:R-фильтр ≥ 1:2 (канон Fabervaale: ролик cUTsoU-15Tc «1 to 2, 1 to 2.5»,
   chartfanatics «1:2.5 to 1:5» — swing-цель должна окупать риск, иначе TP не
   покрывает даже fees, кейс #468). 1:2 = канон-флор первоисточника; 2026-06-29
   возврат с 2.5 к 2.0 — крипто BTC/ETH/SOL тоньше NQ, R:R≥2.5 недостижимо
   (бот встал). Выход — полный на swing point; re-entry — отдельной сделкой на
   следующей зоне (§5.3, §8).
   ─── Research basis (Trade Management, видео «The Only Orderflow Guide»
   39:00) ─── после пробоя уровня поглощения — «put your stop loss to break
   even» (risk-free), затем трейл по order-flow-агрессии. BE-lock реализован в
   executor._maybe_be_lock: favourable ≥ zone_width → SL в entry±buffer
   (буфер sl_buffer_bps, anti-flicker). Никакой частичной фиксации — BE это не
   фиксация, а перемещение стопа (канон её не описывает как частичную).
   Источник: https://youtu.be/Pz8f0wWW12M (Fabervaale ENG, Trade Management).
   Trail по order-flow — стадия 2 (канон «this print a new one, you bring
   your stop loss here»).

Только по направлению аукциона (continuation, §5.4) — контртренд не торгуем.
``evaluate`` — чистая (по снапшоту + per-swing профилю + контексту), тестируется
отдельно.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from flowzone_bot.analysis.context import Context
from flowzone_bot.analysis.hook import detect_hook
from flowzone_bot.analysis.orderflow import (big_trade_threshold,
                                             detect_absorption,
                                             detect_initiative)
from flowzone_bot.analysis.swings import Swing, nearest_swing_target
from flowzone_bot.analysis.volume_profile import VolumeProfile
from flowzone_bot.analysis.zone import Zone, build_zones
from flowzone_bot.data.aggregates import SymbolSnapshot


@dataclass
class Signal:
    symbol: str
    side: str                 # long | short (continuation)
    entry_ref: float          # цена лимитки в зоне
    sl_level: float           # стоп ЗА зоной (far_edge + N × ширина зоны)
    tp_level: float           # цель = ближайшая swing-точка (канон §5.3)
    score: int                # confluence-score зоны
    reasons: list[str] = field(default_factory=list)
    strategy: str = "flowzone"
    zone_low: float = 0.0
    zone_high: float = 0.0
    entry_order_type: str | None = None


def _finalize(snap: SymbolSnapshot, context: Context, side: str, *, cfg,
              swings: list[Swing] | None, sl: float, score: int,
              zone_low: float, zone_high: float,
              setup: str, extra_reasons: list[str]) -> Signal | None:
    """Общий хвост обоих канон-сетапов: swing-цель, геометрия, R:R-фильтр.

    Канон §5.3 — цель всегда ближайшая swing-точка по тренду; §5.1 — R:R не
    ниже 1:2 («The Simplest Orderflow Trading Model»: *«maybe it's 1 to 2, one
    to 2.5»*, потолок 1:5 из «The Only Orderflow Guide» 40:01).
    """
    last = snap.last_price
    if last is None:
        return None
    tp = nearest_swing_target(swings or [], side, last)
    if tp is None:
        return None
    if (side == "short" and not (sl > last > tp)) or \
       (side == "long" and not (sl < last < tp)):
        return None  # геометрия сделки невалидна (защита)
    reward = abs(tp - last)
    risk = abs(sl - last)
    if risk <= 0 or reward / risk < cfg.min_rr:
        return None
    reasons = [f"ctx={context.state}", f"shape={context.shape}",
               f"setup={setup}", "tp=swing", f"rr={reward / risk:.1f}"]
    reasons += extra_reasons
    return Signal(symbol=snap.symbol, side=side, entry_ref=last, sl_level=sl,
                  tp_level=tp, score=score, reasons=reasons,
                  zone_low=zone_low, zone_high=zone_high)


def evaluate(snap: SymbolSnapshot, context: Context,
             zone_profile: VolumeProfile | None, *, cfg,
             swings: list[Swing] | None = None,
             hook_prints: list | None = None) -> Signal | None:
    """Прогнать чеклист входа. Возвращает Signal или None.

    ``context`` — залатченный контекст аукциона (per-SESSION профиль, §2).
    ``zone_profile`` — per-swing профиль ПРЕДЫДУЩЕЙ swing-точки (канон §3:
    исполненный поток в окне [ts prev swing, now]); из него строятся зоны.
    ``swings`` — подтверждённые swing-точки M5 (канон §5.3): цель = ближайшая
    swing по тренду. Если swing-цели нет — сделка НЕ берётся (канон: цель
    всегда swing point, никаких структурных фолбэков).
    ``hook_prints`` — persisted-поток для сетапа hook (C5, §4.2): failed auction
    у границы value area разворачивается дольше 5-минутного окна снапшота.

    Порядок канон-сетапов: сперва основной reload (зона конфлюэнса +
    подтверждение потоком, §3-§4), затем hook / failed auction (§4.2).
    """
    side = context.trade_side
    if side is None or snap.last_price is None:
        return None  # шаг 1: нет трендового контекста
    sig = _evaluate_reload(snap, context, zone_profile, cfg=cfg, swings=swings)
    if sig is not None:
        return sig
    if getattr(cfg, "hook_enabled", False):
        return _evaluate_hook(snap, context, cfg=cfg, swings=swings,
                              hook_prints=hook_prints)
    return None


def _evaluate_hook(snap: SymbolSnapshot, context: Context, *, cfg,
                   swings: list[Swing] | None,
                   hook_prints: list | None) -> Signal | None:
    """Сетап hook / failed auction (C5, канон 26:17 и 27:20).

    Цена отвергнута за границей value area и вернулась внутрь → вход в сторону
    аукциона. Стоп — за экстремумом неудачной вылазки: принятие цены снаружи
    опровергает тезис «failed auction». «Зоной» сделки записывается сама
    область отвержения (канон: *«all this it's a rejection area»*).
    """
    side = context.trade_side
    last = snap.last_price
    if side is None or last is None or not hook_prints:
        return None
    hook = detect_hook(hook_prints, side, vah=context.vah, val=context.val,
                       last_price=last, value_area_pct=cfg.value_area_pct)
    if not hook.confirmed:
        return None
    buf = cfg.sl_buffer_bps / 10000.0 * last
    if side == "long":
        sl = hook.extreme - buf
        zone_low, zone_high = hook.extreme, hook.boundary
    else:
        sl = hook.extreme + buf
        zone_low, zone_high = hook.boundary, hook.extreme
    return _finalize(snap, context, side, cfg=cfg, swings=swings, sl=sl,
                     score=cfg.zone_min_confluence, zone_low=zone_low,
                     zone_high=zone_high, setup="hook",
                     extra_reasons=hook.reasons)


def _evaluate_reload(snap: SymbolSnapshot, context: Context,
                     zone_profile: VolumeProfile | None, *, cfg,
                     swings: list[Swing] | None) -> Signal | None:
    """Основной канон-сетап: зона конфлюэнса + подтверждение потоком (§3-§4)."""
    side = context.trade_side
    if side is None or zone_profile is None or snap.last_price is None:
        return None  # нет трендового контекста / нет per-swing профиля
    last = snap.last_price
    profile = zone_profile

    big_thr = big_trade_threshold(snap.trades, pct=cfg.big_trade_pct,
                                  min_samples=cfg.big_trade_min_samples)

    # шаг 2: зоны конфлюэнса по направлению аукциона
    zones = build_zones(
        profile, side, last, snap.trades, big_threshold=big_thr,
        min_confluence=cfg.zone_min_confluence,
        cluster_ticks=cfg.zone_cluster_ticks,
        delta_min_frac=cfg.zone_delta_min_frac)
    if not zones:
        return None

    # шаг 3-4: цена ДОШЛА до зоны (внутри полосы) → проверяем absorption
    pad = profile.bucket_size * cfg.zone_cluster_ticks
    reached = [z for z in zones if z.contains(last, pad)]
    if not reached:
        return None  # цена ещё не в зоне — только алерт/ожидание
    zone: Zone = max(reached, key=lambda z: z.score)

    cut = snap.ts - cfg.absorption_window_sec
    window = [t for t in snap.trades if t.ts >= cut]
    absorption = detect_absorption(
        window, side, big_threshold=big_thr,
        min_counter_frac=cfg.absorption_min_counter_frac)
    # C2: подтверждение потоком — absorption ЛИБО initiative auction (канон
    # 37:03 перечисляет их как равноправные паттерны исполнения; «The Simplest
    # Orderflow Trading Model»: *«we can use this as a confirmation trigger to
    # go long… you can take a momentum trade»*). Absorption приоритетнее:
    # это основной reload-сетап §4, initiative — momentum-вариант, когда рынок
    # не даёт теста зоны с поглощением.
    if absorption.confirmed:
        trigger, trigger_reasons = "absorption", absorption.reasons
    else:
        trigger, trigger_reasons = None, []
        if getattr(cfg, "initiative_exhaustion_enabled", False):
            initiative = detect_initiative(
                window, side, big_threshold=big_thr,
                min_delta_frac=cfg.initiative_min_delta_frac)
            if initiative.confirmed:
                trigger, trigger_reasons = "initiative", initiative.reasons
    if trigger is None:
        return None  # зона без подтверждения потоком — не сигнал (§4, §8)

    # шаг 5: стоп ЗА зоной — канон «1-2-3 / 1-2-4 / 1-2-5» = far_edge зоны +
    # N × ширина зоны (§5.2). N = cfg.sl_zone_mult (1/2/3 — selectable
    # консервативность, «how much you want to be safe»). Небольшой анти-фитиль
    # буфер sl_buffer_bps — технический, не масштаб стопа.
    zone_width = zone.high - zone.low
    buf = cfg.sl_buffer_bps / 10000.0 * last
    beyond = max(zone_width * cfg.sl_zone_mult, buf)
    if side == "short":
        sl = zone.high + beyond
    else:
        sl = zone.low - beyond
    return _finalize(snap, context, side, cfg=cfg, swings=swings, sl=sl,
                     score=zone.score, zone_low=zone.low, zone_high=zone.high,
                     setup="reload",
                     extra_reasons=[f"zone={'+'.join(zone.factors)}",
                                    f"trigger={trigger}", *trigger_reasons])
