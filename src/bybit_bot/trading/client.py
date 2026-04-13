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


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    """Торговые правила инструмента с Bybit API."""
    symbol: str
    status: str
    min_order_qty: float
    qty_step: float
    tick_size: float
    min_notional: float
    max_leverage: float


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

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        price: str,
        *,
        sl: float | None = None,
        tp: float | None = None,
    ) -> OrderResult:
        """Лимитный PostOnly ордер (maker fee 0.02%)."""
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": qty,
            "price": price,
            "timeInForce": "PostOnly",
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
                log.info("Limit PostOnly %s %s qty=%s price=%s → orderId=%s",
                         side, symbol, qty, price, order_id)
                return OrderResult(
                    order_id=order_id, symbol=symbol, side=side, qty=qty, success=True,
                )
            msg = resp.get("retMsg", "unknown error")
            log.warning("Limit PostOnly %s %s отклонён: %s", side, symbol, msg)
            return OrderResult(order_id="", symbol=symbol, side=side, qty=qty, success=False, message=msg)
        except Exception as e:
            log.warning("Limit PostOnly %s %s exception: %s", side, symbol, e)
            return OrderResult(order_id="", symbol=symbol, side=side, qty=qty, success=False, message=str(e))

    def close_position(self, symbol: str, side: str, qty: str) -> OrderResult:
        """Закрыть позицию рыночным ордером с reduceOnly=True."""
        close_side = "Sell" if side == "Buy" else "Buy"
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": qty,
            "reduceOnly": True,
            "timeInForce": "GTC",
        }
        try:
            resp = self._session.place_order(**params)
            ret_code = resp.get("retCode", -1)
            if ret_code == 0:
                order_id = resp["result"]["orderId"]
                log.info("Закрытие %s %s qty=%s → orderId=%s", close_side, symbol, qty, order_id)
                return OrderResult(order_id=order_id, symbol=symbol, side=close_side, qty=qty, success=True)
            msg = resp.get("retMsg", "unknown error")
            log.error("Ошибка закрытия %s: %s", symbol, msg)
            return OrderResult(order_id="", symbol=symbol, side=close_side, qty=qty, success=False, message=msg)
        except Exception as e:
            log.exception("Исключение при закрытии %s", symbol)
            return OrderResult(order_id="", symbol=symbol, side=close_side, qty=qty, success=False, message=str(e))

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

    def set_trailing_stop(
        self,
        symbol: str,
        distance: float,
        active_price: float | None = None,
    ) -> bool:
        """Установить trailing stop через POST /v5/position/trading-stop."""
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "tpslMode": "Full",
            "positionIdx": 0,
            "trailingStop": str(round(distance, 8)),
        }
        if active_price is not None:
            params["activePrice"] = str(round(active_price, 8))
        try:
            resp = self._session.set_trading_stop(**params)
            ok = resp.get("retCode", -1) == 0
            if ok:
                log.info("Trailing stop %s: distance=%.4f", symbol, distance)
            else:
                log.warning("Trailing stop %s failed: %s", symbol, resp.get("retMsg", ""))
            return ok
        except Exception as e:
            if "not modified" in str(e).lower() or "34040" in str(e):
                return True
            log.warning("Trailing stop %s error: %s", symbol, e)
            return False

    def cancel_sl_tp(self, symbol: str) -> bool:
        """Отменить SL/TP для позиции (для Stat-Arb ног)."""
        return self.amend_sl_tp(symbol, sl=0.0, tp=0.0)

    def get_closed_pnl(
        self,
        symbol: str | None = None,
        limit: int = 50,
        start_time: int | None = None,
    ) -> list[dict]:
        """GET /v5/position/closed-pnl -- реализованный PnL закрытых позиций.

        Каждый элемент содержит: symbol, orderId, side, qty, closedPnl,
        avgEntryPrice, avgExitPrice, openFee, closeFee, updatedTime и др.
        """
        params: dict = {"category": self._category, "limit": limit}
        if symbol:
            params["symbol"] = symbol
        if start_time is not None:
            params["startTime"] = start_time
        try:
            resp = self._session.get_closed_pnl(**params)
            return resp.get("result", {}).get("list", [])
        except Exception as e:
            log.warning("get_closed_pnl error: %s", e)
            return []

    def fetch_realized_pnl(self, symbol: str, since_ms: int) -> dict | None:
        """Найти последнюю запись closed-pnl для символа после since_ms.

        Возвращает dict с ключами closedPnl, avgEntryPrice, avgExitPrice
        или None если не найдено.
        """
        records = self.get_closed_pnl(symbol=symbol, limit=5, start_time=since_ms)
        if records:
            return records[0]
        return None

    def get_instruments(self, symbols: tuple[str, ...] | list[str] | None = None) -> dict[str, InstrumentInfo]:
        """Загрузить торговые правила инструментов (minQty, qtyStep, tickSize, maxLeverage).

        Bybit API возвращает по 500 записей, пагинируем через cursor.
        Если symbols задан — фильтруем только запрошенные.
        """
        result: dict[str, InstrumentInfo] = {}
        cursor = ""
        wanted = set(symbols) if symbols else None

        while True:
            params: dict = {"category": self._category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor

            resp = self._session.get_instruments_info(**params)
            items = resp.get("result", {}).get("list", [])

            for item in items:
                sym = item["symbol"]
                status = item.get("status", "")
                if status != "Trading":
                    continue
                if wanted and sym not in wanted:
                    continue

                lot = item.get("lotSizeFilter", {})
                price_f = item.get("priceFilter", {})
                lev_f = item.get("leverageFilter", {})

                result[sym] = InstrumentInfo(
                    symbol=sym,
                    status=status,
                    min_order_qty=float(lot.get("minOrderQty", "0.001")),
                    qty_step=float(lot.get("qtyStep", "0.001")),
                    tick_size=float(price_f.get("tickSize", "0.01")),
                    min_notional=float(lot.get("minNotionalValue", "5")),
                    max_leverage=float(lev_f.get("maxLeverage", "1")),
                )

            next_cursor = resp.get("result", {}).get("nextPageCursor", "")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        log.info("Загружено %d инструментов с Bybit API", len(result))
        return result

    def get_kline(
        self,
        symbol: str,
        interval: str = "60",
        limit: int = 200,
    ) -> list[dict]:
        """GET /v5/market/kline — OHLCV свечи с Bybit.

        interval: "1","3","5","15","30","60","120","240","360","720","D","W","M"
        Возвращает list[dict] с ключами: startTime, open, high, low, close, volume.
        Bybit отдаёт от новых к старым — разворачиваем.
        """
        try:
            resp = self._session.get_kline(
                category=self._category,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            raw = resp.get("result", {}).get("list", [])
            return list(reversed(raw))
        except Exception as e:
            log.warning("get_kline %s error: %s", symbol, e)
            return []

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
