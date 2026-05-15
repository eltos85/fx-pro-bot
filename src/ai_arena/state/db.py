"""SQLite-хранилище AI Arena.

Хранит:
- ``positions``      — открытые/закрытые позиции (orderLinkId='arena_*'),
                       с Nof1-полями (confidence, invalidation_condition,
                       risk_usd, profit_target).
- ``decisions``      — полный audit-trail каждого решения LLM, включая
                       confidence / invalidation / risk_usd / sharpe_at_decision /
                       minutes_elapsed.
- ``equity_snapshots`` — equity на каждый цикл, для расчёта cumulative
                       Sharpe (с момента старта эксперимента — 1-в-1
                       с Nof1, см. правило ai-arena-sources.mdc).
- ``daily_pnl``      — дневная агрегация **net** realized_pnl (после
                       fees + funding, как у Bybit `closedPnl`). Для
                       аналитики и Telegram /pnl команды; не
                       используется как capital-safety blocker —
                       Nof1 source их не имеет.
- ``kv_state``       — telegram_chat_id, started_at и пр.

Полностью изолирован от ai_trader (та БД — ai_trader.sqlite, эта —
ai_arena.sqlite). См. правило ``strategy-guard.mdc`` (изоляция кодовых баз).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterator


@dataclass
class ArenaPosition:
    id: int
    symbol: str
    side: str  # "Buy" / "Sell"
    qty: float
    entry_price: float
    sl_price: float | None
    tp_price: float | None
    leverage: int
    order_link_id: str
    opened_at: str
    closed_at: str | None
    exit_price: float | None
    realized_pnl_usd: float | None
    close_reason: str | None
    llm_justification: str
    confidence: float | None
    invalidation_condition: str | None
    risk_usd: float | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    sl_price REAL,
    tp_price REAL,
    leverage INTEGER NOT NULL,
    order_link_id TEXT NOT NULL UNIQUE,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_price REAL,
    realized_pnl_usd REAL,
    close_reason TEXT,
    llm_justification TEXT NOT NULL,
    confidence REAL,
    invalidation_condition TEXT,
    risk_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(closed_at) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    ts TEXT NOT NULL,
    minutes_elapsed INTEGER,
    sharpe_at_decision REAL,
    prompt_system TEXT NOT NULL,
    prompt_user TEXT NOT NULL,
    response_raw TEXT,
    parsed_action TEXT,
    signal TEXT,
    confidence REAL,
    invalidation_condition TEXT,
    risk_usd REAL,
    executed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_decisions_signal ON decisions(signal);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    total_equity_usd REAL NOT NULL,
    available_cash_usd REAL NOT NULL,
    total_return_pct REAL NOT NULL,
    sharpe_rolling_14d REAL,
    cycle_no INTEGER
);

CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts);

CREATE TABLE IF NOT EXISTS daily_pnl (
    day TEXT PRIMARY KEY,
    realized_pnl_usd REAL NOT NULL DEFAULT 0,
    n_trades INTEGER NOT NULL DEFAULT 0,
    n_wins INTEGER NOT NULL DEFAULT 0,
    api_cost_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class AiArenaStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ─── Decisions audit-trail ───────────────────────────────────────────

    def log_decision(
        self,
        *,
        cycle: int,
        minutes_elapsed: int | None,
        sharpe_at_decision: float | None,
        prompt_system: str,
        prompt_user: str,
        response_raw: str | None,
        parsed_action: dict[str, Any] | None,
        signal: str | None,
        confidence: float | None,
        invalidation_condition: str | None,
        risk_usd: float | None,
        executed: bool,
        error: str | None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        cost_usd: float | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO decisions (
                    cycle, ts, minutes_elapsed, sharpe_at_decision,
                    prompt_system, prompt_user, response_raw, parsed_action,
                    signal, confidence, invalidation_condition, risk_usd,
                    executed, error, tokens_input, tokens_output, cost_usd
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle,
                    datetime.now(tz=UTC).isoformat(),
                    minutes_elapsed,
                    sharpe_at_decision,
                    prompt_system,
                    prompt_user,
                    response_raw,
                    json.dumps(parsed_action) if parsed_action else None,
                    signal,
                    confidence,
                    invalidation_condition,
                    risk_usd,
                    1 if executed else 0,
                    error,
                    tokens_input,
                    tokens_output,
                    cost_usd,
                ),
            )
            return int(cur.lastrowid or 0)

    # ─── Positions ───────────────────────────────────────────────────────

    def open_position(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        sl_price: float | None,
        tp_price: float | None,
        leverage: int,
        order_link_id: str,
        llm_justification: str,
        confidence: float | None,
        invalidation_condition: str | None,
        risk_usd: float | None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO positions (
                    symbol, side, qty, entry_price, sl_price, tp_price, leverage,
                    order_link_id, opened_at, llm_justification,
                    confidence, invalidation_condition, risk_usd
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    side,
                    qty,
                    entry_price,
                    sl_price,
                    tp_price,
                    leverage,
                    order_link_id,
                    datetime.now(tz=UTC).isoformat(),
                    llm_justification,
                    confidence,
                    invalidation_condition,
                    risk_usd,
                ),
            )
            return int(cur.lastrowid or 0)

    def close_position(
        self,
        position_id: int,
        *,
        exit_price: float,
        realized_pnl_usd: float,
        close_reason: str,
    ) -> None:
        """Закрывает позицию + обновляет daily_pnl агрегат.

        ВАЖНО: ``realized_pnl_usd`` должен быть **net** (после fees +
        funding), идентичный Bybit `closedPnl`. Локальный gross-расчёт
        `(exit-entry)*qty` запрещён — расходится с биржей и ломает
        Sharpe + /status (см. BUILDLOG 2026-05-15 «net PnL alignment»).
        """
        with self._conn() as c:
            c.execute(
                """
                UPDATE positions
                SET closed_at = ?, exit_price = ?, realized_pnl_usd = ?,
                    close_reason = ?
                WHERE id = ?
                """,
                (
                    datetime.now(tz=UTC).isoformat(),
                    exit_price,
                    realized_pnl_usd,
                    close_reason,
                    position_id,
                ),
            )
            today = date.today().isoformat()
            won = 1 if realized_pnl_usd > 0 else 0
            c.execute(
                """
                INSERT INTO daily_pnl (day, realized_pnl_usd, n_trades, n_wins)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(day) DO UPDATE SET
                    realized_pnl_usd = realized_pnl_usd + excluded.realized_pnl_usd,
                    n_trades = n_trades + 1,
                    n_wins = n_wins + excluded.n_wins
                """,
                (today, realized_pnl_usd, won),
            )

    def update_position_realized(
        self,
        position_id: int,
        *,
        exit_price: float,
        realized_pnl_usd: float,
    ) -> float:
        """Перезаписывает ``exit_price`` и ``realized_pnl_usd`` уже закрытой
        позиции и пересчитывает daily_pnl агрегат на разницу.

        Используется в backfill-скрипте (`scripts/ai_arena_backfill_pnl.py`)
        для замены ранее сохранённого gross PnL на net PnL из
        `get_closed_pnl`. Возвращает ``delta`` = новый PnL − старый PnL
        (для логирования).

        Если позиция ещё открыта (closed_at IS NULL) — выбрасывает
        ``ValueError``: backfill применим только к закрытым.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT closed_at, realized_pnl_usd FROM positions WHERE id = ?",
                (position_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"position id={position_id} not found")
            if row["closed_at"] is None:
                raise ValueError(f"position id={position_id} is still open")
            old_pnl = float(row["realized_pnl_usd"] or 0.0)
            delta = realized_pnl_usd - old_pnl
            old_won = 1 if old_pnl > 0 else 0
            new_won = 1 if realized_pnl_usd > 0 else 0
            won_delta = new_won - old_won
            c.execute(
                """
                UPDATE positions
                SET exit_price = ?, realized_pnl_usd = ?
                WHERE id = ?
                """,
                (exit_price, realized_pnl_usd, position_id),
            )
            day = (row["closed_at"] or "")[:10]
            if day:
                c.execute(
                    """
                    UPDATE daily_pnl
                    SET realized_pnl_usd = realized_pnl_usd + ?,
                        n_wins = MAX(0, n_wins + ?)
                    WHERE day = ?
                    """,
                    (delta, won_delta, day),
                )
            return delta

    def get_open_positions(self) -> list[ArenaPosition]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at"
            ).fetchall()
        return [ArenaPosition(**dict(r)) for r in rows]

    def get_position_by_link_id(self, order_link_id: str) -> ArenaPosition | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM positions WHERE order_link_id = ?",
                (order_link_id,),
            ).fetchone()
        return ArenaPosition(**dict(row)) if row else None

    # ─── Equity snapshots (для rolling Sharpe) ───────────────────────────

    def add_equity_snapshot(
        self,
        *,
        total_equity_usd: float,
        available_cash_usd: float,
        total_return_pct: float,
        sharpe_rolling_14d: float | None,
        cycle_no: int,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO equity_snapshots
                (ts, total_equity_usd, available_cash_usd, total_return_pct,
                 sharpe_rolling_14d, cycle_no)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(datetime.now(tz=UTC).timestamp()),
                    total_equity_usd,
                    available_cash_usd,
                    total_return_pct,
                    sharpe_rolling_14d,
                    cycle_no,
                ),
            )

    def get_equity_snapshots_since(self, since_ts: int) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM equity_snapshots WHERE ts >= ? ORDER BY ts",
                (since_ts,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_equity_snapshots(self) -> list[dict[str, Any]]:
        """Все snapshot'ы с момента старта эксперимента (для cumulative
        Sharpe и `total_return_pct` — 1-в-1 с Nof1 Season 1, который
        идёт cumulative с 17 окт 2025).
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM equity_snapshots ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_first_equity_snapshot(self) -> dict[str, Any] | None:
        """Самый ранний snapshot — baseline для `Current Total Return`.

        Возвращает None если snapshot'ов ещё нет (бот только запущен и
        ещё не сохранил первый snapshot после первого цикла).
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM equity_snapshots ORDER BY ts ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # ─── PnL аналитика (для Telegram /pnl, /status — не для blocker'ов) ─

    def get_today_pnl(self) -> float:
        today = date.today().isoformat()
        with self._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM daily_pnl WHERE day = ?", (today,)
            ).fetchone()
        return float(row[0]) if row else 0.0

    def get_total_pnl(self) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd), 0) FROM daily_pnl"
            ).fetchone()
        return float(row[0]) if row else 0.0

    def add_api_cost(self, cost_usd: float) -> None:
        today = date.today().isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO daily_pnl (day, api_cost_usd)
                VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    api_cost_usd = api_cost_usd + excluded.api_cost_usd
                """,
                (today, cost_usd),
            )

    # ─── KV state ────────────────────────────────────────────────────────

    def kv_get(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM kv_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO kv_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, datetime.now(tz=UTC).isoformat()),
            )

    def is_paused(self) -> bool:
        return self.kv_get("paused") == "1"

    def set_paused(self, value: bool) -> None:
        self.kv_set("paused", "1" if value else "0")

    def get_telegram_chat_id(self) -> int | None:
        v = self.kv_get("telegram_chat_id")
        try:
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    def set_telegram_chat_id(self, chat_id: int) -> None:
        self.kv_set("telegram_chat_id", str(chat_id))

    def get_started_at_ts(self) -> int:
        """Unix-секунды первого старта бота (для minutes_elapsed в prompt'е).

        При первом вызове кладёт текущее время, дальше возвращает stored.
        Не сбрасывается рестартом контейнера (сохраняется в SQLite).
        """
        v = self.kv_get("started_at_ts")
        if v:
            try:
                return int(v)
            except ValueError:
                pass
        now_ts = int(datetime.now(tz=UTC).timestamp())
        self.kv_set("started_at_ts", str(now_ts))
        return now_ts

    # ─── Recent decisions / stats ────────────────────────────────────────

    def get_recent_decisions(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT cycle, ts, signal, confidence, parsed_action, executed, error
                FROM decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_positions_count(self) -> tuple[int, int]:
        """(всего закрытых, прибыльных)."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins
                FROM positions WHERE closed_at IS NOT NULL
                """
            ).fetchone()
        return (int(row[0]) if row else 0, int(row[1]) if row else 0)
