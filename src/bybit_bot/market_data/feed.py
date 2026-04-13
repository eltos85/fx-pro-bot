"""Загрузка рыночных данных напрямую с Bybit API (get_kline).

V2: убран yfinance — свечи берутся с биржи, нет задержек и расхождений.
Bybit API отдаёт до 200 свечей за запрос. Для 1h таймфрейма 200 свечей = 8 дней.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from bybit_bot.market_data.models import Bar
from bybit_bot.trading.client import BybitClient

log = logging.getLogger(__name__)


def fetch_bars_bybit(
    client: BybitClient,
    symbol: str,
    interval: str = "60",
    limit: int = 200,
) -> list[Bar]:
    """Загрузить бары для одного символа через Bybit API get_kline."""
    raw = client.get_kline(symbol, interval=interval, limit=limit)
    if not raw:
        log.warning("Нет данных kline для %s", symbol)
        return []
    return _raw_to_bars(raw, symbol)


def fetch_bars_batch_bybit(
    client: BybitClient,
    symbols: tuple[str, ...] | list[str],
    interval: str = "60",
    limit: int = 200,
) -> dict[str, list[Bar]]:
    """Загрузить бары для всех символов через Bybit API.

    Один HTTP-запрос на символ (Bybit не поддерживает batch kline).
    Для 5 символов = 5 запросов, ~2-3 сек.
    """
    result: dict[str, list[Bar]] = {}
    for sym in symbols:
        bars = fetch_bars_bybit(client, sym, interval=interval, limit=limit)
        if bars:
            result[sym] = bars
    log.info("Klines: загружено %d/%d символов (interval=%s)",
             len(result), len(symbols), interval)
    return result


def _raw_to_bars(raw: list, symbol: str) -> list[Bar]:
    """Конвертировать raw kline ответ Bybit в список Bar.

    Bybit kline format: [startTime, open, high, low, close, volume, turnover]
    """
    bars: list[Bar] = []
    for item in raw:
        try:
            ts_ms = int(item[0])
            ts_dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            bars.append(Bar(
                symbol=symbol,
                ts=ts_dt,
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
                volume=float(item[5]),
            ))
        except (IndexError, ValueError, TypeError):
            continue
    return bars
