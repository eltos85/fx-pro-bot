"""Низкоуровневый клиент cTrader Open API (protobuf/TCP через Twisted).

Twisted reactor запускается в фоновом daemon-потоке.
Все публичные методы — синхронные и потокобезопасные.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

_reactor_started = False
_reactor_lock = threading.Lock()

HEARTBEAT_INTERVAL_SEC = 10


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
        refresh_token: str = "",
        on_token_refreshed: Callable[[str, str], None] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._account_id = account_id
        self._host_type = host_type
        self._refresh_token = refresh_token
        self._on_token_refreshed = on_token_refreshed

        self._client: Any = None
        self._connected = threading.Event()
        self._app_auth_done = threading.Event()
        self._account_auth_done = threading.Event()
        self._lock = threading.Lock()
        self._waiters: dict[int, list[tuple[threading.Event, list]]] = {}
        self._running = False
        self._reconnecting = False
        self._reconnect_attempt = 0
        self._heartbeat_loop: Any = None

    @property
    def is_ready(self) -> bool:
        return self._connected.is_set() and self._account_auth_done.is_set()

    def start(self, timeout: float = 30, retries: int = 5) -> None:
        """Подключиться и авторизоваться с retry. Блокирующий вызов."""
        import time as _time

        _ensure_reactor()

        self._running = True
        self._reconnect_attempt = 0

        delays = [5, 10, 20, 30, 60]
        for attempt in range(retries):
            self._reconnecting = True
            try:
                self._connect_and_auth(timeout)
                self._reconnecting = False
                return
            except Exception as exc:
                delay = delays[min(attempt, len(delays) - 1)]
                log.error(
                    "cTrader: не удалось подключиться (%s), retry #%d через %ds",
                    exc, attempt + 1, delay,
                )
                self._cleanup_client()
                _time.sleep(delay)

        self._reconnecting = False
        log.error("cTrader: все %d попыток исчерпаны, запускаем reconnect в фоне", retries)
        self._schedule_reconnect()

    def _connect_and_auth(self, timeout: float = 30) -> None:
        """Создать свежий Client, подключиться и авторизоваться."""
        import time as _time

        from ctrader_open_api import Client, EndPoints, TcpProtocol
        from twisted.internet import reactor

        host = (
            EndPoints.PROTOBUF_LIVE_HOST
            if self._host_type == "live"
            else EndPoints.PROTOBUF_DEMO_HOST
        )

        self._connected.clear()
        self._app_auth_done.clear()
        self._account_auth_done.clear()

        with self._lock:
            self._waiters.clear()

        ready = threading.Event()

        def _setup():
            self._client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
            self._client.setConnectedCallback(self._on_connected)
            self._client.setDisconnectedCallback(self._on_disconnected)
            self._client.setMessageReceivedCallback(self._on_message)
            self._client.startService()
            ready.set()

        reactor.callFromThread(_setup)
        ready.wait(10)

        if not self._connected.wait(timeout):
            raise TimeoutError("cTrader: таймаут TCP подключения")
        log.info("cTrader: подключено к %s", host)

        _time.sleep(3)

        self._do_auth(timeout)

    def _cleanup_client(self) -> None:
        """Остановить текущего клиента (best effort)."""
        self._stop_heartbeat()
        self._connected.clear()
        self._account_auth_done.clear()
        with self._lock:
            for waiters in self._waiters.values():
                for ev, res in waiters:
                    res[1] = ConnectionError("cleanup")
                    ev.set()
            self._waiters.clear()
        if self._client:
            try:
                from twisted.internet import reactor
                reactor.callFromThread(self._client.stopService)
            except Exception:
                pass

    def _do_auth(self, timeout: float = 30) -> None:
        """Авторизация приложения и аккаунта (вызывается из start и reconnect)."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAccountAuthReq,
            ProtoOAAccountAuthRes,
            ProtoOAApplicationAuthReq,
            ProtoOAApplicationAuthRes,
            ProtoOAGetAccountListByAccessTokenReq,
            ProtoOAGetAccountListByAccessTokenRes,
        )

        app_auth = ProtoOAApplicationAuthReq()
        app_auth.clientId = self._client_id
        app_auth.clientSecret = self._client_secret
        self._send_and_wait(
            app_auth,
            ProtoOAApplicationAuthRes().payloadType,
            timeout=timeout,
        )
        log.info("cTrader: приложение авторизовано")

        account_id = self._account_id
        acct_list_req = ProtoOAGetAccountListByAccessTokenReq()
        acct_list_req.accessToken = self._access_token
        acct_list_res = self._send_and_wait(
            acct_list_req,
            ProtoOAGetAccountListByAccessTokenRes().payloadType,
            timeout=timeout,
        )
        accounts = getattr(acct_list_res, "ctidTraderAccount", [])
        if accounts:
            is_live = self._host_type == "live"
            for acct in accounts:
                log.info(
                    "cTrader: найден аккаунт ctid=%d, isLive=%s, login=%s",
                    acct.ctidTraderAccountId, acct.isLive,
                    getattr(acct, "traderLogin", "?"),
                )
                if acct.isLive == is_live:
                    account_id = acct.ctidTraderAccountId
                    break
            else:
                account_id = accounts[0].ctidTraderAccountId
            if account_id != self._account_id:
                log.info(
                    "cTrader: используем ctidTraderAccountId=%d (настройка была %d)",
                    account_id, self._account_id,
                )
                self._account_id = account_id

        acc_auth = ProtoOAAccountAuthReq()
        acc_auth.ctidTraderAccountId = self._account_id
        acc_auth.accessToken = self._access_token
        self._send_and_wait(
            acc_auth,
            ProtoOAAccountAuthRes().payloadType,
            timeout=timeout,
        )
        self._account_auth_done.set()
        log.info("cTrader: аккаунт %d авторизован, готов к торговле", self._account_id)

    def stop(self) -> None:
        """Отключиться от cTrader."""
        self._running = False
        self._stop_heartbeat()
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
        relative_stop_loss: int | None = None,
        relative_take_profit: int | None = None,
        comment: str = "",
        label: str = "fx-pro-bot",
    ) -> Any:
        """Отправить рыночный ордер. Блокирует до исполнения.

        relative_stop_loss / relative_take_profit задаются в формате cTrader:
        price_delta * 100_000 (int64). cTrader сам рассчитает абсолютные
        SL/TP от реальной цены заливки.
        """
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
        if relative_stop_loss is not None and relative_stop_loss > 0:
            req.relativeStopLoss = relative_stop_loss
        if relative_take_profit is not None and relative_take_profit > 0:
            req.relativeTakeProfit = relative_take_profit
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

    def get_symbols(self) -> Any:
        """Получить список light-символов (symbolId + symbolName)."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOASymbolsListReq,
            ProtoOASymbolsListRes,
        )

        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self._account_id
        return self._send_and_wait(req, ProtoOASymbolsListRes().payloadType, timeout=30)

    def get_symbol_details(self, symbol_ids: list[int]) -> Any:
        """Получить полные данные символов (digits, minVolume, ...) через ProtoOASymbolByIdReq."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOASymbolByIdReq,
            ProtoOASymbolByIdRes,
        )

        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = self._account_id
        for sid in symbol_ids:
            req.symbolId.append(sid)
        return self._send_and_wait(req, ProtoOASymbolByIdRes().payloadType, timeout=30)

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

    def get_unrealized_pnl(self) -> Any:
        """P&L открытых позиций — рассчитанный бэкендом cTrader (точный)."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAGetPositionUnrealizedPnLReq,
            ProtoOAGetPositionUnrealizedPnLRes,
        )

        req = ProtoOAGetPositionUnrealizedPnLReq()
        req.ctidTraderAccountId = self._account_id
        return self._send_and_wait(req, ProtoOAGetPositionUnrealizedPnLRes().payloadType, timeout=30)

    def get_deal_list(self, from_ts: int, to_ts: int, max_rows: int = 1000) -> Any:
        """Список сделок (deals) за период. Содержит closePositionDetail.grossProfit."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOADealListReq,
            ProtoOADealListRes,
        )

        req = ProtoOADealListReq()
        req.ctidTraderAccountId = self._account_id
        req.fromTimestamp = from_ts
        req.toTimestamp = to_ts
        req.maxRows = max_rows
        return self._send_and_wait(req, ProtoOADealListRes().payloadType, timeout=30)

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
                d.addErrback(lambda f: log.debug("cTrader deferred errback: %s", f))
            except Exception as exc:
                result[1] = exc
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
        log.debug("cTrader: TCP connected (client=%s)", id(client))
        if client is not self._client:
            log.debug("cTrader: ignoring connect from stale client")
            return
        self._connected.set()
        self._start_heartbeat()

    def _on_disconnected(self, client: Any, reason: Any) -> None:
        if client is not self._client:
            log.debug("cTrader: ignoring disconnect from stale client")
            return
        log.warning("cTrader: отключено — %s", reason)
        self._stop_heartbeat()
        self._connected.clear()
        self._account_auth_done.clear()
        with self._lock:
            for waiters in self._waiters.values():
                for ev, res in waiters:
                    res[1] = ConnectionError(f"Disconnected: {reason}")
                    ev.set()
            self._waiters.clear()

        if self._running and not self._reconnecting:
            self._schedule_reconnect()

    # -- heartbeat ----------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Запустить периодическую отправку ProtoHeartbeatEvent (каждые 10 сек)."""
        from twisted.internet import reactor, task

        self._stop_heartbeat()
        loop = task.LoopingCall(self._send_heartbeat)
        self._heartbeat_loop = loop
        reactor.callFromThread(loop.start, HEARTBEAT_INTERVAL_SEC, now=False)
        log.debug("cTrader: heartbeat запущен (каждые %ds)", HEARTBEAT_INTERVAL_SEC)

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_loop and self._heartbeat_loop.running:
            try:
                from twisted.internet import reactor
                reactor.callFromThread(self._heartbeat_loop.stop)
            except Exception:
                pass
        self._heartbeat_loop = None

    def _send_heartbeat(self) -> None:
        """Отправить ProtoHeartbeatEvent. Вызывается из reactor-потока."""
        from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
            ProtoHeartbeatEvent,
        )

        if not self._client or not self._connected.is_set():
            return
        try:
            self._client.send(ProtoHeartbeatEvent())
        except Exception as exc:
            log.debug("cTrader: heartbeat send error: %s", exc)

    def _schedule_reconnect(self) -> None:
        """Переподключение с экспоненциальным backoff (5s → 10s → 30s → 60s, max 120s)."""
        if self._reconnecting:
            return
        self._reconnecting = True

        import time as _time

        def _do_reconnect():
            delays = [5, 10, 30, 60, 120]
            while self._running:
                attempt = self._reconnect_attempt
                delay = delays[min(attempt, len(delays) - 1)]
                log.info("cTrader: reconnect #%d через %ds...", attempt + 1, delay)
                _time.sleep(delay)

                if not self._running:
                    break

                self._cleanup_client()
                _time.sleep(2)

                try:
                    self._connect_and_auth(timeout=30)
                    self._reconnect_attempt = 0
                    self._reconnecting = False
                    log.info("cTrader: переподключение успешно")
                    return
                except Exception as exc:
                    log.error("cTrader: reconnect failed: %s", exc)
                    self._reconnect_attempt += 1

            self._reconnecting = False

        threading.Thread(target=_do_reconnect, daemon=True, name="ctrader-reconnect").start()

    def _on_message(self, client: Any, message: Any) -> None:
        if client is not self._client:
            return
        from ctrader_open_api import Protobuf
        from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
            ProtoErrorRes,
            ProtoHeartbeatEvent,
        )
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAccountDisconnectEvent,
            ProtoOAAccountsTokenInvalidatedEvent,
            ProtoOAErrorRes,
            ProtoOAOrderErrorEvent,
        )

        if message.payloadType == ProtoHeartbeatEvent().payloadType:
            return

        extracted = Protobuf.extract(message)
        payload_type = message.payloadType

        if payload_type == ProtoOAAccountDisconnectEvent().payloadType:
            acct = getattr(extracted, "ctidTraderAccountId", "?")
            log.warning("cTrader: AccountDisconnectEvent (account=%s) — переавторизуем сессию", acct)
            self._account_auth_done.clear()
            threading.Thread(
                target=self._handle_account_disconnect,
                daemon=True,
                name="ctrader-reauth",
            ).start()
            return

        if payload_type == ProtoOAAccountsTokenInvalidatedEvent().payloadType:
            reason = getattr(extracted, "reason", "unknown")
            log.warning("cTrader: TokenInvalidatedEvent — %s", reason)
            self._account_auth_done.clear()
            threading.Thread(
                target=self._handle_token_invalidated,
                daemon=True,
                name="ctrader-token-refresh",
            ).start()
            return

        error_types = (
            ProtoErrorRes().payloadType,
            ProtoOAErrorRes().payloadType,
            ProtoOAOrderErrorEvent().payloadType,
        )

        if payload_type in error_types:
            err_code = getattr(extracted, "errorCode", "?")
            err_desc = getattr(extracted, "description", "")
            log.error("cTrader error (type=%d): %s — %s", payload_type, err_code, err_desc)
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

        log.info("cTrader msg (no waiter): type=%d, %s", payload_type, type(extracted).__name__)

    # -- event handlers -----------------------------------------------------------

    def _handle_account_disconnect(self) -> None:
        """Переавторизация аккаунта после AccountDisconnectEvent (TCP остаётся)."""
        try:
            self._do_auth(timeout=30)
            log.info("cTrader: аккаунт переавторизован после AccountDisconnectEvent")
        except Exception as exc:
            log.error("cTrader: переавторизация не удалась (%s), запускаем reconnect", exc)
            if self._running:
                self._schedule_reconnect()

    def _handle_token_invalidated(self) -> None:
        """Обновить токен и переавторизовать аккаунт после TokenInvalidatedEvent."""
        if not self._refresh_token:
            log.error("cTrader: refresh_token отсутствует, reconnect невозможен через refresh")
            if self._running:
                self._schedule_reconnect()
            return

        try:
            from fx_pro_bot.trading.auth import refresh_access_token

            new_token = refresh_access_token(
                self._refresh_token, self._client_id, self._client_secret,
            )
            self._access_token = new_token.access_token
            self._refresh_token = new_token.refresh_token
            log.info("cTrader: access token обновлён через refresh_token")

            if self._on_token_refreshed:
                try:
                    self._on_token_refreshed(new_token.access_token, new_token.refresh_token)
                except Exception as cb_err:
                    log.warning("cTrader: on_token_refreshed callback error: %s", cb_err)

            self._do_auth(timeout=30)
            log.info("cTrader: аккаунт переавторизован после обновления токена")
        except Exception as exc:
            log.error("cTrader: refresh + reauth не удался (%s), полный reconnect", exc)
            if self._running:
                self._schedule_reconnect()
