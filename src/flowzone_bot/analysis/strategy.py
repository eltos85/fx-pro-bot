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
   far_edge + N × ширина зоны, §5.2); цель — ближайшая swing-точка (§5.3).
   Выход — полный на swing point; re-entry — отдельной сделкой на следующей
   зоне (§5.3, §8). Никакой частичной фиксации и структурного фолбэка —
   канон их не описывает (правило no-data-fitting.mdc).

Только по направлению аукциона (continuation, §5.4) — контртренд не торгуем.
``evaluate`` — чистая (по снапшоту + per-swing профилю + контексту), тестируется
отдельно.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from flowzone_bot.analysis.context import Context
from flowzone_bot.analysis.orderflow import big_trade_threshold, detect_absorption
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


def evaluate(snap: SymbolSnapshot, context: Context,
             zone_profile: VolumeProfile | None, *, cfg,
             swings: list[Swing] | None = None) -> Signal | None:
    """Прогнать чеклист входа. Возвращает Signal или None.

    ``context`` — залатченный контекст аукциона (per-SESSION профиль, §2).
    ``zone_profile`` — per-swing профиль ПРЕДЫДУЩЕЙ swing-точки (канон §3:
    исполненный поток в окне [ts prev swing, now]); из него строятся зоны.
    ``swings`` — подтверждённые swing-точки M5 (канон §5.3): цель = ближайшая
    swing по тренду. Если swing-цели нет — сделка НЕ берётся (канон: цель
    всегда swing point, никаких структурных фолбэков).
    """
    side = context.trade_side
    if side is None or zone_profile is None or snap.last_price is None:
        return None  # шаг 1: нет трендового контекста / нет per-swing профиля
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
    if not absorption.confirmed:
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
    # цель = ближайший swing по тренду (канон §5.3 «targeting for a swing
    # point»). Нет swing-цели → нет сделки (канон не предусматривает иного).
    tp = nearest_swing_target(swings or [], side, last)
    if tp is None:
        return None
    if (side == "short" and not (sl > last > tp)) or \
       (side == "long" and not (sl < last < tp)):
        return None  # геометрия сделки невалидна (защита)

    reasons = [f"ctx={context.state}", f"zone={'+'.join(zone.factors)}",
               "tp=swing"]
    reasons += absorption.reasons
    return Signal(symbol=snap.symbol, side=side, entry_ref=last, sl_level=sl,
                  tp_level=tp, score=zone.score, reasons=reasons,
                  zone_low=zone.low, zone_high=zone.high)
