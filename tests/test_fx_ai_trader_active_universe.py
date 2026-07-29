"""Regression tests for runtime-universe prompt isolation.

GOLD-only runtime must not receive enabled NG/BZ experiment blocks or
self-reflection rows for instruments it cannot trade. News provenance must
include publication time and age so the LLM can distinguish fresh from stale.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.llm.prompts import build_system_prompt, build_user_prompt
from fx_ai_trader.news.rss import NewsItem
from fx_ai_trader.state.db import AiFxPosition, AiFxTraderStore
from fx_ai_trader.trading.context import (
    MarketContext,
    SymbolSnapshot,
    format_context_for_prompt,
    format_context_for_review,
)


def _closed_trade(store: AiFxTraderStore, symbol: str) -> int:
    position_id = store.open_position(
        symbol=symbol,
        side="BUY",
        volume_lots=0.01,
        entry_price=100.0,
        sl_price=99.0,
        tp_price=102.0,
        broker_position_id=position_id_seed(symbol),
        broker_order_label="ai-fx-trader",
        llm_reason=f"{symbol} setup",
        is_paper=False,
    )
    store.close_position(
        position_id,
        exit_price=101.0,
        realized_pnl_usd=1.0,
        close_reason=f"{symbol} close",
    )
    return position_id


def position_id_seed(symbol: str) -> int:
    return {"XAUUSD": 101, "BZ=F": 102, "NG=F": 103}[symbol]


def test_gold_only_system_prompt_has_authoritative_scope() -> None:
    settings = AiFxTraderSettings(
        _env_file=None,
        symbols=("XAUUSD",),
    )
    prompt = build_system_prompt(settings)
    assert prompt.startswith("=== RUNTIME ACTIVE UNIVERSE (AUTHORITATIVE) ===")
    assert "Active symbols: XAUUSD." in prompt
    assert "Dormant instruments: BZ=F, NG=F." in prompt
    assert "Do not analyse, mention, compare, or trade them" in prompt


def test_inactive_mode_blocks_are_not_injected_for_gold_only() -> None:
    prompt = build_user_prompt(
        "MARKET_CTX",
        active_symbols=("XAUUSD",),
        ng_mode_v2_enabled=True,
        bz_breakout_mode_enabled=True,
    )
    assert "XAUUSD only" in prompt
    assert "NG MODE V2" not in prompt
    assert "BZ MOMENTUM MODE" not in prompt


def test_active_energy_mode_blocks_remain_available() -> None:
    prompt = build_user_prompt(
        "MARKET_CTX",
        active_symbols=("XAUUSD", "BZ=F", "NG=F"),
        ng_mode_v2_enabled=True,
        bz_breakout_mode_enabled=True,
    )
    assert "NG MODE V2" in prompt
    assert "BZ MOMENTUM MODE" in prompt


def test_recent_trades_and_lessons_filter_to_active_symbols(tmp_path: Path) -> None:
    store = AiFxTraderStore(tmp_path / "active-universe.sqlite")
    xau_id = _closed_trade(store, "XAUUSD")
    bz_id = _closed_trade(store, "BZ=F")
    store.add_lesson(
        lesson_text="Gold lesson",
        symbol="XAUUSD",
        trade_id=xau_id,
    )
    store.add_lesson(
        lesson_text="Brent lesson",
        symbol="BZ=F",
        trade_id=bz_id,
    )
    store.add_lesson(lesson_text="General risk lesson", symbol=None)

    trades = store.get_recent_closed_trades(
        limit=10, symbols=("XAUUSD",)
    )
    assert [trade["symbol"] for trade in trades] == ["XAUUSD"]

    lessons = store.get_active_lessons(
        limit=10, symbols=("XAUUSD",)
    )
    assert [lesson["lesson_text"] for lesson in lessons] == [
        "Gold lesson",
        "General risk lesson",
    ]


def test_news_prompt_includes_publication_time_and_age(monkeypatch) -> None:
    import fx_ai_trader.trading.context as context_module

    class FixedDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 7, 21, 16, 0, tzinfo=tz or UTC)

        @staticmethod
        def fromisoformat(value: str):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(context_module, "datetime", FixedDatetime)
    item = NewsItem(
        title="Gold reacts to CPI",
        summary="Treasury yields fell after the release.",
        source="BLS",
        published_iso="2026-07-21T14:30:00+00:00",
        url="https://example.test/cpi",
        symbols=["XAUUSD"],
    )
    context = MarketContext(
        snapshots=[],
        open_positions=[],
        virtual_capital_usd=2000.0,
        news_per_symbol={"XAUUSD": [item]},
    )

    prompt = format_context_for_prompt(context)
    assert "published=2026-07-21T14:30+00:00" in prompt
    assert "age=1.5h" in prompt


def test_review_prompt_includes_server_computed_unrealised_r() -> None:
    position = AiFxPosition(
        id=1,
        symbol="XAUUSD",
        side="BUY",
        volume_lots=0.01,
        entry_price=4000.0,
        sl_price=3990.0,
        tp_price=4020.0,
        broker_position_id=123,
        broker_order_label="ai-fx-trader",
        opened_at="2026-07-21T12:00:00+00:00",
        closed_at=None,
        exit_price=None,
        realized_pnl_usd=None,
        close_reason=None,
        llm_reason="test",
        is_paper=0,
    )
    snapshot = SymbolSnapshot(
        symbol="XAUUSD",
        current_price=4015.0,
        bars_1h=[],
        bars_4h=[],
    )
    context = MarketContext(
        snapshots=[snapshot],
        open_positions=[position],
        virtual_capital_usd=2000.0,
    )

    prompt = format_context_for_review(context)
    assert "unrealised_R=+1.50R" in prompt

