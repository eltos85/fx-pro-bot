"""Настройки solana-bot. Префикс SOLANA_."""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SolanaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOLANA_", extra="ignore")

    data_dir: str = Field(default="/data")
    log_level: str = Field(default="INFO")

    # Выключен по решению 2026-08-24: не сканит и не пишет в TG.
    enabled: bool = Field(default=False)
    # На VPS ключа может не быть — скан крутится без свапов.
    trading_enabled: bool = Field(default=False)
    private_key: str = Field(default="")
    rpc_url: str = Field(default="https://api.mainnet-beta.solana.com")
    jupiter_api_key: str = Field(default="")

    # Teletype lexdollar: объём ≥$100k / 5 мин.
    volume_m5_usd: float = Field(default=100_000)
    # Ход за 5 мин — операционный пол «щиток уже пошёл», не бэктест.
    move_m5_pct: float = Field(default=5.0)
    # Риск-капы: в источнике нет точных чисел.
    min_liquidity_usd: float = Field(default=25_000)
    min_age_sec: int = Field(default=1800)

    # Цели +7% / кап +30% (Teletype). SL −12% — риск-кап, в посте нет.
    tp_pct: float = Field(default=7.0)
    cap_pct: float = Field(default=30.0)
    sl_pct: float = Field(default=12.0)

    poll_sec: int = Field(default=30)
    max_open: int = Field(default=1)
    size_sol: float = Field(default=0.05)
    max_size_sol: float = Field(default=0.20)
    slippage_bps: int = Field(default=150)

    skip_mints: str = Field(default="")

    telegram_enabled: bool = Field(default=False)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")
    telegram_cooldown_sec: int = Field(default=1800)

    @property
    def skip_set(self) -> set[str]:
        return {s.strip() for s in self.skip_mints.split(",") if s.strip()}

    @property
    def db_path(self) -> str:
        return f"{self.data_dir}/solana_bot.sqlite"


def load_settings() -> SolanaSettings:
    s = SolanaSettings()
    if not s.telegram_bot_token:
        s.telegram_bot_token = os.environ.get("SCALP_TELEGRAM_BOT_TOKEN", "")
    if not s.telegram_chat_id:
        s.telegram_chat_id = os.environ.get("SCALP_TELEGRAM_CHAT_ID", "")
    return s
