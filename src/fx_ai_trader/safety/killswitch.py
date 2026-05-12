"""KillSwitch для FX AI Trader — FX-параметры + correlation-aware.

Проверки перед каждым решением и перед каждым open:
- max_daily_loss_usd: дневная просадка превышена → блокируем до завтра.
- max_total_loss_usd: общий минус по эксперименту → полная остановка.
- max_open_positions: больше N открытых → не открываем новые.
- max_positions_per_symbol: защита от over-allocation на один инструмент
  (research: Janus Henderson 2026 «position limits per asset type»).
- same-direction concentration check: 3-я позиция в одну сторону
  запрещена (research: finaur «correlations spike in crisis» — risk-off
  заваливает все долгие/короткие сразу).
- correlation_haircut: размер 2-й позиции в одну сторону через
  коррелированные active'ы умножается на haircut (research: Janus
  Henderson 2026, finaur 2026).

Все срабатывания логируются в standard logger; verbose-причина
возвращается caller'у для записи в decisions.error.
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
    correlation_haircut: float  # ∈ (0, 1], 0.7 = haircut 30%


@dataclass
class CheckResult:
    allowed: bool
    reason: str = ""
    # Доп. поля для open-checks: рекомендованный размер после haircut.
    size_multiplier: float = 1.0


# Группы коррелированных commodity-инструментов. Risk-off ралли часто
# гонит и gold, и oil в одну сторону (например, oil вверх + gold вверх
# при escalation Middle-East tensions). Внутри группы considered "correlated"
# для same-direction concentration / haircut целей.
# Сейчас одна группа на оба наших инструмента — упрощение Phase 1.
_CORRELATED_GROUPS: tuple[set[str], ...] = (
    {"XAUUSD", "BZ=F"},
)


def _correlated_with(symbol: str) -> set[str]:
    """Возвращает множество correlated-инструментов (исключая сам symbol)."""
    for group in _CORRELATED_GROUPS:
        if symbol in group:
            return group - {symbol}
    return set()


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
        side: str,
    ) -> CheckResult:
        """Проверка перед открытием конкретной позиции.

        side: ``"BUY"`` или ``"SELL"`` (cTrader uppercase нотация).
        """
        gen = self.check_can_trade()
        if not gen.allowed:
            return gen

        open_positions = self.store.get_open_positions()
        open_count = len(open_positions)
        if open_count >= self.config.max_open_positions:
            return CheckResult(
                allowed=False,
                reason=f"max positions reached: {open_count}/{self.config.max_open_positions}",
            )

        # Per-symbol cap.
        same_symbol = [p for p in open_positions if p.symbol == symbol]
        if len(same_symbol) >= self.config.max_positions_per_symbol:
            return CheckResult(
                allowed=False,
                reason=(
                    f"max positions per symbol ({symbol}): "
                    f"{len(same_symbol)}/{self.config.max_positions_per_symbol}"
                ),
            )

        # Same-direction concentration check.
        # Если уже есть 2+ позиции в ту же сторону (по любому из
        # correlated-инструментов), 3-ю в ту же сторону не открываем.
        side_up = side.upper()
        correlated = _correlated_with(symbol) | {symbol}
        same_dir_count = sum(
            1 for p in open_positions
            if p.side.upper() == side_up and p.symbol in correlated
        )
        if same_dir_count >= 2:
            return CheckResult(
                allowed=False,
                reason=(
                    f"same-direction concentration: уже {same_dir_count} {side_up} позиций "
                    f"по correlated assets {sorted(correlated)}, 3-я запрещена "
                    f"(research: finaur 2026 «correlations spike in crisis»)"
                ),
            )

        # Correlation haircut: если уже есть ≥1 позиция в ту же сторону
        # по correlated active → размер новой умножаем на haircut.
        haircut = 1.0
        if same_dir_count >= 1:
            haircut = self.config.correlation_haircut
            log.info(
                "KS: correlation-haircut applied: %.2f (already %d %s positions "
                "in correlated set %s)",
                haircut, same_dir_count, side_up, sorted(correlated),
            )

        return CheckResult(allowed=True, size_multiplier=haircut)

    def position_count_by_symbol(self, positions: list[AiFxPosition]) -> dict[str, int]:
        """Helper для дашборда / логирования: распределение позиций по символам."""
        out: dict[str, int] = {}
        for p in positions:
            out[p.symbol] = out.get(p.symbol, 0) + 1
        return out
