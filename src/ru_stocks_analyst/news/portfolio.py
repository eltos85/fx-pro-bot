"""Тикеры из портфеля Tinkoff."""
from __future__ import annotations

from typing import Any


def portfolio_tickers(portfolio: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for p in portfolio.get("positions") or []:
        t = (p.get("ticker") or "").strip().upper()
        if t and t not in out:
            out.append(t)
    return out
