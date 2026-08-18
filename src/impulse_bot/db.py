"""SQLite impulse-bot. Чужие позиции на общем счёте не трогаем."""

from __future__ import annotations

import sqlite3
import time


class ImpulseDB:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS positions (
                 symbol TEXT PRIMARY KEY,
                 side TEXT NOT NULL,
                 qty REAL NOT NULL,
                 entry REAL NOT NULL,
                 sl REAL NOT NULL,
                 tp REAL NOT NULL,
                 ts_open INTEGER NOT NULL,
                 link_id TEXT NOT NULL
               )""")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts_open INTEGER, ts_close INTEGER,
                 symbol TEXT, side TEXT, qty REAL,
                 entry REAL, exit REAL, pnl_usd REAL, reason TEXT
               )""")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS session_day (
                 day TEXT PRIMARY KEY,
                 trades INTEGER NOT NULL
               )""")
        self._db.commit()

    def owned(self, symbol: str) -> dict | None:
        row = self._db.execute(
            "SELECT symbol, side, qty, entry, sl, tp, ts_open, link_id "
            "FROM positions WHERE symbol=?", (symbol,)).fetchone()
        if not row:
            return None
        keys = ("symbol", "side", "qty", "entry", "sl", "tp", "ts_open", "link_id")
        return dict(zip(keys, row))

    def open_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM positions").fetchone()[0])

    def all_owned(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT symbol, side, qty, entry, sl, tp, ts_open, link_id "
            "FROM positions").fetchall()
        keys = ("symbol", "side", "qty", "entry", "sl", "tp", "ts_open", "link_id")
        return [dict(zip(keys, r)) for r in rows]

    def open_pos(self, symbol: str, side: str, qty: float, entry: float,
                 sl: float, tp: float, link_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO positions "
            "(symbol, side, qty, entry, sl, tp, ts_open, link_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (symbol, side, qty, entry, sl, tp, int(time.time()), link_id))
        self._db.commit()

    def close_pos(self, symbol: str, exit_px: float, reason: str) -> None:
        pos = self.owned(symbol)
        if not pos:
            return
        sign = 1 if pos["side"] == "Buy" else -1
        pnl = sign * (exit_px - pos["entry"]) * pos["qty"]
        self._db.execute(
            "INSERT INTO trades (ts_open, ts_close, symbol, side, qty, "
            "entry, exit, pnl_usd, reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (pos["ts_open"], int(time.time()), symbol, pos["side"], pos["qty"],
             pos["entry"], exit_px, pnl, reason))
        self._db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self._db.commit()

    def session_trades(self, day: str) -> int:
        row = self._db.execute(
            "SELECT trades FROM session_day WHERE day=?", (day,)).fetchone()
        return int(row[0]) if row else 0

    def bump_session(self, day: str) -> None:
        n = self.session_trades(day) + 1
        self._db.execute(
            "INSERT OR REPLACE INTO session_day (day, trades) VALUES (?,?)",
            (day, n))
        self._db.commit()
