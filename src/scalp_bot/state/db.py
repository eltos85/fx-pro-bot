"""SQLite-состояние scalp_bot: сделки + агрегаты для killswitch.

Хранится в ``{data_dir}/scalp_bot.sqlite`` (volume scalp_bot_data).
``realized_pnl_usd`` — расчётный net с учётом ``fees_usd``; для аудита
PnL ground truth = биржевая выписка (stats-collection.mdc), БД —
приблизительный источник для killswitch и трассировки.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    score INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    entry_order_id TEXT,
    ts_close REAL,
    exit REAL,
    pnl_usd REAL,
    fees_usd REAL,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ts_close ON trades(ts_close);
"""


@dataclass
class TradeRow:
    id: int
    ts_open: float
    symbol: str
    side: str
    qty: float
    entry: float
    sl: float
    tp: float
    score: int
    reasons: str
    mode: str
    status: str
    entry_order_id: str | None
    ts_close: float | None
    exit: float | None
    pnl_usd: float | None
    fees_usd: float | None
    close_reason: str | None


class ScalpDB:
    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "scalp_bot.sqlite")
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ─── writes ──────────────────────────────────────────────────────────

    def insert_open(
        self, *, symbol: str, side: str, qty: float, entry: float, sl: float,
        tp: float, score: int, reasons: str, mode: str,
        entry_order_id: str | None = None, ts_open: float | None = None,
    ) -> int:
        ts = ts_open if ts_open is not None else time.time()
        cur = self._conn.execute(
            "INSERT INTO trades (ts_open,symbol,side,qty,entry,sl,tp,score,"
            "reasons,mode,status,entry_order_id) VALUES (?,?,?,?,?,?,?,?,?,?,'open',?)",
            (ts, symbol, side, qty, entry, sl, tp, score, reasons, mode, entry_order_id),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def mark_closed(
        self, trade_id: int, *, exit_price: float, pnl_usd: float,
        fees_usd: float, close_reason: str, ts_close: float | None = None,
    ) -> None:
        ts = ts_close if ts_close is not None else time.time()
        self._conn.execute(
            "UPDATE trades SET status='closed', ts_close=?, exit=?, pnl_usd=?, "
            "fees_usd=?, close_reason=? WHERE id=?",
            (ts, exit_price, pnl_usd, fees_usd, close_reason, trade_id),
        )
        self._conn.commit()

    # ─── reads ───────────────────────────────────────────────────────────

    def open_trades(self) -> list[TradeRow]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY id"
        ).fetchall()
        return [self._row(r) for r in rows]

    def realized_pnl_since(self, ts: float) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM trades "
            "WHERE status='closed' AND ts_close>=?",
            (ts,),
        ).fetchone()
        return float(row["s"] or 0.0)

    def total_realized_pnl(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM trades WHERE status='closed'"
        ).fetchone()
        return float(row["s"] or 0.0)

    def trades_since(self, ts: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE ts_open>=?", (ts,)
        ).fetchone()
        return int(row["c"] or 0)

    def open_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE status='open'"
        ).fetchone()
        return int(row["c"] or 0)

    @staticmethod
    def _row(r: sqlite3.Row) -> TradeRow:
        return TradeRow(
            id=r["id"], ts_open=r["ts_open"], symbol=r["symbol"], side=r["side"],
            qty=r["qty"], entry=r["entry"], sl=r["sl"], tp=r["tp"], score=r["score"],
            reasons=r["reasons"], mode=r["mode"], status=r["status"],
            entry_order_id=r["entry_order_id"], ts_close=r["ts_close"], exit=r["exit"],
            pnl_usd=r["pnl_usd"], fees_usd=r["fees_usd"], close_reason=r["close_reason"],
        )
