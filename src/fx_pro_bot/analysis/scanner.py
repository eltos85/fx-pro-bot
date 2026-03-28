"""Сканер: перебирает список инструментов, для каждого считает сигнал, возвращает отсортированные по силе."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fx_pro_bot.analysis.signals import Signal, TrendDirection, simple_ma_crossover
from fx_pro_bot.config.settings import display_name
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.market_data.yfinance_feed import bars_from_yfinance

log = logging.getLogger(__name__)


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

        if len(bars) < slow + 1:
            log.debug("%s: мало баров (%d), нужно %d+", symbol, len(bars), slow + 1)
            continue

        signal = simple_ma_crossover(bars, fast=fast, slow=slow)
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
    """Только ненейтральные (LONG / SHORT) результаты."""
    return [r for r in scan if r.signal.direction != TrendDirection.FLAT]
