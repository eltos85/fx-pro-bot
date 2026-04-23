from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYMBOLS = (
    # FX majors (7)
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "AUDUSD=X",
    "USDCAD=X",
    "NZDUSD=X",
    "USDCHF=X",
    # FX crosses (3) — ORB-совместимые при ликвидных сессиях
    "EURJPY=X",
    "GBPJPY=X",
    "EURGBP=X",
    # Commodities (4) — возвращены для ORB/news_fade на NY-сессии
    # По данным 09-22.04 выборка <30 → правило sample-size запрещает
    # делать вывод об убыточности. Собираем статистику.
    "GC=F",   # Gold
    "CL=F",   # WTI Crude
    "BZ=F",   # Brent
    "NG=F",   # Natural Gas
    # Indices (2) — классические ORB-инструменты на US open 14:30 UTC
    "ES=F",   # S&P 500 futures
    "NQ=F",   # Nasdaq 100 futures
)

# Инструменты, которые явно исключаются из скальпинга.
# Пусто: все инструменты из DEFAULT_SYMBOLS участвуют в скальпинге.
# JPY/EUR-crosses вернулись — они работали на ORB (GBPJPY PF 1.25 за 29 сделок).
SCALPING_EXCLUDE_SYMBOLS: frozenset[str] = frozenset()

SCALPING_CRYPTO_ALLOWED: frozenset[str] = frozenset()

CRYPTO_SYMBOLS: frozenset[str] = frozenset({
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD",
    "ADA-USD", "LINK-USD", "AVAX-USD", "LTC-USD", "BNB-USD", "DOT-USD",
})


def is_crypto(instrument: str) -> bool:
    return instrument in CRYPTO_SYMBOLS

SCALPING_EXTRA_SYMBOLS: tuple[str, ...] = ()

DISPLAY_NAMES: dict[str, str] = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "EURGBP=X": "EUR/GBP",
    "USDCHF=X": "USD/CHF",
    "EURJPY=X": "EUR/JPY",
    "GBPJPY=X": "GBP/JPY",
    "NZDUSD=X": "NZD/USD",
    "GC=F": "Золото (XAU)",
    "SI=F": "Серебро (XAG)",
    "CL=F": "Нефть WTI",
    "BZ=F": "Нефть Brent",
    "NG=F": "Газ (NG)",
    "HG=F": "Медь (HG)",
    "PL=F": "Платина (PL)",
    "ES=F": "S&P 500",
    "NQ=F": "Nasdaq 100",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "SOL-USD": "Solana",
    "XRP-USD": "XRP",
    "DOGE-USD": "Dogecoin",
    "ADA-USD": "Cardano",
    "LINK-USD": "Chainlink",
    "AVAX-USD": "Avalanche",
    "LTC-USD": "Litecoin",
    "BNB-USD": "BNB",
    "DOT-USD": "Polkadot",
    "ZN=F": "US 10Y Bond",
}

PIP_SIZES: dict[str, float] = {
    "EURUSD=X": 0.0001,
    "GBPUSD=X": 0.0001,
    "USDJPY=X": 0.01,
    "AUDUSD=X": 0.0001,
    "USDCAD=X": 0.0001,
    "EURGBP=X": 0.0001,
    "USDCHF=X": 0.0001,
    "EURJPY=X": 0.01,
    "GBPJPY=X": 0.01,
    "NZDUSD=X": 0.0001,
    "GC=F": 0.10,
    "SI=F": 0.01,
    "CL=F": 0.01,
    "BZ=F": 0.01,
    "NG=F": 0.001,
    "HG=F": 0.0005,
    "PL=F": 0.10,
    "ES=F": 0.25,
    "NQ=F": 0.25,
    "BTC-USD": 1.0,
    "ETH-USD": 0.10,
    "SOL-USD": 0.01,
    "XRP-USD": 0.0001,
    "DOGE-USD": 0.00001,
    "ADA-USD": 0.0001,
    "LINK-USD": 0.01,
    "AVAX-USD": 0.01,
    "LTC-USD": 0.10,
    "BNB-USD": 0.10,
    "DOT-USD": 0.001,
    "ZN=F": 0.01,
}


PIP_VALUES_USD: dict[str, float] = {
    "EURUSD=X": 0.10,
    "GBPUSD=X": 0.10,
    "USDJPY=X": 0.07,
    "AUDUSD=X": 0.10,
    "USDCAD=X": 0.07,
    "EURGBP=X": 0.13,
    "USDCHF=X": 0.10,
    "EURJPY=X": 0.07,
    "GBPJPY=X": 0.07,
    "NZDUSD=X": 0.10,
    "GC=F": 0.10,
    "SI=F": 0.50,
    "CL=F": 0.10,
    "BZ=F": 0.10,
    "NG=F": 0.10,
    "HG=F": 0.10,
    "PL=F": 0.10,
    "ES=F": 0.125,
    "NQ=F": 0.05,
    "BTC-USD": 0.01,
    "ETH-USD": 0.01,
    "SOL-USD": 0.01,
    "XRP-USD": 0.01,
    "DOGE-USD": 0.01,
    "ADA-USD": 0.01,
    "LINK-USD": 0.01,
    "AVAX-USD": 0.01,
    "LTC-USD": 0.01,
    "BNB-USD": 0.01,
    "DOT-USD": 0.01,
    "ZN=F": 0.10,
}

SPREAD_PIPS: dict[str, float] = {
    "EURUSD=X": 1.5,
    "GBPUSD=X": 1.8,
    "USDJPY=X": 1.5,
    "AUDUSD=X": 1.8,
    "USDCAD=X": 2.2,
    "EURGBP=X": 1.8,
    "USDCHF=X": 1.8,
    "EURJPY=X": 2.0,
    "GBPJPY=X": 2.5,
    "NZDUSD=X": 2.0,
    "GC=F": 3.5,
    "SI=F": 3.5,
    "CL=F": 4.0,
    "BZ=F": 4.0,
    "NG=F": 5.0,
    "HG=F": 3.0,
    "PL=F": 4.0,
    "ES=F": 2.0,
    "NQ=F": 3.0,
    "BTC-USD": 8.0,
    "ETH-USD": 4.0,
    "SOL-USD": 5.0,
    "XRP-USD": 5.0,
    "DOGE-USD": 5.0,
    "ADA-USD": 5.0,
    "LINK-USD": 5.0,
    "AVAX-USD": 5.0,
    "LTC-USD": 5.0,
    "BNB-USD": 5.0,
    "DOT-USD": 5.0,
    "ZN=F": 1.5,
}


def pip_value_usd(symbol: str, lot_size: float = 0.01) -> float:
    base = PIP_VALUES_USD.get(symbol, 0.10)
    return base * (lot_size / 0.01)


_QUOTE_TO_USD: dict[str, float] = {
    "JPY": 1 / 150.0,
    "CAD": 1 / 1.37,
    "CHF": 1 / 0.88,
    "GBP": 1.27,
}

_USD_QUOTED = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "XAGUSD", "XAUUSD"}
_COMMODITY_USD = {"GC=F", "SI=F", "CL=F", "BZ=F", "NG=F", "HG=F", "PL=F",
                  "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD",
                  "ADA-USD", "LINK-USD", "AVAX-USD", "LTC-USD", "BNB-USD",
                  "DOT-USD", "ES=F", "NQ=F", "ZN=F"}


def pip_value_from_volume(symbol: str, broker_volume: int) -> float:
    """Pip value в USD по фактическому cTrader volume (units×100)."""
    units = broker_volume / 100.0
    ps = pip_size(symbol)
    raw = units * ps

    sym_upper = symbol.replace("=X", "").replace("=F", "").replace("-", "")

    if symbol in _COMMODITY_USD or any(sym_upper.endswith(q) for q in ("USD",)):
        return raw

    for ccy, rate in _QUOTE_TO_USD.items():
        if ccy in sym_upper[3:]:
            return raw * rate

    return raw


FXPRO_COMMISSION_PER_SIDE = 3.50  # USD за 1.0 лот за сторону (cTrader Raw+)


def broker_commission_usd(lot_size: float = 0.01) -> float:
    """Round-trip комиссия FxPro cTrader Raw+ в USD."""
    return FXPRO_COMMISSION_PER_SIDE * lot_size * 2


def spread_cost_pips(symbol: str) -> float:
    return SPREAD_PIPS.get(symbol, 2.0)


# ATR-scaled position sizing
#
# Research: Van K. Tharp «Trade Your Way to Financial Freedom» (2007), ch.11,
# «Position sizing»; Ralph Vince «The Mathematics of Money Management» (1992).
# Стандарт systematic trading — fixed fractional risk 0.5-1% на сделку.
# При депозите $1500 → $15 = 1% per trade.
MIN_LOT_SIZE = 0.01
MAX_LOT_SIZE = 0.20  # предохранитель от overleveraged при очень tight SL


def calc_lot_size(
    instrument: str,
    sl_distance: float,
    risk_usd: float,
    *,
    min_lot: float = MIN_LOT_SIZE,
    max_lot: float = MAX_LOT_SIZE,
) -> float:
    """Рассчитать лот, чтобы потенциальный убыток при SL равнялся `risk_usd`.

    Формула: lot = risk_usd / (sl_pips × pip_value_per_lot).

    Args:
        instrument: yfinance-символ (e.g. "EURUSD=X", "GC=F", "NG=F")
        sl_distance: расстояние от entry до SL в единицах цены
            (например, 1.5 × ATR).
        risk_usd: желаемый риск в USD (стандарт: 1% депозита).
        min_lot / max_lot: ограничители (защита от нулевого ATR или extreme).

    Returns:
        Округлённый лот-сайз, шаг 0.01.
    """
    ps = pip_size(instrument)
    if ps <= 0 or sl_distance <= 0 or risk_usd <= 0:
        return min_lot

    sl_pips = sl_distance / ps
    pip_val_per_001 = pip_value_usd(instrument, lot_size=0.01)
    if sl_pips <= 0 or pip_val_per_001 <= 0:
        return min_lot

    risk_per_001 = sl_pips * pip_val_per_001
    if risk_per_001 <= 0:
        return min_lot

    lot = (risk_usd / risk_per_001) * 0.01
    lot = round(lot * 100) / 100
    return max(min_lot, min(lot, max_lot))


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

    yfinance_period: str = Field(default="1mo", validation_alias="YFINANCE_PERIOD")
    yfinance_interval: str = Field(default="5m", validation_alias="YFINANCE_INTERVAL")

    poll_interval_sec: int = Field(default=300, validation_alias="POLL_INTERVAL_SEC")

    verify_horizons_raw: str = Field(
        default="15,30,60",
        validation_alias="VERIFY_HORIZONS",
    )

    account_balance: float = Field(default=250.0, validation_alias="ACCOUNT_BALANCE")
    lot_size: float = Field(default=0.01, validation_alias="LOT_SIZE")
    # ATR-scaled risk per trade (USD). 1% от $1500 депозита = $15.
    # [Van K. Tharp «Trade Your Way to Financial Freedom» (2007) ch.11].
    # При risk=0 → fallback на фиксированный lot_size (обратная совместимость).
    risk_per_trade_usd: float = Field(default=15.0, validation_alias="RISK_PER_TRADE_USD")

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
    # max_positions снижены 23.04.2026 (см. BUILDLOG):
    # Research (Tharp, Vince) рекомендует 6-12 concurrent positions для
    # контроля correlation risk. Ранее 20/50/15 создавали overleveraged book.
    leaders_max_positions: int = Field(default=10, validation_alias="LEADERS_MAX_POSITIONS")
    leaders_capital_pct: float = Field(default=0.67, validation_alias="LEADERS_CAPITAL_PCT")
    leaders_sl_atr: float = Field(default=2.0, validation_alias="LEADERS_SL_ATR")
    leaders_trail_atr: float = Field(default=0.7, validation_alias="LEADERS_TRAIL_ATR")

    outsiders_enabled: bool = Field(default=True, validation_alias="OUTSIDERS_ENABLED")
    outsiders_max_positions: int = Field(default=10, validation_alias="OUTSIDERS_MAX_POSITIONS")
    outsiders_max_per_instrument: int = Field(default=1, validation_alias="OUTSIDERS_MAX_PER_INSTRUMENT")
    outsiders_capital_pct: float = Field(default=0.33, validation_alias="OUTSIDERS_CAPITAL_PCT")
    outsiders_mode: str = Field(default="confirmed", validation_alias="OUTSIDERS_MODE")

    shadow_enabled: bool = Field(default=True, validation_alias="SHADOW_ENABLED")

    # Скальпинг-стратегии
    scalping_vwap_enabled: bool = Field(default=True, validation_alias="SCALPING_VWAP_ENABLED")
    scalping_statarb_enabled: bool = Field(default=True, validation_alias="SCALPING_STATARB_ENABLED")
    scalping_orb_enabled: bool = Field(default=True, validation_alias="SCALPING_ORB_ENABLED")
    scalping_max_positions: int = Field(default=10, validation_alias="SCALPING_MAX_POSITIONS")

    # cTrader Open API (автоторговля)
    ctrader_trading_enabled: bool = Field(
        default=False, validation_alias="CTRADER_TRADING_ENABLED",
    )
    ctrader_host_type: str = Field(default="demo", validation_alias="CTRADER_HOST_TYPE")
    ctrader_client_id: str = Field(default="", validation_alias="CTRADER_CLIENT_ID")
    ctrader_client_secret: str = Field(default="", validation_alias="CTRADER_CLIENT_SECRET")
    ctrader_account_id: int = Field(default=0, validation_alias="CTRADER_ACCOUNT_ID")
    ctrader_redirect_uri: str = Field(
        default="https://openapi.ctrader.com/apps/token",
        validation_alias="CTRADER_REDIRECT_URI",
    )

    # Kill Switch (защита от потерь)
    killswitch_max_daily_loss: float = Field(
        default=50.0, validation_alias="KILLSWITCH_MAX_DAILY_LOSS",
    )
    killswitch_max_drawdown_pct: float = Field(
        default=20.0, validation_alias="KILLSWITCH_MAX_DRAWDOWN_PCT",
    )
    killswitch_max_positions: int = Field(
        default=10, validation_alias="KILLSWITCH_MAX_POSITIONS",
    )
    killswitch_max_loss_per_trade: float = Field(
        default=25.0, validation_alias="KILLSWITCH_MAX_LOSS_PER_TRADE",
    )

    @property
    def ctrader_token_path(self) -> Path:
        return self.data_dir / "ctrader_tokens.json"

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
