"""Tests for fx_ai_trader macro_rates module (Phase 2 D1).

Закрывает: DXY / UST10Y / TIPS-proxy через yfinance + интеграция в
MarketContext + рендер в format_context_for_prompt.

Без сетевых вызовов: yfinance.Ticker полностью замокан через
unittest.mock.patch на уровне импорта (`yfinance.Ticker`).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ─── format_macro_rates_snapshot ───────────────────────────────────────


class TestFormatMacroRatesSnapshot:
    def test_returns_none_for_none(self):
        from fx_ai_trader.data.macro_rates import format_macro_rates_snapshot

        assert format_macro_rates_snapshot(None) is None

    def test_returns_none_when_all_fields_empty(self):
        from fx_ai_trader.data.macro_rates import (
            MacroRatesSnapshot,
            format_macro_rates_snapshot,
        )

        snap = MacroRatesSnapshot(
            dxy_last=None, dxy_change_24h_pct=None, dxy_change_5d_pct=None,
            ust10y_last_pct=None, ust10y_change_24h_bps=None,
            ust10y_change_5d_bps=None,
            tip_last=None, tip_change_24h_pct=None, tip_change_5d_pct=None,
            fetched_at_utc="2026-05-27T06:30:00+00:00",
        )
        assert format_macro_rates_snapshot(snap) is None

    def test_renders_full_block_with_all_three_series(self):
        from fx_ai_trader.data.macro_rates import (
            MacroRatesSnapshot,
            format_macro_rates_snapshot,
        )

        snap = MacroRatesSnapshot(
            dxy_last=105.42,
            dxy_change_24h_pct=-0.18,
            dxy_change_5d_pct=-0.71,
            ust10y_last_pct=4.31,
            ust10y_change_24h_bps=-3.0,
            ust10y_change_5d_bps=-12.0,
            tip_last=110.85,
            tip_change_24h_pct=+0.20,
            tip_change_5d_pct=+1.05,
            fetched_at_utc="2026-05-27T06:30:00+00:00",
        )
        out = format_macro_rates_snapshot(snap)
        assert out is not None
        # Header — canonical gold hierarchy reference
        assert "US MACRO RATES" in out
        assert "real yields" in out and "DXY" in out
        # DXY-строка
        assert "DXY" in out and "105.42" in out
        assert "24h=-0.18%" in out and "5d=-0.71%" in out
        # UST10Y-строка
        assert "UST10Y nominal" in out and "4.31%" in out
        assert "24h=-3.0bps" in out and "5d=-12.0bps" in out
        # TIP-строка
        assert "TIP" in out and "110.85" in out
        assert "real-yields proxy" in out
        # Fetched timestamp
        assert "2026-05-27" in out

    def test_renders_partial_block_only_dxy(self):
        from fx_ai_trader.data.macro_rates import (
            MacroRatesSnapshot,
            format_macro_rates_snapshot,
        )

        snap = MacroRatesSnapshot(
            dxy_last=104.10,
            dxy_change_24h_pct=+0.30,
            dxy_change_5d_pct=None,
            ust10y_last_pct=None, ust10y_change_24h_bps=None,
            ust10y_change_5d_bps=None,
            tip_last=None, tip_change_24h_pct=None, tip_change_5d_pct=None,
            fetched_at_utc="2026-05-27T06:30:00+00:00",
        )
        out = format_macro_rates_snapshot(snap)
        assert out is not None
        assert "DXY" in out and "104.10" in out
        assert "5d=n/a" in out
        assert "UST10Y" not in out
        assert "TIP" not in out

    def test_handles_missing_delta_gracefully(self):
        from fx_ai_trader.data.macro_rates import (
            MacroRatesSnapshot,
            format_macro_rates_snapshot,
        )

        snap = MacroRatesSnapshot(
            dxy_last=100.0, dxy_change_24h_pct=None, dxy_change_5d_pct=None,
            ust10y_last_pct=4.0, ust10y_change_24h_bps=None,
            ust10y_change_5d_bps=None,
            tip_last=None, tip_change_24h_pct=None, tip_change_5d_pct=None,
            fetched_at_utc="2026-05-27T06:30:00+00:00",
        )
        out = format_macro_rates_snapshot(snap)
        assert out is not None
        assert "24h=n/a" in out and "5d=n/a" in out


# ─── MacroRatesProvider — happy path с мок-yfinance ────────────────────


def _df_from_closes(closes: list[float]) -> pd.DataFrame:
    """Сконструировать DataFrame в формате yfinance.Ticker.history."""
    return pd.DataFrame({"Close": closes, "Open": closes, "High": closes,
                         "Low": closes, "Volume": [0] * len(closes)})


class TestMacroRatesProviderHappyPath:
    def test_get_snapshot_constructs_all_three_series(self):
        from fx_ai_trader.data.macro_rates import MacroRatesProvider

        dxy_closes = [104.0, 104.2, 104.5, 104.8, 105.0, 105.1, 105.3, 105.4]
        ust10y_closes = [4.20, 4.22, 4.25, 4.28, 4.31, 4.32, 4.30, 4.31]
        tip_closes = [109.5, 109.7, 110.0, 110.3, 110.5, 110.6, 110.8, 110.85]

        mock_ticker = MagicMock()

        def _ticker_factory(ticker_name: str) -> MagicMock:
            m = MagicMock()
            if ticker_name == "DX-Y.NYB":
                m.history.return_value = _df_from_closes(dxy_closes)
            elif ticker_name == "^TNX":
                m.history.return_value = _df_from_closes(ust10y_closes)
            elif ticker_name == "TIP":
                m.history.return_value = _df_from_closes(tip_closes)
            else:
                m.history.return_value = pd.DataFrame()
            return m

        with patch("yfinance.Ticker", side_effect=_ticker_factory):
            provider = MacroRatesProvider(cache_ttl_sec=60)
            snap = provider.get_snapshot()

        assert snap is not None
        assert snap.dxy_last == pytest.approx(105.4)
        # 24h Δ % = (105.4 - 105.3) / 105.3 * 100
        assert snap.dxy_change_24h_pct == pytest.approx(
            (105.4 - 105.3) / 105.3 * 100, rel=1e-6
        )
        # 5d Δ % vs iloc[-6] = 104.5
        assert snap.dxy_change_5d_pct == pytest.approx(
            (105.4 - 104.5) / 104.5 * 100, rel=1e-6
        )
        # UST10Y last (4.31 уже в %)
        assert snap.ust10y_last_pct == pytest.approx(4.31)
        # 24h bps = (4.31 - 4.30) * 100 = +1.0 bps
        assert snap.ust10y_change_24h_bps == pytest.approx(1.0, rel=1e-3)
        # 5d bps = (4.31 - 4.25) * 100 = +6.0 bps
        assert snap.ust10y_change_5d_bps == pytest.approx(6.0, rel=1e-3)
        # TIP last
        assert snap.tip_last == pytest.approx(110.85)
        assert snap.fetched_at_utc  # not empty
        del mock_ticker  # quiet linter unused

    def test_ust10y_legacy_normalize_divides_by_10(self):
        """^TNX в старых yfinance возвращался как yield*10. Normalize."""
        from fx_ai_trader.data.macro_rates import MacroRatesProvider

        legacy_closes = [42.0, 43.0, 43.5, 43.8, 43.0, 43.2, 43.1, 43.1]

        def _ticker_factory(ticker_name: str) -> MagicMock:
            m = MagicMock()
            if ticker_name == "^TNX":
                m.history.return_value = _df_from_closes(legacy_closes)
            else:
                m.history.return_value = _df_from_closes([100.0, 100.5])
            return m

        with patch("yfinance.Ticker", side_effect=_ticker_factory):
            provider = MacroRatesProvider(cache_ttl_sec=60)
            snap = provider.get_snapshot()

        assert snap is not None
        # 43.1 / 10 = 4.31
        assert snap.ust10y_last_pct == pytest.approx(4.31, rel=1e-6)
        # 24h bps: (4.31 - 4.31) * 100 = 0 (43.1 → 43.1 после делёжки)
        assert snap.ust10y_change_24h_bps == pytest.approx(0.0, abs=0.05)

    def test_short_history_partial_deltas(self):
        """Если closes <6 — 5d Δ None, 24h Δ ок при >=2."""
        from fx_ai_trader.data.macro_rates import MacroRatesProvider

        short_closes = [104.0, 105.0]  # holiday streak edge case

        def _ticker_factory(ticker_name: str) -> MagicMock:
            m = MagicMock()
            m.history.return_value = _df_from_closes(short_closes)
            return m

        with patch("yfinance.Ticker", side_effect=_ticker_factory):
            provider = MacroRatesProvider(cache_ttl_sec=60)
            snap = provider.get_snapshot()

        assert snap is not None
        assert snap.dxy_last == 105.0
        assert snap.dxy_change_24h_pct is not None
        assert snap.dxy_change_5d_pct is None

    def test_empty_history_returns_none(self):
        from fx_ai_trader.data.macro_rates import MacroRatesProvider

        def _ticker_factory(ticker_name: str) -> MagicMock:
            m = MagicMock()
            m.history.return_value = pd.DataFrame()
            return m

        with patch("yfinance.Ticker", side_effect=_ticker_factory):
            provider = MacroRatesProvider(cache_ttl_sec=60)
            snap = provider.get_snapshot()

        # Все три тикера пустые → snapshot=None (graceful)
        assert snap is None


# ─── MacroRatesProvider — caching ──────────────────────────────────────


class TestMacroRatesProviderCaching:
    def test_second_call_within_ttl_uses_cache(self):
        from fx_ai_trader.data.macro_rates import MacroRatesProvider

        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]

        call_count = {"n": 0}

        def _ticker_factory(ticker_name: str) -> MagicMock:
            call_count["n"] += 1
            m = MagicMock()
            m.history.return_value = _df_from_closes(closes)
            return m

        with patch("yfinance.Ticker", side_effect=_ticker_factory):
            provider = MacroRatesProvider(cache_ttl_sec=60)
            snap1 = provider.get_snapshot()
            first_count = call_count["n"]
            snap2 = provider.get_snapshot()

        assert snap1 is snap2  # тот же объект из кэша
        assert call_count["n"] == first_count  # yfinance не дёрнули второй раз

    def test_ttl_expiry_refetches(self):
        from fx_ai_trader.data.macro_rates import MacroRatesProvider

        closes = [100.0, 105.0]

        def _ticker_factory(ticker_name: str) -> MagicMock:
            m = MagicMock()
            m.history.return_value = _df_from_closes(closes)
            return m

        with patch("yfinance.Ticker", side_effect=_ticker_factory):
            provider = MacroRatesProvider(cache_ttl_sec=0)
            snap1 = provider.get_snapshot()
            time.sleep(0.01)
            snap2 = provider.get_snapshot()

        assert snap1 is not None and snap2 is not None
        # При TTL=0 — это разные объекты (refetch)
        assert snap1 is not snap2


# ─── MacroRatesProvider — graceful degradation ─────────────────────────


class TestMacroRatesProviderGracefulDegradation:
    def test_yfinance_throws_returns_none_first_call(self):
        from fx_ai_trader.data.macro_rates import MacroRatesProvider

        def _ticker_factory(ticker_name: str) -> MagicMock:
            raise RuntimeError("yfinance network error")

        with patch("yfinance.Ticker", side_effect=_ticker_factory):
            provider = MacroRatesProvider(cache_ttl_sec=60)
            snap = provider.get_snapshot()

        # Первый вызов и нет кэша → None (не падаем, не raise)
        assert snap is None

    def test_yfinance_throws_returns_stale_cache(self):
        from fx_ai_trader.data.macro_rates import MacroRatesProvider

        good_closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]

        first_call = {"fail": False}

        def _ticker_factory(ticker_name: str) -> MagicMock:
            if first_call["fail"]:
                raise RuntimeError("network down")
            m = MagicMock()
            m.history.return_value = _df_from_closes(good_closes)
            return m

        with patch("yfinance.Ticker", side_effect=_ticker_factory):
            provider = MacroRatesProvider(cache_ttl_sec=0)  # always refetch
            snap1 = provider.get_snapshot()
            assert snap1 is not None
            first_call["fail"] = True
            snap2 = provider.get_snapshot()

        # При failure refetch — возвращаем последний успешный кэш
        assert snap2 is snap1


# ─── MarketContext integration ─────────────────────────────────────────


class TestMarketContextIntegration:
    def test_collect_market_context_includes_macro_rates_when_provider_given(self):
        from fx_ai_trader.data.macro_rates import (
            MacroRatesProvider,
            MacroRatesSnapshot,
        )
        from fx_ai_trader.trading.context import collect_market_context

        adapter = MagicMock()
        adapter.get_bars.return_value = []
        store = MagicMock()
        store.get_open_positions.return_value = []

        provider = MacroRatesProvider(cache_ttl_sec=999)
        canned = MacroRatesSnapshot(
            dxy_last=105.0, dxy_change_24h_pct=-0.1, dxy_change_5d_pct=-0.5,
            ust10y_last_pct=4.30, ust10y_change_24h_bps=-2.0,
            ust10y_change_5d_bps=-10.0,
            tip_last=110.0, tip_change_24h_pct=+0.1, tip_change_5d_pct=+0.5,
            fetched_at_utc="2026-05-27T06:30:00+00:00",
        )
        provider._cache = canned
        provider._cache_ts = time.time()  # внутри TTL

        ctx = collect_market_context(
            adapter, store, ("XAUUSD",), 500.0,
            macro_rates_provider=provider,
        )
        assert ctx.macro_rates_block is not None
        assert "DXY" in ctx.macro_rates_block
        assert "105.0" in ctx.macro_rates_block

    def test_collect_market_context_no_provider_no_block(self):
        from fx_ai_trader.trading.context import collect_market_context

        adapter = MagicMock()
        adapter.get_bars.return_value = []
        store = MagicMock()
        store.get_open_positions.return_value = []

        ctx = collect_market_context(
            adapter, store, ("XAUUSD",), 500.0,
            macro_rates_provider=None,
        )
        assert ctx.macro_rates_block is None

    def test_collect_market_context_provider_failure_no_block_but_no_raise(self):
        from fx_ai_trader.trading.context import collect_market_context

        adapter = MagicMock()
        adapter.get_bars.return_value = []
        store = MagicMock()
        store.get_open_positions.return_value = []

        bad_provider = MagicMock()
        bad_provider.enabled = True
        bad_provider.get_snapshot.side_effect = RuntimeError("yfinance down")

        ctx = collect_market_context(
            adapter, store, ("XAUUSD",), 500.0,
            macro_rates_provider=bad_provider,
        )
        # Падать не должны, блок просто отсутствует
        assert ctx.macro_rates_block is None


# ─── format_context_for_prompt rendering ───────────────────────────────


class TestFormatContextRendering:
    def _make_ctx(self, *, with_rates: bool):
        from fx_ai_trader.trading.context import MarketContext

        rates_block = None
        if with_rates:
            rates_block = (
                "=== US MACRO RATES (gold/oil drivers; gold-canonical "
                "hierarchy: real yields → DXY) ===\n"
                "DXY (US Dollar Index, ICE futures DX-Y.NYB): 105.40 "
                "(24h=-0.10%, 5d=-0.50%)\n"
                "UST10Y nominal yield (CBOE TNX): 4.30% "
                "(24h=-2.0bps, 5d=-10.0bps)\n"
                "TIP (iShares TIPS ETF, real-yields proxy — price↑ ↔ "
                "real yields↓): $110.00 (24h=+0.10%, 5d=+0.50%)\n"
                "(fetched 2026-05-27T06:30:00+00:00 UTC)"
            )
        return MarketContext(
            snapshots=[],
            open_positions=[],
            virtual_capital_usd=500.0,
            macro_rates_block=rates_block,
        )

    def test_prompt_includes_macro_rates_block_when_present(self):
        from fx_ai_trader.trading.context import format_context_for_prompt

        ctx = self._make_ctx(with_rates=True)
        out = format_context_for_prompt(ctx)
        assert "US MACRO RATES" in out
        assert "DXY" in out and "UST10Y" in out and "TIP" in out
        # И блок появляется ДО market-data (gold hierarchy first)
        assert out.index("US MACRO RATES") < out.index("MARKET DATA")

    def test_prompt_omits_macro_rates_block_when_none(self):
        from fx_ai_trader.trading.context import format_context_for_prompt

        ctx = self._make_ctx(with_rates=False)
        out = format_context_for_prompt(ctx)
        assert "US MACRO RATES" not in out

    def test_review_format_does_not_include_macro_rates(self):
        """Review-cycle = lite (NO macro feed). Rates туда не подмешиваем."""
        from fx_ai_trader.trading.context import format_context_for_review

        ctx = self._make_ctx(with_rates=True)  # даже если по errors поле есть
        out = format_context_for_review(ctx)
        assert "US MACRO RATES" not in out
        assert "DXY" not in out
