"""Tests for COLD-START DISCOVERY RULE (v1.Y, 2026-05-28).

Закрывает: per-(symbol × side) PnL aggregation в DB + format helper +
content asserts на новый раздел SYSTEM_PROMPT + JSON example +
SELF-REFLECTION bullet + DECISION TYPES — OPEN tightening.

Research basis: Sutton & Barto (2018) §2.7 «Optimistic Initial
Values». См. BUILDLOG_AI_FX_TRADER.md 2026-05-28.
"""
from __future__ import annotations

import tempfile
from pathlib import Path


# ─── DB: get_pnl_by_symbol_side ────────────────────────────────────────


def _make_store(tmpdir: Path):
    from fx_ai_trader.state.db import AiFxTraderStore

    return AiFxTraderStore(str(tmpdir / "test.sqlite"))


def _insert_closed_position(
    store, symbol: str, side: str, pnl_usd: float, is_paper: bool = False
):
    """Helper: вставить closed live-trade в БД для теста агрегатов."""
    with store._conn() as c:
        c.execute(
            """
            INSERT INTO positions
              (symbol, side, volume_lots, entry_price, sl_price, tp_price,
               broker_position_id, broker_order_label, opened_at, closed_at,
               exit_price, realized_pnl_usd, close_reason, llm_reason, is_paper)
            VALUES
              (?, ?, 0.01, 100.0, 99.0, 101.0,
               NULL, 'ai-fx-trader-test', '2026-05-01T00:00:00+00:00',
               '2026-05-01T01:00:00+00:00',
               100.5, ?, 'test-close', 'test-open', ?)
            """,
            (symbol, side, pnl_usd, 1 if is_paper else 0),
        )


class TestGetPnlBySymbolSide:
    def test_returns_n0_for_untraded_pair(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            out = store.get_pnl_by_symbol_side(["XAUUSD", "BZ=F"])
            assert len(out) == 4  # 2 symbols × 2 sides
            assert all(r["n"] == 0 for r in out)
            sides_seen = {(r["symbol"], r["side"]) for r in out}
            assert sides_seen == {
                ("XAUUSD", "BUY"), ("XAUUSD", "SELL"),
                ("BZ=F", "BUY"), ("BZ=F", "SELL"),
            }

    def test_aggregates_side_split_correctly(self):
        """Sanity-check: 3 SELL wins + 0 BUY trades = разные строки."""
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_closed_position(store, "XAUUSD", "SELL", +7.0)
            _insert_closed_position(store, "XAUUSD", "SELL", +5.0)
            _insert_closed_position(store, "XAUUSD", "SELL", +9.0)

            out = store.get_pnl_by_symbol_side(["XAUUSD"])
            by_side = {r["side"]: r for r in out}
            assert by_side["BUY"]["n"] == 0  # cold-start flag
            assert by_side["SELL"]["n"] == 3
            assert by_side["SELL"]["wins"] == 3
            assert by_side["SELL"]["win_rate_pct"] == 100.0
            assert by_side["SELL"]["sum_pnl_usd"] == 21.0

    def test_paper_trades_excluded(self):
        """Paper-fills не должны попадать в live-агрегаты."""
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_closed_position(
                store, "XAUUSD", "BUY", +100.0, is_paper=True
            )
            out = store.get_pnl_by_symbol_side(["XAUUSD"])
            by_side = {r["side"]: r for r in out}
            assert by_side["BUY"]["n"] == 0  # paper-trade игнорируется

    def test_order_preserved_with_buy_first_per_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            out = store.get_pnl_by_symbol_side(["NG=F", "XAUUSD", "BZ=F"])
            assert [(r["symbol"], r["side"]) for r in out] == [
                ("NG=F", "BUY"), ("NG=F", "SELL"),
                ("XAUUSD", "BUY"), ("XAUUSD", "SELL"),
                ("BZ=F", "BUY"), ("BZ=F", "SELL"),
            ]

    def test_mixed_wins_losses_compute_wr(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_closed_position(store, "BZ=F", "BUY", +10.0)
            _insert_closed_position(store, "BZ=F", "BUY", -5.0)
            _insert_closed_position(store, "BZ=F", "BUY", +3.0)
            _insert_closed_position(store, "BZ=F", "BUY", -2.0)

            out = store.get_pnl_by_symbol_side(["BZ=F"])
            buy = next(r for r in out if r["side"] == "BUY")
            assert buy["n"] == 4
            assert buy["wins"] == 2
            assert buy["win_rate_pct"] == 50.0
            assert buy["sum_pnl_usd"] == 6.0


# ─── Format helper: format_performance_by_symbol_side ──────────────────


class TestFormatPerformanceBySymbolSide:
    def test_returns_empty_for_none(self):
        from fx_ai_trader.llm.prompts import format_performance_by_symbol_side
        assert format_performance_by_symbol_side(None) == ""

    def test_returns_empty_for_empty_list(self):
        from fx_ai_trader.llm.prompts import format_performance_by_symbol_side
        assert format_performance_by_symbol_side([]) == ""

    def test_cold_start_marker_for_n0(self):
        from fx_ai_trader.llm.prompts import format_performance_by_symbol_side
        stats = [
            {
                "symbol": "XAUUSD", "side": "BUY", "n": 0, "wins": 0,
                "win_rate_pct": 0.0, "avg_pnl_usd": 0.0, "sum_pnl_usd": 0.0,
            }
        ]
        out = format_performance_by_symbol_side(stats)
        assert "PERFORMANCE BY SYMBOL × SIDE" in out
        assert "XAUUSD BUY" in out
        assert "COLD-START" in out
        assert "DISCOVERY RULE" in out

    def test_renders_full_data_with_pnl(self):
        from fx_ai_trader.llm.prompts import format_performance_by_symbol_side
        stats = [
            {
                "symbol": "XAUUSD", "side": "BUY", "n": 0, "wins": 0,
                "win_rate_pct": 0.0, "avg_pnl_usd": 0.0, "sum_pnl_usd": 0.0,
            },
            {
                "symbol": "XAUUSD", "side": "SELL", "n": 3, "wins": 3,
                "win_rate_pct": 100.0, "avg_pnl_usd": 7.09,
                "sum_pnl_usd": 21.28,
            },
        ]
        out = format_performance_by_symbol_side(stats)
        assert "XAUUSD BUY: n=0" in out
        assert "COLD-START" in out
        assert "XAUUSD SELL: n=3" in out
        assert "100.0%" in out
        assert "+21.28$" in out
        # Cold-start строка НЕ должна содержать PnL-цифры (n=0 = no data)
        cold_start_line = [
            line for line in out.split("\n") if "XAUUSD BUY" in line
        ][0]
        assert "$" not in cold_start_line
        assert "wins=" not in cold_start_line


# ─── SYSTEM_PROMPT content asserts ─────────────────────────────────────


class TestSystemPromptColdStartSection:
    def test_cold_start_section_header_present(self):
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "COLD-START DISCOVERY RULE" in SYSTEM_PROMPT

    def test_cites_sutton_barto_research(self):
        """Compliance: no-data-fitting.mdc требует research-ссылку."""
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "Sutton & Barto" in SYSTEM_PROMPT
        assert "Optimistic Initial Values" in SYSTEM_PROMPT
        assert "cold-start" in SYSTEM_PROMPT.lower()

    def test_four_guards_listed(self):
        """4 защитных условия должны быть явно перечислены."""
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "MACRO supportive" in SYSTEM_PROMPT
        assert "SENTIMENT clean" in SYSTEM_PROMPT
        assert "aggregate_uncertainty ≤ 0.5" in SYSTEM_PROMPT
        assert "SIZE strictly minimum" in SYSTEM_PROMPT
        assert "volume_lots = 0.01" in SYSTEM_PROMPT
        assert "CADENCE" in SYSTEM_PROMPT
        assert "ONE discovery trade per (symbol × side) per" in SYSTEM_PROMPT

    def test_reason_prefix_requirement(self):
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "COLD-START discovery:" in SYSTEM_PROMPT

    def test_what_this_rule_is_not(self):
        """Защита от misinterpretation."""
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "NOT permission to bypass" in SYSTEM_PROMPT
        assert "NOT permission to revenge-trade" in SYSTEM_PROMPT
        assert "NOT applicable once" in SYSTEM_PROMPT

    def test_decision_open_tightening_referenced(self):
        """DECISION TYPES — OPEN должен упоминать tightened gate 0.5."""
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        # Не точная фраза, но обязательно tightening должен упоминаться
        assert "tightens to 0.5" in SYSTEM_PROMPT or (
            "gate tightens" in SYSTEM_PROMPT
        )

    def test_self_reflection_mentions_cold_start_handling(self):
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "COLD-START handling" in SYSTEM_PROMPT
        assert "cold-start trap" in SYSTEM_PROMPT


class TestSystemPromptColdStartJsonExample:
    def test_concrete_example_present(self):
        """В CONCRETE EXAMPLES должен быть COLD-START discovery JSON."""
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "Example OPEN — COLD-START discovery" in SYSTEM_PROMPT

    def test_example_uses_minimum_lot_size(self):
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        # Example должен showcase минимальный размер
        cold_start_block_start = SYSTEM_PROMPT.find(
            "Example OPEN — COLD-START"
        )
        cold_start_block_end = SYSTEM_PROMPT.find(
            "Example CLOSE", cold_start_block_start
        )
        block = SYSTEM_PROMPT[cold_start_block_start:cold_start_block_end]
        assert '"volume_lots": 0.01' in block
        assert "COLD-START discovery:" in block
        assert '"action": "open"' in block

    def test_example_uncertainty_below_05(self):
        """Example aggregate_uncertainty должен быть ≤0.5 (consistent с rule)."""
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        cold_start_block_start = SYSTEM_PROMPT.find(
            "Example OPEN — COLD-START"
        )
        cold_start_block_end = SYSTEM_PROMPT.find(
            "Example CLOSE", cold_start_block_start
        )
        block = SYSTEM_PROMPT[cold_start_block_start:cold_start_block_end]
        # Уровень 0.42 в нашем примере → меньше 0.5
        assert '"aggregate_uncertainty": 0.42' in block


# ─── build_user_prompt integration ─────────────────────────────────────


class TestBuildUserPromptColdStart:
    def test_includes_symbol_side_block_when_provided(self):
        from fx_ai_trader.llm.prompts import (
            build_user_prompt,
            format_performance_by_symbol_side,
        )
        stats = [
            {
                "symbol": "XAUUSD", "side": "BUY", "n": 0, "wins": 0,
                "win_rate_pct": 0.0, "avg_pnl_usd": 0.0, "sum_pnl_usd": 0.0,
            },
        ]
        out = build_user_prompt(
            "market context here",
            performance_by_symbol_side=format_performance_by_symbol_side(
                stats
            ),
        )
        assert "PERFORMANCE BY SYMBOL × SIDE" in out
        assert "COLD-START" in out
        assert "market context here" in out

    def test_omits_block_when_none(self):
        from fx_ai_trader.llm.prompts import build_user_prompt
        out = build_user_prompt("mkt", performance_by_symbol_side=None)
        assert "PERFORMANCE BY SYMBOL × SIDE" not in out

    def test_backward_compat_no_new_param(self):
        """Старый вызов без нового параметра должен работать."""
        from fx_ai_trader.llm.prompts import build_user_prompt
        out = build_user_prompt("mkt")
        assert "mkt" in out
        assert "PERFORMANCE BY SYMBOL × SIDE" not in out

    def test_block_order_per_symbol_then_per_side(self):
        """Если оба блока заданы — per-symbol первый, per-side следующий."""
        from fx_ai_trader.llm.prompts import build_user_prompt
        out = build_user_prompt(
            "mkt",
            performance_by_symbol="=== PERFORMANCE BY SYMBOL (live) ===\nx",
            performance_by_symbol_side="=== PERFORMANCE BY SYMBOL × SIDE (live) ===\ny",
        )
        assert (
            out.index("PERFORMANCE BY SYMBOL (live)")
            < out.index("PERFORMANCE BY SYMBOL × SIDE")
        )
