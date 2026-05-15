"""Тесты форматтера market context для AI Arena.

Source: gist nof1-prompt.md, секция «User Prompt 完整逆向» (L332-486).
Особое внимание:
- per-symbol header `### ALL <COIN> DATA` без USDT (L345);
- open positions block — Python repr-style (L457-478, не JSON);
- Funding Rate без `+` модификатора (L355);
- signed quantity для positions без отдельного `'side'` поля.
"""
from __future__ import annotations

from ai_arena.state.db import ArenaPosition
from ai_arena.trading.context import (
    SymbolBlock,
    _python_repr_list,
    format_open_positions_block,
    format_symbol_block,
)


def _make_position(
    *,
    pid: int = 1,
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    qty: float = 0.5,
    entry: float = 100000.0,
    sl: float | None = 99000.0,
    tp: float | None = 102000.0,
    leverage: int = 5,
    confidence: float | None = 0.7,
    invalidation: str | None = "BTC < 98k",
    risk_usd: float | None = 500.0,
) -> ArenaPosition:
    return ArenaPosition(
        id=pid,
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        leverage=leverage,
        order_link_id=f"arena_test_{pid}",
        opened_at="2026-05-15T09:00:00+00:00",
        closed_at=None,
        exit_price=None,
        realized_pnl_usd=None,
        close_reason=None,
        llm_justification="test",
        confidence=confidence,
        invalidation_condition=invalidation,
        risk_usd=risk_usd,
    )


class TestSymbolBlockHeader:
    def test_header_uses_arena_naming_no_usdt(self):
        # gist L345: `### ALL BTC DATA` (без USDT суффикса)
        block = SymbolBlock(
            symbol="BTCUSDT",
            ticker=None,
            intraday=None,
            longer_term=None,
            oi_latest=None,
            oi_avg=None,
        )
        out = format_symbol_block(block)
        assert "### ALL BTC DATA" in out
        assert "### ALL BTCUSDT DATA" not in out

    def test_header_for_each_universe_coin(self):
        for bybit_sym, arena_sym in [
            ("ETHUSDT", "ETH"),
            ("SOLUSDT", "SOL"),
            ("BNBUSDT", "BNB"),
            ("DOGEUSDT", "DOGE"),
            ("XRPUSDT", "XRP"),
        ]:
            block = SymbolBlock(
                symbol=bybit_sym,
                ticker=None,
                intraday=None,
                longer_term=None,
                oi_latest=None,
                oi_avg=None,
            )
            out = format_symbol_block(block)
            assert f"### ALL {arena_sym} DATA" in out
            assert f"### ALL {bybit_sym} DATA" not in out


class TestOpenPositionsBlockFormat:
    def test_empty_returns_python_empty_list(self):
        out = format_open_positions_block(
            [],
            current_prices={},
            liquidation_prices={},
            notional_by_symbol={},
            unrealized_by_symbol={},
        )
        assert out == "[]"

    def test_uses_single_quotes_not_json(self):
        # gist L457-478: Python literal с одинарными кавычками, не JSON
        pos = _make_position()
        out = format_open_positions_block(
            [pos],
            current_prices={"BTCUSDT": 100500.0},
            liquidation_prices={"BTCUSDT": 90000.0},
            notional_by_symbol={"BTCUSDT": 50250.0},
            unrealized_by_symbol={"BTCUSDT": 250.0},
        )
        # Должны быть одинарные кавычки
        assert "'symbol':" in out
        assert "'quantity':" in out
        # И НЕ должно быть double-quoted JSON-style
        assert '"symbol":' not in out
        assert '"quantity":' not in out
        # null → None (Python literal)
        assert "null" not in out

    def test_symbol_field_uses_arena_naming(self):
        # `BTCUSDT` в БД → `BTC` в prompt (gist L460)
        pos = _make_position(symbol="ETHUSDT")
        out = format_open_positions_block(
            [pos],
            current_prices={"ETHUSDT": 3000.0},
            liquidation_prices={"ETHUSDT": 0},
            notional_by_symbol={"ETHUSDT": 1500.0},
            unrealized_by_symbol={"ETHUSDT": 0.0},
        )
        assert "'symbol': 'ETH'" in out
        assert "'symbol': 'ETHUSDT'" not in out

    def test_signed_quantity_long_positive(self):
        pos = _make_position(side="Buy", qty=0.5)
        out = format_open_positions_block(
            [pos],
            current_prices={"BTCUSDT": 100000.0},
            liquidation_prices={"BTCUSDT": 0},
            notional_by_symbol={"BTCUSDT": 0},
            unrealized_by_symbol={"BTCUSDT": 0},
        )
        assert "'quantity': 0.5" in out
        # И НЕТ отдельного `side` поля — направление в знаке quantity
        assert "'side':" not in out

    def test_signed_quantity_short_negative(self):
        pos = _make_position(side="Sell", qty=0.5)
        out = format_open_positions_block(
            [pos],
            current_prices={"BTCUSDT": 100000.0},
            liquidation_prices={"BTCUSDT": 0},
            notional_by_symbol={"BTCUSDT": 0},
            unrealized_by_symbol={"BTCUSDT": 0},
        )
        assert "'quantity': -0.5" in out
        assert "'side':" not in out

    def test_none_invalidation_renders_python_none(self):
        pos = _make_position(invalidation=None, confidence=None, risk_usd=None)
        out = format_open_positions_block(
            [pos],
            current_prices={"BTCUSDT": 100000.0},
            liquidation_prices={"BTCUSDT": 0},
            notional_by_symbol={"BTCUSDT": 0},
            unrealized_by_symbol={"BTCUSDT": 0},
        )
        assert "'invalidation_condition': None" in out
        assert "'confidence': None" in out
        assert "'risk_usd': None" in out
        assert "null" not in out

    def test_repr_indented_for_readability(self):
        pos = _make_position()
        out = format_open_positions_block(
            [pos],
            current_prices={"BTCUSDT": 100500.0},
            liquidation_prices={"BTCUSDT": 90000.0},
            notional_by_symbol={"BTCUSDT": 50250.0},
            unrealized_by_symbol={"BTCUSDT": 250.0},
        )
        # Должны быть переносы строк (как в gist'е), не one-liner
        assert "\n" in out
        # Первая строка — `[`, последняя — `]`
        assert out.startswith("[\n")
        assert out.endswith("\n]")


class TestPythonReprListInternal:
    def test_simple_dict(self):
        out = _python_repr_list([{"a": 1, "b": "x"}])
        assert "'a': 1" in out
        assert "'b': 'x'" in out

    def test_nested_dict(self):
        out = _python_repr_list([{"k": {"sub": 42}}])
        assert "'k':" in out
        assert "'sub': 42" in out

    def test_string_with_apostrophe_escaped(self):
        out = _python_repr_list([{"text": "it's fine"}])
        assert "\\'" in out  # экранированный апостроф

    def test_none_and_bool_python_literals(self):
        out = _python_repr_list([{"a": None, "b": True, "c": False}])
        assert "'a': None" in out
        assert "'b': True" in out
        assert "'c': False" in out
