"""Общие типы событий коллекторов yorsh_bot.

Dataclasses для сырого потока: trades, L2-диффы, снапшоты. Поля
``ts_exch``/``ts_local`` — биржевой и локальный таймстемп (UTC, секунды).
``exchange``/``symbol`` — атрибутика. ``payload`` — биржа-специфичный словарь
(сырой JSON/protobuf-decoded), пишется в recorder как есть.

Реализация — milestone M1 (MEXC) / M2 (Bitget). Здесь — каркас типов.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Trade:
    """Агрессивный принт (taker trade)."""
    exchange: str
    symbol: str
    ts_exch: float
    ts_local: float
    price: float
    size: float
    side: str          # buy | sell
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class DepthDiff:
    """Инкрементальный L2-дифф."""
    exchange: str
    symbol: str
    ts_exch: float
    ts_local: float
    # side -> list[(price, size)]; size=0 = уровень удалён
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    # version-последовательность: MEXC (from_version,to_version) / Bitget (seq,pseq)
    seq: int | None = None
    prev_seq: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class BookSnapshot:
    """Полный снапшот книги (REST reinit / периодическая запись)."""
    exchange: str
    symbol: str
    ts_exch: float
    ts_local: float
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    seq: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Источник снапшота. Для MEXC REST-snapshot — авторитетная инициализация
    # книги (lastUpdateId в том же version-пространстве, что WS fromVersion).
    # Для Bitget WS books-snapshot (action:"snapshot") инициализирует книгу,
    # а REST orderbook отдаёт ts (не в WS seq-пространстве) → только запись.
    source: str = "rest"   # "rest" | "ws_books"
