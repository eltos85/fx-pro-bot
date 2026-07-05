"""Сырая запись потока (milestone M1).

Партиции ``{YORSH_DATA_DIR}/raw/{exchange}/{symbol}/{YYYY-MM-DD}/{HH}.jsonl.gz``
— одна строка = одно событие
``{"ts_exch":..., "ts_local":..., "type":"trade|diff|snapshot", "payload":...}``.
Ротация файла по часу. Retention по ``YORSH_RAW_RETENTION_DAYS`` и cap
``YORSH_RAW_MAX_GB`` — при превышении удаляются самые старые партиции,
событие пишется в лог и в ``meta`` (через callback, чтобы не тащить БД-зависимость).

gzip-stream: каждый write — отдельная json-строка, flush в gzip-фрейм. Файл
открывается лениво и держится открытым до ротации часа (или close).
"""
from __future__ import annotations

import glob
import gzip
import json
import logging
import os
import time
from typing import Any, Callable

from yorsh_bot.exchanges.base import BookSnapshot, DepthDiff, Trade

log = logging.getLogger("yorsh_bot.recorder")

# callback для логирования retention/cap событий в collector_health/meta.
# signature: (event:str, detail:str) -> None
HealthLogger = Callable[[str, str], None]


class RawRecorder:
    """Сырая запись ВСЕХ событий (trades, diffs, снапшоты) в jsonl.gz по часам.

    Потокобезопасность: один recorder = один коллектор (один event-loop).
    Для параллельных бирж — по recorder'у на биржу (или внешний lock).
    """

    def __init__(self, data_dir: str, *, exchange: str,
                 retention_days: int = 30, max_gb: float = 20.0,
                 health_log: HealthLogger | None = None) -> None:
        self.data_dir = data_dir
        self.exchange = exchange
        self.retention_days = retention_days
        self.max_gb = max_gb
        self._health = health_log
        self._fh: gzip.GzipFile | None = None
        self._cur_hour_key: tuple[str, str] | None = None  # (date, hour)
        self._cur_path: str | None = None

    # ─── path ────────────────────────────────────────────────────────────
    def _partition_path(self, symbol: str, date: str, hour: str) -> str:
        return os.path.join(
            self.data_dir, "raw", self.exchange, symbol, date,
            f"{hour}.jsonl.gz")

    @staticmethod
    def _utc_hour_key(ts: float) -> tuple[str, str, str]:
        # UTC date/hour из unix-секунд (таймстемпы событий — биржевые UTC).
        import time as _t
        gm = _t.gmtime(ts)
        date = f"{gm.tm_year:04d}-{gm.tm_mon:02d}-{gm.tm_mday:02d}"
        hour = f"{gm.tm_hour:02d}"
        return date, hour, date

    # ─── write ───────────────────────────────────────────────────────────
    def write_trade(self, t: Trade) -> None:
        self._write(t.ts_local, "trade", t.symbol, {
            "ts_exch": t.ts_exch, "price": t.price, "size": t.size,
            "side": t.side, "payload": t.payload})

    def write_diff(self, d: DepthDiff) -> None:
        self._write(d.ts_local, "diff", d.symbol, {
            "ts_exch": d.ts_exch, "bids": d.bids, "asks": d.asks,
            "seq": d.seq, "prev_seq": d.prev_seq, "payload": d.payload})

    def write_snapshot(self, s: BookSnapshot) -> None:
        self._write(s.ts_local, "snapshot", s.symbol, {
            "ts_exch": s.ts_exch, "bids": s.bids, "asks": s.asks,
            "seq": s.seq, "payload": s.payload})

    def _write(self, ts_local: float, etype: str, symbol: str,
               body: dict[str, Any]) -> None:
        date, hour, _ = self._utc_hour_key(ts_local)
        key = (date, hour)
        if key != self._cur_hour_key:
            self._rotate(symbol, date, hour)
        assert self._fh is not None
        rec = {"ts_local": ts_local, "type": etype, "exchange": self.exchange,
               "symbol": symbol, **body}
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        self._fh.write(line.encode("utf-8"))
        # не flush каждый раз (gzip-фрейм дорогой); закроется при ротации.
        # Но чтобы не терять данные при краше — periodic flush делает caller
        # через flush().

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._cur_hour_key = None
            self._cur_path = None

    def _rotate(self, symbol: str, date: str, hour: str) -> None:
        self.close()
        path = self._partition_path(symbol, date, hour)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # append-режим — несколько collector-циклов в один час-файл.
        self._fh = gzip.open(path, "ab")
        self._cur_hour_key = (date, hour)
        self._cur_path = path
        # после ротации — проверка retention/cap (дешёвый best-effort).
        self._enforce_retention()

    # ─── retention / cap ─────────────────────────────────────────────────
    def _raw_root(self) -> str:
        return os.path.join(self.data_dir, "raw", self.exchange)

    def _partition_dirs(self) -> list[str]:
        """Все партиции (…/{symbol}/{date}/) — для retention/cap."""
        root = self._raw_root()
        if not os.path.isdir(root):
            return []
        out = []
        for sym in os.listdir(root):
            sym_dir = os.path.join(root, sym)
            if not os.path.isdir(sym_dir):
                continue
            for date in os.listdir(sym_dir):
                dd = os.path.join(sym_dir, date)
                if os.path.isdir(dd):
                    out.append(dd)
        return out

    def _dir_size_bytes(self, d: str) -> int:
        total = 0
        for f in glob.glob(os.path.join(d, "*.jsonl.gz")):
            try:
                total += os.path.getsize(f)
            except OSError:
                pass
        return total

    def _enforce_retention(self) -> None:
        """Удалить партиции старше retention_days и/или при превышении max_gb.

        Retention — по дате в имени партиции (``.../{symbol}/{YYYY-MM-DD}/``),
        не по mtime: mtime чурается на VPS/копиях, а дата партиции = канон.
        Cap — по суммарному размеру, удаляем самые старые (по дате) партиции.
        """
        import datetime as _dt
        parts = self._partition_dirs()
        if not parts:
            return
        today = _dt.date.today()
        cutoff = today - _dt.timedelta(days=self.retention_days)

        def part_date_str(d: str) -> str:
            return os.path.basename(d)  # YYYY-MM-DD

        def part_date(d: str) -> _dt.date:
            return _dt.date.fromisoformat(part_date_str(d))

        parts_sorted = sorted(parts, key=part_date_str)
        deleted: list[str] = []

        # 1) retention по дате партиции
        for d in list(parts_sorted):
            try:
                pd = part_date(d)
            except ValueError:
                continue
            if pd < cutoff:
                self._remove_partition(d)
                deleted.append(d)
                parts_sorted.remove(d)

        # 2) cap по размеру (удаляем самые старые, пока не уложимся)
        total_gb = self._total_size_gb()
        if total_gb > self.max_gb:
            for d in parts_sorted:
                if total_gb <= self.max_gb:
                    break
                self._remove_partition(d)
                deleted.append(d)
                total_gb = self._total_size_gb()

        if deleted and self._health is not None:
            self._health("retention",
                         f"removed {len(deleted)} partitions, "
                         f"oldest={part_date_str(deleted[0])}")

    def _total_size_gb(self) -> float:
        total = 0
        for d in self._partition_dirs():
            total += self._dir_size_bytes(d)
        return total / (1024 ** 3)

    def _remove_partition(self, d: str) -> None:
        for f in glob.glob(os.path.join(d, "*.jsonl.gz")):
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass
