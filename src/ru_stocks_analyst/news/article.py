"""Модель новостной статьи из RSS."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NewsArticle:
    title: str
    summary: str
    source: str
    url: str
    published_iso: str = ""
    tickers: list[str] = field(default_factory=list)
    is_market_wide: bool = False
