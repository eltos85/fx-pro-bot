"""Настройки Bybit crypto-бота: API, символы, стратегии, лимиты."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SYMBOLS = (
    # Majors (BTCUSDT убран: 12% WR, -$33 за 24 сделки)
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    # Large-cap alts (DOGEUSDT убран: 13% WR, -$16; AAVEUSDT убран: 0% WR, -$17)
    "ADAUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "NEARUSDT",
    "APTUSDT",
    "ARBUSDT",
    "SUIUSDT",
    "UNIUSDT",
    "ATOMUSDT",
    "TRXUSDT",
    # Mid-cap alts (HBARUSDT убран: 0% WR, -$6)
    "FILUSDT",
    "INJUSDT",
    "FETUSDT",
    "RENDERUSDT",
    "TONUSDT",
    "SEIUSDT",
    "TIAUSDT",
    "ONDOUSDT",
    "PENDLEUSDT",
    "WLDUSDT",
    "OPUSDT",
    "RUNEUSDT",
    "ALGOUSDT",
    # Stat-Arb (legacy PoW forks, corr 0.75+)
    "ETCUSDT",
    "BCHUSDT",
    # Meme
    "SHIBUSDT",
    "PEPEUSDT",
    "WIFUSDT",
    "BONKUSDT",
    "FLOKIUSDT",
)

DISPLAY_NAMES: dict[str, str] = {
    "BTCUSDT": "Bitcoin",
    "ETHUSDT": "Ethereum",
    "SOLUSDT": "Solana",
    "XRPUSDT": "XRP",
    "BNBUSDT": "BNB",
    "DOGEUSDT": "Dogecoin",
    "ADAUSDT": "Cardano",
    "LINKUSDT": "Chainlink",
    "AVAXUSDT": "Avalanche",
    "LTCUSDT": "Litecoin",
    "DOTUSDT": "Polkadot",
    "NEARUSDT": "NEAR",
    "APTUSDT": "Aptos",
    "ARBUSDT": "Arbitrum",
    "SUIUSDT": "Sui",
    "UNIUSDT": "Uniswap",
    "AAVEUSDT": "Aave",
    "ATOMUSDT": "Cosmos",
    "TRXUSDT": "TRON",
    "FILUSDT": "Filecoin",
    "INJUSDT": "Injective",
    "FETUSDT": "Fetch.ai",
    "RENDERUSDT": "Render",
    "TONUSDT": "Toncoin",
    "SEIUSDT": "Sei",
    "TIAUSDT": "Celestia",
    "ONDOUSDT": "Ondo",
    "PENDLEUSDT": "Pendle",
    "WLDUSDT": "Worldcoin",
    "OPUSDT": "Optimism",
    "HBARUSDT": "Hedera",
    "RUNEUSDT": "THORChain",
    "ALGOUSDT": "Algorand",
    "ETCUSDT": "Ethereum Classic",
    "BCHUSDT": "Bitcoin Cash",
    "SHIBUSDT": "Shiba Inu",
    "PEPEUSDT": "Pepe",
    "WIFUSDT": "dogwifhat",
    "BONKUSDT": "Bonk",
    "FLOKIUSDT": "Floki",
}

BYBIT_TO_YFINANCE: dict[str, str] = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "SOLUSDT": "SOL-USD",
    "XRPUSDT": "XRP-USD",
    "BNBUSDT": "BNB-USD",
    "DOGEUSDT": "DOGE-USD",
    "ADAUSDT": "ADA-USD",
    "LINKUSDT": "LINK-USD",
    "AVAXUSDT": "AVAX-USD",
    "LTCUSDT": "LTC-USD",
    "DOTUSDT": "DOT-USD",
    "NEARUSDT": "NEAR-USD",
    "APTUSDT": "APT21794-USD",
    "ARBUSDT": "ARB-USD",
    "SUIUSDT": "SUI20947-USD",
    "UNIUSDT": "UNI7083-USD",
    "AAVEUSDT": "AAVE-USD",
    "ATOMUSDT": "ATOM-USD",
    "TRXUSDT": "TRX-USD",
    "FILUSDT": "FIL-USD",
    "INJUSDT": "INJ-USD",
    "FETUSDT": "FET-USD",
    "RENDERUSDT": "RENDER-USD",
    "TONUSDT": "TON11419-USD",
    "SEIUSDT": "SEI-USD",
    "TIAUSDT": "TIA-USD",
    "ONDOUSDT": "ONDO-USD",
    "PENDLEUSDT": "PENDLE-USD",
    "WLDUSDT": "WLD-USD",
    "OPUSDT": "OP-USD",
    "HBARUSDT": "HBAR-USD",
    "RUNEUSDT": "RUNE-USD",
    "ALGOUSDT": "ALGO-USD",
    "ETCUSDT": "ETC-USD",
    "BCHUSDT": "BCH-USD",
    "SHIBUSDT": "SHIB-USD",
    "PEPEUSDT": "PEPE24478-USD",
    "WIFUSDT": "WIF-USD",
    "BONKUSDT": "BONK-USD",
    "FLOKIUSDT": "FLOKI-USD",
}

YFINANCE_TO_BYBIT: dict[str, str] = {v: k for k, v in BYBIT_TO_YFINANCE.items()}


def display_name(symbol: str) -> str:
    return DISPLAY_NAMES.get(symbol, symbol)


def to_yfinance(bybit_symbol: str) -> str:
    return BYBIT_TO_YFINANCE.get(bybit_symbol, bybit_symbol)


def to_bybit(yfinance_symbol: str) -> str:
    return YFINANCE_TO_BYBIT.get(yfinance_symbol, yfinance_symbol)


def _parse_symbols(raw: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in raw.split(",") if s.strip())


class Settings(BaseSettings):
    """Настройки крипто-бота Bybit."""

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

    # Рынок: linear (USDT-perpetual) по умолчанию
    category: str = Field(default="linear", validation_alias="BYBIT_BOT_CATEGORY")

    # Инструменты
    scan_symbols_raw: str = Field(
        default=",".join(DEFAULT_SYMBOLS),
        validation_alias="BYBIT_BOT_SCAN_SYMBOLS",
    )

    # Рыночные данные
    yfinance_period: str = Field(default="5d", validation_alias="BYBIT_BOT_YFINANCE_PERIOD")
    yfinance_interval: str = Field(default="5m", validation_alias="BYBIT_BOT_YFINANCE_INTERVAL")
    poll_interval_sec: int = Field(default=300, validation_alias="BYBIT_BOT_POLL_INTERVAL_SEC")

    # Баланс / позиции
    # Defaults рассчитаны на микро-счёт $500.
    # Формула: effective_risk = balance × capital_per_trade_pct / leverage.
    # При balance=500, pct=0.05, leverage=5: $500 × 0.05 / 5 = $5 = 1% risk per trade.
    account_balance: float = Field(default=500.0, validation_alias="BYBIT_BOT_ACCOUNT_BALANCE")
    leverage: int = Field(default=5, validation_alias="BYBIT_BOT_LEVERAGE")
    capital_per_trade_pct: float = Field(
        default=0.05, validation_alias="BYBIT_BOT_CAPITAL_PER_TRADE_PCT",
    )
    max_margin_per_trade_pct: float = Field(
        default=0.25, validation_alias="BYBIT_BOT_MAX_MARGIN_PER_TRADE_PCT",
    )

    # Стратегия
    strategy_sl_atr_mult: float = Field(
        default=2.0, validation_alias="BYBIT_BOT_STRATEGY_SL_ATR",
    )
    strategy_tp_atr_mult: float = Field(
        default=3.0, validation_alias="BYBIT_BOT_STRATEGY_TP_ATR",
    )
    strategy_trail_atr_mult: float = Field(
        default=1.0, validation_alias="BYBIT_BOT_STRATEGY_TRAIL_ATR",
    )
    max_positions: int = Field(default=3, validation_alias="BYBIT_BOT_MAX_POSITIONS")
    min_ensemble_votes: int = Field(default=3, validation_alias="BYBIT_BOT_MIN_VOTES")

    # Скальпинг
    scalping_vwap_enabled: bool = Field(
        default=True, validation_alias="BYBIT_BOT_SCALP_VWAP_ENABLED",
    )
    scalping_statarb_enabled: bool = Field(
        default=True, validation_alias="BYBIT_BOT_SCALP_STATARB_ENABLED",
    )
    scalping_funding_enabled: bool = Field(
        default=True, validation_alias="BYBIT_BOT_SCALP_FUNDING_ENABLED",
    )
    scalping_volume_enabled: bool = Field(
        default=True, validation_alias="BYBIT_BOT_SCALP_VOLUME_ENABLED",
    )
    scalping_max_positions: int = Field(
        default=3, validation_alias="BYBIT_BOT_SCALP_MAX_POSITIONS",
    )

    # Kill Switch — лимиты для демо-торговли.
    # Daily loss 7.5% = $37.50 при $500 (больше свободы на демо).
    # Drawdown 25% = $125 от пика (при переходе на реал — снизить до 10%).
    # Max loss per trade 2.5% = $12.50 (буфер для проскальзывания).
    killswitch_max_daily_loss: float = Field(
        default=37.50, validation_alias="BYBIT_BOT_KS_MAX_DAILY_LOSS",
    )
    killswitch_max_drawdown_pct: float = Field(
        default=25.0, validation_alias="BYBIT_BOT_KS_MAX_DRAWDOWN_PCT",
    )
    killswitch_max_positions: int = Field(
        default=5, validation_alias="BYBIT_BOT_KS_MAX_POSITIONS",
    )
    killswitch_max_loss_per_trade: float = Field(
        default=12.50, validation_alias="BYBIT_BOT_KS_MAX_LOSS_PER_TRADE",
    )

    @property
    def scan_symbols(self) -> tuple[str, ...]:
        return _parse_symbols(self.scan_symbols_raw)

    @property
    def stats_db_path(self) -> Path:
        return self.data_dir / "bybit_stats.sqlite"
