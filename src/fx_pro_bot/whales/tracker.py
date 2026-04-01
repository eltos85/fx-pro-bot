"""Объединение whale-источников: COT + Myfxbook sentiment, запись сигналов в БД."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fx_pro_bot.analysis.signals import TrendDirection
from fx_pro_bot.config.settings import Settings, display_name
from fx_pro_bot.market_data.yfinance_feed import bars_from_yfinance
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.whales.cot import CotSignal, fetch_cot_signals
from fx_pro_bot.whales.sentiment import SentimentSignal, fetch_sentiment_signals

log = logging.getLogger(__name__)

COT_CACHE_TTL = 6 * 3600


@dataclass
class _CotCache:
    signals: list[CotSignal]
    fetched_at: float = 0.0


class WhaleTracker:
    """Трекер китов: COT-отчёты + sentiment, запись сигналов, сравнение с ансамблем."""

    def __init__(self, store: StatsStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings
        self._cot_cache = _CotCache(signals=[])

    def run(self) -> None:
        """Один цикл: получить сигналы от китов и записать в БД."""
        cot_signals = self._get_cot_signals()
        sentiment_signals = self._get_sentiment_signals()

        self._record_cot(cot_signals)
        self._record_sentiment(sentiment_signals)

        self._log_whale_summary(cot_signals, sentiment_signals)

    def _get_cot_signals(self) -> list[CotSignal]:
        now = time.time()
        if self._cot_cache.signals and (now - self._cot_cache.fetched_at) < COT_CACHE_TTL:
            return self._cot_cache.signals

        signals = fetch_cot_signals()
        if signals:
            self._cot_cache = _CotCache(signals=signals, fetched_at=now)
        return signals

    def _get_sentiment_signals(self) -> list[SentimentSignal]:
        return fetch_sentiment_signals(
            email=self._settings.myfxbook_email,
            password=self._settings.myfxbook_password,
        )

    def _record_cot(self, signals: list[CotSignal]) -> None:
        for sig in signals:
            if sig.direction == TrendDirection.FLAT:
                continue

            price = self._current_price(sig.symbol)
            reasons = (
                f"COT net={sig.net_position:+d}",
                f"long {sig.long_pct:.0f}%",
                f"chg={sig.net_change:+d}",
                f"report {sig.report_date[:10]}",
            )

            self._store.record_suggestion(
                instrument=sig.symbol,
                direction=sig.direction.value,
                advice_text=f"COT: киты {sig.direction.value.upper()} {display_name(sig.symbol)}",
                reasons=reasons,
                price_at_signal=price,
                events_context=None,
                source="whale_cot",
            )

    def _record_sentiment(self, signals: list[SentimentSignal]) -> None:
        for sig in signals:
            if sig.direction == TrendDirection.FLAT:
                continue

            price = self._current_price(sig.symbol)
            reasons = (
                f"retail long {sig.retail_long_pct:.0f}%",
                f"retail short {sig.retail_short_pct:.0f}%",
                f"contrarian → {sig.direction.value.upper()}",
            )

            self._store.record_suggestion(
                instrument=sig.symbol,
                direction=sig.direction.value,
                advice_text=(
                    f"Sentiment: ретейл {sig.retail_long_pct:.0f}%L/{sig.retail_short_pct:.0f}%S"
                    f" → контрариан {sig.direction.value.upper()} {display_name(sig.symbol)}"
                ),
                reasons=reasons,
                price_at_signal=price,
                events_context=None,
                source="whale_sentiment",
            )

    def _current_price(self, symbol: str) -> float | None:
        try:
            bars = bars_from_yfinance(symbol, period="1d", interval="1m")
            return bars[-1].close if bars else None
        except Exception:
            return None

    def _log_whale_summary(
        self, cot: list[CotSignal], sentiment: list[SentimentSignal]
    ) -> None:
        log.info("── Киты ──")

        if cot:
            parts: list[str] = []
            for s in cot:
                dn = display_name(s.symbol)
                if s.direction != TrendDirection.FLAT:
                    parts.append(f"{dn} {s.direction.value.upper()} ({s.long_pct:.0f}%L)")
                else:
                    parts.append(f"{dn} FLAT")
            log.info("  COT (%s): %s", cot[0].report_date[:10], ", ".join(parts))
        else:
            log.info("  COT: данные недоступны")

        if sentiment:
            parts = []
            for s in sentiment:
                dn = display_name(s.symbol)
                if s.direction != TrendDirection.FLAT:
                    parts.append(
                        f"{dn} ретейл {s.retail_long_pct:.0f}%L"
                        f" → контрариан {s.direction.value.upper()}"
                    )
            if parts:
                log.info("  Sentiment: %s", ", ".join(parts))
            else:
                log.info("  Sentiment: нет сильных расхождений (порог %d%%)", 70)
        else:
            log.info("  Sentiment: Myfxbook не настроен")

    def log_whale_stats(self) -> None:
        """Вывести статистику по whale-источникам."""
        by_source = self._store.verification_summary_by_source()
        if not by_source:
            return

        lot = self._settings.lot_size
        balance = self._settings.account_balance

        log.info("── Статистика по источникам ──")
        for row in by_source:
            src = str(row["source"])
            total = int(row["total"])
            wr = float(row["win_rate"]) * 100
            tp = float(row["total_profit"])

            from fx_pro_bot.config.settings import pip_value_usd
            avg_pv = pip_value_usd("EURUSD=X", lot)
            net_usd = tp * avg_pv

            label = {
                "ensemble": "Ансамбль (наш)",
                "whale_cot": "COT (киты)",
                "whale_sentiment": "Sentiment (контрариан)",
            }.get(src, src)

            log.info(
                "  %s: %d проверок, win-rate %.0f%%, %+.1f пунктов, ~$%+.2f",
                label, total, wr, tp, net_usd,
            )

        log.info(
            "  (Счёт $%.0f, лот %.2f — расчёт по среднему pip-value)",
            balance, lot,
        )
