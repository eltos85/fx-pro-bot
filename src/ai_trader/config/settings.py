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
# нет ли коллизии с активными парами основного бота, иначе одну пару
# будут параллельно вести оба бота, и непонятно чьи открытые позиции
# принадлежат кому (нет общего ledger).
#
# v0.4 (2026-05-07): расширили с 5 до 10. Добавлены:
#   - AVAXUSDT, LTCUSDT, ATOMUSDT — крупные L1 с разными нарративами
#     (Avalanche subnets, digital silver / mining, Cosmos hub / IBC).
#   - WLDUSDT, TAOUSDT — narrative-плеи 2025-2026 (Worldcoin / identity,
#     Bittensor / decentralized AI). Дают LLM-агенту возможность
#     отыгрывать AI-флоу без перекрытия с bybit_bot.
DEFAULT_AI_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "ATOMUSDT",
    "WLDUSDT",
    "TAOUSDT",
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
        default=4096, validation_alias="AI_TRADER_DEEPSEEK_MAX_TOKENS"
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
    # v0.3 (AUDIT_2026.md P1): risk-per-trade 5% → 2%, паритет с industry
    # standard 2026 (KuCoin Risk Management 2026, Atlas Peak Research,
    # Hyper-Quant: 1–2% — mainstream consensus, 5% соответствует full Kelly
    # с edge ~10% и опасен из-за drawdown-риска).
    # При risk-per-trade 2% ($10) и max-pos 3:
    # - $50/день = 5 убыточных сделок до блока
    # - $200 total = 40% virtual capital, кончается раньше «доедания депо»
    max_daily_loss_usd: float = Field(
        default=50.0, validation_alias="AI_TRADER_MAX_DAILY_LOSS"
    )
    max_total_loss_usd: float = Field(
        default=200.0, validation_alias="AI_TRADER_MAX_TOTAL_LOSS"
    )
    max_open_positions: int = Field(
        default=5, validation_alias="AI_TRADER_MAX_POSITIONS"
    )
    # 3 → 5 (2026-05-07): пул пар расширен с 5 до 10, увеличиваем
    # одновременную ёмкость пропорционально (50% пар = типичный режим
    # «несколько setup'ов одновременно»). Risk-per-trade остаётся 2%
    # ($10), значит max realised drawdown за один цикл = 5×$10 = $50,
    # ровно равен `max_daily_loss_usd`. Дальше — killswitch.
    max_leverage: int = Field(default=5, validation_alias="AI_TRADER_MAX_LEVERAGE")
    # Risk per trade в долях (0.02 = 2%). Используется LLM в промпте + для
    # будущих helper-функций position sizing.
    risk_per_trade_pct: float = Field(
        default=0.02, validation_alias="AI_TRADER_RISK_PER_TRADE"
    )

    # ─── Storage ─────────────────────────────────────────────────────────
    data_dir: str = Field(default="/data", validation_alias="AI_TRADER_DATA_DIR")
    db_filename: str = Field(
        default="ai_trader.sqlite", validation_alias="AI_TRADER_DB_FILENAME"
    )

    # ─── Telegram ────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(
        default="", validation_alias="TELEGRAM_BOT_TOKEN"
    )
    # Принимаем chat_id как строку: пустая = None (auto-detect),
    # любое число = фиксированный chat_id. pydantic int|None ломается на
    # пустой строке из docker-compose ENV interpolation.
    telegram_chat_id_raw: str = Field(
        default="", validation_alias="TELEGRAM_CHAT_ID"
    )
    telegram_enabled: bool = Field(
        default=True, validation_alias="AI_TRADER_TELEGRAM_ENABLED"
    )

    # ─── News ────────────────────────────────────────────────────────────
    news_enabled: bool = Field(
        default=True, validation_alias="AI_TRADER_NEWS_ENABLED"
    )
    news_max_age_hours: int = Field(
        default=6, validation_alias="AI_TRADER_NEWS_MAX_AGE_HOURS"
    )
    news_max_items: int = Field(
        default=8, validation_alias="AI_TRADER_NEWS_MAX_ITEMS"
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

    @property
    def telegram_chat_id(self) -> int | None:
        v = self.telegram_chat_id_raw.strip()
        if not v:
            return None
        try:
            return int(v)
        except ValueError:
            return None
