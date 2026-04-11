"""SQLite-хранилище статистики для Bybit-бота."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class PositionRow:
    id: int
    symbol: str
    side: str
    qty: str
    entry_price: float
    sl: float | None
    tp: float | None
    order_id: str
    strategy: str
    signal_strength: float
    signal_reasons: str
    opened_at: str
    closed_at: str | None = None
    exit_price: float | None = None
    pnl_usd: float | None = None
    close_reason: str | None = None


@dataclass
class SignalRow:
    id: int
    symbol: str
    direction: str
    strength: float
    reasons: str
    price: float
    created_at: str


class StatsStore:
    """SQLite для хранения сигналов и позиций Bybit-бота."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._connect()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._connect()

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                strength REAL NOT NULL,
                reasons TEXT,
                price REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty TEXT NOT NULL,
                entry_price REAL NOT NULL,
                sl REAL,
                tp REAL,
                order_id TEXT,
                strategy TEXT NOT NULL DEFAULT 'ensemble',
                signal_strength REAL DEFAULT 0,
                signal_reasons TEXT DEFAULT '',
                opened_at TEXT NOT NULL DEFAULT (datetime('now')),
                closed_at TEXT,
                exit_price REAL,
                pnl_usd REAL,
                close_reason TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
            CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(closed_at);
            CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
        """)

    def log_signal(
        self,
        symbol: str,
        direction: str,
        strength: float,
        reasons: str,
        price: float,
    ) -> int:
        now = datetime.now(tz=UTC).isoformat()
        cur = self.conn.execute(
            "INSERT INTO signals (symbol, direction, strength, reasons, price, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, direction, strength, reasons, price, now),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def open_position(
        self,
        symbol: str,
        side: str,
        qty: str,
        entry_price: float,
        order_id: str,
        *,
        sl: float | None = None,
        tp: float | None = None,
        strategy: str = "ensemble",
        signal_strength: float = 0.0,
        signal_reasons: str = "",
    ) -> int:
        now = datetime.now(tz=UTC).isoformat()
        cur = self.conn.execute(
            "INSERT INTO positions "
            "(symbol, side, qty, entry_price, sl, tp, order_id, strategy, "
            " signal_strength, signal_reasons, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, side, qty, entry_price, sl, tp, order_id, strategy,
             signal_strength, signal_reasons, now),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def close_position(
        self,
        position_id: int,
        exit_price: float,
        pnl_usd: float,
        close_reason: str = "signal",
    ) -> None:
        now = datetime.now(tz=UTC).isoformat()
        self.conn.execute(
            "UPDATE positions SET closed_at=?, exit_price=?, pnl_usd=?, close_reason=? "
            "WHERE id=?",
            (now, exit_price, pnl_usd, close_reason, position_id),
        )
        self.conn.commit()

    def get_open_positions(self) -> list[PositionRow]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at",
        ).fetchall()
        return [self._row_to_position(r) for r in rows]

    def get_open_position_by_symbol(self, symbol: str) -> PositionRow | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE symbol=? AND closed_at IS NULL LIMIT 1",
            (symbol,),
        ).fetchone()
        return self._row_to_position(row) if row else None

    def get_daily_pnl(self, date_str: str | None = None) -> float:
        if date_str is None:
            date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl_usd), 0) as total FROM positions "
            "WHERE closed_at IS NOT NULL AND closed_at >= ?",
            (date_str,),
        ).fetchone()
        return float(row["total"]) if row else 0.0

    def get_total_stats(self) -> dict:
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(pnl_usd), 0) as total_pnl,
                COALESCE(AVG(pnl_usd), 0) as avg_pnl
            FROM positions WHERE closed_at IS NOT NULL
        """).fetchone()
        if not row:
            return {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0, "avg_pnl": 0}
        total = row["total_trades"]
        return {
            "total_trades": total,
            "wins": row["wins"] or 0,
            "losses": row["losses"] or 0,
            "total_pnl": round(row["total_pnl"], 2),
            "avg_pnl": round(row["avg_pnl"], 2),
            "win_rate": round((row["wins"] or 0) / total * 100, 1) if total > 0 else 0,
        }

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> PositionRow:
        return PositionRow(
            id=row["id"],
            symbol=row["symbol"],
            side=row["side"],
            qty=row["qty"],
            entry_price=row["entry_price"],
            sl=row["sl"],
            tp=row["tp"],
            order_id=row["order_id"],
            strategy=row["strategy"],
            signal_strength=row["signal_strength"],
            signal_reasons=row["signal_reasons"],
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
            exit_price=row["exit_price"],
            pnl_usd=row["pnl_usd"],
            close_reason=row["close_reason"],
        )
