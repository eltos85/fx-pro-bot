"""SQLite-состояние flowzone_bot: сделки + агрегаты для killswitch.

Хранится в ``{data_dir}/flowzone_bot.sqlite`` (volume flowzone_data).
``pnl_usd`` — net closedPnl с биржи (через WS execution / REST reconcile); для
аудита ground truth = биржевая выписка (stats-collection.mdc), БД —
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
    strategy TEXT NOT NULL DEFAULT 'flowzone',
    status TEXT NOT NULL DEFAULT 'open',
    entry_order_id TEXT,
    ts_close REAL,
    exit REAL,
    pnl_usd REAL,
    fees_usd REAL,
    close_reason TEXT,
    pnl_provisional INTEGER NOT NULL DEFAULT 0,
    pnl_verified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ts_close ON trades(ts_close);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE TABLE IF NOT EXISTS prints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    side TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prints_symbol_ts ON prints(symbol, ts);
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
    pnl_verified: int = 0


@dataclass
class StrategyStat:
    strategy: str
    trades: int
    wins: int
    losses: int
    pnl_usd: float

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return (self.wins / decided) if decided else 0.0


class FlowzoneDB:
    def __init__(self, data_dir: str) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "flowzone_bot.sqlite")
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Аддитивные миграции существующих БД (новые колонки)."""
        cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(trades)").fetchall()}
        if "pnl_verified" not in cols:
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN pnl_verified "
                "INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self._conn.close()

    # ─── writes ──────────────────────────────────────────────────────────

    def insert_open(
        self, *, symbol: str, side: str, qty: float, entry: float, sl: float,
        tp: float, score: int, reasons: str, mode: str,
        strategy: str = "flowzone",
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

    def update_entry(self, trade_id: int, entry: float) -> None:
        """Обновить entry реальной ценой исполнения (VWAP входных филлов)."""
        if entry <= 0:
            return
        self._conn.execute(
            "UPDATE trades SET entry=? WHERE id=?", (entry, trade_id))
        self._conn.commit()

    def update_levels(self, trade_id: int, *, sl: float, tp: float) -> None:
        if sl <= 0 or tp <= 0:
            return
        self._conn.execute(
            "UPDATE trades SET sl=?, tp=? WHERE id=?", (sl, tp, trade_id))
        self._conn.commit()

    def finalize_pnl(self, trade_id: int, *, pnl_usd: float,
                     exit_price: float | None = None,
                     close_reason: str | None = None) -> None:
        """Заменить предварительный PnL реальным closedPnl и снять provisional."""
        sets = ["pnl_usd=?", "pnl_provisional=0"]
        args: list = [pnl_usd]
        if exit_price is not None:
            sets.insert(1, "exit=?")
            args.append(exit_price)
        if close_reason is not None:
            sets.append("close_reason=?")
            args.append(close_reason)
        args.append(trade_id)
        self._conn.execute(
            f"UPDATE trades SET {', '.join(sets)} WHERE id=?", tuple(args))
        self._conn.commit()

    def verify_pnl(self, trade_id: int, *, pnl_usd: float,
                   exit_price: float | None = None,
                   close_reason: str | None = None) -> None:
        """Авторитетный true-up против REST closedPnl: ставит net, снимает
        provisional И помечает verified (больше не пересверяем — rate-limit).
        closedPnl уже net (офдок Bybit close-pnl: gross − openFee − closeFee)."""
        sets = ["pnl_usd=?", "pnl_provisional=0", "pnl_verified=1"]
        args: list = [pnl_usd]
        if exit_price is not None:
            sets.insert(1, "exit=?")
            args.append(exit_price)
        if close_reason is not None:
            sets.append("close_reason=?")
            args.append(close_reason)
        args.append(trade_id)
        self._conn.execute(
            f"UPDATE trades SET {', '.join(sets)} WHERE id=?", tuple(args))
        self._conn.commit()

    # ─── prints (footprint-тики для per-swing/per-session профилей, A2) ───
    # Канон STRATEGY §3: зона = профиль ПРЕДЫДУЩЕЙ swing-точки; §2: контекст =
    # форма СЕССИОННОГО профиля. Профиль строится из исполненного потока (footprint,
    # не kline-volume). Принты persist-ятся сюда, чтобы per-swing окно (переменная
    # длина — от ts предыдущего swing до now) можно было собрать в любой момент.

    def insert_prints(self, rows: list[tuple]) -> int:
        """Batch-insert принтов [(ts, symbol, price, size, side), ...].
        Возвращает число вставленных строк. Пустой список — no-op (без commit)."""
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT INTO prints (ts,symbol,price,size,side) "
            "VALUES (?,?,?,?,?)", rows)
        self._conn.commit()
        return len(rows)

    def prints_since(self, symbol: str, since_ts: float,
                     until_ts: float | None = None) -> list[tuple]:
        """Принты символа с ts>=since_ts (и опц. <until_ts) в порядке ts.
        Возвращает [(ts, price, size, side), ...] — для построения VP окна."""
        if until_ts is None:
            rows = self._conn.execute(
                "SELECT ts,price,size,side FROM prints "
                "WHERE symbol=? AND ts>=? ORDER BY ts", (symbol, since_ts))
        else:
            rows = self._conn.execute(
                "SELECT ts,price,size,side FROM prints "
                "WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
                (symbol, since_ts, until_ts))
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def prune_prints_before(self, before_ts: float) -> int:
        """Retention: удалить принты старше before_ts. Возвращает число удалённых.
        Вызывается периодически (старые per-swing окна уже не нужны — swing-точка
        подтверждена, её профиль собран)."""
        cur = self._conn.execute(
            "DELETE FROM prints WHERE ts<?", (before_ts,))
        self._conn.commit()
        return int(cur.rowcount or 0)

    def prints_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM prints").fetchone()
        return int(row["c"] or 0)

    # закрытия, не имеющие биржевого closedPnl (нечего сверять) — сразу verified
    _NON_TRADE_REASONS = ("restart_flat", "entry_Cancelled", "entry_Rejected",
                          "entry_Deactivated", "entry_timeout")

    def provisional_closed_since(self, ts: float) -> list[TradeRow]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND pnl_provisional=1 "
            "AND ts_close>=? ORDER BY id", (ts,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def unverified_closed_live_since(self, ts: float) -> list[TradeRow]:
        """Закрытые LIVE-сделки, ещё не сверённые с биржевым closedPnl
        (pnl_verified=0). Канон: REST closedPnl — источник правды для ВСЕХ
        закрытий, не только provisional (иначе WS-дрейф комиссий не чинится).
        Технические закрытия (entry_* / restart_flat) исключаем — у них нет
        closedPnl для сверки."""
        placeholders = ",".join("?" for _ in self._NON_TRADE_REASONS)
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='closed' AND mode='live' "
            "AND pnl_verified=0 AND ts_close>=? "
            f"AND (close_reason IS NULL OR close_reason NOT IN ({placeholders})) "
            "ORDER BY id", (ts, *self._NON_TRADE_REASONS)
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
        Реконсил-закрытия (restart_flat / entry_*) исключаем — не торговые исходы."""
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
            pnl_verified=r["pnl_verified"] if "pnl_verified" in r.keys() else 0,
        )
