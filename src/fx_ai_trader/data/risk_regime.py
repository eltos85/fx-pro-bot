"""Risk-regime feed (Enhancement C, 2026-05-29): CBOE VIX через yfinance.

Зачем: VIX — рыночный «индекс страха». Risk-on/off режим напрямую влияет
на наши инструменты:
- Gold = safe haven: всплеск VIX часто = bid на золото (flight to safety).
- Oil / risk assets: всплеск VIX = risk-off, давление вниз на нефть.

Подаём СЫРОЕ значение VIX + 24h Δ. Интерпретацию (calm / elevated / stress)
делает LLM — мы не зашиваем пороговые «magic numbers» в код (no-data-fitting:
VIX-режимные банды это эвристика, а не данные). LLM сам сопоставит уровень
с safe-haven логикой золота.

Research basis:
- Whaley (2000, J. Portfolio Management) «The Investor Fear Gauge» — VIX как
  мера ожидаемой волатильности / риск-аппетита.
- Baur & Lucey (2010, Financial Review) «Is Gold a Hedge or a Safe Haven?» —
  золото получает bid в периоды рыночного стресса (рост implied vol).

Источник данных (free, без ключа): yfinance тикер ``^VIX`` (CBOE VIX index).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

_TICKER_VIX = "^VIX"


@dataclass
class RiskRegimeSnapshot:
    vix_last: float | None
    vix_change_24h_pct: float | None
    vix_change_5d_pct: float | None
    fetched_at_utc: str


class RiskRegimeProvider:
    """Кэширующий yfinance-клиент для VIX. TTL по умолчанию 30 мин."""

    def __init__(self, cache_ttl_sec: int = 1800) -> None:
        self._cache_ttl = cache_ttl_sec
        self._cache: RiskRegimeSnapshot | None = None
        self._cache_ts: float = 0.0

    @property
    def enabled(self) -> bool:
        return True

    def get_snapshot(self) -> RiskRegimeSnapshot | None:
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache
        try:
            snap = self._fetch_fresh()
        except Exception:
            log.exception("RiskRegime fetch failed (продолжаю с прошлым кэшем)")
            return self._cache
        if snap is not None:
            self._cache = snap
            self._cache_ts = now
        return snap or self._cache

    def _fetch_fresh(self) -> RiskRegimeSnapshot | None:
        from datetime import UTC, datetime

        import yfinance as yf

        try:
            df = yf.Ticker(_TICKER_VIX).history(
                period="10d", interval="1d", auto_adjust=False
            )
        except Exception:
            log.exception("yfinance failure для %s", _TICKER_VIX)
            return self._cache
        if df is None or df.empty or "Close" not in df.columns:
            log.info("RiskRegime: пустой DataFrame для VIX")
            return None
        closes = [float(x) for x in df["Close"].tolist() if x == x]
        if not closes:
            return None
        last = closes[-1]
        pct_24h = (
            (closes[-1] - closes[-2]) / closes[-2] * 100.0
            if len(closes) >= 2 and closes[-2] != 0
            else None
        )
        pct_5d = (
            (closes[-1] - closes[-6]) / closes[-6] * 100.0
            if len(closes) >= 6 and closes[-6] != 0
            else None
        )
        return RiskRegimeSnapshot(
            vix_last=last,
            vix_change_24h_pct=pct_24h,
            vix_change_5d_pct=pct_5d,
            fetched_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        )


def format_risk_regime_snapshot(snap: RiskRegimeSnapshot | None) -> str | None:
    """Text-блок VIX для LLM. None если данных нет."""
    if snap is None or snap.vix_last is None:
        return None
    d24 = (
        f"24h={snap.vix_change_24h_pct:+.1f}%"
        if snap.vix_change_24h_pct is not None
        else "24h=n/a"
    )
    d5 = (
        f"5d={snap.vix_change_5d_pct:+.1f}%"
        if snap.vix_change_5d_pct is not None
        else "5d=n/a"
    )
    return (
        "=== RISK REGIME (CBOE VIX; gold safe-haven bid on stress, "
        "oil risk-off on spikes) ===\n"
        f"VIX: {snap.vix_last:.2f} ({d24}, {d5})\n"
        f"(fetched {snap.fetched_at_utc} UTC)"
    )
