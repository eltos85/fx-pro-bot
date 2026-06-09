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
    atr_take_mult: float = Field(
        default=3.5, validation_alias="MOMENTUM_BOT_ATR_TAKE_MULT"
    )
    signal_threshold: float = Field(
        default=0.0015, validation_alias="MOMENTUM_BOT_SIGNAL_THRESHOLD"
    )

    lot_size: float = Field(default=0.01, validation_alias="MOMENTUM_BOT_LOT_SIZE")
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
    # управлять/считать ТОЛЬКО их (иначе подхватит XAUUSD-сделки AI после
    # включения VP-стратегии по золоту). См. BUILDLOG.md 2026-06-09.
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

    # ─── Volume Profile strategy (optional, OFF by default) ───
    # Механический Market/Volume Profile сетап (Steidlmayer 2003 / Dalton
    # 2007), gold-only. Символы из vp_symbols идут на эту стратегию вместо
    # momentum; данные качаются на vp_yfinance_interval (5m, реальный объём
    # у фьючерса GC=F). Пусто = выключено.
    vp_symbols_raw: str = Field(
        default="", validation_alias="MOMENTUM_BOT_VP_SYMBOLS"
    )
    vp_yfinance_interval: str = Field(
        default="5m", validation_alias="MOMENTUM_BOT_VP_YFINANCE_INTERVAL"
    )
    vp_yfinance_period: str = Field(
        default="5d", validation_alias="MOMENTUM_BOT_VP_YFINANCE_PERIOD"
    )
    # Сессионное окно профиля (London pre-NY), таймзона по NY.
    vp_session_start: str = Field(
        default="03:00", validation_alias="MOMENTUM_BOT_VP_SESSION_START"
    )
    vp_session_end: str = Field(
        default="07:00", validation_alias="MOMENTUM_BOT_VP_SESSION_END"
    )
    vp_session_tz: str = Field(
        default="America/New_York", validation_alias="MOMENTUM_BOT_VP_SESSION_TZ"
    )
    # Value area = 70% объёма (канон Dalton, ≈1σ) — не подгонять без источника.
    vp_value_area_pct: float = Field(
        default=0.70, validation_alias="MOMENTUM_BOT_VP_VALUE_AREA_PCT"
    )
    vp_num_bins: int = Field(default=50, validation_alias="MOMENTUM_BOT_VP_NUM_BINS")
    vp_atr_period: int = Field(default=14, validation_alias="MOMENTUM_BOT_VP_ATR_PERIOD")
    # Минимальный risk:reward (видео: «at least 1:2»; берём 1.5 как мягкий пол).
    vp_min_rr: float = Field(default=1.5, validation_alias="MOMENTUM_BOT_VP_MIN_RR")
    # Сколько 5m-баров назад искать свип (failed auction) / держать консолидацию.
    vp_breach_lookback: int = Field(
        default=6, validation_alias="MOMENTUM_BOT_VP_BREACH_LOOKBACK"
    )
    vp_consolidation_bars: int = Field(
        default=6, validation_alias="MOMENTUM_BOT_VP_CONSOLIDATION_BARS"
    )
    # Лимит сделок в одну сторону за день (видео: max 2 per direction).
    vp_max_trades_per_dir_per_day: int = Field(
        default=2, validation_alias="MOMENTUM_BOT_VP_MAX_TRADES_PER_DIR_PER_DAY"
    )
    vp_lot_size: float = Field(default=0.01, validation_alias="MOMENTUM_BOT_VP_LOT_SIZE")
    vp_order_label: str = Field(
        default="momentum-bot-vp", validation_alias="MOMENTUM_BOT_VP_ORDER_LABEL"
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
    def vp_symbols(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.vp_symbols_raw.split(",") if s.strip())

    @property
    def all_symbols(self) -> tuple[str, ...]:
        """Momentum-символы + VP-символы (для fetch/позиций), без дублей."""
        seen: dict[str, None] = {}
        for s in (*self.symbols, *self.vp_symbols):
            seen.setdefault(s, None)
        return tuple(seen)

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / self.db_filename

    @property
    def token_path(self) -> Path:
        return Path(self.data_dir) / self.token_filename

