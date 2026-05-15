"""Тесты Bybit↔Nof1 coin naming маппинга.

Source (gist nof1-prompt.md L73, L168): голые тикеры без USDT.
Bybit V5 USDT-perp требует `<COIN>USDT` для API-вызовов.
Маппинг — единственное допустимое отклонение для coin naming
(см. правило `.cursor/rules/ai-arena-sources.mdc`).
"""
from __future__ import annotations

from ai_arena.trading.symbols import (
    arena_symbols,
    arena_to_bybit,
    bybit_to_arena,
)


class TestArenaToBybit:
    def test_adds_usdt_suffix(self):
        assert arena_to_bybit("BTC") == "BTCUSDT"
        assert arena_to_bybit("ETH") == "ETHUSDT"
        assert arena_to_bybit("DOGE") == "DOGEUSDT"

    def test_idempotent_when_already_bybit(self):
        assert arena_to_bybit("BTCUSDT") == "BTCUSDT"


class TestBybitToArena:
    def test_strips_usdt_suffix(self):
        assert bybit_to_arena("BTCUSDT") == "BTC"
        assert bybit_to_arena("ETHUSDT") == "ETH"
        assert bybit_to_arena("SOLUSDT") == "SOL"

    def test_idempotent_when_already_arena(self):
        assert bybit_to_arena("BTC") == "BTC"


class TestArenaSymbols:
    def test_full_nof1_universe(self):
        bybit = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")
        assert arena_symbols(bybit) == ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE")

    def test_empty(self):
        assert arena_symbols(()) == ()
