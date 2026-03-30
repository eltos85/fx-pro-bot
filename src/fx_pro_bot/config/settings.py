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

    fxpro_enabled: bool = Field(default=False, validation_alias="FXPRO_ENABLED")
    fxpro_client_id: str = Field(default="", validation_alias="FXPRO_CLIENT_ID")
    fxpro_client_secret: str = Field(default="", validation_alias="FXPRO_CLIENT_SECRET")
    fxpro_account_id: str = Field(default="", validation_alias="FXPRO_ACCOUNT_ID")
    fxpro_api_url: str = Field(
        default="https://connect.fxpro.com/api/v1",
        validation_alias="FXPRO_API_URL",
    )

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
