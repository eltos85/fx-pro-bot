"""Синхронная генерация тестовых баров (без async и без брокера)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fx_pro_bot.market_data.models import Bar, InstrumentId


def generate_stub_bars(symbol: str, *, n: int = 200, timeframe_sec: int = 3600) -> list[Bar]:
    instrument = InstrumentId(symbol=symbol)
    price = 1.0850
    t0 = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    out: list[Bar] = []
    for i in range(n):
        drift = 0.00002 * (i % 17 - 8)
        noise = 0.00001 * ((i * 7) % 11 - 5)
        o = price
        c = max(0.5, o + drift + noise)
        hi = max(o, c) + 0.00003
        lo = min(o, c) - 0.00003
        ts = t0 + timedelta(seconds=timeframe_sec * i)
        out.append(
            Bar(
                instrument=instrument,
                ts=ts,
                open=o,
                high=hi,
                low=lo,
                close=c,
                volume=1.0,
            )
        )
        price = c
    return out
