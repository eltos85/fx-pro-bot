"""Конфигурация AI Arena (Nof1 Alpha Arena clone на Bybit).

Все env-переменные с префиксом ``AI_ARENA_*``. Полностью изолирован от
существующего ``ai_trader`` (тот живёт под префиксом ``AI_TRADER_*``) —
два разных бота, две разные БД, два разных Bybit-аккаунта.

Параметры строго соответствуют публичной архитектуре Nof1 Alpha Arena
(см. правило ``.cursor/rules/ai-arena-sources.mdc``). Менять можно
только инфраструктурные адаптации (Bybit-специфичные round'инги,
Capital Safety hard-limits) — состав индикаторов / output schema /
action space / decision-цикл фиксированы под Nof1.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Asset universe Nof1 Season 1: BTC, ETH, SOL, BNB, DOGE, XRP (6 пар).
# У нас SOLUSDT занят bybit_bot scan_symbols (правило strategy-guard.mdc:
# изоляция экосистем). Поэтому в ai_arena 5 пар — без SOL.
DEFAULT_ARENA_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
)


class AiArenaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ─── DeepSeek API ────────────────────────────────────────────────────
    # V4-Pro = direct upgrade c V3.1 standard (Nof1 использует именно
    # standard, не reasoning — см. комментарий @wquguru в gist'е).
    # На `reasoning_effort=off` поведение V4-Pro совпадает с V3.1, но
    # на ~12-22% выше reasoning-бенчмарки (HuggingFace blog deepseekv4).
    #
    # ОТДЕЛЬНЫЙ ключ от ai_trader (env var AI_ARENA_DEEPSEEK_API_KEY).
    # Это даёт независимый rate-limit pool (rate-limit на DeepSeek
    # account-level) и независимый billing/audit. Если AI_ARENA_DEEPSEEK_API_KEY
    # пустой — бот выходит при старте с ошибкой.
    deepseek_api_key: str = Field(
        default="", validation_alias="AI_ARENA_DEEPSEEK_API_KEY"
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/anthropic",
        validation_alias="AI_ARENA_DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-pro",
        validation_alias="AI_ARENA_DEEPSEEK_MODEL",
    )
    deepseek_max_tokens: int = Field(
        default=8192, validation_alias="AI_ARENA_DEEPSEEK_MAX_TOKENS"
    )
    # Nof1 НЕ использует reasoning-mode (см. gist: «their documentation
    # explicitly mentions DeepSeek v3.1 — the standard version, not R1»).
    # CoT принуждается через required JSON-поля (justification/confidence/
    # invalidation_condition), а не через native reasoning.
    # Дефолт `off` = direct аналог V3.1 standard.
    deepseek_reasoning_effort: str = Field(
        default="off", validation_alias="AI_ARENA_DEEPSEEK_REASONING"
    )

    # ─── Bybit API (ОТДЕЛЬНЫЙ аккаунт от ai_trader / bybit_bot) ─────────
    bybit_api_key: str = Field(default="", validation_alias="AI_ARENA_BYBIT_API_KEY")
    bybit_api_secret: str = Field(
        default="", validation_alias="AI_ARENA_BYBIT_API_SECRET"
    )
    bybit_demo: bool = Field(default=True, validation_alias="AI_ARENA_BYBIT_DEMO")
    bybit_category: str = Field(
        default="linear", validation_alias="AI_ARENA_BYBIT_CATEGORY"
    )

    # ─── Trading cycle ───────────────────────────────────────────────────
    symbols_raw: str = Field(
        default=",".join(DEFAULT_ARENA_SYMBOLS),
        validation_alias="AI_ARENA_SYMBOLS",
    )
    # Nof1: «Decision Frequency: Every 2-3 minutes». Стартуем с 3 мин.
    # Увеличить до 300 если упрёмся в latency LLM или Bybit rate-limits.
    poll_interval_sec: int = Field(
        default=180, validation_alias="AI_ARENA_POLL_INTERVAL_SEC"
    )

    # Виртуальный капитал — наша sandbox-граница (Nof1 = $10k, у нас $500).
    # Используется для: notional cap = virtual_capital × leverage.
    virtual_capital_usd: float = Field(
        default=500.0, validation_alias="AI_ARENA_VIRTUAL_CAPITAL"
    )

    # ─── Capital Safety (hard infrastructure — не часть Nof1-стратегии) ──
    # Cap leverage 5x вместо Nof1 20x — наш sandbox safety. Conviction →
    # leverage mapping в SYSTEM_PROMPT адаптирован под этот cap.
    max_daily_loss_usd: float = Field(
        default=50.0, validation_alias="AI_ARENA_MAX_DAILY_LOSS"
    )
    max_total_loss_usd: float = Field(
        default=200.0, validation_alias="AI_ARENA_MAX_TOTAL_LOSS"
    )
    max_open_positions: int = Field(
        default=3, validation_alias="AI_ARENA_MAX_POSITIONS"
    )
    max_leverage: int = Field(default=5, validation_alias="AI_ARENA_MAX_LEVERAGE")
    max_risk_per_trade_usd: float = Field(
        default=10.0, validation_alias="AI_ARENA_MAX_RISK_PER_TRADE"
    )
    # R:R ≥ 1.5 hard-check на parser-уровне. В SYSTEM_PROMPT тоже
    # явно сказано "if your idea has R:R < 1.5, bot will reject; return HOLD".
    min_risk_reward_ratio: float = Field(
        default=1.5, validation_alias="AI_ARENA_MIN_RR"
    )

    # ─── Storage ─────────────────────────────────────────────────────────
    data_dir: str = Field(default="/data", validation_alias="AI_ARENA_DATA_DIR")
    db_filename: str = Field(
        default="ai_arena.sqlite", validation_alias="AI_ARENA_DB_FILENAME"
    )

    # ─── Telegram (ОТДЕЛЬНЫЙ токен от ai_trader для изоляции UX) ─────────
    # Если AI_ARENA_TELEGRAM_BOT_TOKEN не задан — TG-модуль молчит.
    telegram_bot_token: str = Field(
        default="", validation_alias="AI_ARENA_TELEGRAM_BOT_TOKEN"
    )
    telegram_chat_id_raw: str = Field(
        default="", validation_alias="AI_ARENA_TELEGRAM_CHAT_ID"
    )
    telegram_enabled: bool = Field(
        default=True, validation_alias="AI_ARENA_TELEGRAM_ENABLED"
    )

    # ─── Misc ────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", validation_alias="AI_ARENA_LOG_LEVEL")
    # PAPER mode by default. В LIVE — только после убеждения что
    # промпт валиден, парсер работает, latency приемлемая (см. План §9, D12).
    trading_enabled: bool = Field(
        default=False, validation_alias="AI_ARENA_TRADING_ENABLED"
    )

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
