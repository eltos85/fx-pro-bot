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
    # v0.33 (2026-05-28): bump 4096 → 8192. С v0.30+v0.31+v0.32 промптом
    # (institutional rewrite + verbose commentary: EQUITY READ + MACRO
    # REGIME + PER-ASSET HIERARCHY + MFP + COST AWARENESS + JSON) thinking
    # блоки DeepSeek-V4-Flash ~2-4K + commentary ~3-4K + JSON ~500 не
    # умещались в 4096 — ответ обрезался на commentary, JSON не
    # генерировался («Parse error: no JSON object found»).
    # 8192 = beta-limit DeepSeek (https://api-docs.deepseek.com/news/news0725
    # "8K max_tokens (Beta)"), Anthropic-compat endpoint его принимает без
    # требования streaming (порог nonstreaming-timeout SDK ≈ 10 мин).
    # Выше 8192 (например 16384/32768) ловим:
    # ValueError: Streaming is required for operations that may take longer
    # than 10 minutes — SDK прикидывает время по max_tokens × tokens/sec
    # и блокирует non-streaming запрос.
    deepseek_max_tokens: int = Field(
        default=8192, validation_alias="AI_TRADER_DEEPSEEK_MAX_TOKENS"
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
    )  # 15 минут — full cycle (полный анализ, может открывать сделки)

    # Review-cycle (2026-05-10): между full-cycles запускается lite-цикл,
    # который только следит за уже открытыми позициями (close/hold, без open).
    # Контекст урезан до тикер + 1H индикаторы + funding (без macro/news/4H/DVOL).
    # Цель: дать LLM в 3 раза больше точек реакции на adverse evidence,
    # чтобы успеть закрыть позицию до SL hit (см. кейс TAO id=27).
    # 0 = review отключён (только full-cycle 15min).
    review_interval_sec: int = Field(
        default=300, validation_alias="AI_TRADER_REVIEW_INTERVAL_SEC"
    )

    # Виртуальный капитал для расчёта qty. На demo баланс может быть $50k+,
    # но мы хотим эмулировать поведение на $500. Все qty считаются как
    # будто капитал ровно столько.
    virtual_capital_usd: float = Field(
        default=500.0, validation_alias="AI_TRADER_VIRTUAL_CAPITAL"
    )

    # ─── KillSwitch (v0.31 aggressive mandate, 2026-05-28) ──────────────
    # Aggressive профиль по запросу пользователя: killswitch $350/day = 70%
    # capital. При risk-per-trade $10 и max-pos 5 это позволяет ~35
    # убыточных сделок до блока (или ~17 циклов с 2-3 losses каждый).
    # Risk-per-trade оставляем 2% ($10) — industry standard 2026 (KuCoin
    # Risk Management 2026, Atlas Peak Research; 5% уже Kelly-territory с
    # опасным drawdown). Агрессия достигается через ЧАСТОТУ setup'ов,
    # max_positions, и mandate в промпте — НЕ через risk-per-trade.
    max_daily_loss_usd: float = Field(
        default=350.0, validation_alias="AI_TRADER_MAX_DAILY_LOSS"
    )
    max_total_loss_usd: float = Field(
        default=400.0, validation_alias="AI_TRADER_MAX_TOTAL_LOSS"
    )
    max_open_positions: int = Field(
        default=5, validation_alias="AI_TRADER_MAX_POSITIONS"
    )
    max_leverage: int = Field(default=5, validation_alias="AI_TRADER_MAX_LEVERAGE")
    # v0.31: лот (position_size_usd в JSON open) capped $100. С leverage до
    # 5x это даёт notional до $500 = весь virtual capital — классический
    # «aggressive but not gambling» режим. По confidence band:
    #   - low (0.30-0.49):  $25-50  (1-3x leverage typical)
    #   - medium (0.50-0.69): $50-75  (3-4x leverage typical)
    #   - high (0.70-1.00):  $75-100 (4-5x leverage typical)
    # Прежний неявный cap = virtual_capital ($500) разрешал 1x trades на
    # весь капитал, что плохо для diversified portfolio при max_pos=5.
    max_position_size_usd: float = Field(
        default=100.0, validation_alias="AI_TRADER_MAX_POSITION_SIZE_USD"
    )
    # Risk per trade в долях (0.02 = 2%). Используется LLM в промпте + для
    # будущих helper-функций position sizing.
    risk_per_trade_pct: float = Field(
        default=0.02, validation_alias="AI_TRADER_RISK_PER_TRADE"
    )

    # v0.20 (2026-05-28): Bybit taker fee per side в долях. Default 0.00055
    # = 0.055% per side (VIP-0 на demo + spot/perp linear, см. id=121
    # сверка: openFee=1.3597 на cumEntryValue=2472.21 → ровно 0.055%).
    # Round-trip = 2× per_side = 0.11% от notional.
    # Используется ВСЮДУ:
    # - prompts.py: рендер числа в FEE AWARENESS блок (open + close)
    # - context.py: peak_pnl_r_net / current_pnl_r_net / live unrealised net
    # - executor._apply_open: hard-валидация effective R:R и
    #   risk_usd + fee_RT <= cap.
    # При переходе на live / другой VIP-tier — менять через .env
    # (`AI_TRADER_TAKER_FEE_PCT=0.0006` для 0.06%).
    taker_fee_pct: float = Field(
        default=0.00055, validation_alias="AI_TRADER_TAKER_FEE_PCT"
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

    # ─── Macro context (v0.30 — institutional rewrite) ───────────────────
    # US rates feed (DXY + UST10Y) через yfinance. Port из FX-trader D1
    # (BUILDLOG_AI_FX_TRADER.md 2026-05-27). Crypto коррелирует с DXY на
    # 30-day rolling −0.72…−0.90 (BitMEX 2026, Intellectia 2026-04), с
    # UST10Y слабее: ≈ −0.55 (Convex 2026). Закрывает hidden-disconnect
    # «промпт обещает BTC dominance / DXY, контекст не отдаёт».
    macro_rates_enabled: bool = Field(
        default=True, validation_alias="AI_TRADER_MACRO_RATES_ENABLED"
    )
    macro_rates_cache_ttl_sec: int = Field(
        default=1800, validation_alias="AI_TRADER_MACRO_RATES_CACHE_TTL_SEC"
    )

    # BTC dominance + total crypto market cap через CoinGecko /global
    # (free tier 10k calls/month, no key). При cache 1h = 720 calls/month —
    # с запасом. BTC.D current ≈60.3% (May 2026, BYDFi/AInvest); threshold
    # для altseason = 59.63% support / 66.06% resistance.
    crypto_macro_enabled: bool = Field(
        default=True, validation_alias="AI_TRADER_CRYPTO_MACRO_ENABLED"
    )
    crypto_macro_cache_ttl_sec: int = Field(
        default=3600, validation_alias="AI_TRADER_CRYPTO_MACRO_CACHE_TTL_SEC"
    )

    # REGIME-CHANGE WINDOW (порт из FX-trader, BUILDLOG_AI_FX_TRADER.md
    # 2026-05-28). Pre-deploy v0.30 trades — результат старой стратегии
    # (без THESIS DISCIPLINE / per-asset hierarchy). SELF-REFLECTION
    # фильтрует по этой дате чтобы не учить LLM на outcome другой DGP.
    # Research: Lopez de Prado «Advances in Financial ML» 2018 ch.7 +
    # Hamilton (1989) regime-switching framework. Пустая строка = legacy
    # behavior (учитываем всю историю — для тестов backward-compat).
    stats_window_start: str = Field(
        default="2026-05-30T00:00:00+00:00",
        validation_alias="AI_TRADER_STATS_WINDOW_START",
    )

    # 5-dim news sentiment: aggregate_uncertainty > этого порога → open
    # автоматически блокируется executor'ом (`open blocked:
    # aggregate_uncertainty=X > Y`). Default 0.7 совпадает с FX-trader
    # (prompts.py:565 «Aggregate the news block. If aggregate_uncertainty
    # > 0.7 — return HOLD»). COLD-START discovery trades используют
    # более строгий порог 0.5 (см. COLD-START DISCOVERY RULE в промпте,
    # справится сам LLM, executor не enforces — это behavior, не gate).
    news_uncertainty_block_threshold: float = Field(
        default=0.7, validation_alias="AI_TRADER_NEWS_UNCERTAINTY_BLOCK"
    )

    # ─── Event-driven analyst (v0.34, 2026-05-29; порт fx Фаз 1-3) ──────
    # Живой поток цены (pybit WebSocket public ticker, см. price_stream.py)
    # + датчики (price_sensor.py) будят внеплановый вызов аналитика по
    # факту рыночного события, не дожидаясь планового таймера. Датчики
    # читают in-memory кэш живой цены — БЕЗ доп. API-запросов. НЕ меняют
    # правила входа/выхода: решение по-прежнему за LLM. Откат: выключить
    # флаги ниже → чистый polling (full 15min + review 5min).
    event_full_enabled: bool = Field(
        default=True, validation_alias="AI_TRADER_EVENT_FULL_ENABLED"
    )
    # Интервал опроса датчиков (live-кэш) между плановыми циклами.
    event_sensor_interval_sec: int = Field(
        default=15, validation_alias="AI_TRADER_EVENT_SENSOR_INTERVAL_SEC"
    )
    # Макс. возраст live-цены: старше → датчики на символе молчат
    # (безопасная деградация при обрыве WS).
    event_price_max_age_sec: int = Field(
        default=60, validation_alias="AI_TRADER_EVENT_PRICE_MAX_AGE_SEC"
    )

    # LockedProfit → внеплановый REVIEW (guardian фиксирует прибыль).
    # threshold_r ДОЛЖЕН совпадать с locked-profit порогом в
    # SYSTEM_PROMPT_REVIEW (1.5R) — датчик будит review ровно когда у
    # guardian появляется право зафиксировать прибыль.
    locked_profit_enabled: bool = Field(
        default=True, validation_alias="AI_TRADER_LOCKED_PROFIT_ENABLED"
    )
    locked_profit_threshold_r: float = Field(
        default=1.5, validation_alias="AI_TRADER_LOCKED_PROFIT_THRESHOLD_R"
    )
    locked_profit_hysteresis_r: float = Field(
        default=0.3, validation_alias="AI_TRADER_LOCKED_PROFIT_HYSTERESIS_R"
    )
    locked_profit_cooldown_sec: int = Field(
        default=120, validation_alias="AI_TRADER_LOCKED_PROFIT_COOLDOWN_SEC"
    )
    locked_profit_max_per_hour: int = Field(
        default=6, validation_alias="AI_TRADER_LOCKED_PROFIT_MAX_PER_HOUR"
    )

    # AdverseMove → внеплановый FULL (стратег с macro пересматривает
    # тезис). 1R = натуральная единица риска (дистанция до SL).
    adverse_move_enabled: bool = Field(
        default=True, validation_alias="AI_TRADER_ADVERSE_MOVE_ENABLED"
    )
    adverse_move_threshold_r: float = Field(
        default=1.0, validation_alias="AI_TRADER_ADVERSE_MOVE_THRESHOLD_R"
    )
    adverse_move_hysteresis_r: float = Field(
        default=0.3, validation_alias="AI_TRADER_ADVERSE_MOVE_HYSTERESIS_R"
    )
    adverse_move_cooldown_sec: int = Field(
        default=300, validation_alias="AI_TRADER_ADVERSE_MOVE_COOLDOWN_SEC"
    )
    adverse_move_max_per_hour: int = Field(
        default=4, validation_alias="AI_TRADER_ADVERSE_MOVE_MAX_PER_HOUR"
    )

    # EntryBreakout → внеплановый FULL (аналитик решает open/hold по
    # пробою Donchian-канала). lookback 20 — Donchian/Turtle canonical
    # (Faith 2003). buffer_atr — confirmation band (анти-шум).
    entry_breakout_enabled: bool = Field(
        default=True, validation_alias="AI_TRADER_ENTRY_BREAKOUT_ENABLED"
    )
    entry_breakout_lookback: int = Field(
        default=20, validation_alias="AI_TRADER_ENTRY_BREAKOUT_LOOKBACK"
    )
    entry_breakout_buffer_atr: float = Field(
        default=0.05, validation_alias="AI_TRADER_ENTRY_BREAKOUT_BUFFER_ATR"
    )
    entry_breakout_cooldown_sec: int = Field(
        default=300, validation_alias="AI_TRADER_ENTRY_BREAKOUT_COOLDOWN_SEC"
    )
    entry_breakout_max_per_hour: int = Field(
        default=4, validation_alias="AI_TRADER_ENTRY_BREAKOUT_MAX_PER_HOUR"
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
