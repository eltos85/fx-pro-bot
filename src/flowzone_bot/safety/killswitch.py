"""Killswitch и риск-гейты flowzone_bot.

Лимиты — research-mainstream risk-management: фикс-риск на сделку (Van K. Tharp
2007), дневной/совокупный стоп, кэп открытых позиций, rate-limit сделок/час
(анти-overtrading). На demo killswitch по умолчанию ВЫКЛЮЧЕН (лимиты ≤0).
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
    """Жёсткая остановка по дневному/совокупному убытку. Лимит ≤0 = ВЫКЛЮЧЕН
    (demo: деньги виртуальные; total-лимит не сбрасывается и заблокировал бы
    форвард-тест навсегда). Для live вернуть через env."""
    if settings.max_daily_loss_usd > 0:
        day_pnl = db.realized_pnl_since(_start_of_utc_day(now))
        if day_pnl <= -settings.max_daily_loss_usd:
            return GateDecision(False, f"daily loss {day_pnl:.2f} ≤ -{settings.max_daily_loss_usd}")
    if settings.max_total_loss_usd > 0:
        total_pnl = db.total_realized_pnl()
        if total_pnl <= -settings.max_total_loss_usd:
            return GateDecision(False, f"total loss {total_pnl:.2f} ≤ -{settings.max_total_loss_usd}")
    return GateDecision(True)


def can_open(db, settings, now: float | None = None) -> GateDecision:
    """Можно ли открыть НОВУЮ позицию (поверх is_killed). Лимит ≤0 = ВЫКЛЮЧЕН.

    ``max_trades_per_hour`` — НЕ канон-параметр (в STRATEGY_FLOWZONE.md лимита
    частоты нет; канон §5.3/§8 наоборот поощряет reload). Это generic анти-
    overtrading гард из модели scalp (TASKSPEC §6 п.8). ≤0 → выключен, тогда темп
    входов ограничивают только ``max_open_positions`` и per-symbol cooldown'ы."""
    killed = is_killed(db, settings, now)
    if not killed.allowed:
        return killed
    if settings.max_open_positions > 0 and db.open_count() >= settings.max_open_positions:
        return GateDecision(False, f"open positions ≥ {settings.max_open_positions}")
    now = now if now is not None else time.time()
    if (settings.max_trades_per_hour > 0
            and db.trades_since(now - 3600.0) >= settings.max_trades_per_hour):
        return GateDecision(False, f"rate-limit ≥ {settings.max_trades_per_hour}/h")
    return GateDecision(True)
