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


# -- proactive token refresh при timeout на GetAccountList (bug 11.05 #2) -----


def test_do_auth_triggers_refresh_on_account_list_timeout():
    """TimeoutError на GetAccountListByAccessTokenRes → refresh + reconnect.

    Это lechu от silent token rotation. См. community.ctrader.com/forum/45954.
    """
    c = _make_client()

    # Заглушаем `_send_and_wait`: первый вызов (app_auth) ОК, второй
    # (GetAccountList) бросает TimeoutError.
    calls = {"n": 0}

    def fake_send(message, expected_type, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            return object()  # app_auth_res
        raise TimeoutError("cTrader: таймаут ожидания ответа (type=2150)")

    with patch.object(c, "_send_and_wait", side_effect=fake_send), \
         patch.object(c, "_try_refresh_token", return_value=True) as mock_refresh:
        with pytest.raises(ConnectionError, match="token refreshed"):
            c._do_auth(timeout=5)

    mock_refresh.assert_called_once()


def test_do_auth_does_not_loop_refresh_when_allow_refresh_false():
    """`allow_refresh=False` → refresh не вызывается, исходный TimeoutError пробрасывается.

    Гарантия что не зациклимся: если уже сделали refresh и timeout повторился,
    уходим в общий reconnect, а не в бесконечный refresh-loop.
    """
    c = _make_client()

    def fake_send(message, expected_type, timeout=30):
        if expected_type == 1:  # фиктивный, app_auth
            return object()
        raise TimeoutError("type=2150")

    # Первый вызов — app_auth, второй — GetAccountList.
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
            c._do_auth(timeout=5, allow_refresh=False)

    mock_refresh.assert_not_called()


def test_do_auth_no_refresh_when_refresh_token_missing():
    """Если refresh_token пустой, refresh не дёргается, TimeoutError пробрасывается."""
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
    """on_token_refreshed callback вызывается с новыми токенами."""
    captured: dict = {}

    def on_refresh(access: str, refresh: str) -> None:
        captured["access"] = access
        captured["refresh"] = refresh

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

    with patch(
        "fx_pro_bot.trading.auth.refresh_access_token",
        return_value=_NewTok(),
    ):
        assert c._try_refresh_token() is True

    assert c._access_token == "new_at"
    assert c._refresh_token == "new_rt"
    assert captured == {"access": "new_at", "refresh": "new_rt"}
