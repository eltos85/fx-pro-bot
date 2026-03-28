"""Заглушка брокера: синтетические бары для разработки без реального API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fx_pro_bot.broker.base import BrokerConnection
from fx_pro_bot.execution.orders import OrderIntent
from fx_pro_bot.market_data.models import Bar, InstrumentId, Tick


class FxProStubBroker(BrokerConnection):
    def __init__(self, seed_bars: int = 500) -> None:
        self._seed_bars = seed_bars
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def ensure_authenticated(self) -> None:
        if not self._connected:
            msg = "not connected"
            raise RuntimeError(msg)

    def subscribe_bars(
        self,
        instrument: InstrumentId,
        timeframe_sec: int,
    ) -> AsyncIterator[Bar]:
        async def _gen():
            await self.ensure_authenticated()
            price = 1.0850
            t0 = datetime.now(tz=UTC).replace(second=0, microsecond=0)
            for i in range(self._seed_bars):
                drift = 0.00002 * (i % 17 - 8)
                noise = 0.00001 * ((i * 7) % 11 - 5)
                o = price
                c = max(0.5, o + drift + noise)
                hi = max(o, c) + 0.00003
                lo = min(o, c) - 0.00003
                ts = t0 + timedelta(seconds=timeframe_sec * i)
                yield Bar(
                    instrument=instrument,
                    ts=ts,
                    open=o,
                    high=hi,
                    low=lo,
                    close=c,
                    volume=1.0,
                )
                price = c
                await asyncio.sleep(0)  # уступить циклу event loop

        return _gen()

    def subscribe_ticks(self, instrument: InstrumentId) -> AsyncIterator[Tick]:
        async def _gen():
            await self.ensure_authenticated()
            p = 1.0850
            t0 = datetime.now(tz=UTC)
            for i in range(50):
                p += 0.00001 * (i % 5 - 2)
                yield Tick(instrument=instrument, ts=t0 + timedelta(seconds=i), bid=p - 0.00002, ask=p + 0.00002)
                await asyncio.sleep(0)

        return _gen()

    async def place_order(self, intent: OrderIntent) -> str:
        await self.ensure_authenticated()
        return f"stub-{intent.instrument.symbol}-{intent.side}"
