"""Unit-тесты ctrader-token-service: TokenManager + HTTP API + клиент.

Покрытие:
- TokenManager.get() — auto refresh при близком expiry;
- TokenManager.force_refresh() — dedup-окно блокирует повторный refresh;
- TokenManager.push() — приём токена от бота (newer overrides, older ignored);
- TokenManager._read_disk/_write_disk — round-trip через JSON;
- FastAPI auth (Bearer required);
- FastAPI endpoints (GET /token, POST /refresh, POST /token, /healthz, /status);
- shared_oauth.token_client — fetch/force_refresh/push с retry на 5xx;
- CTraderClient._try_refresh_via_service() — приоритет service над local
  refresh + use-newer-from-service логика.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ctrader_token_service.manager import RefreshError, TokenData, TokenManager


# ─── TokenManager ───────────────────────────────────────────────────────────


def _make_manager(tmp_path: Path, **overrides) -> TokenManager:
    return TokenManager(
        token_path=tmp_path / "tokens.json",
        client_id="cid",
        client_secret="csec",
        refresh_margin_sec=overrides.get("refresh_margin_sec", 60.0),
        refresh_dedup_window_sec=overrides.get("refresh_dedup_window_sec", 5.0),
    )


def _seed_disk(tmp_path: Path, *, expires_in_sec: float, refresh_token: str = "rt-1") -> None:
    payload = {
        "access_token": "AT-OLD",
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in_sec,
        "token_type": "bearer",
        "last_refresh_ts": 0.0,
        "last_pushed_by": "test-seed",
        "last_pushed_ts": time.time(),
    }
    (tmp_path / "tokens.json").write_text(json.dumps(payload))


def test_manager_loads_from_disk_on_init(tmp_path: Path) -> None:
    _seed_disk(tmp_path, expires_in_sec=86400 * 14)
    mgr = _make_manager(tmp_path)
    snap = mgr.snapshot()
    assert snap.access_token == "AT-OLD"
    assert snap.refresh_token == "rt-1"


def test_manager_get_returns_fresh_without_refresh(tmp_path: Path) -> None:
    _seed_disk(tmp_path, expires_in_sec=86400 * 14)
    mgr = _make_manager(tmp_path)
    with patch("ctrader_token_service.manager.requests.post") as post_mock:
        snap = mgr.get()
        post_mock.assert_not_called()
    assert snap.access_token == "AT-OLD"


def test_manager_get_triggers_refresh_when_close_to_expiry(tmp_path: Path) -> None:
    _seed_disk(tmp_path, expires_in_sec=30)  # < refresh_margin (60s)
    mgr = _make_manager(tmp_path, refresh_margin_sec=60.0)
    response = MagicMock()
    response.json.return_value = {
        "accessToken": "AT-NEW",
        "refreshToken": "rt-2",
        "expiresIn": 86400 * 30,
    }
    response.raise_for_status = MagicMock()
    with patch("ctrader_token_service.manager.requests.post", return_value=response):
        new = mgr.get()
    assert new.access_token == "AT-NEW"
    assert new.refresh_token == "rt-2"
    disk = json.loads((tmp_path / "tokens.json").read_text())
    assert disk["access_token"] == "AT-NEW"


def test_manager_force_refresh_dedup_window(tmp_path: Path) -> None:
    _seed_disk(tmp_path, expires_in_sec=86400 * 14)
    mgr = _make_manager(tmp_path, refresh_dedup_window_sec=10.0)

    response = MagicMock()
    response.json.return_value = {
        "accessToken": "AT-NEW",
        "refreshToken": "rt-2",
        "expiresIn": 86400 * 30,
    }
    response.raise_for_status = MagicMock()

    with patch("ctrader_token_service.manager.requests.post", return_value=response) as post_mock:
        first = mgr.force_refresh(reason="bot-1")
        second = mgr.force_refresh(reason="bot-2")  # должно быть dedup
        assert post_mock.call_count == 1
    assert first.access_token == "AT-NEW"
    assert second.access_token == "AT-NEW"


def test_manager_push_newer_overrides_older(tmp_path: Path) -> None:
    _seed_disk(tmp_path, expires_in_sec=86400 * 14)
    mgr = _make_manager(tmp_path)

    new_expiry = time.time() + 86400 * 30
    pushed = mgr.push(
        access_token="AT-PUSH",
        refresh_token="rt-push",
        expires_at=new_expiry,
        client_label="advisor",
    )
    assert pushed.access_token == "AT-PUSH"
    assert pushed.last_pushed_by == "advisor"


def test_manager_push_ignores_older_token(tmp_path: Path) -> None:
    _seed_disk(tmp_path, expires_in_sec=86400 * 14)
    mgr = _make_manager(tmp_path)

    old_expiry = time.time() + 60  # сильно меньше текущего
    result = mgr.push(
        access_token="AT-OLDER",
        refresh_token="rt-older",
        expires_at=old_expiry,
        client_label="fx-ai-trader",
    )
    # Manager возвращает текущий (без overwrite), потому что pushed старее.
    assert result.access_token != "AT-OLDER"


def test_manager_refresh_error_propagates(tmp_path: Path) -> None:
    _seed_disk(tmp_path, expires_in_sec=30)
    mgr = _make_manager(tmp_path)
    response = MagicMock()
    response.json.return_value = {
        "errorCode": "INVALID_REQUEST",
        "description": "Access denied",
    }
    response.raise_for_status = MagicMock()
    with patch("ctrader_token_service.manager.requests.post", return_value=response):
        with pytest.raises(RefreshError):
            mgr.get()


# ─── FastAPI app ────────────────────────────────────────────────────────────


def _make_app_with_seeded_disk(tmp_path: Path):
    from fastapi.testclient import TestClient

    from ctrader_token_service.app import create_app
    from ctrader_token_service.settings import Settings

    _seed_disk(tmp_path, expires_in_sec=86400 * 14)
    settings = Settings(
        token_path=tmp_path / "tokens.json",
        client_id="cid",
        client_secret="csec",
        api_secret="SECRET",
        background_check_interval_sec=3600.0,
    )
    app = create_app(settings)
    return TestClient(app), settings


def test_app_healthz_open(tmp_path: Path) -> None:
    client, _ = _make_app_with_seeded_disk(tmp_path)
    with client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_app_token_requires_auth(tmp_path: Path) -> None:
    client, _ = _make_app_with_seeded_disk(tmp_path)
    with client:
        resp_no = client.get("/token")
        resp_wrong = client.get("/token", headers={"Authorization": "Bearer wrong"})
        resp_ok = client.get("/token", headers={"Authorization": "Bearer SECRET"})
    assert resp_no.status_code == 401
    assert resp_wrong.status_code == 401
    assert resp_ok.status_code == 200
    body = resp_ok.json()
    assert body["access_token"] == "AT-OLD"


def test_app_push_then_get_returns_pushed(tmp_path: Path) -> None:
    client, _ = _make_app_with_seeded_disk(tmp_path)
    new_expiry = time.time() + 86400 * 60
    with client:
        push_resp = client.post(
            "/token",
            headers={"Authorization": "Bearer SECRET"},
            json={
                "access_token": "AT-FROM-BOT",
                "refresh_token": "rt-from-bot",
                "expires_at": new_expiry,
                "client_label": "advisor",
            },
        )
        get_resp = client.get("/token", headers={"Authorization": "Bearer SECRET"})
    assert push_resp.status_code == 200
    assert get_resp.json()["access_token"] == "AT-FROM-BOT"
    assert get_resp.json()["last_pushed_by"] == "advisor"


def test_app_force_refresh_calls_ctrader(tmp_path: Path) -> None:
    client, _ = _make_app_with_seeded_disk(tmp_path)

    response = MagicMock()
    response.json.return_value = {
        "accessToken": "AT-FORCED",
        "refreshToken": "rt-forced",
        "expiresIn": 86400 * 45,
    }
    response.raise_for_status = MagicMock()
    with client:
        with patch("ctrader_token_service.manager.requests.post", return_value=response):
            resp = client.post(
                "/refresh",
                headers={"Authorization": "Bearer SECRET"},
                json={"reason": "test", "client_label": "advisor"},
            )
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "AT-FORCED"


def test_app_status_includes_last_pushed_by(tmp_path: Path) -> None:
    client, _ = _make_app_with_seeded_disk(tmp_path)
    with client:
        client.post(
            "/token",
            headers={"Authorization": "Bearer SECRET"},
            json={
                "access_token": "AT-A",
                "refresh_token": "rt-a",
                "expires_at": time.time() + 86400 * 90,
                "client_label": "advisor",
            },
        )
        status_resp = client.get("/status", headers={"Authorization": "Bearer SECRET"})
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["has_token"] is True
    assert body["last_pushed_by"] == "advisor"
    assert body["days_until_expiry"] > 80


# ─── shared_oauth client ────────────────────────────────────────────────────


def test_token_client_load_returns_none_without_env(monkeypatch) -> None:
    from shared_oauth.token_client import load_service_config

    monkeypatch.delenv("CTRADER_TOKEN_SERVICE_URL", raising=False)
    monkeypatch.delenv("CTRADER_TOKEN_SERVICE_SECRET", raising=False)
    assert load_service_config("test") is None


def test_token_client_load_with_env(monkeypatch) -> None:
    from shared_oauth.token_client import load_service_config

    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_URL", "http://svc:8080/")
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_SECRET", "abc")
    cfg = load_service_config("bot-x")
    assert cfg is not None
    assert cfg.url == "http://svc:8080"
    assert cfg.secret == "abc"
    assert cfg.client_label == "bot-x"
    assert cfg.auth_header == {"Authorization": "Bearer abc"}


def test_token_client_fetch_token_parses_payload(monkeypatch) -> None:
    from shared_oauth.token_client import ServiceConfig, fetch_token

    cfg = ServiceConfig(url="http://svc", secret="s", client_label="test")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_at": 12345.0,
        "token_type": "bearer",
        "last_pushed_by": "advisor",
        "last_pushed_ts": 999.0,
    }
    with patch("shared_oauth.token_client.requests.request", return_value=response):
        tok = fetch_token(cfg)
    assert tok.access_token == "AT"
    assert tok.refresh_token == "RT"
    assert tok.expires_at == 12345.0
    assert tok.last_pushed_by == "advisor"


def test_token_client_retries_on_5xx(monkeypatch) -> None:
    from shared_oauth.token_client import ServiceConfig, TokenServiceUnavailable, fetch_token

    cfg = ServiceConfig(url="http://svc", secret="s", client_label="test")
    bad = MagicMock(); bad.status_code = 503; bad.text = "boom"
    ok = MagicMock(); ok.status_code = 200; ok.json.return_value = {
        "access_token": "AT", "refresh_token": "RT", "expires_at": 1.0, "token_type": "bearer",
    }
    with patch("shared_oauth.token_client.requests.request", side_effect=[bad, bad, ok]):
        with patch("shared_oauth.token_client.time.sleep"):
            tok = fetch_token(cfg)
    assert tok.access_token == "AT"

    with patch("shared_oauth.token_client.requests.request", side_effect=[bad, bad, bad]):
        with patch("shared_oauth.token_client.time.sleep"):
            with pytest.raises(TokenServiceUnavailable):
                fetch_token(cfg)


def test_token_client_401_raises_rejected() -> None:
    from shared_oauth.token_client import ServiceConfig, TokenServiceRejected, fetch_token

    cfg = ServiceConfig(url="http://svc", secret="bad", client_label="test")
    response = MagicMock(); response.status_code = 401; response.text = "unauthorized"
    with patch("shared_oauth.token_client.requests.request", return_value=response):
        with pytest.raises(TokenServiceRejected):
            fetch_token(cfg)


# ─── CTraderClient.token-service integration ────────────────────────────────


def test_ctrader_client_prefers_newer_service_token(monkeypatch) -> None:
    """Если service знает более свежий токен — клиент берёт его без force refresh."""
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_URL", "http://svc")
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_SECRET", "secret")

    from fx_pro_bot.trading.client import CTraderClient

    client = CTraderClient.__new__(CTraderClient)  # type: ignore[call-arg]
    client._client_id = "cid"
    client._client_secret = "csec"
    client._access_token = "AT-OLD"
    client._refresh_token = "RT-OLD"
    client._token_expires_at = time.time() + 60

    captured: dict = {}

    def _cb(a: str, r: str, exp: float) -> None:
        captured["a"] = a; captured["r"] = r; captured["exp"] = exp

    client._on_token_refreshed = _cb

    fresh_expiry = time.time() + 86400 * 30
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "access_token": "AT-NEW",
        "refresh_token": "RT-NEW",
        "expires_at": fresh_expiry,
        "token_type": "bearer",
        "last_pushed_by": "advisor",
    }
    with patch("shared_oauth.token_client.requests.request", return_value=response) as req_mock:
        ok = client._try_refresh_via_service()

    assert ok is True
    assert client._access_token == "AT-NEW"
    assert client._refresh_token == "RT-NEW"
    assert captured["a"] == "AT-NEW"
    # Только GET /token, force_refresh не вызывался.
    assert req_mock.call_count == 1
    _, _, kwargs = req_mock.mock_calls[0]
    assert kwargs.get("method", "GET").upper() == "GET" or req_mock.call_args.args[0] == "GET"


def test_ctrader_client_force_refresh_when_service_has_same_token(monkeypatch) -> None:
    """Service вернул тот же токен → CTraderClient делает POST /refresh."""
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_URL", "http://svc")
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_SECRET", "secret")

    from fx_pro_bot.trading.client import CTraderClient

    client = CTraderClient.__new__(CTraderClient)  # type: ignore[call-arg]
    client._client_id = "cid"
    client._client_secret = "csec"
    client._access_token = "AT-SAME"
    client._refresh_token = "RT-SAME"
    client._token_expires_at = time.time() + 60
    client._on_token_refreshed = None

    get_resp = MagicMock(); get_resp.status_code = 200; get_resp.json.return_value = {
        "access_token": "AT-SAME",
        "refresh_token": "RT-SAME",
        "expires_at": time.time() + 60,
        "token_type": "bearer",
    }
    refresh_resp = MagicMock(); refresh_resp.status_code = 200; refresh_resp.json.return_value = {
        "access_token": "AT-FORCED",
        "refresh_token": "RT-FORCED",
        "expires_at": time.time() + 86400 * 30,
        "token_type": "bearer",
    }
    with patch(
        "shared_oauth.token_client.requests.request",
        side_effect=[get_resp, refresh_resp],
    ):
        ok = client._try_refresh_via_service()
    assert ok is True
    assert client._access_token == "AT-FORCED"
    assert client._refresh_token == "RT-FORCED"


def test_ctrader_client_falls_back_when_service_unavailable(monkeypatch) -> None:
    """Если service недоступен — _try_refresh_via_service() возвращает False (caller fallback-ит)."""
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_URL", "http://svc")
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_SECRET", "secret")

    from fx_pro_bot.trading.client import CTraderClient

    client = CTraderClient.__new__(CTraderClient)  # type: ignore[call-arg]
    client._client_id = "cid"
    client._client_secret = "csec"
    client._access_token = "AT-OLD"
    client._refresh_token = "RT-OLD"
    client._token_expires_at = time.time() + 60
    client._on_token_refreshed = None

    bad = MagicMock(); bad.status_code = 503; bad.text = "boom"
    with patch("shared_oauth.token_client.requests.request", return_value=bad):
        with patch("shared_oauth.token_client.time.sleep"):
            ok = client._try_refresh_via_service()
    assert ok is False
    # Старые токены сохранились — caller теперь fallback-ит на local refresh.
    assert client._access_token == "AT-OLD"


def test_ctrader_client_skips_service_when_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("CTRADER_TOKEN_SERVICE_URL", raising=False)
    monkeypatch.delenv("CTRADER_TOKEN_SERVICE_SECRET", raising=False)

    from fx_pro_bot.trading.client import CTraderClient

    client = CTraderClient.__new__(CTraderClient)  # type: ignore[call-arg]
    client._client_id = "cid"
    client._client_secret = "csec"
    client._access_token = "AT-OLD"
    client._refresh_token = "RT-OLD"
    client._token_expires_at = time.time() + 60
    client._on_token_refreshed = None

    with patch("shared_oauth.token_client.requests.request") as req_mock:
        ok = client._try_refresh_via_service()
    assert ok is False
    req_mock.assert_not_called()


# ─── ensure_valid_token / ensure_valid_token_race_safe via service ──────────


def test_ensure_valid_token_uses_service(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_URL", "http://svc")
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_SECRET", "S")

    from fx_pro_bot.trading.auth import TokenStore, ensure_valid_token

    store = TokenStore(tmp_path / "advisor_tokens.json")
    response = MagicMock(); response.status_code = 200; response.json.return_value = {
        "access_token": "AT-FROM-SERVICE",
        "refresh_token": "RT-FROM-SERVICE",
        "expires_at": time.time() + 86400 * 30,
        "token_type": "bearer",
    }
    with patch("shared_oauth.token_client.requests.request", return_value=response):
        token = ensure_valid_token(store, "cid", "csec", client_label="advisor")
    assert token.access_token == "AT-FROM-SERVICE"
    disk = json.loads((tmp_path / "advisor_tokens.json").read_text())
    assert disk["access_token"] == "AT-FROM-SERVICE"


def test_ensure_valid_token_falls_back_when_service_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_URL", "http://svc")
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_SECRET", "S")

    from fx_pro_bot.trading.auth import TokenData, TokenStore, ensure_valid_token

    store = TokenStore(tmp_path / "advisor_tokens.json")
    store.save(TokenData(
        access_token="AT-LOCAL",
        refresh_token="RT-LOCAL",
        expires_at=time.time() + 86400 * 30,
    ))

    bad = MagicMock(); bad.status_code = 503; bad.text = "down"
    with patch("shared_oauth.token_client.requests.request", return_value=bad):
        with patch("shared_oauth.token_client.time.sleep"):
            token = ensure_valid_token(store, "cid", "csec")
    assert token.access_token == "AT-LOCAL"


def test_ensure_valid_token_race_safe_uses_service(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_URL", "http://svc")
    monkeypatch.setenv("CTRADER_TOKEN_SERVICE_SECRET", "S")

    from fx_ai_trader.trading.token_lock import ensure_valid_token_race_safe

    response = MagicMock(); response.status_code = 200; response.json.return_value = {
        "access_token": "AT-SERVICE-FXAI",
        "refresh_token": "RT-SERVICE-FXAI",
        "expires_at": time.time() + 86400 * 30,
        "token_type": "bearer",
    }
    with patch("shared_oauth.token_client.requests.request", return_value=response):
        token = ensure_valid_token_race_safe(
            tmp_path / "fx_ai_tokens.json",
            "cid",
            "csec",
            client_label="fx-ai-trader",
        )
    assert token.access_token == "AT-SERVICE-FXAI"
    disk = json.loads((tmp_path / "fx_ai_tokens.json").read_text())
    assert disk["access_token"] == "AT-SERVICE-FXAI"
