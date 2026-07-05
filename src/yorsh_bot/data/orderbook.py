"""Локальная книга заявок (milestone M1).

Применение инкрементальных diff'ов с проверкой version-последовательности;
при gap — сигнал reinit (вызывает REST snapshot у коллектора). Параметризация
под разные биржи:

- MEXC (spot v3, aggre depth): version-диапазон (from_version, to_version).
  Правило из офиц. доки (https://www.mexc.com/api-docs/spot-v3/websocket-
  market-streams/how-to-properly-maintain-a-local-copy-of-the-order-book):
  после snapshot (last_update_id) дифф применяется, если
  ``from_version == last_version + 1`` (допускается from_version > last+1
  только для первого после snapshot — тогда reinit); после каждого диффа
  last_version := to_version. Дифф с ``to_version <= last_version`` —
  пропускается (устаревший, до snapshot).
- Bitget:单调ный ``seq`` + ``pseq`` (предыдущий seq). Gap —
  ``pseq != last_seq``. Реализация — M2 (расширим этот же класс).

Уровень с size=0 удаляется. Книга хранит bids/asks как dict[price] -> size.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from yorsh_bot.exchanges.base import BookSnapshot, DepthDiff


@dataclass
class OrderBookState:
    """Снимок состояния книги (для recorder/отчёта)."""
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    last_version: int | None = None


class LocalOrderBook:
    """Локальная L2-книга с version-контролем.

    ``version_mode``:
    - ``"range"``  — MEXC: diff несёт (from_version, to_version); проверяем
      ``from_version == last_version + 1`` (или первый после snapshot).
    - ``"seq"``    — Bitget: diff несёт (seq, prev_seq); проверяем
      ``prev_seq == last_seq``.
    """

    def __init__(self, exchange: str, symbol: str,
                 *, version_mode: str = "range") -> None:
        if version_mode not in ("range", "seq"):
            raise ValueError(f"unknown version_mode: {version_mode}")
        self.exchange = exchange
        self.symbol = symbol
        self.version_mode = version_mode
        self.last_version: int | None = None
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        # буфер диффов, накопленных ДО snapshot (процедура MEXC step 1-2)
        self._pre_snapshot_buffer: list[DepthDiff] = []
        self._snapshot_loaded = False

    # ─── snapshot ────────────────────────────────────────────────────────
    def apply_snapshot(self, snap: BookSnapshot) -> None:
        """Инициализировать книгу REST-снапшотом (MEXC step 3-6).

        Согласно процедуре: discard диффы с to_version <= last_update_id;
        первый оставшийся дифф должен иметь from_version == last_update_id+1
        (иначе — reinit). Здесь — только загрузка snapshot + сброс буфера;
        отбрасывание устаревших диффов делает ``apply_diff``.
        """
        self.bids = {float(p): float(s) for p, s in snap.bids if float(s) > 0}
        self.asks = {float(p): float(s) for p, s in snap.asks if float(s) > 0}
        self.last_version = snap.seq
        self._snapshot_loaded = True
        # Применяем буфер диффов, накопленных до snapshot (процедура MEXC).
        buffered = self._pre_snapshot_buffer
        self._pre_snapshot_buffer = []
        for diff in buffered:
            self.apply_diff(diff)

    def buffer_pre_snapshot(self, diff: DepthDiff) -> None:
        """MEXC step 1: кешируем диффы до получения snapshot."""
        self._pre_snapshot_buffer.append(diff)

    # ─── diff ────────────────────────────────────────────────────────────
    def _diff_versions(self, diff: DepthDiff) -> tuple[int | None, int | None]:
        """Вернуть (from_or_prev, to_or_seq) в зависимости от mode."""
        if self.version_mode == "range":
            # MEXC: seq=to_version, prev_seq=from_version
            return diff.prev_seq, diff.seq
        # Bitget: seq=seq, prev_seq=pseq
        return diff.prev_seq, diff.seq

    def needs_reinit(self, diff: DepthDiff) -> bool:
        """True, если version-последовательность нарушена — нужен REST reinit.

        MEXC range-mode (https://www.mexc.com/api-docs/spot-v3/websocket-
        market-streams/how-to-properly-maintain-a-local-copy-of-the-order-book,
        step 5-6): первый после snapshot дифф требует
        ``from_version == last_update_id + 1``; последующие —
        ``from_version == previous to_version + 1``. Нарушение → reinit.
        Диффы с ``to_version <= last_version`` — устаревшие, не reinit
        (пропускаются в apply_diff).

        Bitget seq-mode: gap = ``prev_seq != last_seq`` ( monotonic seq,
        pseq = предыдущий применённый seq).
        """
        from_v, to_v = self._diff_versions(diff)
        if from_v is None or to_v is None:
            return False
        if self.last_version is None:
            return False  # snapshot ещё не загружен → буфер
        if to_v <= self.last_version:
            return False   # устаревший (MEXC step 4) — пропускаем, не reinit
        if self.version_mode == "seq":
            # Bitget: prev_seq должен совпадать с последним применённым seq.
            return from_v != self.last_version
        # MEXC range: from_version == last_version + 1
        return from_v > self.last_version + 1

    def apply_diff(self, diff: DepthDiff) -> bool:
        """Применить дифф к книге. Вернуть True, если применён; False — пропущен.

        Если needs_reinit — НЕ применяет (коллектор должен сделать REST reinit
        и перезагрузить книгу). Устаревшие диффы (to_version <= last_version)
        пропускаются. Если snapshot ещё не загружен — дифф буферизуется.
        """
        if not self._snapshot_loaded:
            self.buffer_pre_snapshot(diff)
            return False
        if self.needs_reinit(diff):
            return False
        from_v, to_v = self._diff_versions(diff)
        if to_v is not None and self.last_version is not None \
                and to_v <= self.last_version:
            return False  # устаревший (MEXC step 4)
        self._apply_levels(diff)
        if to_v is not None:
            self.last_version = to_v
        return True

    def _apply_levels(self, diff: DepthDiff) -> None:
        for price, size in diff.bids:
            p, s = float(price), float(size)
            if s <= 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = s
        for price, size in diff.asks:
            p, s = float(price), float(size)
            if s <= 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = s

    # ─── read ────────────────────────────────────────────────────────────
    def snapshot(self) -> OrderBookState:
        """Отсортированный снимок: bids убыв. цены, asks возр. цены."""
        return OrderBookState(
            bids=sorted(self.bids.items(), key=lambda kv: -kv[0]),
            asks=sorted(self.asks.items(), key=lambda kv: kv[0]),
            last_version=self.last_version,
        )

    def best_bid_ask(self) -> tuple[float | None, float | None]:
        bid = max(self.bids) if self.bids else None
        ask = min(self.asks) if self.asks else None
        return bid, ask
