from __future__ import annotations

from dataclasses import dataclass

from fx_pro_bot.analysis.signals import Signal, TrendDirection
from fx_pro_bot.market_data.models import InstrumentId


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    quantity: float | None = None


class RiskManager:
    """Лимиты на сделку и день; расширяется под реальный equity."""

    def __init__(
        self,
        *,
        max_risk_per_trade_pct: float,
        max_daily_loss_pct: float,
        equity: float = 100_000.0,
        lot_size: float = 1.0,
    ) -> None:
        self._max_risk = max_risk_per_trade_pct
        self._max_daily = max_daily_loss_pct
        self._equity = equity
        self._lot_size = lot_size
        self._daily_pnl = 0.0

    def on_day_rollover(self) -> None:
        self._daily_pnl = 0.0

    def record_pnl(self, delta: float) -> None:
        self._daily_pnl += delta

    def evaluate(self, instrument: InstrumentId, signal: Signal) -> RiskDecision:
        if not instrument.symbol:
            return RiskDecision(allowed=False, reason="invalid_instrument")

        if self._daily_pnl <= -self._max_daily / 100.0 * self._equity:
            return RiskDecision(allowed=False, reason="daily_loss_limit")

        if signal.direction == TrendDirection.FLAT:
            return RiskDecision(allowed=False, reason="flat_signal")

        # Дальше: объём от риска на сделку, ATR и цены стопа — пока фиксированный лот
        _ = self._max_risk
        return RiskDecision(allowed=True, reason="ok", quantity=self._lot_size)
