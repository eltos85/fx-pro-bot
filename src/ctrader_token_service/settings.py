"""Конфигурация ctrader-token-service из ENV."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    token_path: Path
    client_id: str
    client_secret: str
    api_secret: str
    host: str = "0.0.0.0"
    port: int = 8080
    refresh_margin_sec: float = 86400.0
    refresh_dedup_window_sec: float = 5.0
    background_check_interval_sec: float = 3600.0


def load_settings() -> Settings:
    token_path = Path(os.environ.get("CTRADER_TOKEN_SERVICE_TOKEN_PATH", "/data/ctrader_tokens.json"))
    client_id = os.environ.get("CTRADER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "").strip()
    api_secret = os.environ.get("CTRADER_TOKEN_SERVICE_SECRET", "").strip()

    if not client_id or not client_secret:
        raise RuntimeError(
            "CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET обязательны для token-service"
        )
    if not api_secret:
        raise RuntimeError(
            "CTRADER_TOKEN_SERVICE_SECRET обязателен (HTTP-Bearer для ботов)"
        )

    return Settings(
        token_path=token_path,
        client_id=client_id,
        client_secret=client_secret,
        api_secret=api_secret,
        host=os.environ.get("CTRADER_TOKEN_SERVICE_HOST", "0.0.0.0"),
        port=int(os.environ.get("CTRADER_TOKEN_SERVICE_PORT", "8080")),
        refresh_margin_sec=float(
            os.environ.get("CTRADER_TOKEN_SERVICE_REFRESH_MARGIN_SEC", "86400")
        ),
        refresh_dedup_window_sec=float(
            os.environ.get("CTRADER_TOKEN_SERVICE_DEDUP_SEC", "5")
        ),
        background_check_interval_sec=float(
            os.environ.get("CTRADER_TOKEN_SERVICE_BG_INTERVAL_SEC", "3600")
        ),
    )
