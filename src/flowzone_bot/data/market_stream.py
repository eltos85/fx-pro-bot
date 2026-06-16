"""Bybit public WebSocket → SymbolState (flowzone_bot).

Подписки (api-docs.mdc — официальная дока Bybit v5):
- ``publicTrade.{symbol}``  — тиковые сделки (footprint: delta-by-price, big
  trades, absorption). https://bybit-exchange.github.io/docs/v5/websocket/public/trade
- ``orderbook.50.{symbol}`` — L2-стакан snapshot/delta (ob_imbalance — доп-фактор).
  https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook

Канону flowzone (STRATEGY_FLOWZONE.md) нужен исполненный поток и стакан; funding/
ликвидации не используются (в отличие от scalp_bot) — не подписываемся.

Public market-data одинаковы для demo и live; demo-сабдомен только для private.
Поэтому ``testnet`` по умолчанию False (mainnet — полная ликвидность данных).

L2-стакан поддерживается локально: snapshot заменяет книгу, delta мёржит
(size "0" = удалить уровень) — как требует Bybit orderbook-протокол.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from flowzone_bot.data.aggregates import SymbolState

log = logging.getLogger("flowzone_bot.stream")


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
            log.info("BybitMarketStream: подписка trade+orderbook%d на %s",
                     self._ob_depth, ", ".join(self._symbols))
        except Exception:
            log.exception("BybitMarketStream.start failed")
            self._ws = None

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


def _f(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
