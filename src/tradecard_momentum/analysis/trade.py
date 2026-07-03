"""Нормализованная модель сделки tradecard_momentum.

Сделка momentum-бота **реконструируется из cTrader deal-list** (ground truth по
P&L) + traceability-сигнала из ``momentum_decisions`` (сила momentum/ATR на
входе). У momentum-бота нет таблицы закрытых сделок с realized PnL — поэтому
истина по деньгам берётся у брокера (stats-collection.mdc), а БД даёт лишь
контекст входа.

R-multiple реконструируется в **ценовых единицах** так же, как считает сам бот:
``R = signed_move / risk_price``, где ``risk_price = atr × atr_stop_mult`` —
плановая SL-дистанция входа (fx_momentum_bot/app/main.py: ``sl_distance =
signal.atr * settings.atr_stop_mult``). ATR берётся из совпавшего по времени
executed-решения (``momentum_position_state`` чистится при закрытии, ATR-решение
персистит). Если решение не найдено — R/grade недоступны (None), но net$ всё
равно учитывается в P&L.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class MomentumTrade:
    position_id: int
    symbol: str               # cTrader name (EURUSD/GBPUSD/...)
    side: str                 # "long" | "short"
    ts_open: float            # epoch сек (UTC)
    ts_close: float | None
    entry: float
    exit: float | None
    volume_units: int
    # ground truth (broker deal-list)
    gross_usd: float
    swap_usd: float
    commission_usd: float
    # сигнал входа (traceability, momentum_decisions) — может быть None
    signal_momentum: float | None = None   # |momentum_value| на входе (грейд-score)
    signal_atr: float | None = None
    # ctx_* — контекст входа (пишется ботом с 2026-07-03): режим/тренд/спред
    # на момент решения. None для сделок до внедрения метрик.
    ctx_ema_dist_atr: float | None = None
    ctx_adx: float | None = None
    ctx_with_htf: bool | None = None
    ctx_spread_pips: float | None = None
    # реконструированный плановый риск (ценовая SL-дистанция) и его источник
    risk_price: float | None = None
    n_closing_deals: int = 1               # >1 → был частичный выход (partial)
    mode: str = "live"                     # broker deal-list = реальные ордера

    # ─── derived ─────────────────────────────────────────────────────────

    @property
    def net_usd(self) -> float:
        """Net P&L = gross + swap + commission (broker-净, ground truth)."""
        return self.gross_usd + self.swap_usd + self.commission_usd

    @property
    def is_closed(self) -> bool:
        return self.ts_close is not None and self.exit is not None

    @property
    def is_decided(self) -> bool:
        """Закрытая сделка с известным net — годна для WR/EXP/грейда."""
        return self.is_closed

    @property
    def is_win(self) -> bool:
        return self.is_decided and self.net_usd > 0

    @property
    def is_loss(self) -> bool:
        return self.is_decided and self.net_usd < 0

    @property
    def r_multiple(self) -> float | None:
        """Реализованный R = signed price-move / плановый риск (Van Tharp).

        Ценовой R (как считает сам бот в _r_multiple): нормирует ход на плановую
        SL-дистанцию входа. None, если нет risk_price (не нашли решение входа)
        или нет exit. partial-выходы не искажают (берём финальный exit как
        средневзвешенную точку выхода последнего closing-deal — приближение).
        """
        if self.risk_price is None or self.risk_price <= 0 or self.exit is None:
            return None
        if self.side == "long":
            move = self.exit - self.entry
        else:
            move = self.entry - self.exit
        return move / self.risk_price

    @property
    def hour_utc(self) -> int:
        return int((self.ts_open % 86400.0) // 3600.0)

    @property
    def session(self) -> str:
        """FX-сессия по UTC-часу входа (Asia 00–07 / London 07–12 / NY 12–21 /
        Late 21–24). Каноничные FX-сессии (BIS); срез для detector'ов, не гейт."""
        h = self.hour_utc
        if h < 7:
            return "asia"
        if h < 12:
            return "london"
        if h < 21:
            return "ny"
        return "late"

    @property
    def open_dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts_open, tz=UTC)

    @property
    def iso_week(self) -> str:
        y, w, _ = self.open_dt.isocalendar()
        return f"{y}-{w:02d}"


def win_rate(trades: list[MomentumTrade]) -> float:
    decided = [t for t in trades if t.is_decided]
    if not decided:
        return 0.0
    return sum(1 for t in decided if t.is_win) / len(decided)


def net_pnl(trades: list[MomentumTrade]) -> float:
    return sum(t.net_usd for t in trades if t.is_decided)


def expectancy_r(trades: list[MomentumTrade]) -> float | None:
    """Средний R (EXP) по сделкам с валидным R-multiple."""
    rs = [t.r_multiple for t in trades if t.is_decided and t.r_multiple is not None]
    if not rs:
        return None
    return sum(rs) / len(rs)


def decided(trades: list[MomentumTrade]) -> list[MomentumTrade]:
    return [t for t in trades if t.is_decided]
