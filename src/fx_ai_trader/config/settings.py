"""Конфигурация FX AI Trader.

Все env-переменные с префиксом ``AI_FX_TRADER_*``. Не пересекается ни с
``BYBIT_BOT_*`` (Bybit-бот), ни с ``AI_TRADER_*`` (Bybit AI-агент), ни с
``CTRADER_*`` (Advisor cTrader). Изоляция настроек = изоляция ботов.

Phase 1 — paper-mode MVP на gold (XAUUSD spot) + oil (BZ=F → BRENT) через
cTrader FxPro demo. dual-timer 15 мин full / 5 мин review.

Параметры заморожены на ≥14 дней paper-observation (правило
``no-data-fitting.mdc``); менять = curve-fitting, требует перезапуска
эксперимента с n=0.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Дефолтные инструменты Phase 1.
# - XAUUSD: spot gold CFD (от Advisor GC=F futures отделён на cTrader как
#   разный symbolId → полная broker-side изоляция).
# - BZ=F: Brent crude в yfinance-нотации; в SymbolCache маппится на cTrader
#   "BRENT" (см. src/fx_pro_bot/trading/symbols.py).
DEFAULT_AI_FX_SYMBOLS: tuple[str, ...] = ("XAUUSD", "BZ=F")


class AiFxTraderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ─── DeepSeek API ────────────────────────────────────────────────────
    # Шарится тот же ключ что у ai_trader — это API-key одного провайдера,
    # rate-limit аккаунта, не per-bot. Оба бота могут использовать ключ
    # параллельно.
    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/anthropic",
        validation_alias="AI_FX_TRADER_DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="AI_FX_TRADER_DEEPSEEK_MODEL",
    )
    # max_tokens=8000: full-cycle output = thinking-блок (DeepSeek-V4
    # внутренний reasoning) + commentary (4–8 строк) + JSON c multi-dim
    # sentiment блоком (~300–600 токенов). С 4096 наблюдался ``out=4096``
    # и оборванный JSON («no JSON object with 'action' found»). 8K даёт
    # запас в ~×2 без заметного удара по стоимости (~$0.0028/full cycle
    # по DeepSeek-V4 pricing). Anthropic-compat API DeepSeek поддерживает
    # max_tokens до 8192 (см. api-docs.deepseek.com Anthropic guide).
    deepseek_max_tokens: int = Field(
        default=8000, validation_alias="AI_FX_TRADER_DEEPSEEK_MAX_TOKENS"
    )
    deepseek_thinking_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_DEEPSEEK_THINKING"
    )

    # ─── cTrader API (FxPro demo) ────────────────────────────────────────
    # OAuth-токены **изолированы** от Advisor (с 2026-05-12 после
    # инцидента с single-use refresh_token rotation, BUILDLOG.md
    # «token rotation hardening»). У каждого бота свой OAuth grant +
    # свой token-файл — refresh одного не задевает другого.
    # Сам client_id / client_secret общие (это credentials cTrader
    # приложения, не пары access/refresh).
    ctrader_client_id: str = Field(
        default="", validation_alias="CTRADER_CLIENT_ID"
    )
    ctrader_client_secret: str = Field(
        default="", validation_alias="CTRADER_CLIENT_SECRET"
    )
    ctrader_account_id: int = Field(
        default=0, validation_alias="CTRADER_ACCOUNT_ID"
    )
    ctrader_host_type: str = Field(
        default="demo", validation_alias="CTRADER_HOST_TYPE"
    )
    # Путь к JSON с access/refresh токенами — отдельный от Advisor'а
    # (/data/ctrader_tokens.json). Полная изоляция OAuth: refresh
    # одного бота не задевает refresh другого. Перед первым стартом
    # нужно пройти OAuth-flow и положить токены в этот файл (через
    # отдельный fx-pro-auth -> exchange_code_for_tokens + atomic save).
    ctrader_token_path: str = Field(
        default="/data/ctrader_tokens_ai_fx.json",
        validation_alias="AI_FX_TRADER_CTRADER_TOKEN_PATH",
    )

    # ─── Trading ─────────────────────────────────────────────────────────
    # Список инструментов в yfinance-нотации (XAUUSD, BZ=F). Внутренний
    # клиент-адаптер мапит их в cTrader (XAUUSD остаётся "XAUUSD", BZ=F → "BRENT").
    symbols_raw: str = Field(
        default=",".join(DEFAULT_AI_FX_SYMBOLS),
        validation_alias="AI_FX_TRADER_SYMBOLS",
    )

    # Label для broker-side изоляции от Advisor (label="fx-pro-bot").
    # cTrader ProtoOANewOrderReq.label = string ≤100 chars (см. OpenAPI
    # forum 41177, FAQ). Reconcile ИГНОРИРУЕТ позиции с чужим label,
    # ни один бот не закроет чужую позицию как orphan.
    order_label: str = Field(
        default="ai-fx-trader",
        validation_alias="AI_FX_TRADER_ORDER_LABEL",
    )

    # Dual-timer: full-cycle делает полный анализ + может open/close/hold,
    # review-cycle только следит за уже открытыми позициями (close/hold,
    # без open) — даёт LLM в 3× больше точек реакции на adverse evidence.
    poll_interval_sec: int = Field(
        default=900, validation_alias="AI_FX_TRADER_POLL_INTERVAL_SEC"
    )
    review_interval_sec: int = Field(
        default=300, validation_alias="AI_FX_TRADER_REVIEW_INTERVAL_SEC"
    )

    # Виртуальный капитал для расчёта lot size в промпте. Демо FxPro
    # обычно $1000–$5000, но мы хотим эмулировать поведение на $500
    # (как у ai_trader). Все размеры считаются как будто это $500.
    virtual_capital_usd: float = Field(
        default=500.0, validation_alias="AI_FX_TRADER_VIRTUAL_CAPITAL"
    )

    # ─── Broker safety (катастрофические лимиты, НЕ micro-management) ───
    # Философия v1.0 (12-May-2026): LLM получает свободу профессионального
    # discretionary trader (Mark Douglas / Van Tharp). R:R / risk-per-trade
    # / correlation haircut — это его собственное решение по setup'у,
    # НЕ наши hard caps.
    # Здесь оставлены ТОЛЬКО три класса защиты:
    #   1. catastrophic daily/total loss (stop-experiment, не tuning).
    #   2. broker margin safety (max_lot_size, max_open_positions).
    #   3. anti-hallucination gate (sentiment uncertainty > 0.7 в executor).
    max_daily_loss_usd: float = Field(
        default=150.0, validation_alias="AI_FX_TRADER_MAX_DAILY_LOSS"
    )
    max_total_loss_usd: float = Field(
        default=300.0, validation_alias="AI_FX_TRADER_MAX_TOTAL_LOSS"
    )
    # Общий cap на количество одновременно открытых позиций (broker
    # margin sanity). LLM может разместить максимум 3 позиции в любой
    # комбинации gold/oil. Это НЕ ограничение стратегии (LLM сам решит
    # коррелировать ли gold-long с oil-long), а защита от runaway
    # open-loop, когда LLM из-за бага начнёт открывать каждый цикл.
    max_open_positions: int = Field(
        default=3, validation_alias="AI_FX_TRADER_MAX_POSITIONS"
    )
    # На один инструмент максимум 3 позиции (= общий лимит). Снято
    # ограничение «=2 для защиты от over-allocation»: LLM сам решит
    # сколько накопить на gold vs oil. Hard cap идёт через
    # max_open_positions.
    max_positions_per_symbol: int = Field(
        default=3, validation_alias="AI_FX_TRADER_MAX_POSITIONS_PER_SYMBOL"
    )
    # Hard cap на lot size — broker margin safety. На demo $1500 и
    # типичной XAUUSD margin requirement ~$3000/lot (5% margin), 0.5 лот
    # = ~$1500 margin = весь капитал. Это catastrophic limit: даже если
    # LLM попросит 5 лотов, executor режет до 0.5.
    max_lot_size: float = Field(
        default=0.50, validation_alias="AI_FX_TRADER_MAX_LOT_SIZE"
    )

    # ─── Storage ─────────────────────────────────────────────────────────
    data_dir: str = Field(default="/data", validation_alias="AI_FX_TRADER_DATA_DIR")
    db_filename: str = Field(
        default="fx_ai_trader.sqlite", validation_alias="AI_FX_TRADER_DB_FILENAME"
    )

    # ─── News (RSS + EIA) ────────────────────────────────────────────────
    news_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_NEWS_ENABLED"
    )
    news_max_age_hours: int = Field(
        default=12, validation_alias="AI_FX_TRADER_NEWS_MAX_AGE_HOURS"
    )
    news_max_items_per_symbol: int = Field(
        default=5, validation_alias="AI_FX_TRADER_NEWS_MAX_ITEMS_PER_SYMBOL"
    )
    # EIA Open Data API key (бесплатная регистрация на eia.gov).
    # Пустой ключ → EIA-блок отключается, RSS остаётся.
    eia_api_key: str = Field(default="", validation_alias="AI_FX_TRADER_EIA_API_KEY")
    eia_cache_ttl_sec: int = Field(
        default=21600, validation_alias="AI_FX_TRADER_EIA_CACHE_TTL_SEC"
    )  # 6 часов

    # ─── Misc ────────────────────────────────────────────────────────────
    log_level: str = Field(
        default="INFO", validation_alias="AI_FX_TRADER_LOG_LEVEL"
    )
    # Если False — только логируем decisions, реально ордера на cTrader НЕ
    # ставим. Phase 1 = paper-mode минимум 14 дней (research: NexusTrade
    # 2026 «30–90 days paper before live», Kiploks «weeks not hours»).
    trading_enabled: bool = Field(
        default=False, validation_alias="AI_FX_TRADER_TRADING_ENABLED"
    )

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.symbols_raw.split(",") if s.strip())

    @property
    def db_path(self) -> str:
        from pathlib import Path

        return str(Path(self.data_dir) / self.db_filename)
