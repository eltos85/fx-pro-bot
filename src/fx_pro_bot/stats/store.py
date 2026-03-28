"""SQLite: учёт советов, автоматическая верификация и статистика точности."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Verdict = Literal["pending", "right", "wrong"]


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


@dataclass(frozen=True, slots=True)
class VerificationRow:
    id: str
    suggestion_id: str
    horizon_minutes: int
    price_at_check: float
    profit_pips: float
    verdict: str
    checked_at: datetime


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
                    notes TEXT
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
            conn.commit()

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
    ) -> str:
        sid = str(uuid.uuid4())
        now = datetime.now(tz=UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO suggestions (
                    id, created_at, instrument, direction, advice_text,
                    reasons_json, price_at_signal, events_context, verdict
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
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


def _row_to_suggestion(r: sqlite3.Row) -> SuggestionRow:
    reasons = tuple(json.loads(r["reasons_json"]))
    va = r["verdict_at"]
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
