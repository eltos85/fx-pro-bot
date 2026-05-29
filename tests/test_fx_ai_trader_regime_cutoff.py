"""Tests для regime-change cutoff (v1.Z, 2026-05-28).

Покрывает:
- DB: ``since=`` фильтр на get_pnl_by_symbol / get_pnl_by_symbol_side /
  get_recent_closed_trades; backward-compat (since=None == legacy).
- Format helpers: ``window_label=`` header rendering; default без label.
- SYSTEM_PROMPT: новый раздел REGIME-CHANGE WINDOW + research citation
  (Lopez de Prado, Hamilton).
- Settings: stats_window_start default == Phase 1 deploy ts.

Compliance: no-data-fitting.mdc (cutoff обоснован Lopez de Prado 2018 ch.7),
strategy-guard.mdc (БД нетронута, только query-time фильтр), sample-size.mdc
(инструменты не отключаются).
"""
from __future__ import annotations

import tempfile
from pathlib import Path


def _make_store(tmpdir: Path):
    from fx_ai_trader.state.db import AiFxTraderStore
    return AiFxTraderStore(str(tmpdir / "test.sqlite"))


def _insert_position(
    store, symbol: str, side: str, pnl_usd: float, opened_at: str,
    closed_at: str | None = None, is_paper: bool = False
):
    """Helper: вставить позицию с заданным opened_at."""
    closed_at = closed_at or opened_at  # для closed trades — простой closed_at
    with store._conn() as c:
        c.execute(
            """
            INSERT INTO positions
              (symbol, side, volume_lots, entry_price, sl_price, tp_price,
               broker_position_id, broker_order_label, opened_at, closed_at,
               exit_price, realized_pnl_usd, close_reason, llm_reason, is_paper)
            VALUES
              (?, ?, 0.01, 100.0, 99.0, 101.0,
               NULL, 'ai-fx-trader-test', ?, ?,
               100.5, ?, 'test-close', 'test-open', ?)
            """,
            (symbol, side, opened_at, closed_at, pnl_usd, 1 if is_paper else 0),
        )


# ─── DB: get_pnl_by_symbol_side с since= ───────────────────────────────


class TestGetPnlBySymbolSideSince:
    def test_since_filters_pre_cutoff(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            # 2 pre-cutoff, 1 post-cutoff
            _insert_position(store, "NG=F", "BUY", -5.0,
                             "2026-05-20T10:00:00+00:00")
            _insert_position(store, "NG=F", "BUY", -10.0,
                             "2026-05-25T15:00:00+00:00")
            _insert_position(store, "NG=F", "BUY", +3.0,
                             "2026-05-27T12:00:00+00:00")

            cutoff = "2026-05-26T07:42:00+00:00"
            out = store.get_pnl_by_symbol_side(["NG=F"], since=cutoff)
            buy = next(r for r in out if r["side"] == "BUY")
            assert buy["n"] == 1  # только post-cutoff
            assert buy["sum_pnl_usd"] == 3.0  # только последний trade

    def test_since_none_returns_all(self):
        """Backward-compat: since=None == legacy behavior."""
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_position(store, "BZ=F", "BUY", +10.0,
                             "2026-05-13T08:00:00+00:00")
            _insert_position(store, "BZ=F", "BUY", -5.0,
                             "2026-05-26T08:00:00+00:00")

            out = store.get_pnl_by_symbol_side(["BZ=F"], since=None)
            buy = next(r for r in out if r["side"] == "BUY")
            assert buy["n"] == 2
            assert buy["sum_pnl_usd"] == 5.0

    def test_since_empty_string_returns_all(self):
        """Empty string == disabled cutoff (env override через ''")."""
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_position(store, "BZ=F", "BUY", +10.0,
                             "2026-05-13T08:00:00+00:00")

            out = store.get_pnl_by_symbol_side(["BZ=F"], since="")
            buy = next(r for r in out if r["side"] == "BUY")
            assert buy["n"] == 1

    def test_since_cold_start_for_pre_only_pair(self):
        """Pre-cutoff-only pair → post-cutoff n=0 (cold-start re-trigger)."""
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            for _ in range(12):
                _insert_position(store, "NG=F", "BUY", -5.0,
                                 "2026-05-20T10:00:00+00:00")

            cutoff = "2026-05-26T07:42:00+00:00"
            out = store.get_pnl_by_symbol_side(["NG=F"], since=cutoff)
            buy = next(r for r in out if r["side"] == "BUY")
            assert buy["n"] == 0  # все pre-cutoff отфильтрованы

    def test_since_inclusive_boundary(self):
        """opened_at == cutoff — попадает в выборку (>=)."""
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_position(store, "BZ=F", "SELL", +10.0,
                             "2026-05-26T07:42:00+00:00")  # ровно cutoff

            cutoff = "2026-05-26T07:42:00+00:00"
            out = store.get_pnl_by_symbol_side(["BZ=F"], since=cutoff)
            sell = next(r for r in out if r["side"] == "SELL")
            assert sell["n"] == 1


# ─── DB: get_pnl_by_symbol с since= ────────────────────────────────────


class TestGetPnlBySymbolSince:
    def test_since_filters_pre_cutoff(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_position(store, "XAUUSD", "SELL", +5.0,
                             "2026-05-20T10:00:00+00:00")
            _insert_position(store, "XAUUSD", "BUY", +7.0,
                             "2026-05-28T08:00:00+00:00")

            cutoff = "2026-05-26T07:42:00+00:00"
            out = store.get_pnl_by_symbol(["XAUUSD"], since=cutoff)
            assert out[0]["n"] == 1
            assert out[0]["sum_pnl_usd"] == 7.0

    def test_since_none_default_compat(self):
        """Дефолт since=None — legacy v1.X behavior, существующие тесты."""
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_position(store, "XAUUSD", "SELL", +5.0,
                             "2026-05-20T10:00:00+00:00")
            out = store.get_pnl_by_symbol(["XAUUSD"])  # без since
            assert out[0]["n"] == 1


# ─── DB: get_recent_closed_trades с since= ─────────────────────────────


class TestGetRecentClosedTradesSince:
    def test_since_filters_recent_trades(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_position(store, "BZ=F", "BUY", +10.0,
                             "2026-05-13T08:00:00+00:00",
                             "2026-05-13T10:00:00+00:00")
            _insert_position(store, "BZ=F", "BUY", -5.0,
                             "2026-05-28T08:00:00+00:00",
                             "2026-05-28T10:00:00+00:00")

            cutoff = "2026-05-26T07:42:00+00:00"
            trades = store.get_recent_closed_trades(limit=10, since=cutoff)
            assert len(trades) == 1
            assert trades[0]["realized_pnl_usd"] == -5.0

    def test_since_none_default(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            _insert_position(store, "BZ=F", "BUY", +10.0,
                             "2026-05-13T08:00:00+00:00",
                             "2026-05-13T10:00:00+00:00")
            trades = store.get_recent_closed_trades(limit=10)
            assert len(trades) == 1


# ─── Format helpers с window_label ─────────────────────────────────────


class TestFormatHelpersWindowLabel:
    def test_per_symbol_with_window_label(self):
        from fx_ai_trader.llm.prompts import format_performance_by_symbol
        stats = [{
            "symbol": "XAUUSD", "n": 1, "wins": 1,
            "win_rate_pct": 100.0, "avg_pnl_usd": 6.95, "sum_pnl_usd": 6.95,
        }]
        out = format_performance_by_symbol(
            stats, window_label="since 2026-05-26 regime-change cutoff"
        )
        assert "since 2026-05-26 regime-change cutoff" in out
        assert "PERFORMANCE BY SYMBOL" in out

    def test_per_symbol_default_no_label(self):
        """Backward-compat: без window_label показывает legacy header."""
        from fx_ai_trader.llm.prompts import format_performance_by_symbol
        stats = [{
            "symbol": "XAUUSD", "n": 1, "wins": 1,
            "win_rate_pct": 100.0, "avg_pnl_usd": 6.95, "sum_pnl_usd": 6.95,
        }]
        out = format_performance_by_symbol(stats)
        assert "since experiment start" in out
        assert "regime-change cutoff" not in out

    def test_per_side_with_window_label(self):
        from fx_ai_trader.llm.prompts import (
            format_performance_by_symbol_side,
        )
        stats = [{
            "symbol": "NG=F", "side": "BUY", "n": 0, "wins": 0,
            "win_rate_pct": 0.0, "avg_pnl_usd": 0.0, "sum_pnl_usd": 0.0,
        }]
        out = format_performance_by_symbol_side(
            stats, window_label="since 2026-05-26 regime-change cutoff"
        )
        assert "since 2026-05-26 regime-change cutoff" in out
        assert "PERFORMANCE BY SYMBOL × SIDE" in out
        assert "COLD-START" in out  # marker сохраняется

    def test_per_side_default_no_label(self):
        from fx_ai_trader.llm.prompts import (
            format_performance_by_symbol_side,
        )
        stats = [{
            "symbol": "NG=F", "side": "BUY", "n": 0, "wins": 0,
            "win_rate_pct": 0.0, "avg_pnl_usd": 0.0, "sum_pnl_usd": 0.0,
        }]
        out = format_performance_by_symbol_side(stats)
        assert "since experiment start" in out

    def test_recent_trades_with_window_label(self):
        from fx_ai_trader.llm.prompts import format_recent_trades
        trades = [{
            "id": 1, "symbol": "XAUUSD", "side": "BUY", "volume_lots": 0.01,
            "entry_price": 4389.95, "exit_price": 4394.76,
            "realized_pnl_usd": 6.95, "opened_at": "2026-05-28T08:32+00:00",
            "closed_at": "2026-05-28T09:21+00:00", "duration_minutes": 49,
            "llm_reason": "COLD-START discovery", "close_reason": "partial",
        }]
        out = format_recent_trades(
            trades, window_label="since 2026-05-26 regime-change cutoff"
        )
        assert "since 2026-05-26 regime-change cutoff" in out
        assert "RECENT CLOSED TRADES" in out

    def test_recent_trades_default_no_label(self):
        from fx_ai_trader.llm.prompts import format_recent_trades
        trades = [{
            "id": 1, "symbol": "XAUUSD", "side": "BUY", "volume_lots": 0.01,
            "entry_price": 4389.95, "exit_price": 4394.76,
            "realized_pnl_usd": 6.95, "opened_at": "2026-05-28T08:32+00:00",
            "closed_at": "2026-05-28T09:21+00:00", "duration_minutes": 49,
            "llm_reason": "COLD-START", "close_reason": "partial",
        }]
        out = format_recent_trades(trades)
        assert "regime-change cutoff" not in out
        assert "RECENT CLOSED TRADES" in out


# ─── SYSTEM_PROMPT content asserts ─────────────────────────────────────


class TestSystemPromptRegimeChangeSection:
    def test_section_header_present(self):
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "REGIME-CHANGE WINDOW" in SYSTEM_PROMPT

    def test_research_citation_lopez_de_prado(self):
        """Compliance: no-data-fitting.mdc requires research-cite."""
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "Lopez de Prado" in SYSTEM_PROMPT
        assert "structural breaks" in SYSTEM_PROMPT
        # Hamilton 1989 regime-switching также упомянут
        assert "Hamilton" in SYSTEM_PROMPT

    def test_explains_why_pre_cutoff_excluded(self):
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "MATERIALLY DIFFERENT" in SYSTEM_PROMPT
        assert "audit database" in SYSTEM_PROMPT  # данные сохранены

    def test_cold_start_interaction_explained(self):
        """Cold-start re-trigger через cutoff явно обсуждён."""
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "cold-start" in SYSTEM_PROMPT.lower()
        # Раздел должен явно говорить что cold-start applicable
        assert "DISCOVERY RULE is legitimately applicable" in SYSTEM_PROMPT

    def test_not_a_loophole_warning(self):
        from fx_ai_trader.llm.prompts import SYSTEM_PROMPT
        assert "NOT a loophole" in SYSTEM_PROMPT
        assert "ALL FOUR guards" in SYSTEM_PROMPT


# ─── Settings: stats_window_start ──────────────────────────────────────


class TestSettingsStatsWindowStart:
    def test_default_is_phase03_deploy_ts(self):
        """Cutoff advanced to Phase 0-3 deploy (2026-05-29), event-driven
        architecture structural break. См. BUILDLOG_AI_FX_TRADER.md 2026-05-29.
        """
        from fx_ai_trader.config.settings import AiFxTraderSettings
        s = AiFxTraderSettings()
        assert s.stats_window_start == "2026-05-29T08:26:00+00:00"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "AI_FX_TRADER_STATS_WINDOW_START",
            "2026-06-01T00:00:00+00:00",
        )
        from fx_ai_trader.config.settings import AiFxTraderSettings
        s = AiFxTraderSettings()
        assert s.stats_window_start == "2026-06-01T00:00:00+00:00"

    def test_env_override_empty_disables(self, monkeypatch):
        """Пустая строка — disable cutoff (legacy v1.X behavior)."""
        monkeypatch.setenv("AI_FX_TRADER_STATS_WINDOW_START", "")
        from fx_ai_trader.config.settings import AiFxTraderSettings
        s = AiFxTraderSettings()
        assert s.stats_window_start == ""
