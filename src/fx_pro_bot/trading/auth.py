"""OAuth2 авторизация для cTrader Open API.

Обеспечивает получение и обновление access/refresh токенов через REST API.
Токены хранятся в JSON-файле в data-директории.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

TOKEN_ENDPOINT = "https://openapi.ctrader.com/apps/token"
AUTH_URL_TEMPLATE = (
    "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
    "?client_id={client_id}&redirect_uri={redirect_uri}&scope=trading&product=web"
)

TOKEN_REFRESH_MARGIN_SEC = 86400  # обновлять за сутки до истечения


@dataclass
class TokenData:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    token_type: str = "bearer"

    @property
    def is_expired(self) -> bool:
        return time.time() >= (self.expires_at - TOKEN_REFRESH_MARGIN_SEC)

    @property
    def is_valid(self) -> bool:
        return bool(self.access_token) and not self.is_expired


class TokenStore:
    """Хранение OAuth-токенов в JSON-файле."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> TokenData:
        if not self._path.exists():
            return TokenData()
        try:
            raw = json.loads(self._path.read_text())
            return TokenData(**{k: raw[k] for k in TokenData.__dataclass_fields__ if k in raw})
        except Exception:
            log.warning("Не удалось загрузить токены из %s", self._path)
            return TokenData()

    def save(self, data: TokenData) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(asdict(data), indent=2))
        log.info("Токены сохранены в %s", self._path)


def get_auth_url(client_id: str, redirect_uri: str) -> str:
    return AUTH_URL_TEMPLATE.format(client_id=client_id, redirect_uri=redirect_uri)


def exchange_code_for_tokens(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> TokenData:
    """Обменять authorization code на access/refresh токены."""
    resp = requests.get(
        TOKEN_ENDPOINT,
        params={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("errorCode"):
        raise RuntimeError(f"cTrader auth error: {data.get('description', data['errorCode'])}")

    return TokenData(
        access_token=data["accessToken"],
        refresh_token=data["refreshToken"],
        expires_at=time.time() + data.get("expiresIn", 2_628_000),
        token_type=data.get("tokenType", "bearer"),
    )


def refresh_access_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> TokenData:
    """Обновить access token по refresh token."""
    resp = requests.post(
        TOKEN_ENDPOINT,
        params={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("errorCode"):
        raise RuntimeError(f"cTrader refresh error: {data.get('description', data['errorCode'])}")

    return TokenData(
        access_token=data["accessToken"],
        refresh_token=data["refreshToken"],
        expires_at=time.time() + data.get("expiresIn", 2_628_000),
        token_type=data.get("tokenType", "bearer"),
    )


def ensure_valid_token(
    store: TokenStore,
    client_id: str,
    client_secret: str,
) -> TokenData:
    """Загрузить токен и обновить при необходимости."""
    token = store.load()
    if not token.access_token:
        raise RuntimeError(
            "Токены не найдены. Выполните авторизацию: fx-pro-auth"
        )

    if token.is_expired and token.refresh_token:
        log.info("Access token истёк, обновляю через refresh token...")
        token = refresh_access_token(token.refresh_token, client_id, client_secret)
        store.save(token)
        log.info("Access token обновлён, истекает через %.0f дней",
                 (token.expires_at - time.time()) / 86400)

    return token
