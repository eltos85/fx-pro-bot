"""Нормализованная модель сделки tradecard (общая для scalp / hybrid).

Обе БД ботов имеют идентичную схему ``trades`` (TASKSPEC §3.1) → единая модель.
Здесь же — производные метрики, нужные детекторам и грейдингу: R-multiple,
сессия (UTC-час), флаги win/decided, и фильтр **не-торговых** закрытий
(реконсил, не исход — TASKSPEC §3.1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from tradecard_bybit.data.reasons import factor_tokens, parse_reasons

# Закрытия без биржевого исхода (реконсил) — исключаются из WR/EXP
# (как делает сам бот: scalp_bot/state/db.py _NON_TRADE_REASONS).
NON_TRADE_REASONS = frozenset({
    "restart_flat", "entry_Cancelled", "entry_Rejected",
    "entry_Deactivated", "entry_timeout",
})


@dataclass
class Trade:
    id: int
    bot: str              # "scalp" | "hybrid"
    ts_open: float
    symbol: str
    side: str             # "long" | "short"
    qty: float
    entry: float
    sl: float
    tp: float
    score: int
    reasons_raw: str
    mode: str             # "paper" | "live"
    strategy: str
    status: str
    ts_close: float | None
    exit: float | None
    pnl_usd: float | None
    fees_usd: float | None
    close_reason: str | None
    pnl_provisional: int = 0
    pnl_verified: int = 0
    # P&L ground-truth source: "verified" | "provisional" | "db" (см. §3.2).
    pnl_source: str = "db"
    reasons: list[str] = field(default_factory=list)
    factors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.reasons:
            self.reasons = parse_reasons(self.reasons_raw)
        if not self.factors:
            self.factors = factor_tokens(self.reasons_raw)

    # ─── derived ─────────────────────────────────────────────────────────

    @property
    def is_closed(self) -> bool:
        return self.status == "closed"

    @property
    def is_non_trade(self) -> bool:
        """Реконсил-закрытие (не исход) — исключаем из WR/EXP."""
        return (self.close_reason or "") in NON_TRADE_REASONS

    @property
    def is_decided(self) -> bool:
        """Торговый исход с известным P&L (годен для WR/EXP/грейда)."""
        return (self.is_closed and not self.is_non_trade
                and self.pnl_usd is not None)

    @property
    def is_win(self) -> bool:
        return self.is_decided and (self.pnl_usd or 0.0) > 0

    @property
    def is_loss(self) -> bool:
        return self.is_decided and (self.pnl_usd or 0.0) < 0

    # SL ближе этой доли entry = «нет риск-дистанции» (float-эпсилон округления
    # цены / трейл в безубыток / SL не записан отдельно в БД). Любой реальный
    # SL на порядки дальше (наблюдаемые риск-дистанции ботов ≈0.4–1.3% entry),
    # поэтому 1e-6 — консервативный структурный фильтр данных, не подгонка P&L.
    _MIN_RISK_REL = 1e-6

    @property
    def planned_risk_usd(self) -> float | None:
        """$-риск до планового SL: qty × |entry − sl|. R-единица сделки.

        Если SL совпадает с entry (точно или в пределах ``_MIN_RISK_REL`` от
        цены) — это не реальный риск-план: R по такой сделке не определён (None)
        и она не входит в EXP/avgR (иначе деление на ~0 даёт мусорные R≈1e12).
        """
        if self.qty <= 0 or self.entry <= 0:
            return None
        dist = abs(self.entry - self.sl)
        if dist <= abs(self.entry) * self._MIN_RISK_REL:
            return None
        return self.qty * dist

    @property
    def r_multiple(self) -> float | None:
        """Реализованный R = net P&L / плановый $-риск (Van Tharp R-multiple).

        Net предпочитается verified > provisional > db (см. pnl_source). R-метрика
        нормирует исход на риск входа — основа EXP/avgR для грейдинга/детекторов.
        """
        risk = self.planned_risk_usd
        if risk is None or self.pnl_usd is None:
            return None
        return self.pnl_usd / risk

    @property
    def hour_utc(self) -> int:
        return int((self.ts_open % 86400.0) // 3600.0)

    @property
    def session(self) -> str:
        """Грубая FX/crypto-сессия по UTC-часу входа (часть «режима», §4/§9).

        Asia 00–07, London 07–12, NY 12–21, Late 21–24 (UTC). Окна нейтральные,
        каноничные FX-сессии (BIS); это срез для детектора regime_leak, не гейт.
        """
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
        """ISO год-неделя входа (YYYY-WW) — для weekly-агрегации/momentum."""
        y, w, _ = self.open_dt.isocalendar()
        return f"{y}-{w:02d}"


def win_rate(trades: list[Trade]) -> float:
    decided = [t for t in trades if t.is_decided]
    if not decided:
        return 0.0
    return sum(1 for t in decided if t.is_win) / len(decided)


def net_pnl(trades: list[Trade]) -> float:
    return sum((t.pnl_usd or 0.0) for t in trades if t.is_decided)


def expectancy_r(trades: list[Trade]) -> float | None:
    """Средний R (EXP) по сделкам с валидным R-multiple."""
    rs = [t.r_multiple for t in trades if t.is_decided and t.r_multiple is not None]
    if not rs:
        return None
    return sum(rs) / len(rs)


def decided(trades: list[Trade]) -> list[Trade]:
    return [t for t in trades if t.is_decided]
