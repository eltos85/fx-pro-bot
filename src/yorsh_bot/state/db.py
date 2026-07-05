"""SQLite-состояние yorsh_bot: плотности, прострелы, кандидаты, операционка.

Хранится в ``{data_dir}/yorsh_bot.sqlite`` (volume yorsh_data). Схема —
раздел 3 ТЗ (docs/TZ_YORSH_SCANNER.md). Миграции идемпотентны (volume на VPS).

Таблицы:
- densities        — жизненный цикл L2-плотностей (genuine/iceberg/spoof/unknown)
- spurt_events     — ВСЕ прострелы (и не прошедшие фильтры — для калибровки M6)
- candidates       — «ёрш»-кандидаты (повторяющиеся прострелы от genuine density)
- universe_log     — история вселенной подписок
- collector_health — gaps/reconnects/lag коллекторов
- meta             — key/value для retention/cap событий + runtime-мета
"""
from __future__ import annotations

import os
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS densities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,             -- bid | ask
    price REAL NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    peak_size REAL NOT NULL,
    persistence_sec REAL NOT NULL DEFAULT 0,
    partial_fill_vol REAL NOT NULL DEFAULT 0,
    pull_count INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT 'unknown',  -- genuine|iceberg|spoof|unknown
    refilled INTEGER NOT NULL DEFAULT 0,       -- iceberg refill-флаг (0/1)
    moved INTEGER NOT NULL DEFAULT 0           -- переставлялась ли (0/1)
);
CREATE INDEX IF NOT EXISTS idx_densities_sym ON densities(exchange, symbol);
CREATE INDEX IF NOT EXISTS idx_densities_verdict ON densities(verdict);
CREATE INDEX IF NOT EXISTS idx_densities_last_seen ON densities(last_seen);

CREATE TABLE IF NOT EXISTS spurt_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,        -- up | down
    amplitude_pct REAL NOT NULL,
    duration_ms INTEGER NOT NULL,
    trigger_print_size REAL,
    density_id INTEGER,             -- FK densities.id (может быть NULL)
    revert_ms INTEGER,              -- время до отката (ms), NULL = не откатился
    passed_filters INTEGER NOT NULL DEFAULT 0,  -- 0/1 — прошёл ёрш-фильтры
    FOREIGN KEY (density_id) REFERENCES densities(id)
);
CREATE INDEX IF NOT EXISTS idx_spurt_sym ON spurt_events(exchange, symbol);
CREATE INDEX IF NOT EXISTS idx_spurt_ts ON spurt_events(ts);
CREATE INDEX IF NOT EXISTS idx_spurt_passed ON spurt_events(passed_filters);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    first_detected REAL NOT NULL,
    last_detected REAL NOT NULL,
    spurts_per_day REAL NOT NULL DEFAULT 0,
    regularity_pvalue REAL,         -- p-value repeat-frequency test (Poisson-null)
    print_cluster_size REAL,        -- медианный размер кластера принтов-триггеров
    status TEXT NOT NULL DEFAULT 'active'  -- active | closed
);
CREATE INDEX IF NOT EXISTS idx_cand_sym ON candidates(exchange, symbol);
CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cand_active
    ON candidates(exchange, symbol) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS universe_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    exchange TEXT NOT NULL,
    event TEXT NOT NULL,            -- add | remove | refresh
    symbol TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_universe_ts ON universe_log(ts);

CREATE TABLE IF NOT EXISTS collector_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT,
    event TEXT NOT NULL,            -- gap | reconnect | lag | snapshot | reinit
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON collector_health(ts);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at INTEGER
);
"""


class YorshDB:
    """SQLite-обёртка yorsh_bot. Идемпотентная инициализация + миграции."""

    def __init__(self, data_dir: str, *, filename: str = "yorsh_bot.sqlite") -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, filename)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Идемпотентные миграции для уже существующих БД (volume на VPS).

        На M0 схема создаётся с нуля — миграций нет, но хук оставлен для
        будущих milestone'ов (M4/M5 добавят колонки жизненного цикла).
        """
        # версия схемы в meta
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key,value,updated_at) VALUES (?,?,?)",
                ("schema_version", "1", int(time.time())))

    def close(self) -> None:
        self._conn.close()

    # ─── meta helpers (key/value) ────────────────────────────────────────
    def meta_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, int(time.time())))
        self._conn.commit()

    # ─── health/universe logging (для M1+; здесь — чтобы тесты могли звать) ──
    def log_health(self, *, exchange: str, event: str,
                   symbol: str | None = None, detail: str | None = None,
                   ts: float | None = None) -> None:
        t = ts if ts is not None else time.time()
        self._conn.execute(
            "INSERT INTO collector_health(ts,exchange,symbol,event,detail) "
            "VALUES (?,?,?,?,?)",
            (t, exchange, symbol, event, detail))
        self._conn.commit()

    def log_universe(self, *, exchange: str, event: str,
                     symbol: str | None = None, detail: str | None = None,
                     ts: float | None = None) -> None:
        t = ts if ts is not None else time.time()
        self._conn.execute(
            "INSERT INTO universe_log(ts,exchange,event,symbol,detail) "
            "VALUES (?,?,?,?,?)",
            (t, exchange, event, symbol, detail))
        self._conn.commit()

    @property
    def path(self) -> str:
        return self._path

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ─── densities (M4) ──────────────────────────────────────────────────
    def insert_density(self, *, exchange: str, symbol: str, side: str,
                       price: float, first_seen: float, last_seen: float,
                       peak_size: float, verdict: str = "unknown",
                       persistence_sec: float = 0.0,
                       partial_fill_vol: float = 0.0,
                       pull_count: int = 0, refilled: int = 0,
                       moved: int = 0) -> int:
        cur = self._conn.execute(
            "INSERT INTO densities(exchange,symbol,side,price,first_seen,"
            "last_seen,peak_size,persistence_sec,partial_fill_vol,pull_count,"
            "verdict,refilled,moved) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (exchange, symbol, side, price, first_seen, last_seen, peak_size,
             persistence_sec, partial_fill_vol, pull_count, verdict,
             refilled, moved))
        self._conn.commit()
        return cur.lastrowid

    def update_density(self, density_id: int, *,
                       last_seen: float, peak_size: float,
                       persistence_sec: float, partial_fill_vol: float,
                       pull_count: int, refilled: int, moved: int,
                       verdict: str) -> None:
        self._conn.execute(
            "UPDATE densities SET last_seen=?,peak_size=?,persistence_sec=?,"
            "partial_fill_vol=?,pull_count=?,refilled=?,moved=?,verdict=? "
            "WHERE id=?",
            (last_seen, peak_size, persistence_sec, partial_fill_vol,
             pull_count, refilled, moved, verdict, density_id))
        self._conn.commit()

    # ─── spurt_events / candidates (M5) ──────────────────────────────────
    def insert_spurt(self, *, exchange: str, symbol: str, ts: float,
                     direction: str, amplitude_pct: float, duration_ms: int,
                     trigger_print_size: float | None = None,
                     density_id: int | None = None,
                     revert_ms: int | None = None,
                     passed_filters: int = 0) -> int:
        cur = self._conn.execute(
            "INSERT INTO spurt_events(exchange,symbol,ts,direction,"
            "amplitude_pct,duration_ms,trigger_print_size,density_id,"
            "revert_ms,passed_filters) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (exchange, symbol, ts, direction, amplitude_pct, duration_ms,
             trigger_print_size, density_id, revert_ms, passed_filters))
        self._conn.commit()
        return cur.lastrowid

    def upsert_candidate(self, *, exchange: str, symbol: str,
                         first_detected: float, last_detected: float,
                         spurts_per_day: float,
                         regularity_pvalue: float | None = None,
                         print_cluster_size: float | None = None) -> None:
        """Закрыть старого active-кандидата (если был) + вставить нового active.

        UNIQUE-индекс uq_cand_active гарантирует одного active на (exch,symbol).
        """
        self._conn.execute(
            "UPDATE candidates SET status='closed' "
            "WHERE exchange=? AND symbol=? AND status='active'",
            (exchange, symbol))
        self._conn.execute(
            "INSERT INTO candidates(exchange,symbol,first_detected,"
            "last_detected,spurts_per_day,regularity_pvalue,"
            "print_cluster_size,status) VALUES (?,?,?,?,?,?,?,'active')",
            (exchange, symbol, first_detected, last_detected,
             spurts_per_day, regularity_pvalue, print_cluster_size))
        self._conn.commit()

    def spurts_for_day(self, exchange: str, symbol: str,
                       day_start: float, day_end: float) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM spurt_events WHERE exchange=? AND symbol=? "
            "AND ts>=? AND ts<? ORDER BY ts",
            (exchange, symbol, day_start, day_end)).fetchall()

    def active_candidates(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM candidates WHERE status='active' "
            "ORDER BY last_detected DESC").fetchall()

    def densities_near(self, exchange: str, symbol: str,
                       price: float, *, ts_before: float,
                       price_tol: float, verdict_in: tuple[str, ...]) -> list[sqlite3.Row]:
        """Плотности genuine/iceberg рядом с ценой, активные перед ts."""
        ph = ",".join("?" for _ in verdict_in)
        return self._conn.execute(
            f"SELECT * FROM densities WHERE exchange=? AND symbol=? "
            f"AND verdict IN ({ph}) AND first_seen<=? AND last_seen>=? "
            f"AND ABS(price-?)<=? ORDER BY ABS(price-?)",
            (exchange, symbol, *verdict_in, ts_before, ts_before,
             price, price_tol, price)).fetchall()
