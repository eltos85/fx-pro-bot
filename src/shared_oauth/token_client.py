"""HTTP-клиент для ctrader-token-service.

Боты используют эти функции вместо локальных:
- :func:`fetch_token`        — drop-in замена для ``ensure_valid_token`` /
  ``ensure_valid_token_race_safe``;
- :func:`force_refresh`      — централизованный refresh с server-side dedup;
- :func:`push_token`         — push свежего токена в сервис после
  in-flight refresh внутри ``CTraderClient``.

Если ``CTRADER_TOKEN_SERVICE_URL`` пустой — клиент возвращает None /
бросает исключение (вызывающая сторона fallback-ит на локальный
file-based путь).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServiceConfig:
    url: str
    secret: str
    timeout_sec: float = 10.0
    client_label: str = "unknown"

    @property
    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"}


@dataclass(frozen=True)
class ServiceToken:
    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "bearer"
    last_refresh_ts: float = 0.0
    last_pushed_by: str = ""
    last_pushed_ts: float = 0.0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ServiceToken":
        return cls(
            access_token=payload.get("access_token", ""),
            refresh_token=payload.get("refresh_token", ""),
            expires_at=float(payload.get("expires_at", 0.0)),
            token_type=payload.get("token_type", "bearer"),
            last_refresh_ts=float(payload.get("last_refresh_ts", 0.0)),
            last_pushed_by=payload.get("last_pushed_by", ""),
            last_pushed_ts=float(payload.get("last_pushed_ts", 0.0)),
        )


class TokenServiceUnavailable(RuntimeError):
    """Сервис не отвечает или вернул 5xx — вызывающая сторона может fallback-ить."""


class TokenServiceRejected(RuntimeError):
    """Сервис вернул 4xx (например, 401 auth fail)."""


def load_service_config(client_label: str) -> Optional[ServiceConfig]:
    """Прочитать ENV. Если URL пустой — None (token-service не используется)."""
    url = os.environ.get("CTRADER_TOKEN_SERVICE_URL", "").strip().rstrip("/")
    secret = os.environ.get("CTRADER_TOKEN_SERVICE_SECRET", "").strip()
    if not url or not secret:
        return None
    return ServiceConfig(url=url, secret=secret, client_label=client_label)


def _do_request(
    method: str,
    cfg: ServiceConfig,
    path: str,
    *,
    json_body: dict | None = None,
    retries: int = 3,
    retry_backoff_sec: float = 1.0,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.request(
                method,
                f"{cfg.url}{path}",
                headers=cfg.auth_header,
                json=json_body,
                timeout=cfg.timeout_sec,
            )
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            log.warning("token-service: %s %s attempt #%d failed: %s",
                        method, path, attempt + 1, exc)
            time.sleep(retry_backoff_sec * (2 ** attempt))
            continue

        if resp.status_code == 401:
            raise TokenServiceRejected("token-service auth rejected (check CTRADER_TOKEN_SERVICE_SECRET)")
        if 500 <= resp.status_code < 600:
            last_exc = TokenServiceUnavailable(f"{resp.status_code}: {resp.text[:200]}")
            log.warning("token-service: %s %s 5xx attempt #%d: %s",
                        method, path, attempt + 1, resp.text[:200])
            time.sleep(retry_backoff_sec * (2 ** attempt))
            continue
        if resp.status_code >= 400:
            raise TokenServiceRejected(f"{resp.status_code}: {resp.text[:200]}")
        return resp.json()

    assert last_exc is not None
    raise TokenServiceUnavailable(str(last_exc))


def fetch_token(cfg: ServiceConfig) -> ServiceToken:
    """GET /token — текущий свежий токен (сервис сам refresh-ит при необходимости)."""
    payload = _do_request("GET", cfg, "/token")
    return ServiceToken.from_payload(payload)


def force_refresh(cfg: ServiceConfig, reason: str = "explicit") -> ServiceToken:
    """POST /refresh — принудительный refresh (с dedup на стороне сервиса)."""
    payload = _do_request(
        "POST", cfg, "/refresh",
        json_body={"reason": reason, "client_label": cfg.client_label},
    )
    return ServiceToken.from_payload(payload)


def push_token(
    cfg: ServiceConfig,
    access_token: str,
    refresh_token: str,
    expires_at: float,
) -> ServiceToken:
    """POST /token — отдать сервису свежий токен после in-flight refresh.

    Сервис сам решает, перезаписывать ли его (только если pushed новее).
    """
    payload = _do_request(
        "POST", cfg, "/token",
        json_body={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "client_label": cfg.client_label,
        },
    )
    return ServiceToken.from_payload(payload)


def make_push_callback(cfg: ServiceConfig) -> Callable[[str, str, float], None]:
    """Удобный helper: возвращает callback совместимый с ``CTraderClient.on_token_refreshed``."""

    def _cb(access_token: str, refresh_token: str, expires_at: float) -> None:
        try:
            push_token(cfg, access_token, refresh_token, expires_at)
        except Exception as exc:
            log.warning("token-service: push_token callback failed: %s", exc)

    return _cb
