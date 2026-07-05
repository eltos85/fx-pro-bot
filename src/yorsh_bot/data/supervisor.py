"""Менеджер подписок поверх UniverseManager (milestone M3).

Связывает вселенную (target-сеты по батчам) с коллекторами: по одному
клиенту-задаче на батч (соединение) на биржу. На ротации вселенной —
перестраивает изменившиеся батчи (cancel + новый клиент с обновлённым набором).
Rotation редкая (``universe_refresh_hours``), потеря момента данных при
ребилде батча допустима (reconnect коллектора всё равно случается ≤24ч).

Client-factory — инжектируемая (для тестов подменяется mock-клиентом с
``run()`` и ``request_reinit``). Live MEXC/Bitget клиенты — в app/main.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Protocol

from yorsh_bot.data.universe import UniverseManager

log = logging.getLogger("yorsh_bot.supervisor")


class CollectorClient(Protocol):
    """Минимальный интерфейс коллектора для supervisor'а."""
    async def run(self) -> None: ...
    def request_reinit(self, symbol: str) -> None: ...


# factory(exchange, symbols_batch) -> CollectorClient (с уже привязанными колбэками)
ClientFactory = Callable[[str, list[str]], CollectorClient]


class SubscriptionSupervisor:
    """Динамически управляет коллекторами по батчам вселенной.

    ``reconcile(exchange)`` —refresh'ит вселенную, строит батчи, стартует
    новые / останавливает пропавшие батч-клиенты. Идемпотентен.
    """

    def __init__(self, manager: UniverseManager, factory: ClientFactory) -> None:
        self.manager = manager
        self.factory = factory
        # tasks[(exchange, batch_idx)] = asyncio.Task
        self.tasks: dict[tuple[str, int], asyncio.Task] = {}

    async def reconcile(self, exchange: str) -> None:
        await self.manager.refresh(exchange)
        batches = self.manager.batches(exchange)
        # какой набор батчей нужен сейчас
        wanted: set[tuple[str, int]] = {(exchange, i) for i in range(len(batches))}
        # стартуем/заменяем (в детерминированном порядке — set-итерация нестабильна)
        for key in sorted(wanted):
            batch_idx = key[1]
            syms = batches[batch_idx]
            existing = self.tasks.get(key)
            if existing is not None and self._batch_matches(key, syms):
                continue   # батч не изменился — не трогаем
            if existing is not None:
                existing.cancel()
                await asyncio.gather(existing, return_exceptions=True)
            self._start_task(exchange, batch_idx, syms)
        # останавливаем пропавшие батчи (индексы сверх wanted)
        for key in list(self.tasks):
            if key[0] == exchange and key not in wanted:
                self.tasks[key].cancel()
                await asyncio.gather(self.tasks.pop(key), return_exceptions=True)

    def _start_task(self, exchange: str, batch_idx: int, syms: list[str]) -> None:
        if not syms:
            return
        client = self.factory(exchange, syms)
        task = asyncio.create_task(client.run(),
                                   name=f"{exchange}-b{batch_idx}")
        self.tasks[(exchange, batch_idx)] = task
        # храним набор символов батча для diff'а
        setattr(task, "_syms", tuple(syms))

    def _batch_matches(self, key: tuple[str, int], syms: list[str]) -> bool:
        task = self.tasks.get(key)
        if task is None:
            return False
        return tuple(syms) == getattr(task, "_syms", None)

    async def stop_all(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()

    async def run_loop(self, stop: Callable[[], bool] = lambda: False) -> None:
        """Периодический reconcile всех бирж (== universe_refresh_hours)."""
        interval = self.manager.settings.universe_refresh_hours * 3600
        while not stop():
            for exch in self.manager.settings.exchange_list:
                try:
                    await self.reconcile(exch)
                except Exception as e:  # noqa: BLE001
                    log.warning("supervisor reconcile %s failed: %s", exch, e)
            await asyncio.sleep(interval)
