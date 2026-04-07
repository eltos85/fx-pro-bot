"""Низкоуровневый клиент cTrader Open API (protobuf/TCP через Twisted).

Twisted reactor запускается в фоновом daemon-потоке.
Все публичные методы — синхронные и потокобезопасные.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

_reactor_started = False
_reactor_lock = threading.Lock()


def _ensure_reactor() -> None:
    """Запустить Twisted reactor в фоновом потоке (один раз на процесс)."""
    global _reactor_started
    with _reactor_lock:
        if _reactor_started:
            return
        from twisted.internet import reactor

        t = threading.Thread(
            target=lambda: reactor.run(installSignalHandlers=False),
            daemon=True,
            name="ctrader-reactor",
        )
        t.start()
        _reactor_started = True
        log.info("Twisted reactor запущен в фоновом потоке")


class CTraderClient:
    """Thread-safe обёртка над cTrader Open API.

    Использование:
        client = CTraderClient(client_id, client_secret, access_token, account_id)
        client.start()          # подключение + авторизация
        result = client.send_order(...)
        client.stop()
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        access_token: str,
        account_id: int,
        host_type: str = "demo",
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._account_id = account_id
        self._host_type = host_type

        self._client: Any = None
        self._connected = threading.Event()
        self._app_auth_done = threading.Event()
        self._account_auth_done = threading.Event()
        self._lock = threading.Lock()
        self._waiters: dict[int, list[tuple[threading.Event, list]]] = {}
        self._running = False

    @property
    def is_ready(self) -> bool:
        return self._connected.is_set() and self._account_auth_done.is_set()

    def start(self, timeout: float = 30) -> None:
        """Подключиться и авторизоваться. Блокирующий вызов."""
        from ctrader_open_api import Client, EndPoints, TcpProtocol
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAccountAuthReq,
            ProtoOAAccountAuthRes,
            ProtoOAApplicationAuthReq,
            ProtoOAApplicationAuthRes,
        )
        from twisted.internet import reactor

        _ensure_reactor()

        host = (
            EndPoints.PROTOBUF_LIVE_HOST
            if self._host_type == "live"
            else EndPoints.PROTOBUF_DEMO_HOST
        )

        self._connected.clear()
        self._app_auth_done.clear()
        self._account_auth_done.clear()

        def _setup():
            self._client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
            self._client.setConnectedCallback(self._on_connected)
            self._client.setDisconnectedCallback(self._on_disconnected)
            self._client.setMessageReceivedCallback(self._on_message)
            self._client.startService()

        reactor.callFromThread(_setup)

        if not self._connected.wait(timeout):
            raise TimeoutError("cTrader: таймаут подключения")
        log.info("cTrader: подключено к %s", host)

        app_auth = ProtoOAApplicationAuthReq()
        app_auth.clientId = self._client_id
        app_auth.clientSecret = self._client_secret
        self._send_and_wait(
            app_auth,
            ProtoOAApplicationAuthRes().payloadType,
            timeout=timeout,
        )
        log.info("cTrader: приложение авторизовано")

        acc_auth = ProtoOAAccountAuthReq()
        acc_auth.ctidTraderAccountId = self._account_id
        acc_auth.accessToken = self._access_token
        self._send_and_wait(
            acc_auth,
            ProtoOAAccountAuthRes().payloadType,
            timeout=timeout,
        )
        self._account_auth_done.set()
        self._running = True
        log.info("cTrader: аккаунт %d авторизован, готов к торговле", self._account_id)

    def stop(self) -> None:
        """Отключиться от cTrader."""
        self._running = False
        if self._client:
            from twisted.internet import reactor
            reactor.callFromThread(self._client.stopService)
        self._connected.clear()
        self._account_auth_done.clear()
        log.info("cTrader: отключено")

    def send_new_order(
        self,
        symbol_id: int,
        trade_side: str,
        volume: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        comment: str = "",
        label: str = "fx-pro-bot",
    ) -> Any:
        """Отправить рыночный ордер. Блокирует до исполнения."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAExecutionEvent,
            ProtoOANewOrderReq,
        )
        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
            ProtoOAOrderType,
            ProtoOATradeSide,
        )

        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = symbol_id
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = ProtoOATradeSide.BUY if trade_side.upper() == "BUY" else ProtoOATradeSide.SELL
        req.volume = volume
        if stop_loss is not None:
            req.stopLoss = stop_loss
        if take_profit is not None:
            req.takeProfit = take_profit
        if comment:
            req.comment = comment[:512]
        if label:
            req.label = label[:100]

        result = self._send_and_wait(
            req,
            ProtoOAExecutionEvent().payloadType,
            timeout=30,
        )

        if hasattr(result, "errorCode") and result.errorCode:
            raise RuntimeError(f"cTrader order rejected: {result.errorCode}")

        log.info("cTrader: ордер исполнен, positionId=%s", getattr(result, "position", None))
        return result

    def close_position(self, position_id: int, volume: int) -> Any:
        """Закрыть позицию. Блокирует до подтверждения."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAClosePositionReq,
            ProtoOAExecutionEvent,
        )

        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self._account_id
        req.positionId = position_id
        req.volume = volume

        result = self._send_and_wait(
            req,
            ProtoOAExecutionEvent().payloadType,
            timeout=30,
        )

        if hasattr(result, "errorCode") and result.errorCode:
            raise RuntimeError(f"cTrader close rejected: {result.errorCode}")

        log.info("cTrader: позиция %d закрыта", position_id)
        return result

    def get_symbols(self) -> list:
        """Получить список доступных символов."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOASymbolsListReq,
            ProtoOASymbolsListRes,
        )

        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self._account_id
        return self._send_and_wait(req, ProtoOASymbolsListRes().payloadType, timeout=30)

    def get_trader_info(self) -> Any:
        """Получить информацию о трейдере (баланс, equity и т.д.)."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOATraderReq,
            ProtoOATraderRes,
        )

        req = ProtoOATraderReq()
        req.ctidTraderAccountId = self._account_id
        return self._send_and_wait(req, ProtoOATraderRes().payloadType, timeout=30)

    def reconcile(self) -> Any:
        """Получить текущие открытые позиции и ожидающие ордера."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAReconcileReq,
            ProtoOAReconcileRes,
        )

        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self._account_id
        return self._send_and_wait(req, ProtoOAReconcileRes().payloadType, timeout=30)

    def amend_position_sl_tp(
        self,
        position_id: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> Any:
        """Изменить SL/TP существующей позиции."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAmendPositionSLTPReq,
            ProtoOAExecutionEvent,
        )

        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self._account_id
        req.positionId = position_id
        if stop_loss is not None:
            req.stopLoss = stop_loss
        if take_profit is not None:
            req.takeProfit = take_profit

        return self._send_and_wait(req, ProtoOAExecutionEvent().payloadType, timeout=30)

    # -- internal -----------------------------------------------------------------

    def _send_and_wait(self, message: Any, expected_type: int, timeout: float = 30) -> Any:
        """Отправить protobuf-сообщение и ждать ответа нужного типа."""
        if not self._connected.is_set():
            raise ConnectionError("cTrader: нет подключения")

        from twisted.internet import reactor

        event = threading.Event()
        result: list = [None, None]  # [response, error]

        with self._lock:
            self._waiters.setdefault(expected_type, []).append((event, result))

        def _do_send():
            try:
                d = self._client.send(message)
                d.addErrback(lambda f: _on_send_error(f))
            except Exception as exc:
                result[1] = exc
                event.set()

        def _on_send_error(failure):
            result[1] = RuntimeError(f"Send failed: {failure}")
            event.set()

        reactor.callFromThread(_do_send)

        if not event.wait(timeout=timeout):
            with self._lock:
                waiters = self._waiters.get(expected_type, [])
                if (event, result) in waiters:
                    waiters.remove((event, result))
            raise TimeoutError(f"cTrader: таймаут ожидания ответа (type={expected_type})")

        if result[1]:
            raise result[1]

        return result[0]

    def _on_connected(self, client: Any) -> None:
        log.debug("cTrader: TCP connected")
        self._connected.set()

    def _on_disconnected(self, client: Any, reason: Any) -> None:
        log.warning("cTrader: отключено — %s", reason)
        self._connected.clear()
        self._account_auth_done.clear()
        with self._lock:
            for waiters in self._waiters.values():
                for ev, res in waiters:
                    res[1] = ConnectionError(f"Disconnected: {reason}")
                    ev.set()
            self._waiters.clear()

    def _on_message(self, client: Any, message: Any) -> None:
        from ctrader_open_api import Protobuf
        from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAErrorRes

        if message.payloadType == ProtoHeartbeatEvent().payloadType:
            return

        extracted = Protobuf.extract(message)
        payload_type = message.payloadType

        error_type = ProtoOAErrorRes().payloadType
        if payload_type == error_type:
            err_code = getattr(extracted, "errorCode", "?")
            err_desc = getattr(extracted, "description", "")
            log.error("cTrader API error: %s — %s", err_code, err_desc)
            with self._lock:
                for waiters in self._waiters.values():
                    if waiters:
                        ev, res = waiters.pop(0)
                        res[1] = RuntimeError(f"cTrader error {err_code}: {err_desc}")
                        ev.set()
                        return
            return

        with self._lock:
            waiters = self._waiters.get(payload_type, [])
            if waiters:
                ev, res = waiters.pop(0)
                res[0] = extracted
                ev.set()
                return

        log.debug("cTrader msg (no waiter): type=%d", payload_type)
