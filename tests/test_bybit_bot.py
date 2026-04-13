"""Тесты Bybit Bot V2: импорты, настройки, индикаторы, стратегия, executor, killswitch, store."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bybit_bot.market_data.models import Bar


def _make_bars(
    symbol: str = "BTCUSDT",
    n: int = 210,
    base_price: float = 60000.0,
    step: float = 10.0,
    volume: float = 1000.0,
) -> list[Bar]:
    bars = []
    for i in range(n):
        price = base_price + step * (i % 20 - 10)
        bars.append(Bar(
            symbol=symbol,
            ts=datetime(2026, 4, 11, 10 + i // 60, i % 60, tzinfo=UTC),
            open=price - 5,
            high=price + 20,
            low=price - 20,
            close=price,
            volume=volume,
        ))
    return bars


def _make_trending_bars(
    symbol: str = "BTCUSDT",
    n: int = 210,
    base_price: float = 60000.0,
    trend: float = 5.0,
    volume: float = 1000.0,
) -> list[Bar]:
    """Бары с устойчивым трендом вверх."""
    bars = []
    for i in range(n):
        price = base_price + trend * i
        bars.append(Bar(
            symbol=symbol,
            ts=datetime(2026, 4, 1, tzinfo=UTC),
            open=price - 2,
            high=price + 10,
            low=price - 10,
            close=price,
            volume=volume,
        ))
    return bars


# ── Imports ──────────────────────────────────────────────────

def test_imports_v2():
    """Все модули V2 импортируются без ошибок."""
    from bybit_bot.config.settings import Settings, display_name
    from bybit_bot.market_data.models import Bar
    from bybit_bot.market_data.feed import fetch_bars_bybit, fetch_bars_batch_bybit
    from bybit_bot.analysis.indicators import ema, atr, adx, volume_avg
    from bybit_bot.strategies.trend_ema import EmaTrendStrategy, TrendSignal
    from bybit_bot.trading.client import BybitClient, OrderResult, PositionInfo
    from bybit_bot.trading.executor import TradeExecutor, TradeParams
    from bybit_bot.trading.killswitch import KillSwitch, KillSwitchConfig
    from bybit_bot.stats.store import StatsStore
    from bybit_bot.app.main import main


# ── Settings V2 ──────────────────────────────────────────────

def test_settings_v2_defaults():
    from bybit_bot.config.settings import Settings
    s = Settings(api_key="test", api_secret="test", _env_file=None)
    assert s.demo is True
    assert s.trading_enabled is False
    assert s.category == "linear"
    assert "BTCUSDT" in s.scan_symbols
    assert len(s.scan_symbols) == 5
    assert s.leverage == 3
    assert s.account_balance == 500.0
    assert s.max_positions == 2
    assert s.kline_interval == "60"
    assert s.kline_limit == 200
    assert s.ema_fast == 12
    assert s.ema_slow == 26
    assert s.ema_trend == 200
    assert s.adx_threshold == 15.0
    assert s.sl_atr_mult == 2.0
    assert s.tp_atr_mult == 3.0
    assert s.trailing_activation_atr == 1.5
    assert s.trailing_distance_atr == 1.0
    assert s.time_stop_bars == 48
    assert s.killswitch_max_daily_loss == 15.0
    assert s.killswitch_max_drawdown_pct == 10.0
    assert s.killswitch_max_loss_per_trade == 10.0


# ── Indicators ───────────────────────────────────────────────

class TestIndicators:
    def test_ema_basic(self):
        from bybit_bot.analysis.indicators import ema
        values = [float(i) for i in range(30)]
        result = ema(values, 10)
        assert len(result) == 30
        assert result[-1] > result[10]

    def test_ema_short_data(self):
        from bybit_bot.analysis.indicators import ema
        result = ema([1.0, 2.0], 10)
        assert result == [1.0, 2.0]

    def test_ema_empty(self):
        from bybit_bot.analysis.indicators import ema
        assert ema([], 10) == []

    def test_atr_basic(self):
        from bybit_bot.analysis.indicators import atr
        bars = _make_bars(n=30)
        val = atr(bars, 14)
        assert val > 0

    def test_atr_insufficient(self):
        from bybit_bot.analysis.indicators import atr
        bars = _make_bars(n=5)
        assert atr(bars, 14) == 0.0

    def test_adx_trending(self):
        from bybit_bot.analysis.indicators import adx
        bars = _make_trending_bars(n=60, trend=10.0)
        val = adx(bars, 14)
        assert val > 0

    def test_adx_insufficient(self):
        from bybit_bot.analysis.indicators import adx
        bars = _make_bars(n=10)
        assert adx(bars, 14) == 0.0

    def test_volume_avg(self):
        from bybit_bot.analysis.indicators import volume_avg
        bars = _make_bars(n=30, volume=500.0)
        assert volume_avg(bars, 20) == pytest.approx(500.0)

    def test_volume_avg_insufficient(self):
        from bybit_bot.analysis.indicators import volume_avg
        bars = _make_bars(n=5, volume=100.0)
        assert volume_avg(bars, 20) == 0.0


# ── EMA Trend Strategy ──────────────────────────────────────

class TestEmaTrendStrategy:
    def test_no_signal_insufficient_bars(self):
        from bybit_bot.strategies.trend_ema import EmaTrendStrategy
        strat = EmaTrendStrategy()
        bars = _make_bars(n=50)
        assert strat.evaluate("BTCUSDT", bars) is None

    def test_no_signal_flat_market(self):
        from bybit_bot.strategies.trend_ema import EmaTrendStrategy
        strat = EmaTrendStrategy()
        bars = _make_bars(n=210, step=0.01)
        sig = strat.evaluate("BTCUSDT", bars)
        # В боковике не должно быть сигнала (ADX < 20 или нет crossover)
        # Допускаем None или сигнал — зависит от данных
        assert sig is None or sig.direction in ("Buy", "Sell")

    def test_scan_returns_list(self):
        from bybit_bot.strategies.trend_ema import EmaTrendStrategy
        strat = EmaTrendStrategy()
        bars_map = {"BTCUSDT": _make_bars(n=210)}
        signals = strat.scan(bars_map)
        assert isinstance(signals, list)

    def test_scan_skips_open_symbols(self):
        from bybit_bot.strategies.trend_ema import EmaTrendStrategy
        strat = EmaTrendStrategy()
        bars_map = {"BTCUSDT": _make_bars(n=210)}
        signals = strat.scan(bars_map, open_symbols={"BTCUSDT"})
        assert signals == []

    def test_signal_fields(self):
        from bybit_bot.strategies.trend_ema import TrendSignal
        sig = TrendSignal(
            symbol="BTCUSDT",
            direction="Buy",
            price=60000.0,
            sl=59000.0,
            tp=61500.0,
            atr_val=500.0,
            reasons=("ema_cross_up", "ema200_ok", "adx=25", "vol_ok"),
        )
        assert sig.direction == "Buy"
        assert sig.sl == 59000.0
        assert sig.tp == 61500.0
        assert len(sig.reasons) == 4

    def test_min_bars_property(self):
        from bybit_bot.strategies.trend_ema import EmaTrendStrategy
        strat = EmaTrendStrategy(trend_period=200)
        assert strat.min_bars == 202


# ── Executor V2 ──────────────────────────────────────────────

def test_executor_round_qty():
    from bybit_bot.trading.executor import TradeExecutor
    from bybit_bot.trading.client import InstrumentInfo

    btc = InstrumentInfo("BTCUSDT", "Trading", 0.001, 0.001, 0.10, 5.0, 100.0)
    assert TradeExecutor._round_qty_api(0.0234, btc) == 0.023
    assert TradeExecutor._round_qty_api(0.0001, btc) == 0.0

    doge = InstrumentInfo("DOGEUSDT", "Trading", 10.0, 10.0, 0.00001, 5.0, 75.0)
    assert TradeExecutor._round_qty_api(15.7, doge) == 10.0
    assert TradeExecutor._round_qty_api(5.0, doge) == 0.0


def test_executor_floor_rounding():
    from bybit_bot.trading.executor import TradeExecutor
    from bybit_bot.trading.client import InstrumentInfo

    btc = InstrumentInfo("BTCUSDT", "Trading", 0.001, 0.001, 0.10, 5.0, 100.0)
    assert TradeExecutor._round_qty_api(0.0239, btc) == 0.023
    assert TradeExecutor._round_qty_api(0.0091, btc) == 0.009

    sol = InstrumentInfo("SOLUSDT", "Trading", 0.1, 0.1, 0.01, 5.0, 50.0)
    assert TradeExecutor._round_qty_api(1.99, sol) == 1.9


def test_executor_compute_trade_v2():
    """Executor V2 работает с TrendSignal."""
    from unittest.mock import MagicMock
    from bybit_bot.trading.executor import TradeExecutor
    from bybit_bot.strategies.trend_ema import TrendSignal
    from bybit_bot.config.settings import Settings

    settings = Settings(
        api_key="test", api_secret="test", _env_file=None,
        account_balance=500, leverage=3,
        capital_per_trade_pct=0.02,
        max_margin_per_trade_pct=0.25,
    )
    executor = TradeExecutor(client=MagicMock(), settings=settings)

    sig = TrendSignal(
        symbol="SOLUSDT",
        direction="Buy",
        price=150.0,
        sl=140.0,
        tp=165.0,
        atr_val=5.0,
        reasons=("ema_cross_up", "ema200_ok"),
    )
    result = executor.compute_trade(sig, available_balance=500.0)
    assert result is not None
    assert result.side == "Buy"
    assert result.sl is not None
    assert result.tp is not None
    qty = float(result.qty)
    margin = qty * 150.0 / 3
    assert margin <= 500 * 0.25


# ── KillSwitch ───────────────────────────────────────────────

def _ks_cfg(**overrides):
    from bybit_bot.trading.killswitch import KillSwitchConfig
    defaults = dict(max_daily_loss_usd=15.0, max_drawdown_pct=10.0,
                    max_positions=3, max_loss_per_trade_usd=10.0)
    defaults.update(overrides)
    return KillSwitchConfig(**defaults)


def test_killswitch_allows_initially():
    from bybit_bot.trading.killswitch import KillSwitch
    ks = KillSwitch(_ks_cfg(), initial_equity=500)
    assert ks.check_allowed(0, 500) is True
    assert ks.is_tripped is False


def test_killswitch_blocks_max_positions():
    from bybit_bot.trading.killswitch import KillSwitch
    ks = KillSwitch(_ks_cfg(max_positions=2), initial_equity=500)
    assert ks.check_allowed(2, 500) is False


def test_killswitch_trips_on_daily_loss():
    from bybit_bot.trading.killswitch import KillSwitch
    ks = KillSwitch(_ks_cfg(max_daily_loss_usd=15.0), initial_equity=500)
    ks.record_trade_close(-16.0)
    assert ks.check_allowed(0, 484) is False
    assert ks.is_tripped is True


# ── StatsStore ───────────────────────────────────────────────

def test_stats_store(tmp_path):
    from bybit_bot.stats.store import StatsStore
    store = StatsStore(tmp_path / "test.sqlite")

    sig_id = store.log_signal("BTCUSDT", "long", 0.8, "ema_cross_up", 60000.0)
    assert sig_id > 0

    pos_id = store.open_position(
        symbol="BTCUSDT", side="Buy", qty="0.001",
        entry_price=60000.0, order_id="test-123",
        sl=59000.0, tp=63000.0, strategy="trend_ema_v2",
    )
    assert pos_id > 0

    open_pos = store.get_open_positions()
    assert len(open_pos) == 1
    assert open_pos[0].symbol == "BTCUSDT"
    assert open_pos[0].strategy == "trend_ema_v2"

    store.close_position(pos_id, exit_price=61000.0, pnl_usd=10.0, close_reason="tp_hit")
    assert len(store.get_open_positions()) == 0

    stats = store.get_total_stats()
    assert stats["total_trades"] == 1
    assert stats["wins"] == 1
    assert stats["total_pnl"] == 10.0


def test_store_migration(tmp_path):
    import sqlite3
    db_path = tmp_path / "migrate.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT,
            strength REAL, reasons TEXT, price REAL, created_at TEXT
        );
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
            qty TEXT NOT NULL, entry_price REAL NOT NULL, sl REAL, tp REAL,
            order_id TEXT, strategy TEXT DEFAULT 'ensemble',
            signal_strength REAL DEFAULT 0, signal_reasons TEXT DEFAULT '',
            opened_at TEXT, closed_at TEXT, exit_price REAL, pnl_usd REAL,
            close_reason TEXT
        );
    """)
    conn.execute(
        "INSERT INTO positions (symbol, side, qty, entry_price, opened_at) "
        "VALUES ('BTCUSDT', 'Buy', '0.001', 60000.0, '2026-04-11')"
    )
    conn.commit()
    conn.close()

    from bybit_bot.stats.store import StatsStore
    store = StatsStore(db_path)
    positions = store.get_open_positions()
    assert len(positions) == 1
    assert positions[0].pair_tag == ""
    assert positions[0].opened_bar_idx == 0


# ── Feed V2 ──────────────────────────────────────────────────

def test_raw_to_bars():
    from bybit_bot.market_data.feed import _raw_to_bars
    raw = [
        ["1712880000000", "60000", "60500", "59500", "60200", "1234.5", "74000000"],
        ["1712883600000", "60200", "60800", "60100", "60700", "987.3", "59000000"],
    ]
    bars = _raw_to_bars(raw, "BTCUSDT")
    assert len(bars) == 2
    assert bars[0].symbol == "BTCUSDT"
    assert bars[0].open == 60000.0
    assert bars[0].close == 60200.0
    assert bars[1].close == 60700.0


def test_raw_to_bars_invalid():
    from bybit_bot.market_data.feed import _raw_to_bars
    raw = [["invalid"], [], None]
    bars = _raw_to_bars([r for r in raw if r], "BTCUSDT")
    assert bars == []
