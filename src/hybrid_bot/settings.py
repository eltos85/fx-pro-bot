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
    # Решение пользователя 2026-08-20: воспроизводим наблюдённое поведение, а не
    # оптимальное. В разобранных сделках порога не было вовсе — закрывал чужой
    # бот, и расстояния вышли +0.62%…+6.17% с серединным +0.91% (§17.3). +1% —
    # это оно. Известная плата: 15 закрытий в месяц против 2 при +6%, и по
    # истории итог отрицательный из-за комиссий (§18.5).
    fix_threshold_pct: float = Field(default=1.0)
    # Капитал, которым бот считает себя ограниченным. На демо-счёте лежит
    # больше, но бот работает так, будто у него $1000: суммарный объём его
    # позиций эту величину не превышает.
    virtual_capital: float = Field(default=1000.0)
    # Базовая ставка при реализованной воле 40% годовых (MOP 2012, §16.2).
    # Живой размер = base × min(3, 0.40 / vol). Потолок 3× и остаток
    # virtual_capital. Окно волы — 60 дней 4h, не подбирать.
    position_usd: float = Field(default=200.0)
    min_notional_usd: float = Field(default=10.0)
    leverage: int = Field(default=1)
    # Ставка taker для linear без VIP. Используется только в режиме наблюдения,
    # где фактических комиссий нет: иначе бумажные цифры выглядели бы лучше
    # живых. В торговле комиссия берётся из исполнения.
    # https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate
    taker_fee: float = Field(default=0.00055)

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
