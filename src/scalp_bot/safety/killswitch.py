"""Killswitch и риск-гейты scalp_bot.

Лимиты — research-mainstream 2026 risk-management (KuCoin Risk Mgmt 2026):
1-2% риска на сделку, дневной стоп, кэп открытых позиций, rate-limit
сделок/час (анти-overtrading для скальпа, b2broker 2026).
"""
from __future__ import annotations

import time
from dataclasses import dataclass


def _start_of_utc_day(now: float | None = None) -> float:
    now = now if now is not None else time.time()
    return now - (now % 86400.0)


@dataclass
class GateDecision:
    allowed: bool
    reason: str | None = None


def is_killed(db, settings, now: float | None = None) -> GateDecision:
    """Жёсткая остановка по дневному/совокупному убытку."""
    day_pnl = db.realized_pnl_since(_start_of_utc_day(now))
    if day_pnl <= -abs(settings.max_daily_loss_usd):
        return GateDecision(False, f"daily loss {day_pnl:.2f} ≤ -{settings.max_daily_loss_usd}")
    total_pnl = db.total_realized_pnl()
    if total_pnl <= -abs(settings.max_total_loss_usd):
        return GateDecision(False, f"total loss {total_pnl:.2f} ≤ -{settings.max_total_loss_usd}")
    return GateDecision(True)


def can_open(db, settings, now: float | None = None) -> GateDecision:
    """Можно ли открыть НОВУЮ позицию (поверх is_killed)."""
    killed = is_killed(db, settings, now)
    if not killed.allowed:
        return killed
    if db.open_count() >= settings.max_open_positions:
        return GateDecision(False, f"open positions ≥ {settings.max_open_positions}")
    now = now if now is not None else time.time()
    if db.trades_since(now - 3600.0) >= settings.max_trades_per_hour:
        return GateDecision(False, f"rate-limit ≥ {settings.max_trades_per_hour}/h")
    return GateDecision(True)
