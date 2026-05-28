"""Tests for ai_trader v0.30 SELF-REFLECTION DB primitives.

Покрытие:
- ``AiTraderStore.get_pnl_by_symbol`` — n=0 fallback, sum/avg/wr,
  preserves order, since-cutoff (regime-change window).
- ``AiTraderStore.get_pnl_by_symbol_side`` — cold-start signal
  (Buy/Sell разбиение, n=0 на untested side), since-cutoff.
- ``AiTraderStore.get_recent_closed_trades`` — limit, ordering
  (oldest → newest), duration_minutes, reason_clamp,
  macro_thesis clamp, since-cutoff.

Порт из ``tests/test_fx_ai_trader_self_reflection.py``,
адаптация: в ai-trader нет ``is_paper`` (вся история = live demo),
side = ``Buy`` / ``Sell`` (не BUY/SELL как FX), нет ``volume_lots``
вместо этого ``qty`` + ``leverage``.

См.:
- ``.cursor/rules/sample-size.mdc`` — никаких hard-cap'ов, только
  информативный feedback для LLM.
- ``BUILDLOG_AI_TRADER.md`` v0.30.
- Sutton & Barto (2018) §2.7 Optimistic Initial Values.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_trader.state.db import AiTraderStore


@pytest.fixture
def store(tmp_path: Path) -> AiTraderStore:
    return AiTraderStore(tmp_path / "ai_trader.sqlite")


def _open_and_close(
    store: AiTraderStore,
    *,
    symbol: str,
    side: str,
    entry: float,
    exit_price: float,
    pnl: float,
    qty: float = 0.01,
    leverage: int = 5,
    llm_reason: str = "test setup",
    close_reason: str = "test close",
    macro_thesis: str | None = None,
    order_link_id_suffix: str = "",
) -> int:
    pid = store.open_position(
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry,
        sl_price=entry * 0.99 if side == "Buy" else entry * 1.01,
        tp_price=entry * 1.02 if side == "Buy" else entry * 0.98,
        leverage=leverage,
        order_link_id=f"ai_test_{symbol}_{side}_{order_link_id_suffix}_{entry}_{exit_price}",
        llm_reason=llm_reason,
        macro_thesis=macro_thesis,
    )
    store.close_position(
        pid,
        exit_price=exit_price,
        realized_pnl_usd=pnl,
        close_reason=close_reason,
    )
    return pid


# ─── get_pnl_by_symbol ────────────────────────────────────────────────────


class TestGetPnlBySymbol:
    def test_empty_store_returns_n_zero_for_each_symbol(
        self, store: AiTraderStore
    ):
        rows = store.get_pnl_by_symbol(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        for r in rows:
            assert r["n"] == 0
            assert r["wins"] == 0
            assert r["win_rate_pct"] == 0.0
            assert r["avg_pnl_usd"] == 0.0
            assert r["sum_pnl_usd"] == 0.0

    def test_aggregates_only_closed_positions(self, store: AiTraderStore):
        # open но не closed → не должно учитываться
        store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.01,
            entry_price=60000.0, sl_price=59400.0, tp_price=61200.0,
            leverage=5, order_link_id="ai_test_open_only",
            llm_reason="open only", macro_thesis="open thesis",
        )
        # 3 закрытых
        _open_and_close(store, symbol="BTCUSDT", side="Buy",
                        entry=60000.0, exit_price=61000.0, pnl=10.0,
                        order_link_id_suffix="1")
        _open_and_close(store, symbol="BTCUSDT", side="Buy",
                        entry=60000.0, exit_price=59500.0, pnl=-5.0,
                        order_link_id_suffix="2")
        _open_and_close(store, symbol="BTCUSDT", side="Buy",
                        entry=60000.0, exit_price=59000.0, pnl=-10.0,
                        order_link_id_suffix="3")
        rows = store.get_pnl_by_symbol(["BTCUSDT"])
        assert len(rows) == 1
        r = rows[0]
        assert r["n"] == 3  # ОТКРЫТУЮ не считаем
        assert r["wins"] == 1
        assert r["win_rate_pct"] == pytest.approx(100 / 3, abs=0.01)
        assert r["avg_pnl_usd"] == pytest.approx(-5.0 / 3, abs=0.01)
        assert r["sum_pnl_usd"] == pytest.approx(-5.0, abs=0.01)

    def test_preserves_symbol_order(self, store: AiTraderStore):
        _open_and_close(store, symbol="ETHUSDT", side="Buy",
                        entry=3500.0, exit_price=3550.0, pnl=10.0)
        rows = store.get_pnl_by_symbol(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert rows[0]["n"] == 0
        assert rows[1]["n"] == 1
        assert rows[2]["n"] == 0

    def test_win_rate_zero_pnl_is_not_win(self, store: AiTraderStore):
        # PnL == 0 → НЕ-win (break-even не победа)
        _open_and_close(store, symbol="BTCUSDT", side="Buy",
                        entry=60000.0, exit_price=60000.0, pnl=0.0)
        rows = store.get_pnl_by_symbol(["BTCUSDT"])
        assert rows[0]["n"] == 1
        assert rows[0]["wins"] == 0

    def test_since_cutoff_excludes_pre_regime_trades(
        self, store: AiTraderStore
    ):
        """v0.30 regime-change cutoff: pre-stats_window_start trades должны
        исключаться чтобы LLM не учился на outcome другой стратегии.
        """
        # Сначала вручную записываем позицию с opened_at до cutoff
        with store._conn() as c:
            c.execute(
                """
                INSERT INTO positions
                (symbol, side, qty, entry_price, sl_price, tp_price, leverage,
                 order_link_id, opened_at, closed_at, exit_price,
                 realized_pnl_usd, close_reason, llm_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "BTCUSDT", "Buy", 0.01, 60000.0, 59400.0, 61200.0, 5,
                    "ai_old_pre_cutoff",
                    "2026-05-01T00:00:00+00:00",
                    "2026-05-01T12:00:00+00:00",
                    61000.0, 10.0, "old close", "old reason",
                ),
            )
        # Post-cutoff loss
        _open_and_close(store, symbol="BTCUSDT", side="Buy",
                        entry=60000.0, exit_price=59500.0, pnl=-5.0)
        # Без cutoff: видим 2 трейда
        rows_all = store.get_pnl_by_symbol(["BTCUSDT"])
        assert rows_all[0]["n"] == 2
        # С cutoff 2026-05-15: только новый trade с loss
        rows_filtered = store.get_pnl_by_symbol(
            ["BTCUSDT"], since="2026-05-15T00:00:00+00:00",
        )
        assert rows_filtered[0]["n"] == 1
        assert rows_filtered[0]["wins"] == 0
        assert rows_filtered[0]["sum_pnl_usd"] == pytest.approx(-5.0)


# ─── get_pnl_by_symbol_side (cold-start) ─────────────────────────────────


class TestGetPnlBySymbolSide:
    def test_empty_store_returns_buy_and_sell_n_zero(
        self, store: AiTraderStore
    ):
        rows = store.get_pnl_by_symbol_side(["BTCUSDT", "ETHUSDT"])
        # 2 symbols × 2 sides = 4 строки
        assert len(rows) == 4
        # Buy идёт первой внутри symbol
        assert [(r["symbol"], r["side"]) for r in rows] == [
            ("BTCUSDT", "Buy"),
            ("BTCUSDT", "Sell"),
            ("ETHUSDT", "Buy"),
            ("ETHUSDT", "Sell"),
        ]
        for r in rows:
            assert r["n"] == 0
            assert r["wins"] == 0

    def test_cold_start_signal_buy_only_history(
        self, store: AiTraderStore
    ):
        """Critical case: 3 BTCUSDT Buy wins, 0 BTCUSDT Sell trades.
        Aggregated by symbol скрывает что Sell — cold-start. По split'у
        мы видим обе записи отдельно.
        """
        for i in range(3):
            _open_and_close(
                store, symbol="BTCUSDT", side="Buy",
                entry=60000.0 + i, exit_price=61000.0 + i, pnl=10.0,
                order_link_id_suffix=str(i),
            )
        rows = store.get_pnl_by_symbol_side(["BTCUSDT"])
        # Buy: 3 wins / 3 trades
        assert rows[0]["side"] == "Buy"
        assert rows[0]["n"] == 3
        assert rows[0]["wins"] == 3
        assert rows[0]["win_rate_pct"] == 100.0
        # Sell: cold-start (n=0)
        assert rows[1]["side"] == "Sell"
        assert rows[1]["n"] == 0
        assert rows[1]["wins"] == 0

    def test_since_cutoff_applies(self, store: AiTraderStore):
        with store._conn() as c:
            c.execute(
                """
                INSERT INTO positions
                (symbol, side, qty, entry_price, sl_price, tp_price, leverage,
                 order_link_id, opened_at, closed_at, exit_price,
                 realized_pnl_usd, close_reason, llm_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "BTCUSDT", "Sell", 0.01, 60000.0, 60600.0, 58800.0, 5,
                    "ai_old_sell",
                    "2026-05-01T00:00:00+00:00",
                    "2026-05-01T12:00:00+00:00",
                    59000.0, 10.0, "old", "old",
                ),
            )
        # С cutoff: Sell снова cold-start (старый трейд скрыт)
        rows = store.get_pnl_by_symbol_side(
            ["BTCUSDT"], since="2026-05-15T00:00:00+00:00",
        )
        sell_row = [r for r in rows if r["side"] == "Sell"][0]
        assert sell_row["n"] == 0


# ─── get_recent_closed_trades ────────────────────────────────────────────


class TestGetRecentClosedTrades:
    def test_empty_store_returns_empty_list(self, store: AiTraderStore):
        assert store.get_recent_closed_trades(limit=10) == []

    def test_returns_oldest_to_newest(self, store: AiTraderStore):
        ids = []
        for i in range(3):
            pid = _open_and_close(
                store, symbol="BTCUSDT", side="Buy",
                entry=60000.0 + i, exit_price=61000.0 + i, pnl=10.0,
                order_link_id_suffix=str(i),
                llm_reason=f"trade_{i}",
            )
            ids.append(pid)
        trades = store.get_recent_closed_trades(limit=10)
        assert [t["id"] for t in trades] == ids

    def test_limit_keeps_most_recent(self, store: AiTraderStore):
        for i in range(5):
            _open_and_close(
                store, symbol="BTCUSDT", side="Buy",
                entry=60000.0, exit_price=60000.0 + i * 100, pnl=float(i),
                order_link_id_suffix=str(i),
            )
        trades = store.get_recent_closed_trades(limit=3)
        assert len(trades) == 3
        # Sorted oldest → newest, последние 3 (id 3,4,5)
        assert [t["id"] for t in trades] == [3, 4, 5]

    def test_clamps_long_reasons_and_macro_thesis(self, store: AiTraderStore):
        long_text = "A" * 500
        _open_and_close(
            store, symbol="BTCUSDT", side="Sell",
            entry=60000.0, exit_price=59000.0, pnl=10.0,
            llm_reason=long_text, close_reason=long_text,
            macro_thesis=long_text,
        )
        trades = store.get_recent_closed_trades(limit=10, reason_clamp=180)
        assert len(trades) == 1
        assert len(trades[0]["llm_reason"]) == 180
        assert len(trades[0]["close_reason"]) == 180
        assert len(trades[0]["macro_thesis"]) == 180

    def test_duration_minutes_non_negative(self, store: AiTraderStore):
        _open_and_close(
            store, symbol="BTCUSDT", side="Buy",
            entry=60000.0, exit_price=60500.0, pnl=5.0,
        )
        trades = store.get_recent_closed_trades(limit=10)
        assert trades[0]["duration_minutes"] is not None
        assert trades[0]["duration_minutes"] >= 0

    def test_macro_thesis_preserved_in_trade(self, store: AiTraderStore):
        _open_and_close(
            store, symbol="BTCUSDT", side="Buy",
            entry=60000.0, exit_price=61000.0, pnl=10.0,
            macro_thesis="ETF inflow $1.2B 5d + DXY -0.8%",
        )
        trades = store.get_recent_closed_trades(limit=10)
        assert trades[0]["macro_thesis"] == "ETF inflow $1.2B 5d + DXY -0.8%"

    def test_since_cutoff_applies(self, store: AiTraderStore):
        with store._conn() as c:
            c.execute(
                """
                INSERT INTO positions
                (symbol, side, qty, entry_price, sl_price, tp_price, leverage,
                 order_link_id, opened_at, closed_at, exit_price,
                 realized_pnl_usd, close_reason, llm_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "BTCUSDT", "Buy", 0.01, 60000.0, 59400.0, 61200.0, 5,
                    "ai_old_pre_cutoff_recent",
                    "2026-05-01T00:00:00+00:00",
                    "2026-05-01T12:00:00+00:00",
                    61000.0, 10.0, "old close", "old reason",
                ),
            )
        _open_and_close(
            store, symbol="BTCUSDT", side="Buy",
            entry=60000.0, exit_price=61000.0, pnl=10.0,
            llm_reason="new trade",
        )
        trades = store.get_recent_closed_trades(
            limit=10, since="2026-05-15T00:00:00+00:00",
        )
        assert len(trades) == 1
        assert trades[0]["llm_reason"] == "new trade"
