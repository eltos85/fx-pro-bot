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
        # Intraday-свежесть VIX (2026-06-02): VIX скачет внутри дня, daily
        # close прятал стресс. Берём живой 5-мин last; 24h/5d Δ остаются
        # day-over-day (vs дневные closes). См. _vix_deltas.
        intraday_last = _latest_intraday_close(_TICKER_VIX)
        last, pct_24h, pct_5d = _vix_deltas(closes, intraday_last)
        return RiskRegimeSnapshot(
            vix_last=last,
            vix_change_24h_pct=pct_24h,
            vix_change_5d_pct=pct_5d,
            fetched_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        )


def _vix_deltas(
    daily_closes: list[float], intraday_last: float | None,
) -> tuple[float, float | None, float | None]:
    """Pure: (last, 24h%%, 5d%%) из daily closes + (опц.) intraday-last.

    ``last`` = intraday-last если есть, иначе последний daily close.
    Δ-baselines дневные (``[-2]`` / ``[-6]``) — day-over-day семантика
    сохранена, а уровень VIX живой. Выделено для unit-тестов без сети.
    """
    last = intraday_last if intraday_last is not None else daily_closes[-1]
    pct_24h = (
        (last - daily_closes[-2]) / daily_closes[-2] * 100.0
        if len(daily_closes) >= 2 and daily_closes[-2] != 0
        else None
    )
    pct_5d = (
        (last - daily_closes[-6]) / daily_closes[-6] * 100.0
        if len(daily_closes) >= 6 and daily_closes[-6] != 0
        else None
    )
    return last, pct_24h, pct_5d


def _latest_intraday_close(ticker: str) -> float | None:
    """Последний intraday Close (5-мин бары за день). None при сбое/пустоте."""
    import yfinance as yf

    try:
        df = yf.Ticker(ticker).history(
            period="1d", interval="5m", auto_adjust=False
        )
    except Exception:
        log.exception("yfinance intraday failure для %s", ticker)
        return None
    if df is None or df.empty or "Close" not in df.columns:
        return None
    vals = [float(x) for x in df["Close"].tolist() if x == x]
    return vals[-1] if vals else None


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
