"""Density-tracker (milestone M4).

Поток L2-диффов + трейдов → детекция «плотностей» (уровней с размером >>
соседних, параметр ``kratnosti``) и жизненный цикл: появление, persistence,
partial fills, pull (снятие при подходе цены), refill (iceberg: размер
восстанавливается после fills), move (прыгает по уровням). Вердикт:
``genuine | iceberg | spoof | unknown``.

─── Research basis ───
Правила вердиктов — из аудита п.2 «Уточнение под нашу страту»
(`docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md`), 4 признака:
  1. Persistence score: плотность стоит >60с без перестановки.
  2. Partial-fill evidence: executed trades на уровне, размер убывает но не
     до нуля (или восстанавливается = iceberg).
  3. Spoof-reject: level исчезает при подходе цены / прыгает → дисквал.
  4. Volume/depth mismatch: cumulative traded at price >> visible depth →
     iceberg (сильная опора).
Внешние источники (blog/SEO, не research-grade — только обоснование правил):
  - Nydar «Iceberg Orders & Spoofing»: 5+ fills ±20% на одной цене → iceberg.
  - Kalena: persistent bid + partial fills = genuine; clean cancel <1s = spoof.
  - dxFeed Iceberg Detection: tranche в небольшом окне после fill.

Все пороги — **стартовые точки** (``no-data-fitting.mdc``), финальные только
из калибровки M6 на собранной ленте. Канонических порогов для нашего сетапа
в literature нет (аудит, «Качество источников»).
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Callable

from yorsh_bot.config.settings import YorshSettings
from yorsh_bot.exchanges.base import DepthDiff, Trade
from yorsh_bot.state.db import YorshDB

log = logging.getLogger("yorsh_bot.density")


@dataclass
class DensityEvent:
    """Событие жизненного цикла плотности для колбэка-персистора."""
    kind: str               # "open" | "update" | "close"
    exchange: str
    symbol: str
    side: str
    price: float
    first_seen: float
    last_seen: float
    peak_size: float
    persistence_sec: float
    partial_fill_vol: float
    pull_count: int
    refilled: int
    moved: int
    verdict: str
    db_id: int | None = None


@dataclass
class _Density:
    """In-memory состояние плотности (ключ = (side, price))."""
    side: str
    price: float
    first_seen: float
    last_seen: float
    peak_size: float
    cur_size: float
    partial_fill_vol: float = 0.0
    pull_count: int = 0
    refilled: int = 0
    moved: int = 0
    db_id: int | None = None
    # момент последнего ухода size→0 (для refill/move окон)
    vanished_at: float | None = None
    vanished_peak: float = 0.0
    closed: bool = False


class DensityTracker:
    """Трекер плотностей одного (exchange, symbol).

    Подает L2-диффы через ``apply_diff`` (с текущими best_bid/best_ask для
    детекции spoof-pull «при подходе цены») и трейды через ``apply_trade``
    (partial-fill учёт по совпадению цены). ``flush(now)`` закрывает
    «протухшие» плотности (size=0 дольше ``density_gap_close_sec``) и эмитит
    финальные вердикты через ``on_event``.
    """

    def __init__(self, exchange: str, symbol: str, settings: YorshSettings,
                 *, tick_size: float,
                 on_event: Callable[[DensityEvent], None]) -> None:
        self.exchange = exchange
        self.symbol = symbol
        self.s = settings
        self.tick = tick_size
        self._on_event = on_event
        # sizes[side][price] = текущий размер уровня (из диффов)
        self._sizes: dict[str, dict[float, float]] = {"bid": {}, "ask": {}}
        # active[(side, price)] = _Density
        self._active: dict[tuple[str, float], _Density] = {}
        # recently closed для refill/move атрибуции: key → (vanished_at, peak, side)
        self._graveyard: list[dict] = []   # {side, price, vanished_at, peak, density}

    # ─── public API ──────────────────────────────────────────────────────

    def apply_diff(self, d: DepthDiff,
                   best_bid: float | None, best_ask: float | None) -> None:
        for side in ("bid", "ask"):
            levels = getattr(d, f"{side}s", None)
            if not levels:
                continue
            for price, size in levels:
                self._update_level(side, float(price), float(size),
                                   d.ts_exch, best_bid, best_ask)
        self._detect_new(d.ts_exch, best_bid, best_ask)

    def apply_trade(self, t: Trade) -> None:
        # partial-fill: трейд по цене плотности → cumulative traded volume
        # buy-trade ест ask-плотность, sell-trade ест bid-плотность
        victim_side = "ask" if t.side == "buy" else "bid"
        key = (victim_side, t.price)
        dens = self._active.get(key)
        if dens is not None and not dens.closed:
            dens.partial_fill_vol += t.size
            dens.last_seen = t.ts_exch

    def flush(self, now: float) -> None:
        """Закрыть плотности, отсутствующие дольше gap_close_sec — финальный вердикт."""
        for key in list(self._active):
            dens = self._active[key]
            if dens.closed:
                continue
            if dens.vanished_at is not None and \
                    now - dens.vanished_at >= self.s.density_gap_close_sec:
                self._close(dens, now, verdict=self._verdict(dens, now))

    # ─── internal ────────────────────────────────────────────────────────

    def _update_level(self, side: str, price: float, size: float,
                      ts: float, best_bid: float | None,
                      best_ask: float | None) -> None:
        book = self._sizes[side]
        key = (side, price)
        dens = self._active.get(key)

        if size <= 0:
            book.pop(price, None)
            if dens is not None and not dens.closed and dens.vanished_at is None:
                # уход размера → возможный pull (spoof) если цена подошла
                best = best_bid if side == "bid" else best_ask
                pulled = False
                if best is not None:
                    approach = self.s.density_approach_ticks * self.tick
                    if abs(best - price) <= approach:
                        dens.pull_count += 1
                        pulled = True
                dens.vanished_at = ts
                dens.vanished_peak = dens.peak_size
                dens.cur_size = 0.0
                if pulled:
                    log.debug("density pull %s %s %s (price approached)",
                              self.symbol, side, price)
            return

        book[price] = size
        if dens is not None and not dens.closed:
            # refill: размер вернулся после ухода (iceberg-паттерн)
            if dens.vanished_at is not None:
                if ts - dens.vanished_at <= self.s.density_refill_window_sec \
                        and dens.vanished_peak > 0:
                    dens.refilled += 1
                    dens.vanished_at = None
                    log.debug("density refill %s %s %s (iceberg)",
                              self.symbol, side, price)
                else:
                    # ушёл давно и вернулся — это новая плотность, не refill
                    self._close(dens, ts, verdict=self._verdict(dens, ts))
                    self._active.pop(key, None)
                    dens = None
            if dens is not None:
                dens.cur_size = size
                dens.peak_size = max(dens.peak_size, size)
                dens.last_seen = ts
                self._emit_update(dens, ts)
        else:
            # возможен «move»: плотность пропала и тут же всплыла на новой цене
            self._attribute_move(side, price, size, ts)

    def _attribute_move(self, side: str, price: float, size: float,
                        ts: float) -> None:
        """Если недавно пропавшая (vanished, не closed) плотность той же
        стороны всплыла на новой цене в пределах move_window — маркируем
        оригинал moved (spoof-jump) и закрываем его."""
        for key, dens in list(self._active.items()):
            if dens.side != side or dens.closed:
                continue
            if dens.vanished_at is None:
                continue
            if ts - dens.vanished_at <= self.s.density_move_window_sec \
                    and abs(dens.price - price) > self.tick:
                dens.moved += 1
                self._close(dens, ts, verdict=self._verdict(dens, ts))
                log.debug("density move %s %s: %s → %s (spoof-jump)",
                          self.symbol, side, dens.price, price)
                break

    def _detect_new(self, ts: float, best_bid: float | None,
                    best_ask: float | None) -> None:
        """Найти уровни, ставшие «плотностями» (size >> соседних), и стартовать."""
        for side in ("bid", "ask"):
            book = self._sizes[side]
            if len(book) < 3:
                continue
            sizes = list(book.values())
            # медиана размеров стороны как «соседний» масштаб
            med = statistics.median(sizes)
            if med <= 0:
                continue
            for price, size in book.items():
                if size < self.s.density_kratnosti * med:
                    continue
                key = (side, price)
                if key in self._active and not self._active[key].closed:
                    continue
                # не детектим как новую, если уже в graveyard-окне (refill её обработает)
                if any(g["side"] == side and g["price"] == price and
                       ts - g["vanished_at"] <= self.s.density_refill_window_sec
                       for g in self._graveyard):
                    continue
                dens = _Density(side=side, price=price, first_seen=ts,
                                last_seen=ts, peak_size=size, cur_size=size)
                self._active[key] = dens
                self._emit_open(dens, ts)

    def _close(self, dens: _Density, now: float, *, verdict: str) -> None:
        dens.closed = True
        dens.last_seen = now
        self._emit_close(dens, now, verdict=verdict)
        # в graveyard для move-атрибуции
        self._graveyard.append({
            "side": dens.side, "price": dens.price,
            "vanished_at": dens.vanished_at or now,
            "peak": dens.peak_size, "density": dens})
        # чистим старый graveyard
        self._graveyard = [g for g in self._graveyard
                           if now - g["vanished_at"] <= self.s.density_move_window_sec]

    def _verdict(self, dens: _Density, now: float) -> str:
        persistence = (dens.vanished_at or now) - dens.first_seen \
            if dens.vanished_at is not None else now - dens.first_seen
        # spoof: pull при подходе цены ИЛИ прыжки по уровням
        if dens.pull_count > 0 or dens.moved > 0:
            return "spoof"
        # iceberg: refill ИЛИ cumulative traded >> visible peak (mismatch)
        if dens.refilled > 0:
            return "iceberg"
        if dens.peak_size > 0 and \
                dens.partial_fill_vol >= self.s.density_mismatch_ratio * dens.peak_size:
            return "iceberg"
        # genuine: persistence + partial fills + не переставлялась
        if persistence >= self.s.density_min_persistence_sec \
                and dens.partial_fill_vol > 0 and dens.moved == 0 \
                and dens.pull_count == 0:
            return "genuine"
        return "unknown"

    # ─── emit ────────────────────────────────────────────────────────────

    def _persistence(self, dens: _Density, now: float) -> float:
        if dens.vanished_at is not None:
            return dens.vanished_at - dens.first_seen
        return now - dens.first_seen

    def _emit_open(self, dens: _Density, ts: float) -> None:
        ev = DensityEvent("open", self.exchange, self.symbol, dens.side,
                          dens.price, dens.first_seen, dens.last_seen,
                          dens.peak_size, 0.0, 0.0, 0, 0, 0, "unknown")
        self._on_event(ev)
        dens.db_id = ev.db_id

    def _emit_update(self, dens: _Density, ts: float, *, verdict: str | None = None) -> None:
        v = verdict or self._verdict(dens, ts)
        self._on_event(DensityEvent(
            "update", self.exchange, self.symbol, dens.side, dens.price,
            dens.first_seen, dens.last_seen, dens.peak_size,
            self._persistence(dens, ts), dens.partial_fill_vol,
            dens.pull_count, dens.refilled, dens.moved, v, db_id=dens.db_id))

    def _emit_close(self, dens: _Density, ts: float, *, verdict: str) -> None:
        self._on_event(DensityEvent(
            "close", self.exchange, self.symbol, dens.side, dens.price,
            dens.first_seen, dens.last_seen, dens.peak_size,
            self._persistence(dens, ts), dens.partial_fill_vol,
            dens.pull_count, dens.refilled, dens.moved, verdict,
            db_id=dens.db_id))


def make_db_persistor(db: YorshDB) -> Callable[[DensityEvent], None]:
    """Колбэк-персистор: DensityEvent → SQLite densities (insert/update/close)."""
    def on_event(ev: DensityEvent) -> None:
        if ev.kind == "open":
            ev.db_id = db.insert_density(
                exchange=ev.exchange, symbol=ev.symbol, side=ev.side,
                price=ev.price, first_seen=ev.first_seen, last_seen=ev.last_seen,
                peak_size=ev.peak_size, verdict=ev.verdict,
                persistence_sec=ev.persistence_sec,
                partial_fill_vol=ev.partial_fill_vol, pull_count=ev.pull_count,
                refilled=ev.refilled, moved=ev.moved)
        elif ev.kind == "update" and ev.db_id is not None:
            db.update_density(
                ev.db_id, last_seen=ev.last_seen, peak_size=ev.peak_size,
                persistence_sec=ev.persistence_sec,
                partial_fill_vol=ev.partial_fill_vol, pull_count=ev.pull_count,
                refilled=ev.refilled, moved=ev.moved, verdict=ev.verdict)
        elif ev.kind == "close" and ev.db_id is not None:
            db.update_density(
                ev.db_id, last_seen=ev.last_seen, peak_size=ev.peak_size,
                persistence_sec=ev.persistence_sec,
                partial_fill_vol=ev.partial_fill_vol, pull_count=ev.pull_count,
                refilled=ev.refilled, moved=ev.moved, verdict=ev.verdict)
    return on_event
