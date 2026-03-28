from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CalendarEvent(BaseModel):
    title: str
    at: datetime
    importance: str = Field(default="medium", description="low | medium | high")
