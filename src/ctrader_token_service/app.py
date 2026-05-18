"""FastAPI приложение ctrader-token-service.

Endpoints:
- GET  /healthz             — liveness, всегда 200
- GET  /token               — текущий токен (auto-refresh при близком expiry)
- POST /refresh             — force refresh (с dedup-окном)
- POST /token               — push токена от бота (после in-flight refresh)
- GET  /status              — диагностика (валидность, last_pushed_by/_ts)

Аутентификация: ``Authorization: Bearer <CTRADER_TOKEN_SERVICE_SECRET>``.
Доступ только внутри docker-network; наружу порт не публикуем.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from ctrader_token_service.manager import RefreshError, TokenManager
from ctrader_token_service.settings import Settings, load_settings

log = logging.getLogger("ctrader_token_service")


class PushBody(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: float
    client_label: str = "unknown"


class RefreshBody(BaseModel):
    reason: str = "explicit"
    client_label: str = "unknown"


def _make_auth_dep(api_secret: str):
    def _check(authorization: str = Header(default="")) -> None:
        expected = f"Bearer {api_secret}"
        if authorization != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token-service credentials",
            )
    return _check


def _background_loop(manager: TokenManager, interval_sec: float, stop_event: threading.Event) -> None:
    log.info("token-service background loop start (interval=%.0fs)", interval_sec)
    while not stop_event.is_set():
        try:
            manager.background_tick()
        except Exception as exc:
            log.error("token-service background tick error: %s", exc)
        stop_event.wait(interval_sec)
    log.info("token-service background loop stop")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    manager = TokenManager(
        token_path=settings.token_path,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        refresh_margin_sec=settings.refresh_margin_sec,
        refresh_dedup_window_sec=settings.refresh_dedup_window_sec,
    )

    stop_event = threading.Event()
    bg_thread: threading.Thread | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal bg_thread
        bg_thread = threading.Thread(
            target=_background_loop,
            args=(manager, settings.background_check_interval_sec, stop_event),
            daemon=True,
            name="token-service-bg",
        )
        bg_thread.start()
        try:
            snapshot = manager.snapshot()
            if snapshot.access_token:
                left = max(0.0, snapshot.expires_at - time.time()) / 86400.0
                log.info(
                    "token-service: startup, token expires in %.1f days, last_pushed_by=%s",
                    left, snapshot.last_pushed_by or "<n/a>",
                )
            else:
                log.warning(
                    "token-service: startup, токен ОТСУТСТВУЕТ в %s — требуется fx-pro-auth",
                    settings.token_path,
                )
            yield
        finally:
            stop_event.set()
            if bg_thread:
                bg_thread.join(timeout=5)

    app = FastAPI(title="ctrader-token-service", lifespan=lifespan)
    require_auth = _make_auth_dep(settings.api_secret)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/status", dependencies=[Depends(require_auth)])
    def get_status() -> dict:
        snap = manager.snapshot()
        return {
            "has_token": bool(snap.access_token),
            "expires_at": snap.expires_at,
            "seconds_until_expiry": max(0.0, snap.expires_at - time.time()),
            "days_until_expiry": max(0.0, snap.expires_at - time.time()) / 86400.0,
            "last_refresh_ts": snap.last_refresh_ts,
            "last_pushed_by": snap.last_pushed_by,
            "last_pushed_ts": snap.last_pushed_ts,
            "seconds_since_last_push": (
                time.time() - snap.last_pushed_ts if snap.last_pushed_ts > 0 else None
            ),
        }

    @app.get("/token", dependencies=[Depends(require_auth)])
    def get_token() -> dict:
        try:
            data = manager.get()
        except RefreshError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not data.access_token:
            raise HTTPException(status_code=503, detail="no token loaded")
        return data.to_response()

    @app.post("/refresh", dependencies=[Depends(require_auth)])
    def force_refresh(body: RefreshBody) -> dict:
        reason = f"{body.reason} by {body.client_label}"
        try:
            data = manager.force_refresh(reason=reason)
        except RefreshError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not data.access_token:
            raise HTTPException(status_code=503, detail="no token after refresh")
        return data.to_response()

    @app.post("/token", dependencies=[Depends(require_auth)])
    def push_token(body: PushBody) -> dict:
        data = manager.push(
            access_token=body.access_token,
            refresh_token=body.refresh_token,
            expires_at=body.expires_at,
            client_label=body.client_label,
        )
        return data.to_response()

    return app
