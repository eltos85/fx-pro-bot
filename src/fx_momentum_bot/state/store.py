from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class MomentumStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS momentum_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    momentum_value REAL NOT NULL,
                    atr REAL NOT NULL,
                    close_price REAL NOT NULL,
                    executed INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS momentum_state (
                    symbol TEXT PRIMARY KEY,
                    last_direction TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.commit()

    def get_last_direction(self, symbol: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_direction FROM momentum_state WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return str(row[0]) if row else None

    def set_last_direction(self, symbol: str, direction: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO momentum_state(symbol, last_direction, updated_at)
                VALUES(?, ?, datetime('now'))
                ON CONFLICT(symbol) DO UPDATE SET
                    last_direction = excluded.last_direction,
                    updated_at = datetime('now')
                """,
                (symbol, direction),
            )
            conn.commit()

    def add_decision(
        self,
        *,
        symbol: str,
        direction: str,
        momentum_value: float,
        atr: float,
        close_price: float,
        executed: bool,
        note: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO momentum_decisions(
                    symbol, direction, momentum_value, atr, close_price, executed, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    direction,
                    momentum_value,
                    atr,
                    close_price,
                    int(executed),
                    note,
                ),
            )
            conn.commit()

    def recent_decisions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, symbol, direction, momentum_value, atr, close_price, executed, note
                FROM momentum_decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        keys = [
            "created_at",
            "symbol",
            "direction",
            "momentum_value",
            "atr",
            "close_price",
            "executed",
            "note",
        ]
        return [dict(zip(keys, row, strict=False)) for row in rows]

