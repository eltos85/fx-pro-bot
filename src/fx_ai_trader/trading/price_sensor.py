"""Phase 2 (2026-05-29): событийный датчик «locked-profit достигнут».

Поверх живого spot-стрима (Phase 1) даёт event-driven реакцию: как
только открытая позиция доходит до зоны locked-profit (≥ threshold_r),
датчик инициирует внеплановый review-цикл — НЕ дожидаясь планового
5-минутного. Это ловит спайк в прибыль, который иначе мог откатиться
внутри review-окна.

Что датчик НЕ делает (важно для strategy-guard.mdc):
- НЕ открывает позиции, НЕ меняет SL/TP, НЕ закрывает сам.
- НЕ меняет exit-правила: решение по-прежнему принимает LLM в
  review-цикле (Phase 0 guardian: close ТОЛЬКО на locked-profit ≥1.5R).
- Только меняет *когда* запускается review — добавляет точки реакции
  поверх таймера (изначальная цель dual-timer: «3× больше точек
  реакции»). Это execution-timing, не торговая логика.

Защита от шума и расходов:
- rising-edge: срабатывает на *входе* в зону (≥ threshold_r), затем
  «обезоруживается» пока R не упадёт ниже (threshold_r − hysteresis_r).
  Без этого позиция, висящая на +1.6R, дёргала бы review бесконечно.
- cooldown_sec: минимум времени между event-review.
- max_events_per_hour: жёсткий потолок внеплановых review за час.

Research basis:
- Sutton & Barto «Reinforcement Learning» (2018) §3 — event-driven
  реакция на изменение состояния среды эффективнее фиксированного
  опроса при разреженных значимых событиях.
- Lopez de Prado «Advances in Financial ML» (2018) ch.2 «event-based
  sampling» (CUSUM/threshold) — сэмплировать по значимым ценовым
  событиям, а не по календарному времени.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable


def compute_unrealised_r(
    side: str, entry_price: float, sl_price: float | None, price: float | None,
) -> float | None:
    """Нереализованный результат позиции в единицах R (риск = |entry − SL|).

    Returns None если нет SL / цены / вырожденный риск (нельзя посчитать R).
    BUY: (price − entry) / risk. SELL: (entry − price) / risk.
    """
    if price is None or sl_price is None:
        return None
    if entry_price <= 0:
        return None
    risk = abs(entry_price - sl_price)
    if risk <= 0:
        return None
    if side.upper() == "BUY":
        pnl = price - entry_price
    else:
        pnl = entry_price - price
    return pnl / risk


@dataclass
class EventDecision:
    fire: bool
    positions: list[tuple[int, float]] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)  # human-readable причины
    throttled: bool = False  # заблокировано cooldown'ом
    rate_capped: bool = False  # упёрлись в max_events_per_hour


class LockedProfitSensor:
    """Rising-edge детектор входа позиции в зону locked-profit (≥ threshold_r).

    Stateful: помнит «armed» статус каждой позиции и историю срабатываний.
    Потокобезопасность не требуется — вызывается из единственного main-loop.
    """

    def __init__(
        self,
        threshold_r: float = 1.5,
        hysteresis_r: float = 0.3,
        cooldown_sec: float = 120.0,
        max_events_per_hour: int = 6,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold_r = threshold_r
        self.hysteresis_r = hysteresis_r
        self.cooldown_sec = cooldown_sec
        self.max_events_per_hour = max_events_per_hour
        self._now = now
        self._armed: dict[int, bool] = {}
        self._event_times: deque[float] = deque()
        self._last_event_ts: float = -math.inf

    def evaluate(self, positions: list[tuple[int, float | None]]) -> EventDecision:
        """positions: список (position_id, unrealised_R | None).

        Возвращает EventDecision.fire=True если нужно запустить внеплановый
        review (и какие позиции его спровоцировали).
        """
        now = self._now()
        present_ids: set[int] = set()
        candidates: list[tuple[int, float]] = []

        for pos_id, r in positions:
            present_ids.add(pos_id)
            armed = self._armed.setdefault(pos_id, True)
            if r is None:
                continue
            if r < self.threshold_r - self.hysteresis_r:
                # вернулись ниже зоны (с гистерезисом) → перевзвести
                self._armed[pos_id] = True
            elif r >= self.threshold_r and armed:
                candidates.append((pos_id, r))

        # очистить state закрытых позиций
        for pid in [p for p in self._armed if p not in present_ids]:
            del self._armed[pid]

        if not candidates:
            return EventDecision(fire=False)

        # rate-cap: окно в час
        while self._event_times and now - self._event_times[0] > 3600.0:
            self._event_times.popleft()

        if now - self._last_event_ts < self.cooldown_sec:
            # cooldown не пройден — кандидаты ОСТАЮТСЯ armed, повторим позже
            return EventDecision(fire=False, throttled=True)
        if len(self._event_times) >= self.max_events_per_hour:
            return EventDecision(fire=False, rate_capped=True)

        # стреляем: disarm спровоцировавшие позиции до возврата ниже зоны
        for pos_id, _r in candidates:
            self._armed[pos_id] = False
        self._last_event_ts = now
        self._event_times.append(now)
        triggers = [f"#{pid} {r:+.2f}R locked-profit" for pid, r in candidates]
        return EventDecision(fire=True, positions=candidates, triggers=triggers)


class AdverseMoveSensor:
    """Rising-edge детектор движения ПРОТИВ позиции (R ≤ −threshold_r).

    Phase 3 (2026-05-29): когда открытая позиция уходит в минус на
    ≥ threshold_r, будит внеплановый FULL-цикл — чтобы стратег с macro
    пересмотрел тезис (broken/intact). Это согласуется с Phase 0: тезис
    судит full-цикл с macro, а НЕ review-guardian по голому 1H. Раньше
    full-цикл этого ждал до 15 мин; теперь реагирует по факту движения.

    threshold_r здесь — натуральная единица риска (1R = дистанция до SL),
    не подгоночный параметр. 1R против = половина пути к стопу.
    """

    def __init__(
        self,
        threshold_r: float = 1.0,
        hysteresis_r: float = 0.3,
        cooldown_sec: float = 300.0,
        max_events_per_hour: int = 4,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold_r = threshold_r
        self.hysteresis_r = hysteresis_r
        self.cooldown_sec = cooldown_sec
        self.max_events_per_hour = max_events_per_hour
        self._now = now
        self._armed: dict[int, bool] = {}
        self._event_times: deque[float] = deque()
        self._last_event_ts: float = -math.inf

    def evaluate(self, positions: list[tuple[int, float | None]]) -> EventDecision:
        now = self._now()
        present_ids: set[int] = set()
        candidates: list[tuple[int, float]] = []

        for pos_id, r in positions:
            present_ids.add(pos_id)
            armed = self._armed.setdefault(pos_id, True)
            if r is None:
                continue
            # re-arm когда вернулись выше (−threshold + hysteresis)
            if r > -self.threshold_r + self.hysteresis_r:
                self._armed[pos_id] = True
            elif r <= -self.threshold_r and armed:
                candidates.append((pos_id, r))

        for pid in [p for p in self._armed if p not in present_ids]:
            del self._armed[pid]

        if not candidates:
            return EventDecision(fire=False)

        while self._event_times and now - self._event_times[0] > 3600.0:
            self._event_times.popleft()
        if now - self._last_event_ts < self.cooldown_sec:
            return EventDecision(fire=False, throttled=True)
        if len(self._event_times) >= self.max_events_per_hour:
            return EventDecision(fire=False, rate_capped=True)

        for pos_id, _r in candidates:
            self._armed[pos_id] = False
        self._last_event_ts = now
        self._event_times.append(now)
        triggers = [f"#{pid} {r:+.2f}R adverse" for pid, r in candidates]
        return EventDecision(fire=True, positions=candidates, triggers=triggers)


@dataclass
class ReferenceLevels:
    """Donchian-канал (highest high / lowest low за lookback) + ATR.

    Обновляется из full-цикла (он и так тянет бары) — датчик входа потом
    сравнивает живую цену с этими уровнями БЕЗ обращения к API.
    """
    hi: float
    lo: float
    atr: float | None = None


class EntryBreakoutSensor:
    """Datчик возможности входа: пробой Donchian-канала живой ценой.

    Phase 3 (2026-05-29): дешёвый «наблюдатель за экраном». Когда живая
    цена пробивает 20-баровый (1H) максимум/минимум (Donchian channel),
    будит внеплановый FULL-цикл — аналитик с macro/news решает open/hold.
    Это реализует идею «график показал сетап → позвали аналитика».

    Research basis: Donchian channel breakout (Donchian 1960s; Turtle
    Trading rules, Faith 2003 — 20-period lookback каноничен). Lopez de
    Prado «Advances in Financial ML» (2018) ch.2 — event-based sampling.

    Уровни обновляются из full-цикла (``update_reference``); сам датчик
    не делает API-вызовов. ``buffer_atr`` — confirmation band за краем
    канала (анти-шум на 1-тиковые проколы), не подгоночный порог.
    """

    def __init__(
        self,
        buffer_atr: float = 0.05,
        cooldown_sec: float = 300.0,
        max_events_per_hour: int = 4,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.buffer_atr = buffer_atr
        self.cooldown_sec = cooldown_sec
        self.max_events_per_hour = max_events_per_hour
        self._now = now
        self._ref: dict[str, ReferenceLevels] = {}
        self._armed_up: dict[str, bool] = {}
        self._armed_dn: dict[str, bool] = {}
        self._event_times: deque[float] = deque()
        self._last_event_ts: float = -math.inf

    def update_reference(
        self, symbol: str, hi: float, lo: float, atr: float | None = None,
    ) -> None:
        self._ref[symbol] = ReferenceLevels(hi, lo, atr)
        self._armed_up.setdefault(symbol, True)
        self._armed_dn.setdefault(symbol, True)

    def evaluate(
        self, live_prices: dict[str, float | None], slots_free: bool,
    ) -> EventDecision:
        """live_prices: symbol → живая цена (mid) или None.

        slots_free: есть ли свободный слот под новую позицию (total_open
        < max_open). Если слотов нет — арминг обновляем, но не стреляем
        (нет смысла будить аналитика для open).
        """
        now = self._now()
        candidates: list[tuple[str, str, float]] = []  # (symbol, dir, price)

        for sym, ref in self._ref.items():
            price = live_prices.get(sym)
            if price is None:
                continue
            buf = (ref.atr or 0.0) * self.buffer_atr
            up_level = ref.hi + buf
            dn_level = ref.lo - buf
            # re-arm при возврате внутрь канала
            if price <= ref.hi:
                self._armed_up[sym] = True
            if price >= ref.lo:
                self._armed_dn[sym] = True
            if price > up_level and self._armed_up.get(sym, True):
                candidates.append((sym, "up", price))
            elif price < dn_level and self._armed_dn.get(sym, True):
                candidates.append((sym, "down", price))

        if not candidates or not slots_free:
            return EventDecision(fire=False)

        while self._event_times and now - self._event_times[0] > 3600.0:
            self._event_times.popleft()
        if now - self._last_event_ts < self.cooldown_sec:
            return EventDecision(fire=False, throttled=True)
        if len(self._event_times) >= self.max_events_per_hour:
            return EventDecision(fire=False, rate_capped=True)

        triggers: list[str] = []
        for sym, direction, price in candidates:
            if direction == "up":
                self._armed_up[sym] = False
                triggers.append(f"{sym} up-break @{price:.6g} > Donchian hi")
            else:
                self._armed_dn[sym] = False
                triggers.append(f"{sym} down-break @{price:.6g} < Donchian lo")
        self._last_event_ts = now
        self._event_times.append(now)
        return EventDecision(fire=True, triggers=triggers)
