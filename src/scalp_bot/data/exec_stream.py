"""Bybit private WebSocket → realtime executions (scalp_bot).

Подписка ``execution`` (api-docs.mdc — официальная дока Bybit v5):
https://bybit-exchange.github.io/docs/v5/websocket/private/execution

Каждое исполнение несёт ТОЧНЫЕ значения — без REST-опроса и без оценок:
- ``execPnl``   — realized P&L закрывающего филла (= cashFlow в Transaction Log);
- ``execFee``   — фактическая комиссия именно этого филла;
- ``execPrice``/``execQty`` — реальная цена/объём филла (не mark price!);
- ``orderLinkId`` — наш тег ордера (вход ``scalp_{sym}_{ts}``,
  выход ``scalp_{reason}_{id}``) → точный матч к сделке без гонок по времени;
- ``closedSize`` — закрытый объём (>0 у закрывающего филла).

net сделки = Σ execPnl − Σ execFee по всем филлам позиции (= Bybit closedPnl,
проверено по офдоку close-pnl: closedPnl = Σ execPnl − openFee − closeFee).

Приватный demo-домен включается флагом ``demo`` (как в REST-клиенте). Колбэк
pybit исполняется в отдельном треде → складываем в потокобезопасную очередь,
главный цикл забирает ``drain()`` и атрибутирует к сделкам (в своём треде).
"""
from __future__ import annotations

import logging
import queue
from typing import Any, Callable

log = logging.getLogger("scalp_bot.exec_ws")

# Только реальные торговые филлы несут позиционный P&L. Funding/Settlement —
# отдельные кэшфлоу, в per-trade closedPnl Bybit их НЕ включает, поэтому
# в атрибуцию сделки не берём (иначе исказим net и комиссии).
_FILL_TYPES = {"Trade", "AdlTrade", "BustTrade"}


def _f(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class BybitExecStream:
    """Приватный поток исполнений → потокобезопасная очередь нормализованных
    словарей. Источник истины по P&L/комиссиям (REST не нужен)."""

    def __init__(self, api_key: str, api_secret: str, *, demo: bool = True,
                 testnet: bool = False,
                 ws_factory: Callable[..., Any] | None = None) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._demo = demo
        self._testnet = testnet
        self._ws_factory = ws_factory
        self._ws: Any = None
        self._q: "queue.Queue[dict]" = queue.Queue()

    def start(self) -> None:
        try:
            if self._ws_factory is not None:
                self._ws = self._ws_factory()
            else:
                from pybit.unified_trading import WebSocket

                self._ws = WebSocket(
                    channel_type="private",
                    api_key=self._api_key, api_secret=self._api_secret,
                    demo=self._demo, testnet=self._testnet,
                    ping_interval=20, ping_timeout=10, retries=10,
                    restart_on_error=True, trace_logging=False,
                )
            self._ws.execution_stream(callback=self._on_exec)
            log.info("BybitExecStream: подписка execution (demo=%s testnet=%s)",
                     self._demo, self._testnet)
        except Exception:
            log.exception("BybitExecStream.start failed")
            self._ws = None

    def _on_exec(self, msg: dict) -> None:
        try:
            for row in msg.get("data", []) or []:
                if (row.get("execType") or "Trade") not in _FILL_TYPES:
                    continue  # funding/settlement и пр. — не позиционный филл
                self._q.put({
                    "symbol": row.get("symbol", "") or "",
                    "orderLinkId": row.get("orderLinkId", "") or "",
                    "orderId": row.get("orderId", "") or "",
                    "side": row.get("side", "") or "",
                    "execFee": _f(row.get("execFee")),
                    "execPnl": _f(row.get("execPnl")),
                    "execPrice": _f(row.get("execPrice")),
                    "execQty": _f(row.get("execQty")),
                    "closedSize": _f(row.get("closedSize")),
                    "leavesQty": _f(row.get("leavesQty")),
                    "stopOrderType": row.get("stopOrderType", "") or "",
                    "execTime": _f(row.get("execTime")),
                })
        except Exception:
            log.exception("BybitExecStream._on_exec parse failed")

    def drain(self) -> list[dict]:
        """Забрать все накопленные исполнения (вызывать из главного треда)."""
        out: list[dict] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            return bool(self._ws.is_connected())
        except Exception:
            return False

    def stop(self) -> None:
        if self._ws is None:
            return
        try:
            self._ws.exit()
        except Exception:
            log.exception("BybitExecStream.stop failed")
        finally:
            self._ws = None
