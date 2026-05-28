"""Tests for ai_trader v0.30 macro_rates module.

Закрывает: DXY + UST10Y nominal через yfinance + рендер в text-блок.
TIPS ETF (real-yield proxy) — НЕ в ai_trader (релевантно для золота,
не для крипты; см. модуль docstring).

Без сетевых вызовов: yfinance.Ticker полностью замокан через
unittest.mock.patch на уровне импорта.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ai_trader.data.macro_rates import (
    MacroRatesProvider,
    MacroRatesSnapshot,
    format_macro_rates_snapshot,
)


# ─── format_macro_rates_snapshot ─────────────────────────────────────────


class TestFormatMacroRatesSnapshot:
    def test_returns_none_for_none(self):
        assert format_macro_rates_snapshot(None) is None

    def test_returns_none_when_all_fields_empty(self):
        snap = MacroRatesSnapshot(
            dxy_last=None, dxy_change_24h_pct=None, dxy_change_5d_pct=None,
            ust10y_last_pct=None, ust10y_change_24h_bps=None,
            ust10y_change_5d_bps=None,
            fetched_at_utc="2026-05-27T06:30:00+00:00",
        )
        assert format_macro_rates_snapshot(snap) is None

    def test_renders_full_block_with_both_series(self):
        snap = MacroRatesSnapshot(
            dxy_last=105.42,
            dxy_change_24h_pct=-0.18,
            dxy_change_5d_pct=-0.71,
            ust10y_last_pct=4.31,
            ust10y_change_24h_bps=-3.0,
            ust10y_change_5d_bps=-12.0,
            fetched_at_utc="2026-05-27T06:30:00+00:00",
        )
        out = format_macro_rates_snapshot(snap)
        assert out is not None
        assert "US MACRO RATES" in out
        assert "BTC↔DXY corr" in out  # crypto-specific reference
        assert "DXY" in out and "105.42" in out
        assert "24h=-0.18%" in out and "5d=-0.71%" in out
        assert "UST10Y nominal" in out and "4.31%" in out
        assert "24h=-3.0bps" in out and "5d=-12.0bps" in out
        assert "2026-05-27" in out

    def test_renders_partial_block_only_dxy(self):
        snap = MacroRatesSnapshot(
            dxy_last=105.42, dxy_change_24h_pct=-0.18, dxy_change_5d_pct=None,
            ust10y_last_pct=None, ust10y_change_24h_bps=None,
            ust10y_change_5d_bps=None,
            fetched_at_utc="2026-05-27T06:30:00+00:00",
        )
        out = format_macro_rates_snapshot(snap)
        assert out is not None
        assert "DXY" in out
        assert "UST10Y" not in out
        assert "5d=n/a" in out

    def test_renders_with_n_a_24h(self):
        snap = MacroRatesSnapshot(
            dxy_last=105.0, dxy_change_24h_pct=None, dxy_change_5d_pct=-0.5,
            ust10y_last_pct=None, ust10y_change_24h_bps=None,
            ust10y_change_5d_bps=None,
            fetched_at_utc="2026-05-27T06:30:00+00:00",
        )
        out = format_macro_rates_snapshot(snap)
        assert out is not None
        assert "24h=n/a" in out


# ─── MacroRatesProvider with mocked yfinance ─────────────────────────────


def _mock_ticker_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": closes})


class TestMacroRatesProviderFetch:
    def test_fetch_dxy_and_ust10y_happy_path(self):
        # 6+ закрытий → есть и pct_24h и pct_5d
        dxy_closes = [105.0, 105.1, 105.3, 105.5, 105.4, 105.2, 105.3]
        ust10y_closes = [43.0, 43.1, 43.2, 43.3, 43.1, 43.0, 43.1]  # raw ÷10

        def fake_ticker(t):
            mock = MagicMock()
            if t == "DX-Y.NYB":
                mock.history.return_value = _mock_ticker_df(dxy_closes)
            elif t == "^TNX":
                mock.history.return_value = _mock_ticker_df(ust10y_closes)
            return mock

        provider = MacroRatesProvider(cache_ttl_sec=1800)
        with patch("yfinance.Ticker", side_effect=fake_ticker):
            snap = provider.get_snapshot()
        assert snap is not None
        assert snap.dxy_last == pytest.approx(105.3)
        # UST10Y >25 → /10 normalization: 43.1 → 4.31
        assert snap.ust10y_last_pct == pytest.approx(4.31, abs=0.01)
        assert snap.dxy_change_24h_pct is not None
        assert snap.dxy_change_5d_pct is not None
        assert snap.ust10y_change_24h_bps is not None
        assert snap.ust10y_change_5d_bps is not None

    def test_ust10y_below_25_no_normalization(self):
        """Если ticker вернёт уже-нормализованные значения (например при
        смене схемы yfinance), нормализация НЕ должна срабатывать.
        """
        dxy_closes = [105.0, 105.5]
        ust10y_closes = [4.3, 4.4]  # уже % - не делить на 10

        def fake_ticker(t):
            mock = MagicMock()
            if t == "DX-Y.NYB":
                mock.history.return_value = _mock_ticker_df(dxy_closes)
            elif t == "^TNX":
                mock.history.return_value = _mock_ticker_df(ust10y_closes)
            return mock

        provider = MacroRatesProvider()
        with patch("yfinance.Ticker", side_effect=fake_ticker):
            snap = provider.get_snapshot()
        assert snap is not None
        assert snap.ust10y_last_pct == pytest.approx(4.4, abs=0.01)

    def test_cache_hit_within_ttl_no_refetch(self):
        dxy_closes = [105.0, 105.5]
        ust10y_closes = [43.0, 43.1]

        call_counter = {"count": 0}

        def fake_ticker(t):
            call_counter["count"] += 1
            mock = MagicMock()
            if t == "DX-Y.NYB":
                mock.history.return_value = _mock_ticker_df(dxy_closes)
            elif t == "^TNX":
                mock.history.return_value = _mock_ticker_df(ust10y_closes)
            return mock

        provider = MacroRatesProvider(cache_ttl_sec=1800)
        with patch("yfinance.Ticker", side_effect=fake_ticker):
            snap1 = provider.get_snapshot()
            snap2 = provider.get_snapshot()
        assert snap1 is snap2  # тот же объект из кэша
        # 2 ticker'а (DXY + UST10Y) на первый fetch, второй из кэша
        assert call_counter["count"] == 2

    def test_yfinance_exception_returns_cache_or_none(self):
        provider = MacroRatesProvider(cache_ttl_sec=1800)
        with patch("yfinance.Ticker", side_effect=RuntimeError("net down")):
            snap = provider.get_snapshot()
        assert snap is None  # кэша нет, None

    def test_empty_dataframe_returns_none(self):
        def fake_ticker(t):
            mock = MagicMock()
            mock.history.return_value = pd.DataFrame()
            return mock

        provider = MacroRatesProvider()
        with patch("yfinance.Ticker", side_effect=fake_ticker):
            snap = provider.get_snapshot()
        assert snap is None

    def test_partial_history_only_last_no_deltas(self):
        # 1 close → only last, без 24h/5d
        def fake_ticker(t):
            mock = MagicMock()
            if t == "DX-Y.NYB":
                mock.history.return_value = _mock_ticker_df([105.3])
            elif t == "^TNX":
                mock.history.return_value = pd.DataFrame()  # пусто
            return mock

        provider = MacroRatesProvider()
        with patch("yfinance.Ticker", side_effect=fake_ticker):
            snap = provider.get_snapshot()
        assert snap is not None
        assert snap.dxy_last == pytest.approx(105.3)
        assert snap.dxy_change_24h_pct is None
        assert snap.dxy_change_5d_pct is None
        assert snap.ust10y_last_pct is None

    def test_cache_serves_after_first_success_then_failure(self):
        """Cache: после успешного fetch с ttl=0, failure не уничтожает кэш."""
        good_dxy = [105.0, 105.5]
        good_ust = [43.0, 43.1]

        # First call: успех
        def good_ticker(t):
            mock = MagicMock()
            if t == "DX-Y.NYB":
                mock.history.return_value = _mock_ticker_df(good_dxy)
            elif t == "^TNX":
                mock.history.return_value = _mock_ticker_df(good_ust)
            return mock

        provider = MacroRatesProvider(cache_ttl_sec=0)
        with patch("yfinance.Ticker", side_effect=good_ticker):
            snap1 = provider.get_snapshot()
        assert snap1 is not None
        time.sleep(0.01)
        # Second call: exception → cache fallback
        with patch("yfinance.Ticker", side_effect=RuntimeError("fail")):
            snap2 = provider.get_snapshot()
        assert snap2 is snap1  # cached snapshot returned
