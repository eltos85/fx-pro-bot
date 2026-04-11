"""Обёртка над pybit для работы с Bybit Unified Trading API v5."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pybit.unified_trading import HTTP

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PositionInfo:
    symbol: str
    side: str  # "Buy" | "Sell"
    size: str
    entry_price: float
    unrealised_pnl: float
    leverage: str
    position_idx: int  # 0=one-way, 1=buy-hedge, 2=sell-hedge


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: str
    symbol: str
    side: str
    qty: str
    success: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class AccountBalance:
    total_equity: float
    available_balance: float
    unrealised_pnl: float


class BybitClient:
    """Синхронный клиент Bybit через pybit."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        demo: bool = True,
        category: str = "linear",
    ) -> None:
        self._category = category
        self._demo = demo
        self._session = HTTP(
            api_key=api_key,
            api_secret=api_secret,
            demo=demo,
        )
        mode = "DEMO" if demo else "LIVE"
        log.info("BybitClient: подключение (%s), category=%s", mode, category)

    @property
    def is_demo(self) -> bool:
        return self._demo

    def get_balance(self) -> AccountBalance:
        """Получить баланс Unified Trading Account."""
        resp = self._session.get_wallet_balance(accountType="UNIFIED")
        coins = resp["result"]["list"][0]
        return AccountBalance(
            total_equity=float(coins.get("totalEquity", 0)),
            available_balance=float(coins.get("totalAvailableBalance", 0)),
            unrealised_pnl=float(coins.get("totalPerpUPL", 0)),
        )

    def get_positions(self) -> list[PositionInfo]:
        """Получить открытые позиции."""
        resp = self._session.get_positions(category=self._category, settleCoin="USDT")
        positions: list[PositionInfo] = []
        for p in resp["result"]["list"]:
            size = p.get("size", "0")
            if float(size) == 0:
                continue
            positions.append(PositionInfo(
                symbol=p["symbol"],
                side=p["side"],
                size=size,
                entry_price=float(p.get("avgPrice", 0)),
                unrealised_pnl=float(p.get("unrealisedPnl", 0)),
                leverage=p.get("leverage", "1"),
                position_idx=int(p.get("positionIdx", 0)),
            ))
        return positions

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        *,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        """Открыть рыночный ордер."""
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty,
            "timeInForce": "GTC",
        }
        if sl is not None:
            params["stopLoss"] = str(sl)
        if tp is not None:
            params["takeProfit"] = str(tp)

        try:
            resp = self._session.place_order(**params)
            ret_code = resp.get("retCode", -1)
            if ret_code == 0:
                order_id = resp["result"]["orderId"]
                log.info("Ордер %s %s %s qty=%s → orderId=%s", side, symbol, self._category, qty, order_id)
                return OrderResult(
                    order_id=order_id, symbol=symbol, side=side, qty=qty, success=True,
                )
            msg = resp.get("retMsg", "unknown error")
            log.error("Ошибка ордера %s %s: %s", side, symbol, msg)
            return OrderResult(order_id="", symbol=symbol, side=side, qty=qty, success=False, message=msg)
        except Exception as e:
            log.exception("Исключение при отправке ордера %s %s", side, symbol)
            return OrderResult(order_id="", symbol=symbol, side=side, qty=qty, success=False, message=str(e))

    def close_position(self, symbol: str, side: str, qty: str) -> OrderResult:
        """Закрыть позицию рыночным ордером (встречная сторона)."""
        close_side = "Sell" if side == "Buy" else "Buy"
        return self.place_order(symbol, close_side, qty)

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Установить плечо для символа."""
        try:
            self._session.set_leverage(
                category=self._category,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            log.info("Leverage %s → %dx", symbol, leverage)
            return True
        except Exception as e:
            if "leverage not modified" in str(e).lower() or "110043" in str(e):
                return True
            log.warning("Не удалось установить leverage %s: %s", symbol, e)
            return False

    def amend_sl_tp(
        self,
        symbol: str,
        *,
        sl: float | None = None,
        tp: float | None = None,
    ) -> bool:
        """Обновить SL/TP для позиции."""
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "positionIdx": 0,
        }
        if sl is not None:
            params["stopLoss"] = str(sl)
        if tp is not None:
            params["takeProfit"] = str(tp)
        try:
            resp = self._session.set_trading_stop(**params)
            return resp.get("retCode", -1) == 0
        except Exception as e:
            log.warning("Не удалось обновить SL/TP %s: %s", symbol, e)
            return False

    def get_tickers(self, symbol: str) -> dict:
        """Получить текущую цену."""
        resp = self._session.get_tickers(category=self._category, symbol=symbol)
        if resp["result"]["list"]:
            return resp["result"]["list"][0]
        return {}

    def close_all_positions(self) -> int:
        """Аварийное закрытие всех позиций."""
        positions = self.get_positions()
        closed = 0
        for p in positions:
            result = self.close_position(p.symbol, p.side, p.size)
            if result.success:
                closed += 1
        log.warning("Закрыто %d/%d позиций", closed, len(positions))
        return closed
