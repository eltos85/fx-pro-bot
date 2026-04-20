"""Загрузка исторических M5-баров через cTrader Open API.

Используется как основной источник для FxPro-инструментов: бары надёжнее
и без дыр на открытии сессий, в отличие от yfinance (Yahoo).

Для символов, которых нет в каталоге cTrader (например, крипта через yfinance),
вызывается yfinance как fallback.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.market_data.yfinance_feed import bars_from_yfinance

if TYPE_CHECKING:
    from fx_pro_bot.trading.client import CTraderClient
    from fx_pro_bot.trading.symbols import SymbolCache

log = logging.getLogger(__name__)


PERIOD_TO_DAYS: dict[str, int] = {
    "1d": 1,
    "2d": 2,
    "5d": 5,
    "7d": 7,
    "1mo": 30,
    "3mo": 90,
    "6mo": 180,
    "1y": 365,
}

INTERVAL_TO_MINUTES: dict[str, int] = {
    "1m": 1,
    "2m": 2,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}

MIN_BARS_FOR_OK = 51


def _decode_trendbar(tb, digits: int, instrument: InstrumentId) -> Bar:
    """cTrader encodes trendbars as low + deltas. Восстанавливаем OHLC."""
    scale = 10 ** digits
    low_abs = tb.low
    low = low_abs / scale
    open_ = (low_abs + tb.deltaOpen) / scale
    high = (low_abs + tb.deltaHigh) / scale
    close = (low_abs + tb.deltaClose) / scale
    ts = datetime.fromtimestamp(tb.utcTimestampInMinutes * 60, UTC)
    volume = float(tb.volume)
    return Bar(
        instrument=instrument,
        ts=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def bars_from_ctrader(
    yahoo_symbol: str,
    *,
    client: CTraderClient,
    symbol_cache: SymbolCache,
    period: str = "5d",
    interval: str = "5m",
) -> list[Bar]:
    """Загрузить M5-бары через cTrader Open API.

    Возвращает пустой список если символ не найден в cTrader каталоге.
    Raise не делаем: вызывающий код сам решит, нужен ли fallback.
    """
    sym_info = symbol_cache.resolve_yfinance(yahoo_symbol)
    if sym_info is None:
        return []

    period_days = PERIOD_TO_DAYS.get(period, 5)
    interval_min = INTERVAL_TO_MINUTES.get(interval, 5)

    now_ms = int(time.time() * 1000)
    from_ms = now_ms - period_days * 24 * 60 * 60 * 1000

    raw_bars = client.get_trendbars(
        symbol_id=sym_info.symbol_id,
        period_minutes=interval_min,
        from_ts_ms=from_ms,
        to_ts_ms=now_ms,
    )

    instrument = InstrumentId(symbol=yahoo_symbol)
    bars = [_decode_trendbar(tb, sym_info.digits, instrument) for tb in raw_bars]
    return bars


def bars_with_fallback(
    yahoo_symbol: str,
    *,
    client: CTraderClient | None,
    symbol_cache: SymbolCache | None,
    period: str = "5d",
    interval: str = "5m",
) -> list[Bar]:
    """Попытаться cTrader; при недостатке данных — откатиться к yfinance.

    Если cTrader клиент не передан или символ не в каталоге — сразу yfinance.
    Если cTrader вернул < MIN_BARS_FOR_OK — тоже yfinance (лучше чем ничего).
    На любую ошибку cTrader — тоже yfinance (не останавливаем торговлю).
    """
    if client is None or symbol_cache is None or not symbol_cache.loaded:
        return bars_from_yfinance(yahoo_symbol, period=period, interval=interval)

    try:
        bars = bars_from_ctrader(
            yahoo_symbol,
            client=client,
            symbol_cache=symbol_cache,
            period=period,
            interval=interval,
        )
    except Exception as exc:
        log.warning(
            "cTrader bars %s failed (%s), fallback на yfinance",
            yahoo_symbol, exc,
        )
        return bars_from_yfinance(yahoo_symbol, period=period, interval=interval)

    if len(bars) >= MIN_BARS_FOR_OK:
        return bars

    log.warning(
        "cTrader вернул %d баров для %s (нужно ≥%d), fallback на yfinance",
        len(bars), yahoo_symbol, MIN_BARS_FOR_OK,
    )
    return bars_from_yfinance(yahoo_symbol, period=period, interval=interval)
