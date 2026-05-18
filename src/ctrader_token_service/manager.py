"""TokenManager — singleton, держит свежий cTrader OAuth-токен.

Защищён от concurrent refresh из разных HTTP-handlers через
``threading.Lock`` + **dedup-окно**: если refresh случился N секунд
назад — не делаем повторный refresh, возвращаем закэшированный.

Это закрывает гонку «два бота одновременно увидели token expired →
оба дёрнули /refresh → cTrader rotation chain split». Внутри одного
процесса достаточно in-process lock'а, файловые fcntl-locks не нужны.

Sources:
- Auth0 Refresh Token Rotation pattern.
- Nango «How to handle concurrency with OAuth token refreshes»
  (singleflight + cooldown).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import requests

log = logging.getLogger(__name__)

TOKEN_ENDPOINT = "https://openapi.ctrader.com/apps/token"


@dataclass
class TokenData:
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    token_type: str = "bearer"
    last_refresh_ts: float = 0.0
    last_pushed_by: str = ""
    last_pushed_ts: float = 0.0

    @property
    def is_valid(self) -> bool:
        return bool(self.access_token) and self.expires_at > time.time() + 60

    def to_response(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "last_refresh_ts": self.last_refresh_ts,
            "last_pushed_by": self.last_pushed_by,
            "last_pushed_ts": self.last_pushed_ts,
        }


class RefreshError(RuntimeError):
    """Refresh OAuth-endpoint вернул ошибку (Access denied / expired)."""


@dataclass
class TokenManager:
    """Singleton хранилище токена с защитой от concurrent refresh."""

    token_path: Path
    client_id: str
    client_secret: str
    refresh_margin_sec: float = 86400.0
    refresh_dedup_window_sec: float = 5.0

    _data: TokenData = field(default_factory=TokenData)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._data = self._read_disk()

    # ─── disk I/O ────────────────────────────────────────────────────────

    def _read_disk(self) -> TokenData:
        if not self.token_path.exists():
            log.warning("token-service: %s не существует — пустой стартовый стейт", self.token_path)
            return TokenData()
        try:
            raw = json.loads(self.token_path.read_text())
        except Exception as exc:
            log.error("token-service: не удалось прочитать %s: %s", self.token_path, exc)
            return TokenData()
        return TokenData(
            access_token=raw.get("access_token", ""),
            refresh_token=raw.get("refresh_token", ""),
            expires_at=float(raw.get("expires_at", 0.0)),
            token_type=raw.get("token_type", "bearer"),
            last_refresh_ts=float(raw.get("last_refresh_ts", 0.0)),
            last_pushed_by=raw.get("last_pushed_by", ""),
            last_pushed_ts=float(raw.get("last_pushed_ts", 0.0)),
        )

    def _write_disk(self, data: TokenData) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(self.token_path.suffix + ".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(asdict(data), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, self.token_path)
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    # ─── API ─────────────────────────────────────────────────────────────

    def snapshot(self) -> TokenData:
        """Не-блокирующий снапшот текущего токена (без refresh)."""
        with self._lock:
            return replace(self._data)

    def get(self) -> TokenData:
        """Вернуть свежий токен; если до expiry < refresh_margin — refresh."""
        with self._lock:
            if self._needs_refresh_locked():
                self._refresh_locked(reason="auto-on-get")
            return replace(self._data)

    def force_refresh(self, reason: str = "explicit") -> TokenData:
        """Принудительный refresh с dedup-окном.

        Если refresh случился < dedup_window секунд назад — возвращаем
        текущий токен **без** реального вызова cTrader. Это защищает от
        burst-запросов из разных ботов в одну секунду.
        """
        with self._lock:
            since_last = time.time() - self._data.last_refresh_ts
            if since_last < self.refresh_dedup_window_sec and self._data.is_valid:
                log.info(
                    "token-service: dedup-skip force_refresh (%s), last_refresh %.1fs назад",
                    reason, since_last,
                )
                return replace(self._data)
            self._refresh_locked(reason=reason)
            return replace(self._data)

    def push(
        self,
        access_token: str,
        refresh_token: str,
        expires_at: float,
        client_label: str,
    ) -> TokenData:
        """Принять токен от бота (когда тот сам refresh-нул, например при
        TokenInvalidatedEvent в TCP-сессии).

        Сохраняем только если pushed token **новее** текущего (по
        expires_at). Иначе игнорируем — другой источник имеет более
        свежий и push был бы откатом.
        """
        with self._lock:
            current = self._data
            if not access_token:
                return replace(current)
            if expires_at and current.expires_at > expires_at + 60:
                log.info(
                    "token-service: ignore push from %s (текущий expires_at=%.0f > pushed=%.0f)",
                    client_label, current.expires_at, expires_at,
                )
                return replace(current)
            now = time.time()
            new_data = TokenData(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                token_type=current.token_type or "bearer",
                last_refresh_ts=now,
                last_pushed_by=client_label,
                last_pushed_ts=now,
            )
            self._data = new_data
            self._write_disk(new_data)
            log.info("token-service: token pushed by %s (expires in %.1f days)",
                     client_label, (expires_at - now) / 86400.0)
            return replace(new_data)

    def background_tick(self) -> None:
        """Вызвать из фонового таймера: если близко к expiry — refresh."""
        with self._lock:
            if self._needs_refresh_locked():
                try:
                    self._refresh_locked(reason="background")
                except Exception as exc:
                    log.error("token-service: background refresh failed: %s", exc)

    # ─── internal ────────────────────────────────────────────────────────

    def _needs_refresh_locked(self) -> bool:
        if not self._data.access_token:
            return False
        return self._data.expires_at - time.time() <= self.refresh_margin_sec

    def _refresh_locked(self, reason: str) -> None:
        """Реальный вызов cTrader refresh-endpoint. Должен вызываться под self._lock."""
        if not self._data.refresh_token:
            raise RefreshError("refresh_token пустой — требуется manual reauth")
        log.info("token-service: refresh (%s), запрос к cTrader OAuth", reason)
        resp = requests.post(
            TOKEN_ENDPOINT,
            params={
                "grant_type": "refresh_token",
                "refresh_token": self._data.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errorCode"):
            err = payload.get("description", payload["errorCode"])
            raise RefreshError(f"cTrader refresh error: {err}")

        now = time.time()
        new_data = TokenData(
            access_token=payload["accessToken"],
            refresh_token=payload["refreshToken"],
            expires_at=now + float(payload.get("expiresIn", 2_628_000)),
            token_type=payload.get("tokenType", "bearer"),
            last_refresh_ts=now,
            last_pushed_by="token-service",
            last_pushed_ts=now,
        )
        self._data = new_data
        self._write_disk(new_data)
        log.info(
            "token-service: refresh OK (expires in %.1f days, reason=%s)",
            (new_data.expires_at - now) / 86400.0, reason,
        )
