"""Unit-тесты token-sync и auth-flow CTraderClient (регрессия каскада рефрешей).

Баг 2026-07-06 (BUILDLOG): два бота (fx-momentum-bot + fx-ai-trader) делят
один cTrader client_id и ctrader-token-service. Эвристика «silent rotation» в
``_do_auth`` трактовала ``GetAccountListByAccessTokenRes`` timeout как silent
token-rotation и делала ``force_refresh``. Но timeout почти никогда не
настоящий rotation (0 ``ProtoOAAccountsTokenInvalidatedEvent`` за 2 дня против
154 false-рефрешей) — а каждый ``force_refresh`` инвалидировал токен ДРУГОГО
бота (cTrader шлёт ``ProtoOAAccountsTokenInvalidatedEvent`` при refresh всем
сессиям на старом токене, https://help.ctrader.com/open-api/messages/).
Бот B переподключался со stale in-memory токеном → снова timeout → снова
``force_refresh`` → бесконечный каскад (154 свернутых 30-дневных токена за 2
дня, оба бота в server-side троттле).

Фикс (по офиц. доке cTrader):
1. ``_sync_token_from_service`` — перед auth подтянуть актуальный токен из
   token-service (single source of truth), чтобы не идти со stale in-memory.
2. Убрать эвристику silent-rotation: timeout → просто проброс, reconnect-loop
   ретраит. Рефреш — только по авторитативному
   ``ProtoOAAccountsTokenInvalidatedEvent`` (``_handle_token_invalidated``).
"""

from __future__ import annotations

import time

import pytest

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOAApplicationAuthRes,
)
from shared_oauth.token_client import ServiceToken

from fx_pro_bot.trading.client import CTraderClient


def _make_client() -> CTraderClient:
    c = CTraderClient(
        client_id="cid",
        client_secret="csec",
        access_token="tok-inmemory-stale",
        account_id=12345,
        host_type="demo",
        refresh_token="rtok-stale",
    )
    return c


# ─── _sync_token_from_service ────────────────────────────────────────────────


def _set_service_env(monkeypatch, url: str = "http://token-service:8000",
                     secret: str = "svc-secret") -> None:
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_URL", url)
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_SECRET", secret)


def test_sync_token_from_service_pulls_newer_token(monkeypatch):
    """Сервис отдал более свежий токен (другой бот обновил) → in-memory заменён."""
    _set_service_env(monkeypatch)
    c = _make_client()
    fresher = ServiceToken(
        access_token="tok-fresh-from-service",
        refresh_token="rtok-fresh",
        expires_at=time.time() + 2_500_000,
    )
    monkeypatch.setattr(
        "shared_oauth.token_client.fetch_token", lambda cfg: fresher
    )

    c._sync_token_from_service()

    assert c._access_token == "tok-fresh-from-service"
    assert c._refresh_token == "rtok-fresh"
    assert c._token_expires_at == pytest.approx(fresher.expires_at)


def test_sync_token_from_service_same_token_is_noop(monkeypatch):
    """Сервис отдал тот же токен → состояние не трогаем (лишних записей нет)."""
    _set_service_env(monkeypatch)
    c = _make_client()
    before = (c._access_token, c._refresh_token, c._token_expires_at)
    monkeypatch.setattr(
        "shared_oauth.token_client.fetch_token",
        lambda cfg: ServiceToken(
            access_token=c._access_token,
            refresh_token=c._refresh_token,
            expires_at=c._token_expires_at,
        ),
    )

    c._sync_token_from_service()

    assert (c._access_token, c._refresh_token, c._token_expires_at) == before


def test_sync_token_from_service_unavailable_keeps_inmemory(monkeypatch):
    """Сервис недоступен → остаёмся на in-memory, исключение не валит auth."""
    _set_service_env(monkeypatch)
    c = _make_client()
    before_access = c._access_token

    def _boom(_cfg):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("shared_oauth.token_client.fetch_token", _boom)

    # Не должно бросать — sync опционален, auth должен идти дальше.
    c._sync_token_from_service()

    assert c._access_token == before_access


def test_sync_token_from_service_not_configured_is_noop(monkeypatch):
    """Нет CTRADER_TOKEN_SERVICE_URL — локальный режим, sync пропускается."""
    monkeypatch.delenv("CTRADER_TOKEN_SERVICE_URL", raising=False)
    monkeypatch.delenv("CTRADER_TOKEN_SERVICE_SECRET", raising=False)
    c = _make_client()
    before_access = c._access_token

    c._sync_token_from_service()

    assert c._access_token == before_access


# ─── _do_auth: timeout больше не триггерит refresh ───────────────────────────


def test_do_auth_get_account_list_timeout_does_not_refresh(monkeypatch):
    """Регрессия каскада: timeout на GetAccountList → просто проброс, БЕЗ refresh.

    Раньше здесь звался _try_refresh_token → force_refresh → инвалидация токена
    другого бота → встречный refresh → бесконечный цикл (154 рефреша за 2 дня).
    Теперь auth со stale токеном невозможен: _connect_and_auth подтягивает
    свежий токен из сервиса ДО _do_auth, а timeout = транзиентный сбой →
    reconnect-loop ретраит с backoff.
    """
    c = _make_client()

    # app-auth OK, GetAccountList — timeout (имитируем stale-токен сценарий,
    # но теперь это НЕ должно приводить к refresh).
    def _fake_send_and_wait(message, expected_type, timeout=30):
        if expected_type == ProtoOAApplicationAuthRes().payloadType:
            return ProtoOAApplicationAuthRes()
        if expected_type == ProtoOAGetAccountListByAccessTokenRes().payloadType:
            raise TimeoutError("cTrader: таймаут ожидания ответа (type=2150)")
        raise AssertionError(f"unexpected expected_type={expected_type}")

    monkeypatch.setattr(c, "_send_and_wait", _fake_send_and_wait)

    refresh_called = []
    monkeypatch.setattr(c, "_try_refresh_token",
                        lambda: refresh_called.append(1) or True)

    with pytest.raises(TimeoutError):
        c._do_auth(timeout=1)

    assert not refresh_called, (
        "timeout на GetAccountList НЕ должен триггерить refresh "
        "(регрессия silent-rotation каскада 2026-07-06)"
    )


def test_do_auth_no_allow_refresh_parameter(monkeypatch):
    """Параметр allow_refresh убран — вызовы без него не должны падать."""
    c = _make_client()

    def _fake_send_and_wait(message, expected_type, timeout=30):
        if expected_type == ProtoOAApplicationAuthRes().payloadType:
            return ProtoOAApplicationAuthRes()
        res = ProtoOAGetAccountListByAccessTokenRes()
        return res

    monkeypatch.setattr(c, "_send_and_wait", _fake_send_and_wait)
    # Не должно бросать TypeError про allow_refresh.
    c._do_auth(timeout=1)
