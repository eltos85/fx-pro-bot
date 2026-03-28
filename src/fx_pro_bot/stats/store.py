"""SQLite: учёт советов и ручная оценка «угадал / ошибся» при тестах."""

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
            conn.commit()

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
                """
                UPDATE suggestions SET verdict = ?, verdict_at = ?, notes = ?
                WHERE id = ?
                """,
                (verdict, now, notes, suggestion_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def summary(self) -> dict[str, int | float]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT verdict, COUNT(*) AS c FROM suggestions GROUP BY verdict
                """
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

    def list_recent(self, limit: int = 20) -> list[SuggestionRow]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM suggestions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_dataclass(r) for r in rows]


def _row_to_dataclass(r: sqlite3.Row) -> SuggestionRow:
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
