"""Bybit-клиент для AI-Trader.

Обёртка над pybit.unified_trading.HTTP. БЕЗ импортов из bybit_bot —
изолированная экосистема (правило strategy-guard.mdc).

Минимальный набор операций для агентного цикла:
- get_klines: исторические свечи для market context
- get_positions: текущие открытые позиции
- get_wallet_balance: equity для расчёта margin
- get_tickers: последняя цена + funding rate
- place_order: открытие/закрытие с orderLinkId
- set_trading_stop: установка SL/TP на бирже
- set_leverage: установка плеча перед открытием
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pybit.unified_trading import HTTP

log = logging.getLogger(__name__)


@dataclass
class Bar:
    ts: int  # unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Position:
    symbol: str
    side: str  # "Buy" / "Sell" / "None"
    size: float
    entry_price: float
    leverage: float
    unrealised_pnl: float
    position_value: float


@dataclass
class Ticker:
    symbol: str
    last_price: float
    bid: float
    ask: float
    funding_rate: float
    volume_24h: float
    price_change_pct_24h: float


class AiBybitClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        demo: bool = True,
        category: str = "linear",
    ) -> None:
        self._session = HTTP(
            api_key=api_key,
            api_secret=api_secret,
            demo=demo,
            recv_window=10000,
        )
        self._category = category

    # ─── Market data ─────────────────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str = "60", limit: int = 50) -> list[Bar]:
        """interval Bybit format: '1','3','5','15','30','60','120','240','D','W'."""
        try:
            resp = self._session.get_kline(
                category=self._category,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
        except Exception:
            log.exception("get_klines %s %s failed", symbol, interval)
            return []
        items = resp.get("result", {}).get("list", []) or []
        bars: list[Bar] = []
        for row in items:
            try:
                bars.append(
                    Bar(
                        ts=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
            except (ValueError, IndexError, TypeError):
                continue
        bars.sort(key=lambda b: b.ts)
        return bars

    def get_ticker(self, symbol: str) -> Ticker | None:
        try:
            resp = self._session.get_tickers(category=self._category, symbol=symbol)
        except Exception:
            log.exception("get_ticker %s failed", symbol)
            return None
        items = resp.get("result", {}).get("list", []) or []
        if not items:
            return None
        t = items[0]
        try:
            return Ticker(
                symbol=t.get("symbol", symbol),
                last_price=float(t.get("lastPrice", 0) or 0),
                bid=float(t.get("bid1Price", 0) or 0),
                ask=float(t.get("ask1Price", 0) or 0),
                funding_rate=float(t.get("fundingRate", 0) or 0),
                volume_24h=float(t.get("volume24h", 0) or 0),
                price_change_pct_24h=float(t.get("price24hPcnt", 0) or 0) * 100,
            )
        except (ValueError, TypeError):
            log.exception("ticker parse failed %s: %s", symbol, t)
            return None

    # ─── Account / positions ─────────────────────────────────────────────

    def get_wallet_balance(self) -> float:
        """Доступный equity в USDT."""
        try:
            resp = self._session.get_wallet_balance(accountType="UNIFIED")
        except Exception:
            log.exception("get_wallet_balance failed")
            return 0.0
        accounts = resp.get("result", {}).get("list", []) or []
        for acc in accounts:
            for coin in acc.get("coin", []) or []:
                if coin.get("coin") == "USDT":
                    try:
                        return float(coin.get("equity", 0) or 0)
                    except (ValueError, TypeError):
                        return 0.0
        return 0.0

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        try:
            params: dict = {"category": self._category, "settleCoin": "USDT"}
            if symbol:
                params["symbol"] = symbol
            resp = self._session.get_positions(**params)
        except Exception:
            log.exception("get_positions failed")
            return []
        items = resp.get("result", {}).get("list", []) or []
        out: list[Position] = []
        for p in items:
            try:
                size = float(p.get("size", 0) or 0)
                if size <= 0:
                    continue
                out.append(
                    Position(
                        symbol=p.get("symbol", ""),
                        side=p.get("side", ""),
                        size=size,
                        entry_price=float(p.get("avgPrice", 0) or 0),
                        leverage=float(p.get("leverage", 1) or 1),
                        unrealised_pnl=float(p.get("unrealisedPnl", 0) or 0),
                        position_value=float(p.get("positionValue", 0) or 0),
                    )
                )
            except (ValueError, TypeError):
                continue
        return out

    # ─── Orders / leverage / SL-TP ───────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self._session.set_leverage(
                category=self._category,
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
            return True
        except Exception as e:
            msg = str(e)
            if "leverage not modified" in msg.lower() or "110043" in msg:
                return True  # уже стоит то же значение, не ошибка
            log.warning("set_leverage %s %dx failed: %s", symbol, leverage, e)
            return False

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_link_id: str,
        sl_price: float | None = None,
        tp_price: float | None = None,
        reduce_only: bool = False,
    ) -> dict:
        """Market-ордер с опциональными SL/TP.

        side: 'Buy' / 'Sell'
        order_link_id: должен начинаться с 'ai_' для нашей идентификации

        Возвращает dict:
        - При успехе: {"ok": True, "result": <bybit result>}
        - При ошибке: {"ok": False, "error": <message>, "params": <params>}

        AUDIT_2026.md P0 fix: ранее возвращался ``None``, что прятало
        реальную причину отказа Bybit (min order size / leverage / margin)
        и попадало в БД как generic «place_order returned None».
        """
        params: dict = {
            "category": self._category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "orderLinkId": order_link_id,
            "reduceOnly": reduce_only,
        }
        if sl_price is not None and not reduce_only:
            params["stopLoss"] = str(sl_price)
        if tp_price is not None and not reduce_only:
            params["takeProfit"] = str(tp_price)
        try:
            resp = self._session.place_order(**params)
        except Exception as e:
            log.exception("place_order exception: %s", params)
            return {"ok": False, "error": f"exception: {e}", "params": params}
        ret_code = resp.get("retCode")
        ret_msg = resp.get("retMsg", "")
        if ret_code not in (0, None):
            log.warning(
                "place_order non-zero retCode: code=%s msg=%s params=%s",
                ret_code,
                ret_msg,
                params,
            )
            return {
                "ok": False,
                "error": f"bybit retCode={ret_code} msg={ret_msg}",
                "params": params,
                "raw": resp,
            }
        return {"ok": True, "result": resp.get("result"), "raw": resp}

    def close_position(self, symbol: str, side: str, qty: float, link_id: str) -> dict:
        """Закрыть позицию reduce-only ордером с противоположной стороной.

        Возвращает то же что place_order: {"ok": True/False, ...}.
        """
        opposite = "Sell" if side == "Buy" else "Buy"
        return self.place_order(
            symbol=symbol,
            side=opposite,
            qty=qty,
            order_link_id=link_id,
            reduce_only=True,
        )
