from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from fx_pro_bot.events.models import CalendarEvent


def load_events(path: Path) -> list[CalendarEvent]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    items = data.get("events") or []
    out: list[CalendarEvent] = []
    for it in items:
        at = it["at"]
        if isinstance(at, str):
            at_parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        else:
            continue
        if at_parsed.tzinfo is None:
            at_parsed = at_parsed.replace(tzinfo=UTC)
        out.append(CalendarEvent(title=it["title"], at=at_parsed, importance=it.get("importance", "medium")))
    return sorted(out, key=lambda e: e.at)


def events_near(
    events: list[CalendarEvent],
    *,
    now: datetime,
    within_hours: float = 48.0,
    min_importance: str = "medium",
) -> tuple[CalendarEvent, ...]:
    """События в окне [now, now+within_hours], фильтр по важности."""
    rank = {"low": 0, "medium": 1, "high": 2}
    min_r = rank.get(min_importance, 1)
    end = now + timedelta(hours=within_hours)
    chosen: list[CalendarEvent] = []
    for e in events:
        if e.at < now or e.at > end:
            continue
        if rank.get(e.importance, 0) < min_r:
            continue
        chosen.append(e)
    return tuple(chosen)


def events_to_json_blob(events: tuple[CalendarEvent, ...]) -> str:
    return json.dumps(
        [{"title": e.title, "at": e.at.isoformat(), "importance": e.importance} for e in events],
        ensure_ascii=False,
    )
