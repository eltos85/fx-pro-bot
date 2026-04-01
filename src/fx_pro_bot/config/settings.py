from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYMBOLS = (
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "AUDUSD=X",
    "USDCAD=X",
    "EURGBP=X",
    "GC=F",
    "SI=F",
    "CL=F",
    "BZ=F",
)

DISPLAY_NAMES: dict[str, str] = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "EURGBP=X": "EUR/GBP",
    "GC=F": "Золото (XAU)",
    "SI=F": "Серебро (XAG)",
    "CL=F": "Нефть WTI",
    "BZ=F": "Нефть Brent",
}

PIP_SIZES: dict[str, float] = {
    "EURUSD=X": 0.0001,
    "GBPUSD=X": 0.0001,
    "USDJPY=X": 0.01,
    "AUDUSD=X": 0.0001,
    "USDCAD=X": 0.0001,
    "EURGBP=X": 0.0001,
    "GC=F": 0.10,
    "SI=F": 0.01,
    "CL=F": 0.01,
    "BZ=F": 0.01,
}


PIP_VALUES_USD: dict[str, float] = {
    "EURUSD=X": 0.10,
    "GBPUSD=X": 0.10,
    "USDJPY=X": 0.07,
    "AUDUSD=X": 0.10,
    "USDCAD=X": 0.07,
    "EURGBP=X": 0.13,
    "GC=F": 0.10,
    "SI=F": 0.50,
    "CL=F": 0.10,
    "BZ=F": 0.10,
}

SPREAD_PIPS: dict[str, float] = {
    "EURUSD=X": 1.5,
    "GBPUSD=X": 1.8,
    "USDJPY=X": 1.5,
    "AUDUSD=X": 1.8,
    "USDCAD=X": 2.2,
    "EURGBP=X": 1.8,
    "GC=F": 3.5,
    "SI=F": 3.5,
    "CL=F": 4.0,
    "BZ=F": 4.0,
}


def pip_value_usd(symbol: str, lot_size: float = 0.01) -> float:
    base = PIP_VALUES_USD.get(symbol, 0.10)
    return base * (lot_size / 0.01)


def spread_cost_pips(symbol: str) -> float:
    return SPREAD_PIPS.get(symbol, 2.0)


def _parse_symbols(raw: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def _parse_horizons(raw: str) -> tuple[int, ...]:
    return tuple(int(s.strip()) for s in raw.split(",") if s.strip())


def display_name(symbol: str) -> str:
    return DISPLAY_NAMES.get(symbol, symbol)


def pip_size(symbol: str) -> float:
    return PIP_SIZES.get(symbol, 0.0001)


class Settings(BaseSettings):
    """Настройки сканера-советника: список инструментов, интервалы, горизонты проверки."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    data_dir: Path = Field(default=Path("data"), validation_alias="DATA_DIR")

    scan_symbols_raw: str = Field(
        default=",".join(DEFAULT_SYMBOLS),
        validation_alias="SCAN_SYMBOLS",
    )

    yfinance_period: str = Field(default="5d", validation_alias="YFINANCE_PERIOD")
    yfinance_interval: str = Field(default="5m", validation_alias="YFINANCE_INTERVAL")

    poll_interval_sec: int = Field(default=300, validation_alias="POLL_INTERVAL_SEC")

    verify_horizons_raw: str = Field(
        default="15,30,60",
        validation_alias="VERIFY_HORIZONS",
    )

    account_balance: float = Field(default=250.0, validation_alias="ACCOUNT_BALANCE")
    lot_size: float = Field(default=0.01, validation_alias="LOT_SIZE")

    fxpro_enabled: bool = Field(default=False, validation_alias="FXPRO_ENABLED")
    fxpro_client_id: str = Field(default="", validation_alias="FXPRO_CLIENT_ID")
    fxpro_client_secret: str = Field(default="", validation_alias="FXPRO_CLIENT_SECRET")
    fxpro_account_id: str = Field(default="", validation_alias="FXPRO_ACCOUNT_ID")
    fxpro_api_url: str = Field(
        default="https://connect.fxpro.com/api/v1",
        validation_alias="FXPRO_API_URL",
    )

    whale_cot_enabled: bool = Field(default=True, validation_alias="WHALE_COT_ENABLED")
    whale_sentiment_enabled: bool = Field(default=False, validation_alias="WHALE_SENTIMENT_ENABLED")
    myfxbook_email: str = Field(default="", validation_alias="MYFXBOOK_EMAIL")
    myfxbook_password: str = Field(default="", validation_alias="MYFXBOOK_PASSWORD")

    leaders_enabled: bool = Field(default=True, validation_alias="LEADERS_ENABLED")
    leaders_max_positions: int = Field(default=20, validation_alias="LEADERS_MAX_POSITIONS")
    leaders_capital_pct: float = Field(default=0.67, validation_alias="LEADERS_CAPITAL_PCT")
    leaders_sl_atr: float = Field(default=2.0, validation_alias="LEADERS_SL_ATR")
    leaders_trail_atr: float = Field(default=0.7, validation_alias="LEADERS_TRAIL_ATR")

    outsiders_enabled: bool = Field(default=True, validation_alias="OUTSIDERS_ENABLED")
    outsiders_max_positions: int = Field(default=50, validation_alias="OUTSIDERS_MAX_POSITIONS")
    outsiders_capital_pct: float = Field(default=0.33, validation_alias="OUTSIDERS_CAPITAL_PCT")

    shadow_enabled: bool = Field(default=True, validation_alias="SHADOW_ENABLED")

    @property
    def scan_symbols(self) -> tuple[str, ...]:
        return _parse_symbols(self.scan_symbols_raw)

    @property
    def verify_horizons(self) -> tuple[int, ...]:
        return _parse_horizons(self.verify_horizons_raw)

    @property
    def stats_db_path(self) -> Path:
        return self.data_dir / "advisor_stats.sqlite"

    @property
    def events_calendar_path(self) -> Path:
        return self.data_dir / "events_calendar.yaml"
