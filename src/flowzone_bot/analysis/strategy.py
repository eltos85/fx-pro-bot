"""Пайплайн входа flowzone_bot: контекст → зона → absorption → Signal.

Канон STRATEGY §7 (детерминированный чеклист входа):
1. Контекст: трендовый сценарий (acceptance за VA). Нет → не торгуем.
2. Зона: confluence ≥2 факторов VP по направлению аукциона.
3. Алерт на зону, ждём подхода цены.
4. Подтверждение потоком в зоне: absorption контр-стороны (deep trades в теле
   свечи, «failed» контр-сторона).
5. Вход: лимитка в зоне; стоп ЗА зоной; цель — ближайшая структура (swing —
   фаза 5, сейчас ближайший POC / противоположная граница VA).

Только по направлению аукциона (continuation, §5.4) — контртренд не торгуем.
``evaluate`` — чистая (по снапшоту + профилю + контексту), тестируется отдельно.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from flowzone_bot.analysis.context import Context
from flowzone_bot.analysis.orderflow import big_trade_threshold, detect_absorption
from flowzone_bot.analysis.swings import Swing, swing_targets
from flowzone_bot.analysis.volume_profile import VolumeProfile
from flowzone_bot.analysis.zone import Zone, build_zones
from flowzone_bot.data.aggregates import SymbolSnapshot, TradePrint


@dataclass
class Signal:
    symbol: str
    side: str                 # long | short (continuation)
    entry_ref: float          # цена лимитки в зоне
    sl_level: float           # стоп ЗА зоной
    tp_level: float           # цель 1 (ближайший swing; фолбэк — структура)
    score: int                # confluence-score зоны
    reasons: list[str] = field(default_factory=list)
    strategy: str = "flowzone"
    zone_low: float = 0.0
    zone_high: float = 0.0
    entry_order_type: str | None = None
    tp2_level: float | None = None   # цель 2 (след. swing) для частичной фиксации


def _structural_target(profile: VolumeProfile, side: str,
                       entry: float) -> float | None:
    """Фолбэк-цель из VP-структуры, когда подтверждённой swing-точки нет
    (канон §5.3 «ближайший swing» — приоритет; structural — запасной вариант).

    Шорт (вниз): ближайший уровень НИЖЕ entry из {poc, val}. Лонг (вверх):
    ближайший ВЫШЕ entry из {poc, vah}. None если структуры в сторону цели нет.
    """
    if side == "short":
        cands = [p for p in (profile.poc_price, profile.val) if p < entry]
        return max(cands) if cands else None  # ближайшая снизу
    cands = [p for p in (profile.poc_price, profile.vah) if p > entry]
    return min(cands) if cands else None      # ближайшая сверху


def evaluate(snap: SymbolSnapshot, profile: VolumeProfile | None,
             context: Context, *, cfg,
             swings: list[Swing] | None = None) -> Signal | None:
    """Прогнать чеклист входа. Возвращает Signal или None.

    ``swings`` — подтверждённые swing-точки M5 (канон §5.3): цель 1 = ближайшая
    swing по тренду, цель 2 = следующая (частичная фиксация). Если swing-целей
    нет — фолбэк на структурную цель из VP.
    """
    side = context.trade_side
    if side is None or profile is None or snap.last_price is None:
        return None  # шаг 1: нет трендового контекста — не торгуем
    last = snap.last_price

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

    # шаг 5: стоп ЗА зоной (+буфер), цель — ближайшая структура
    buf = max(cfg.sl_buffer_bps, cfg.min_sl_bps) / 10000.0 * last
    if side == "short":
        sl = zone.high + buf
    else:
        sl = zone.low - buf
    # цель 1 = ближайший swing (канон §5.3); цель 2 = следующий swing (частичная
    # фиксация/reload). Фолбэк на VP-структуру, если swing-целей нет.
    targets = swing_targets(swings or [], side, last)
    tp = targets[0] if targets else _structural_target(profile, side, last)
    tp2 = targets[1] if len(targets) >= 2 else None
    if tp is None:
        return None  # нет цели ни по swing, ни по структуре — пропускаем
    if (side == "short" and not (sl > last > tp)) or \
       (side == "long" and not (sl < last < tp)):
        return None  # геометрия сделки невалидна (защита)

    tgt_src = "swing" if targets else "vp"
    reasons = [f"ctx={context.state}", f"zone={'+'.join(zone.factors)}",
               f"tp={tgt_src}"]
    reasons += absorption.reasons
    return Signal(symbol=snap.symbol, side=side, entry_ref=last, sl_level=sl,
                  tp_level=tp, score=zone.score, reasons=reasons,
                  zone_low=zone.low, zone_high=zone.high, tp2_level=tp2)
