"""Загрузка рыночных данных через yfinance (без API-ключа)."""

from __future__ import annotations

import logging
from datetime import UTC

import yfinance as yf

from bybit_bot.config.settings import to_yfinance
from bybit_bot.market_data.models import Bar

log = logging.getLogger(__name__)


def fetch_bars(
    bybit_symbol: str,
    *,
    period: str = "5d",
    interval: str = "5m",
) -> list[Bar]:
    """Загрузить бары для Bybit-символа через yfinance."""
    yf_symbol = to_yfinance(bybit_symbol)
    ticker = yf.Ticker(yf_symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=False)

    if df is None or df.empty:
        log.warning("Нет данных для %s (yfinance: %s)", bybit_symbol, yf_symbol)
        return []

    bars: list[Bar] = []
    for ts, row in df.iterrows():
        ts_dt = ts.to_pydatetime()
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=UTC)
        else:
            ts_dt = ts_dt.astimezone(UTC)

        raw_v = row.get("Volume", 0) or 0
        try:
            v = float(raw_v)
        except (TypeError, ValueError):
            v = 0.0
        if v != v:  # NaN
            v = 0.0

        bars.append(
            Bar(
                symbol=bybit_symbol,
                ts=ts_dt,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=v,
            )
        )
    return bars
