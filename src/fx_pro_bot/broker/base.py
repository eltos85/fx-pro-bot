from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fx_pro_bot.execution.orders import OrderIntent
    from fx_pro_bot.market_data.models import Bar, InstrumentId, Tick


class BrokerConnection(ABC):
    """Адаптер брокера: авторизация, поток данных, ордера."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def ensure_authenticated(self) -> None:
        """Токен/сессия валидны; при необходимости обновить."""

    @abstractmethod
    def subscribe_bars(
        self,
        instrument: InstrumentId,
        timeframe_sec: int,
    ) -> AsyncIterator[Bar]:
        ...

    @abstractmethod
    def subscribe_ticks(self, instrument: InstrumentId) -> AsyncIterator[Tick]:
        ...

    @abstractmethod
    async def place_order(self, intent: OrderIntent) -> str:
        """Вернуть client_order_id или id ордера у брокера."""
