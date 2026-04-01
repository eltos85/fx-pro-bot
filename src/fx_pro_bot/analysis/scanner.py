"""Сканер: перебирает список инструментов, для каждого запускает ансамбль стратегий."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fx_pro_bot.analysis.ensemble import STRATEGY_NAMES, ensemble_signal
from fx_pro_bot.analysis.signals import Signal, TrendDirection
from fx_pro_bot.config.settings import display_name
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.market_data.yfinance_feed import bars_from_yfinance

log = logging.getLogger(__name__)

MIN_STRENGTH = 0.7


@dataclass(frozen=True, slots=True)
class ScanResult:
    symbol: str
    display_name: str
    signal: Signal
    last_price: float
    bars: list[Bar]


def scan_instruments(
    symbols: tuple[str, ...],
    *,
    period: str = "5d",
    interval: str = "5m",
    fast: int = 10,
    slow: int = 30,
) -> list[ScanResult]:
    results: list[ScanResult] = []

    for symbol in symbols:
        try:
            bars = bars_from_yfinance(symbol, period=period, interval=interval)
        except Exception:
            log.warning("Не удалось загрузить %s, пропускаю", symbol)
            continue

        if len(bars) < 51:
            log.debug("%s: мало баров (%d), нужно 51+", symbol, len(bars))
            continue

        signal = ensemble_signal(bars, fast=fast, slow=slow)
        log.debug(
            "%s: %s (сила %.0f%%) reasons=%s",
            display_name(symbol), signal.direction.value.upper(),
            signal.strength * 100, ", ".join(signal.reasons),
        )
        results.append(
            ScanResult(
                symbol=symbol,
                display_name=display_name(symbol),
                signal=signal,
                last_price=bars[-1].close,
                bars=bars,
            )
        )

    results.sort(key=lambda r: r.signal.strength, reverse=True)
    return results


def active_signals(scan: list[ScanResult]) -> list[ScanResult]:
    """Только сигналы с согласием 3+ стратегий."""
    return [
        r for r in scan
        if r.signal.direction != TrendDirection.FLAT and r.signal.strength >= MIN_STRENGTH
    ]
