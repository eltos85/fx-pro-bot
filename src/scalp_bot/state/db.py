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
    strategy TEXT NOT NULL DEFAULT 'sweep_fade',
    status TEXT NOT NULL DEFAULT 'open',
    entry_order_id TEXT,
    ts_close REAL,
    exit REAL,
    pnl_usd REAL,
    fees_usd REAL,
    close_reason TEXT,
    pnl_provisional INTEGER NOT NULL DEFAULT 0
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
    strategy: str
    status: str
    entry_order_id: str | None
    ts_close: float | None
    exit: float | None
    pnl_usd: float | None
    fees_usd: float | None
    close_reason: str | None
    pnl_provisional: int = 0


@dataclass
class StrategyStat:
    """Сводка по одной стратегии за период (для постратегийного мониторинга)."""
    strategy: str
    trades: int
    wins: int
    losses: int
    pnl_usd: float

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return (self.wins / decided) if decided else 0.0


class ScalpDB:
    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "scalp_bot.sqlite")
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Идемпотентные миграции для уже существующих БД (volume на VPS)."""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "strategy" not in cols:
            # старые сделки до мультистратегии — это sweep_fade
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN strategy TEXT NOT NULL "
                "DEFAULT 'sweep_fade'")
        if "pnl_provisional" not in cols:
            # PnL предварительный (оценка), требует сверки с биржей
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN pnl_provisional INTEGER "
                "NOT NULL DEFAULT 0")
        # индекс создаём после миграции (на старой БД колонки ещё не было)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy)")

    def close(self) -> None:
        self._conn.close()

    # ─── writes ──────────────────────────────────────────────────────────

    def insert_open(
        self, *, symbol: str, side: str, qty: float, entry: float, sl: float,
        tp: float, score: int, reasons: str, mode: str,
        strategy: str = "sweep_fade",
        entry_order_id: str | None = None, ts_open: float | None = None,
    ) -> int:
        ts = ts_open if ts_open is not None else time.time()
        cur = self._conn.execute(
            "INSERT INTO trades (ts_open,symbol,side,qty,entry,sl,tp,score,"
            "reasons,mode,strategy,status,entry_order_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?)",
            (ts, symbol, side, qty, entry, sl, tp, score, reasons, mode,
             strategy, entry_order_id),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def mark_closed(
        self, trade_id: int, *, exit_price: float, pnl_usd: float,
        fees_usd: float, close_reason: str, ts_close: float | None = None,
        provisional: bool = False,
    ) -> None:
        ts = ts_close if ts_close is not None else time.time()
        self._conn.execute(
            "UPDATE trades SET status='closed', ts_close=?, exit=?, pnl_usd=?, "
            "fees_usd=?, close_reason=?, pnl_provisional=? WHERE id=?",
            (ts, exit_price, pnl_usd, fees_usd, close_reason,
             1 if provisional else 0, trade_id),
        )
        self._conn.commit()

    def finalize_pnl(self, trade_id: int, *, pnl_usd: float,
                     exit_price: float | None = None) -> None:
        """Заменить предварительный (оценочный) PnL реальным closedPnl с биржи
        и снять флаг pnl_provisional (после сверки в reconcile)."""
        if exit_price is not None:
            self._conn.execute(
                "UPDATE trades SET pnl_usd=?, exit=?, pnl_provisional=0 WHERE id=?",
                (pnl_usd, exit_price, trade_id))
        else:
            self._conn.execute(
                "UPDATE trades SET pnl_usd=?, pnl_provisional=0 WHERE id=?",
                (pnl_usd, trade_id))
        self._conn.commit()

    def provisional_closed_since(self, ts: float) -> list[TradeRow]:
        """Закрытые сделки с оценочным PnL (нужна сверка с биржей), ts_close>=ts."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND pnl_provisional=1 "
            "AND ts_close>=? ORDER BY id", (ts,)
        ).fetchall()
        return [self._row(r) for r in rows]

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

    def stats_by_strategy(self, since: float = 0.0) -> list[StrategyStat]:
        """Постратегийная сводка по ЗАКРЫТЫМ сделкам с ts_close>=since.

        wins/losses считаем по знаку pnl_usd; pnl_usd в БД — net closedPnl
        (с комиссиями, см. модульный docstring). Реконсил-закрытия
        (restart_flat / entry_*) исключаем — это не торговые исходы.
        """
        rows = self._conn.execute(
            "SELECT strategy, "
            "COUNT(*) AS trades, "
            "SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) AS losses, "
            "COALESCE(SUM(pnl_usd),0) AS pnl "
            "FROM trades WHERE status='closed' AND ts_close>=? "
            "AND close_reason NOT IN ('restart_flat','entry_Cancelled',"
            "'entry_Rejected','entry_Deactivated','entry_timeout') "
            "GROUP BY strategy ORDER BY pnl DESC",
            (since,),
        ).fetchall()
        return [
            StrategyStat(
                strategy=r["strategy"], trades=int(r["trades"] or 0),
                wins=int(r["wins"] or 0), losses=int(r["losses"] or 0),
                pnl_usd=float(r["pnl"] or 0.0),
            )
            for r in rows
        ]

    @staticmethod
    def _row(r: sqlite3.Row) -> TradeRow:
        return TradeRow(
            id=r["id"], ts_open=r["ts_open"], symbol=r["symbol"], side=r["side"],
            qty=r["qty"], entry=r["entry"], sl=r["sl"], tp=r["tp"], score=r["score"],
            reasons=r["reasons"], mode=r["mode"], strategy=r["strategy"],
            status=r["status"], entry_order_id=r["entry_order_id"],
            ts_close=r["ts_close"], exit=r["exit"], pnl_usd=r["pnl_usd"],
            fees_usd=r["fees_usd"], close_reason=r["close_reason"],
            pnl_provisional=r["pnl_provisional"] if "pnl_provisional"
            in r.keys() else 0,
        )
