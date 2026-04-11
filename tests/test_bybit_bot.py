"""Базовые тесты bybit_bot: импорт всех модулей + unit-тесты ключевых компонентов."""

from __future__ import annotations

import pytest


def test_imports():
    """Все модули bybit_bot импортируются без ошибок."""
    from bybit_bot.config.settings import Settings, display_name, to_bybit, to_yfinance
    from bybit_bot.market_data.models import Bar
    from bybit_bot.market_data.feed import fetch_bars
    from bybit_bot.analysis.signals import Direction, Signal, rsi, atr, macd, bollinger
    from bybit_bot.analysis.ensemble import ensemble_signal
    from bybit_bot.analysis.scanner import scan_instruments, active_signals
    from bybit_bot.trading.client import BybitClient, OrderResult, PositionInfo
    from bybit_bot.trading.executor import TradeExecutor, TradeParams
    from bybit_bot.trading.killswitch import KillSwitch, KillSwitchConfig
    from bybit_bot.strategies.momentum import MomentumStrategy, TradeSignal
    from bybit_bot.stats.store import StatsStore
    from bybit_bot.app.main import main


def test_settings_defaults():
    from bybit_bot.config.settings import Settings
    s = Settings(
        api_key="test", api_secret="test",
        _env_file=None,
    )
    assert s.demo is True
    assert s.trading_enabled is False
    assert s.category == "linear"
    assert "BTCUSDT" in s.scan_symbols
    assert s.leverage == 5
    assert s.account_balance == 500.0
    assert s.max_positions == 3
    assert s.max_margin_per_trade_pct == 0.25
    assert s.killswitch_max_daily_loss == 15.0
    assert s.killswitch_max_drawdown_pct == 10.0
    assert s.killswitch_max_loss_per_trade == 7.50
    assert s.scalping_max_positions == 3


def test_symbol_mapping():
    from bybit_bot.config.settings import to_yfinance, to_bybit
    assert to_yfinance("BTCUSDT") == "BTC-USD"
    assert to_bybit("BTC-USD") == "BTCUSDT"
    assert to_yfinance("ETHUSDT") == "ETH-USD"
    assert to_bybit("ETH-USD") == "ETHUSDT"


def test_direction_enum():
    from bybit_bot.analysis.signals import Direction
    assert Direction.LONG.value == "long"
    assert Direction.SHORT.value == "short"
    assert Direction.FLAT.value == "flat"


def test_rsi_basic():
    from bybit_bot.analysis.signals import rsi
    closes = [100 + i * 0.5 for i in range(30)]
    val = rsi(closes, 14)
    assert 50 < val <= 100


def test_rsi_insufficient_data():
    from bybit_bot.analysis.signals import rsi
    assert rsi([100, 101], 14) == 50.0


def test_killswitch_allows_initially():
    from bybit_bot.trading.killswitch import KillSwitch, KillSwitchConfig
    ks = KillSwitch(KillSwitchConfig(max_positions=5), initial_equity=1000)
    assert ks.check_allowed(0, 1000) is True
    assert ks.is_tripped is False


def test_killswitch_blocks_max_positions():
    from bybit_bot.trading.killswitch import KillSwitch, KillSwitchConfig
    ks = KillSwitch(KillSwitchConfig(max_positions=3), initial_equity=1000)
    assert ks.check_allowed(3, 1000) is False


def test_killswitch_trips_on_daily_loss():
    from bybit_bot.trading.killswitch import KillSwitch, KillSwitchConfig
    ks = KillSwitch(KillSwitchConfig(max_daily_loss_usd=10.0), initial_equity=1000)
    ks.record_trade_close(-11.0)
    assert ks.check_allowed(0, 989) is False
    assert ks.is_tripped is True


def test_stats_store(tmp_path):
    from bybit_bot.stats.store import StatsStore
    store = StatsStore(tmp_path / "test.sqlite")

    sig_id = store.log_signal("BTCUSDT", "long", 0.8, "macd,bollinger", 60000.0)
    assert sig_id > 0

    pos_id = store.open_position(
        symbol="BTCUSDT", side="Buy", qty="0.001",
        entry_price=60000.0, order_id="test-123",
        sl=59000.0, tp=63000.0,
    )
    assert pos_id > 0

    open_pos = store.get_open_positions()
    assert len(open_pos) == 1
    assert open_pos[0].symbol == "BTCUSDT"

    store.close_position(pos_id, exit_price=61000.0, pnl_usd=10.0, close_reason="tp_hit")
    assert len(store.get_open_positions()) == 0

    stats = store.get_total_stats()
    assert stats["total_trades"] == 1
    assert stats["wins"] == 1
    assert stats["total_pnl"] == 10.0


def test_executor_round_qty():
    from bybit_bot.trading.executor import TradeExecutor
    from bybit_bot.trading.client import InstrumentInfo

    btc = InstrumentInfo("BTCUSDT", "Trading", 0.001, 0.001, 0.10, 5.0, 100.0)
    assert TradeExecutor._round_qty_api(0.0234, btc) == 0.023
    assert TradeExecutor._round_qty_api(0.0001, btc) == 0.0

    doge = InstrumentInfo("DOGEUSDT", "Trading", 10.0, 10.0, 0.00001, 5.0, 75.0)
    assert TradeExecutor._round_qty_api(15.7, doge) == 20.0
    assert TradeExecutor._round_qty_api(5.0, doge) == 0.0


def test_executor_margin_check():
    """Executor отклоняет сделку если маржа > max_margin_per_trade_pct от баланса."""
    from datetime import datetime, UTC
    from unittest.mock import MagicMock
    from bybit_bot.trading.executor import TradeExecutor
    from bybit_bot.analysis.signals import Direction, Signal
    from bybit_bot.market_data.models import Bar
    from bybit_bot.config.settings import Settings

    settings = Settings(
        api_key="test", api_secret="test", _env_file=None,
        account_balance=500, leverage=5,
        capital_per_trade_pct=0.05,
        max_margin_per_trade_pct=0.25,
    )
    executor = TradeExecutor(client=MagicMock(), settings=settings)

    bars = [
        Bar("BTCUSDT", datetime(2026, 1, 1, 10, i, tzinfo=UTC),
            100000 + i * 10, 100000 + i * 10 + 5,
            100000 + i * 10 - 5, 100000 + i * 10, 1000)
        for i in range(30)
    ]
    signal = Signal(direction=Direction.LONG, strength=0.8, reasons=("test",))

    result = executor.compute_trade("BTCUSDT", signal, bars, available_balance=500.0)
    if result is not None:
        qty = float(result.qty)
        margin = qty * bars[-1].close / settings.leverage
        assert margin <= 500 * 0.25, f"Margin ${margin:.2f} превышает 25% от $500"


def test_executor_micro_account_sizing():
    """Размер позиции корректен для микро-счёта $500."""
    from datetime import datetime, UTC
    from unittest.mock import MagicMock
    from bybit_bot.trading.executor import TradeExecutor
    from bybit_bot.analysis.signals import Direction, Signal
    from bybit_bot.market_data.models import Bar
    from bybit_bot.config.settings import Settings

    settings = Settings(
        api_key="test", api_secret="test", _env_file=None,
        account_balance=500, leverage=5,
        capital_per_trade_pct=0.05,
        max_margin_per_trade_pct=0.25,
    )
    executor = TradeExecutor(client=MagicMock(), settings=settings)

    bars = [
        Bar("SOLUSDT", datetime(2026, 1, 1, 10, i, tzinfo=UTC),
            150 + i * 0.1, 150 + i * 0.1 + 0.5,
            150 + i * 0.1 - 0.5, 150 + i * 0.1, 50000)
        for i in range(30)
    ]
    signal = Signal(direction=Direction.LONG, strength=0.8, reasons=("test",))
    result = executor.compute_trade("SOLUSDT", signal, bars, available_balance=500.0)

    assert result is not None, "SOL позиция должна открываться на $500 счёте"
    qty = float(result.qty)
    assert qty >= 0.1, f"qty={qty} слишком мал"
    risk_approx = qty * 2.0  # ~SL distance for SOL ≈ $2
    assert risk_approx < 50, "Risk per trade не должен превышать $50"


def test_ensemble_insufficient_bars():
    from bybit_bot.analysis.ensemble import ensemble_signal
    from bybit_bot.analysis.signals import Direction
    result = ensemble_signal([], min_votes=3)
    assert result.direction == Direction.FLAT
    assert result.strength == 0.0
