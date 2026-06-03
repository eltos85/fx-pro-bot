"""SQLite: дедупликация уже отправленных алертов."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class SignalStore:
    def __init__(self, data_dir: str) -> None:
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self._db = path / "ru_stocks.sqlite3"
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_signals (
                    ticker TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (ticker, direction)
                )
                """
            )

    def was_sent(self, ticker: str, direction: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM sent_signals WHERE ticker=? AND direction=?",
                (ticker.upper(), direction),
            ).fetchone()
        return row is not None

    def mark_sent(self, ticker: str, direction: str, sent_at: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO sent_signals (ticker, direction, sent_at)
                VALUES (?, ?, ?)
                ON CONFLICT(ticker, direction) DO UPDATE SET sent_at=excluded.sent_at
                """,
                (ticker.upper(), direction, sent_at),
            )
