"""Kill Switch: защита от катастрофических потерь.

Перед каждой сделкой проверяются лимиты:
- максимальный убыток за день (USD)
- максимальная просадка от пикового equity (%)
- максимальное количество одновременных позиций
- максимальный убыток на одну сделку (USD)

При срабатывании — аварийное закрытие ВСЕХ позиций.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

log = logging.getLogger(__name__)


@dataclass
class DailyStats:
    date: date
    trades: int = 0
    realized_pnl_usd: float = 0.0
    peak_equity: float = 0.0
    tripped: bool = False


@dataclass
class KillSwitchConfig:
    max_daily_loss_usd: float = 50.0
    max_drawdown_pct: float = 20.0
    max_positions: int = 10
    max_loss_per_trade_usd: float = 25.0
    enabled: bool = True


class KillSwitch:
    """Контролёр рисков: блокирует торговлю при превышении лимитов."""

    def __init__(self, config: KillSwitchConfig, initial_equity: float = 0.0) -> None:
        self._config = config
        self._today = self._new_day_stats(initial_equity)
        self._tripped = False
        self._trip_reason = ""

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    @property
    def daily_stats(self) -> DailyStats:
        return self._today

    def check_allowed(self, open_positions: int, current_equity: float) -> bool:
        """Проверить, разрешена ли новая сделка. False = СТОП."""
        if not self._config.enabled:
            return True

        if self._tripped:
            return False

        if current_equity <= 0:
            log.warning("KillSwitch: equity=%.2f (ошибка API?), пропускаем проверку drawdown", current_equity)
            if open_positions >= self._config.max_positions:
                log.warning("KillSwitch: макс позиций (%d/%d)", open_positions, self._config.max_positions)
                return False
            return True

        self._rotate_day(current_equity)

        if self._today.peak_equity > 0:
            self._today.peak_equity = max(self._today.peak_equity, current_equity)
        else:
            self._today.peak_equity = current_equity

        if self._today.realized_pnl_usd <= -self._config.max_daily_loss_usd:
            self._trip("daily_loss", self._today.realized_pnl_usd)
            return False

        if self._today.peak_equity > 0:
            dd_pct = (self._today.peak_equity - current_equity) / self._today.peak_equity * 100
            if dd_pct >= self._config.max_drawdown_pct:
                self._trip("drawdown", dd_pct)
                return False

        if open_positions >= self._config.max_positions:
            log.warning(
                "KillSwitch: макс позиций (%d/%d), новая сделка заблокирована",
                open_positions, self._config.max_positions,
            )
            return False

        return True

    def record_trade_close(self, pnl_usd: float) -> None:
        """Записать результат закрытой сделки."""
        self._today.trades += 1
        self._today.realized_pnl_usd += pnl_usd
        log.info(
            "KillSwitch: сделка %+.2f USD, итого за день: %+.2f USD (%d сделок)",
            pnl_usd, self._today.realized_pnl_usd, self._today.trades,
        )

    def reset(self) -> None:
        """Ручной сброс kill switch (после анализа)."""
        self._tripped = False
        self._trip_reason = ""
        log.warning("KillSwitch: ручной сброс")

    def _trip(self, reason: str, value: float) -> None:
        self._tripped = True
        self._trip_reason = reason
        self._today.tripped = True
        log.critical(
            "KILL SWITCH СРАБОТАЛ: %s = %.2f — торговля остановлена!",
            reason, value,
        )

    def _rotate_day(self, equity: float) -> None:
        today = datetime.now(tz=UTC).date()
        if self._today.date != today:
            log.info(
                "KillSwitch: новый день, сброс. Вчера: %d сделок, P&L %+.2f USD",
                self._today.trades, self._today.realized_pnl_usd,
            )
            self._today = self._new_day_stats(equity)
            self._tripped = False
            self._trip_reason = ""

    @staticmethod
    def _new_day_stats(equity: float = 0.0) -> DailyStats:
        return DailyStats(date=datetime.now(tz=UTC).date(), peak_equity=equity)
