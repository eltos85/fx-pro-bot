"""Низкоуровневый клиент cTrader Open API (protobuf/TCP через Twisted).

Twisted reactor запускается в фоновом daemon-потоке.
Все публичные методы — синхронные и потокобезопасные.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

_reactor_started = False
_reactor_lock = threading.Lock()

# Heartbeat policy.
#
# Spotware (help.ctrader.com/open-api/connection/, .../faq/) требует
# отправлять ProtoHeartbeatEvent **не реже чем раз в 10 секунд**, иначе
# сервер закрывает TCP-сессию за inactivity. 10s — это hard cap, поэтому
# берём 8s как запас на jitter сети/планирования reactor-потока.
# `now=True` в LoopingCall — первый heartbeat сразу при connect,
# не на 10-й секунде (иначе серверная сторона может закрыть сессию
# до получения первого heartbeat при медленном app_auth).
HEARTBEAT_INTERVAL_SEC = 8

# Reconnect policy.
#
# cTrader Open API docs (help.ctrader.com/open-api/connection/, .../faq/):
# - Heartbeat ≤10s, иначе server disconnect by inactivity.
# - Rate limits: 50 req/s non-historical, 5 req/s historical, 25 concurrent
#   connections per app (client_id).
# - При throttle сервер шлёт REQUEST_FREQUENCY_EXCEEDED (или закрывает
#   TCP cleanly без ErrorRes если spam новых connections превысил лимит
#   на client_id).
#
# История багов:
# - 06-07.05.2026: 244 reconnects за 11ч с фиксированным delay=120s →
#   server-side throttle на client_id, каждое новое connection отвергается
#   cleanly сразу после handshake. Лечили: пауза 16 мин + расширенный backoff.
# - 11.05.2026: после 47ч idle (weekend, markets closed) серверная сторона
#   silent-rotated access token. App-auth работает (это OAuth client-cred),
#   но `ProtoOAGetAccountListByAccessTokenReq` → 30s timeout без ответа,
#   сервер закрывает TCP cleanly. См. community.ctrader.com/forum/.../45954.
#   Требовался proactive refresh token + smart-reset фикс.
#
# Решение:
# 1. Расширенный exponential backoff до 15 минут max.
# 2. STABLE_UPTIME_SEC + _account_auth_done: attempt-counter сбрасывается
#    ТОЛЬКО если предыдущее соединение прошло ПОЛНЫЙ auth-handshake И
#    прожило ≥5 минут. Если упало раньше или на этапе auth — это
#    server-side reject, продолжаем backoff.
# 3. При TimeoutError на GetAccountListByAccessTokenRes (type=2150) —
#    proactive token refresh, не уход в reconnect-loop.
RECONNECT_DELAYS_SEC: tuple[int, ...] = (5, 10, 30, 60, 120, 300, 900)
STABLE_UPTIME_SEC = 300

# Payload type для GetAccountListByAccessTokenRes (см. OpenApiMessages.proto).
# Используется для распознавания «silent token rotation» по timeout этого
# конкретного ответа — тогда триггерим refresh, а не общий reconnect.
_ACCOUNT_LIST_RES_TYPE = 2150


def _ensure_reactor() -> None:
    """Запустить Twisted reactor в фоновом потоке (один раз на процесс)."""
    global _reactor_started
    with _reactor_lock:
        if _reactor_started:
            return

        from twisted.python import log as twisted_log

        class _QuietObserver:
            """Подавить спам 'Unhandled error in Deferred' от Twisted."""
            def __call__(self, event: dict) -> None:
                if event.get("isError"):
                    text = str(event.get("failure", ""))
                    if "TimeoutError" in text or "CancelledError" in text:
                        return
                    log.debug("twisted: %s", text)

        twisted_log.startLoggingWithObserver(_QuietObserver(), setStdout=False)

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
        expires_at: float = 0.0,
        on_token_refreshed: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._account_id = account_id
        self._host_type = host_type
        self._refresh_token = refresh_token
        # In-memory expires_at нужен для defensive save: после _do_auth
        # success передаём актуальное значение в callback, чтобы shared
        # tokens-file всегда был синхронизирован с in-memory state клиента.
        # Закрывает race "refresh прошёл, callback упал" → file со spent
        # refresh_token остаётся на диске.
        self._token_expires_at = expires_at
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
        # Timestamp of last successful auth (set in _connect_and_auth).
        # Используется в _on_disconnected: если соединение прожило
        # ≥STABLE_UPTIME_SEC — реальный network drop, сброс attempt
        # counter; иначе — server reject, накапливаем backoff.
        self._last_successful_connect_ts: float = 0.0

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
        # Сбрасываем timestamp ДО попытки auth: smart-reset в
        # _on_disconnected должен видеть «нет валидной сессии», пока
        # _do_auth() не завершится полностью успехом. Иначе uptime
        # считается от ПРЕДЫДУЩЕЙ успешной сессии (баг 11.05.2026).
        self._last_successful_connect_ts = 0.0

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
        self._last_successful_connect_ts = _time.time()

    def _cleanup_client(self) -> None:
        """Остановить текущего клиента (best effort)."""
        self._stop_heartbeat()
        self._connected.clear()
        self._account_auth_done.clear()
        # Smart-reset не должен видеть «стабильный uptime» от мёртвой
        # сессии — иначе counter сбрасывается в 0 и backoff не растёт.
        self._last_successful_connect_ts = 0.0
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

    def _do_auth(self, timeout: float = 30, allow_refresh: bool = True) -> None:
        """Авторизация приложения и аккаунта (вызывается из start и reconnect).

        Если `GetAccountListByAccessTokenRes` падает по timeout — это
        классический симптом silent token-rotation на серверной стороне
        cTrader (Spotware закрывает TCP cleanly без ProtoOAErrorRes).
        В этом случае при `allow_refresh=True` пробуем обновить access_token
        через refresh_token и переавторизоваться ОДИН раз; иначе пробрасываем.
        """
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
        try:
            acct_list_res = self._send_and_wait(
                acct_list_req,
                ProtoOAGetAccountListByAccessTokenRes().payloadType,
                timeout=timeout,
            )
        except TimeoutError as exc:
            if allow_refresh and self._refresh_token:
                log.warning(
                    "cTrader: GetAccountListByAccessTokenRes timeout — "
                    "пробуем proactive refresh access_token (silent rotation?)"
                )
                if self._try_refresh_token():
                    # После refresh access_token cessия сервера, скорее всего,
                    # уже невалидна — нужен новый TCP-connect. Пробрасываем
                    # специфический exception, чтобы reconnect-loop повторил
                    # connect+auth с новым токеном.
                    raise ConnectionError(
                        "cTrader: token refreshed, reconnect required"
                    ) from exc
            raise
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
        # Defensive token sync: после каждого успешного auth пишем
        # in-memory токены в shared file. Если callback при refresh упал
        # ранее (file остался со spent refresh_token) — этот вызов закроет
        # дыру, как только клиент успешно подключится с in-memory свежим
        # токеном. См. BUILDLOG.md 2026-05-12 «token rotation hardening».
        self._save_current_tokens()

    def _save_current_tokens(self) -> None:
        """Sync in-memory OAuth-state в shared token-store через callback.

        Идемпотентно: если file актуальный — overwrite того же содержимого
        (no-op для is_expired check). Если file устарел — file обновится.
        Не падает на исключениях callback (callback логирует сам).
        """
        if not self._on_token_refreshed:
            return
        if not self._access_token or not self._refresh_token:
            return
        try:
            self._on_token_refreshed(
                self._access_token,
                self._refresh_token,
                self._token_expires_at,
            )
        except Exception as cb_err:
            log.warning("cTrader: defensive token save failed: %s", cb_err)

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

    def get_trendbars(
        self,
        symbol_id: int,
        period_minutes: int,
        from_ts_ms: int,
        to_ts_ms: int,
    ) -> list[Any]:
        """Получить исторические OHLCV-бары (trendbars) за период.

        Возвращает raw ProtoOATrendbar — для декодирования см. ctrader_feed.
        period_minutes: 1, 2, 3, 4, 5, 10, 15, 30, 60, 240, 720, 1440, 10080, 43200.
        """
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAGetTrendbarsReq,
            ProtoOAGetTrendbarsRes,
        )
        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
            ProtoOATrendbarPeriod,
        )

        period_map = {
            1: ProtoOATrendbarPeriod.M1,
            2: ProtoOATrendbarPeriod.M2,
            3: ProtoOATrendbarPeriod.M3,
            4: ProtoOATrendbarPeriod.M4,
            5: ProtoOATrendbarPeriod.M5,
            10: ProtoOATrendbarPeriod.M10,
            15: ProtoOATrendbarPeriod.M15,
            30: ProtoOATrendbarPeriod.M30,
            60: ProtoOATrendbarPeriod.H1,
            240: ProtoOATrendbarPeriod.H4,
            720: ProtoOATrendbarPeriod.H12,
            1440: ProtoOATrendbarPeriod.D1,
            10080: ProtoOATrendbarPeriod.W1,
            43200: ProtoOATrendbarPeriod.MN1,
        }
        period = period_map.get(period_minutes)
        if period is None:
            raise ValueError(
                f"cTrader: неподдерживаемый период {period_minutes} минут. "
                f"Доступно: {sorted(period_map)}"
            )

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = symbol_id
        req.period = period
        req.fromTimestamp = from_ts_ms
        req.toTimestamp = to_ts_ms

        res = self._send_and_wait(
            req, ProtoOAGetTrendbarsRes().payloadType, timeout=30,
        )
        return list(res.trendbar)

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

        log.info(
            "cTrader AMEND wire: pos=%d stopLoss=%s takeProfit=%s",
            position_id,
            f"{stop_loss:.5f}" if stop_loss is not None else "—",
            f"{take_profit:.5f}" if take_profit is not None else "—",
        )
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
        import time as _time

        uptime = (
            _time.time() - self._last_successful_connect_ts
            if self._last_successful_connect_ts else 0.0
        )
        log.warning(
            "cTrader: отключено (uptime %.0fs) — %s", uptime, reason,
        )
        self._stop_heartbeat()
        self._connected.clear()
        self._account_auth_done.clear()
        with self._lock:
            for waiters in self._waiters.values():
                for ev, res in waiters:
                    res[1] = ConnectionError(f"Disconnected: {reason}")
                    ev.set()
            self._waiters.clear()

        # Smart-reset attempt counter. Сбрасываем ТОЛЬКО если:
        #  (1) предыдущая сессия прошла полный auth-handshake
        #      (_last_successful_connect_ts > 0 — устанавливается в
        #      _connect_and_auth ПОСЛЕ _do_auth success);
        #  (2) сессия прожила ≥STABLE_UPTIME_SEC.
        # Иначе это либо server-throttle (TCP закрыт сразу), либо
        # silent-token-rotation (TCP закрыт после app_auth) — оба
        # случая требуют накапливать backoff, не возвращаться к 5s.
        if self._last_successful_connect_ts > 0 and uptime >= STABLE_UPTIME_SEC:
            if self._reconnect_attempt > 0:
                log.info(
                    "cTrader: соединение было стабильно %.0fs — "
                    "сброс reconnect attempt counter",
                    uptime,
                )
            self._reconnect_attempt = 0

        if self._running and not self._reconnecting:
            self._schedule_reconnect()

    # -- heartbeat ----------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Запустить периодическую отправку ProtoHeartbeatEvent.

        Spotware закрывает TCP, если не получает activity ≥10s. Используем
        интервал 8s с `now=True` — первый heartbeat шлётся сразу при connect,
        чтобы сервер увидел признак жизни до завершения app_auth.
        """
        from twisted.internet import reactor, task

        self._stop_heartbeat()
        loop = task.LoopingCall(self._send_heartbeat)
        self._heartbeat_loop = loop
        reactor.callFromThread(loop.start, HEARTBEAT_INTERVAL_SEC, now=True)
        log.debug("cTrader: heartbeat запущен (каждые %ds, now=True)", HEARTBEAT_INTERVAL_SEC)

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
        """Переподключение с экспоненциальным backoff.

        Delays: RECONNECT_DELAYS_SEC = (5, 10, 30, 60, 120, 300, 900).
        После 6 неудачных попыток delay = 15 минут — это даёт серверной
        стороне cTrader время очистить stale-сессии и снять throttle.

        Reset attempt counter:
        - НЕ сбрасывается при успешном reconnect (мог быть transient
          server-rejected accept → drop через секунды).
        - Сбрасывается в `_on_disconnected` ТОЛЬКО если предыдущее
          соединение прожило ≥STABLE_UPTIME_SEC. Так bot не возвращается
          к малым delays при server-side throttle.
        """
        if self._reconnecting:
            return
        self._reconnecting = True

        import time as _time

        def _do_reconnect():
            while self._running:
                attempt = self._reconnect_attempt
                delay = RECONNECT_DELAYS_SEC[
                    min(attempt, len(RECONNECT_DELAYS_SEC) - 1)
                ]
                log.info("cTrader: reconnect #%d через %ds...", attempt + 1, delay)
                _time.sleep(delay)

                if not self._running:
                    break

                self._cleanup_client()
                _time.sleep(2)

                try:
                    self._connect_and_auth(timeout=30)
                    self._reconnecting = False
                    log.info(
                        "cTrader: переподключение успешно (attempt #%d)",
                        attempt + 1,
                    )
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

    def _try_refresh_token(self) -> bool:
        """Обновить access_token. Стратегия:

        1. Если настроен ``CTRADER_TOKEN_SERVICE_URL`` — сначала GET /token
           (другой бот мог уже обновить → используем готовый, избегаем
           дублирующего refresh и rotation conflict).
        2. Если service выдал тот же или более старый токен — POST /refresh
           (сервис с server-side dedup делает refresh ровно один раз).
        3. Если service недоступен — fallback на локальный
           ``refresh_access_token`` (старое поведение, backward compat).

        НЕ трогает соединение — это задача вызывающей стороны.
        Возвращает True если access_token успешно обновлён.
        """
        if self._try_refresh_via_service():
            return True

        if not self._refresh_token:
            log.error("cTrader: refresh_token отсутствует — refresh невозможен")
            return False

        try:
            from fx_pro_bot.trading.auth import refresh_access_token

            new_token = refresh_access_token(
                self._refresh_token, self._client_id, self._client_secret,
            )
            self._access_token = new_token.access_token
            self._refresh_token = new_token.refresh_token
            self._token_expires_at = new_token.expires_at
            log.info(
                "cTrader: access token обновлён через refresh_token "
                "(expires through %.1f дней)",
                (new_token.expires_at - time.time()) / 86400.0,
            )

            if self._on_token_refreshed:
                try:
                    self._on_token_refreshed(
                        new_token.access_token,
                        new_token.refresh_token,
                        new_token.expires_at,
                    )
                except Exception as cb_err:
                    log.warning("cTrader: on_token_refreshed callback error: %s", cb_err)
            return True
        except Exception as exc:
            log.error("cTrader: refresh_access_token не удался: %s", exc)
            return False

    def _try_refresh_via_service(self) -> bool:
        """Попытка обновить токен через ctrader-token-service.

        Returns:
            True — токен обновлён (in-memory state + callback вызван);
            False — service не настроен/недоступен (caller fallback-ит).
        """
        try:
            from shared_oauth.token_client import (  # type: ignore
                TokenServiceRejected,
                TokenServiceUnavailable,
                fetch_token,
                force_refresh,
                load_service_config,
            )
        except Exception:
            return False

        cfg = load_service_config(client_label="ctrader-client")
        if cfg is None:
            return False

        try:
            tok = fetch_token(cfg)
            if (
                tok.access_token
                and tok.access_token != self._access_token
                and tok.expires_at > self._token_expires_at + 60
            ):
                log.info(
                    "cTrader: token-service выдал более свежий токен "
                    "(last_pushed_by=%s, expires +%.1f дней) — используем без refresh",
                    tok.last_pushed_by,
                    (tok.expires_at - time.time()) / 86400.0,
                )
                self._update_token_state(tok.access_token, tok.refresh_token, tok.expires_at)
                return True

            tok = force_refresh(cfg, reason="ctrader-client-silent-rotation")
            if tok.access_token:
                log.info(
                    "cTrader: token-service refresh OK (expires +%.1f дней)",
                    (tok.expires_at - time.time()) / 86400.0,
                )
                self._update_token_state(tok.access_token, tok.refresh_token, tok.expires_at)
                return True
            log.warning("cTrader: token-service force_refresh вернул пустой токен")
            return False
        except TokenServiceRejected as exc:
            log.error("cTrader: token-service rejected (%s) — fallback на local refresh", exc)
            return False
        except TokenServiceUnavailable as exc:
            log.warning("cTrader: token-service unavailable (%s) — fallback на local refresh", exc)
            return False
        except Exception as exc:
            log.warning("cTrader: token-service unexpected (%s) — fallback на local refresh", exc)
            return False

    def _update_token_state(self, access_token: str, refresh_token: str, expires_at: float) -> None:
        """Применить новый токен внутри клиента и нотифицировать callback."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = expires_at
        if self._on_token_refreshed:
            try:
                self._on_token_refreshed(access_token, refresh_token, expires_at)
            except Exception as cb_err:
                log.warning("cTrader: on_token_refreshed callback error: %s", cb_err)

    def _handle_token_invalidated(self) -> None:
        """Обновить токен и переавторизовать аккаунт после TokenInvalidatedEvent."""
        if not self._try_refresh_token():
            if self._running:
                self._schedule_reconnect()
            return

        try:
            # allow_refresh=False — мы уже только что обновились; второй
            # подряд refresh при том же symptom скорее всего бесполезен,
            # лучше уйти в reconnect.
            self._do_auth(timeout=30, allow_refresh=False)
            log.info("cTrader: аккаунт переавторизован после обновления токена")
        except Exception as exc:
            log.error("cTrader: reauth после refresh не удался (%s), полный reconnect", exc)
            if self._running:
                self._schedule_reconnect()
