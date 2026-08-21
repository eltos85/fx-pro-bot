"""Настройки horizon_bot. Префикс env задаёт роль контейнера.

DAYTREND_* — дневной SMA200. SWING_* — 4h SMA 20/50.
HORIZON_NAME выбирает, какой префикс читать (daytrend | swing).
"""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _prefix() -> str:
    name = os.environ.get("HORIZON_NAME", "daytrend").strip().lower()
    return "SWING_" if name == "swing" else "DAYTREND_"


class HorizonSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=_prefix(), extra="ignore")

    strategy: str = Field(default="sma200_daily")
    interval: str = Field(default="D")
    data_dir: str = Field(default="/data")
    log_level: str = Field(default="INFO")

    bybit_api_key: str = Field(default="")
    bybit_api_secret: str = Field(default="")
    bybit_demo: bool = Field(default=True)
    bybit_category: str = Field(default="linear")
    trading_enabled: bool = Field(default=True)

    symbols: str = Field(default="BTCUSDT,ETHUSDT")
    poll_sec: int = Field(default=300)
    # Доля капитала на один символ. Без плеча. Tharp: лучше недобрать, чем
    # рисковать ликвидацией на демо-счёте, общем со скальпом.
    position_frac: float = Field(default=0.15)
    # Решение пользователя 2026-08-21: считаем ставку от виртуальных $1000,
    # сколько бы ни лежало на общем демо. 0 = от живого счёта (старое поведение).
    virtual_capital: float = Field(default=1000.0)
    leverage: int = Field(default=1)
    min_notional_usd: float = Field(default=10.0)

    @property
    def bot_name(self) -> str:
        return os.environ.get("HORIZON_NAME", "daytrend").strip().lower()

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip() for s in self.symbols.split(",") if s.strip()]

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, f"{self.bot_name}_bot.sqlite")

    @property
    def link_prefix(self) -> str:
        return f"{self.bot_name}_"


def load_settings() -> HorizonSettings:
    name = os.environ.get("HORIZON_NAME", "daytrend").strip().lower()
    if name == "swing":
        os.environ.setdefault("SWING_STRATEGY", "sma20_50_4h")
        os.environ.setdefault("SWING_INTERVAL", "240")
        os.environ.setdefault("SWING_POLL_SEC", "180")
    else:
        os.environ.setdefault("DAYTREND_STRATEGY", "sma200_daily")
        os.environ.setdefault("DAYTREND_INTERVAL", "D")
        os.environ.setdefault("DAYTREND_POLL_SEC", "600")
    return HorizonSettings()
