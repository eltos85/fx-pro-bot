from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MomentumBotSettings(BaseSettings):
    """Isolated settings for momentum bot.

    Uses only MOMENTUM_BOT_* variables to avoid overlap with legacy bots.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = Field(default="INFO", validation_alias="MOMENTUM_BOT_LOG_LEVEL")
    trading_enabled: bool = Field(
        default=False, validation_alias="MOMENTUM_BOT_TRADING_ENABLED"
    )
    poll_interval_sec: int = Field(
        default=300, validation_alias="MOMENTUM_BOT_POLL_INTERVAL_SEC"
    )
    symbols_raw: str = Field(
        default="EURUSD=X,GBPUSD=X,USDJPY=X,AUDUSD=X",
        validation_alias="MOMENTUM_BOT_SYMBOLS",
    )
    yfinance_interval: str = Field(
        default="1h", validation_alias="MOMENTUM_BOT_YFINANCE_INTERVAL"
    )
    yfinance_period: str = Field(
        default="3mo", validation_alias="MOMENTUM_BOT_YFINANCE_PERIOD"
    )

    momentum_lookback_bars: int = Field(
        default=24, validation_alias="MOMENTUM_BOT_LOOKBACK_BARS"
    )
    atr_period: int = Field(default=14, validation_alias="MOMENTUM_BOT_ATR_PERIOD")
    atr_stop_mult: float = Field(
        default=2.5, validation_alias="MOMENTUM_BOT_ATR_STOP_MULT"
    )
    # НЕ используется с 2026-06-10: брокерский TP у momentum-входов убран
    # (стоял на 1.4R и закрывал позицию до активации partial/trailing@1.5R —
    # runner был мёртвым кодом). Выход ведёт сопровождение: BE@1R +
    # partial@1.5R + ATR-trailing (Raschke partial+runner, LeBeau Chandelier).
    # Поле оставлено для совместимости env, согласовано с пользователем.
    atr_take_mult: float = Field(
        default=3.5, validation_alias="MOMENTUM_BOT_ATR_TAKE_MULT"
    )
    signal_threshold: float = Field(
        default=0.0015, validation_alias="MOMENTUM_BOT_SIGNAL_THRESHOLD"
    )

    # Fallback-лот, если ATR-сайзинг выключен (risk_per_trade_usd=0).
    lot_size: float = Field(default=0.01, validation_alias="MOMENTUM_BOT_LOT_SIZE")
    # ─── ATR-scaled sizing (Tharp ch.11, Vince 1992) ───
    # Фиксированный риск $ на сделку, лот пересчитывается из SL-дистанции:
    # выравнивает риск между инструментами с разной ценой пункта. Канон
    # fixed-fractional risk уже реализован у advisor (calc_lot_size, риск
    # $15 = 1% от $1500) — переиспользуем ту же функцию. lot = risk /
    # (sl_pips × pip_value); cap MAX_LOT_SIZE=0.05 (инцидент 23.04).
    # 0 = выключить (фикс-лот).
    risk_per_trade_usd: float = Field(
        default=15.0, validation_alias="MOMENTUM_BOT_RISK_PER_TRADE_USD"
    )
    max_lot_size: float = Field(
        default=0.05, validation_alias="MOMENTUM_BOT_MAX_LOT_SIZE"
    )
    # ─── Spread-guard на входе ───
    # Скип входа, если live bid/ask спред > доли SL-дистанции: спред —
    # прямой вычет из R (вход по ask, SL/выход по bid). При 10% спреда
    # к риску система 2:1 теряет ~0.1R на сделку до начала торговли
    # (cost-to-risk контроль, Harris «Trading and Exchanges» 2003 ch.21).
    # Естественно блокирует ночь/роллувер 17:00 ET/пост-релизные минуты —
    # меряем фактический спред, не хардкодим часы. 0 = выключить.
    max_spread_risk_fraction: float = Field(
        default=0.10, validation_alias="MOMENTUM_BOT_MAX_SPREAD_RISK_FRACTION"
    )
    # Лимит одновременно открытых momentum-позиций (по всем символам).
    max_open_positions: int = Field(
        default=3, validation_alias="MOMENTUM_BOT_MAX_OPEN_POSITIONS"
    )
    order_label: str = Field(
        default="momentum-bot", validation_alias="MOMENTUM_BOT_ORDER_LABEL"
    )
    # Broker-side label (cTrader ProtoOAPosition.label) для ИЗОЛЯЦИИ позиций
    # на общем счёте: и fx_momentum_bot, и fx_ai_trader сидят на одном
    # cTrader-аккаунте. fx_ai_trader фильтрует свои позиции по label
    # "ai-fx-trader"; momentum обязан помечать свои отдельным label и
    # управлять/считать ТОЛЬКО их (иначе подхватит XAUUSD-сделки AI на
    # общем счёте). См. BUILDLOG.md 2026-06-09.
    position_label: str = Field(
        default="momentum-bot", validation_alias="MOMENTUM_BOT_POSITION_LABEL"
    )
    # Legacy-label: до введения position_label momentum открывал ордера через
    # общий executor с дефолтным label="fx-pro-bot". Чтобы бот продолжал вести
    # СВОИ позиции, открытые до миграции (BE/трейлинг), управляем и этим label.
    # Изоляция от fx_ai_trader сохраняется (у него label="ai-fx-trader").
    # Advisor тоже "fx-pro-bot", но по deploy-правилу profile=disabled (не
    # торгует). Пусто = вести только position_label.
    manage_legacy_label: str = Field(
        default="fx-pro-bot", validation_alias="MOMENTUM_BOT_MANAGE_LEGACY_LABEL"
    )

    @property
    def managed_labels(self) -> frozenset[str]:
        """Набор broker-label, которые бот считает своими (управление + счёт)."""
        return frozenset(
            lbl for lbl in (self.position_label, self.manage_legacy_label) if lbl
        )
    # ─── Event-guard: блок входов вокруг HIGH-impact релизов ───
    # Окно ±60 мин по Andersen et al. 2003 (пик реакции и волатильности):
    # вход momentum в момент релиза ловит шип/фейкаут. Блокируются ТОЛЬКО
    # входы; сопровождение и выходы работают. Per-symbol scoping: US-релизы
    # (CPI/FOMC/NFP) блокируют все пары, ECB — только EUR, BoJ — только JPY.
    # См. src/fx_momentum_bot/strategy/event_guard.py.
    news_block_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_NEWS_BLOCK_ENABLED"
    )
    news_block_before_min: int = Field(
        default=60, validation_alias="MOMENTUM_BOT_NEWS_BLOCK_BEFORE_MIN"
    )
    news_block_after_min: int = Field(
        default=60, validation_alias="MOMENTUM_BOT_NEWS_BLOCK_AFTER_MIN"
    )

    # Position management (trader-backed):
    # - Van Tharp: R-multiple discipline + break-even transfer.
    # - Linda Raschke discretionary practice: partial profit + runner.
    # - Turtle/LeBeau family: ATR-style trailing for trend persistence.
    position_management_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_POSITION_MANAGEMENT_ENABLED"
    )
    break_even_r: float = Field(
        default=1.0, validation_alias="MOMENTUM_BOT_BREAK_EVEN_R"
    )
    partial_take_r: float = Field(
        default=1.5, validation_alias="MOMENTUM_BOT_PARTIAL_TAKE_R"
    )
    partial_take_fraction: float = Field(
        default=0.5, validation_alias="MOMENTUM_BOT_PARTIAL_TAKE_FRACTION"
    )
    trailing_activate_r: float = Field(
        default=1.5, validation_alias="MOMENTUM_BOT_TRAILING_ACTIVATE_R"
    )
    trailing_atr_mult: float = Field(
        default=1.5, validation_alias="MOMENTUM_BOT_TRAILING_ATR_MULT"
    )

    # Dedicated cTrader credentials for this bot only.
    ctrader_host_type: str = Field(
        default="demo", validation_alias="MOMENTUM_BOT_CTRADER_HOST_TYPE"
    )
    ctrader_client_id: str = Field(
        default="", validation_alias="MOMENTUM_BOT_CTRADER_CLIENT_ID"
    )
    ctrader_client_secret: str = Field(
        default="", validation_alias="MOMENTUM_BOT_CTRADER_CLIENT_SECRET"
    )
    ctrader_account_id: int = Field(
        default=0, validation_alias="MOMENTUM_BOT_CTRADER_ACCOUNT_ID"
    )
    ctrader_redirect_uri: str = Field(
        default="https://openapi.ctrader.com/apps/token",
        validation_alias="MOMENTUM_BOT_CTRADER_REDIRECT_URI",
    )
    token_service_url: str = Field(
        default="", validation_alias="MOMENTUM_BOT_TOKEN_SERVICE_URL"
    )
    token_service_secret: str = Field(
        default="", validation_alias="MOMENTUM_BOT_TOKEN_SERVICE_SECRET"
    )
    token_service_label: str = Field(
        default="momentum_bot", validation_alias="MOMENTUM_BOT_TOKEN_SERVICE_LABEL"
    )
    require_token_service: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_REQUIRE_TOKEN_SERVICE"
    )

    data_dir: str = Field(default="/data", validation_alias="MOMENTUM_BOT_DATA_DIR")
    db_filename: str = Field(
        default="momentum_bot.sqlite", validation_alias="MOMENTUM_BOT_DB_FILENAME"
    )
    token_filename: str = Field(
        default="momentum_bot_tokens.json",
        validation_alias="MOMENTUM_BOT_TOKEN_FILENAME",
    )

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.symbols_raw.split(",") if s.strip())

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / self.db_filename

    @property
    def token_path(self) -> Path:
        return Path(self.data_dir) / self.token_filename

