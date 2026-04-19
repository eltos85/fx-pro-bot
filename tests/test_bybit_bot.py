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
    assert "SOLUSDT" in s.scan_symbols
    assert len(s.scan_symbols) == 8
    assert s.leverage == 5
    assert s.account_balance == 500.0
    assert s.max_margin_per_trade_pct == 0.25
    assert s.killswitch_max_daily_loss == 37.50
    assert s.killswitch_max_drawdown_pct == 25.0
    assert s.scalping_max_positions == 3
    assert s.momentum_enabled is False
    assert s.scalping_funding_enabled is False


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


def _ks_cfg(**overrides) -> "KillSwitchConfig":
    from bybit_bot.trading.killswitch import KillSwitchConfig
    defaults = dict(max_daily_loss_usd=37.50, max_drawdown_pct=25.0, max_positions=5)
    defaults.update(overrides)
    return KillSwitchConfig(**defaults)


def test_killswitch_allows_initially():
    from bybit_bot.trading.killswitch import KillSwitch
    ks = KillSwitch(_ks_cfg(max_positions=5), initial_equity=1000)
    assert ks.check_allowed(0, 1000) is True
    assert ks.is_tripped is False


def test_killswitch_blocks_max_positions():
    from bybit_bot.trading.killswitch import KillSwitch
    ks = KillSwitch(_ks_cfg(max_positions=3), initial_equity=1000)
    assert ks.check_allowed(3, 1000) is False


def test_killswitch_trips_on_daily_loss():
    from bybit_bot.trading.killswitch import KillSwitch
    ks = KillSwitch(_ks_cfg(max_daily_loss_usd=10.0), initial_equity=1000)
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

    assert store.get_cumulative_pnl() == pytest.approx(10.0)


def test_executor_round_qty():
    from bybit_bot.trading.executor import TradeExecutor
    from bybit_bot.trading.client import InstrumentInfo

    btc = InstrumentInfo("BTCUSDT", "Trading", 0.001, 0.001, 0.10, 5.0, 100.0)
    assert TradeExecutor._round_qty_api(0.0234, btc) == 0.023
    assert TradeExecutor._round_qty_api(0.0001, btc) == 0.0

    doge = InstrumentInfo("DOGEUSDT", "Trading", 10.0, 10.0, 0.00001, 5.0, 75.0)
    assert TradeExecutor._round_qty_api(15.7, doge) == 10.0  # floor: 15.7 -> 10
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


def test_signal_new_fields():
    """Signal поддерживает per-strategy SL/TP, pair_tag, strategy_name."""
    from bybit_bot.analysis.signals import Direction, Signal
    sig = Signal(
        direction=Direction.LONG, strength=0.8, reasons=("test",),
        sl_atr_mult=2.0, tp_atr_mult=1.5,
        pair_tag="sa_BTC_ETH_abc123", strategy_name="scalp_statarb",
    )
    assert sig.sl_atr_mult == 2.0
    assert sig.tp_atr_mult == 1.5
    assert sig.pair_tag == "sa_BTC_ETH_abc123"
    assert sig.strategy_name == "scalp_statarb"

    sig_default = Signal(direction=Direction.FLAT, strength=0.0, reasons=("x",))
    assert sig_default.sl_atr_mult is None
    assert sig_default.tp_atr_mult is None
    assert sig_default.pair_tag is None
    assert sig_default.strategy_name == ""


def test_store_pair_tag(tmp_path):
    """StatsStore: pair_tag и opened_bar_idx сохраняются и читаются."""
    from bybit_bot.stats.store import StatsStore
    store = StatsStore(tmp_path / "test_pair.sqlite")

    pos_id = store.open_position(
        symbol="BTCUSDT", side="Buy", qty="0.001",
        entry_price=60000.0, order_id="test-pair-1",
        strategy="scalp_statarb",
        pair_tag="sa_BTC_ETH_abc123", opened_bar_idx=42,
    )
    pos_id2 = store.open_position(
        symbol="ETHUSDT", side="Sell", qty="0.01",
        entry_price=3000.0, order_id="test-pair-2",
        strategy="scalp_statarb",
        pair_tag="sa_BTC_ETH_abc123", opened_bar_idx=42,
    )

    pair_pos = store.get_open_by_pair_tag("sa_BTC_ETH_abc123")
    assert len(pair_pos) == 2
    assert pair_pos[0].pair_tag == "sa_BTC_ETH_abc123"
    assert pair_pos[0].opened_bar_idx == 42

    tags = store.get_open_pair_tags()
    assert "sa_BTC_ETH_abc123" in tags

    store.close_position(pos_id, exit_price=61000.0, pnl_usd=5.0, close_reason="zscore_exit")
    store.close_position(pos_id2, exit_price=2950.0, pnl_usd=3.0, close_reason="pair_close")

    assert store.get_cumulative_pnl() == 8.0
    assert store.get_open_by_pair_tag("sa_BTC_ETH_abc123") == []


def test_store_migration(tmp_path):
    """Миграция добавляет pair_tag и opened_bar_idx к существующей БД."""
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


def test_executor_statarb_no_sltp():
    """Stat-Arb сигнал: executor не ставит SL/TP."""
    from datetime import datetime, UTC
    from unittest.mock import MagicMock
    from bybit_bot.trading.executor import TradeExecutor
    from bybit_bot.analysis.signals import Direction, Signal
    from bybit_bot.market_data.models import Bar
    from bybit_bot.config.settings import Settings

    settings = Settings(
        api_key="test", api_secret="test", _env_file=None,
        account_balance=500, leverage=5,
    )
    executor = TradeExecutor(client=MagicMock(), settings=settings)

    bars = [
        Bar("ETHUSDT", datetime(2026, 1, 1, 10, i, tzinfo=UTC),
            3000 + i, 3000 + i + 5, 3000 + i - 5, 3000 + i, 10000)
        for i in range(30)
    ]
    sig = Signal(
        direction=Direction.SHORT, strength=0.7,
        reasons=("statarb_z=2.5",),
        sl_atr_mult=None, tp_atr_mult=None,
        pair_tag="sa_BTC_ETH_test", strategy_name="scalp_statarb",
    )
    result = executor.compute_trade("ETHUSDT", sig, bars, available_balance=500.0)
    if result is not None:
        assert result.sl is None, "Stat-Arb не должен иметь SL"
        assert result.tp is None, "Stat-Arb не должен иметь TP"


def test_executor_floor_rounding():
    """Floor rounding: qty округляется вниз, не к ближайшему."""
    from bybit_bot.trading.executor import TradeExecutor
    from bybit_bot.trading.client import InstrumentInfo

    btc = InstrumentInfo("BTCUSDT", "Trading", 0.001, 0.001, 0.10, 5.0, 100.0)
    assert TradeExecutor._round_qty_api(0.0239, btc) == 0.023  # floor, not 0.024
    assert TradeExecutor._round_qty_api(0.0091, btc) == 0.009  # floor, not 0.009

    sol = InstrumentInfo("SOLUSDT", "Trading", 0.1, 0.1, 0.01, 5.0, 50.0)
    assert TradeExecutor._round_qty_api(1.99, sol) == 1.9  # floor, not 2.0


def test_sync_positions_on_startup(tmp_path):
    """При старте бот восстанавливает все позиции с биржи в БД."""
    from unittest.mock import MagicMock
    from bybit_bot.stats.store import StatsStore
    from bybit_bot.trading.client import PositionInfo
    from bybit_bot.app.main import _sync_positions_on_startup

    store = StatsStore(tmp_path / "sync_test.sqlite")

    mock_client = MagicMock()
    mock_client.get_positions.return_value = [
        PositionInfo(
            symbol="DOTUSDT", side="Buy", size="535.1",
            entry_price=1.1628, unrealised_pnl=38.42,
            leverage="5", position_idx=0,
        ),
        PositionInfo(
            symbol="ATOMUSDT", side="Buy", size="10",
            entry_price=5.0, unrealised_pnl=-1.64,
            leverage="5", position_idx=0,
        ),
    ]

    assert len(store.get_open_positions()) == 0
    _sync_positions_on_startup(mock_client, store)

    open_pos = store.get_open_positions()
    assert len(open_pos) == 2
    symbols = {p.symbol for p in open_pos}
    assert symbols == {"DOTUSDT", "ATOMUSDT"}
    assert all(p.strategy == "recovered" for p in open_pos)

    _sync_positions_on_startup(mock_client, store)
    assert len(store.get_open_positions()) == 2


def _make_exit_mocks(tmp_path, initial_positions, fresh_positions, fetch_realized_pnl_return=None):
    """Вспомогательная фабрика для тестов _process_exits.

    initial_positions / fresh_positions — что возвращает 1-й и 2-й вызовы get_positions.
    fetch_realized_pnl_return — что возвращает fetch_realized_pnl (None = пусто).
    """
    from unittest.mock import MagicMock
    from bybit_bot.stats.store import StatsStore
    from bybit_bot.trading.killswitch import KillSwitch, KillSwitchConfig

    store = StatsStore(tmp_path / "exit_test.sqlite")
    mock_client = MagicMock()
    mock_client.get_positions.side_effect = [initial_positions, fresh_positions]
    mock_client.fetch_realized_pnl.return_value = fetch_realized_pnl_return
    killswitch = KillSwitch(KillSwitchConfig(
        max_daily_loss_usd=100.0,
        max_drawdown_pct=50.0,
        max_positions=5,
    ))
    return store, mock_client, killswitch


def test_process_exits_race_guard_preserves_live_position(tmp_path):
    """API race-guard: если позиция исчезает в 1-м запросе и появляется во 2-м,
    бот не должен помечать её как sync_pending/orphan — это ложное срабатывание.

    Регрессия: до фикса 9/104 позиций baseline получали sync_orphan с pnl=0,
    хотя на бирже они оставались открытыми."""
    from bybit_bot.app.main import _process_exits
    from bybit_bot.config.settings import Settings
    from bybit_bot.trading.client import PositionInfo

    live_pos = PositionInfo(
        symbol="TIAUSDT", side="Buy", size="1090.3",
        entry_price=0.3911, unrealised_pnl=-1.5,
        leverage="5", position_idx=0,
    )

    store, client, ks = _make_exit_mocks(
        tmp_path,
        initial_positions=[],
        fresh_positions=[live_pos],
    )
    store.open_position(
        symbol="TIAUSDT", side="Buy", qty="1090.3",
        entry_price=0.3911, order_id="test_order_1",
        strategy="scalp_vwap",
    )
    settings = Settings(api_key="k", api_secret="s", _env_file=None)

    _process_exits(
        client=client, stats=store, killswitch=ks,
        settings=settings, bars_map={}, scalp_statarb=None,
    )

    open_after = store.get_open_positions()
    assert len(open_after) == 1, "Живая позиция должна остаться открытой"
    assert open_after[0].symbol == "TIAUSDT"
    assert open_after[0].close_reason is None
    assert client.get_positions.call_count == 2, "Ожидался повторный запрос (race guard)"
    client.fetch_realized_pnl.assert_not_called()


def test_process_exits_closes_truly_missing_position(tmp_path):
    """Если позиция отсутствует и в 1-м, и во 2-м запросе — это реальное закрытие
    на бирже. Бот должен подтянуть PnL из closed-pnl API и закрыть в БД."""
    from bybit_bot.app.main import _process_exits
    from bybit_bot.config.settings import Settings

    store, client, ks = _make_exit_mocks(
        tmp_path,
        initial_positions=[],
        fresh_positions=[],
        fetch_realized_pnl_return={"closedPnl": "-2.50", "avgExitPrice": "0.3850"},
    )
    store.open_position(
        symbol="TIAUSDT", side="Buy", qty="1090.3",
        entry_price=0.3911, order_id="test_order_1",
        strategy="scalp_vwap",
    )
    settings = Settings(api_key="k", api_secret="s", _env_file=None)

    _process_exits(
        client=client, stats=store, killswitch=ks,
        settings=settings, bars_map={}, scalp_statarb=None,
    )

    open_after = store.get_open_positions()
    assert len(open_after) == 0, "Позиция должна быть закрыта в БД"
    client.fetch_realized_pnl.assert_called_once()


def test_process_exits_pending_when_api_empty(tmp_path):
    """Позиция отсутствует в обоих запросах + closed-pnl API пусто → sync_pending
    (а не orphan сразу). Orphan выставится позже в _reconcile_pending_sync."""
    from bybit_bot.app.main import _process_exits
    from bybit_bot.config.settings import Settings

    store, client, ks = _make_exit_mocks(
        tmp_path,
        initial_positions=[],
        fresh_positions=[],
        fetch_realized_pnl_return=None,
    )
    store.open_position(
        symbol="TIAUSDT", side="Buy", qty="1090.3",
        entry_price=0.3911, order_id="test_order_1",
        strategy="scalp_vwap",
    )
    settings = Settings(api_key="k", api_secret="s", _env_file=None)

    _process_exits(
        client=client, stats=store, killswitch=ks,
        settings=settings, bars_map={}, scalp_statarb=None,
    )

    pending = store.get_pending_sync_positions(older_than_sec=0)
    assert len(pending) == 1
    assert pending[0].close_reason == "sync_pending"
    assert pending[0].pnl_usd == 0.0
