"""KillSwitch для AI-Trader.

Проверки перед каждым решением и перед каждым исполнением:
- max_daily_loss_usd: дневная просадка превышена → блокируем до завтра
- max_total_loss_usd: общий минус по эксперименту → полная остановка
- max_open_positions: больше N открытых → не открываем новые
- max_leverage: запрашиваемое плечо больше лимита → reject

Все срабатывания логируются.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ai_trader.state.db import AiTraderStore

log = logging.getLogger(__name__)


@dataclass
class KillSwitchConfig:
    max_daily_loss_usd: float
    max_total_loss_usd: float
    max_open_positions: int
    max_leverage: int


@dataclass
class CheckResult:
    allowed: bool
    reason: str = ""


class KillSwitch:
    def __init__(self, config: KillSwitchConfig, store: AiTraderStore) -> None:
        self.config = config
        self.store = store

    def check_can_trade(self) -> CheckResult:
        """Глобальная проверка: можем ли вообще что-то делать."""
        today = self.store.get_today_pnl()
        if today <= -self.config.max_daily_loss_usd:
            return CheckResult(
                allowed=False,
                reason=(
                    f"daily loss limit hit: today=${today:+.2f} ≤ "
                    f"-${self.config.max_daily_loss_usd:.2f}"
                ),
            )

        total = self.store.get_total_pnl()
        if total <= -self.config.max_total_loss_usd:
            return CheckResult(
                allowed=False,
                reason=(
                    f"TOTAL loss limit hit: ${total:+.2f} ≤ "
                    f"-${self.config.max_total_loss_usd:.2f} — эксперимент остановлен"
                ),
            )

        return CheckResult(allowed=True)

    def check_can_open_position(self, leverage: int) -> CheckResult:
        """Проверка перед открытием конкретной позиции."""
        gen = self.check_can_trade()
        if not gen.allowed:
            return gen

        open_count = len(self.store.get_open_positions())
        if open_count >= self.config.max_open_positions:
            return CheckResult(
                allowed=False,
                reason=f"max positions reached: {open_count}/{self.config.max_open_positions}",
            )

        if leverage > self.config.max_leverage:
            return CheckResult(
                allowed=False,
                reason=f"leverage {leverage}x > max {self.config.max_leverage}x",
            )

        return CheckResult(allowed=True)
