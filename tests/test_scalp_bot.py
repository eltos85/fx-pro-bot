"""Юнит-тесты scalp_bot: orderflow-сигналы, агрегаты, sizing, killswitch.

Все цели — чистая детерминированная логика (без сети/WS/биржи).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scalp_bot.analysis.signals import (
    SweepReclaimDetector,
    build_signal,
    cvd_divergence,
    detect_sweep,
    diagnose,
    evaluate,
    flow_invalidated,
    funding_supportive,
    liq_flush,
    ob_supportive,
    reclaimed,
    reversal_momentum,
)
from scalp_bot.data.aggregates import CvdSample, LiqEvent, SymbolSnapshot, SymbolState
from scalp_bot.safety import killswitch
from scalp_bot.trading.executor import Executor, paper_pnl, position_size, taker_pnl


# ─── helpers ─────────────────────────────────────────────────────────────

def _cfg(**over):
    base = dict(
        min_confluence=3, liq_flush_usd=50000.0,
        funding_extreme_pos=0.0005, funding_extreme_neg=0.0003,
        ob_imbalance_min=0.58, take_profit_r=2.0, sl_buffer_bps=8.0,
        require_reclaim=True, reclaim_frac=0.5, momentum_window_sec=3.0,
        round_trip_fee_frac=0.00075, min_target_fee_mult=3.0,
        div_min_late_trades=2, arm_timeout_sec=60.0,
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


def test_cvd_divergence_strict_rejects_flat_cvd():
    # цена ниже, но CVD РОВНО равен (плоско) → строгое > → не дивергенция
    early = [CvdSample(1, 100, -5), CvdSample(2, 99, -3), CvdSample(3, 98, -5)]
    late = [CvdSample(4, 97, -5), CvdSample(5, 96.5, -4), CvdSample(6, 97.5, -5)]
    # min(late.cvd)=-5 == min(early.cvd)=-5 → не строго больше → False
    assert cvd_divergence(early + late, "long") is False


def test_cvd_divergence_min_late_activity_filter():
    # поздняя половина из 3 сделок, но требуем ≥5 → отсев «пустоты»
    assert cvd_divergence(_long_samples(), "long", min_late=5) is False
    assert cvd_divergence(_long_samples(), "long", min_late=2) is True


def test_split_too_few_samples():
    assert detect_sweep([CvdSample(1, 100, 0)], "long") is False
    assert cvd_divergence([CvdSample(1, 100, 0)], "short") is False


# ─── liquidations ────────────────────────────────────────────────────────

def test_liq_flush_long_counts_buy_side():
    # Bybit S="Buy" = ликвидирован ЛОНГ (forced sell) → fade лонгом
    liqs = [LiqEvent(1, "Buy", 30000, 100), LiqEvent(2, "Buy", 30000, 100),
            LiqEvent(3, "Sell", 99999, 100)]
    assert liq_flush(liqs, "long", 50000) is True   # Buy sum=60000
    # short считает Sell-ликвидации (ликвидирован шорт, forced buy)
    assert liq_flush(liqs, "short", 50000) is True  # Sell sum=99999
    assert liq_flush(liqs, "long", 200000) is False


def test_liq_flush_below_threshold():
    assert liq_flush([LiqEvent(1, "Buy", 10000, 100)], "long", 50000) is False


# ─── funding / orderbook ───────────────────────────────────────────────────

def test_funding_supportive_asymmetric():
    # long требует funding ≤ −neg(0.0003); short требует funding ≥ +pos(0.0005)
    assert funding_supportive(-0.0004, "long", 0.0005, 0.0003) is True
    assert funding_supportive(-0.0002, "long", 0.0005, 0.0003) is False
    assert funding_supportive(0.0006, "short", 0.0005, 0.0003) is True
    assert funding_supportive(0.0004, "short", 0.0005, 0.0003) is False  # < pos-порог
    assert funding_supportive(None, "long", 0.0005, 0.0003) is False


def test_ob_supportive():
    assert ob_supportive(0.60, "long", 0.58) is True
    assert ob_supportive(0.40, "short", 0.58) is True
    assert ob_supportive(0.50, "long", 0.58) is False
    assert ob_supportive(None, "long", 0.58) is False


# ─── reclaim / momentum / flow invalidation ────────────────────────────────

def test_reclaimed_long_true_when_price_returns():
    # свип вниз до 96.5, цена вернулась к 97.5 (>50% пути к 98) → reclaim
    assert reclaimed(_long_samples(), "long", 0.5) is True


def test_reclaimed_long_false_when_price_stays_low():
    early = [CvdSample(1, 100, -1), CvdSample(2, 99, -3), CvdSample(3, 98, -5)]
    late = [CvdSample(4, 97, -4), CvdSample(5, 96.5, -2), CvdSample(6, 96.4, -1)]
    assert reclaimed(early + late, "long", 0.5) is False  # last 96.4, нужно ≥97.2


def test_reversal_momentum_long_true_when_cvd_rising():
    # окно 3с: cvd последних сэмплов растёт (−5→−1)
    assert reversal_momentum(_long_samples(), "long", 3.0) is True


def test_reversal_momentum_long_false_when_cvd_falling():
    s = [CvdSample(4, 97, -1), CvdSample(5, 96.5, -3), CvdSample(6, 96.4, -5)]
    assert reversal_momentum(s, "long", 3.0) is False


def test_flow_invalidated_long_when_cvd_turns_down():
    # лента качнулась в short (CVD падает) → лонг инвалидирован
    s = [CvdSample(4, 97, -1), CvdSample(5, 96.5, -3), CvdSample(6, 96.4, -6)]
    snap = _snap(s)
    assert flow_invalidated(snap, "long", 3.0) is True
    assert flow_invalidated(snap, "short", 3.0) is False


# ─── evaluate (интеграция правил) ──────────────────────────────────────────

def _snap(samples, **over):
    base = dict(
        symbol="SOLUSDT", ts=10.0, last_price=97.0, best_bid=96.9, best_ask=97.1,
        ob_imbalance=0.62, funding_rate=-0.0005,
        open_interest=1.0, cvd_samples=samples,
        liq_events=[LiqEvent(1, "Buy", 60000, 97)], stale=False,
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


def test_evaluate_none_when_no_reclaim():
    # require_reclaim=True, но цена осталась на свип-лоях → нет reclaim → None
    early = [CvdSample(1, 100, -1), CvdSample(2, 99, -3), CvdSample(3, 98, -5)]
    late = [CvdSample(4, 97, -4), CvdSample(5, 96.5, -2), CvdSample(6, 96.4, -1)]
    snap = _snap(early + late, last_price=96.4)
    assert evaluate(snap, _cfg()) is None


def test_evaluate_fee_guard_blocks_tiny_target():
    # завышаем требуемый множитель → ход до TP < min → сигнал отброшен
    assert evaluate(_snap(_long_samples()), _cfg(min_target_fee_mult=1000.0)) is None


def test_evaluate_passes_with_require_reclaim_off():
    # отключив подтверждение разворота, сигнал проходит на голом конфлюенсе
    sig = evaluate(_snap(_long_samples()), _cfg(require_reclaim=False))
    assert sig is not None and sig.side == "long"


def test_evaluate_sl_from_recent_sweep_half():
    # SL берётся по экстремуму поздней половины (свежий свип), не глоб. минимуму
    sig = evaluate(_snap(_long_samples()), _cfg())
    late_min = min(96.5, 97.0, 97.5)  # поздняя половина _long_samples
    assert sig is not None
    assert sig.sl_level == pytest.approx(late_min * (1 - 8 / 1e4))


def test_diagnose_reports_rule_states():
    d = diagnose(_snap(_long_samples()), _cfg())
    assert d is not None
    assert d["side"] == "long"
    assert d["div"] is True and d["sweep"] is True
    assert d["signal"] is True
    assert d["score"] >= 3


def test_diagnose_none_when_stale():
    assert diagnose(_snap(_long_samples(), stale=True), _cfg()) is None


def test_build_signal_maker_uses_own_book_side():
    # snap: best_bid=96.9, best_ask=97.1
    snap = _snap(_long_samples())
    # post-only LONG → мейкер по best_bid (не пересекает спред → не отменится)
    s = build_signal(snap, "long", 96.5, _cfg(entry_order_type="post_only_limit"), 3, ["x"])
    assert s is not None and s.entry_ref == pytest.approx(96.9)
    # post-only SHORT → мейкер по best_ask
    s2 = build_signal(snap, "short", 98.0, _cfg(entry_order_type="post_only_limit"), 3, ["x"])
    assert s2 is not None and s2.entry_ref == pytest.approx(97.1)
    # market LONG → тейкер-референс best_ask
    s3 = build_signal(snap, "long", 96.5, _cfg(entry_order_type="market"), 3, ["x"])
    assert s3 is not None and s3.entry_ref == pytest.approx(97.1)


# ─── двухфазный детектор (взвод → выстрел) ─────────────────────────────────

def _arm_samples():
    """Свип+дивергенция, но цена осталась на лоях (reclaim ещё нет)."""
    early = [CvdSample(1, 100, -1), CvdSample(2, 99, -3), CvdSample(3, 98, -5)]
    late = [CvdSample(4, 97, -4), CvdSample(5, 96.5, -2), CvdSample(6, 96.5, -1)]
    return early + late


def _fire_samples():
    """Цена вернулась наверх, CVD растёт, нового свипа нет."""
    return [CvdSample(10, 97.4, -3), CvdSample(11, 97.45, -2),
            CvdSample(12, 97.5, -1), CvdSample(13, 97.5, 0),
            CvdSample(14, 97.55, 1), CvdSample(15, 97.6, 2)]


def test_detector_arms_then_fires_two_phase():
    det = SweepReclaimDetector("SOLUSDT", _cfg())
    # фаза 1: взвод без выстрела (нет reclaim)
    assert det.update(_snap(_arm_samples(), last_price=96.5), now=100.0) is None
    assert det.armed is True
    # фаза 2: reclaim + разворот CVD → вход
    sig = det.update(_snap(_fire_samples(), last_price=97.6), now=130.0)
    assert sig is not None and sig.side == "long"
    assert "reclaim" in sig.reasons and "mom" in sig.reasons
    assert det.armed is False  # разоружился после входа


def test_detector_no_fire_without_reclaim():
    det = SweepReclaimDetector("SOLUSDT", _cfg())
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)
    assert det.armed is True
    # цена так и осталась внизу → выстрела нет, но взвод держится
    assert det.update(_snap(_arm_samples(), last_price=96.5), now=110.0) is None
    assert det.armed is True


def test_detector_arm_expires_after_timeout():
    det = SweepReclaimDetector("SOLUSDT", _cfg(arm_timeout_sec=10.0))
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)
    assert det.armed is True
    flat = [CvdSample(t, 96.5, 0) for t in range(120, 126)]
    assert det.update(_snap(flat, last_price=96.5), now=125.0) is None
    assert det.armed is False  # взвод истёк по таймауту


def test_detector_reset_clears_state():
    det = SweepReclaimDetector("SOLUSDT", _cfg())
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)
    assert det.armed is True
    det.reset()
    assert det.armed is False


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


def test_position_size_no_float_artifact():
    # регресс: $100 @82.42, step 0.1 → 1.2 (а НЕ 1.2000000000000002 → ErrCode 10001)
    qty = position_size(100.0, 82.42, qty_step=0.1, min_qty=0.1)
    assert qty == 1.2
    assert str(qty) == "1.2"


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


def test_taker_pnl_estimate():
    # обе ноги taker: gross − qty*(entry+exit)*TAKER
    assert taker_pnl("long", 100.0, 101.0, 5.0) == pytest.approx(5.0 - 5 * 201 * 0.00055)
    assert taker_pnl("short", 100.0, 99.0, 5.0) == pytest.approx(5.0 - 5 * 199 * 0.00055)


def test_realized_or_estimate_falls_back_when_closedpnl_none():
    # killswitch не должен «ослепнуть»: при closedPnl=None берём оценку по цене
    fake_client = SimpleNamespace(last_closed_pnl=lambda sym, pref: None)
    ex = Executor(db=None, settings=SimpleNamespace(), client=fake_client)
    tr = SimpleNamespace(id=1, symbol="ETHUSDT", side="long", entry=2000.0, qty=0.04)
    pnl = ex._realized_or_estimate(tr, 1990.0)  # убыток ~ −0.4 минус комиссии
    assert pnl == pytest.approx(taker_pnl("long", 2000.0, 1990.0, 0.04))
    assert pnl < 0  # убыток реально записывается, а не 0


def test_realized_or_estimate_uses_exchange_pnl_when_available():
    fake_client = SimpleNamespace(last_closed_pnl=lambda sym, pref: -3.21)
    ex = Executor(db=None, settings=SimpleNamespace(), client=fake_client)
    tr = SimpleNamespace(id=2, symbol="BTCUSDT", side="short", entry=70000.0, qty=0.001)
    assert ex._realized_or_estimate(tr, 70100.0) == pytest.approx(-3.21)


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


# ─── telegram notifier ─────────────────────────────────────────────────────

def test_notifier_inactive_without_token():
    from scalp_bot.telegram.notifier import TelegramNotifier
    n = TelegramNotifier("", "", enabled=True)
    assert n.active is False
    n.send("hi")  # no-op, не должно бросать/ходить в сеть


def test_notifier_inactive_when_disabled():
    from scalp_bot.telegram.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat", enabled=False)
    assert n.active is False
    n.send("hi")
