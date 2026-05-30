"""Юнит-тесты scalp_bot: orderflow-сигналы, агрегаты, sizing, killswitch.

Все цели — чистая детерминированная логика (без сети/WS/биржи).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scalp_bot.analysis.signals import (
    cvd_divergence,
    detect_sweep,
    evaluate,
    funding_supportive,
    liq_flush,
    ob_supportive,
)
from scalp_bot.data.aggregates import CvdSample, LiqEvent, SymbolSnapshot, SymbolState
from scalp_bot.safety import killswitch
from scalp_bot.trading.executor import paper_pnl, position_size


# ─── helpers ─────────────────────────────────────────────────────────────

def _cfg(**over):
    base = dict(
        min_confluence=3, liq_flush_usd=50000.0, funding_extreme=0.0003,
        ob_imbalance_min=0.58, take_profit_r=1.5, sl_buffer_bps=8.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _long_samples():
    """Bull-дивергенция + sweep: late делает price lower-low, cvd higher-low."""
    early = [CvdSample(1, 100, -1), CvdSample(2, 99, -3), CvdSample(3, 98, -5)]
    late = [CvdSample(4, 97, -4), CvdSample(5, 96.5, -2), CvdSample(6, 97.5, -1)]
    return early + late


def _short_samples():
    """Bear-дивергенция + sweep: late делает price higher-high, cvd lower-high."""
    early = [CvdSample(1, 100, 1), CvdSample(2, 101, 3), CvdSample(3, 102, 5)]
    late = [CvdSample(4, 103, 4), CvdSample(5, 103.5, 2), CvdSample(6, 102.5, 1)]
    return early + late


# ─── sweep / divergence ────────────────────────────────────────────────────

def test_detect_sweep_long_true():
    assert detect_sweep(_long_samples(), "long") is True


def test_detect_sweep_short_true():
    assert detect_sweep(_short_samples(), "short") is True


def test_cvd_divergence_long_true():
    assert cvd_divergence(_long_samples(), "long") is True


def test_cvd_divergence_short_true():
    assert cvd_divergence(_short_samples(), "short") is True


def test_cvd_divergence_false_when_cvd_follows_price():
    # цена ниже И cvd ниже → нет дивергенции (нет поглощения)
    s = [CvdSample(1, 100, 0), CvdSample(2, 99, -1), CvdSample(3, 98, -2),
         CvdSample(4, 97, -5), CvdSample(5, 96, -7), CvdSample(6, 95, -9)]
    assert cvd_divergence(s, "long") is False


def test_split_too_few_samples():
    assert detect_sweep([CvdSample(1, 100, 0)], "long") is False
    assert cvd_divergence([CvdSample(1, 100, 0)], "short") is False


# ─── liquidations ────────────────────────────────────────────────────────

def test_liq_flush_long_counts_sell_side():
    liqs = [LiqEvent(1, "Sell", 30000, 100), LiqEvent(2, "Sell", 30000, 100),
            LiqEvent(3, "Buy", 99999, 100)]
    assert liq_flush(liqs, "long", 50000) is True
    # для short считаем Buy-ликвидации — тут одна на 99999
    assert liq_flush(liqs, "short", 50000) is True
    assert liq_flush(liqs, "short", 200000) is False


def test_liq_flush_below_threshold():
    assert liq_flush([LiqEvent(1, "Sell", 10000, 100)], "long", 50000) is False


# ─── funding / orderbook ───────────────────────────────────────────────────

def test_funding_supportive_long_needs_negative():
    assert funding_supportive(-0.0004, "long", 0.0003) is True
    assert funding_supportive(0.0004, "long", 0.0003) is False
    assert funding_supportive(0.0004, "short", 0.0003) is True
    assert funding_supportive(None, "long", 0.0003) is False


def test_ob_supportive():
    assert ob_supportive(0.60, "long", 0.58) is True
    assert ob_supportive(0.40, "short", 0.58) is True
    assert ob_supportive(0.50, "long", 0.58) is False
    assert ob_supportive(None, "long", 0.58) is False


# ─── evaluate (интеграция правил) ──────────────────────────────────────────

def _snap(samples, **over):
    base = dict(
        symbol="SOLUSDT", ts=10.0, last_price=97.0, best_bid=96.9, best_ask=97.1,
        ob_imbalance=0.62, funding_rate=-0.0005,
        open_interest=1.0, cvd_samples=samples,
        liq_events=[LiqEvent(1, "Sell", 60000, 97)], stale=False,
    )
    base.update(over)
    return SymbolSnapshot(**base)


def test_evaluate_long_signal_full_confluence():
    sig = evaluate(_snap(_long_samples()), _cfg())
    assert sig is not None
    assert sig.side == "long"
    assert sig.score >= 3
    assert "cvd_div" in sig.reasons
    # SL ниже свипнутого лоя, TP выше входа
    assert sig.sl_level < min(s.price for s in _long_samples())
    assert sig.tp_level > sig.entry_ref


def test_evaluate_none_when_no_divergence():
    # цена+cvd падают вместе → div=False → правило обязательное не выполнено
    s = [CvdSample(1, 100, 0), CvdSample(2, 99, -1), CvdSample(3, 98, -2),
         CvdSample(4, 97, -5), CvdSample(5, 96, -7), CvdSample(6, 95, -9)]
    assert evaluate(_snap(s, funding_rate=-0.0005), _cfg()) is None


def test_evaluate_none_when_stale():
    assert evaluate(_snap(_long_samples(), stale=True), _cfg()) is None


def test_evaluate_below_confluence_returns_none():
    # убираем поддержку funding+ob+liq → остаётся sweep+cvd = 2 < 3
    snap = _snap(_long_samples(), funding_rate=0.0, ob_imbalance=0.50,
                 liq_events=[])
    assert evaluate(snap, _cfg(min_confluence=3)) is None


# ─── aggregates (SymbolState) ──────────────────────────────────────────────

def test_symbolstate_cvd_accumulates_signed():
    clock = {"t": 0.0}
    st = SymbolState("BTCUSDT", now=lambda: clock["t"])
    st.on_trade(100.0, 2.0, "Buy")
    st.on_trade(100.0, 1.0, "Sell")
    snap = st.snapshot()
    assert snap.cvd_samples[-1].cvd == pytest.approx(1.0)  # +2 -1
    assert snap.last_price == 100.0


def test_symbolstate_orderbook_imbalance():
    st = SymbolState("BTCUSDT", ob_levels=2)
    st.on_orderbook(bids=[(100, 6.0), (99, 2.0)], asks=[(101, 1.0), (102, 1.0)])
    snap = st.snapshot()
    # bid_vol=8, ask_vol=2 → 8/10 = 0.8
    assert snap.ob_imbalance == pytest.approx(0.8)
    assert snap.best_bid == 100
    assert snap.best_ask == 101


def test_symbolstate_evicts_old_samples():
    clock = {"t": 0.0}
    st = SymbolState("BTCUSDT", cvd_window_sec=10.0, now=lambda: clock["t"])
    st.on_trade(100, 1, "Buy")
    clock["t"] = 100.0
    st.on_trade(101, 1, "Buy")
    snap = st.snapshot()
    assert len(snap.cvd_samples) == 1  # старый сэмпл вытеснен


def test_symbolstate_stale_flag():
    clock = {"t": 0.0}
    st = SymbolState("BTCUSDT", max_age_sec=5.0, now=lambda: clock["t"])
    st.on_trade(100, 1, "Buy")
    clock["t"] = 100.0
    assert st.snapshot().stale is True


# ─── position sizing / pnl ─────────────────────────────────────────────────

def test_position_size_from_notional():
    assert position_size(100.0, 100.0) == pytest.approx(1.0)


def test_position_size_floors_to_min_notional():
    # целевой $5 < min $10 → берём $10 notional
    assert position_size(5.0, 100.0, min_notional=10.0) == pytest.approx(0.1)


def test_position_size_rounds_down_to_step():
    qty = position_size(100.0, 100.0, qty_step=0.3)
    assert qty == pytest.approx(0.9)  # floor(1.0/0.3)=3 → 0.9


def test_position_size_below_exchange_min_uses_min_qty():
    # наш лот мельче биржевого минимума → берём биржевой минимум
    assert position_size(1.0, 100.0, min_qty=0.5) == pytest.approx(0.5)


def test_position_size_zero_entry():
    assert position_size(100.0, 0.0) == 0.0


def test_paper_pnl_long_includes_fees():
    net, fees = paper_pnl("long", 100.0, 101.0, 5.0)
    assert fees == pytest.approx(5 * (100 * 0.0002 + 101 * 0.00055))
    assert net == pytest.approx(5.0 - fees)


def test_paper_pnl_short():
    net, _ = paper_pnl("short", 100.0, 99.0, 5.0)
    assert net > 0


# ─── killswitch ────────────────────────────────────────────────────────────

class _FakeDB:
    def __init__(self, day=0.0, total=0.0, open_n=0, hour_trades=0):
        self._day, self._total = day, total
        self._open, self._hour = open_n, hour_trades

    def realized_pnl_since(self, ts):  # noqa: ARG002
        return self._day

    def total_realized_pnl(self):
        return self._total

    def open_count(self):
        return self._open

    def trades_since(self, ts):  # noqa: ARG002
        return self._hour


def _ks_cfg(**over):
    base = dict(max_daily_loss_usd=50.0, max_total_loss_usd=150.0,
                max_open_positions=2, max_trades_per_hour=20)
    base.update(over)
    return SimpleNamespace(**base)


def test_is_killed_daily_loss():
    d = killswitch.is_killed(_FakeDB(day=-50.0), _ks_cfg(), now=1000.0)
    assert d.allowed is False


def test_is_killed_total_loss():
    d = killswitch.is_killed(_FakeDB(total=-150.0), _ks_cfg(), now=1000.0)
    assert d.allowed is False


def test_can_open_blocks_on_max_positions():
    d = killswitch.can_open(_FakeDB(open_n=2), _ks_cfg(), now=1000.0)
    assert d.allowed is False
    assert "open positions" in d.reason


def test_can_open_blocks_on_rate_limit():
    d = killswitch.can_open(_FakeDB(hour_trades=20), _ks_cfg(), now=1000.0)
    assert d.allowed is False
    assert "rate-limit" in d.reason


def test_can_open_ok():
    assert killswitch.can_open(_FakeDB(), _ks_cfg(), now=1000.0).allowed is True
