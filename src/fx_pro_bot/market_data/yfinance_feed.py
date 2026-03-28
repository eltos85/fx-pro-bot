"""Загрузка истории через yfinance (публичные данные Yahoo, без API-ключа)."""

from __future__ import annotations

from datetime import UTC

from fx_pro_bot.market_data.models import Bar, InstrumentId


def bars_from_yfinance(
    yahoo_symbol: str,
    *,
    period: str = "1mo",
    interval: str = "1h",
) -> list[Bar]:
    import yfinance as yf  # noqa: PLC0415 — опциональная зависимость

    ticker = yf.Ticker(yahoo_symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        return []

    instrument = InstrumentId(symbol=yahoo_symbol)
    out: list[Bar] = []
    for ts, row in df.iterrows():
        ts_dt = ts.to_pydatetime()
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=UTC)
        else:
            ts_dt = ts_dt.astimezone(UTC)
        o = float(row["Open"])
        h = float(row["High"])
        low = float(row["Low"])
        c = float(row["Close"])
        raw_v = row.get("Volume", 0) or 0
        try:
            v = float(raw_v)
        except (TypeError, ValueError):
            v = 0.0
        if v != v:  # NaN
            v = 0.0
        out.append(
            Bar(
                instrument=instrument,
                ts=ts_dt,
                open=o,
                high=h,
                low=low,
                close=c,
                volume=v,
            )
        )
    return out
