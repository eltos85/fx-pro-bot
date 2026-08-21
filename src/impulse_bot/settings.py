"""Настройки impulse-bot. Префикс IMPULSE_."""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ImpulseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IMPULSE_", extra="ignore")

    data_dir: str = Field(default="/data")
    log_level: str = Field(default="INFO")

    bybit_api_key: str = Field(default="")
    bybit_api_secret: str = Field(default="")
    bybit_demo: bool = Field(default=True)
    bybit_category: str = Field(default="linear")
    trading_enabled: bool = Field(default=True)

    # Bitcointalk 5577812: не BTC/ETH/SOL.
    skip_symbols: str = Field(default="BTCUSDT,ETHUSDT,SOLUSDT")
    # Тот же тред: оборот $100k–$15M, иначе удар не двигает цену.
    turnover_lo: float = Field(default=100_000)
    turnover_hi: float = Field(default=15_000_000)
    universe_cap: int = Field(default=40)

    # «$30k за 15с и сдвиг ≥0.2%». Поллинг ≈15с, дельта turnover24h ≈ влив.
    burst_usd: float = Field(default=30_000)
    burst_move_pct: float = Field(default=0.2)
    poll_sec: int = Field(default=15)

    # CScalp: лента подтверждает сторону удара.
    tape_sec: int = Field(default=15)
    tape_ratio: float = Field(default=1.2)

    # ForexFactory 1014708: цель больше издержки, не «крысиные 3 пункта».
    # VIP 0 RT 0.110% → тейк 0.45% (~4×), стоп 0.25%.
    tp_pct: float = Field(default=0.45)
    sl_pct: float = Field(default=0.25)
    scratch_sec: int = Field(default=90)

    # Smart-lab 963593: 1–2 часа, не сутки. FF: лондон.
    session_start_utc: int = Field(default=7)
    session_end_utc: int = Field(default=16)
    max_trades_session: int = Field(default=8)
    max_open: int = Field(default=1)

    # В журналах 20× руками. Автомат: риск 1.5% депо, плечо кап 10.
    risk_frac: float = Field(default=0.015)
    # Решение пользователя 2026-08-21: риск считаем от виртуальных $1000,
    # сколько бы ни лежало на общем демо. 0 = от живого счёта (старое поведение).
    virtual_capital: float = Field(default=1000.0)
    leverage: int = Field(default=10)
    min_notional_usd: float = Field(default=10.0)

    telegram_enabled: bool = Field(default=True)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    @property
    def skip_set(self) -> set[str]:
        return {s.strip().upper() for s in self.skip_symbols.split(",") if s.strip()}

    @property
    def db_path(self) -> str:
        return f"{self.data_dir}/impulse_bot.sqlite"


def load_settings() -> ImpulseSettings:
    s = ImpulseSettings()
    if not s.bybit_api_key:
        s.bybit_api_key = os.environ.get("SCALP_BYBIT_API_KEY", "")
    if not s.bybit_api_secret:
        s.bybit_api_secret = os.environ.get("SCALP_BYBIT_API_SECRET", "")
    if not s.telegram_bot_token:
        s.telegram_bot_token = os.environ.get("SCALP_TELEGRAM_BOT_TOKEN", "")
    if not s.telegram_chat_id:
        s.telegram_chat_id = os.environ.get("SCALP_TELEGRAM_CHAT_ID", "")
    demo = os.environ.get("IMPULSE_BYBIT_DEMO")
    if demo is None:
        scalp_demo = os.environ.get("SCALP_BYBIT_DEMO")
        if scalp_demo is not None:
            s.bybit_demo = scalp_demo.strip().lower() in ("1", "true", "yes")
    return s
