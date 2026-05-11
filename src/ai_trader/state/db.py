"""SQLite-хранилище AI-Trader.

Хранит:
- positions: открытые/закрытые позиции (только наши, по orderLinkId='ai_*')
- decisions: полный audit-trail каждого решения LLM
  (timestamp, prompt, response, parsed action, выполнено или нет, error)
- daily_pnl: дневная статистика для killswitch

Отдельная БД от bybit_bot и fx_pro_bot — никаких пересечений.
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
class AiPosition:
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
    llm_reason: str  # rationale из LLM


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
    llm_reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(closed_at) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    ts TEXT NOT NULL,
    prompt_system TEXT NOT NULL,
    prompt_user TEXT NOT NULL,
    response_raw TEXT,
    parsed_action TEXT,
    executed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    cost_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

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

-- v0.12 (2026-05-11): cooldown after stop-loss. После SL по паре (symbol, side)
-- хранится timestamp последнего SL и количество SL подряд (consecutive). Этот
-- счётчик используется как N в Fibonacci scheme (1,1,2,3,5,8 баров на TF=15м).
-- Сбрасывается на 0 если по этой паре была прибыльная сделка ИЛИ прошло >=24ч
-- без новых SL.
-- Research basis: TradingView 2026 "Mean-Reversion with Cooldown" (jannisMCMXCV).
CREATE TABLE IF NOT EXISTS sl_cooldown (
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    last_sl_at TEXT NOT NULL,
    consecutive_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (symbol, side)
);
"""


class AiTraderStore:
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
        prompt_system: str,
        prompt_user: str,
        response_raw: str | None,
        parsed_action: dict[str, Any] | None,
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
                (cycle, ts, prompt_system, prompt_user, response_raw,
                 parsed_action, executed, error, tokens_input, tokens_output, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle,
                    datetime.now(tz=UTC).isoformat(),
                    prompt_system,
                    prompt_user,
                    response_raw,
                    json.dumps(parsed_action) if parsed_action else None,
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
        llm_reason: str,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO positions
                (symbol, side, qty, entry_price, sl_price, tp_price, leverage,
                 order_link_id, opened_at, llm_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    llm_reason,
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

    def get_open_positions(self) -> list[AiPosition]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM positions WHERE closed_at IS NULL ORDER BY opened_at"
            ).fetchall()
        return [AiPosition(**dict(r)) for r in rows]

    def get_position_by_link_id(self, order_link_id: str) -> AiPosition | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM positions WHERE order_link_id = ?",
                (order_link_id,),
            ).fetchone()
        return AiPosition(**dict(row)) if row else None

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

    # ─── KV state (chat_id, paused, etc) ──────────────────────────────────

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

    # ─── SL Cooldown (v0.12) ────────────────────────────────────────────────
    # После SL по паре (symbol, side) запрещаем повторный вход на N минут.
    # Длительность растёт по Fibonacci при повторяющихся SL подряд:
    # 1-й SL → 15 мин, 2-й → 15 мин, 3-й → 30 мин, 4-й → 45 мин,
    # 5-й → 75 мин, 6+ → 120 мин (cap).
    # Сброс счётчика: 24ч без новых SL ИЛИ закрытие в плюс по этой паре.
    # Research basis: TradingView jannisMCMXCV 2026, AOTrading 2026 "3-5-7
    # Rule for mean-reversion cooldown".

    @staticmethod
    def _fib_cooldown_minutes(consecutive: int) -> int:
        # consecutive_count это сколько SL было ПОДРЯД (включая текущий).
        # Fibonacci-баров для TF=15м, умноженные на 15:
        # n=1 → 1 bar, n=2 → 1 bar, n=3 → 2 bar, n=4 → 3, n=5 → 5, n>=6 → 8.
        schedule = {1: 1, 2: 1, 3: 2, 4: 3, 5: 5}
        bars = schedule.get(max(1, consecutive), 8)
        return bars * 15

    def record_sl(self, symbol: str, side: str) -> int:
        """Записывает SL по (symbol, side); инкрементирует consecutive_count.
        Возвращает новый consecutive_count."""
        side_n = side.lower()
        now_iso = datetime.now(tz=UTC).isoformat()
        with self._conn() as c:
            row = c.execute(
                """
                SELECT consecutive_count, last_sl_at FROM sl_cooldown
                WHERE symbol = ? AND side = ?
                """,
                (symbol, side_n),
            ).fetchone()
            if row is None:
                c.execute(
                    """
                    INSERT INTO sl_cooldown (symbol, side, last_sl_at, consecutive_count)
                    VALUES (?, ?, ?, 1)
                    """,
                    (symbol, side_n, now_iso),
                )
                return 1
            prev_count = int(row["consecutive_count"])
            prev_at = datetime.fromisoformat(row["last_sl_at"])
            age_hours = (datetime.now(tz=UTC) - prev_at).total_seconds() / 3600
            new_count = 1 if age_hours >= 24 else prev_count + 1
            c.execute(
                """
                UPDATE sl_cooldown SET last_sl_at = ?, consecutive_count = ?
                WHERE symbol = ? AND side = ?
                """,
                (now_iso, new_count, symbol, side_n),
            )
            return new_count

    def reset_cooldown(self, symbol: str, side: str) -> None:
        """Сбросить счётчик SL по паре (например, после прибыльной сделки)."""
        side_n = side.lower()
        with self._conn() as c:
            c.execute(
                "DELETE FROM sl_cooldown WHERE symbol = ? AND side = ?",
                (symbol, side_n),
            )

    def get_cooldown_remaining_minutes(self, symbol: str, side: str) -> int:
        """Сколько минут осталось до конца cooldown по (symbol, side).
        0 если cooldown не активен."""
        side_n = side.lower()
        with self._conn() as c:
            row = c.execute(
                """
                SELECT consecutive_count, last_sl_at FROM sl_cooldown
                WHERE symbol = ? AND side = ?
                """,
                (symbol, side_n),
            ).fetchone()
        if row is None:
            return 0
        last_sl_at = datetime.fromisoformat(row["last_sl_at"])
        cooldown_min = self._fib_cooldown_minutes(int(row["consecutive_count"]))
        elapsed_min = (datetime.now(tz=UTC) - last_sl_at).total_seconds() / 60
        remaining = cooldown_min - elapsed_min
        return int(max(0, remaining))

    def get_telegram_chat_id(self) -> int | None:
        v = self.kv_get("telegram_chat_id")
        try:
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    def set_telegram_chat_id(self, chat_id: int) -> None:
        self.kv_set("telegram_chat_id", str(chat_id))

    def get_recent_decisions(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT cycle, ts, parsed_action, executed, error
                FROM decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_positions_count(self) -> tuple[int, int]:
        """Возвращает (всего закрытых, прибыльных)."""
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins
                FROM positions WHERE closed_at IS NOT NULL
                """
            ).fetchone()
        return (int(row[0]) if row else 0, int(row[1]) if row else 0)
