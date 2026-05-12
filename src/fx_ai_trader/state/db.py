"""SQLite-хранилище FX AI Trader.

Хранит:
- positions: открытые/закрытые позиции (только наши, по `label=ai-fx-trader`)
- decisions: полный audit-trail каждого решения LLM
  (timestamp, prompt, response, parsed action + sentiment, выполнено или нет, error)
- daily_pnl: дневная статистика для killswitch
- kv_state: paused-флаг и прочие settings runtime

Отдельная БД от bybit_bot, ai_trader и fx_pro_bot — никаких пересечений
(правило ``buildlog.mdc``: отдельный бот = отдельный datastore).

В отличие от Bybit-агента, у нас:
- side: ``"BUY"`` / ``"SELL"`` (cTrader uppercase нотация)
- entry_volume_lots: лоты (float), а не Bybit-qty (count of contracts)
- broker_position_id: cTrader positionId (int) — для reconcile
- broker_order_label: для broker-side изоляции от Advisor
- sentiment_json: multi-dim sentiment результат (relevance/polarity/
  intensity/uncertainty/forwardness per news-item) — research: arxiv 2603.11408
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
class AiFxPosition:
    id: int
    symbol: str  # внутренний (yfinance) символ: "XAUUSD" / "BZ=F"
    side: str  # "BUY" / "SELL"
    volume_lots: float
    entry_price: float
    sl_price: float | None
    tp_price: float | None
    broker_position_id: int | None  # cTrader positionId (None в paper-mode)
    broker_order_label: str
    opened_at: str
    closed_at: str | None
    exit_price: float | None
    realized_pnl_usd: float | None
    close_reason: str | None
    llm_reason: str
    is_paper: int  # 1 = paper-mode (нет реального ордера), 0 = live


_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume_lots REAL NOT NULL,
    entry_price REAL NOT NULL,
    sl_price REAL,
    tp_price REAL,
    broker_position_id INTEGER,
    broker_order_label TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_price REAL,
    realized_pnl_usd REAL,
    close_reason TEXT,
    llm_reason TEXT NOT NULL,
    is_paper INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(closed_at) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_positions_broker ON positions(broker_position_id);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    cycle_type TEXT NOT NULL,            -- 'full' / 'review'
    ts TEXT NOT NULL,
    prompt_system TEXT NOT NULL,
    prompt_user TEXT NOT NULL,
    response_raw TEXT,
    parsed_action TEXT,
    sentiment_json TEXT,                  -- multi-dim sentiment audit
    executed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_decisions_cycle_type ON decisions(cycle_type);

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


class AiFxTraderStore:
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
        cycle_type: str,
        prompt_system: str,
        prompt_user: str,
        response_raw: str | None,
        parsed_action: dict[str, Any] | None,
        sentiment: dict[str, Any] | None,
        executed: bool,
        error: str | None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        cost_usd: float | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO decisions
                (cycle, cycle_type, ts, prompt_system, prompt_user, response_raw,
                 parsed_action, sentiment_json, executed, error,
                 tokens_input, tokens_output, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle,
                    cycle_type,
                    datetime.now(tz=UTC).isoformat(),
                    prompt_system,
                    prompt_user,
                    response_raw,
                    json.dumps(parsed_action) if parsed_action else None,
                    json.dumps(sentiment) if sentiment else None,
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
        volume_lots: float,
        entry_price: float,
        sl_price: float | None,
        tp_price: float | None,
        broker_position_id: int | None,
        broker_order_label: str,
        llm_reason: str,
        is_paper: bool,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO positions
                (symbol, side, volume_lots, entry_price, sl_price, tp_price,
                 broker_position_id, broker_order_label, opened_at, llm_reason, is_paper)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    side,
                    volume_lots,
                    entry_price,
                    sl_price,
                    tp_price,
                    broker_position_id,
                    broker_order_label,
                    datetime.now(tz=UTC).isoformat(),
                    llm_reason,
                    1 if is_paper else 0,
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

    def get_open_positions(self) -> list[AiFxPosition]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at"
            ).fetchall()
        return [AiFxPosition(**dict(r)) for r in rows]

    def get_position_by_broker_id(self, broker_position_id: int) -> AiFxPosition | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM positions WHERE broker_position_id = ?",
                (broker_position_id,),
            ).fetchone()
        return AiFxPosition(**dict(row)) if row else None

    def count_positions_by_symbol(self, symbol: str) -> int:
        """Сколько открытых позиций по конкретному символу."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM positions WHERE symbol = ? AND closed_at IS NULL",
                (symbol,),
            ).fetchone()
        return int(row[0]) if row else 0

    # ─── PnL для killswitch ──────────────────────────────────────────────

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

    def get_closed_positions_count(self) -> tuple[int, int]:
        """Возвращает (всего закрытых, прибыльных) — для exit-критериев Phase 1."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins
                FROM positions WHERE closed_at IS NOT NULL
                """
            ).fetchone()
        return (int(row[0]) if row else 0, int(row[1]) if row else 0)
