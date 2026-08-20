"""Настройки hybrid_bot (префикс env HYBRID_)."""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HybridSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HYBRID_", extra="ignore")

    data_dir: str = Field(default="/data")
    log_level: str = Field(default="INFO")

    bybit_api_key: str = Field(default="")
    bybit_api_secret: str = Field(default="")
    bybit_demo: bool = Field(default=True)
    bybit_category: str = Field(default="linear")
    # Наблюдение сначала: без явного включения бот считает и пишет, но не
    # отправляет ордера (в БД сделки помечаются mode=paper).
    trading_enabled: bool = Field(default=False)

    # Замер сделан на эфире (STRATEGY_HYBRID.md §17), поэтому он и по умолчанию.
    symbols: str = Field(default="ETHUSDT")
    interval: str = Field(default="240")
    poll_sec: int = Field(default=180)

    # Расстояние от средней цены входа, на котором закрываем позицию целиком.
    # §17.6: на 1460 днях эфира крупный порог даёт лучший итог и меньше
    # комиссий (+6% → +$2300, +0.5% → −$8652). Значение согласуется с
    # пользователем, поэтому торговля по умолчанию выключена.
    fix_threshold_pct: float = Field(default=6.0)
    # Объём позиции. Фиксированный нотионал: в §17.6 считалось именно так, и
    # это единственный способ, при котором размер закрытия предсказуем
    # (порог × объём).
    position_usd: float = Field(default=7000.0)
    min_notional_usd: float = Field(default=10.0)
    leverage: int = Field(default=1)

    telegram_enabled: bool = Field(default=False)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")
    telegram_prefix: str = Field(default="[hybrid]")

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip() for s in self.symbols.split(",") if s.strip()]

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "hybrid_bot.sqlite")

    @property
    def link_prefix(self) -> str:
        return "hybrid_"

    @property
    def trade_mode(self) -> str:
        return "live" if self.trading_enabled else "paper"


def load_settings() -> HybridSettings:
    return HybridSettings()
