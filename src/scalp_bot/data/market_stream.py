"""Bybit public WebSocket → SymbolState (scalp_bot).

Подписки (api-docs.mdc — официальная дока Bybit v5):
- ``publicTrade.{symbol}``     — тиковые сделки (для CVD/дельты).
- ``orderbook.50.{symbol}``    — L2-стакан snapshot/delta (для imbalance).
  https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook
- ``tickers.{symbol}``         — funding/OI/markPrice (delta merge).
- ``allLiquidation.{symbol}``  — все ликвидации, push 500ms.
  https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation

Public market-data одинаковы для demo и live; demo-сабдомен только для
private (как в ai_trader.price_stream). Поэтому testnet/ mainnet выбор —
через ``testnet`` (mainnet по умолчанию для полной ликвидности данных).

L2-стакан поддерживается локально: snapshot заменяет книгу, delta мёржит
(size "0" = удалить уровень) — как требует Bybit orderbook-протокол.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from scalp_bot.data.aggregates import SymbolState

log = logging.getLogger("scalp_bot.stream")


class BybitMarketStream:
    def __init__(
        self,
        symbols: list[str],
        states: dict[str, SymbolState],
        *,
        category: str = "linear",
        testnet: bool = False,
        ob_depth: int = 50,
        ws_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._symbols = list(symbols)
        self._states = states
        self._category = category
        self._testnet = testnet
        self._ob_depth = ob_depth
        self._ws_factory = ws_factory
        self._ws: Any = None
        # Локальные книги: symbol -> {"b": {price: size}, "a": {price: size}}
        self._books: dict[str, dict[str, dict[float, float]]] = {
            s: {"b": {}, "a": {}} for s in symbols
        }

    # ─── lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._symbols:
            log.warning("BybitMarketStream: нет символов")
            return
        try:
            if self._ws_factory is not None:
                self._ws = self._ws_factory()
            else:
                from pybit.unified_trading import WebSocket

                self._ws = WebSocket(
                    testnet=self._testnet,
                    channel_type=self._category,
                    ping_interval=20,
                    ping_timeout=10,
                    retries=10,
                    restart_on_error=True,
                    trace_logging=False,
                )
            self._ws.trade_stream(symbol=self._symbols, callback=self._on_trade)
            self._ws.orderbook_stream(
                depth=self._ob_depth, symbol=self._symbols, callback=self._on_ob
            )
            self._ws.ticker_stream(symbol=self._symbols, callback=self._on_ticker)
            self._subscribe_liquidations()
            log.info(
                "BybitMarketStream: подписка trade+orderbook%d+ticker+liq на %s",
                self._ob_depth, ", ".join(self._symbols),
            )
        except Exception:
            log.exception("BybitMarketStream.start failed")
            self._ws = None

    def _subscribe_liquidations(self) -> None:
        """allLiquidation (push 500ms); fallback на deprecated liquidation."""
        try:
            self._ws.all_liquidation_stream(
                symbol=self._symbols, callback=self._on_liq
            )
        except AttributeError:
            log.warning("all_liquidation_stream нет в pybit — fallback liquidation_stream")
            try:
                self._ws.liquidation_stream(
                    symbol=self._symbols, callback=self._on_liq
                )
            except Exception:
                log.exception("liquidation subscribe failed (ликвидации отключены)")

    def stop(self) -> None:
        if self._ws is None:
            return
        try:
            self._ws.exit()
        except Exception:
            log.exception("BybitMarketStream.stop failed")
        finally:
            self._ws = None

    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            return bool(self._ws.is_connected())
        except Exception:
            return False

    # ─── callbacks ───────────────────────────────────────────────────────

    def _on_trade(self, msg: dict) -> None:
        try:
            for row in msg.get("data", []) or []:
                sym = row.get("s")
                st = self._states.get(sym)
                if st is None:
                    continue
                price = _f(row.get("p"))
                size = _f(row.get("v"))
                side = row.get("S") or ""
                if price is not None and size is not None and side:
                    st.on_trade(price, size, side)
        except Exception:
            log.exception("_on_trade parse failed")

    def _on_ob(self, msg: dict) -> None:
        try:
            data = msg.get("data") or {}
            sym = data.get("s")
            st = self._states.get(sym)
            book = self._books.get(sym)
            if st is None or book is None:
                return
            mtype = msg.get("type")
            if mtype == "snapshot":
                book["b"] = {}
                book["a"] = {}
            self._apply_levels(book["b"], data.get("b", []))
            self._apply_levels(book["a"], data.get("a", []))
            bids = sorted(book["b"].items(), key=lambda x: -x[0])
            asks = sorted(book["a"].items(), key=lambda x: x[0])
            st.on_orderbook(bids, asks)
        except Exception:
            log.exception("_on_ob parse failed")

    @staticmethod
    def _apply_levels(side_map: dict[float, float], updates: list) -> None:
        for lvl in updates or []:
            try:
                price = float(lvl[0])
                size = float(lvl[1])
            except (ValueError, IndexError, TypeError):
                continue
            if size == 0.0:
                side_map.pop(price, None)
            else:
                side_map[price] = size

    def _on_ticker(self, msg: dict) -> None:
        try:
            data = msg.get("data") or {}
            sym = data.get("symbol")
            st = self._states.get(sym)
            if st is None:
                return
            st.on_ticker(
                funding_rate=_f(data.get("fundingRate")),
                open_interest=_f(data.get("openInterest")),
                mark_price=_f(data.get("markPrice")),
            )
        except Exception:
            log.exception("_on_ticker parse failed")

    def _on_liq(self, msg: dict) -> None:
        try:
            payload = msg.get("data")
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sym = row.get("s")
                st = self._states.get(sym)
                if st is None:
                    continue
                side = row.get("S") or ""
                size = _f(row.get("v"))
                price = _f(row.get("p"))
                if side and size is not None and price is not None:
                    st.on_liquidation(side, size, price)
        except Exception:
            log.exception("_on_liq parse failed")


def _f(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
