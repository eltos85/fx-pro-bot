"""SQLite hybrid_bot: своя позиция и свои сделки.

Схема ``trades`` совпадает со схемой scalp_bot (TASKSPEC_TRADECARD_BYBIT §3.1),
чтобы tradecard-bybit мог читать её тем же загрузчиком.

Стоп-лосса у стратегии нет (STRATEGY_HYBRID.md §17.4), поэтому ``sl`` равен
цене входа: tradecard в таком случае не считает R-метрики, а не рисует
бессмысленные значения.
"""

from __future__ import annotations

import sqlite3
import time


class HybridDB:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS positions (
                 symbol TEXT PRIMARY KEY,
                 side TEXT NOT NULL,
                 qty REAL NOT NULL,
                 avg_entry REAL NOT NULL,
                 ts_open INTEGER NOT NULL,
                 link_id TEXT NOT NULL,
                 fixations INTEGER NOT NULL DEFAULT 0
               )""")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 ts_open REAL NOT NULL,
                 ts_close REAL,
                 symbol TEXT NOT NULL,
                 side TEXT NOT NULL,
                 qty REAL NOT NULL,
                 entry REAL NOT NULL,
                 exit REAL,
                 sl REAL NOT NULL DEFAULT 0,
                 tp REAL NOT NULL DEFAULT 0,
                 score INTEGER NOT NULL DEFAULT 0,
                 reasons TEXT NOT NULL DEFAULT '',
                 mode TEXT NOT NULL,
                 strategy TEXT NOT NULL,
                 status TEXT NOT NULL,
                 pnl_usd REAL,
                 fees_usd REAL,
                 close_reason TEXT
               )""")
        self._db.commit()

    # ─── своя позиция ────────────────────────────────────────────────────

    def owned(self, symbol: str) -> dict | None:
        row = self._db.execute(
            "SELECT symbol, side, qty, avg_entry, ts_open, link_id, fixations "
            "FROM positions WHERE symbol=?", (symbol,)).fetchone()
        if not row:
            return None
        keys = ("symbol", "side", "qty", "avg_entry", "ts_open", "link_id",
                "fixations")
        return dict(zip(keys, row))

    def open_pos(self, symbol: str, side: str, qty: float, entry: float,
                 link_id: str, *, fixations: int = 0) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO positions "
            "(symbol, side, qty, avg_entry, ts_open, link_id, fixations) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol, side, qty, entry, int(time.time()), link_id, fixations))
        self._db.commit()

    def drop_pos(self, symbol: str) -> None:
        self._db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self._db.commit()

    def open_notional(self, exclude: str | None = None) -> float:
        """Сколько денег бота уже стоит в позициях (по цене входа).

        Нужно, чтобы суммарный объём не вышел за пределы капитала, которым бот
        считает себя ограниченным.
        """
        q = "SELECT COALESCE(SUM(qty * avg_entry), 0) FROM positions"
        args: tuple = ()
        if exclude:
            q += " WHERE symbol<>?"
            args = (exclude,)
        row = self._db.execute(q, args).fetchone()
        return float(row[0] or 0.0)

    # ─── сделки ──────────────────────────────────────────────────────────

    def record_closed(self, pos: dict, *, exit_px: float, reason: str,
                      mode: str, strategy: str, fees_usd: float = 0.0) -> int:
        """Пишет закрытую сделку. Деньги считаются от средней цены входа."""
        sign = 1.0 if pos["side"] == "Buy" else -1.0
        pnl = sign * (exit_px - pos["avg_entry"]) * pos["qty"]
        cur = self._db.execute(
            "INSERT INTO trades (ts_open, ts_close, symbol, side, qty, entry, "
            "exit, sl, tp, score, reasons, mode, strategy, status, pnl_usd, "
            "fees_usd, close_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'closed',?,?,?)",
            (float(pos["ts_open"]), time.time(), pos["symbol"],
             "long" if pos["side"] == "Buy" else "short", pos["qty"],
             pos["avg_entry"], exit_px, pos["avg_entry"], 0.0, 0, reason,
             mode, strategy, pnl, fees_usd, reason))
        self._db.commit()
        return int(cur.lastrowid or 0)

    def closed_today(self, symbol: str | None = None) -> list[dict]:
        """Закрытия за последние 24 часа — для дневной сводки."""
        since = time.time() - 86400
        q = ("SELECT symbol, side, qty, entry, exit, pnl_usd, close_reason, "
             "ts_close FROM trades WHERE status='closed' AND ts_close>=?")
        args: list = [since]
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        q += " ORDER BY ts_close"
        keys = ("symbol", "side", "qty", "entry", "exit", "pnl_usd",
                "close_reason", "ts_close")
        return [dict(zip(keys, r))
                for r in self._db.execute(q, args).fetchall()]
