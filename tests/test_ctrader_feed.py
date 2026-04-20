"""Тесты cTrader feed: декодинг trendbars, маппинг таймфреймов, fallback."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from fx_pro_bot.market_data.ctrader_feed import (
    INTERVAL_TO_MINUTES,
    MIN_BARS_FOR_OK,
    PERIOD_TO_DAYS,
    bars_from_ctrader,
    bars_with_fallback,
)
from fx_pro_bot.market_data.models import Bar, InstrumentId
from fx_pro_bot.trading.symbols import SymbolCache, SymbolInfo


def _make_trendbar(low_abs: int, dopen: int, dhigh: int, dclose: int, ts_min: int, volume: int):
    return SimpleNamespace(
        low=low_abs,
        deltaOpen=dopen,
        deltaHigh=dhigh,
        deltaClose=dclose,
        utcTimestampInMinutes=ts_min,
        volume=volume,
    )


def _make_symbol_cache() -> SymbolCache:
    cache = SymbolCache()
    cache.populate([
        SymbolInfo(
            symbol_id=1, name="EURUSD",
            min_volume=1000, max_volume=1_000_000_000, step_volume=1000,
            digits=5, contract_size=100_000,
        ),
    ])
    return cache


class TestDecodeTrendbar:
    def test_bars_from_ctrader_decodes_ohlc_correctly(self):
        """low + deltas → корректные OHLC при digits=5."""
        cache = _make_symbol_cache()
        client = MagicMock()
        tb = _make_trendbar(
            low_abs=117500,
            dopen=50,
            dhigh=80,
            dclose=60,
            ts_min=int(datetime(2026, 4, 20, 8, 0, tzinfo=UTC).timestamp() // 60),
            volume=350,
        )
        client.get_trendbars.return_value = [tb]

        bars = bars_from_ctrader(
            "EURUSD=X", client=client, symbol_cache=cache,
            period="5d", interval="5m",
        )

        assert len(bars) == 1
        b = bars[0]
        assert isinstance(b, Bar)
        assert b.instrument == InstrumentId(symbol="EURUSD=X")
        assert b.low == 1.175
        assert b.open == 1.1755
        assert b.high == 1.1758
        assert b.close == 1.1756
        assert b.volume == 350.0
        assert b.ts == datetime(2026, 4, 20, 8, 0, tzinfo=UTC)

    def test_bars_from_ctrader_unknown_symbol_returns_empty(self):
        """Если yfinance-символ не замапен — пустой список, без вызова API."""
        cache = _make_symbol_cache()
        client = MagicMock()
        bars = bars_from_ctrader(
            "SOMETHING-USD", client=client, symbol_cache=cache,
        )
        assert bars == []
        client.get_trendbars.assert_not_called()

    def test_bars_from_ctrader_passes_correct_timeframe(self):
        """interval='5m' → period_minutes=5."""
        cache = _make_symbol_cache()
        client = MagicMock()
        client.get_trendbars.return_value = []

        bars_from_ctrader(
            "EURUSD=X", client=client, symbol_cache=cache,
            period="1d", interval="1m",
        )

        kwargs = client.get_trendbars.call_args.kwargs
        assert kwargs["period_minutes"] == INTERVAL_TO_MINUTES["1m"]
        assert kwargs["symbol_id"] == 1
        # range = 1 день = 86400000 мс
        assert kwargs["to_ts_ms"] - kwargs["from_ts_ms"] == PERIOD_TO_DAYS["1d"] * 86_400_000


class TestFallback:
    def test_fallback_used_when_client_none(self, monkeypatch):
        """Нет cTrader → сразу yfinance."""
        called: dict[str, bool] = {"yf": False}
        def _fake_yf(sym, period, interval):
            called["yf"] = True
            return []

        monkeypatch.setattr(
            "fx_pro_bot.market_data.ctrader_feed.bars_from_yfinance", _fake_yf,
        )

        bars_with_fallback("EURUSD=X", client=None, symbol_cache=None)
        assert called["yf"]

    def test_fallback_used_when_ctrader_returns_few_bars(self, monkeypatch):
        """cTrader вернул мало баров → fallback yfinance."""
        cache = _make_symbol_cache()
        client = MagicMock()
        client.get_trendbars.return_value = [
            _make_trendbar(117500, 0, 0, 0, i, 1) for i in range(10)
        ]
        called: dict[str, bool] = {"yf": False}
        def _fake_yf(sym, period, interval):
            called["yf"] = True
            return [MagicMock()] * (MIN_BARS_FOR_OK + 5)

        monkeypatch.setattr(
            "fx_pro_bot.market_data.ctrader_feed.bars_from_yfinance", _fake_yf,
        )

        result = bars_with_fallback("EURUSD=X", client=client, symbol_cache=cache)
        assert called["yf"]
        assert len(result) == MIN_BARS_FOR_OK + 5

    def test_fallback_used_when_ctrader_raises(self, monkeypatch):
        """cTrader кинул exception → fallback yfinance без краша."""
        cache = _make_symbol_cache()
        client = MagicMock()
        client.get_trendbars.side_effect = TimeoutError("cTrader timeout")
        called: dict[str, bool] = {"yf": False}
        def _fake_yf(sym, period, interval):
            called["yf"] = True
            return []

        monkeypatch.setattr(
            "fx_pro_bot.market_data.ctrader_feed.bars_from_yfinance", _fake_yf,
        )

        bars_with_fallback("EURUSD=X", client=client, symbol_cache=cache)
        assert called["yf"]

    def test_ctrader_used_when_enough_bars(self, monkeypatch):
        """cTrader вернул достаточно баров → yfinance НЕ вызывается."""
        cache = _make_symbol_cache()
        client = MagicMock()
        client.get_trendbars.return_value = [
            _make_trendbar(117500, 0, 0, 0, i, 1) for i in range(MIN_BARS_FOR_OK + 10)
        ]
        called: dict[str, bool] = {"yf": False}
        def _fake_yf(sym, period, interval):
            called["yf"] = True
            return []

        monkeypatch.setattr(
            "fx_pro_bot.market_data.ctrader_feed.bars_from_yfinance", _fake_yf,
        )

        result = bars_with_fallback("EURUSD=X", client=client, symbol_cache=cache)
        assert not called["yf"]
        assert len(result) == MIN_BARS_FOR_OK + 10

    def test_unknown_symbol_falls_back_to_yfinance(self, monkeypatch):
        """cTrader не знает символ (крипта) → fallback yfinance."""
        cache = _make_symbol_cache()
        client = MagicMock()
        called: dict[str, bool] = {"yf": False}
        def _fake_yf(sym, period, interval):
            called["yf"] = True
            return []

        monkeypatch.setattr(
            "fx_pro_bot.market_data.ctrader_feed.bars_from_yfinance", _fake_yf,
        )

        bars_with_fallback("SOMETHING-USD", client=client, symbol_cache=cache)
        assert called["yf"]
        client.get_trendbars.assert_not_called()


class TestScannerIntegration:
    def test_scan_instruments_uses_custom_fetcher(self):
        """scan_instruments принимает bar_fetcher и использует его."""
        from fx_pro_bot.analysis.scanner import scan_instruments

        fake_bar = MagicMock()
        called: dict[str, bool] = {"fetcher": False}

        def _fetcher(sym, period, interval):
            called["fetcher"] = True
            return []

        scan_instruments(("EURUSD=X",), bar_fetcher=_fetcher)
        assert called["fetcher"]
