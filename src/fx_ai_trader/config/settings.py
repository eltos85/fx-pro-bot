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


# Дефолтные инструменты Phase 1+ (2026-05-18: добавлен газ NG=F).
# - XAUUSD: spot gold CFD (от Advisor GC=F futures отделён на cTrader как
#   разный symbolId → полная broker-side изоляция).
# - BZ=F: Brent crude в yfinance-нотации; в SymbolCache маппится на cTrader
#   "BRENT" (см. src/fx_pro_bot/trading/symbols.py).
# - NG=F: Natural gas spot CFD (NYMEX Henry Hub Natural Gas Futures
#   underlying); в SymbolCache маппится на cTrader "NAT.GAS"
#   (id=1118 на FxPro demo, разведка через
#   scripts/fx_ai_scout_gas_symbols.py 2026-05-18).
#   TTF (Dutch front-month) на FxPro demo отсутствует — единственный
#   доступный gas-инструмент NG/Henry Hub.
DEFAULT_AI_FX_SYMBOLS: tuple[str, ...] = ("XAUUSD", "BZ=F", "NG=F")


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
    # output_config.effort для DeepSeek-V4 Anthropic-compat endpoint
    # (api-docs.deepseek.com/guides/anthropic_api: «output_config — Only
    # effort is supported»). Anthropic levels: low|medium|high|max
    # (platform.claude.com/docs/en/build-with-claude/adaptive-thinking).
    # Default 'high' для full-cycle multi-driver commodity analysis —
    # обоснование в BUILDLOG_AI_FX_TRADER.md 2026-05-26 v4-prompt-tune.
    # Пустая строка → не передавать (использовать default endpoint'а).
    deepseek_effort: str = Field(
        default="high", validation_alias="AI_FX_TRADER_DEEPSEEK_EFFORT"
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
    # Per-symbol overrides ниже общих ограничений. JSON-словарь через ENV.
    # Цель — снизить экспозицию по конкретному инструменту без отключения.
    # Применение (NG=F): по правилу sample-size.mdc после 11 NG-трейдов
    # (WR 18%, BUY-bias 11/11) уменьшаем максимальный размер до 0.25 и
    # одновременно к одной открытой NG-позиции. BZ=F и XAUUSD продолжают
    # работать на стандартных 0.50 / 3.
    # См. BUILDLOG_AI_FX_TRADER v1.2 (NG enhancement).
    per_symbol_max_lot_size: dict[str, float] = Field(
        default_factory=lambda: {"NG=F": 0.25},
        validation_alias="AI_FX_TRADER_PER_SYMBOL_MAX_LOT_SIZE",
    )
    per_symbol_max_positions: dict[str, int] = Field(
        default_factory=lambda: {"NG=F": 1},
        validation_alias="AI_FX_TRADER_PER_SYMBOL_MAX_POSITIONS",
    )

    def effective_max_lot_size(self, symbol: str) -> float:
        """Per-symbol max lot size с fallback к общему лимиту."""
        cap = self.per_symbol_max_lot_size.get(symbol)
        if cap is None:
            return self.max_lot_size
        return min(cap, self.max_lot_size)

    def effective_max_positions_per_symbol(self, symbol: str) -> int:
        """Per-symbol position cap с fallback к общему лимиту."""
        cap = self.per_symbol_max_positions.get(symbol)
        if cap is None:
            return self.max_positions_per_symbol
        return min(cap, self.max_positions_per_symbol)

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

    # ─── Macro rates feed (2026-05-27 D1) ───────────────────────────────
    # DXY / UST10Y / TIP через yfinance (без API-ключа). Включён по
    # дефолту: SYSTEM_PROMPT ~20 раз ссылается на эти ряды как primary
    # gold-драйверы. Отключается флагом для тестов или при сбоях
    # yfinance rate-limits. См. src/fx_ai_trader/data/macro_rates.py.
    macro_rates_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_MACRO_RATES_ENABLED"
    )
    macro_rates_cache_ttl_sec: int = Field(
        default=1800,
        validation_alias="AI_FX_TRADER_MACRO_RATES_CACHE_TTL_SEC",
    )  # 30 минут — достаточно freshness, без HTTP-перегруза
    # FRED API key (бесплатная регистрация на fred.stlouisfed.org).
    # Пустой ключ → real-yield (DFII10) / breakeven (T10YIE) НЕ тянутся,
    # остаётся TIP-прокси через yfinance (graceful degrade). С ключом
    # macro_rates добавляет ТОЧНЫЙ 10Y real yield (gold-driver №1) и
    # инфляционные ожидания. См. data/macro_rates.py (Enhancement B).
    fred_api_key: str = Field(
        default="", validation_alias="AI_FX_TRADER_FRED_API_KEY"
    )

    # ─── Risk regime (VIX) — Enhancement C (2026-05-29) ─────────────────
    # CBOE VIX через yfinance (^VIX, без ключа). Risk-on/off контекст:
    # gold = safe haven (VIX↑ → bid), oil = risk asset (VIX↑ → offer).
    # Подаётся как сырое значение + 24h Δ; интерпретацию делает LLM.
    risk_regime_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_RISK_REGIME_ENABLED"
    )
    risk_regime_cache_ttl_sec: int = Field(
        default=1800, validation_alias="AI_FX_TRADER_RISK_REGIME_CACHE_TTL_SEC"
    )

    # ─── CFTC COT positioning — Enhancement A (2026-05-29) ──────────────
    # Commitments of Traders (CFTC public API, без ключа, weekly Tue snapshot).
    # SYSTEM_PROMPT в иерархии драйверов называет «ETF/COT», но COT не
    # подавался. Net speculative positioning + экстремумы = контрарный
    # сигнал. См. data/cot.py.
    cot_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_COT_ENABLED"
    )
    cot_cache_ttl_sec: int = Field(
        default=21600, validation_alias="AI_FX_TRADER_COT_CACHE_TTL_SEC"
    )  # 6 часов — COT обновляется раз в неделю (пятница 15:30 ET)

    # ─── GDELT news tone — Enhancement D (2026-05-29) ───────────────────
    # Global media sentiment (GDELT DOC 2.0 timelinetone, без ключа).
    # Структурный sentiment поверх точечных RSS-заголовков. GDELT бывает
    # медленным → длинный TTL + graceful degrade.
    gdelt_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_GDELT_ENABLED"
    )
    gdelt_cache_ttl_sec: int = Field(
        default=10800, validation_alias="AI_FX_TRADER_GDELT_CACHE_TTL_SEC"
    )  # 3 часа

    # ─── Economic calendar — Enhancement E (2026-05-29) ─────────────────
    # Event-proximity (FOMC/CPI/NFP/EIA). Pure-compute, без сети/ключа.
    # SYSTEM_PROMPT требует scale size near FOMC — теперь LLM видит близость.
    econ_calendar_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_ECON_CALENDAR_ENABLED"
    )
    econ_calendar_horizon_hours: float = Field(
        default=168.0, validation_alias="AI_FX_TRADER_ECON_CALENDAR_HORIZON_HOURS"
    )  # 7 дней — чтобы LLM всегда видел ближайший FOMC/CPI/NFP/EIA

    # ─── Live price stream (2026-05-29 Phase 1) ─────────────────────────
    # Подписка на ProtoOASubscribeSpotsReq → реальная текущая цена из
    # spot-стрима cTrader вместо H1-close (которая могла отставать до часа).
    # get_current_price() предпочитает живой mid (bid+ask)/2, fallback на
    # последний M1-close если стрима ещё/уже нет.
    # Док: help.ctrader.com/open-api/messages/#protooasubscribespotsreq
    live_price_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_LIVE_PRICE_ENABLED"
    )
    # Backstop на «молчащее» соединение: если последний spot старше этого
    # порога — get_current_price падает на M1-close. При живом TCP
    # (heartbeat 8s) и открытом рынке spot обновляется суб-секундно;
    # порог защищает только от dead-connection до срабатывания reconnect.
    # 300s = с запасом покрывает паузы низкой ликвидности (gas off-hours).
    live_price_max_age_sec: int = Field(
        default=300, validation_alias="AI_FX_TRADER_LIVE_PRICE_MAX_AGE_SEC"
    )

    # ─── Event-driven review (2026-05-29 Phase 2) ───────────────────────
    # Датчик locked-profit: между плановыми review запускает внеплановый,
    # как только позиция входит в зону ≥ threshold_r (по живой цене из
    # Phase 1 spot-стрима + локальной БД, БЕЗ API-запросов). НЕ меняет
    # exit-правила — решение по-прежнему за LLM-guardian (Phase 0).
    # См. src/fx_ai_trader/trading/price_sensor.py.
    event_review_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_EVENT_REVIEW_ENABLED"
    )
    # threshold_r ДОЛЖЕН совпадать с locked-profit порогом в
    # SYSTEM_PROMPT_REVIEW (1.5R) — датчик будит review ровно когда у
    # guardian появляется право зафиксировать прибыль.
    event_review_threshold_r: float = Field(
        default=1.5, validation_alias="AI_FX_TRADER_EVENT_REVIEW_THRESHOLD_R"
    )
    event_review_hysteresis_r: float = Field(
        default=0.3, validation_alias="AI_FX_TRADER_EVENT_REVIEW_HYSTERESIS_R"
    )
    event_review_cooldown_sec: int = Field(
        default=120, validation_alias="AI_FX_TRADER_EVENT_REVIEW_COOLDOWN_SEC"
    )
    event_review_sensor_interval_sec: int = Field(
        default=15, validation_alias="AI_FX_TRADER_EVENT_REVIEW_SENSOR_INTERVAL_SEC"
    )
    event_review_max_per_hour: int = Field(
        default=6, validation_alias="AI_FX_TRADER_EVENT_REVIEW_MAX_PER_HOUR"
    )

    # ─── Event-driven FULL cycle (2026-05-29 Phase 3) ───────────────────
    # Будит ВНЕплановый full-цикл (аналитик с macro+news) по событиям:
    #  (1) entry-breakout: живая цена пробила Donchian-канал → аналитик
    #      решает open/hold («график показал сетап → позвали аналитика»);
    #  (2) adverse-move: открытая позиция ушла в минус на ≥ threshold_r →
    #      стратег с macro пересматривает тезис (Phase 0: тезис судит full).
    # Плановый full остаётся пульс-страховкой. НЕ меняет правила
    # входа/выхода — решает LLM. См. price_sensor.py.
    event_full_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_EVENT_FULL_ENABLED"
    )
    # Entry-breakout (Donchian channel, lookback 20 — Donchian/Turtle
    # canonical, Faith 2003). buffer_atr — confirmation band (анти-шум).
    entry_breakout_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_ENTRY_BREAKOUT_ENABLED"
    )
    entry_breakout_lookback: int = Field(
        default=20, validation_alias="AI_FX_TRADER_ENTRY_BREAKOUT_LOOKBACK"
    )
    entry_breakout_buffer_atr: float = Field(
        default=0.05, validation_alias="AI_FX_TRADER_ENTRY_BREAKOUT_BUFFER_ATR"
    )
    entry_breakout_cooldown_sec: int = Field(
        default=300, validation_alias="AI_FX_TRADER_ENTRY_BREAKOUT_COOLDOWN_SEC"
    )
    entry_breakout_max_per_hour: int = Field(
        default=4, validation_alias="AI_FX_TRADER_ENTRY_BREAKOUT_MAX_PER_HOUR"
    )
    # Adverse-move: 1R = натуральная единица риска (дистанция до SL).
    adverse_move_enabled: bool = Field(
        default=True, validation_alias="AI_FX_TRADER_ADVERSE_MOVE_ENABLED"
    )
    adverse_move_threshold_r: float = Field(
        default=1.0, validation_alias="AI_FX_TRADER_ADVERSE_MOVE_THRESHOLD_R"
    )
    adverse_move_hysteresis_r: float = Field(
        default=0.3, validation_alias="AI_FX_TRADER_ADVERSE_MOVE_HYSTERESIS_R"
    )
    adverse_move_cooldown_sec: int = Field(
        default=300, validation_alias="AI_FX_TRADER_ADVERSE_MOVE_COOLDOWN_SEC"
    )
    adverse_move_max_per_hour: int = Field(
        default=4, validation_alias="AI_FX_TRADER_ADVERSE_MOVE_MAX_PER_HOUR"
    )

    # ─── Self-reflection regime change cutoff (2026-05-28; advanced 2026-05-29) ─
    # Фильтрует closed trades в SELF-REFLECTION блоках USER_PROMPT
    # (get_pnl_by_symbol / get_pnl_by_symbol_side / get_recent_closed_trades).
    # Trades с opened_at < этой метки НЕ показываются LLM, хотя физически
    # ОСТАЮТСЯ в БД (для аудита и анализа).
    #
    # Зачем: 2026-05-29 08:26 UTC завершён деплой Phase 0–3 (event-driven
    # архитектура) — самый крупный structural break reasoning/exit-rules
    # после старта эксперимента:
    #   • Phase 0 (Review Guardian): review-цикл больше НЕ закрывает по 1H
    #     техническому шуму — только locked-profit ≥1.5R. Все pre-Phase-0
    #     убытки (22/26 закрытий на 1H noise) — outcome УЖЕ исправленной
    #     логики и пугают бота за поведение, которого больше нет.
    #   • Phase 1 (live spot) / Phase 2 (locked-profit sensor) / Phase 3
    #     (event-driven analyst) — full/review теперь реагируют на события,
    #     а не только на таймер.
    # Использовать pre-cutoff trades как evidence для SELF-REFLECTION текущей
    # стратегии = systematic bias. Ранее cutoff стоял на 2026-05-26 07:42
    # (Phase 1 persistent-thesis deploy) — историческая запись.
    #
    # Research basis: Lopez de Prado "Advances in Financial ML" (2018)
    # ch.7 «Cross-Validation in Finance» — structural breaks invalidate
    # use of pre-break outcomes as evidence for post-break performance.
    # Hamilton (1989) regime-switching framework.
    #
    # ВАЖНО (sample-size.mdc): сдвиг cutoff обнуляет выборку (n=0 → re-trigger
    # cold-start). Двигать ТОЛЬКО на реальный structural break, НЕ на каждую
    # мелкую правку — иначе выборка никогда не накопится до порога валидации.
    #
    # Compliance: НЕ отключает инструменты (sample-size.mdc), НЕ меняет
    # торговую логику (strategy-guard.mdc), НЕ удаляет данные
    # (full audit trail сохраняется в БД).
    #
    # Format: ISO 8601 с timezone. Empty string ("") = фильтр отключён,
    # бот видит всю историю (legacy v1.X behavior).
    stats_window_start: str = Field(
        default="2026-05-29T08:26:00+00:00",
        validation_alias="AI_FX_TRADER_STATS_WINDOW_START",
    )

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
