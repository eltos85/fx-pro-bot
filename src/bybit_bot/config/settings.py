"""Настройки Bybit crypto-бота V2: API, символы, trend-following стратегия, лимиты."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


V2_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
)

DISPLAY_NAMES: dict[str, str] = {
    "BTCUSDT": "Bitcoin",
    "ETHUSDT": "Ethereum",
    "SOLUSDT": "Solana",
    "BNBUSDT": "BNB",
    "XRPUSDT": "XRP",
}


def display_name(symbol: str) -> str:
    return DISPLAY_NAMES.get(symbol, symbol)


def _parse_symbols(raw: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in raw.split(",") if s.strip())


class Settings(BaseSettings):
    """Настройки крипто-бота Bybit V2 (trend-following)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="BYBIT_BOT_",
        extra="ignore",
    )

    log_level: str = "INFO"

    data_dir: Path = Field(default=Path("data"), validation_alias="BYBIT_BOT_DATA_DIR")

    # Bybit API
    api_key: str = Field(default="", validation_alias="BYBIT_BOT_API_KEY")
    api_secret: str = Field(default="", validation_alias="BYBIT_BOT_API_SECRET")
    demo: bool = Field(default=True, validation_alias="BYBIT_BOT_DEMO")
    trading_enabled: bool = Field(default=False, validation_alias="BYBIT_BOT_TRADING_ENABLED")

    category: str = Field(default="linear", validation_alias="BYBIT_BOT_CATEGORY")

    # Инструменты (V2: 5 ликвидных пар)
    scan_symbols_raw: str = Field(
        default=",".join(V2_SYMBOLS),
        validation_alias="BYBIT_BOT_SCAN_SYMBOLS",
    )

    # Рыночные данные (V2: Bybit klines, 1h)
    kline_interval: str = Field(default="60", validation_alias="BYBIT_BOT_KLINE_INTERVAL")
    kline_limit: int = Field(default=200, validation_alias="BYBIT_BOT_KLINE_LIMIT")
    poll_interval_sec: int = Field(default=300, validation_alias="BYBIT_BOT_POLL_INTERVAL_SEC")

    # Баланс / позиции ($500 микро-счёт)
    account_balance: float = Field(default=500.0, validation_alias="BYBIT_BOT_ACCOUNT_BALANCE")
    leverage: int = Field(default=3, validation_alias="BYBIT_BOT_LEVERAGE")
    capital_per_trade_pct: float = Field(
        default=0.02, validation_alias="BYBIT_BOT_CAPITAL_PER_TRADE_PCT",
    )
    max_margin_per_trade_pct: float = Field(
        default=0.25, validation_alias="BYBIT_BOT_MAX_MARGIN_PER_TRADE_PCT",
    )
    max_positions: int = Field(default=2, validation_alias="BYBIT_BOT_MAX_POSITIONS")

    # EMA Trend Strategy (9/21 — best 1h expectancy per quant-signals.com backtests)
    ema_fast: int = Field(default=9, validation_alias="BYBIT_BOT_EMA_FAST")
    ema_slow: int = Field(default=21, validation_alias="BYBIT_BOT_EMA_SLOW")
    ema_trend: int = Field(default=200, validation_alias="BYBIT_BOT_EMA_TREND")
    adx_threshold: float = Field(default=20.0, validation_alias="BYBIT_BOT_ADX_THRESHOLD")
    volume_filter_ratio: float = Field(default=0.5, validation_alias="BYBIT_BOT_VOLUME_FILTER_RATIO")
    pullback_pct: float = Field(default=0.003, validation_alias="BYBIT_BOT_PULLBACK_PCT")

    # SL / TP / Trailing
    sl_atr_mult: float = Field(default=1.5, validation_alias="BYBIT_BOT_SL_ATR")
    tp_atr_mult: float = Field(default=3.0, validation_alias="BYBIT_BOT_TP_ATR")
    trailing_activation_atr: float = Field(default=1.5, validation_alias="BYBIT_BOT_TRAIL_ACTIVATION_ATR")
    trailing_distance_atr: float = Field(default=1.0, validation_alias="BYBIT_BOT_TRAIL_DISTANCE_ATR")
    time_stop_bars: int = Field(default=48, validation_alias="BYBIT_BOT_TIME_STOP_BARS")

    # Kill Switch ($500 депозит)
    killswitch_max_daily_loss: float = Field(
        default=15.0, validation_alias="BYBIT_BOT_KS_MAX_DAILY_LOSS",
    )
    killswitch_max_drawdown_pct: float = Field(
        default=10.0, validation_alias="BYBIT_BOT_KS_MAX_DRAWDOWN_PCT",
    )
    killswitch_max_positions: int = Field(
        default=3, validation_alias="BYBIT_BOT_KS_MAX_POSITIONS",
    )
    killswitch_max_loss_per_trade: float = Field(
        default=10.0, validation_alias="BYBIT_BOT_KS_MAX_LOSS_PER_TRADE",
    )

    @property
    def scan_symbols(self) -> tuple[str, ...]:
        return _parse_symbols(self.scan_symbols_raw)

    @property
    def stats_db_path(self) -> Path:
        return self.data_dir / "bybit_stats.sqlite"
