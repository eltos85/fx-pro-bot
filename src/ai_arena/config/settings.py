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


# Asset universe 1-в-1 с Nof1 Alpha Arena Season 1 (gist line 62):
#   "Asset Universe: BTC, ETH, SOL, BNB, DOGE, XRP (perpetual contracts)"
# и в JSON output schema (gist line 157):
#   "coin": "BTC" | "ETH" | "SOL" | "BNB" | "DOGE" | "XRP"
# 6 фиксированных монет — часть архитектуры Nof1, не наш выбор.
# bybit_bot работает на mainnet, ai_arena — на отдельном demo-аккаунте,
# конфликта по позициям/SOL нет; правило изоляции экосистем
# (strategy-guard.mdc) не нарушается.
DEFAULT_ARENA_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "XRPUSDT",
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

    # Виртуальный капитал — номинальная метка для SYSTEM_PROMPT
    # ("Starting Capital: $X"). 1-в-1 с Nof1 каноном $10,000 USD.
    # См. правило ai-arena-sources.mdc § «Что МОЖНО менять» — virtual
    # capital должен совпадать с scaled equity (иначе LLM решит что
    # просел в 5-10× и будет в панике).
    virtual_capital_usd: float = Field(
        default=10000.0, validation_alias="AI_ARENA_VIRTUAL_CAPITAL"
    )

    # Делитель реального Bybit equity для масштабирования вниз перед
    # передачей в LLM-промпт. На demo-аккаунте Bybit стартовый баланс
    # ≈ $50k, делитель 5 → LLM видит ≈$10k (1-в-1 с Nof1 budget на
    # Hyperliquid, см. gist L62: «Starting Capital: $10,000 USD»).
    # Это единственное обоснованное отклонение от source — у Bybit
    # demo фиксированный $50k, на Hyperliquid Nof1 даёт ровно $10k.
    # Через scaling 1:5 LLM работает в $10k окне (= virtual_capital_usd).
    equity_scale_divisor: float = Field(
        default=5.0, validation_alias="AI_ARENA_EQUITY_SCALE_DIVISOR"
    )

    # Leverage cap — source говорит 1-20x (gist:
    # "Leverage Range: 1x to 20x"). Используется только для подстановки
    # в SYSTEM_PROMPT (текстом "1-20x"), серверного hard-checking нет.
    leverage_max: int = Field(default=20, validation_alias="AI_ARENA_LEVERAGE_MAX")

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
