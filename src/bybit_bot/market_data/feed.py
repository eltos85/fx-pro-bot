"""Загрузка рыночных данных через yfinance (без API-ключа).

yfinance.download() поддерживает batch-загрузку нескольких тикеров
одним HTTP-запросом (многопоток). Лимит Yahoo: ~60 req/min.
Документация: https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html
"""

from __future__ import annotations

import logging
from datetime import UTC

import yfinance as yf

from bybit_bot.config.settings import to_bybit, to_yfinance
from bybit_bot.market_data.models import Bar

log = logging.getLogger(__name__)


def fetch_bars(
    bybit_symbol: str,
    *,
    period: str = "5d",
    interval: str = "5m",
) -> list[Bar]:
    """Загрузить бары для одного Bybit-символа через yfinance."""
    yf_symbol = to_yfinance(bybit_symbol)
    ticker = yf.Ticker(yf_symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=False)

    if df is None or df.empty:
        log.warning("Нет данных для %s (yfinance: %s)", bybit_symbol, yf_symbol)
        return []

    return _df_to_bars(df, bybit_symbol)


def fetch_bars_batch(
    bybit_symbols: tuple[str, ...] | list[str],
    *,
    period: str = "5d",
    interval: str = "5m",
) -> dict[str, list[Bar]]:
    """Batch-загрузка баров для всех символов одним вызовом yfinance.download().

    Возвращает dict {bybit_symbol: [Bar, ...]}.
    Один HTTP-запрос вместо N отдельных — укладываемся в лимит Yahoo.
    """
    yf_symbols = [to_yfinance(s) for s in bybit_symbols]
    yf_to_bybit = {to_yfinance(s): s for s in bybit_symbols}

    try:
        df = yf.download(
            tickers=yf_symbols,
            period=period,
            interval=interval,
            auto_adjust=False,
            threads=True,
            group_by="ticker",
            progress=False,
            timeout=30,
        )
    except Exception:
        log.exception("yfinance batch download failed")
        return {}

    if df is None or df.empty:
        log.warning("yfinance вернул пустые данные для batch-запроса")
        return {}

    result: dict[str, list[Bar]] = {}

    if len(yf_symbols) == 1:
        yf_sym = yf_symbols[0]
        bybit_sym = yf_to_bybit[yf_sym]
        bars = _df_to_bars(df, bybit_sym)
        if bars:
            result[bybit_sym] = bars
        return result

    for yf_sym in yf_symbols:
        bybit_sym = yf_to_bybit.get(yf_sym)
        if not bybit_sym:
            continue
        try:
            ticker_df = df[yf_sym].dropna(how="all")
            if ticker_df.empty:
                log.debug("Нет данных для %s (%s)", bybit_sym, yf_sym)
                continue
            bars = _df_to_bars(ticker_df, bybit_sym)
            if bars:
                result[bybit_sym] = bars
        except (KeyError, TypeError):
            log.debug("Тикер %s не найден в batch-ответе", yf_sym)

    log.info("Batch: загружено %d/%d тикеров", len(result), len(bybit_symbols))
    return result


def _df_to_bars(df: object, bybit_symbol: str) -> list[Bar]:
    """Конвертировать DataFrame в список Bar."""
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
        if v != v:
            v = 0.0

        try:
            o = float(row["Open"])
            h = float(row["High"])
            lo = float(row["Low"])
            c = float(row["Close"])
        except (TypeError, ValueError, KeyError):
            continue

        if o != o or h != h or lo != lo or c != c:
            continue

        bars.append(Bar(
            symbol=bybit_symbol,
            ts=ts_dt,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=v,
        ))
    return bars
