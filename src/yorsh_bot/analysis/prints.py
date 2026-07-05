"""Детекция «прострелов» (spurt) и кластеризация принтов-триггеров (M5).

Прострел = быстрое движение цены ≥ ``YORSH_SPURT_MIN_AMPLITUDE_PCT`` за
короткое окно, стартованное агрессивными маркет-принтами. Триггер-принты
кластеризуются по размеру («одинаковый принт» — аудит п.1, признак 1).

─── Research basis ───
- RisingWave «Building a Real-Time Crypto Pump-and-Dump Detector with SQL»
  (https://risingwave.com/blog/build-real-time-crypto-pump-dump-detector-sql/):
  1-мин return ≥2%, volume Z≥3, buy_ratio ≥0.65 → pump. Пороги — engineering-
  эвристика из блога, НЕ research → стартовая точка для калибровки M6
  (аудит п.1 «Качество источников», ``no-data-fitting.mdc``).
- «Одинаковый принт» — кластеризация по размеру с ±20% variance (реюз
  Nydar-правила из п.2 для iceberg-fills; та же микроструктурная логика).

─── Выбор кластеризации ───
Гистограммная кластеризация по размеру на stdlib вместо DBSCAN: (1) sklearn
в проект не тащим (ТЗ M5); (2) признак 1-D (размер принта) — DBSCAN по
(size, price-offset) избыточен, гистограмма с adaptive-bin (20% variance)
даёт тот же результат детерминированно и O(n log n); (3) «одинаковый
принт» = кластер, где max/min size ≤ 1.2 (20% variance). Обоснование —
в docstring ``cluster_prints_by_size``.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

from yorsh_bot.config.settings import YorshSettings
from yorsh_bot.exchanges.base import Trade

log = logging.getLogger("yorsh_bot.prints")

# Окно детекции прострела (мс) — стартовая точка, калибровать (RisingWave
# использует 1мин; мы стартуем короче — «быстрый» прострел).
SPURT_WINDOW_MS_DEFAULT = 60_000
# variance для «одинакового принта» (Nydar 20% rule).
SAME_PRINT_VARIANCE = 0.20


@dataclass
class Spurt:
    """Обнаруженный прострел (+ триггер-принты для downstream-фильтров)."""
    exchange: str
    symbol: str
    ts: float                  # ts_exch старта
    direction: str             # "up" | "down"
    amplitude_pct: float
    duration_ms: int
    trigger_prints: list[Trade] = field(default_factory=list)
    trigger_cluster_size: float | None = None   # медиана размера кластера
    start_price: float = 0.0
    end_price: float = 0.0


def cluster_prints_by_size(prints: list[Trade],
                           variance: float = SAME_PRINT_VARIANCE
                           ) -> list[list[Trade]]:
    """Гистограммная кластеризация принтов по размеру (±variance).

    Сортируем по size; в один кластер — принты, у которых max/min ≤ 1+variance.
    Эквивалент DBSCAN по 1-D size с eps=variance, но детерминированный и без
    зависимостей. Возвращает список кластеров (каждый — список Trade).
    """
    if not prints:
        return []
    sorted_p = sorted(prints, key=lambda t: t.size)
    clusters: list[list[Trade]] = []
    cur: list[Trade] = [sorted_p[0]]
    for t in sorted_p[1:]:
        lo = min(p.size for p in cur)
        if t.size <= lo * (1 + variance):
            cur.append(t)
        else:
            clusters.append(cur)
            cur = [t]
    clusters.append(cur)
    return clusters


class SpurtDetector:
    """Оконный детектор прострелов на потоке трейдов одного символа.

    Подает трейды через ``apply_trade``; при накоплении движения ≥
    ``amplitude_pct`` за окно ``window_ms`` — эмитит ``Spurt`` через
    ``on_spurt``. Триггер-принты = агрессивные принты в окне, доминирующего
    направления (buy для up, sell для down).
    """

    def __init__(self, exchange: str, symbol: str, settings: YorshSettings,
                 *, on_spurt, window_ms: int = SPURT_WINDOW_MS_DEFAULT) -> None:
        self.exchange = exchange
        self.symbol = symbol
        self.s = settings
        self.window_ms = window_ms
        self._on_spurt = on_spurt
        # ring buffer of trades in current window
        self._window: list[Trade] = []
        self._last_emit_ts: float = 0.0

    def apply_trade(self, t: Trade) -> None:
        self._window.append(t)
        cutoff = t.ts_exch - self.window_ms / 1000.0
        self._window = [p for p in self._window if p.ts_exch >= cutoff]
        self._maybe_emit(t)

    def _maybe_emit(self, last: Trade) -> None:
        if len(self._window) < 2:
            return
        start = self._window[0]
        # запрет на повторную эмиссию внутри окна (cooldown)
        if last.ts_exch - self._last_emit_ts < self.window_ms / 1000.0 \
                and self._last_emit_ts > 0:
            return
        amp = (last.price - start.price) / start.price * 100.0
        if abs(amp) < self.s.spurt_min_amplitude_pct:
            return
        direction = "up" if amp > 0 else "down"
        # триггер-принты: доминирующего направления в окне
        triggers = [p for p in self._window
                    if (p.side == "buy" and direction == "up")
                    or (p.side == "sell" and direction == "down")]
        cluster_size: float | None = None
        clusters = cluster_prints_by_size(triggers)
        if clusters:
            # крупнейший кластер (по числу принтов) — доминирующий «тот же принт»
            biggest = max(clusters, key=len)
            cluster_size = statistics.median(p.size for p in biggest)
        dur_ms = int((last.ts_exch - start.ts_exch) * 1000)
        sp = Spurt(self.exchange, self.symbol, start.ts_exch, direction,
                   abs(amp), dur_ms, triggers, cluster_size,
                   start_price=start.price, end_price=last.price)
        self._last_emit_ts = last.ts_exch
        self._on_spurt(sp)
        # сбрасываем окно после эмиссии (прострел «закрыт»)
        self._window = []
