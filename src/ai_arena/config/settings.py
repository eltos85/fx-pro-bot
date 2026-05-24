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
    #
    # Допустимые значения: ``off|high|max`` (см. llm/client.py
    # ``_build_thinking_extra_body``). DeepSeek V4 thinking enabled by
    # default — на ``off`` мы ЯВНО отправляем ``{"thinking": {"type":
    # "disabled"}}``, иначе модель всё равно генерирует CoT-блок
    # (до 2026-05-18 это был баг: значение игнорировалось, см. BUILDLOG).
    # Дефолт `off` = соответствие Nof1 (single-turn без reasoning).
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
    # v2.z2 user-approved exception #3 (2026-05-22): cycle 180→600s.
    # Nof1 SYSTEM_PROMPT canonical: «Decision Frequency: Every 2-3 minutes»
    # — оставляем 1-в-1 (синтаксис gist'а, LLM не использует это для
    # числовых вычислений). Реальный poll_interval = **600s (10 мин)** —
    # обоснование в `.cursor/rules/ai-arena-sources.mdc` § «Допустимые
    # исключения» (исключение #3). Bybit/Hyperliquid difference: Nof1
    # запускал 4 модели параллельно (gpt-5/grok/deepseek/qwen) на 180s
    # heartbeat ради сравнения throughput; у нас 1 модель в forward-test,
    # дешевле/качественнее с расширенным cycle. Также post-v2.y observed:
    # LLM открывает позицию → закрывает через 30-90 мин по «передумал»
    # на 3-минутном noise. Cycle 600s даёт reasoning больше времени на
    # формирование setup'а и снижает re-decision-frequency.
    # Возврат к 180 — установить `AI_ARENA_POLL_INTERVAL_SEC=180`.
    poll_interval_sec: int = Field(
        default=600, validation_alias="AI_ARENA_POLL_INTERVAL_SEC"
    )

    # Виртуальный капитал — sandbox-обманка для LLM. Единственное
    # обоснованное отклонение от Nof1 ($10k у source). Bybit demo
    # выдаёт фиксированный $50k, мы хотим обкатывать на $1k → LLM
    # видит $1000 «начального капитала», а **реальный PnL в $$
    # прибавляется/вычитается напрямую** (offset-based, не divisor):
    #
    #   scaled_equity = virtual_capital + (real_equity - real_at_start)
    #
    # Quantities в sandbox-вселенной = quantities на Bybit (исполняются
    # как есть). Real PnL не масштабируется — это уже малая абсолютная
    # сумма, и она 1-в-1 отражается в sandbox.
    #
    # Например: real_at_start=$50000, real_now=$50007.32
    #   → scaled_equity = $1000 + $7.32 = $1007.32
    virtual_capital_usd: float = Field(
        default=1000.0, validation_alias="AI_ARENA_VIRTUAL_CAPITAL"
    )

    # DEPRECATED. Раньше использовался для divisor-scaling
    # (scaled = real / divisor), но это давало некорректную семантику
    # PnL: реальный профит +$7.32 → виртуальный +$0.15. Сейчас
    # используется offset-based формула с anchor `real_equity_at_start`
    # (в kv_state). Поле оставлено для обратной совместимости с .env;
    # значение игнорируется. Удалить после 2026-06.
    equity_scale_divisor: float = Field(
        default=1.0, validation_alias="AI_ARENA_EQUITY_SCALE_DIVISOR"
    )

    # Leverage cap — source говорит 1-20x (gist:
    # "Leverage Range: 1x to 20x"). Используется только для подстановки
    # в SYSTEM_PROMPT (текстом "1-20x"), серверного hard-checking нет.
    leverage_max: int = Field(default=20, validation_alias="AI_ARENA_LEVERAGE_MAX")

    # v2.z3 user-approved exception #4 (2026-05-22): server-side notional
    # cap для одной позиции = `max_allocation_pct × virtual_capital_usd`.
    # При virtual_capital=$10000 и cap=0.30 → max notional = $3000 на
    # позицию (3 одновременных позиции по равным долям = ~90% allocation).
    #
    # Поведение: silent rescale + log в next prompt. Если LLM запрашивает
    # qty с notional > cap → qty уменьшается до cap, факт фиксируется в
    # kv_state ("pending_rescale_notice") и показывается ОДИН раз в
    # следующем USER_PROMPT блоком "System notice". После показа — clear.
    #
    # Обоснование (post-v2.y observed): SOLUSDT #33 — qty 545 × $87.14 =
    # $47,491 notional на $10k virtual capital (4.7× equity exposure).
    # Move 0.08% × такая позиция = $90 loss за 28 минут. Cap решает эту
    # архитектурную проблему за пределами LLM-prompt'а (поскольку
    # SYSTEM_PROMPT canonical и менять нельзя).
    #
    # См. правило `.cursor/rules/ai-arena-sources.mdc` § «Допустимые
    # исключения» исключение #4 и BUILDLOG_AI_ARENA.md v2.z3.
    # Откат: `AI_ARENA_MAX_ALLOCATION_PCT=1.0` отключит cap (qty * price
    # ≤ 100% × virtual_capital = $10000 — фактически no-cap).
    max_allocation_pct: float = Field(
        default=0.30, validation_alias="AI_ARENA_MAX_ALLOCATION_PCT"
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
