from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки советника: без логина к брокеру, только данные и пути к статистике."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Каталог данных (в Docker монтируется как том)
    data_dir: Path = Field(default=Path("data"), validation_alias="DATA_DIR")

    # Источник котировок: stub — синтетика; yfinance — публичные данные Yahoo (без ключа API)
    data_source: str = Field(default="stub", validation_alias="DATA_SOURCE")

    # Для yfinance: тикер Yahoo (например EURUSD=X, CL=F для нефти WTI)
    yfinance_symbol: str = Field(default="EURUSD=X", validation_alias="YFINANCE_SYMBOL")
    yfinance_period: str = Field(default="1mo", validation_alias="YFINANCE_PERIOD")
    yfinance_interval: str = Field(default="1h", validation_alias="YFINANCE_INTERVAL")

    # Инструмент для отображения (человекочитаемо)
    display_name: str = Field(default="EUR/USD", validation_alias="DISPLAY_NAME")

    @property
    def stats_db_path(self) -> Path:
        return self.data_dir / "advisor_stats.sqlite"

    @property
    def events_calendar_path(self) -> Path:
        return self.data_dir / "events_calendar.yaml"
