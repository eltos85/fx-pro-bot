"""Мультистратегийный каркас scalp_bot.

Бот гоняет несколько НЕЗАВИСИМЫХ стратегий поверх одного потока данных Bybit.
Контракт (см. обсуждение архитектуры 2026-05-30):

- Каждая стратегия сама ищет вход (``update``), помечает сигнал своим именем
  и сама сопровождает свою позицию (``should_exit``). Вход и выход — в паре:
  позицию, открытую стратегией A, НЕЛЬЗЯ закрывать логикой стратегии B.
- Универсальные выходы (TP / SL / тайм-стоп) и риск (killswitch, размер лота,
  fee-guard) — ОБЩИЕ, живут в executor/killswitch, стратегиям безразличны.
- На один символ — максимум одна позиция. Если две стратегии в один тик дают
  ПРОТИВОПОЛОЖНЫЕ направления по символу — не берём ни одну (``resolve``).

Стратегии:
- ``sweep_fade``      — текущий свип+поглощение mean-reversion (SweepReclaimDetector).
- ``density_bounce``  — отскок от плотности в стакане (Фаза 2, добавляется позже).
"""
from __future__ import annotations

import logging
import math
from typing import Protocol

from scalp_bot.analysis.signals import (
    Signal,
    SweepReclaimDetector,
    build_signal,
    flow_invalidated,
)
from scalp_bot.data.aggregates import SymbolSnapshot

play = logging.getLogger("scalp_bot.play")

_SIDE_RU = {"long": "LONG↑", "short": "SHORT↓"}


class Strategy(Protocol):
    """Интерфейс стратегии. Реализации держат своё пер-символьное состояние."""

    name: str

    def update(self, snap: SymbolSnapshot, now: float) -> Signal | None:
        """Поиск входа по символу snap.symbol. None — сетапа нет."""
        ...

    def armed(self, symbol: str) -> bool:
        """Стратегия «во взводе» по символу (для funnel-диагностики)."""
        ...

    def reset(self, symbol: str) -> None:
        """Сбросить состояние по символу (вызывается при открытой позиции)."""
        ...

    def should_exit(self, tr, snap: SymbolSnapshot, now: float
                    ) -> tuple[str, float] | None:
        """Дискреционный выход стратегии (помимо общих TP/SL/тайм-стопа).

        Возвращает (close_reason, exit_price) или None. Вызывается executor-ом
        ТОЛЬКО для сделок этой стратегии (tr.strategy == self.name)."""
        ...


class SweepFadeStrategy:
    """Стратегия №1: свип ликвидности + поглощение (mean-reversion fade).

    Обёртка над двухфазным ``SweepReclaimDetector`` (по детектору на символ).
    Дискреционный выход — fee-aware «профит-лок по развороту ленты»
    (перенесён из executor: см. BUILDLOG 2026-05-30 v0.3.4): закрываем
    раньше TP только если ход уже покрыл round-trip комиссию И поток (CVD)
    развернулся против позиции. Флэт/мелкий плюс не скретчим (иначе −fee),
    убыточные ведёт общий SL.
    """

    name = "sweep_fade"

    def __init__(self, cfg, symbols: list[str]) -> None:
        self.cfg = cfg
        self._det: dict[str, SweepReclaimDetector] = {
            s: SweepReclaimDetector(s, cfg) for s in symbols
        }

    def update(self, snap: SymbolSnapshot, now: float) -> Signal | None:
        det = self._det.get(snap.symbol)
        if det is None:
            return None
        sig = det.update(snap, now)
        if sig is not None:
            sig.strategy = self.name
        return sig

    def armed(self, symbol: str) -> bool:
        det = self._det.get(symbol)
        return bool(det and det.armed)

    def reset(self, symbol: str) -> None:
        det = self._det.get(symbol)
        if det is not None:
            det.reset()

    def should_exit(self, tr, snap: SymbolSnapshot, now: float
                    ) -> tuple[str, float] | None:
        cfg = self.cfg
        if not getattr(cfg, "active_exit_enabled", False) or snap is None:
            return None
        if now - tr.ts_open < cfg.active_exit_min_age_sec:
            return None
        price = snap.last_price
        if price is None:
            return None
        # ход в нашу пользу должен покрыть round-trip taker, иначе не выходим
        favorable = (price - tr.entry) if tr.side == "long" else (tr.entry - price)
        fee_px = tr.entry * cfg.round_trip_fee_frac
        if favorable < fee_px:
            return None
        if flow_invalidated(snap, tr.side, cfg.momentum_window_sec):
            return ("flow_exit", price)
        return None


# ─── density_bounce helpers (чистые, тестируемые без WS) ───────────────────

def near_round(price: float, frac: float) -> bool:
    """Цена рядом с круглым числом (в пределах frac×price).

    Шаг круглости масштабируется к величине цены: step = 10^(порядок−1)
    (~1% от цены). Напр. 66→шаг 1 (рядом 65/66/67), 518→шаг 10 (510/520),
    2.4→шаг 0.1. Данилов: плотности на круглых уровнях держат надёжнее.
    """
    if price <= 0:
        return False
    step = 10.0 ** (math.floor(math.log10(price)) - 1)
    if step <= 0:
        return False
    nearest = round(price / step) * step
    return abs(price - nearest) <= frac * price


def _baseline_avg(sizes: list[float]) -> float:
    """Средний размер «обычного» уровня = mean без единственного максимума
    (Kalena: стена выражается как кратное СРЕДНЕГО, аномалию в базу не берём,
    иначе крупная стена сама раздувает свой порог при малом N уровней)."""
    if len(sizes) < 2:
        return sizes[0] if sizes else 0.0
    mx = max(sizes)
    others = list(sizes)
    others.remove(mx)
    return sum(others) / len(others)


def detect_wall(levels: list[tuple[float, float]], wall_mult: float,
                min_usd: float = 0.0) -> tuple[float, float] | None:
    """Крупнейшая «стена» на стороне книги: size ≥ wall_mult × baseline_avg.

    baseline_avg — средний размер обычного уровня (без самой стены).
    min_usd — опциональный абсолютный пол (price×size).
    Возвращает (price, size) стены или None.
    """
    if len(levels) < 5:
        return None
    base = _baseline_avg([sz for _, sz in levels])
    if base <= 0:
        return None
    price, size = max(levels, key=lambda ps: ps[1])
    if size < wall_mult * base:
        return None
    if min_usd > 0 and price * size < min_usd:
        return None
    return (price, size)


def _wall_in_range(levels: list[tuple[float, float]], lo: float, hi: float,
                   wall_mult: float, min_usd: float = 0.0) -> bool:
    """Есть ли всё ещё квалифицирующая стена в ценовом диапазоне [lo, hi]."""
    if len(levels) < 5:
        return False
    base = _baseline_avg([sz for _, sz in levels])
    if base <= 0:
        return False
    for price, size in levels:
        if lo <= price <= hi and size >= wall_mult * base:
            if min_usd <= 0 or price * size >= min_usd:
                return True
    return False


class DensityBounceStrategy:
    """Стратегия №2: отскок от плотности (крупной лимитки) в стакане.

    ─── Research basis ───
    Kalena «Crypto Wall Detection» 2026: стена = ≥5–8× средний размер уровня
    (относительный порог, не абсолютный $); если >30% стены ушло за <10с —
    остаток скоро снимут (спуфинг) → не торгуем. arXiv 2604.20949: depth-
    сигналы причинно раньше flow. Данилов (YouTube 2025): отскок от плотности
    на круглом числе, стоп сразу за стеной (короткий → хороший R:R).

    Логика (на символ):
    1. Найти стену на bid (→long) / ask (→short), близко к круглому числу.
    2. Отслеживать её: должна продержаться ≥ persist_sec (анти-спуфинг);
       если поглощается (size упал на ≥ absorb_frac за absorb_window) — снять.
    3. Когда цена подошла к стене (≤ near_bps) и стена «выстояла» → вход в
       отскок, SL сразу за стеной (build_signal swept=цена_стены), TP по R с
       общим fee-guard.
    Выход (should_exit): стена, на которую опирались, исчезла → тезис снят.
    """

    name = "density_bounce"

    def __init__(self, cfg, symbols: list[str]) -> None:
        self.cfg = cfg
        # на символ: {"bid": wallstate|None, "ask": wallstate|None}
        self._track: dict[str, dict[str, dict | None]] = {
            s: {"bid": None, "ask": None} for s in symbols
        }
        self._last_log: dict[str, float] = {}

    def armed(self, symbol: str) -> bool:
        t = self._track.get(symbol)
        return bool(t and (t["bid"] or t["ask"]))

    def reset(self, symbol: str) -> None:
        if symbol in self._track:
            self._track[symbol] = {"bid": None, "ask": None}

    def _update_track(self, sym: str, book_side: str,
                      levels: list[tuple[float, float]], now: float) -> None:
        cfg = self.cfg
        t = self._track[sym]
        wall = detect_wall(levels, cfg.density_wall_mult, cfg.density_min_wall_usd)
        cur = t[book_side]
        if wall is None or not near_round(wall[0], cfg.density_round_frac):
            t[book_side] = None
            return
        price, size = wall
        if cur is None or abs(cur["price"] - price) > 1e-12:
            t[book_side] = {"price": price, "size0": size, "last_size": size,
                            "first_seen": now}
            return
        # та же стена: обновляем размер + проверяем поглощение (анти-спуфинг)
        cur["last_size"] = size
        eaten = (cur["size0"] - size) / cur["size0"] if cur["size0"] > 0 else 0.0
        if (eaten >= cfg.density_absorb_frac
                and now - cur["first_seen"] <= cfg.density_absorb_window_sec):
            play.info("🧱 [%s] стена %s %.6f поглощается (%.0f%% за %.0fс) — "
                      "снимаю наблюдение (спуфинг/пробой)", sym, book_side,
                      price, eaten * 100, now - cur["first_seen"])
            t[book_side] = None

    def update(self, snap: SymbolSnapshot, now: float) -> Signal | None:
        cfg = self.cfg
        if snap.stale or snap.last_price is None:
            return None
        sym = snap.symbol
        if sym not in self._track:
            self._track[sym] = {"bid": None, "ask": None}
        self._update_track(sym, "bid", snap.bids, now)
        self._update_track(sym, "ask", snap.asks, now)
        last = snap.last_price
        near = cfg.density_near_bps / 1e4
        # bid-стена → отскок ВВЕРХ (long); ask-стена → отскок ВНИЗ (short)
        for book_side, side in (("bid", "long"), ("ask", "short")):
            w = self._track[sym][book_side]
            if w is None:
                continue
            if now - w["first_seen"] < cfg.density_persist_sec:
                continue  # ещё не выстояла (анти-спуфинг)
            if abs(last - w["price"]) > near * w["price"]:
                continue  # цена ещё не подошла к стене
            reasons = ["density", "round", "persist"]
            sig = build_signal(snap, side, w["price"], cfg, len(reasons), reasons)
            if sig is None:
                continue  # fee-guard / risk не прошли
            sig.strategy = self.name
            play.info("🧱 [%s] ОТСКОК %s от стены %.6f (выстояла %.0fс, цена "
                      "%.6f) → вход @%.4f SL %.4f TP %.4f", sym,
                      _SIDE_RU.get(side, side), w["price"],
                      now - w["first_seen"], last, sig.entry_ref,
                      sig.sl_level, sig.tp_level)
            return sig
        return None

    def should_exit(self, tr, snap: SymbolSnapshot, now: float
                    ) -> tuple[str, float] | None:
        """Стена, на которую опирались, исчезла → тезис снят, выходим.

        Якорь стены ≈ возле SL (SL ставился сразу за стеной). Для long ищем
        bid-уровень в (sl, entry], для short — ask-уровень в [entry, sl)."""
        cfg = self.cfg
        if snap is None or snap.last_price is None:
            return None
        if now - tr.ts_open < cfg.active_exit_min_age_sec:
            return None
        if tr.side == "long":
            present = _wall_in_range(snap.bids, tr.sl, tr.entry,
                                     cfg.density_wall_mult, cfg.density_min_wall_usd)
        else:
            present = _wall_in_range(snap.asks, tr.entry, tr.sl,
                                     cfg.density_wall_mult, cfg.density_min_wall_usd)
        if not present:
            return ("density_gone", snap.last_price)
        return None


def build_strategies(cfg, symbols: list[str]) -> list[Strategy]:
    """Фабрика стратегий по cfg.enabled_strategies (CSV). Неизвестные — скип."""
    enabled = getattr(cfg, "strategy_list", ["sweep_fade"])
    registry: dict[str, type] = {
        SweepFadeStrategy.name: SweepFadeStrategy,
        DensityBounceStrategy.name: DensityBounceStrategy,
    }
    out: list[Strategy] = []
    for name in enabled:
        cls = registry.get(name)
        if cls is None:
            play.info("⚠️ неизвестная стратегия в конфиге: %s — пропускаю", name)
            continue
        out.append(cls(cfg, symbols))
    if not out:  # защита: всегда хотя бы sweep_fade
        out.append(SweepFadeStrategy(cfg, symbols))
    return out


def resolve(signals: list[Signal]) -> Signal | None:
    """Гард на конфликт по одному символу.

    - нет сигналов → None;
    - все сигналы в ОДНУ сторону → берём с максимальным score (при равенстве —
      первый по порядку стратегий);
    - есть и long, и short → конфликт, не берём НИЧЕГО (неоднозначность).
    """
    if not signals:
        return None
    sides = {s.side for s in signals}
    if len(sides) > 1:
        syms = signals[0].symbol
        names = ",".join(sorted({s.strategy for s in signals}))
        play.info("🛑 [%s] конфликт стратегий (%s): разные направления — "
                  "пропускаю тик", syms, names)
        return None
    return max(signals, key=lambda s: s.score)
