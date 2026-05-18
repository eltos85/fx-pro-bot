"""Entrypoint: python -m ctrader_token_service."""
from __future__ import annotations

import logging
import os

import uvicorn

from ctrader_token_service.app import create_app
from ctrader_token_service.settings import load_settings


def main() -> None:
    log_level = os.environ.get("CTRADER_TOKEN_SERVICE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
