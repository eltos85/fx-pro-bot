"""Маппинг символов yfinance ↔ cTrader и кеш symbolId."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

log = logging.getLogger(__name__)

_FUTURES_MONTH_CODES: dict[str, int] = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

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
    # Spot energy CFDs (futures contracts на FxPro часто отключены)
    "NG=F": "NAT.GAS",
    "BZ=F": "BRENT",
}

_YFINANCE_PREFIX_MAP: dict[str, str] = {
    "CL=F": "#USOIL",
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

        candidates = [
            name for name in self._by_name
            if name.startswith(prefix.upper() + "_")
        ]
        if not candidates:
            return None

        best = _pick_front_month(candidates)
        if best:
            sym = self._by_name[best]
            log.info("Prefix match: %s → %s (id=%d)", yf_symbol, sym.name, sym.symbol_id)
            return sym

        return None

    @property
    def loaded(self) -> bool:
        return len(self._by_name) > 0


def _pick_front_month(names: list[str]) -> str | None:
    """Выбрать ближайший активный фьючерсный контракт из списка имён.

    Формат суффикса: _X26 где X — код месяца (F..Z), 26 — год.
    Фьючерсы экспирируют до начала месяца поставки, поэтому
    берём контракт строго > текущего месяца.
    """
    today = date.today()
    current = (today.year % 100, today.month)

    parsed: list[tuple[int, int, str]] = []
    for name in names:
        parts = name.rsplit("_", 1)
        if len(parts) != 2 or len(parts[1]) < 2:
            continue
        month_code = parts[1][0].upper()
        year_str = parts[1][1:]
        month = _FUTURES_MONTH_CODES.get(month_code)
        if month is None:
            continue
        try:
            year = int(year_str)
        except ValueError:
            continue
        if (year, month) > current:
            parsed.append((year, month, name))

    if not parsed:
        return names[0] if names else None

    parsed.sort()
    return parsed[0][2]


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
