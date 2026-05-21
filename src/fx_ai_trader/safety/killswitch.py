"""KillSwitch для FX AI Trader — broker-safety only (НЕ strategy tuning).

Философия v1.0 (12-May-2026 после переработки промпта на discretionary
commodity trader): LLM имеет свободу принимать решения по R:R, risk
size, correlation как профессиональный трейдер. KillSwitch охраняет
ТОЛЬКО три класса риска:

1. Catastrophic loss caps — daily/total max loss как полный стоп
   эксперимента. НЕ tuning-параметр, а защита от runaway baseline.

2. Broker margin safety — max_open_positions cap (защита от runaway
   open-loop) + max_positions_per_symbol (sanity).

3. Position direction validation (в executor.py) — SL/TP в правильную
   сторону, volume > 0, базовые броker-input проверки.

Сняты в v1.0 (были в v0.x):
- correlation_haircut — LLM сам решит, коррелировать ли gold+oil.
- same-direction concentration block (3-rd same-side rejected) —
  тоже LLM-решение, не наша эвристика.
- R:R ≥ 1.5 hard в executor — LLM сам решит R:R по setup'у.
- risk_per_trade $25 hard в executor — LLM сам решит position size
  по Van Tharp R-multiple, ограничен max_lot_size как safety floor.

Source: реальная discretionary methodology (Mark Douglas "Trading in
the Zone", Van Tharp "Definitive Guide to Position Sizing") + KenMacro
institutional framework по gold/oil. См. docstring в prompts.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fx_ai_trader.state.db import AiFxPosition, AiFxTraderStore

log = logging.getLogger(__name__)


@dataclass
class KillSwitchConfig:
    max_daily_loss_usd: float
    max_total_loss_usd: float
    max_open_positions: int
    max_positions_per_symbol: int
    # Per-symbol override: NG=F → 1 позиция (vs 3 у gold/oil). См.
    # config/settings.py docstring и BUILDLOG NG enhancement v1.2.
    per_symbol_max_positions: dict[str, int] | None = None

    def get_max_positions_for(self, symbol: str) -> int:
        if self.per_symbol_max_positions:
            cap = self.per_symbol_max_positions.get(symbol)
            if cap is not None:
                return min(cap, self.max_positions_per_symbol)
        return self.max_positions_per_symbol


@dataclass
class CheckResult:
    allowed: bool
    reason: str = ""
    # Backwards-compat для executor.py: всегда 1.0 в v1.0 (correlation
    # haircut снят). Поле оставлено чтобы не ломать caller-сторону.
    size_multiplier: float = 1.0


class KillSwitch:
    def __init__(self, config: KillSwitchConfig, store: AiFxTraderStore) -> None:
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

    def check_can_open_position(
        self,
        *,
        symbol: str,
        side: str,  # noqa: ARG002  — kept for API stability, не используется в v1.0
    ) -> CheckResult:
        """Проверка перед открытием позиции — только broker safety.

        v1.0 убрала: correlation haircut, same-direction concentration
        block. LLM сам решает аллокацию. Здесь — только max-cap'ы.
        """
        gen = self.check_can_trade()
        if not gen.allowed:
            return gen

        open_positions = self.store.get_open_positions()
        open_count = len(open_positions)
        if open_count >= self.config.max_open_positions:
            return CheckResult(
                allowed=False,
                reason=(
                    f"max positions reached: "
                    f"{open_count}/{self.config.max_open_positions} — "
                    f"broker margin safety, не strategy tuning"
                ),
            )

        same_symbol = [p for p in open_positions if p.symbol == symbol]
        symbol_cap = self.config.get_max_positions_for(symbol)
        if len(same_symbol) >= symbol_cap:
            return CheckResult(
                allowed=False,
                reason=(
                    f"max positions per symbol ({symbol}): "
                    f"{len(same_symbol)}/{symbol_cap}"
                    + (
                        f" (per-symbol override; default "
                        f"{self.config.max_positions_per_symbol})"
                        if symbol_cap != self.config.max_positions_per_symbol
                        else ""
                    )
                ),
            )

        return CheckResult(allowed=True, size_multiplier=1.0)

    def position_count_by_symbol(self, positions: list[AiFxPosition]) -> dict[str, int]:
        """Helper для дашборда / логирования."""
        out: dict[str, int] = {}
        for p in positions:
            out[p.symbol] = out.get(p.symbol, 0) + 1
        return out
