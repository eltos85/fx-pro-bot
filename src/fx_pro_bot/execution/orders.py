from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from fx_pro_bot.market_data.models import InstrumentId


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Намерение отправить ордер (после слоя риска)."""

    client_order_id: str
    instrument: InstrumentId
    side: OrderSide
    quantity: float
    order_type: OrderType
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
