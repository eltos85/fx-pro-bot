"""Background writer тиковых принтов flowzone_bot в SQLite (A2, канон §2/§3).

Канон STRATEGY §3: зона = профиль ПРЕДЫДУЩЕЙ swing-точки; §2: контекст = форма
СЕССИОННОГО профиля. Профиль строится из исполненного потока (footprint), не из
kline-volume (no-data-fitting.mdc). Чтобы собрать per-swing окно (переменная
длина — от ts предыдущего swing до now) в любой момент, принты persist-ятся в
таблицу ``prints`` (state/db.py).

WS-callback (``SymbolState.on_trade``) вызывается из pybit-потока десятки раз в
сек; синхронная запись в SQLite заблокировала бы поток колбэков → loss данных.
Поэтому ``PrintStore`` накапливает принты в lock-protected deque и flush-ит
batch-ем из отдельного daemon-потока раз ``flush_interval_sec`` (по умолчанию
2с). Flush группирует ``executemany`` — одна транзакция на батч.

Retention: ``prune_older_than_sec`` (по умолчанию 6ч) — принты старше порога
удаляются в том же flush-цикле. Per-swing окно — максимум длина тренда внутри
сессии (часы); 6ч — с запасом. Не trading-порог, технический объём БД.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("flowzone_bot.prints")


@dataclass
class _FlushStats:
    flushed: int = 0
    pruned: int = 0


class PrintStore:
    """Потокобезопасный batched writer тиковых принтов в SQLite.

    Жизненный цикл: ``start()`` запускает daemon-поток flush; ``stop()`|
    корректно дожидается последнего flush. ``ingest()`` вызывается из WS-потока.
    """

    def __init__(self, db, *, flush_interval_sec: float = 2.0,
                 prune_older_than_sec: float = 6 * 3600.0,
                 max_buffer: int = 50_000,
                 now: callable = time.monotonic,
                 wall_now: callable = time.time) -> None:
        self._db = db
        self._flush_interval = flush_interval_sec
        self._prune_older = prune_older_than_sec
        self._max_buffer = max_buffer
        self._now = now
        self._wall_now = wall_now
        self._buf: deque[tuple] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        t = threading.Thread(target=self._loop, name="flowzone-prints",
                             daemon=True)
        t.start()
        self._thread = t

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
            self._thread = None
        # финальный flush остатка буфера
        try:
            self._flush_once(final=True)
        except Exception:
            log.exception("final print flush failed")

    def ingest(self, ts: float, symbol: str, price: float, size: float,
               side: str) -> None:
        """Добавить принт в буфер (вызов из WS-потока). При переполнении буфера
        отбрасываем старейшие (memory-guard) — лучше потерять старые тики, чем
        уронить процесс по OOM."""
        row = (ts, symbol, price, size, side)
        with self._lock:
            self._buf.append(row)
            if len(self._buf) > self._max_buffer:
                self._buf.popleft()

    def buffered(self) -> int:
        with self._lock:
            return len(self._buf)

    # ─── внутренние ──────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._flush_interval)
            try:
                self._flush_once()
            except Exception:
                log.exception("print flush loop failed")

    def _flush_once(self, *, final: bool = False) -> _FlushStats:
        with self._lock:
            rows = list(self._buf)
            self._buf.clear()
        flushed = 0
        if rows and self._db is not None:
            flushed = self._db.insert_prints(rows)
        pruned = 0
        if self._db is not None and self._prune_older > 0:
            cutoff = self._wall_now() - self._prune_older
            try:
                pruned = self._db.prune_prints_before(cutoff)
            except Exception:
                log.exception("prune prints failed")
        if final and (flushed or pruned):
            log.info("prints final flush: %d rows, %d pruned", flushed, pruned)
        return _FlushStats(flushed=flushed, pruned=pruned)
