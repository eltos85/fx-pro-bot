"""Race-safe OAuth refresh для shared cTrader token-store.

Проблема: Advisor (``fx_pro_bot``) и AI-агент (``fx_ai_trader``) живут в
отдельных Docker-контейнерах и используют **общий** файл
``/data/ctrader_tokens.json`` (один demo-аккаунт = один OAuth-grant).

При одновременном expire access_token оба процесса могут попытаться
сделать refresh параллельно — но cTrader refresh_token single-use:
первый получит новые токены, второй получит ``Access denied`` и
invalidate'нет уже сохранённые свежие токены.

Решение (production pattern):
- ``Coder PR #22904`` — singleflight + optimistic locking
- ``Nango blog «How to handle concurrency with OAuth token refreshes»``
- ``openai/codex issue #10332`` — file-lock + re-check

Реализация: ``fcntl.flock`` advisory exclusive lock + re-check expires_at
после acquire + atomic save через ``os.rename``.

NB: в-process mutex здесь не работает — процессы изолированы Docker'ом.
Этот модуль работает только если оба контейнера mount-ят ОДИН volume
с tokens-файлом.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from fx_pro_bot.trading.auth import TokenData, refresh_access_token

log = logging.getLogger(__name__)


# Safety margin перед expires_at: считаем токен «истекающим» если до
# expires_at осталось ≤ N секунд. Должен быть СОГЛАСОВАН с
# ``fx_pro_bot.trading.auth.TOKEN_REFRESH_MARGIN_SEC = 86400`` (1 день),
# иначе оба процесса разойдутся в решении «нужен refresh» / «свежий».
TOKEN_REFRESH_MARGIN_SEC = 86400


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Advisory exclusive lock на файл lock_path. Блокирующий."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: TokenData) -> None:
    """Атомарная запись TokenData в JSON: write → fsync → rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(asdict(data), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _read_token(path: Path) -> TokenData:
    """Безопасно прочитать TokenData. Возвращает пустой при отсутствии/ошибке."""
    if not path.exists():
        return TokenData()
    try:
        raw = json.loads(path.read_text())
        fields = TokenData.__dataclass_fields__
        return TokenData(**{k: raw[k] for k in fields if k in raw})
    except Exception:
        log.warning("Не удалось прочитать токены из %s", path)
        return TokenData()


def ensure_valid_token_race_safe(
    token_path: Path | str,
    client_id: str,
    client_secret: str,
) -> TokenData:
    """Race-safe аналог ``fx_pro_bot.trading.auth.ensure_valid_token``.

    Поведение:
    1. Читаем текущий токен из ``token_path``.
    2. Если ``expires_at - now > TOKEN_REFRESH_MARGIN_SEC`` — свежий, возвращаем.
    3. Иначе acquire flock на ``token_path.lock``, RE-READ (другой процесс
       мог refresh'нуть пока мы ждали), и:
       - если уже свежий после re-read → возвращаем (avoid duplicate refresh);
       - иначе refresh → atomic save → release lock → return.
    """
    token_path = Path(token_path)
    lock_path = token_path.with_suffix(token_path.suffix + ".lock")

    token = _read_token(token_path)
    if not token.access_token:
        raise RuntimeError(
            f"Токены не найдены в {token_path}. Выполните авторизацию: fx-pro-auth"
        )

    if token.expires_at - time.time() > TOKEN_REFRESH_MARGIN_SEC:
        return token

    log.info("FX AI: access_token подходит к expire, refresh под flock")
    with _file_lock(lock_path):
        # Re-read под локом: другой процесс мог уже refresh'нуть.
        token = _read_token(token_path)
        if token.expires_at - time.time() > TOKEN_REFRESH_MARGIN_SEC:
            log.info(
                "FX AI: другой процесс уже refresh'нул токен (expires_at=%.0f), "
                "используем без refresh",
                token.expires_at,
            )
            return token

        if not token.refresh_token:
            raise RuntimeError("refresh_token пустой, нужна manual reauth (fx-pro-auth)")

        new = refresh_access_token(token.refresh_token, client_id, client_secret)
        _atomic_write(token_path, new)
        log.info(
            "FX AI: access_token обновлён, истекает через %.0f дней",
            (new.expires_at - time.time()) / 86400,
        )
        return new


def save_refreshed_token(
    token_path: Path | str,
    access_token: str,
    refresh_token: str,
    expires_at: float | None = None,
) -> None:
    """Callback для ``CTraderClient.on_token_refreshed`` — атомарная запись под flock.

    Используется когда сам клиент cTrader выполняет refresh внутри своей
    логики (например, через _handle_token_invalidated). Чтобы не
    переписать токен Advisor'а, который мог refresh'нуть параллельно,
    делаем acquire lock + re-check expires_at.
    """
    token_path = Path(token_path)
    lock_path = token_path.with_suffix(token_path.suffix + ".lock")

    with _file_lock(lock_path):
        current = _read_token(token_path)
        # Если на диске уже более свежий токен — не перезаписываем.
        if current.access_token == access_token:
            return
        if current.expires_at > (expires_at or 0) + 60:
            log.info(
                "FX AI save_refreshed_token: skipped (on-disk token свежее: %.0f > %.0f)",
                current.expires_at, expires_at or 0,
            )
            return
        _atomic_write(
            token_path,
            TokenData(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at if expires_at else time.time() + 2_628_000,
            ),
        )
        log.info("FX AI save_refreshed_token: записан в %s", token_path)
