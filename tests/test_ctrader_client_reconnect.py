"""Unit-тесты на reconnect/auth-логику CTraderClient.

Покрывают регрессии багов 06–11.05.2026:
- Bug 06–07.05: 244 reconnects → server-throttle на client_id.
- Bug 11.05 #1: smart-reset сбрасывал counter в 0 на старом
  `_last_successful_connect_ts` после неудачного auth (uptime от
  предыдущей сессии 47ч назад) — backoff не рос.
- Bug 11.05 #2: silent token rotation на сервере → 30s timeout на
  `GetAccountListByAccessTokenRes` без `TokenInvalidatedEvent`, мы
  уходили в бесконечный reconnect-loop вместо token refresh.

Тесты без сетевых вызовов — мокаем `_do_auth`, `_try_refresh_token` и
`_send_and_wait` через `unittest.mock.patch.object`.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fx_pro_bot.trading.client import (
    HEARTBEAT_INTERVAL_SEC,
    RECONNECT_DELAYS_SEC,
    STABLE_UPTIME_SEC,
    CTraderClient,
)


def _make_client() -> CTraderClient:
    return CTraderClient(
        client_id="cid",
        client_secret="csec",
        access_token="atok",
        account_id=12345,
        host_type="demo",
        refresh_token="rtok",
    )


# -- heartbeat policy ----------------------------------------------------------


def test_heartbeat_interval_under_server_threshold():
    """Heartbeat <10s (cTrader server hard cap). См. help.ctrader.com/open-api/faq/."""
    assert HEARTBEAT_INTERVAL_SEC < 10
    assert HEARTBEAT_INTERVAL_SEC >= 5  # запас от спама


# -- reconnect backoff ---------------------------------------------------------


def test_reconnect_delays_monotonic_and_capped_at_15min():
    """Backoff растёт монотонно, последний шаг = 15 минут (server-throttle relief)."""
    assert RECONNECT_DELAYS_SEC == tuple(sorted(RECONNECT_DELAYS_SEC))
    assert RECONNECT_DELAYS_SEC[-1] == 900
    assert RECONNECT_DELAYS_SEC[0] == 5


def test_stable_uptime_threshold_at_least_5min():
    """Smart-reset gating: <5 мин uptime считается транзиентным reject."""
    assert STABLE_UPTIME_SEC >= 300


# -- smart-reset gating (bug 11.05 #1) -----------------------------------------


def test_smart_reset_NOT_triggered_when_auth_never_succeeded():
    """`_last_successful_connect_ts == 0` → counter НЕ сбрасывается.

    Bug 11.05: после неудачного `_do_auth` ts оставался от ПРЕДЫДУЩЕЙ
    сессии (47ч назад), uptime=171k > 300 → counter сбрасывался в 0 →
    delay=5s каждый раз → spam reconnect, усиление throttle.
    """
    c = _make_client()
    c._running = True
    c._reconnect_attempt = 3
    c._last_successful_connect_ts = 0.0  # auth ещё ни разу не прошёл
    c._reconnecting = True  # чтобы _on_disconnected не запускал _schedule_reconnect

    c._on_disconnected(client=None, reason="test")

    assert c._reconnect_attempt == 3, "counter не должен сбрасываться без успешного auth"


def test_smart_reset_triggered_after_stable_session():
    """Counter СБРАСЫВАЕТСЯ если auth был успешен И uptime ≥ 5 мин."""
    c = _make_client()
    c._running = True
    c._reconnect_attempt = 5
    c._last_successful_connect_ts = time.time() - (STABLE_UPTIME_SEC + 60)
    c._reconnecting = True

    c._on_disconnected(client=None, reason="test")

    assert c._reconnect_attempt == 0


def test_smart_reset_NOT_triggered_for_short_session():
    """Сессия <STABLE_UPTIME_SEC = server-side reject → counter растёт."""
    c = _make_client()
    c._running = True
    c._reconnect_attempt = 2
    c._last_successful_connect_ts = time.time() - 30  # 30s uptime
    c._reconnecting = True

    c._on_disconnected(client=None, reason="test")

    assert c._reconnect_attempt == 2, "<5 мин uptime не сбрасывает counter"


# -- cleanup сбрасывает timestamp ---------------------------------------------


def test_cleanup_resets_last_successful_connect_ts():
    """После _cleanup_client uptime считается заново."""
    c = _make_client()
    c._last_successful_connect_ts = time.time() - 100
    c._client = None  # без реального twisted-client

    c._cleanup_client()

    assert c._last_successful_connect_ts == 0.0


# -- утечка ClientService (bug 26.08-01.09.2026) -------------------------------
#
# `Client.stopService()` в ctrader-open-api останавливает сервис только при
# `self.running and self.isConnected`, а зовём мы её именно когда соединения
# нет. Невыключенный ClientService продолжал переподключаться своим
# retryPolicy параллельно нашему backoff → 81 живая TCP-сессия к
# demo.ctraderapi.com при рекомендованных двух
# (help.ctrader.com/open-api/connection/, Best practices) → сервер закрывал
# каждое новое соединение, бот не мог авторизоваться 6 суток.


class _FakeClientService:
    """Двойник twisted ClientService: фиксирует вызовы базового stopService."""

    calls: list = []

    @staticmethod
    def stopService(client):
        _FakeClientService.calls.append(client)


def _run_force_stop(client) -> list:
    _FakeClientService.calls = []
    with patch("twisted.application.internet.ClientService", _FakeClientService), \
         patch("twisted.internet.reactor.callFromThread", lambda fn, *a: fn(*a)):
        CTraderClient._force_stop_service(client)
    return _FakeClientService.calls


def test_force_stop_service_bypasses_library_guard():
    """Базовый stopService зовётся даже когда isConnected=False.

    Это и есть суть бага: библиотечный guard делал остановку no-op ровно в
    том состоянии, в котором мы её и вызываем.
    """
    client = SimpleNamespace(running=True, isConnected=False)

    assert _run_force_stop(client) == [client]


def test_force_stop_service_noop_for_already_stopped_service():
    """Остановленный сервис не трогаем — повторный stop не нужен."""
    client = SimpleNamespace(running=False, isConnected=False)

    assert _run_force_stop(client) == []


def test_on_disconnected_stops_client_service():
    """Разрыв гасит ClientService, чтобы его retryPolicy не шёл параллельно backoff."""
    c = _make_client()
    c._running = True
    c._reconnecting = True  # не поднимать reconnect-поток в тесте
    fake_client = object()
    c._client = fake_client

    with patch.object(CTraderClient, "_force_stop_service") as mock_stop:
        c._on_disconnected(client=fake_client, reason="test")

    mock_stop.assert_called_once_with(fake_client)


def test_cleanup_client_stops_client_service():
    """_cleanup_client тоже гасит сервис, а не полагается на библиотечный guard."""
    c = _make_client()
    fake_client = object()
    c._client = fake_client

    with patch.object(CTraderClient, "_force_stop_service") as mock_stop:
        c._cleanup_client()

    mock_stop.assert_called_once_with(fake_client)


# -- timeout на GetAccountList: БЕЗ proactive refresh (регрессия каскада) -----
#
# Раньше (bug 11.05 #2 → FIXED 2026-07-06) здесь стояла эвристика «silent
# rotation»: timeout на GetAccountListByAccessTokenRes → force_refresh. Но
# timeout почти никогда не настоящий rotation (0 TokenInvalidatedEvent за 2
# дня против 154 false-рефрешей), а каждый force_refresh инвалидировал токен
# ДРУГОГО бота на общем client_id → встречный refresh → бесконечный каскад
# (154 свернутых 30-дневных токена за 2 дня, оба бота в троттле).
#
# Фикс по доке cTrader (help.ctrader.com/open-api/messages/): свежий токен
# подтягивается из token-service в _connect_and_auth ДО auth; timeout → просто
# проброс, reconnect-loop ретраит; refresh — только по авторитативному
# ProtoOAAccountsTokenInvalidatedEvent (_handle_token_invalidated).
# Подробно: tests/test_ctrader_client_token_sync.py.


def test_do_auth_get_account_list_timeout_does_not_refresh():
    """Timeout на GetAccountList → проброс TimeoutError, refresh НЕ зовётся.

    Регрессия каскада 2026-07-06: раньше здесь зрался _try_refresh_token.
    """
    c = _make_client()
    call_seq = [object(), TimeoutError("type=2150")]
    seq_iter = iter(call_seq)

    def stepper(message, expected_type, timeout=30):
        nxt = next(seq_iter)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    with patch.object(c, "_send_and_wait", side_effect=stepper), \
         patch.object(c, "_try_refresh_token") as mock_refresh:
        with pytest.raises(TimeoutError):
            c._do_auth(timeout=5)

    mock_refresh.assert_not_called()


def test_do_auth_no_refresh_when_refresh_token_missing():
    """Если refresh_token пустой, refresh не дёргается, TimeoutError пробрасывается.

    Актуально и после удаления эвристики: refresh вообще не зовётся из _do_auth.
    """
    c = _make_client()
    c._refresh_token = ""

    call_seq = [object(), TimeoutError("type=2150")]
    seq_iter = iter(call_seq)

    def stepper(message, expected_type, timeout=30):
        nxt = next(seq_iter)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    with patch.object(c, "_send_and_wait", side_effect=stepper), \
         patch.object(c, "_try_refresh_token") as mock_refresh:
        with pytest.raises(TimeoutError):
            c._do_auth(timeout=5)

    mock_refresh.assert_not_called()


def test_try_refresh_token_returns_false_without_refresh_token():
    """Без refresh_token хелпер возвращает False, не падает."""
    c = _make_client()
    c._refresh_token = ""
    assert c._try_refresh_token() is False


def test_try_refresh_token_invokes_callback():
    """on_token_refreshed callback вызывается с новыми токенами + expires_at."""
    captured: dict = {}

    def on_refresh(access: str, refresh: str, expires_at: float) -> None:
        captured["access"] = access
        captured["refresh"] = refresh
        captured["expires_at"] = expires_at

    c = CTraderClient(
        client_id="cid",
        client_secret="csec",
        access_token="old",
        account_id=1,
        refresh_token="old_rt",
        on_token_refreshed=on_refresh,
    )

    class _NewTok:
        access_token = "new_at"
        refresh_token = "new_rt"
        expires_at = 1.7e9

    with patch(
        "fx_pro_bot.trading.auth.refresh_access_token",
        return_value=_NewTok(),
    ):
        assert c._try_refresh_token() is True

    assert c._access_token == "new_at"
    assert c._refresh_token == "new_rt"
    assert c._token_expires_at == 1.7e9
    assert captured == {"access": "new_at", "refresh": "new_rt", "expires_at": 1.7e9}


def test_save_current_tokens_idempotent_no_callback():
    """_save_current_tokens — no-op если callback не задан."""
    c = _make_client()
    c._on_token_refreshed = None
    c._save_current_tokens()  # should not raise


def test_save_current_tokens_calls_callback_with_in_memory_state():
    """defensive save: после _do_auth success callback вызывается с in-memory state."""
    captured: dict = {}

    def on_refresh(access: str, refresh: str, expires_at: float) -> None:
        captured["access"] = access
        captured["refresh"] = refresh
        captured["expires_at"] = expires_at

    c = CTraderClient(
        client_id="cid",
        client_secret="csec",
        access_token="at",
        account_id=1,
        refresh_token="rt",
        expires_at=1.6e9,
        on_token_refreshed=on_refresh,
    )

    c._save_current_tokens()

    assert captured == {"access": "at", "refresh": "rt", "expires_at": 1.6e9}


def test_save_current_tokens_skipped_without_refresh_token():
    """Без refresh_token — defensive save skip (нечего сохранять)."""
    captured: dict = {}

    def on_refresh(access: str, refresh: str, expires_at: float) -> None:
        captured["called"] = True

    c = CTraderClient(
        client_id="cid",
        client_secret="csec",
        access_token="at",
        account_id=1,
        refresh_token="",
        on_token_refreshed=on_refresh,
    )

    c._save_current_tokens()

    assert captured == {}
