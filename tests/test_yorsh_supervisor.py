"""Юнит-тесты yorsh_bot M3: SubscriptionSupervisor (rebuild батчей на ротации).

Без сети. Mock-client-factory создаёт fake-клиенты с блокирующим ``run()``
(живут до cancel) и ``request_reinit``.
"""
from __future__ import annotations

import asyncio

import pytest

from yorsh_bot.config.settings import YorshSettings
from yorsh_bot.data.universe import TickerRow, UniverseManager
from yorsh_bot.data.supervisor import SubscriptionSupervisor


class FakeClient:
    """Блокирует run() до cancel; фиксирует создание и reinit-запросы."""
    instances: list["FakeClient"] = []

    def __init__(self, exchange: str, syms: list[str]) -> None:
        self.exchange = exchange
        self.syms = list(syms)
        self.reinits: list[str] = []
        self.started = False
        self.cancelled = False
        FakeClient.instances.append(self)

    async def run(self) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()   # блокирует до cancel
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    def request_reinit(self, symbol: str) -> None:
        self.reinits.append(symbol)


def _row(sym, vol=500_000):
    return TickerRow(sym, sym[:-4], "USDT", vol)


def _make(fetch_rows, settings=None):
    settings = settings or YorshSettings(
        exchanges="mexc", min_24h_volume_usd=10_000,
        max_24h_volume_usd=2_000_000, universe_refresh_hours=6)

    async def fetcher(exch):
        return fetch_rows(exch)

    mgr = UniverseManager(settings, fetcher=fetcher)
    FakeClient.instances.clear()
    factory = lambda exch, syms: FakeClient(exch, syms)
    sup = SubscriptionSupervisor(mgr, factory)
    return sup


def test_supervisor_starts_one_batch_under_limit():
    rows = [_row(f"S{i:02d}USDT") for i in range(5)]   # < 15 → 1 батч
    sup = _make(lambda exch: rows)

    async def go():
        await sup.reconcile("mexc")
        await asyncio.sleep(0.05)   # дать task'ам стартовать
        try:
            assert len(sup.tasks) == 1
            assert len(FakeClient.instances) == 1
            assert FakeClient.instances[0].started
            assert FakeClient.instances[0].syms == [r.symbol for r in rows]
        finally:
            await sup.stop_all()
    asyncio.run(go())


def test_supervisor_splits_into_batches():
    rows = [_row(f"S{i:02d}USDT") for i in range(20)]   # MEXC: 15 → 2 батча
    sup = _make(lambda exch: rows)

    async def go():
        await sup.reconcile("mexc")
        await asyncio.sleep(0.05)
        try:
            assert len(sup.tasks) == 2
            assert [len(c.syms) for c in FakeClient.instances] == [15, 5]
        finally:
            await sup.stop_all()
    asyncio.run(go())


def test_supervisor_rebuilds_changed_batch_on_rotation():
    """Ротация: 1 символ ушёл → батч 0 меняется → client отменён + новый."""
    r1 = [_row("AUSDT"), _row("BUSDT"), _row("CUSDT")]
    r2 = [_row("AUSDT"), _row("CUSDT")]   # BUSDT пропал
    state = {"i": 0}

    def fetch_rows(exch):
        out = r1 if state["i"] == 0 else r2
        state["i"] += 1
        return out

    sup = _make(fetch_rows)

    async def go():
        await sup.reconcile("mexc")
        await asyncio.sleep(0.05)
        first = FakeClient.instances[0]
        assert first.syms == ["AUSDT", "BUSDT", "CUSDT"]
        await sup.reconcile("mexc")
        await asyncio.sleep(0.05)
        try:
            # первый клиент отменён, поднят новый с обновлённым набором
            assert first.cancelled
            new = FakeClient.instances[-1]
            assert new.syms == ["AUSDT", "CUSDT"]
            assert len(sup.tasks) == 1
        finally:
            await sup.stop_all()
    asyncio.run(go())


def test_supervisor_keeps_unchanged_batch():
    """Батч не изменился → клиент НЕ пересоздаётся."""
    rows = [_row("AUSDT"), _row("BUSDT")]
    sup = _make(lambda exch: rows)

    async def go():
        await sup.reconcile("mexc")
        await asyncio.sleep(0.05)
        first = FakeClient.instances[0]
        await sup.reconcile("mexc")   # те же символы
        await asyncio.sleep(0.05)
        try:
            assert not first.cancelled
            assert len(FakeClient.instances) == 1   # новых нет
        finally:
            await sup.stop_all()
    asyncio.run(go())


def test_supervisor_stops_removed_batches():
    """Символов стало меньше — лишний батч-индекс отменяется."""
    r1 = [_row(f"S{i:02d}USDT") for i in range(20)]   # 2 батча
    r2 = [_row(f"S{i:02d}USDT") for i in range(5)]    # 1 батч
    state = {"i": 0}

    def fetch_rows(exch):
        out = r1 if state["i"] == 0 else r2
        state["i"] += 1
        return out

    sup = _make(fetch_rows)

    async def go():
        await sup.reconcile("mexc")
        await asyncio.sleep(0.05)
        assert len(sup.tasks) == 2
        await sup.reconcile("mexc")
        await asyncio.sleep(0.05)
        try:
            assert len(sup.tasks) == 1
        finally:
            await sup.stop_all()
    asyncio.run(go())
