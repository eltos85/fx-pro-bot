"""Настройки Bybit crypto-бота: API, символы, стратегии, лимиты."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SYMBOLS = (
    # 8 символов по статистике 315 сделок (Bybit API, 2026-04-13).
    # Убраны 27 убыточных: ETH(-$72), BTC(-$33), NEAR(-$14), INJ(-$11) и др.
    "SOLUSDT",    # StatArb +$10.83, WR 44% — прибылен в парном трейдинге
    "ADAUSDT",    # +$3.95, WR 57%
    "LINKUSDT",   # StatArb +$7.60, WR 48% — прибылен в парном трейдинге
    "SUIUSDT",    # +$4.37, WR 57%
    "TONUSDT",    # +$3.66, WR 100% (2 сделки)
    "WIFUSDT",    # +$0.75, WR 67%
    "TIAUSDT",    # +$0.63, WR 100% (2 сделки)
    "DOTUSDT",    # closed -$38 НО open +$38 = нетто $0; прибыль не фиксировалась
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
    min_ensemble_votes: int = Field(default=3, validation_alias="BYBIT_BOT_MIN_VOTES")
    momentum_enabled: bool = Field(default=False, validation_alias="BYBIT_BOT_MOMENTUM_ENABLED")

    # Скальпинг
    scalping_vwap_enabled: bool = Field(
        default=True, validation_alias="BYBIT_BOT_SCALP_VWAP_ENABLED",
    )
    scalping_statarb_enabled: bool = Field(
        default=True, validation_alias="BYBIT_BOT_SCALP_STATARB_ENABLED",
    )
    scalping_funding_enabled: bool = Field(
        default=False, validation_alias="BYBIT_BOT_SCALP_FUNDING_ENABLED",
    )
    scalping_volume_enabled: bool = Field(
        default=True, validation_alias="BYBIT_BOT_SCALP_VOLUME_ENABLED",
    )
    scalping_orb_enabled: bool = Field(
        default=False, validation_alias="BYBIT_BOT_SCALP_ORB_ENABLED",
    )
    # ORB whitelist-ы — по итогам backtest 90д 2026-04-23 (BUILDLOG_BYBIT.md).
    # Формат: CSV-строка, пустая строка = "без ограничений" (все варианты).
    # Дефолт = пустые → обратная совместимость: ORB работает как раньше.
    scalping_orb_sessions: str = Field(
        default="", validation_alias="BYBIT_BOT_SCALP_ORB_SESSIONS",
    )
    scalping_orb_symbols: str = Field(
        default="", validation_alias="BYBIT_BOT_SCALP_ORB_SYMBOLS",
    )
    scalping_orb_direction: str = Field(
        default="", validation_alias="BYBIT_BOT_SCALP_ORB_DIRECTION",
    )
    scalping_turtle_enabled: bool = Field(
        default=False, validation_alias="BYBIT_BOT_SCALP_TURTLE_ENABLED",
    )
    scalping_leadlag_enabled: bool = Field(
        default=False, validation_alias="BYBIT_BOT_SCALP_LEADLAG_ENABLED",
    )
    # BTC нужен только как REFERENCE для BTC-Lead-Lag: грузим бары,
    # но в торговых стратегиях НЕ открываем позиции (убыточен в скальпе).
    leadlag_reference_symbol: str = Field(
        default="BTCUSDT",
        validation_alias="BYBIT_BOT_LEADLAG_REF_SYMBOL",
    )
    scalping_max_positions: int = Field(
        default=3, validation_alias="BYBIT_BOT_SCALP_MAX_POSITIONS",
    )

    # Сессионный фильтр: блокировка входов в dead zone (22-07 UTC).
    # По статистике 258 сделок: dead zone = -$147 (72% потерь), WR 31%.
    session_filter_enabled: bool = Field(default=True, validation_alias="BYBIT_BOT_SESSION_FILTER")
    session_start_utc: int = Field(default=7, validation_alias="BYBIT_BOT_SESSION_START")
    session_end_utc: int = Field(default=22, validation_alias="BYBIT_BOT_SESSION_END")

    # Kill Switch — лимиты для демо-торговли.
    # Daily loss 7.5% = $37.50 при $500 (больше свободы на демо).
    # Drawdown 25% = $125 от пика (при переходе на реал — снизить до 10%).
    # Per-trade loss не ограничен отдельно: биржевой SL = 2 ATR уже даёт
    # структурный лимит, дублирующий ks-проверка только мешала exit-логике.
    #
    # На demo KS отключаем: demo-баланс $177k, просадка $45k = 25% триггерит
    # стоп даже при микро-убытках по стратегиям. KS нужен только на реале,
    # где account_balance=500 и лимиты имеют смысл.
    killswitch_enabled: bool = Field(
        default=True, validation_alias="BYBIT_BOT_KS_ENABLED",
    )
    killswitch_max_daily_loss: float = Field(
        default=37.50, validation_alias="BYBIT_BOT_KS_MAX_DAILY_LOSS",
    )
    killswitch_max_drawdown_pct: float = Field(
        default=25.0, validation_alias="BYBIT_BOT_KS_MAX_DRAWDOWN_PCT",
    )
    killswitch_max_positions: int = Field(
        default=5, validation_alias="BYBIT_BOT_KS_MAX_POSITIONS",
    )

    @property
    def scan_symbols(self) -> tuple[str, ...]:
        return _parse_symbols(self.scan_symbols_raw)

    @property
    def stats_db_path(self) -> Path:
        return self.data_dir / "bybit_stats.sqlite"
