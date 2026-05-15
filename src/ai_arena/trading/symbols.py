"""Mapping между Bybit perp symbols и Nof1 coin names.

Source (gist nof1-prompt.md, line 73 + 168) использует голые тикеры
без USDT-суффикса:

    Asset Universe: BTC, ETH, SOL, BNB, DOGE, XRP (perpetual contracts)
    "coin": "BTC" | "ETH" | "SOL" | "BNB" | "DOGE" | "XRP"

Bybit V5 USDT-perp использует `<COIN>USDT` для всех API-вызовов
(get_kline, get_tickers, place_order и т.д. — symbol parameter).

Чтобы LLM видел prompt 1-в-1 с source (Hyperliquid-style голые имена),
а Bybit-клиент дёргался корректным символом — конвертируем на границе
prompt↔API. Это единственное допустимое отклонение для coin naming
(см. правило `.cursor/rules/ai-arena-sources.mdc`, секция «Asset
universe» — Bybit perp formal naming).
"""
from __future__ import annotations

_USDT_SUFFIX = "USDT"


def arena_to_bybit(arena_symbol: str) -> str:
    """`BTC` → `BTCUSDT`. Если уже с суффиксом — без изменения."""
    if arena_symbol.endswith(_USDT_SUFFIX):
        return arena_symbol
    return f"{arena_symbol}{_USDT_SUFFIX}"


def bybit_to_arena(bybit_symbol: str) -> str:
    """`BTCUSDT` → `BTC`. Если без суффикса — без изменения."""
    if bybit_symbol.endswith(_USDT_SUFFIX):
        return bybit_symbol[: -len(_USDT_SUFFIX)]
    return bybit_symbol


def arena_symbols(bybit_symbols: tuple[str, ...]) -> tuple[str, ...]:
    """Кортеж Bybit-символов → кортеж Nof1-coin-имён (для whitelist/prompt)."""
    return tuple(bybit_to_arena(s) for s in bybit_symbols)
