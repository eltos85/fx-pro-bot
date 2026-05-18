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
    client_label: str = "advisor",
) -> TokenData:
    """Загрузить токен и обновить при необходимости.

    Если задан ENV ``CTRADER_TOKEN_SERVICE_URL`` — сначала пробуем
    fetch у централизованного token-service. Если service недоступен
    (network error / 5xx) — fallback на локальный TokenStore. Это
    защищает от downtime сервиса (Advisor продолжит работать).
    """
    service_token = _try_fetch_from_service(client_label)
    if service_token is not None:
        token = TokenData(
            access_token=service_token.access_token,
            refresh_token=service_token.refresh_token,
            expires_at=service_token.expires_at,
            token_type=service_token.token_type,
        )
        try:
            store.save(token)
        except Exception as exc:
            log.warning("ensure_valid_token: не удалось зеркалировать токен в %s: %s",
                        store._path, exc)
        return token

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


def _try_fetch_from_service(client_label: str):
    """Возвращает ``ServiceToken`` если token-service настроен и отвечает, иначе None.

    Импорт shared_oauth внутри функции, чтобы избежать cyclic-import при
    распространении пакета без shared_oauth (forward compat).
    """
    try:
        from shared_oauth.token_client import (  # type: ignore
            TokenServiceRejected,
            TokenServiceUnavailable,
            fetch_token,
            load_service_config,
        )
    except Exception:
        return None

    cfg = load_service_config(client_label=client_label)
    if cfg is None:
        return None
    try:
        tok = fetch_token(cfg)
        if not tok.access_token:
            log.warning("token-service: вернул пустой токен (label=%s) — fallback на файл", client_label)
            return None
        return tok
    except TokenServiceRejected as exc:
        log.error("token-service: rejected (%s) — fallback на файл; проверьте CTRADER_TOKEN_SERVICE_SECRET", exc)
        return None
    except TokenServiceUnavailable as exc:
        log.warning("token-service: недоступен (%s) — fallback на локальный TokenStore", exc)
        return None
    except Exception as exc:
        log.warning("token-service: unexpected error (%s) — fallback на TokenStore", exc)
        return None


def log_token_status(
    token: TokenData,
    label: str = "cTrader",
    warn_threshold_days: float = 7.0,
    logger: logging.Logger | None = None,
) -> None:
    """Залогировать состояние OAuth-токена при старте процесса.

    Для каждого бота при `__init__` пишем:
    - INFO: expires_at дата + сколько дней осталось;
    - WARNING если осталось < `warn_threshold_days` (default 7) — повод
      запустить `fx-pro-auth` заранее, чтобы не торопиться;
    - ERROR если токен УЖЕ просрочен (osталось < 0) — bot скорее всего
      вылетит на ближайшем cTrader-вызове, нужно reauth немедленно.

    Это часть «защиты от просрочки токенов» (BUILDLOG.md 2026-05-12):
    видимость в `docker logs` — это **единственная** наша система алертов
    сейчас, ни Telegram, ни email не подключены. Просто посматривать
    логи раз в неделю достаточно при `warn_threshold = 7d`.
    """
    log_ = logger or log
    left_sec = token.expires_at - time.time()
    left_days = left_sec / 86400.0
    exp_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(token.expires_at))

    if left_sec < 0:
        log_.error(
            "%s OAuth: TOKEN EXPIRED %.1f дней назад (expires_at=%s). "
            "Срочно: запустите fx-pro-auth и обновите /data/ctrader_tokens.json.",
            label, -left_days, exp_str,
        )
    elif left_days < warn_threshold_days:
        log_.warning(
            "%s OAuth: токен истекает через %.1f дней (%s). "
            "Рекомендуется проактивно запустить fx-pro-auth, чтобы не "
            "попасть на single-use rotation refresh_token'а.",
            label, left_days, exp_str,
        )
    else:
        log_.info(
            "%s OAuth: токен валиден до %s (осталось %.1f дней)",
            label, exp_str, left_days,
        )
