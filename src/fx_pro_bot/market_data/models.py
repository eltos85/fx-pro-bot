from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class InstrumentId:
    """Унифицированный идентификатор инструмента (символ + площадка при необходимости)."""

    symbol: str
    venue: str | None = None


@dataclass(frozen=True, slots=True)
class Bar:
    instrument: InstrumentId
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Tick:
    instrument: InstrumentId
    ts: datetime
    bid: float
    ask: float
