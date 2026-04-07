"""Тесты модуля автоторговли: символы, kill switch, auth, executor."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fx_pro_bot.trading.auth import TokenData, TokenStore
from fx_pro_bot.trading.killswitch import KillSwitch, KillSwitchConfig
from fx_pro_bot.trading.symbols import (
    CTRADER_TO_YFINANCE,
    YFINANCE_TO_CTRADER,
    SymbolCache,
    SymbolInfo,
    lots_to_volume,
    price_to_relative,
    volume_to_lots,
)


# ── Symbols ─────────────────────────────────────────────────────


class TestSymbolMapping:
    def test_yfinance_to_ctrader_complete(self):
        assert YFINANCE_TO_CTRADER["EURUSD=X"] == "EURUSD"
        assert YFINANCE_TO_CTRADER["GC=F"] == "XAUUSD"
        from fx_pro_bot.trading.symbols import _YFINANCE_PREFIX_MAP
        assert _YFINANCE_PREFIX_MAP["CL=F"] == "#USOIL"

    def test_reverse_mapping(self):
        assert CTRADER_TO_YFINANCE["EURUSD"] == "EURUSD=X"
        assert CTRADER_TO_YFINANCE["XAUUSD"] == "GC=F"

    def test_lots_to_volume(self):
        assert lots_to_volume(0.01) == 100_000
        assert lots_to_volume(0.1) == 1_000_000
        assert lots_to_volume(1.0) == 10_000_000

    def test_volume_to_lots(self):
        assert abs(volume_to_lots(100_000) - 0.01) < 1e-9
        assert abs(volume_to_lots(10_000_000) - 1.0) < 1e-9

    def test_price_to_relative(self):
        assert price_to_relative(0.0050) == 500
        assert price_to_relative(0.50) == 50_000

    def test_symbol_cache(self):
        cache = SymbolCache()
        assert not cache.loaded

        symbols = [
            SymbolInfo(symbol_id=1, name="EURUSD", min_volume=1000,
                       max_volume=10_000_000, step_volume=1000, digits=5),
            SymbolInfo(symbol_id=2, name="XAUUSD", min_volume=100,
                       max_volume=1_000_000, step_volume=100, digits=2),
        ]
        cache.populate(symbols)
        assert cache.loaded

        assert cache.get_by_name("EURUSD").symbol_id == 1
        assert cache.get_by_id(2).name == "XAUUSD"
        assert cache.resolve_yfinance("EURUSD=X").symbol_id == 1
        assert cache.resolve_yfinance("GC=F").symbol_id == 2
        assert cache.resolve_yfinance("UNKNOWN") is None


# ── Kill Switch ─────────────────────────────────────────────────


class TestKillSwitch:
    def _make_ks(self, **overrides):
        defaults = dict(
            max_daily_loss_usd=50.0,
            max_drawdown_pct=20.0,
            max_positions=5,
            max_loss_per_trade_usd=25.0,
        )
        defaults.update(overrides)
        cfg = KillSwitchConfig(**defaults)
        return KillSwitch(cfg, initial_equity=1000.0)

    def test_allows_normal_trade(self):
        ks = self._make_ks()
        assert ks.check_allowed(open_positions=2, current_equity=1000.0)
        assert not ks.is_tripped

    def test_blocks_max_positions(self):
        ks = self._make_ks()
        assert not ks.check_allowed(open_positions=5, current_equity=1000.0)
        assert not ks.is_tripped  # max positions != trip

    def test_trips_on_daily_loss(self):
        ks = self._make_ks(max_daily_loss_usd=50.0)
        ks.record_trade_close(-30.0)
        assert ks.check_allowed(open_positions=0, current_equity=970.0)

        ks.record_trade_close(-25.0)
        assert not ks.check_allowed(open_positions=0, current_equity=945.0)
        assert ks.is_tripped
        assert "daily_loss" in ks.trip_reason

    def test_trips_on_drawdown(self):
        ks = self._make_ks(max_drawdown_pct=10.0)
        ks.check_allowed(open_positions=0, current_equity=1000.0)
        assert not ks.check_allowed(open_positions=0, current_equity=890.0)
        assert ks.is_tripped
        assert "drawdown" in ks.trip_reason

    def test_blocks_after_trip(self):
        ks = self._make_ks(max_daily_loss_usd=10.0)
        ks.record_trade_close(-15.0)
        ks.check_allowed(open_positions=0, current_equity=985.0)
        assert ks.is_tripped
        assert not ks.check_allowed(open_positions=0, current_equity=985.0)

    def test_reset(self):
        ks = self._make_ks(max_daily_loss_usd=10.0)
        ks.record_trade_close(-15.0)
        ks.check_allowed(open_positions=0, current_equity=985.0)
        assert ks.is_tripped

        ks.reset()
        assert not ks.is_tripped

    def test_disabled(self):
        ks = self._make_ks(enabled=False)
        ks.record_trade_close(-999.0)
        assert ks.check_allowed(open_positions=100, current_equity=1.0)

    def test_daily_stats_tracking(self):
        ks = self._make_ks()
        ks.record_trade_close(10.0)
        ks.record_trade_close(-5.0)
        assert ks.daily_stats.trades == 2
        assert abs(ks.daily_stats.realized_pnl_usd - 5.0) < 0.01


# ── Auth / Token Store ──────────────────────────────────────────


class TestTokenStore:
    def test_save_and_load(self, tmp_path):
        path = tmp_path / "tokens.json"
        store = TokenStore(path)

        token = TokenData(
            access_token="abc123",
            refresh_token="ref456",
            expires_at=time.time() + 3600,
        )
        store.save(token)

        loaded = store.load()
        assert loaded.access_token == "abc123"
        assert loaded.refresh_token == "ref456"

    def test_load_missing_file(self, tmp_path):
        store = TokenStore(tmp_path / "nonexistent.json")
        token = store.load()
        assert token.access_token == ""

    def test_token_expiry(self):
        t = TokenData(access_token="x", expires_at=time.time() - 100)
        assert t.is_expired
        assert not t.is_valid

        t2 = TokenData(access_token="x", expires_at=time.time() + 999999)
        assert not t2.is_expired
        assert t2.is_valid


# ── StatsStore: broker_position_id ──────────────────────────────


class TestBrokerPositionId:
    def test_set_and_get_broker_id(self, tmp_path):
        from fx_pro_bot.stats.store import StatsStore

        store = StatsStore(tmp_path / "test.sqlite")

        pid = store.open_position(
            strategy="leaders",
            source="cot",
            instrument="EURUSD=X",
            direction="long",
            entry_price=1.1000,
            stop_loss_price=1.0950,
        )

        pos = store.get_open_positions()[0]
        assert pos.broker_position_id == 0

        store.set_broker_position_id(pid, 12345)

        pos = store.get_open_positions()[0]
        assert pos.broker_position_id == 12345

        found = store.get_position_by_broker_id(12345)
        assert found is not None
        assert found.id == pid

    def test_get_nonexistent_broker_id(self, tmp_path):
        from fx_pro_bot.stats.store import StatsStore

        store = StatsStore(tmp_path / "test.sqlite")
        assert store.get_position_by_broker_id(99999) is None


# ── Executor (mocked client) ────────────────────────────────────


class TestTradeExecutor:
    def test_open_position_unknown_symbol(self):
        from fx_pro_bot.trading.executor import TradeExecutor

        client = MagicMock()
        cache = SymbolCache()
        executor = TradeExecutor(client, cache, lot_size=0.01)

        result = executor.open_position("UNKNOWN_SYMBOL", "long")
        assert not result.success
        assert "не найден" in result.error

    def test_clamp_volume(self):
        from fx_pro_bot.trading.executor import TradeExecutor

        sym = SymbolInfo(
            symbol_id=1, name="EURUSD",
            min_volume=1000, max_volume=5_000_000,
            step_volume=1000, digits=5,
        )
        assert TradeExecutor._clamp_volume(500, sym) == 1000
        assert TradeExecutor._clamp_volume(1500, sym) == 1000
        assert TradeExecutor._clamp_volume(100_000, sym) == 100_000
        assert TradeExecutor._clamp_volume(99_999_999, sym) == 5_000_000
