"""Тесты hybrid_bot: правила входа/фиксации, учёт своей позиции, изоляция.

Стратегия — STRATEGY_HYBRID.md §17.4, порог — §17.6. Сети и ключей тесты не
требуют: решение принимает чистая функция ``plan``, а деньги считает БД.
"""

from __future__ import annotations

import pytest

from hybrid_bot.app.main import plan
from hybrid_bot.db import HybridDB
from hybrid_bot.settings import HybridSettings
from hybrid_bot.signals import (distance_pct, fix_price, should_fix,
                                trend_long)


# ─── правило тренда ──────────────────────────────────────────────────────

def test_trend_needs_fifty_bars():
    assert trend_long([100.0] * 49) is None
    assert trend_long([100.0] * 50) == 0


def test_trend_long_when_fast_average_is_above_slow():
    closes = [100.0] * 40 + [130.0] * 20
    assert trend_long(closes) == 1


def test_trend_flat_when_fast_average_is_below_slow():
    closes = [130.0] * 40 + [100.0] * 20
    assert trend_long(closes) == 0


# ─── порог фиксации ──────────────────────────────────────────────────────

def test_fix_price_is_average_entry_plus_threshold():
    assert fix_price(2000.0, 6.0) == pytest.approx(2120.0)


def test_should_fix_only_at_or_above_the_level():
    assert not should_fix(2119.99, 2000.0, 6.0)
    assert should_fix(2120.0, 2000.0, 6.0)
    assert should_fix(2500.0, 2000.0, 6.0)


def test_should_fix_ignores_broken_input():
    assert not should_fix(0.0, 2000.0, 6.0)
    assert not should_fix(2500.0, 0.0, 6.0)


def test_distance_is_measured_from_average_entry():
    assert distance_pct(2120.0, 2000.0) == pytest.approx(6.0)


# ─── решение по символу ──────────────────────────────────────────────────

def _pos(avg: float = 2000.0, qty: float = 3.5) -> dict:
    return {"symbol": "ETHUSDT", "side": "Buy", "qty": qty, "avg_entry": avg,
            "ts_open": 0, "link_id": "hybrid_x", "fixations": 0}


def test_opens_when_trend_is_up_and_nothing_owned():
    act = plan(want=1, owned=None, last_price=2000.0, broker_size=0.0,
               threshold_pct=6.0)
    assert act["action"] == "open"


def test_stays_out_when_trend_is_down():
    act = plan(want=0, owned=None, last_price=2000.0, broker_size=0.0,
               threshold_pct=6.0)
    assert act["action"] == "stay_out"


def test_foreign_position_is_never_touched():
    """Чужой лот на общем счёте — символ пропускается целиком."""
    act = plan(want=1, owned=None, last_price=2000.0, broker_size=5.0,
               threshold_pct=6.0)
    assert act["action"] == "skip_foreign"
    assert act["size"] == 5.0


def test_missing_position_on_exchange_triggers_resync():
    act = plan(want=1, owned=_pos(), last_price=2000.0, broker_size=0.0,
               threshold_pct=6.0)
    assert act["action"] == "resync"


def test_observe_mode_does_not_compare_with_exchange():
    """broker_size=None — своих ордеров нет, сверять нечего."""
    act = plan(want=1, owned=_pos(), last_price=2000.0, broker_size=None,
               threshold_pct=6.0)
    assert act["action"] == "hold"


def test_fixes_when_price_reached_the_level():
    act = plan(want=1, owned=_pos(avg=2000.0), last_price=2130.0,
               broker_size=3.5, threshold_pct=6.0)
    assert act["action"] == "fix"
    assert act["price"] == pytest.approx(2120.0)


def test_holds_below_the_level_and_reports_distance():
    act = plan(want=1, owned=_pos(avg=2000.0), last_price=2080.0,
               broker_size=3.5, threshold_pct=6.0)
    assert act["action"] == "hold"
    assert act["distance_pct"] == pytest.approx(4.0)


def test_trend_exit_wins_over_threshold():
    """Разворот тренда закрывает позицию, даже если порог достигнут."""
    act = plan(want=0, owned=_pos(avg=2000.0), last_price=2500.0,
               broker_size=3.5, threshold_pct=6.0)
    assert act["action"] == "exit"


def test_no_price_stops_the_symbol():
    act = plan(want=1, owned=None, last_price=0.0, broker_size=0.0,
               threshold_pct=6.0)
    assert act["action"] == "no_price"


def test_not_enough_bars_stops_the_symbol():
    act = plan(want=None, owned=None, last_price=2000.0, broker_size=0.0,
               threshold_pct=6.0)
    assert act["action"] == "no_data"


# ─── БД: своя позиция и деньги ───────────────────────────────────────────

def test_money_is_counted_from_average_entry(tmp_path):
    db = HybridDB(str(tmp_path / "h.sqlite"))
    db.open_pos("ETHUSDT", "Buy", 3.5, 2000.0, "hybrid_a")
    pos = db.owned("ETHUSDT")
    db.record_closed(pos, exit_px=2120.0, reason="fix_threshold",
                     mode="paper", strategy="hybrid_fix_from_avg")
    rows = db.closed_today("ETHUSDT")
    assert len(rows) == 1
    assert rows[0]["pnl_usd"] == pytest.approx((2120.0 - 2000.0) * 3.5)
    assert rows[0]["close_reason"] == "fix_threshold"


def test_reentry_resets_average_and_counts_fixations(tmp_path):
    db = HybridDB(str(tmp_path / "h.sqlite"))
    db.open_pos("ETHUSDT", "Buy", 3.5, 2000.0, "hybrid_a")
    db.drop_pos("ETHUSDT")
    db.open_pos("ETHUSDT", "Buy", 3.3, 2120.0, "hybrid_b", fixations=1)
    pos = db.owned("ETHUSDT")
    assert pos["avg_entry"] == pytest.approx(2120.0)
    assert pos["fixations"] == 1


def test_closed_position_is_gone_from_the_book(tmp_path):
    db = HybridDB(str(tmp_path / "h.sqlite"))
    db.open_pos("ETHUSDT", "Buy", 3.5, 2000.0, "hybrid_a")
    db.drop_pos("ETHUSDT")
    assert db.owned("ETHUSDT") is None


def test_trades_table_matches_tradecard_reader(tmp_path):
    """tradecard читает БД тем же загрузчиком — схема должна совпадать."""
    from tradecard_bybit.data.bot_db import BotDBReadOnly

    path = str(tmp_path / "hybrid_bot.sqlite")
    db = HybridDB(path)
    db.open_pos("ETHUSDT", "Buy", 3.5, 2000.0, "hybrid_a")
    db.record_closed(db.owned("ETHUSDT"), exit_px=2120.0,
                     reason="fix_threshold", mode="live",
                     strategy="hybrid_fix_from_avg")
    with BotDBReadOnly(path, "hybrid") as reader:
        trades = reader.closed_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t.bot == "hybrid" and t.side == "long"
    assert t.pnl_usd == pytest.approx(420.0)
    # Стопа у стратегии нет, поэтому R не определён, а не бессмысленно велик.
    assert t.planned_risk_usd is None


# ─── настройки ───────────────────────────────────────────────────────────

def test_trading_is_off_until_explicitly_enabled():
    cfg = HybridSettings()
    assert cfg.trading_enabled is False
    assert cfg.trade_mode == "paper"


def test_defaults_come_from_the_measurement():
    cfg = HybridSettings()
    assert cfg.symbol_list == ["ETHUSDT"]
    assert cfg.interval == "240"
    assert cfg.fix_threshold_pct == pytest.approx(6.0)
    assert cfg.position_usd == pytest.approx(7000.0)
    assert cfg.link_prefix == "hybrid_"
