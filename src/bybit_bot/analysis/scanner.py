"""Сканер: перебирает крипто-инструменты, для каждого запускает ансамбль."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.ensemble import ensemble_signal
from bybit_bot.analysis.signals import Direction, Signal
from bybit_bot.config.settings import display_name
from bybit_bot.market_data.feed import fetch_bars
from bybit_bot.market_data.models import Bar

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
    min_votes: int = 3,
    bars_map: dict[str, list[Bar]] | None = None,
) -> list[ScanResult]:
    results: list[ScanResult] = []

    for symbol in symbols:
        if bars_map and symbol in bars_map:
            bars = bars_map[symbol]
        else:
            try:
                bars = fetch_bars(symbol, period=period, interval=interval)
            except Exception:
                log.warning("Не удалось загрузить %s, пропускаю", symbol)
                continue

        if len(bars) < 51:
            log.debug("%s: мало баров (%d), нужно 51+", symbol, len(bars))
            continue

        signal = ensemble_signal(bars, min_votes=min_votes)
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


def active_signals(scan: list[ScanResult], min_strength: float = 0.6) -> list[ScanResult]:
    """Только сигналы с согласием 3+ стратегий."""
    return [
        r for r in scan
        if r.signal.direction != Direction.FLAT and r.signal.strength >= min_strength
    ]
