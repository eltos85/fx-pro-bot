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
            # ── ctx_* — метрики контекста входа (observability, BUILDLOG
            # 2026-07-03). Nullable: старые строки и circuits без данных
            # остаются NULL. SQLite не умеет ADD COLUMN IF NOT EXISTS —
            # мигрируем через pragma table_info.
            existing = {
                row[1]
                for row in conn.execute("PRAGMA table_info(momentum_decisions)")
            }
            for col, ddl in (
                ("ctx_ema_dist_atr", "REAL"),
                ("ctx_adx", "REAL"),
                ("ctx_with_htf", "INTEGER"),
                ("ctx_spread_pips", "REAL"),
            ):
                if col not in existing:
                    conn.execute(
                        f"ALTER TABLE momentum_decisions ADD COLUMN {col} {ddl}"
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS momentum_position_state (
                    broker_position_id INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    initial_volume INTEGER NOT NULL,
                    risk_price REAL NOT NULL,
                    break_even_done INTEGER NOT NULL DEFAULT 0,
                    partial_done INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS momentum_closed_deals (
                    deal_id INTEGER PRIMARY KEY,
                    broker_position_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    closed_ts_ms INTEGER NOT NULL,
                    net_usd REAL NOT NULL,
                    gross_usd REAL NOT NULL,
                    swap_usd REAL NOT NULL,
                    commission_usd REAL NOT NULL,
                    volume INTEGER NOT NULL DEFAULT 0,
                    execution_price REAL,
                    entry_price REAL,
                    synced_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_closed_deals_closed_at
                ON momentum_closed_deals(closed_at)
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
        ctx_ema_dist_atr: float | None = None,
        ctx_adx: float | None = None,
        ctx_with_htf: bool | None = None,
        ctx_spread_pips: float | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO momentum_decisions(
                    symbol, direction, momentum_value, atr, close_price, executed, note,
                    ctx_ema_dist_atr, ctx_adx, ctx_with_htf, ctx_spread_pips
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    direction,
                    momentum_value,
                    atr,
                    close_price,
                    int(executed),
                    note,
                    ctx_ema_dist_atr,
                    ctx_adx,
                    None if ctx_with_htf is None else int(ctx_with_htf),
                    ctx_spread_pips,
                ),
            )
            conn.commit()

    def count_executed_today(self, symbol: str, direction: str) -> int:
        """Сколько РЕАЛЬНО открытых сделок по (symbol, direction) за сегодня (UTC).

        Используется VP-стратегией для лимита «макс N сделок в сторону в день»
        (Faiz SMC / Dalton: не перебивать одну зону многократно).
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM momentum_decisions
                WHERE symbol = ? AND direction = ? AND executed = 1
                  AND date(created_at) = date('now')
                """,
                (symbol, direction),
            ).fetchone()
        return int(row[0]) if row else 0

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

    def upsert_position_state(
        self,
        *,
        broker_position_id: int,
        symbol: str,
        entry_price: float,
        initial_volume: int,
        risk_price: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO momentum_position_state(
                    broker_position_id, symbol, entry_price, initial_volume, risk_price,
                    break_even_done, partial_done, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, datetime('now'))
                ON CONFLICT(broker_position_id) DO UPDATE SET
                    symbol = excluded.symbol,
                    entry_price = excluded.entry_price,
                    initial_volume = CASE
                        WHEN momentum_position_state.initial_volume > 0
                        THEN momentum_position_state.initial_volume
                        ELSE excluded.initial_volume
                    END,
                    risk_price = CASE
                        WHEN momentum_position_state.risk_price > 0
                        THEN momentum_position_state.risk_price
                        ELSE excluded.risk_price
                    END,
                    updated_at = datetime('now')
                """,
                (
                    broker_position_id,
                    symbol,
                    entry_price,
                    initial_volume,
                    risk_price,
                ),
            )
            conn.commit()

    def get_position_state(self, broker_position_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT broker_position_id, symbol, entry_price, initial_volume, risk_price,
                       break_even_done, partial_done, updated_at
                FROM momentum_position_state
                WHERE broker_position_id = ?
                """,
                (broker_position_id,),
            ).fetchone()
        if row is None:
            return None
        keys = [
            "broker_position_id",
            "symbol",
            "entry_price",
            "initial_volume",
            "risk_price",
            "break_even_done",
            "partial_done",
            "updated_at",
        ]
        return dict(zip(keys, row, strict=False))

    def set_break_even_done(self, broker_position_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE momentum_position_state
                SET break_even_done = 1, updated_at = datetime('now')
                WHERE broker_position_id = ?
                """,
                (broker_position_id,),
            )
            conn.commit()

    def set_partial_done(self, broker_position_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE momentum_position_state
                SET partial_done = 1, updated_at = datetime('now')
                WHERE broker_position_id = ?
                """,
                (broker_position_id,),
            )
            conn.commit()

    def cleanup_position_state(self, active_position_ids: set[int]) -> int:
        with self._connect() as conn:
            if active_position_ids:
                placeholders = ",".join("?" for _ in active_position_ids)
                cur = conn.execute(
                    f"""
                    DELETE FROM momentum_position_state
                    WHERE broker_position_id NOT IN ({placeholders})
                    """,
                    tuple(active_position_ids),
                )
            else:
                cur = conn.execute("DELETE FROM momentum_position_state")
            conn.commit()
            return int(cur.rowcount or 0)

    def last_closed_deal_ts_ms(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(closed_ts_ms) FROM momentum_closed_deals"
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def insert_closed_deal(
        self,
        *,
        deal_id: int,
        broker_position_id: int,
        symbol: str,
        side: str,
        closed_at: str,
        closed_ts_ms: int,
        net_usd: float,
        gross_usd: float,
        swap_usd: float,
        commission_usd: float,
        volume: int,
        execution_price: float | None,
        entry_price: float | None,
    ) -> bool:
        """True, если строка новая (deal_id ещё не было)."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO momentum_closed_deals(
                    deal_id, broker_position_id, symbol, side, closed_at,
                    closed_ts_ms, net_usd, gross_usd, swap_usd, commission_usd,
                    volume, execution_price, entry_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deal_id,
                    broker_position_id,
                    symbol,
                    side,
                    closed_at,
                    closed_ts_ms,
                    net_usd,
                    gross_usd,
                    swap_usd,
                    commission_usd,
                    volume,
                    execution_price,
                    entry_price,
                ),
            )
            conn.commit()
            return int(cur.rowcount or 0) > 0

    def pnl_snapshot(
        self,
        *,
        since: str,
        open_position_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        """Агрегат broker-net с ``since`` (inclusive, UTC ISO).

        WR считается по полностью закрытым позициям (position_id нет в
        ``open_position_ids``). Частичные закрытия открытых позиций входят
        в ``net_usd``, но не в WR.
        """
        open_ids = open_position_ids or set()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT broker_position_id, symbol, net_usd
                FROM momentum_closed_deals
                WHERE closed_at >= ?
                """,
                (since,),
            ).fetchall()
        net = 0.0
        by_pos: dict[int, list[tuple[str, float]]] = {}
        for pid, symbol, pnl in rows:
            net += float(pnl)
            by_pos.setdefault(int(pid), []).append((str(symbol), float(pnl)))
        closed = {
            pid: legs
            for pid, legs in by_pos.items()
            if pid not in open_ids
        }
        wins = sum(1 for legs in closed.values() if sum(p for _, p in legs) > 0)
        n_closed = len(closed)
        wr = (wins / n_closed) if n_closed else 0.0
        return {
            "n_deals": len(rows),
            "n_closed_positions": n_closed,
            "n_open_with_partials": len(by_pos) - n_closed,
            "net_usd": round(net, 2),
            "wins": wins,
            "wr": wr,
        }

