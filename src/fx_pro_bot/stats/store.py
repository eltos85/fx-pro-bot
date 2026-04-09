"""SQLite: учёт советов, позиций, paper-стратегий и статистика точности."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Verdict = Literal["pending", "right", "wrong"]
PositionStatus = Literal["open", "closed"]
ExitStrategy = Literal["progressive", "grid", "hold90", "scalp"]


@dataclass(frozen=True, slots=True)
class SuggestionRow:
    id: str
    created_at: datetime
    instrument: str
    direction: str
    advice_text: str
    reasons: tuple[str, ...]
    price_at_signal: float | None
    events_context: str | None
    verdict: Verdict
    verdict_at: datetime | None
    notes: str | None
    source: str = "ensemble"


@dataclass(frozen=True, slots=True)
class VerificationRow:
    id: str
    suggestion_id: str
    horizon_minutes: int
    price_at_check: float
    profit_pips: float
    verdict: str
    checked_at: datetime


@dataclass(slots=True)
class PositionRow:
    id: str
    created_at: str
    strategy: str
    source: str
    instrument: str
    direction: str
    entry_price: float
    current_price: float
    peak_price: float
    trough_price: float
    stop_loss_price: float
    trail_price: float
    trail_activated: bool
    profit_pips: float
    profit_pct: float
    status: str
    exit_reason: str
    closed_at: str | None
    broker_position_id: int = 0
    broker_volume: int = 0
    estimated_cost_pips: float = 0.0


@dataclass(slots=True)
class PaperPositionRow:
    id: str
    position_id: str
    exit_strategy: str
    status: str
    entry_price: float
    current_price: float
    peak_price: float
    profit_pips: float
    profit_pct: float
    exit_reason: str
    levels_hit: list[str] = field(default_factory=list)
    created_at: str = ""
    closed_at: str | None = None


class StatsStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS suggestions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    advice_text TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    price_at_signal REAL,
                    events_context TEXT,
                    verdict TEXT NOT NULL DEFAULT 'pending'
                        CHECK (verdict IN ('pending','right','wrong')),
                    verdict_at TEXT,
                    notes TEXT,
                    source TEXT NOT NULL DEFAULT 'ensemble'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS verifications (
                    id TEXT PRIMARY KEY,
                    suggestion_id TEXT NOT NULL REFERENCES suggestions(id),
                    horizon_minutes INTEGER NOT NULL,
                    price_at_check REAL NOT NULL,
                    profit_pips REAL NOT NULL,
                    verdict TEXT NOT NULL CHECK (verdict IN ('right','wrong')),
                    checked_at TEXT NOT NULL,
                    UNIQUE(suggestion_id, horizon_minutes)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    source TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    peak_price REAL NOT NULL,
                    trough_price REAL NOT NULL,
                    stop_loss_price REAL NOT NULL DEFAULT 0,
                    trail_price REAL NOT NULL DEFAULT 0,
                    trail_activated INTEGER NOT NULL DEFAULT 0,
                    profit_pips REAL NOT NULL DEFAULT 0,
                    profit_pct REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open',
                    exit_reason TEXT NOT NULL DEFAULT '',
                    closed_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_positions (
                    id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL REFERENCES positions(id),
                    exit_strategy TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    entry_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    peak_price REAL NOT NULL,
                    profit_pips REAL NOT NULL DEFAULT 0,
                    profit_pct REAL NOT NULL DEFAULT 0,
                    exit_reason TEXT NOT NULL DEFAULT '',
                    levels_hit TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    price REAL NOT NULL,
                    profit_pips REAL NOT NULL,
                    profit_pct REAL NOT NULL,
                    peak_profit_pips REAL NOT NULL,
                    peak_profit_pct REAL NOT NULL,
                    max_drawdown_pips REAL NOT NULL
                )
                """
            )
            self._migrate_add_source(conn)
            self._migrate_add_broker_position_id(conn)
            self._migrate_add_estimated_cost_pips(conn)
            self._migrate_add_broker_volume(conn)
            conn.commit()

    def _migrate_add_source(self, conn: sqlite3.Connection) -> None:
        """Добавить колонку source если её нет (миграция старых БД)."""
        cur = conn.execute("PRAGMA table_info(suggestions)")
        columns = {row[1] for row in cur.fetchall()}
        if "source" not in columns:
            conn.execute(
                "ALTER TABLE suggestions ADD COLUMN source TEXT NOT NULL DEFAULT 'ensemble'"
            )

    def _migrate_add_broker_position_id(self, conn: sqlite3.Connection) -> None:
        """Добавить broker_position_id в positions (миграция для cTrader)."""
        cur = conn.execute("PRAGMA table_info(positions)")
        columns = {row[1] for row in cur.fetchall()}
        if "broker_position_id" not in columns:
            conn.execute(
                "ALTER TABLE positions ADD COLUMN broker_position_id INTEGER NOT NULL DEFAULT 0"
            )

    def _migrate_add_estimated_cost_pips(self, conn: sqlite3.Connection) -> None:
        """Добавить estimated_cost_pips в positions (модель реалистичных издержек)."""
        cur = conn.execute("PRAGMA table_info(positions)")
        columns = {row[1] for row in cur.fetchall()}
        if "estimated_cost_pips" not in columns:
            conn.execute(
                "ALTER TABLE positions ADD COLUMN estimated_cost_pips REAL NOT NULL DEFAULT 0"
            )

    def _migrate_add_broker_volume(self, conn: sqlite3.Connection) -> None:
        """Добавить broker_volume — фактический объём позиции на cTrader."""
        cur = conn.execute("PRAGMA table_info(positions)")
        columns = {row[1] for row in cur.fetchall()}
        if "broker_volume" not in columns:
            conn.execute(
                "ALTER TABLE positions ADD COLUMN broker_volume INTEGER NOT NULL DEFAULT 0"
            )

    # ── Suggestions ──────────────────────────────────────────────

    def record_suggestion(
        self,
        *,
        instrument: str,
        direction: str,
        advice_text: str,
        reasons: tuple[str, ...],
        price_at_signal: float | None,
        events_context: str | None,
        source: str = "ensemble",
    ) -> str:
        sid = str(uuid.uuid4())
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO suggestions (
                    id, created_at, instrument, direction, advice_text,
                    reasons_json, price_at_signal, events_context, verdict, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    sid,
                    now,
                    instrument,
                    direction,
                    advice_text,
                    json.dumps(list(reasons), ensure_ascii=False),
                    price_at_signal,
                    events_context,
                    source,
                ),
            )
            conn.commit()
        return sid

    def set_verdict(self, suggestion_id: str, verdict: Verdict, notes: str | None = None) -> bool:
        if verdict == "pending":
            return False
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE suggestions SET verdict = ?, verdict_at = ?, notes = ? WHERE id = ?",
                (verdict, now, notes, suggestion_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_recent(self, limit: int = 20) -> list[SuggestionRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM suggestions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_suggestion(r) for r in rows]

    def pending_for_verification(self, horizon_minutes: int, now: datetime) -> list[SuggestionRow]:
        """Сигналы со статусом pending, которым уже >= horizon минут и для этого горизонта нет записи."""
        cutoff = now.timestamp() - horizon_minutes * 60
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.* FROM suggestions s
                WHERE s.direction IN ('long', 'short')
                  AND NOT EXISTS (
                      SELECT 1 FROM verifications v
                      WHERE v.suggestion_id = s.id AND v.horizon_minutes = ?
                  )
                  AND strftime('%s', s.created_at) <= ?
                ORDER BY s.created_at ASC
                """,
                (horizon_minutes, str(int(cutoff))),
            ).fetchall()
        return [_row_to_suggestion(r) for r in rows]

    # ── Verifications ────────────────────────────────────────────

    def record_verification(
        self,
        *,
        suggestion_id: str,
        horizon_minutes: int,
        price_at_check: float,
        profit_pips: float,
        verdict: str,
    ) -> str:
        vid = str(uuid.uuid4())
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO verifications
                    (id, suggestion_id, horizon_minutes, price_at_check, profit_pips, verdict, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (vid, suggestion_id, horizon_minutes, price_at_check, profit_pips, verdict, now),
            )
            conn.commit()
        return vid

    def verifications_for(self, suggestion_id: str) -> list[VerificationRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM verifications WHERE suggestion_id = ? ORDER BY horizon_minutes",
                (suggestion_id,),
            ).fetchall()
        return [_row_to_verification(r) for r in rows]

    # ── Statistics ───────────────────────────────────────────────

    def summary(self) -> dict[str, int | float]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT verdict, COUNT(*) AS c FROM suggestions GROUP BY verdict"
            ).fetchall()
        counts = {str(r["verdict"]): int(r["c"]) for r in rows}
        total = sum(counts.values())
        right = counts.get("right", 0)
        wrong = counts.get("wrong", 0)
        judged = right + wrong
        accuracy = (right / judged) if judged else 0.0
        return {
            "total": total,
            "pending": counts.get("pending", 0),
            "right": right,
            "wrong": wrong,
            "judged": judged,
            "accuracy": round(accuracy, 4),
        }

    def verification_summary(self, horizon_minutes: int | None = None) -> dict[str, object]:
        """Статистика по автопроверкам: общая или по конкретному горизонту."""
        where = ""
        params: tuple[object, ...] = ()
        if horizon_minutes is not None:
            where = "WHERE horizon_minutes = ?"
            params = (horizon_minutes,)

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN verdict = 'right' THEN 1 ELSE 0 END) AS right_cnt,
                    SUM(CASE WHEN verdict = 'wrong' THEN 1 ELSE 0 END) AS wrong_cnt,
                    ROUND(AVG(profit_pips), 2) AS avg_profit,
                    ROUND(SUM(profit_pips), 2) AS total_profit,
                    ROUND(AVG(CASE WHEN verdict = 'right' THEN profit_pips END), 2) AS avg_win,
                    ROUND(AVG(CASE WHEN verdict = 'wrong' THEN profit_pips END), 2) AS avg_loss
                FROM verifications {where}
                """,
                params,
            ).fetchone()

        total = int(row["total"]) if row["total"] else 0
        right_cnt = int(row["right_cnt"]) if row["right_cnt"] else 0
        win_rate = round(right_cnt / total, 4) if total else 0.0

        return {
            "total": total,
            "right": right_cnt,
            "wrong": int(row["wrong_cnt"]) if row["wrong_cnt"] else 0,
            "win_rate": win_rate,
            "avg_profit": float(row["avg_profit"]) if row["avg_profit"] is not None else 0.0,
            "total_profit": float(row["total_profit"]) if row["total_profit"] is not None else 0.0,
            "avg_win": float(row["avg_win"]) if row["avg_win"] is not None else 0.0,
            "avg_loss": float(row["avg_loss"]) if row["avg_loss"] is not None else 0.0,
        }

    def verification_summary_by_instrument(
        self, horizon_minutes: int | None = None
    ) -> list[dict[str, object]]:
        where = ""
        params: tuple[object, ...] = ()
        if horizon_minutes is not None:
            where = "AND v.horizon_minutes = ?"
            params = (horizon_minutes,)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    s.instrument,
                    COUNT(*) AS total,
                    SUM(CASE WHEN v.verdict = 'right' THEN 1 ELSE 0 END) AS right_cnt,
                    ROUND(SUM(v.profit_pips), 2) AS total_profit,
                    ROUND(AVG(v.profit_pips), 2) AS avg_profit
                FROM verifications v
                JOIN suggestions s ON s.id = v.suggestion_id
                WHERE 1=1 {where}
                GROUP BY s.instrument
                ORDER BY total_profit DESC
                """,
                params,
            ).fetchall()

        out: list[dict[str, object]] = []
        for r in rows:
            total = int(r["total"])
            right_cnt = int(r["right_cnt"])
            out.append({
                "instrument": str(r["instrument"]),
                "total": total,
                "right": right_cnt,
                "win_rate": round(right_cnt / total, 4) if total else 0.0,
                "total_profit": float(r["total_profit"]) if r["total_profit"] is not None else 0.0,
                "avg_profit": float(r["avg_profit"]) if r["avg_profit"] is not None else 0.0,
            })
        return out

    # ── Positions ─────────────────────────────────────────────

    def open_position(
        self,
        *,
        strategy: str,
        source: str,
        instrument: str,
        direction: str,
        entry_price: float,
        stop_loss_price: float = 0.0,
        trail_price: float = 0.0,
    ) -> str:
        pid = str(uuid.uuid4())
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO positions (
                    id, created_at, strategy, source, instrument, direction,
                    entry_price, current_price, peak_price, trough_price,
                    stop_loss_price, trail_price, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    pid, now, strategy, source, instrument, direction,
                    entry_price, entry_price, entry_price, entry_price,
                    stop_loss_price, trail_price,
                ),
            )
            conn.commit()
        return pid

    def set_estimated_cost(self, position_id: str, cost_pips: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE positions SET estimated_cost_pips=? WHERE id=?",
                (cost_pips, position_id),
            )
            conn.commit()

    def set_broker_position_id(self, position_id: str, broker_id: int, broker_volume: int = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE positions SET broker_position_id=?, broker_volume=? WHERE id=?",
                (broker_id, broker_volume, position_id),
            )
            conn.commit()

    def get_position_by_broker_id(self, broker_id: int) -> PositionRow | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE broker_position_id=? AND status='open'",
                (broker_id,),
            ).fetchone()
            return _row_to_position(row) if row else None

    def close_position(self, position_id: str, exit_reason: str) -> None:
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE positions SET status='closed', exit_reason=?, closed_at=? WHERE id=?",
                (exit_reason, now, position_id),
            )
            conn.commit()

    def update_position_price(
        self,
        position_id: str,
        current_price: float,
        profit_pips: float,
        profit_pct: float,
        peak_price: float,
        trough_price: float,
        trail_price: float = 0.0,
        trail_activated: bool = False,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE positions SET
                    current_price=?, profit_pips=?, profit_pct=?,
                    peak_price=?, trough_price=?,
                    trail_price=?, trail_activated=?
                WHERE id=?
                """,
                (
                    current_price, profit_pips, profit_pct,
                    peak_price, trough_price,
                    trail_price, int(trail_activated),
                    position_id,
                ),
            )
            conn.commit()

    def update_closed_pnl(self, position_id: str, profit_pips: float, profit_pct: float) -> None:
        """Обновить P&L закрытой позиции (например, по данным из broker deal history)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE positions SET profit_pips=?, profit_pct=? WHERE id=?",
                (profit_pips, profit_pct, position_id),
            )
            conn.commit()

    def update_stop_loss(self, position_id: str, new_sl: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE positions SET stop_loss_price=? WHERE id=?",
                (new_sl, position_id),
            )
            conn.commit()

    def get_open_positions(self, strategy: str | None = None) -> list[PositionRow]:
        where = "WHERE status='open'"
        params: list[object] = []
        if strategy:
            where += " AND strategy=?"
            params.append(strategy)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM positions {where} ORDER BY created_at", params
            ).fetchall()
        return [_row_to_position(r) for r in rows]

    def count_open_positions(self, strategy: str | None = None, instrument: str | None = None) -> int:
        where = "WHERE status='open'"
        params: list[object] = []
        if strategy:
            where += " AND strategy=?"
            params.append(strategy)
        if instrument:
            where += " AND instrument=?"
            params.append(instrument)
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM positions {where}", params).fetchone()
        return int(row["c"])

    def position_summary_by_strategy(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_cnt,
                    SUM(CASE WHEN status='closed' AND profit_pips > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_cnt,
                    ROUND(SUM(profit_pips), 2) AS total_pips,
                    ROUND(AVG(CASE WHEN status='closed' THEN profit_pips END), 2) AS avg_pips,
                    ROUND(SUM(CASE WHEN status='closed' THEN estimated_cost_pips ELSE 0 END), 2) AS total_cost_pips
                FROM positions GROUP BY strategy
                """
            ).fetchall()
        out: list[dict[str, object]] = []
        for r in rows:
            closed = int(r["closed_cnt"]) if r["closed_cnt"] else 0
            wins = int(r["wins"]) if r["wins"] else 0
            total_pips = float(r["total_pips"]) if r["total_pips"] is not None else 0.0
            total_cost = float(r["total_cost_pips"]) if r["total_cost_pips"] is not None else 0.0
            out.append({
                "strategy": str(r["strategy"]),
                "total": int(r["total"]),
                "open": int(r["open_cnt"]) if r["open_cnt"] else 0,
                "closed": closed,
                "wins": wins,
                "win_rate": round(wins / closed, 4) if closed else 0.0,
                "total_pips": total_pips,
                "avg_pips": float(r["avg_pips"]) if r["avg_pips"] is not None else 0.0,
                "total_cost_pips": total_cost,
                "net_pips": round(total_pips - total_cost, 2),
            })
        return out

    def pnl_usd_by_strategy(self, lot_size: float = 0.01) -> dict[str, dict]:
        """P&L в долларах по стратегиям, с учётом реального broker_volume."""
        from fx_pro_bot.config.settings import pip_value_from_volume, pip_value_usd

        with self._connect() as conn:
            rows = conn.execute(
                """SELECT strategy, instrument, profit_pips, status, broker_volume
                   FROM positions WHERE broker_position_id > 0"""
            ).fetchall()

        out: dict[str, dict] = {}
        for r in rows:
            strat = str(r["strategy"])
            bv = int(r["broker_volume"] or 0)
            if bv > 0:
                pv = pip_value_from_volume(str(r["instrument"]), bv)
            else:
                pv = pip_value_usd(str(r["instrument"]), lot_size)
            pnl = float(r["profit_pips"]) * pv if r["profit_pips"] else 0.0
            if strat not in out:
                out[strat] = {"total": 0, "closed": 0, "wins": 0,
                              "pnl_usd": 0.0, "realized_usd": 0.0}
            out[strat]["total"] += 1
            out[strat]["pnl_usd"] += pnl
            if r["status"] == "closed":
                out[strat]["closed"] += 1
                out[strat]["realized_usd"] += pnl
                if float(r["profit_pips"] or 0) > 0:
                    out[strat]["wins"] += 1
        return out

    # ── Paper Positions ──────────────────────────────────────

    def open_paper_position(
        self, *, position_id: str, exit_strategy: str, entry_price: float,
    ) -> str:
        ppid = str(uuid.uuid4())
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO paper_positions (
                    id, position_id, exit_strategy, status,
                    entry_price, current_price, peak_price, created_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (ppid, position_id, exit_strategy, entry_price, entry_price, entry_price, now),
            )
            conn.commit()
        return ppid

    def close_paper_position(self, paper_id: str, exit_reason: str) -> None:
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE paper_positions SET status='closed', exit_reason=?, closed_at=? WHERE id=?",
                (exit_reason, now, paper_id),
            )
            conn.commit()

    def update_paper_position(
        self, paper_id: str, current_price: float,
        profit_pips: float, profit_pct: float, peak_price: float,
        levels_hit: list[str] | None = None,
    ) -> None:
        lh = json.dumps(levels_hit or [], ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE paper_positions SET
                    current_price=?, profit_pips=?, profit_pct=?, peak_price=?, levels_hit=?
                WHERE id=?
                """,
                (current_price, profit_pips, profit_pct, peak_price, lh, paper_id),
            )
            conn.commit()

    def get_open_paper_positions(self, position_id: str | None = None) -> list[PaperPositionRow]:
        where = "WHERE status='open'"
        params: list[object] = []
        if position_id:
            where += " AND position_id=?"
            params.append(position_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM paper_positions {where} ORDER BY exit_strategy", params
            ).fetchall()
        return [_row_to_paper(r) for r in rows]

    def paper_summary_by_exit_strategy(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT exit_strategy,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='closed' AND profit_pips > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_cnt,
                    ROUND(SUM(profit_pips), 2) AS total_pips,
                    ROUND(AVG(CASE WHEN status='closed' THEN profit_pips END), 2) AS avg_pips
                FROM paper_positions GROUP BY exit_strategy
                """
            ).fetchall()
        out: list[dict[str, object]] = []
        for r in rows:
            closed = int(r["closed_cnt"]) if r["closed_cnt"] else 0
            wins = int(r["wins"]) if r["wins"] else 0
            out.append({
                "exit_strategy": str(r["exit_strategy"]),
                "total": int(r["total"]),
                "closed": closed,
                "wins": wins,
                "win_rate": round(wins / closed, 4) if closed else 0.0,
                "total_pips": float(r["total_pips"]) if r["total_pips"] is not None else 0.0,
                "avg_pips": float(r["avg_pips"]) if r["avg_pips"] is not None else 0.0,
            })
        return out

    # ── Shadow Log ───────────────────────────────────────────

    def record_shadow(
        self, *, position_id: str, price: float,
        profit_pips: float, profit_pct: float,
        peak_profit_pips: float, peak_profit_pct: float,
        max_drawdown_pips: float,
    ) -> None:
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO shadow_log (
                    position_id, ts, price, profit_pips, profit_pct,
                    peak_profit_pips, peak_profit_pct, max_drawdown_pips
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (position_id, now, price, profit_pips, profit_pct,
                 peak_profit_pips, peak_profit_pct, max_drawdown_pips),
            )
            conn.commit()

    def shadow_summary(self) -> list[dict[str, object]]:
        """Лучшие пики и просадки по стратегиям."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.strategy,
                    MAX(s.peak_profit_pips) AS best_peak,
                    MIN(s.max_drawdown_pips) AS worst_dd,
                    ROUND(AVG(s.peak_profit_pips), 2) AS avg_peak,
                    COUNT(DISTINCT s.position_id) AS positions_tracked
                FROM shadow_log s
                JOIN positions p ON p.id = s.position_id
                GROUP BY p.strategy
                """
            ).fetchall()
        return [
            {
                "strategy": str(r["strategy"]),
                "best_peak": float(r["best_peak"]) if r["best_peak"] is not None else 0.0,
                "worst_dd": float(r["worst_dd"]) if r["worst_dd"] is not None else 0.0,
                "avg_peak": float(r["avg_peak"]) if r["avg_peak"] is not None else 0.0,
                "positions_tracked": int(r["positions_tracked"]),
            }
            for r in rows
        ]

    def verification_summary_by_source(self) -> list[dict[str, object]]:
        """Статистика по автопроверкам с разбивкой по source (ensemble / whale_cot / ...)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(s.source, 'ensemble') AS source,
                    COUNT(*) AS total,
                    SUM(CASE WHEN v.verdict = 'right' THEN 1 ELSE 0 END) AS right_cnt,
                    ROUND(SUM(v.profit_pips), 2) AS total_profit,
                    ROUND(AVG(v.profit_pips), 2) AS avg_profit
                FROM verifications v
                JOIN suggestions s ON s.id = v.suggestion_id
                GROUP BY COALESCE(s.source, 'ensemble')
                ORDER BY total_profit DESC
                """,
            ).fetchall()

        out: list[dict[str, object]] = []
        for r in rows:
            total = int(r["total"])
            right_cnt = int(r["right_cnt"])
            out.append({
                "source": str(r["source"]),
                "total": total,
                "right": right_cnt,
                "win_rate": round(right_cnt / total, 4) if total else 0.0,
                "total_profit": float(r["total_profit"]) if r["total_profit"] is not None else 0.0,
                "avg_profit": float(r["avg_profit"]) if r["avg_profit"] is not None else 0.0,
            })
        return out


def _row_to_suggestion(r: sqlite3.Row) -> SuggestionRow:
    reasons = tuple(json.loads(r["reasons_json"]))
    va = r["verdict_at"]
    try:
        source = r["source"]
    except (IndexError, KeyError):
        source = "ensemble"
    return SuggestionRow(
        id=r["id"],
        created_at=datetime.fromisoformat(r["created_at"]),
        instrument=r["instrument"],
        direction=r["direction"],
        advice_text=r["advice_text"],
        reasons=reasons,
        price_at_signal=r["price_at_signal"],
        events_context=r["events_context"],
        verdict=r["verdict"],  # type: ignore[arg-type]
        verdict_at=datetime.fromisoformat(va) if va else None,
        notes=r["notes"],
        source=source or "ensemble",
    )


def _row_to_verification(r: sqlite3.Row) -> VerificationRow:
    return VerificationRow(
        id=r["id"],
        suggestion_id=r["suggestion_id"],
        horizon_minutes=int(r["horizon_minutes"]),
        price_at_check=float(r["price_at_check"]),
        profit_pips=float(r["profit_pips"]),
        verdict=str(r["verdict"]),
        checked_at=datetime.fromisoformat(r["checked_at"]),
    )


def _row_to_position(r: sqlite3.Row) -> PositionRow:
    return PositionRow(
        id=r["id"],
        created_at=r["created_at"],
        strategy=r["strategy"],
        source=r["source"],
        instrument=r["instrument"],
        direction=r["direction"],
        entry_price=float(r["entry_price"]),
        current_price=float(r["current_price"]),
        peak_price=float(r["peak_price"]),
        trough_price=float(r["trough_price"]),
        stop_loss_price=float(r["stop_loss_price"]),
        trail_price=float(r["trail_price"]),
        trail_activated=bool(r["trail_activated"]),
        profit_pips=float(r["profit_pips"]),
        profit_pct=float(r["profit_pct"]),
        status=r["status"],
        exit_reason=r["exit_reason"] or "",
        closed_at=r["closed_at"],
        broker_position_id=int(r["broker_position_id"]) if "broker_position_id" in r.keys() else 0,
        broker_volume=int(r["broker_volume"]) if "broker_volume" in r.keys() else 0,
        estimated_cost_pips=float(r["estimated_cost_pips"]) if "estimated_cost_pips" in r.keys() else 0.0,
    )


def _row_to_paper(r: sqlite3.Row) -> PaperPositionRow:
    return PaperPositionRow(
        id=r["id"],
        position_id=r["position_id"],
        exit_strategy=r["exit_strategy"],
        status=r["status"],
        entry_price=float(r["entry_price"]),
        current_price=float(r["current_price"]),
        peak_price=float(r["peak_price"]),
        profit_pips=float(r["profit_pips"]),
        profit_pct=float(r["profit_pct"]),
        exit_reason=r["exit_reason"] or "",
        levels_hit=json.loads(r["levels_hit"]) if r["levels_hit"] else [],
        created_at=r["created_at"],
        closed_at=r["closed_at"],
    )
