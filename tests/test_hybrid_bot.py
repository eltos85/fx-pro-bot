"""Тесты hybrid_bot: правила входа/фиксации, учёт своей позиции, изоляция.

Стратегия — STRATEGY_HYBRID.md §17.4, порог — §17.6. Сети и ключей тесты не
требуют: решение принимает чистая функция ``plan``, а деньги считает БД.
"""

from __future__ import annotations

import pytest

from hybrid_bot.app.main import _executed, bet_size, plan
from hybrid_bot.client import Fill, HybridClient
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


# ─── размер ставки и капитал ─────────────────────────────────────────────

def test_bet_is_limited_by_the_maximum():
    assert bet_size(position_usd=200.0, virtual_capital=1000.0,
                    open_notional=0.0) == pytest.approx(200.0)


def test_bet_is_limited_by_the_capital_left():
    assert bet_size(position_usd=200.0, virtual_capital=1000.0,
                    open_notional=900.0) == pytest.approx(100.0)


def test_no_bet_when_capital_is_spent():
    assert bet_size(position_usd=200.0, virtual_capital=1000.0,
                    open_notional=1000.0) == 0.0
    assert bet_size(position_usd=200.0, virtual_capital=1000.0,
                    open_notional=1500.0) == 0.0


def test_open_notional_counts_own_positions(tmp_path):
    db = HybridDB(str(tmp_path / "h.sqlite"))
    db.open_pos("ETHUSDT", "Buy", 0.1, 2000.0, "hybrid_a")
    db.open_pos("BTCUSDT", "Buy", 0.003, 60000.0, "hybrid_b")
    assert db.open_notional() == pytest.approx(200.0 + 180.0)
    # При входе по символу его прошлый объём не должен считаться дважды.
    assert db.open_notional(exclude="ETHUSDT") == pytest.approx(180.0)


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


def test_fees_are_subtracted_from_the_result(tmp_path):
    """Гейт §8.4: в базе должны лежать чистые деньги, а не валовые."""
    db = HybridDB(str(tmp_path / "h.sqlite"))
    db.open_pos("ETHUSDT", "Buy", 0.08, 2000.0, "hybrid_a", entry_fee=0.088)
    db.record_closed(db.owned("ETHUSDT"), exit_px=2120.0,
                     reason="fix_threshold", mode="live",
                     strategy="hybrid_fix_from_avg", exit_fee=0.093)
    row = db.closed_today("ETHUSDT")[0]
    assert row["pnl_usd"] == pytest.approx((2120.0 - 2000.0) * 0.08
                                           - 0.088 - 0.093)


def test_old_database_gets_the_fee_column(tmp_path):
    """Боевая БД уже существует — колонка комиссии должна добавляться на месте."""
    import sqlite3

    path = str(tmp_path / "h.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE positions (symbol TEXT PRIMARY KEY, side TEXT "
                "NOT NULL, qty REAL NOT NULL, avg_entry REAL NOT NULL, "
                "ts_open INTEGER NOT NULL, link_id TEXT NOT NULL, "
                "fixations INTEGER NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO positions VALUES ('ETHUSDT','Buy',0.08,2293.37,"
                "1787231724,'hybrid_live',0)")
    old.commit()
    old.close()

    db = HybridDB(path)
    pos = db.owned("ETHUSDT")
    assert pos is not None
    assert pos["qty"] == pytest.approx(0.08)
    assert pos["entry_fee"] == pytest.approx(0.0)


# ─── учёт по фактическому исполнению ─────────────────────────────────────

class _FakeClient:
    """Клиент, отдающий заранее заданные исполнения по orderLinkId."""

    def __init__(self, fill: Fill | None) -> None:
        self._fill = fill
        self.asked: list[str] = []

    def fill_of(self, link_id: str, **_kwargs) -> Fill | None:
        self.asked.append(link_id)
        return self._fill


def test_live_accounting_uses_the_actual_execution():
    cfg = HybridSettings(trading_enabled=True)
    client = _FakeClient(Fill(qty=0.08, price=2295.10, fee=0.101))
    qty, px, fee = _executed(cfg, client, "hybrid_a", "ETHUSDT", 0.08, 2293.37)
    assert (qty, px, fee) == (0.08, 2295.10, 0.101)
    assert client.asked == ["hybrid_a"]


def test_observation_mode_estimates_the_taker_fee():
    cfg = HybridSettings(trading_enabled=False)
    qty, px, fee = _executed(cfg, _FakeClient(None), "hybrid_a", "ETHUSDT",
                             0.08, 2000.0)
    assert (qty, px) == (0.08, 2000.0)
    assert fee == pytest.approx(0.08 * 2000.0 * cfg.taker_fee)


def test_unreadable_execution_falls_back_to_the_requested_price():
    """Биржа не отдала исполнение — цена заявки, комиссия по taker, не ноль."""
    cfg = HybridSettings(trading_enabled=True)
    qty, px, fee = _executed(cfg, _FakeClient(None), "hybrid_a", "ETHUSDT",
                             0.08, 2293.37)
    assert (qty, px) == (0.08, 2293.37)
    assert fee == pytest.approx(0.08 * 2293.37 * cfg.taker_fee)


def test_fill_is_volume_weighted_and_skips_funding():
    """Рыночный ордер может исполниться частями; funding — не сделка."""
    rows = [
        {"execType": "Trade", "execQty": "0.05", "execPrice": "2000",
         "execFee": "0.055"},
        {"execType": "Trade", "execQty": "0.03", "execPrice": "2100",
         "execFee": "0.035"},
        {"execType": "Funding", "execQty": "0.08", "execPrice": "2050",
         "execFee": "0.9"},
    ]
    client = HybridClient.__new__(HybridClient)
    client._category = "linear"
    client._session = type("S", (), {
        "get_executions": lambda self=None, **kw: {
            "result": {"list": rows}},
    })()
    fill = client.fill_of("hybrid_a")
    assert fill is not None
    assert fill.qty == pytest.approx(0.08)
    assert fill.price == pytest.approx((0.05 * 2000 + 0.03 * 2100) / 0.08)
    assert fill.fee == pytest.approx(0.09)


def test_fill_of_asks_order_id_first_then_link_id():
    """Номер ордера старше нашей метки — так сказано в доке execution/list."""
    seen: list[dict] = []

    def get_executions(**kw):
        seen.append({k: kw[k] for k in ("orderId", "orderLinkId") if k in kw})
        if "orderId" in kw:
            return {"result": {"list": []}}
        return {"result": {"list": [
            {"execType": "Trade", "execQty": "0.08", "execPrice": "2300",
             "execFee": "0.1"},
        ]}}

    client = HybridClient.__new__(HybridClient)
    client._category = "linear"
    client._session = type("S", (), {"get_executions": staticmethod(get_executions)})()
    fill = client.fill_of("hybrid_a", order_id="oid-1", attempts=1)
    assert fill is not None and fill.price == pytest.approx(2300.0)
    assert seen[0] == {"orderId": "oid-1"}
    assert seen[1] == {"orderLinkId": "hybrid_a"}


def test_fill_of_retries_until_the_history_catches_up(monkeypatch):
    """REST отстаёт от ордера — несколько пустых ответов, потом сделка."""
    calls = {"n": 0}
    monkeypatch.setattr("hybrid_bot.client.time.sleep", lambda _s: None)

    def get_executions(**_kw):
        calls["n"] += 1
        if calls["n"] < 4:
            return {"result": {"list": []}}
        return {"result": {"list": [
            {"execType": "Trade", "execQty": "0.08", "execPrice": "2310",
             "execFee": "0.1"},
        ]}}

    client = HybridClient.__new__(HybridClient)
    client._category = "linear"
    client._session = type("S", (), {"get_executions": staticmethod(get_executions)})()
    fill = client.fill_of("hybrid_a", attempts=8, delay_sec=0.0)
    assert fill is not None and fill.price == pytest.approx(2310.0)
    assert calls["n"] == 4


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
    # Порог — серединное расстояние наблюдённых закрытий (§17.3), а не оптимум
    # по итогу (§17.6). Воспроизведение механизма, не подгонка под лучший P&L.
    assert cfg.fix_threshold_pct == pytest.approx(1.0)
    assert cfg.virtual_capital == pytest.approx(1000.0)
    assert cfg.position_usd == pytest.approx(200.0)
    assert cfg.link_prefix == "hybrid_"
