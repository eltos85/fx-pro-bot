"""Capital Safety hard-limits для AI Arena.

ВАЖНО: это **наша инфраструктурная защита**, не часть Nof1-стратегии.
Nof1 Alpha Arena KillSwitch не имеет (у них $10k капитал на S1, у нас
$500 sandbox). Все лимиты явно прописаны в SYSTEM_PROMPT для
прозрачности модели:
- Max 3 simultaneous positions
- Max 5x leverage
- Max $10 risk per trade
- Daily realised loss ≤ $50
- Total realised loss ≤ $200
- R:R ≥ 1.5

Менять лимиты разрешено через env vars (см. settings.py), но любое
изменение должно одновременно отражаться в SYSTEM_PROMPT — иначе
LLM будет генерить решения, которые мы постоянно reject'им
(invalid-action rate растёт, reasoning теряет смысл).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ai_arena.state.db import AiArenaStore

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
    def __init__(self, config: KillSwitchConfig, store: AiArenaStore) -> None:
        self.config = config
        self.store = store

    def check_can_trade(self) -> CheckResult:
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
