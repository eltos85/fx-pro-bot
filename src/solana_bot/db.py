"""SQLite solana-bot."""

from __future__ import annotations

import sqlite3
import time


class SolanaDB:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS positions (
                 mint TEXT PRIMARY KEY,
                 symbol TEXT NOT NULL,
                 qty REAL NOT NULL,
                 entry REAL NOT NULL,
                 ts_open INTEGER NOT NULL
               )""")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts_open INTEGER, ts_close INTEGER,
                 mint TEXT, symbol TEXT, qty REAL,
                 entry REAL, exit REAL, pnl_pct REAL, reason TEXT
               )""")
        self._db.commit()

    def owned(self, mint: str) -> dict | None:
        row = self._db.execute(
            "SELECT mint, symbol, qty, entry, ts_open FROM positions "
            "WHERE mint=?", (mint,)).fetchone()
        if not row:
            return None
        return dict(zip(("mint", "symbol", "qty", "entry", "ts_open"), row))

    def open_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM positions").fetchone()[0])

    def all_owned(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT mint, symbol, qty, entry, ts_open FROM positions").fetchall()
        keys = ("mint", "symbol", "qty", "entry", "ts_open")
        return [dict(zip(keys, r)) for r in rows]

    def open_pos(self, mint: str, symbol: str, qty: float, entry: float) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO positions "
            "(mint, symbol, qty, entry, ts_open) VALUES (?,?,?,?,?)",
            (mint, symbol, qty, entry, int(time.time())))
        self._db.commit()

    def close_pos(self, mint: str, exit_px: float, reason: str) -> None:
        pos = self.owned(mint)
        if not pos:
            return
        pnl = ((exit_px / pos["entry"]) - 1.0) * 100.0 if pos["entry"] else 0.0
        self._db.execute(
            "INSERT INTO trades (ts_open, ts_close, mint, symbol, qty, "
            "entry, exit, pnl_pct, reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (pos["ts_open"], int(time.time()), mint, pos["symbol"], pos["qty"],
             pos["entry"], exit_px, pnl, reason))
        self._db.execute("DELETE FROM positions WHERE mint=?", (mint,))
        self._db.commit()
