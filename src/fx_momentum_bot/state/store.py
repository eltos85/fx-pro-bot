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

