"""Конфигурация FX AI Trend (Trend-follower).

Все env-переменные с префиксом ``AI_FX_TREND_*``. Не пересекается ни с
``AI_FX_TRADER_*`` (Discretionary LLM), ни с ``BYBIT_BOT_*`` (Bybit-бот),
ни с ``AI_TRADER_*`` (Bybit AI-агент), ни с ``CTRADER_*`` (Advisor).
Изоляция настроек = изоляция ботов.

Phase 1 — paper-mode MVP на gold (XAUUSD spot) + Brent oil (BZ=F →
BRENT) + natural gas (NG=F → NAT.GAS) через cTrader FxPro demo.
dual-timer 15 мин full / 5 мин review (тот же тайминг что и у
Discretionary — даёт LLM реакцию на adverse evidence до broker SL/TP).

Параметры заморожены на ≥14 дней paper-observation (правило
``no-data-fitting.mdc``); менять = curve-fitting, требует перезапуска
эксперимента с n=0.

Trend-follower по своей природе:
- Win-rate 30-45% (vs 50%+ для Discretionary) — это **OK**.
- Drawdowns 20-30% — это **OK** (Clenow «Following the Trend»).
- Hold horizon: weeks to months, не sub-day.
- Profit factor 1.8-2.2 на CTA-индустрии (Coriva 2026, SG CTA Index).
Поэтому KillSwitch caps шире чем у Discretionary: total_loss = $500
(vs $300 у Discretionary), чтобы не отключиться на нормальной CTA-
просадке. Daily loss остаётся $150 (тот же uppercap).
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Trend-follower торгует тот же 3-asset commodity-универс что и
# Discretionary (XAUUSD/BZ=F/NG=F) — это enable'ит чистый A/B
# эксперимент. Pip-value и symbol mapping одинаковые (расчёт уже
# verified в fx_ai_trader, см. BUILDLOG_AI_FX_TRADER.md).
DEFAULT_AI_FX_SYMBOLS: tuple[str, ...] = ("XAUUSD", "BZ=F", "NG=F")


class AiFxTrendSettings(BaseSettings):
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
        validation_alias="AI_FX_TREND_DEEPSEEK_BASE_URL",
    )
    deepseek_model: str = Field(
        default="deepseek-v4-flash",
        validation_alias="AI_FX_TREND_DEEPSEEK_MODEL",
    )
    # max_tokens=8000: full-cycle output = thinking-блок (DeepSeek-V4
    # внутренний reasoning) + commentary (4–8 строк) + JSON c multi-dim
    # sentiment блоком (~300–600 токенов). С 4096 наблюдался ``out=4096``
    # и оборванный JSON («no JSON object with 'action' found»). 8K даёт
    # запас в ~×2 без заметного удара по стоимости (~$0.0028/full cycle
    # по DeepSeek-V4 pricing). Anthropic-compat API DeepSeek поддерживает
    # max_tokens до 8192 (см. api-docs.deepseek.com Anthropic guide).
    deepseek_max_tokens: int = Field(
        default=8000, validation_alias="AI_FX_TREND_DEEPSEEK_MAX_TOKENS"
    )
    deepseek_thinking_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TREND_DEEPSEEK_THINKING"
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
    # Token path = fallback ТОЛЬКО когда CTRADER_TOKEN_SERVICE_URL пуст.
    # На VPS работаем через ctrader-token-service (single source of
    # truth), общий файл /data/ctrader_tokens.json не пишется ботом.
    # См. src/ctrader_token_service/ + BUILDLOG.md 2026-05-18.
    ctrader_token_path: str = Field(
        default="/data/ctrader_tokens.json",
        validation_alias="AI_FX_TREND_CTRADER_TOKEN_PATH",
    )

    # ─── Trading ─────────────────────────────────────────────────────────
    # Список инструментов в yfinance-нотации (XAUUSD, BZ=F, NG=F).
    # Внутренний клиент-адаптер мапит их в cTrader (XAUUSD остаётся
    # "XAUUSD", BZ=F → "BRENT", NG=F → "NAT.GAS").
    symbols_raw: str = Field(
        default=",".join(DEFAULT_AI_FX_SYMBOLS),
        validation_alias="AI_FX_TREND_SYMBOLS",
    )

    # Label для broker-side изоляции от Discretionary fx-ai-trader
    # (label="ai-fx-trader") и любых будущих ботов.
    # cTrader ProtoOANewOrderReq.label = string ≤100 chars. Reconcile
    # ИГНОРИРУЕТ позиции с чужим label, ни один бот не закроет чужую
    # позицию как orphan.
    order_label: str = Field(
        default="ai-fx-trend",
        validation_alias="AI_FX_TREND_ORDER_LABEL",
    )

    # Dual-timer: full-cycle делает полный анализ + может open/close/hold,
    # review-cycle только следит за уже открытыми позициями (close/hold,
    # без open) — даёт LLM в 3× больше точек реакции на adverse evidence.
    poll_interval_sec: int = Field(
        default=900, validation_alias="AI_FX_TREND_POLL_INTERVAL_SEC"
    )
    review_interval_sec: int = Field(
        default=300, validation_alias="AI_FX_TREND_REVIEW_INTERVAL_SEC"
    )

    # Виртуальный капитал для расчёта lot size в промпте. Демо FxPro
    # обычно $1000–$5000, но мы хотим эмулировать поведение на $500
    # (как у ai_trader). Все размеры считаются как будто это $500.
    virtual_capital_usd: float = Field(
        default=500.0, validation_alias="AI_FX_TREND_VIRTUAL_CAPITAL"
    )

    # ─── Broker safety (катастрофические лимиты, НЕ micro-management) ───
    # Trend-follower-philosophy v1.0 (18-May-2026): LLM получает свободу
    # systematic CTA — rules-based entries (Donchian/Turtle), ATR stops,
    # pyramiding по правилам, hold-the-trend. R:R/risk-per-trade здесь —
    # его собственный расчёт по N (ATR) и правилам Turtle (см. prompts.py).
    # Hard caps остаются только catastrophic-class:
    #   1. daily/total loss (stop-experiment): trend-follower может
    #      накопить большую unrealised drawdown по природе стратегии
    #      (Clenow: «20-30% DD на тренд = норма для CTA»), поэтому
    #      total_loss = $500 (vs $300 у Discretionary fx-ai-trader).
    #      Daily loss = $150 (тот же).
    #   2. broker margin safety: max_lot_size, max_open_positions.
    #   3. anti-hallucination gate: sentiment uncertainty > 0.7
    #      (наследие fx-ai-trader; sentiment здесь less central, но
    #      gate ловит broken JSON / hallucinated news).
    max_daily_loss_usd: float = Field(
        default=150.0, validation_alias="AI_FX_TREND_MAX_DAILY_LOSS"
    )
    max_total_loss_usd: float = Field(
        default=500.0, validation_alias="AI_FX_TREND_MAX_TOTAL_LOSS"
    )
    # Общий cap на количество одновременно открытых позиций. Для
    # trend-follower'а с pyramiding (до 4 units на инструмент по
    # классическим Turtle rules) реалистичный потолок выше Discretionary:
    # 3 instruments × 4 units = 12 хочется, но margin не позволит на
    # demo $1500. Ставим 6 — достаточно для pyramid на 1-2 инструмента
    # + позиции на третьем. Если LLM попросит больше — режется.
    max_open_positions: int = Field(
        default=6, validation_alias="AI_FX_TREND_MAX_POSITIONS"
    )
    # На один инструмент максимум 4 unit (Turtle canonical pyramid).
    max_positions_per_symbol: int = Field(
        default=4, validation_alias="AI_FX_TREND_MAX_POSITIONS_PER_SYMBOL"
    )
    # Hard cap на lot size — broker margin safety. Trend-follower
    # обычно использует **меньшие** позиции (1% risk × низкая
    # волатильность = маленький lot), но на инструменте с низким ATR
    # (например gold в quiet режиме) расчёт может выдать большой lot.
    # 0.50 lot остаётся тем же что у Discretionary — безопасный потолок.
    max_lot_size: float = Field(
        default=0.50, validation_alias="AI_FX_TREND_MAX_LOT_SIZE"
    )

    # ─── Storage ─────────────────────────────────────────────────────────
    data_dir: str = Field(default="/data", validation_alias="AI_FX_TREND_DATA_DIR")
    db_filename: str = Field(
        default="fx_ai_trend.sqlite", validation_alias="AI_FX_TREND_DB_FILENAME"
    )

    # ─── News (RSS + EIA) ────────────────────────────────────────────────
    news_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TREND_NEWS_ENABLED"
    )
    news_max_age_hours: int = Field(
        default=12, validation_alias="AI_FX_TREND_NEWS_MAX_AGE_HOURS"
    )
    news_max_items_per_symbol: int = Field(
        default=5, validation_alias="AI_FX_TREND_NEWS_MAX_ITEMS_PER_SYMBOL"
    )
    # EIA Open Data API key (бесплатная регистрация на eia.gov).
    # Пустой ключ → EIA-блок отключается, RSS остаётся.
    eia_api_key: str = Field(default="", validation_alias="AI_FX_TREND_EIA_API_KEY")
    eia_cache_ttl_sec: int = Field(
        default=21600, validation_alias="AI_FX_TREND_EIA_CACHE_TTL_SEC"
    )  # 6 часов

    # ─── Misc ────────────────────────────────────────────────────────────
    log_level: str = Field(
        default="INFO", validation_alias="AI_FX_TREND_LOG_LEVEL"
    )
    # Если False — только логируем decisions, реально ордера на cTrader НЕ
    # ставим. Phase 1 = paper-mode минимум 14 дней (research: NexusTrade
    # 2026 «30–90 days paper before live», Kiploks «weeks not hours»).
    trading_enabled: bool = Field(
        default=False, validation_alias="AI_FX_TREND_TRADING_ENABLED"
    )

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.symbols_raw.split(",") if s.strip())

    @property
    def db_path(self) -> str:
        from pathlib import Path

        return str(Path(self.data_dir) / self.db_filename)
