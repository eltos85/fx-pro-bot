"""Конфигурация AI-Trader.

Все env-переменные с префиксом AI_TRADER_*. Не пересекается с переменными
основного Bybit-бота (BYBIT_BOT_*) и FxPro-бота.

Параметры эксперимента ЗАФИКСИРОВАНЫ на 14 дней (см. BUILDLOG_AI_TRADER.md):
менять промпт или лимиты на лету = curve-fitting, требует перезапуска n=0.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Пары для AI-трейдера. ВАЖНО: не пересекаются с bybit_bot scan_symbols
# (SOL, ADA, LINK, SUI, TON, WIF, TIA, DOT). Если поменяешь — проверь
# нет ли коллизии с активными парами основного бота.
DEFAULT_AI_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
)


class AiTraderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ─── DeepSeek API ────────────────────────────────────────────────────
    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/anthropic",
        validation_alias="AI_TRADER_DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="AI_TRADER_DEEPSEEK_MODEL",
    )
    deepseek_max_tokens: int = Field(
        default=2000, validation_alias="AI_TRADER_DEEPSEEK_MAX_TOKENS"
    )
    deepseek_thinking_enabled: bool = Field(
        default=True, validation_alias="AI_TRADER_DEEPSEEK_THINKING"
    )

    # ─── Bybit API ───────────────────────────────────────────────────────
    bybit_api_key: str = Field(default="", validation_alias="AI_TRADER_BYBIT_API_KEY")
    bybit_api_secret: str = Field(
        default="", validation_alias="AI_TRADER_BYBIT_API_SECRET"
    )
    bybit_demo: bool = Field(default=True, validation_alias="AI_TRADER_BYBIT_DEMO")
    bybit_category: str = Field(
        default="linear", validation_alias="AI_TRADER_BYBIT_CATEGORY"
    )

    # ─── Trading ─────────────────────────────────────────────────────────
    symbols_raw: str = Field(
        default=",".join(DEFAULT_AI_SYMBOLS),
        validation_alias="AI_TRADER_SYMBOLS",
    )
    poll_interval_sec: int = Field(
        default=900, validation_alias="AI_TRADER_POLL_INTERVAL_SEC"
    )  # 15 минут

    # Виртуальный капитал для расчёта qty. На demo баланс может быть $50k+,
    # но мы хотим эмулировать поведение на $500. Все qty считаются как
    # будто капитал ровно столько.
    virtual_capital_usd: float = Field(
        default=500.0, validation_alias="AI_TRADER_VIRTUAL_CAPITAL"
    )

    # ─── KillSwitch ──────────────────────────────────────────────────────
    max_daily_loss_usd: float = Field(
        default=50.0, validation_alias="AI_TRADER_MAX_DAILY_LOSS"
    )
    max_total_loss_usd: float = Field(
        default=200.0, validation_alias="AI_TRADER_MAX_TOTAL_LOSS"
    )
    max_open_positions: int = Field(
        default=3, validation_alias="AI_TRADER_MAX_POSITIONS"
    )
    max_leverage: int = Field(default=5, validation_alias="AI_TRADER_MAX_LEVERAGE")

    # ─── Storage ─────────────────────────────────────────────────────────
    data_dir: str = Field(default="/data", validation_alias="AI_TRADER_DATA_DIR")
    db_filename: str = Field(
        default="ai_trader.sqlite", validation_alias="AI_TRADER_DB_FILENAME"
    )

    # ─── Misc ────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", validation_alias="AI_TRADER_LOG_LEVEL")
    trading_enabled: bool = Field(
        default=False, validation_alias="AI_TRADER_TRADING_ENABLED"
    )
    # Если False — только логируем decisions, реально ордера не ставим.
    # Полезно для первого запуска: убеждаемся что промпты валидные и
    # парсер работает, прежде чем разрешить торговлю.

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.symbols_raw.split(",") if s.strip())

    @property
    def db_path(self) -> str:
        from pathlib import Path

        return str(Path(self.data_dir) / self.db_filename)
