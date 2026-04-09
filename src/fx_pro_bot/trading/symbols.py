"""Маппинг символов yfinance ↔ cTrader и кеш symbolId."""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

YFINANCE_TO_CTRADER: dict[str, str] = {
    # Forex
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "USDJPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD",
    "USDCAD=X": "USDCAD",
    "EURGBP=X": "EURGBP",
    "USDCHF=X": "USDCHF",
    "EURJPY=X": "EURJPY",
    "GBPJPY=X": "GBPJPY",
    # Spot commodities
    "GC=F": "XAUUSD",
    "HG=F": "COPPER",
    "PL=F": "XPTUSD",
    # Crypto
    "BTC-USD": "BITCOIN",
    "ETH-USD": "ETHEREUM",
}

_YFINANCE_PREFIX_MAP: dict[str, str] = {
    "CL=F": "#USOIL",
    "BZ=F": "#UKOIL",
    "NG=F": "#NGAS",
    "ES=F": "#US500",
    "NQ=F": "#USTEC",
}

CTRADER_TO_YFINANCE: dict[str, str] = {v: k for k, v in YFINANCE_TO_CTRADER.items()}


@dataclass(slots=True)
class SymbolInfo:
    symbol_id: int
    name: str
    min_volume: int
    max_volume: int
    step_volume: int
    digits: int
    contract_size: int = 100_000


class SymbolCache:
    """Кеш символов cTrader: name → SymbolInfo."""

    def __init__(self) -> None:
        self._by_name: dict[str, SymbolInfo] = {}
        self._by_id: dict[int, SymbolInfo] = {}

    def populate(self, symbols: list[SymbolInfo]) -> None:
        for s in symbols:
            self._by_name[s.name.upper()] = s
            self._by_id[s.symbol_id] = s
        log.info("SymbolCache: загружено %d символов", len(symbols))

    def get_by_name(self, ctrader_name: str) -> SymbolInfo | None:
        return self._by_name.get(ctrader_name.upper())

    def get_by_id(self, symbol_id: int) -> SymbolInfo | None:
        return self._by_id.get(symbol_id)

    def resolve_yfinance(self, yf_symbol: str) -> SymbolInfo | None:
        ctrader_name = YFINANCE_TO_CTRADER.get(yf_symbol)
        if ctrader_name is not None:
            return self.get_by_name(ctrader_name)

        prefix = _YFINANCE_PREFIX_MAP.get(yf_symbol)
        if prefix is None:
            return None

        exact = self.get_by_name(prefix)
        if exact:
            return exact

        candidates = sorted(
            (name for name in self._by_name if name.startswith(prefix.upper() + "_")),
        )
        if candidates:
            sym = self._by_name[candidates[0]]
            log.info("Prefix match: %s → %s (id=%d)", yf_symbol, sym.name, sym.symbol_id)
            return sym

        return None

    @property
    def loaded(self) -> bool:
        return len(self._by_name) > 0


def lots_to_volume(lots: float, contract_size: int = 10_000_000) -> int:
    """Конвертация лотов → cTrader volume.

    cTrader lotSize = contract_size (уже в volume-единицах, т.е. units×100).
    Для Forex: lotSize=10_000_000 → 1 лот = 100k units.
    Для Silver: lotSize=500_000 → 1 лот = 5000 oz.
    """
    return int(round(lots * contract_size))


def volume_to_lots(volume: int, contract_size: int = 10_000_000) -> float:
    return volume / contract_size


def price_to_relative(price_diff: float) -> int:
    """Абсолютная разница цены → относительное значение cTrader (1/100000)."""
    return int(round(abs(price_diff) * 100_000))
