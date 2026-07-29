"""Юнит-тесты scalp_bot: orderflow-сигналы, агрегаты, sizing, killswitch.

Все цели — чистая детерминированная логика (без сети/WS/биржи).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scalp_bot.analysis.counterfactual import (
    CounterfactualCandidate,
    CounterfactualTracker,
    advance_counterfactual,
)
from scalp_bot.analysis.meta_labels import breakout_fuel, fade_exhaustion
from scalp_bot.analysis.signals import (
    Signal,
    SweepReclaimDetector,
    build_signal,
    cvd_divergence,
    detect_sweep,
    diagnose,
    flow_invalidated,
    ob_supportive,
    reclaimed,
    reversal_momentum,
)
from scalp_bot.analysis.regime import compute_regime_features
from scalp_bot.data.aggregates import CvdSample, LiqEvent, SymbolSnapshot, SymbolState
from scalp_bot.safety import killswitch
from scalp_bot.trading.executor import (
    Executor, advance_maker_nonfill_shadow, bracket_exit_reason, paper_pnl, position_size,
    position_size_by_risk, reconciled_bracket_reason, taker_pnl,
)


# ─── helpers ─────────────────────────────────────────────────────────────

def _cfg(**over):
    base = dict(
        ob_imbalance_min=0.58, take_profit_r=2.0, sl_buffer_bps=8.0,
        require_reclaim=True, reclaim_frac=0.5, momentum_window_sec=3.0,
        round_trip_fee_frac=0.00075, min_target_fee_mult=3.0,
        div_min_late_trades=2, arm_timeout_sec=60.0,
        require_ob_imbalance=False,  # v0.7.0: ob_imb — бонус (дефолт прода)
        min_risk_fee_mult=4.0,  # v0.8.1: мин-R пол (fee ≤ 0.25R)
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


# ─── orderbook ─────────────────────────────────────────────────────────────

def test_sec_to_next_funding_intervals():
    from scalp_bot.data.funding import sec_to_next_funding
    assert sec_to_next_funding(0.0, 480) == 8 * 3600
    assert sec_to_next_funding(0.0, 240) == 4 * 3600
    assert sec_to_next_funding(0.0, 60) == 3600
    # в 03:00 UTC до следующей 4ч-метки (04:00) = 1ч; до 8ч-метки (08:00) = 5ч
    assert sec_to_next_funding(3 * 3600, 240) == 3600
    assert sec_to_next_funding(3 * 3600, 480) == 5 * 3600


def test_funding_schedule_per_symbol_and_fallback():
    from scalp_bot.data.funding import FundingSchedule, DEFAULT_INTERVAL_MIN

    class _Cl:
        def get_funding_interval(self, sym):
            return {"ALLOUSDT": 240, "BNBUSDT": 480}.get(sym)  # XLM → None

    fs = FundingSchedule()
    fs.refresh(_Cl(), ["ALLOUSDT", "BNBUSDT", "XLMUSDT"])
    assert fs.interval("ALLOUSDT") == 240
    assert fs.interval("BNBUSDT") == 480
    assert fs.interval("XLMUSDT") == DEFAULT_INTERVAL_MIN  # фолбэк 8ч
    # 03:59 UTC: ALLO(4ч) в окне перед 04:00 → blocked; BNB(8ч) — нет
    t = 4 * 3600 - 60
    assert fs.blocked("ALLOUSDT", t, 120.0) is True
    assert fs.blocked("BNBUSDT", t, 120.0) is False


def test_bracket_exit_reason_splits_tp_sl():
    # LONG: exit выше входа → биржевой TP; ниже → биржевой SL
    assert bracket_exit_reason("long", 100.0, 103.5) == "tp_hit"
    assert bracket_exit_reason("long", 100.0, 99.0) == "sl_hit"
    # SHORT зеркально
    assert bracket_exit_reason("short", 100.0, 96.5) == "tp_hit"
    assert bracket_exit_reason("short", 100.0, 101.0) == "sl_hit"
    # exit неизвестен → legacy-фолбэк tp_sl
    assert bracket_exit_reason("long", 100.0, None) == "tp_sl"


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


# ─── build_signal (SL/TP/fee-guard) + diagnose ────────────────────────────

def _snap(samples, **over):
    base = dict(
        symbol="SOLUSDT", ts=10.0, last_price=97.0, best_bid=96.9, best_ask=97.1,
        ob_imbalance=0.62, funding_rate=-0.0005,
        open_interest=1.0, cvd_samples=samples,
        liq_events=[LiqEvent(1, "Buy", 60000, 97)], stale=False,
    )
    base.update(over)
    return SymbolSnapshot(**base)


def test_build_signal_sl_below_swept_tp_above_entry():
    # LONG: SL ниже свипнутого уровня + буфер, TP выше входа
    snap = _snap(_long_samples())
    swept = 96.5
    sig = build_signal(snap, "long", swept, _cfg(entry_order_type="market"), 4, ["x"])
    assert sig is not None
    assert sig.sl_level < swept
    assert sig.tp_level > sig.entry_ref


def test_build_signal_fee_guard_blocks_tiny_target():
    # завышаем требуемый множитель → ход до TP < min → сигнал отброшен
    snap = _snap(_long_samples())
    assert build_signal(snap, "long", 96.5,
                        _cfg(min_target_fee_mult=1000.0), 4, ["x"]) is None


def test_diagnose_reports_live_detector_flags():
    d = diagnose(_snap(_long_samples()), _cfg())
    assert d is not None
    assert d["side"] == "long"
    assert d["div"] is True and d["sweep"] is True
    # diagnose теперь отражает фазы детектора (sweep/div/reclaim/momentum/ob),
    # без legacy-полей liq/funding/signal
    assert "liq" not in d and "funding" not in d and "signal" not in d
    assert set(d) >= {"sweep", "div", "reclaim", "momentum", "ob", "score"}


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


def test_detector_no_fire_without_ob_imbalance():
    # require_ob_imbalance=True: reclaim+разворот есть, но стакан НЕ подтверждает
    # (imb 0.50 < 0.58) → вход придерживаем, взвод держится
    det = SweepReclaimDetector("SOLUSDT", _cfg(require_ob_imbalance=True))
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)
    assert det.armed is True
    weak_book = _snap(_fire_samples(), last_price=97.6, ob_imbalance=0.50)
    assert det.update(weak_book, now=130.0) is None
    assert det.armed is True  # не разоружился — ждёт подтверждения стакана


def test_detector_fires_with_ob_imbalance_required():
    # тот же сетап, но стакан подтверждает (imb 0.62 ≥ 0.58) → выстрел
    det = SweepReclaimDetector("SOLUSDT", _cfg(require_ob_imbalance=True))
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)
    strong_book = _snap(_fire_samples(), last_price=97.6, ob_imbalance=0.62)
    sig = det.update(strong_book, now=130.0)
    assert sig is not None and "ob_imb" in sig.reasons


def test_detector_waits_for_bar_close_confirmation():
    # confirm_bar_sec=60: reclaim+разворот есть, но БЕЗ закрытия бара входа нет;
    # на границе бара (120) — выстрел (denoise тикового прокола, v0.11.0).
    det = SweepReclaimDetector("SOLUSDT", _cfg(confirm_bar_sec=60.0))
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)  # бар 1
    assert det.armed is True
    # ещё бар 1 (110//60==1): reclaim+mom истинны, но бар не закрылся → держим
    assert det.update(_snap(_fire_samples(), last_price=97.6), now=110.0) is None
    assert det.armed is True
    # 120 → бар 2 (закрытие 1-го бара) → подтверждение → выстрел
    sig = det.update(_snap(_fire_samples(), last_price=97.6), now=120.0)
    assert sig is not None and sig.side == "long"


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


# ─── v0.8.1: мин-R пол (fee ≤ 0.25R) + риск-сайзинг ────────────────────────

def test_build_signal_min_risk_floor_widens_sl():
    # структурный R мал (свип близко к входу) → R расширяется до пола 4×fee.
    # entry=best_ask=100.0; swept 99.95 → struct sl=99.95×(1-8e-4)=99.870,
    # R=0.13 (0.13% < пол 0.3%) → пол: min_risk=4×0.00075×100=0.30 → sl=99.70.
    snap = _snap(_long_samples(), best_ask=100.0, best_bid=99.9, last_price=100.0)
    s = build_signal(snap, "long", 99.95, _cfg(entry_order_type="market"), 3, ["x"])
    assert s is not None
    assert s.sl_level == pytest.approx(99.70, abs=1e-6)
    # TP пересчитан от итогового R: 100 + take_profit_r(2.0)×0.30 = 100.60
    assert s.tp_level == pytest.approx(100.60, abs=1e-6)


def test_build_signal_min_risk_floor_short():
    # short: entry=best_bid=100.0; swept 100.05 → struct R мал → пол отодвигает SL вверх
    snap = _snap(_short_samples(), best_ask=100.1, best_bid=100.0, last_price=100.0)
    s = build_signal(snap, "short", 100.05, _cfg(entry_order_type="market"), 3, ["x"])
    assert s is not None
    assert s.sl_level == pytest.approx(100.30, abs=1e-6)  # 100 + 0.30
    assert s.tp_level == pytest.approx(99.40, abs=1e-6)   # 100 - 2.0×0.30


def test_build_signal_keeps_structure_sl_when_r_above_floor():
    # широкий свип (R > пол) → SL остаётся за структурой, пол не вмешивается
    snap = _snap(_long_samples(), best_ask=100.0, best_bid=99.9, last_price=100.0)
    s = build_signal(snap, "long", 99.0, _cfg(entry_order_type="market"), 3, ["x"])
    assert s is not None
    assert s.sl_level == pytest.approx(99.0 * (1 - 8 / 1e4), abs=1e-6)


def test_position_size_by_risk_basic():
    # риск $1, entry 100, sl 99.55 → dist 0.45 → qty = 1/0.45
    assert position_size_by_risk(1.0, 100.0, 99.55) == pytest.approx(1.0 / 0.45)


def test_position_size_by_risk_floors_to_min_notional():
    # широкий стоп → крошечный лот; пол min_notional $10 поднимает qty.
    # dist 50 → qty=0.02 → notional $2 < $10 → qty = 10/100 = 0.1
    assert position_size_by_risk(1.0, 100.0, 50.0, min_notional=10.0) == pytest.approx(0.1)


def test_position_size_by_risk_zero_distance():
    assert position_size_by_risk(1.0, 100.0, 100.0) == 0.0


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


def _exec(symbol, link, *, fee, pnl=0.0, price, qty, closed=0.0):
    """Нормализованная строка приватного WS execution (как из exec_stream)."""
    return {"symbol": symbol, "orderLinkId": link, "orderId": "", "side": "",
            "execFee": fee, "execPnl": pnl, "execPrice": price, "execQty": qty,
            "closedSize": closed, "leavesQty": 0.0, "stopOrderType": "",
            "execTime": 0.0}


def test_realized_from_fills_none_until_close_arrives():
    # филлы выхода ещё не пришли по WS → оценка по цене, provisional
    ex = Executor(db=None, settings=SimpleNamespace(), client=SimpleNamespace())
    tr = SimpleNamespace(id=1, symbol="ETHUSDT", side="long", entry=2000.0,
                         qty=0.04, ts_open=0.0)
    ex._link2trade["scalp_ETHUSDT_1"] = 1
    # пришёл только входной филл (closedSize=0, pnl=0) — выход ещё нет
    ex.ingest_executions([_exec("ETHUSDT", "scalp_ETHUSDT_1",
                                fee=0.016, price=2000.0, qty=0.04)])
    pnl, exitp, is_real = ex._realized_or_estimate(tr, 1990.0)
    assert is_real is False  # close_qty==0 → неполно
    assert pnl == pytest.approx(taker_pnl("long", 2000.0, 1990.0, 0.04))
    assert exitp == 1990.0


def test_realized_from_fills_net_is_sum_pnl_minus_fees():
    # net = ΣexecPnl − ΣexecFee (вход+выход), exit = VWAP закрывающих филлов
    ex = Executor(db=None, settings=SimpleNamespace(), client=SimpleNamespace())
    tr = SimpleNamespace(id=2, symbol="ZECUSDT", side="long", entry=518.14,
                         qty=0.19, ts_open=0.0)
    ex._link2trade["entry"] = 2
    ex._fills[2] = {"fee": 0.0, "pnl": 0.0, "close_val": 0.0, "close_qty": 0.0}
    # вход: комиссия 0.0541, без pnl
    ex.ingest_executions([_exec("ZECUSDT", "entry", fee=0.0541,
                                price=518.14, qty=0.19)])
    # выход: realized execPnl +0.1482, комиссия 0.0542, цена 518.92
    ex._link2trade["close"] = 2
    ex.ingest_executions([_exec("ZECUSDT", "close", fee=0.0542, pnl=0.1482,
                                price=518.92, qty=0.19, closed=0.19)])
    pnl, exitp, is_real = ex._realized_or_estimate(tr, 0.0)
    assert is_real is True
    assert pnl == pytest.approx(0.1482 - 0.0541 - 0.0542)  # = Bybit closedPnl
    assert exitp == pytest.approx(518.92)


def test_ingest_matches_exchange_tp_sl_by_symbol(tmp_path):
    # биржевой TP/SL: orderLinkId пустой → матч по символу к открытой сделке
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="ZECUSDT", side="long", qty=0.19, entry=518.0,
                         sl=517.0, tp=520.0, score=4, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=0.0)
    ex = Executor(db=db, settings=SimpleNamespace(), client=SimpleNamespace())
    ex._fills[tid] = {"fee": 0.0, "pnl": 0.0, "close_val": 0.0, "close_qty": 0.0}
    ex.ingest_executions([_exec("ZECUSDT", "", fee=0.05, pnl=-0.40,
                                price=517.0, qty=0.19, closed=0.19)])
    tr = SimpleNamespace(id=tid, symbol="ZECUSDT", side="long", entry=518.0,
                         qty=0.19, ts_open=0.0)
    net, exitp, complete = ex._realized_from_fills(tr)
    assert complete is True
    assert net == pytest.approx(-0.45) and exitp == pytest.approx(517.0)
    db.close()


class _FakeRebracketClient:
    """round_price + set_trading_stop для тестов P-3 (сдвиг брекетов после
    слиппеджа market-входа). Фиксирует вызовы аменда."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls: list[tuple] = []

    def round_price(self, symbol, price):
        return round(price, 2)

    def set_trading_stop(self, symbol, *, sl_price=None, tp_price=None):
        self.calls.append((symbol, sl_price, tp_price))
        return {"ok": self.ok,
                "error": None if self.ok else "retCode=10001 test"}


def test_ingest_entry_fill_updates_db_entry_to_real_vwap(tmp_path):
    # A-3 (audit 2026-06-10): MARKET-вход (density_break) наливается со
    # слиппеджем — реальный avgEntryPrice ≠ референс в БД, и REST-реконсиляция
    # не матчила сделку по отпечатку (допуск 0.001%) → provisional зависал.
    # Фикс: входной филл из приватного WS обновляет entry реальным VWAP.
    # P-3 (A-2): заодно SL/TP сдвигаются на дельту слиппеджа (дистанции и
    # $-риск сохраняются) — и в БД, и на бирже (set_trading_stop).
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="BTCUSDT", side="long", qty=0.02, entry=63500.0,
                         sl=63300.0, tp=64200.0, score=3, reasons="x",
                         mode="live", strategy="density_break", ts_open=0.0)
    cl = _FakeRebracketClient()
    ex = Executor(db=db, settings=SimpleNamespace(), client=cl)
    ex._link2trade["scalp_BTCUSDT_1"] = tid
    # два частичных входных филла хуже референса (слиппедж) → VWAP 63512.5
    ex.ingest_executions([
        _exec("BTCUSDT", "scalp_BTCUSDT_1", fee=0.3, price=63510.0, qty=0.01),
        _exec("BTCUSDT", "scalp_BTCUSDT_1", fee=0.3, price=63515.0, qty=0.01),
    ])
    tr = next(t for t in db.open_trades() if t.id == tid)
    assert tr.entry == pytest.approx(63512.5)
    # брекеты сдвинуты на delta=+12.5: дистанции до SL/TP не изменились
    assert tr.sl == pytest.approx(63312.5)
    assert tr.tp == pytest.approx(64212.5)
    assert cl.calls == [("BTCUSDT", 63312.5, 64212.5)]
    db.close()


def test_rebracket_skips_below_threshold(tmp_path):
    # слиппедж < 1 бп (анти-шум порог) — брекеты не трогаем (экономия API),
    # но entry в БД всё равно обновляется (точный отпечаток для реконсиляции).
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="BTCUSDT", side="long", qty=0.02, entry=63500.0,
                         sl=63300.0, tp=64200.0, score=3, reasons="x",
                         mode="live", strategy="density_break", ts_open=0.0)
    cl = _FakeRebracketClient()
    ex = Executor(db=db, settings=SimpleNamespace(), client=cl)
    ex._link2trade["lnk"] = tid
    ex.ingest_executions([_exec("BTCUSDT", "lnk", fee=0.3,
                                price=63500.5, qty=0.02)])  # +0.8 бп
    tr = next(t for t in db.open_trades() if t.id == tid)
    assert tr.entry == pytest.approx(63500.5)
    assert tr.sl == pytest.approx(63300.0) and tr.tp == pytest.approx(64200.0)
    assert cl.calls == []
    db.close()


def test_rebracket_keeps_old_levels_when_exchange_rejects(tmp_path):
    # биржа отклонила set_trading_stop → БД НЕ обновляем (уровни в БД должны
    # отражать реальные биржевые брекеты, а не желаемые).
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="BTCUSDT", side="long", qty=0.02, entry=63500.0,
                         sl=63300.0, tp=64200.0, score=3, reasons="x",
                         mode="live", strategy="density_break", ts_open=0.0)
    cl = _FakeRebracketClient(ok=False)
    ex = Executor(db=db, settings=SimpleNamespace(), client=cl)
    ex._link2trade["lnk"] = tid
    ex.ingest_executions([_exec("BTCUSDT", "lnk", fee=0.3,
                                price=63512.5, qty=0.02)])
    tr = next(t for t in db.open_trades() if t.id == tid)
    assert tr.entry == pytest.approx(63512.5)  # A-3 фикс работает независимо
    assert tr.sl == pytest.approx(63300.0) and tr.tp == pytest.approx(64200.0)
    assert len(cl.calls) == 1  # попытка была
    db.close()


def test_ingest_entry_fill_maker_noop_and_close_untouched(tmp_path):
    # maker-вход филлится ровно по лимит-цене → entry не меняется; закрывающий
    # филл (closedSize>0) в open-аккумулятор не попадает и entry не трогает.
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="ZECUSDT", side="long", qty=0.19, entry=518.14,
                         sl=517.0, tp=520.0, score=5, reasons="x",
                         mode="live", strategy="sweep_fade", ts_open=0.0)
    ex = Executor(db=db, settings=SimpleNamespace(), client=SimpleNamespace())
    ex._link2trade["entry"] = tid
    ex._link2trade["close"] = tid
    ex.ingest_executions([
        _exec("ZECUSDT", "entry", fee=0.05, price=518.14, qty=0.19),
        _exec("ZECUSDT", "close", fee=0.05, pnl=0.15, price=518.92, qty=0.19,
              closed=0.19),
    ])
    tr = next(t for t in db.open_trades() if t.id == tid)
    assert tr.entry == pytest.approx(518.14)  # VWAP входа = лимитка
    acc = ex._fills[tid]
    assert acc["open_qty"] == pytest.approx(0.19)   # только входной филл
    assert acc["close_qty"] == pytest.approx(0.19)  # выход учтён отдельно
    db.close()


def test_ingest_entry_fill_without_db_does_not_crash():
    # db=None (юнит-контекст) — входной филл не должен ронять ingest
    ex = Executor(db=None, settings=SimpleNamespace(), client=SimpleNamespace())
    ex._link2trade["entry"] = 1
    ex.ingest_executions([_exec("ETHUSDT", "entry", fee=0.01,
                                price=2000.0, qty=0.04)])
    assert ex._fills[1]["open_qty"] == pytest.approx(0.04)


def test_reconcile_finalizes_from_ws_ledger(tmp_path):
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="ZECUSDT", side="long", qty=0.19, entry=518.14,
                         sl=517.0, tp=519.0, score=4, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=1000.0)
    # закрыто с ОЦЕНКОЙ (provisional): 0.0721
    db.mark_closed(tid, exit_price=519.09, pnl_usd=0.0721, fees_usd=0.0,
                   close_reason="time_stop", ts_close=1090.0, provisional=True)
    assert len(db.provisional_closed_since(0.0)) == 1
    ex = Executor(db=db, settings=SimpleNamespace(), client=SimpleNamespace(),
                  now=lambda: 1100.0)
    # филлы выхода доехали по WS: реальный net 0.0398 / exit 518.92
    ex._fills[tid] = {"fee": 0.0542, "pnl": 0.0940, "close_val": 518.92 * 0.19,
                      "close_qty": 0.19}
    ex.reconcile()
    assert db.provisional_closed_since(0.0) == []  # флаг снят
    st = {s.strategy: s for s in db.stats_by_strategy(0.0)}["sweep_fade"]
    assert st.pnl_usd == pytest.approx(0.0398)  # БД = выписка
    assert tid not in ex._fills  # трекинг очищен после финализации
    db.close()


def test_reconcile_keeps_provisional_when_fills_absent(tmp_path):
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="ZECUSDT", side="long", qty=0.19, entry=518.14,
                         sl=517.0, tp=519.0, score=4, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=1000.0)
    db.mark_closed(tid, exit_price=519.09, pnl_usd=0.0721, fees_usd=0.0,
                   close_reason="time_stop", ts_close=1090.0, provisional=True)
    ex = Executor(db=db, settings=SimpleNamespace(), client=SimpleNamespace(),
                  now=lambda: 1100.0)
    ex.reconcile()  # филлов в леджере нет → ничего не финализируем
    assert len(db.provisional_closed_since(0.0)) == 1
    db.close()


def test_real_close_notifies_immediately():
    msgs: list[str] = []
    notifier = SimpleNamespace(send=msgs.append)
    ex = Executor(db=None, settings=SimpleNamespace(), client=SimpleNamespace(),
                  notifier=notifier, now=lambda: 1.0)
    tr = SimpleNamespace(id=5, symbol="ZECUSDT", side="long")
    ex._on_close(tr, -0.45, "tp_sl", "TP/SL", is_real=True)
    assert len(msgs) == 1 and "TP/SL" in msgs[0] and "-0.45" in msgs[0]


def test_provisional_close_defers_notify_until_reconcile(tmp_path):
    # Telegram не должен показывать оценку: уведомление откладывается до
    # reconcile, который шлёт РЕАЛЬНЫЙ net по WS-филлам (NEAR #58 из выписки).
    msgs: list[str] = []
    notifier = SimpleNamespace(send=msgs.append)
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="NEARUSDT", side="short", qty=41.2, entry=2.4216,
                         sl=2.4279, tp=2.4123, score=4, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=1000.0)
    ex = Executor(db=db, settings=SimpleNamespace(close_notify_fallback_sec=10.0),
                  client=SimpleNamespace(), notifier=notifier, now=lambda: 1100.0)
    tr = db.open_trades()[0]
    db.mark_closed(tid, exit_price=2.419, pnl_usd=0.0634, fees_usd=0.0,
                   close_reason="flow_exit", ts_close=1100.0, provisional=True)
    ex._on_close(tr, 0.0634, "flow_exit", "flow_exit", is_real=False)
    assert msgs == []  # оценка НЕ ушла в Telegram
    # филлы выхода доехали по WS: cashFlow +0.1071, комиссии 0.0549+0.0548
    ex._fills[tid] = {"fee": 0.1097, "pnl": 0.1071,
                      "close_val": 2.419 * 41.2, "close_qty": 41.2}
    ex.reconcile()
    assert len(msgs) == 1
    assert f"close #{tid}" in msgs[0] and "-0.00" in msgs[0]  # реальный net −0.0026
    db.close()


def test_close_notify_fallback_sends_estimate_after_timeout(tmp_path):
    msgs: list[str] = []
    notifier = SimpleNamespace(send=msgs.append)
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="NEARUSDT", side="short", qty=41.2, entry=2.42,
                         sl=2.43, tp=2.41, score=4, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=1000.0)
    db.mark_closed(tid, exit_price=2.42, pnl_usd=-0.07, fees_usd=0.0,
                   close_reason="time_stop", ts_close=1000.0, provisional=True)
    ex = Executor(db=db, settings=SimpleNamespace(close_notify_fallback_sec=10.0),
                  client=SimpleNamespace(), notifier=notifier, now=lambda: 1005.0)
    ex._close_pending[tid] = {"ts": 1000.0, "label": "time_stop",
                              "symbol": "NEARUSDT"}
    ex.reconcile()  # 5с < 10с и филлов нет → молчим
    assert msgs == []
    ex._now = lambda: 1012.0  # 12с > 10с → фолбэк с пометкой ≈
    ex.reconcile()
    assert len(msgs) == 1 and "≈" in msgs[0] and "-0.07" in msgs[0]
    db.close()


# ─── REST-фолбэк реконсиляции provisional-PnL (v0.18.11) ───────────────────

class _FakeClosedPnlClient:
    """get_closed_pnl-заглушка: по qty отдаёт {pnl, exit} или None; считает вызовы."""

    def __init__(self, by_qty=None):
        self.by_qty = by_qty or {}
        self.calls = 0

    def closed_pnl_detail(self, symbol, *, order_id=None, qty=None,
                          since_ms=None, near_ms=None, until_ms=None,
                          entry_price=None, entry_tol=1e-5, max_pages=10):
        self.calls += 1
        return self.by_qty.get(qty)

    def closed_pnl_position(self, symbol, *, qty, since_ms, until_ms,
                            entry_price=None):
        return None  # partial-sum фолбэк: по умолчанию не матчит


def _mk_provisional(db, *, qty, ts_close, pnl=-3.67, sym="NEARUSDT",
                    strat="density_bounce"):
    tid = db.insert_open(symbol=sym, side="long", qty=qty, entry=2.0026,
                         sl=1.9966, tp=2.0236, score=3, reasons="x", mode="live",
                         strategy=strat, ts_open=ts_close - 100.0)
    # плейсхолдер как #1328: exit=entry, pnl = −оценка комиссии
    db.mark_closed(tid, exit_price=2.0026, pnl_usd=pnl, fees_usd=0.0,
                   close_reason="tp_hit", ts_close=ts_close, provisional=True)
    return tid


def test_reconcile_rest_finalizes_orphaned_provisional(tmp_path):
    """WS-леджер пуст (рестарт обнулил), старая provisional-сделка вне WS-окна →
    добивается через REST closed_pnl до реального net (#1328-кейс)."""
    db = ScalpDB(str(tmp_path))
    tid = _mk_provisional(db, qty=1664.5, ts_close=2000.0, pnl=-3.67)
    client = _FakeClosedPnlClient({1664.5: {"pnl": 31.2, "exit": 2.0236}})
    cfg = SimpleNamespace(reconcile_rest_grace_sec=0.0)
    ex = Executor(db=db, settings=cfg, client=client, now=lambda: 5000.0)
    ex.reconcile()
    assert db.provisional_closed_since(0.0) == []          # флаг снят
    st = {s.strategy: s for s in db.stats_by_strategy(0.0)}["density_bounce"]
    assert st.pnl_usd == pytest.approx(31.2)               # фейк-минус → реальный +
    assert client.calls == 1
    db.close()


def test_reconcile_rest_no_match_keeps_provisional(tmp_path):
    """REST не нашёл запись (нет совпадения) → НЕ финализируем (не выдумываем)."""
    db = ScalpDB(str(tmp_path))
    _mk_provisional(db, qty=1664.5, ts_close=2000.0)
    client = _FakeClosedPnlClient({})                      # пусто
    cfg = SimpleNamespace(reconcile_rest_grace_sec=0.0)
    ex = Executor(db=db, settings=cfg, client=client, now=lambda: 5000.0)
    ex.reconcile()
    assert len(db.provisional_closed_since(0.0)) == 1
    assert client.calls == 1
    db.close()


def test_reconcile_rest_throttles_retry_per_trade(tmp_path):
    """Одну сделку не дёргаем чаще reconcile_rest_retry_sec (rate-limit)."""
    db = ScalpDB(str(tmp_path))
    _mk_provisional(db, qty=1664.5, ts_close=2000.0)
    client = _FakeClosedPnlClient({})
    cfg = SimpleNamespace(reconcile_rest_grace_sec=0.0,
                          reconcile_rest_retry_sec=300.0)
    ex = Executor(db=db, settings=cfg, client=client, now=lambda: 5000.0)
    ex.reconcile()
    ex.reconcile()                       # тот же now → троттл, без нового запроса
    assert client.calls == 1
    ex._now = lambda: 5400.0             # +400с > 300 → ретрай разрешён
    ex.reconcile()
    assert client.calls == 2
    db.close()


def test_reconcile_rest_budget_per_cycle(tmp_path):
    """Бюджет REST-запросов на цикл ограничен (под rate-limit): 5 сделок, бюджет 2."""
    db = ScalpDB(str(tmp_path))
    for i in range(5):
        _mk_provisional(db, qty=100.0 + i, ts_close=2000.0)
    client = _FakeClosedPnlClient({})
    cfg = SimpleNamespace(reconcile_rest_grace_sec=0.0,
                          reconcile_rest_max_per_cycle=2)
    ex = Executor(db=db, settings=cfg, client=client, now=lambda: 5000.0)
    ex.reconcile()
    assert client.calls == 2             # не больше бюджета за цикл
    db.close()


def test_reconcile_rest_grace_protects_fresh(tmp_path):
    """Свежее закрытие (age < grace) НЕ идёт в REST — даём WS-пути шанс."""
    db = ScalpDB(str(tmp_path))
    _mk_provisional(db, qty=1664.5, ts_close=4990.0)       # age=10с
    client = _FakeClosedPnlClient({1664.5: {"pnl": 31.2, "exit": 2.0236}})
    cfg = SimpleNamespace(reconcile_rest_grace_sec=60.0)
    ex = Executor(db=db, settings=cfg, client=client, now=lambda: 5000.0)
    ex.reconcile()
    assert client.calls == 0                               # REST не трогали
    assert len(db.provisional_closed_since(0.0)) == 1
    db.close()


def test_reconciled_bracket_reason_unit():
    """Пересчёт ярлыка по знаку net closedPnl (Bybit close-pnl)."""
    # bracket-плейсхолдер tp_hit + реальный минус → исправляем на sl_hit
    assert reconciled_bracket_reason("tp_hit", -17.47) == "sl_hit"
    # tp_hit + плюс → уже верно, не трогаем
    assert reconciled_bracket_reason("tp_hit", 31.2) is None
    # sl_hit + плюс → исправляем на tp_hit
    assert reconciled_bracket_reason("sl_hit", 5.0) == "tp_hit"
    # legacy tp_sl → конкретизируем по знаку
    assert reconciled_bracket_reason("tp_sl", -1.0) == "sl_hit"
    assert reconciled_bracket_reason("tp_sl", 1.0) == "tp_hit"
    # дискреционные выходы НЕ трогаем (их ярлык не зависит от знака)
    assert reconciled_bracket_reason("flow_exit", -3.0) is None
    assert reconciled_bracket_reason("time_stop", 2.0) is None
    assert reconciled_bracket_reason(None, -1.0) is None


def _close_reason(db, tid):
    return db._conn.execute(
        "SELECT close_reason FROM trades WHERE id=?", (tid,)).fetchone()[0]


def test_reconcile_rest_corrects_stale_tp_hit_label(tmp_path):
    """#1328: provisional-плейсхолдер залип как tp_hit (exit≈entry → favorable=0),
    реальный net минусовой → reconcile исправляет ярлык на sl_hit."""
    db = ScalpDB(str(tmp_path))
    tid = _mk_provisional(db, qty=1664.5, ts_close=2000.0, pnl=-3.67)
    client = _FakeClosedPnlClient({1664.5: {"pnl": -17.47, "exit": 1.9936}})
    cfg = SimpleNamespace(reconcile_rest_grace_sec=0.0)
    ex = Executor(db=db, settings=cfg, client=client, now=lambda: 5000.0)
    ex.reconcile()
    assert _close_reason(db, tid) == "sl_hit"              # tp_hit → sl_hit
    db.close()


def test_reconcile_rest_keeps_tp_hit_when_positive(tmp_path):
    """Реальный net плюсовой → ярлык tp_hit остаётся (не дёргаем зря)."""
    db = ScalpDB(str(tmp_path))
    tid = _mk_provisional(db, qty=1664.5, ts_close=2000.0, pnl=-3.67)
    client = _FakeClosedPnlClient({1664.5: {"pnl": 31.2, "exit": 2.0236}})
    cfg = SimpleNamespace(reconcile_rest_grace_sec=0.0)
    ex = Executor(db=db, settings=cfg, client=client, now=lambda: 5000.0)
    ex.reconcile()
    assert _close_reason(db, tid) == "tp_hit"
    db.close()


# ─── матчер closed_pnl_detail по avgEntryPrice (v0.18.13) ──────────────────
class _FakeSession:
    """Заглушка pybit HTTP: get_closed_pnl отдаёт заранее заданные страницы."""

    def __init__(self, pages):
        self.pages = pages          # list[list[dict]]
        self.calls = 0

    def get_closed_pnl(self, **params):
        cur = int(params.get("cursor", "0") or "0")
        page = self.pages[cur] if cur < len(self.pages) else []
        self.calls += 1
        nxt = str(cur + 1) if cur + 1 < len(self.pages) else ""
        return {"result": {"list": page, "nextPageCursor": nxt}}


def _mk_client(pages):
    from scalp_bot.trading.client import ScalpBybitClient
    cl = ScalpBybitClient.__new__(ScalpBybitClient)
    cl._session = _FakeSession(pages)
    cl._category = "linear"
    cl._instr = {}
    return cl


def _rec(entry, size, pnl, exit_px, created=0):
    return {"avgEntryPrice": str(entry), "closedSize": str(size),
            "closedPnl": str(pnl), "avgExitPrice": str(exit_px),
            "orderId": "oid", "createdTime": str(created)}


def test_closed_pnl_detail_entry_fingerprint_picks_exact():
    """Среди same-qty кандидатов берём ТОЧНОЕ совпадение avgEntryPrice (кейс
    #1194: чужая сделка в 0.004% не должна перебить нашу)."""
    pages = [[
        _rec(60836.90, 0.054, -12.47, 60790.0, 100),   # наш отпечаток
        _rec(60839.40, 0.054, -12.73, 60792.0, 200),   # чужая, Δ0.004%
    ]]
    cl = _mk_client(pages)
    d = cl.closed_pnl_detail("BTCUSDT", qty=0.054, entry_price=60836.90)
    assert d is not None and d["pnl"] == pytest.approx(-12.47)


def test_closed_pnl_detail_ambiguous_refuses():
    """Два кандидата с одинаковой ценой входа (истинная неоднозначность) →
    None (не выдумываем, порча статы хуже пропуска)."""
    pages = [[
        _rec(2000.0, 1.0, 5.0, 2010.0, 100),
        _rec(2000.0, 1.0, -7.0, 1990.0, 200),
    ]]
    cl = _mk_client(pages)
    assert cl.closed_pnl_detail("ETHUSDT", qty=1.0, entry_price=2000.0) is None


def test_set_trading_stop_34040_not_modified_is_idempotent_noop():
    """Bybit 34040 'Not modified' (TP/SL позиции уже равны отправляемым) — pybit
    выбрасывает InvalidRequestError(status_code=34040) вместо возврата retCode.
    Должен считаться успехом (no-op), НЕ логироваться как ERROR и НЕ возвращать
    ok=False — иначе be-lock (manage_levels) не зафиксировал бы _be_locked и
    повторял одинаковый запрос каждый тик → шум traceback'ами (live 2026-06-29:
    #2743 BTCUSDT ~1 req/сек). Офдок: docs/v5/error (34040 Not modified)."""
    from pybit.exceptions import InvalidRequestError
    from scalp_bot.trading.client import ScalpBybitClient

    class _Sess:
        def __init__(self, err): self._err = err
        def set_trading_stop(self, **p): raise self._err

    cl = ScalpBybitClient.__new__(ScalpBybitClient)
    cl._category = "linear"
    err = InvalidRequestError(request="POST /v5/position/trading-stop",
                              message="not modified", status_code=34040,
                              time="06:45:49", resp_headers=None)
    cl._session = _Sess(err)
    res = cl.set_trading_stop("BTCUSDT", sl_price=60209.9, tp_price=59712.9)
    assert res["ok"] is True and res.get("no_op") is True

    # чужой errcode (не 34040) → честный ok=False (не глотаем реальные ошибки)
    cl._session = _Sess(InvalidRequestError(request="x", message="boom",
                                            status_code=10001, time="t",
                                            resp_headers=None))
    res2 = cl.set_trading_stop("BTCUSDT", sl_price=60209.9, tp_price=59712.9)
    assert res2["ok"] is False


def test_closed_pnl_detail_paginates_to_find_record():
    """Нужная запись на 2-й странице — пагинация её достаёт."""
    pages = [
        [_rec(99.0, 10.0, 1.0, 100.0, 100)],            # чужая на стр.1
        [_rec(50.0, 10.0, -3.5, 49.0, 50)],             # наш на стр.2
    ]
    cl = _mk_client(pages)
    d = cl.closed_pnl_detail("XRPUSDT", qty=10.0, entry_price=50.0)
    assert d is not None and d["pnl"] == pytest.approx(-3.5)
    assert cl._session.calls == 2


def test_closed_pnl_detail_no_entry_match_returns_none():
    """entry_price задан, но точного совпадения нет → None (а не чужая запись)."""
    pages = [[_rec(70.0, 10.0, 1.0, 71.0, 100)]]
    cl = _mk_client(pages)
    assert cl.closed_pnl_detail("SOLUSDT", qty=10.0, entry_price=66.0) is None


# ─── partial-sum closed_pnl_position + универсальный true-up (port flowzone) ─
def _rec_x(entry, size, pnl, exit_px, exec_type="Trade"):
    r = _rec(entry, size, pnl, exit_px)
    r["execType"] = exec_type
    return r


def test_closed_pnl_position_sums_partials():
    """Частичные закрытия одной позиции (один avgEntryPrice, разные closedSize)
    суммируются в общий net, если Σ closedSize ≈ qty."""
    pages = [[
        _rec_x(2.0, 0.6, 3.0, 2.1),
        _rec_x(2.0, 0.4, 2.0, 2.2),
    ]]
    cl = _mk_client(pages)
    d = cl.closed_pnl_position("AAAUSDT", qty=1.0, entry_price=2.0,
                               since_ms=0, until_ms=10**13)
    assert d is not None and d["pnl"] == pytest.approx(5.0) and d["count"] == 2


def test_closed_pnl_position_skips_funding_settle_records():
    """Settle/funding-записи (execType!=Trade) НЕ входят в матч по объёму и
    не искажают сумму (офдок close-pnl: execType)."""
    pages = [[
        _rec_x(2.0, 0.6, 3.0, 2.1),
        _rec_x(2.0, 0.4, 2.0, 2.2),
        _rec_x(2.0, 0.5, 99.0, 2.0, exec_type="Settle"),   # funding — игнор
    ]]
    cl = _mk_client(pages)
    d = cl.closed_pnl_position("AAAUSDT", qty=1.0, entry_price=2.0,
                               since_ms=0, until_ms=10**13)
    assert d is not None and d["pnl"] == pytest.approx(5.0)  # без +99


def test_verify_pnl_marks_verified_and_clears_provisional(tmp_path):
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="ZECUSDT", side="long", qty=0.19, entry=518.14,
                         sl=517.0, tp=519.0, score=4, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=1000.0)
    db.mark_closed(tid, exit_price=519.0, pnl_usd=0.07, fees_usd=0.0,
                   close_reason="tp_hit", ts_close=1090.0, provisional=True)
    db.verify_pnl(tid, pnl_usd=0.0398, exit_price=518.92)
    assert db.provisional_closed_since(0.0) == []
    assert db.unverified_closed_live_since(0.0) == []
    row = db._conn.execute(
        "SELECT pnl_usd, pnl_verified, pnl_provisional FROM trades WHERE id=?",
        (tid,)).fetchone()
    assert row["pnl_verified"] == 1 and row["pnl_provisional"] == 0
    assert row["pnl_usd"] == pytest.approx(0.0398)
    db.close()


# ─── fees_usd: комиссия как самостоятельная метрика издержек ───────────────
# Аудит 2026-07-26: колонка fees_usd была нулевой у ВСЕХ live-сделок (live-путь
# хардкодил fees_usd=0.0, а verify_pnl/finalize_pnl её вовсе не писали), хотя
# ΣexecFee уже копился в WS-леджере. Из-за этого издержки исполнения нельзя
# было отделить от качества сигнала и приходилось восстанавливать косвенно.
def test_fee_sum_parses_open_and_close_fee():
    """openFee+closeFee — официальные поля close-pnl, приходят строками.
    https://bybit-exchange.github.io/docs/v5/position/close-pnl"""
    from scalp_bot.trading.client import _fee_sum
    assert _fee_sum({"openFee": "1.5", "closeFee": "1.7"}) == pytest.approx(3.2)
    assert _fee_sum({"openFee": "1.5"}) == pytest.approx(1.5)
    # ни одного поля → None, а НЕ 0.0: «не знаем» ≠ «комиссии не было»
    assert _fee_sum({"closedPnl": "5"}) is None


def test_closed_pnl_detail_exposes_fees():
    rec = _rec(2000.0, 1.0, 5.0, 2010.0, 100)
    rec.update({"openFee": "0.55", "closeFee": "0.60"})
    cl = _mk_client([[rec]])
    d = cl.closed_pnl_detail("ETHUSDT", qty=1.0, entry_price=2000.0)
    assert d is not None and d["fees"] == pytest.approx(1.15)


def test_closed_pnl_position_sums_fees_over_partials():
    a = _rec_x(2.0, 0.6, 3.0, 2.1)
    b = _rec_x(2.0, 0.4, 2.0, 2.2)
    a.update({"openFee": "0.3", "closeFee": "0.3"})
    b.update({"openFee": "0.2", "closeFee": "0.2"})
    cl = _mk_client([[a, b]])
    d = cl.closed_pnl_position("AAAUSDT", qty=1.0, entry_price=2.0,
                               since_ms=0, until_ms=10**13)
    assert d is not None and d["fees"] == pytest.approx(1.0)


def test_verify_pnl_writes_fees_and_none_leaves_column_intact(tmp_path):
    """fees_usd=None не должен затирать уже записанную комиссию нулём —
    иначе WS-значение терялось бы при последующем REST true-up без openFee."""
    db = ScalpDB(str(tmp_path))

    def _mk():
        tid = db.insert_open(symbol="ZECUSDT", side="long", qty=0.19,
                             entry=518.14, sl=517.0, tp=519.0, score=4,
                             reasons="x", mode="live", strategy="sweep_fade",
                             ts_open=1000.0)
        db.mark_closed(tid, exit_price=519.0, pnl_usd=0.07, fees_usd=1.25,
                       close_reason="tp_hit", ts_close=1090.0,
                       provisional=True)
        return tid

    def _fees(tid):
        return db._conn.execute(
            "SELECT fees_usd FROM trades WHERE id=?", (tid,)).fetchone()[0]

    written = _mk()
    db.verify_pnl(written, pnl_usd=0.0398, fees_usd=2.5)
    assert _fees(written) == pytest.approx(2.5)

    kept = _mk()
    db.verify_pnl(kept, pnl_usd=0.0398)          # комиссия неизвестна
    assert _fees(kept) == pytest.approx(1.25)    # прежнее значение уцелело

    fin = _mk()
    db.finalize_pnl(fin, pnl_usd=0.04, fees_usd=3.0)
    assert _fees(fin) == pytest.approx(3.0)
    db.close()


def test_fees_from_rest_falls_back_to_ws_ledger_on_explicit_none():
    """Bybit не прислал openFee/closeFee → detail['fees'] это явный None,
    и dict.get(default) тут бы не сработал. Откатываемся на ΣexecFee из WS."""
    from scalp_bot.trading.executor import Executor
    ex = Executor.__new__(Executor)
    ex._fills = {7: {"fee": 1.8, "pnl": 0.0, "close_val": 0.0, "close_qty": 0.0}}

    class _Tr:
        id = 7

    tr = _Tr()
    assert ex._fees_from_rest({"fees": None}, tr) == pytest.approx(1.8)
    assert ex._fees_from_rest({"fees": 2.4}, tr) == pytest.approx(2.4)
    # комиссия честно нулевая (rebate/промо) — не подменяем WS-значением
    assert ex._fees_from_rest({"fees": 0.0}, tr) == pytest.approx(0.0)


def test_unverified_selector_excludes_paper_tech_and_verified(tmp_path):
    db = ScalpDB(str(tmp_path))

    def _closed(mode, reason, *, qty=1.0):
        tid = db.insert_open(symbol="XUSDT", side="long", qty=qty, entry=1.0,
                             sl=0.9, tp=1.1, score=3, reasons="x", mode=mode,
                             strategy="sweep_fade", ts_open=900.0)
        db.mark_closed(tid, exit_price=1.05, pnl_usd=1.0, fees_usd=0.0,
                       close_reason=reason, ts_close=1000.0)
        return tid

    live = _closed("live", "tp_hit")
    _closed("paper", "tp_hit")                 # paper — нет closedPnl
    _closed("live", "entry_timeout")           # технич. закрытие
    verified = _closed("live", "sl_hit")
    db.verify_pnl(verified, pnl_usd=-2.0)
    ids = {t.id for t in db.unverified_closed_live_since(0.0)}
    assert ids == {live}
    db.close()


def test_reconcile_trues_up_ws_drift_against_closedpnl(tmp_path):
    """WS-финализированная live-сделка (provisional=0, verified=0) с дрейфом
    комиссии досверяется до биржевого closedPnl и помечается verified."""
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="JTOUSDT", side="long", qty=10.0, entry=2.0,
                         sl=1.9, tp=2.2, score=4, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=1900.0)
    # WS-нет завысил (недосчёт комиссии): записан 0.50, биржа даёт 0.42
    db.mark_closed(tid, exit_price=2.05, pnl_usd=0.50, fees_usd=0.0,
                   close_reason="tp_hit", ts_close=2000.0, provisional=False)
    client = _FakeClosedPnlClient({10.0: {"pnl": 0.42, "exit": 2.05}})
    cfg = SimpleNamespace(reconcile_rest_grace_sec=0.0)
    ex = Executor(db=db, settings=cfg, client=client, now=lambda: 5000.0)
    ex.reconcile()
    row = db._conn.execute(
        "SELECT pnl_usd, pnl_verified FROM trades WHERE id=?", (tid,)).fetchone()
    assert row["pnl_verified"] == 1
    assert row["pnl_usd"] == pytest.approx(0.42)
    assert db.unverified_closed_live_since(0.0) == []
    db.close()


def test_rest_verify_gives_up_after_max_fails(tmp_path):
    """Неоднозначная сделка (REST не матчится) после _VERIFY_MAX_FAILS попыток
    принимается как есть (WS-net) и помечается verified — не жжём бюджет."""
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="TAOUSDT", side="long", qty=1.0, entry=300.0,
                         sl=290.0, tp=320.0, score=4, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=1900.0)
    db.mark_closed(tid, exit_price=310.0, pnl_usd=7.5, fees_usd=0.0,
                   close_reason="tp_hit", ts_close=2000.0, provisional=False)
    client = _FakeClosedPnlClient({})          # REST никогда не матчит
    cfg = SimpleNamespace(reconcile_rest_grace_sec=0.0,
                          reconcile_rest_retry_sec=300.0)
    ex = Executor(db=db, settings=cfg, client=client, now=lambda: 5000.0)
    ex.reconcile()                              # fail 1
    ex._now = lambda: 5400.0
    ex.reconcile()                              # fail 2
    ex._now = lambda: 5800.0
    ex.reconcile()                              # fail 3 → сдаёмся, verified
    row = db._conn.execute(
        "SELECT pnl_usd, pnl_verified FROM trades WHERE id=?", (tid,)).fetchone()
    assert row["pnl_verified"] == 1
    assert row["pnl_usd"] == pytest.approx(7.5)  # оставлен WS-net
    db.close()


# ─── fee-aware дискреционный выход sweep_fade (через should_exit) ───────────

def _sweep_strat(now_t=None):
    from scalp_bot.analysis.strategies import SweepFadeStrategy
    cfg = SimpleNamespace(active_exit_enabled=True, active_exit_min_age_sec=10.0,
                          momentum_window_sec=3.0, round_trip_fee_frac=0.0011,
                          scratch_on_flow_flip=True, scratch_min_age_sec=20.0,
                          flow_exit_activate_r=1.0,   # v0.7.1: профит-лок ≥1R
                          scratch_min_adverse_r=0.7)  # v0.9.2: порог глубины
    return SweepFadeStrategy(cfg, [])


def _flow_flip_samples():
    # лента качнулась в short → flow_invalidated(long)=True
    return [CvdSample(4, 97, -1), CvdSample(5, 96.5, -3), CvdSample(6, 96.4, -6)]


# во всех сделках ниже: entry 97.0, sl 96.80 → R = 0.20 (1R-порог flow_exit)

def test_flow_exit_holds_when_profit_below_1r():
    # +0.05 хода < 1R(0.20) → НЕ клипаем, ДЕРЖИМ (даём добежать к TP) — v0.7.1
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=97.05)
    tr = SimpleNamespace(id=1, side="long", entry=97.0, sl=96.80, ts_open=80.0)
    assert st.should_exit(tr, snap, now=100.0) is None


def test_flow_exit_holds_small_profit_anticlip():
    # +0.10 (полпути до 1R) + лента развернулась → раньше клипали, теперь ДЕРЖИМ
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=97.10)
    tr = SimpleNamespace(id=2, side="long", entry=97.0, sl=96.80, ts_open=80.0)
    assert st.should_exit(tr, snap, now=100.0) is None


def test_flow_exit_fires_when_profit_reaches_1r():
    # +0.20 = 1R и лента развернулась → фиксируем осмысленный профит
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=97.20)
    tr = SimpleNamespace(id=3, side="long", entry=97.0, sl=96.80, ts_open=80.0)
    decision = st.should_exit(tr, snap, now=100.0)
    assert decision is not None and decision[0] == "flow_exit"
    assert decision[1] == pytest.approx(97.20)


def test_flow_exit_respects_min_age():
    # возраст 5с < 10с → активный выход не вмешивается, даже если профит большой
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=97.50)
    tr = SimpleNamespace(id=4, side="long", entry=97.0, sl=96.80, ts_open=80.0)
    assert st.should_exit(tr, snap, now=85.0) is None


def test_flow_scratch_fires_when_underwater_and_flow_flips():
    # ход −0.20 = −1.0R (≥ порог глубины 0.7R=0.14) + поток против + созрела (25с)
    # → режем убыток рано (flow_scratch), не ждём SL/тайм-стоп
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=96.80)
    tr = SimpleNamespace(id=5, side="long", entry=97.0, sl=96.80, ts_open=80.0)
    decision = st.should_exit(tr, snap, now=105.0)
    assert decision is not None and decision[0] == "flow_scratch"
    assert decision[1] == pytest.approx(96.80)


def test_flow_scratch_skips_small_underwater():
    # мелкий минус −0.05 (−0.25R) < порог 0.7R → НЕ скретчим (даём развиться)
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=96.95)
    tr = SimpleNamespace(id=6, side="long", entry=97.0, sl=96.80, ts_open=80.0)
    assert st.should_exit(tr, snap, now=105.0) is None


def test_flow_scratch_holds_shallow_underwater_below_threshold():
    # v0.9.2: −0.3R против + флип ленты — РАНЬШЕ скретчили (≥комиссии), теперь
    # ДЕРЖИМ (ниже порога 0.7R), даём уйти в безубыточный time_stop/восстановиться
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=96.70)  # R=1.0 → −0.3R
    tr = SimpleNamespace(id=61, side="long", entry=97.0, sl=96.0, ts_open=80.0)
    assert st.should_exit(tr, snap, now=105.0) is None


def test_flow_scratch_fires_when_deep_underwater():
    # −0.8R против (≥0.7R) + флип + созрела → режем до полного SL
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=96.20)  # R=1.0 → −0.8R
    tr = SimpleNamespace(id=62, side="long", entry=97.0, sl=96.0, ts_open=80.0)
    decision = st.should_exit(tr, snap, now=105.0)
    assert decision is not None and decision[0] == "flow_scratch"


def test_flow_scratch_respects_scratch_min_age():
    # явно в минусе и поток против, но возраст 15с < scratch_min_age 20с →
    # ещё не режем (сетапу даём «созреть»)
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=96.80)
    tr = SimpleNamespace(id=7, side="long", entry=97.0, sl=96.80, ts_open=80.0)
    assert st.should_exit(tr, snap, now=95.0) is None


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


def test_is_killed_zero_limit_disables(tmp_path):
    """v0.18.23: лимит ≤0 = killswitch ВЫКЛЮЧЕН (демо-счёт; запрос пользователя
    2026-06-12 — total −807 ≤ −800 навсегда блокировал форвард-тест). Каждый
    лимит независим: 0 у одного не отключает другой."""
    cfg0 = _ks_cfg(max_daily_loss_usd=0.0, max_total_loss_usd=0.0)
    assert killswitch.is_killed(
        _FakeDB(day=-9999.0, total=-9999.0), cfg0, now=1000.0).allowed is True
    # дневной выключен, совокупный активен
    cfg_t = _ks_cfg(max_daily_loss_usd=0.0, max_total_loss_usd=150.0)
    assert killswitch.is_killed(_FakeDB(day=-9999.0), cfg_t, now=1000.0).allowed is True
    assert killswitch.is_killed(_FakeDB(total=-150.0), cfg_t, now=1000.0).allowed is False
    # совокупный выключен, дневной активен
    cfg_d = _ks_cfg(max_daily_loss_usd=50.0, max_total_loss_usd=0.0)
    assert killswitch.is_killed(_FakeDB(total=-9999.0), cfg_d, now=1000.0).allowed is True
    assert killswitch.is_killed(_FakeDB(day=-50.0), cfg_d, now=1000.0).allowed is False
    # прод-дефолт compose = 0/0 — выключено; дефолты класса защитные (500/800)
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()
    assert s.max_daily_loss_usd == 500.0 and s.max_total_loss_usd == 800.0


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


# ─── мультистратегийный каркас: resolve / тег в БД / диспетч выхода ─────────

from scalp_bot.analysis.signals import Signal  # noqa: E402
from scalp_bot.analysis.strategies import (  # noqa: E402
    DensityBounceStrategy,
    DensityBreakStrategy,
    RollingBaseline,
    SweepFadeCanonStrategy,
    SweepFadeStrategy,
    build_strategies,
    detect_wall,
    near_round,
    near_round_hier,
    resolve,
    resolve_reset_state,
)
from scalp_bot.data.universe import rank_universe  # noqa: E402
from scalp_bot.state.db import ScalpDB  # noqa: E402


def _sig(side, score, strategy="sweep_fade", symbol="SOLUSDT"):
    return Signal(symbol=symbol, side=side, entry_ref=100.0, sl_level=99.0,
                  tp_level=102.0, score=score, reasons=["x"], strategy=strategy)


def test_resolve_none_when_empty():
    resolve_reset_state()
    assert resolve([]) is None


def test_resolve_same_side_picks_highest_score():
    resolve_reset_state()
    a = _sig("long", 4, "sweep_fade")
    b = _sig("long", 6, "density_bounce")
    assert resolve([a, b]) is b  # выше score


def test_resolve_same_side_collision_logged(caplog):
    """v0.18.21: same-side коллизия логируется (замер частоты — решение о
    Partial-брекетах по данным), победитель прежний (max score). v0.18.28: лог
    троттлится per-cluster — логируется на новом кластере, не каждый тик."""
    import logging
    resolve_reset_state()
    a = _sig("long", 4, "sweep_fade")
    b = _sig("long", 6, "density_bounce")
    with caplog.at_level(logging.INFO, logger="scalp_bot.play"):
        assert resolve([a, b]) is b
    msgs = [r.getMessage() for r in caplog.records]
    assert any("SAME-SIDE КОЛЛИЗИЯ" in m and "sweep_fade(score=4)" in m
               for m in msgs)
    # повторный вызов того же кластера — лог не повторяется (троттл per-cluster)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="scalp_bot.play"):
        assert resolve([a, b]) is b
    assert not any("КОЛЛИЗИЯ" in r.getMessage() for r in caplog.records)
    # одиночный сигнал коллизию не пишет
    caplog.clear()
    resolve_reset_state()
    with caplog.at_level(logging.INFO, logger="scalp_bot.play"):
        assert resolve([a]) is a
    assert not any("КОЛЛИЗИЯ" in r.getMessage() for r in caplog.records)


def test_resolve_conflicting_sides_skips():
    resolve_reset_state()
    # long и short по одному символу → неоднозначность → не берём ничего
    assert resolve([_sig("long", 5), _sig("short", 9, "density_bounce")]) is None


def test_resolve_round_robin_among_tied_strategies():
    """v0.18.28: при равенстве score победитель вращается по кластерам, а не
    всегда первая по порядку страта. Canon-наследники дают идентичные сигналы —
    без вращения canon забирал бы 100%, варианты 0. Каждому новому кластеру
    (отличный fingerprint = другой уровень входа) — следующая страта.
    (v0.18.33: sweep_fade_trend удалена; v0.18.37: sweep_fade_run удалена;
    ротация проверяется на трёх любых tied-стратегиях — механизм имени не знает.)"""
    resolve_reset_state()
    strats = ("sweep_fade_canon", "strat_b", "sweep_fade")
    # кластер 1 (entry 100.0): первый раз — canon (idx 0)
    c1 = [_sig("short", 6, s, symbol="BTCUSDT") for s in strats]
    assert resolve(list(c1)).strategy == "sweep_fade_canon"
    # тот же кластер (тот же fp) — стабильный победитель, canon снова
    assert resolve(list(c1)).strategy == "sweep_fade_canon"
    # кластер 2 (другой уровень entry=200.0) — следующий по ротации: strat_b (idx 1)
    c2 = [Signal(symbol="BTCUSDT", side="short", entry_ref=200.0, sl_level=99.0,
                 tp_level=202.0, score=6, reasons=["x"], strategy=s)
          for s in strats]
    assert resolve(c2).strategy == "strat_b"
    # кластер 3 (entry=300.0) — третья страта (idx 2)
    c3 = [Signal(symbol="BTCUSDT", side="short", entry_ref=300.0, sl_level=99.0,
                 tp_level=302.0, score=6, reasons=["x"], strategy=s)
          for s in strats]
    assert resolve(c3).strategy == "sweep_fade"
    # кластер 4 — снова canon (idx 3 % 3 = 0)
    c4 = [Signal(symbol="BTCUSDT", side="short", entry_ref=400.0, sl_level=99.0,
                 tp_level=402.0, score=6, reasons=["x"], strategy=s)
          for s in strats]
    assert resolve(c4).strategy == "sweep_fade_canon"


def test_resolve_round_robin_independent_per_symbol():
    """Ротация per-symbol: BTCUSDT и ETHUSDT ведут независимые счётчики."""
    resolve_reset_state()
    mk = lambda sym, entry, strat: Signal(symbol=sym, side="short",
            entry_ref=entry, sl_level=99.0, tp_level=entry + 2.0, score=6,
            reasons=["x"], strategy=strat)
    # BTC кластер1 → canon
    assert resolve([mk("BTCUSDT", 100.0, "sweep_fade_canon"),
                    mk("BTCUSDT", 100.0, "strat_b")]).strategy == "sweep_fade_canon"
    # ETH кластер1 → canon (свой счётчик, не继承 BTC)
    assert resolve([mk("ETHUSDT", 100.0, "sweep_fade_canon"),
                    mk("ETHUSDT", 100.0, "strat_b")]).strategy == "sweep_fade_canon"
    # BTC кластер2 → strat_b; ETH кластер2 → strat_b (независимо)
    assert resolve([mk("BTCUSDT", 200.0, "sweep_fade_canon"),
                    mk("BTCUSDT", 200.0, "strat_b")]).strategy == "strat_b"
    assert resolve([mk("ETHUSDT", 200.0, "sweep_fade_canon"),
                    mk("ETHUSDT", 200.0, "strat_b")]).strategy == "strat_b"


def test_resolve_round_robin_survives_group_shrink():
    """Fix 2026-07-02: tie-группа может СЖАТЬСЯ между тиками при том же
    fingerprint (страта выпала по мигнувшему гейту) — сохранённый idx выходил
    за границы группы → IndexError валил main-loop. Теперь кламп по модулю:
    победитель валиден, ротация сохранена. (v0.18.33: trend удалена, механизм
    проверяется на любых трёх tied-стратегиях.)"""
    resolve_reset_state()
    mk = lambda entry, strat: Signal(symbol="BTCUSDT", side="short",
            entry_ref=entry, sl_level=99.0, tp_level=entry + 2.0, score=6,
            reasons=["x"], strategy=strat)
    strats = ("sweep_fade_canon", "strat_b", "sweep_fade")
    # три кластера подряд → для entry=300.0 сохранён idx=2 (третья страта)
    resolve([mk(100.0, s) for s in strats])
    resolve([mk(200.0, s) for s in strats])
    assert resolve([mk(300.0, s) for s in strats]).strategy == "sweep_fade"
    # тот же кластер (fp совпадает), но третья страта выпала → группа из 2:
    # раньше group[2] бросал IndexError; теперь 2 % 2 = 0 → canon
    win = resolve([mk(300.0, "sweep_fade_canon"), mk(300.0, "strat_b")])
    assert win is not None and win.strategy == "sweep_fade_canon"


def test_build_strategies_defaults_to_sweep_fade():
    cfg = SimpleNamespace(strategy_list=["sweep_fade"])
    strats = build_strategies(cfg, ["SOLUSDT"])
    assert [s.name for s in strats] == ["sweep_fade"]


def test_build_strategies_unknown_falls_back():
    cfg = SimpleNamespace(strategy_list=["does_not_exist"])
    strats = build_strategies(cfg, ["SOLUSDT"])
    assert [s.name for s in strats] == ["sweep_fade"]  # защита: всегда хоть одна


def test_sweep_fade_tags_signal_strategy():
    # сигнал от стратегии помечается её именем (атрибуция)
    cfg = _cfg()
    st = SweepFadeStrategy(cfg, ["SOLUSDT"])
    # взвод
    armed = _snap([CvdSample(1, 100, -1), CvdSample(2, 99, -3), CvdSample(3, 98, -5),
                   CvdSample(4, 97, -4), CvdSample(5, 96.5, -2), CvdSample(6, 97.0, -1)],
                  ts=10.0, last_price=97.0)
    st.update(armed, now=10.0)
    # выстрел: reclaim + momentum
    fire = _snap([CvdSample(7, 96.5, -2), CvdSample(8, 97.0, 0), CvdSample(9, 97.6, 3),
                  CvdSample(10, 97.8, 5), CvdSample(11, 98.0, 7), CvdSample(12, 98.2, 9)],
                 ts=20.0, last_price=98.2)
    sig = st.update(fire, now=20.0)
    if sig is not None:  # если сетап сложился — тег обязателен
        assert sig.strategy == "sweep_fade"


def test_db_strategy_tag_and_stats(tmp_path):
    db = ScalpDB(str(tmp_path))
    # sweep_fade: 2 сделки (+1.0 win, -0.4 loss); density_bounce: 1 win +2.0
    for strat, pnl in [("sweep_fade", 1.0), ("sweep_fade", -0.4),
                       ("density_bounce", 2.0)]:
        tid = db.insert_open(symbol="SOLUSDT", side="long", qty=1.0, entry=100.0,
                             sl=99.0, tp=102.0, score=4, reasons="x", mode="paper",
                             strategy=strat, ts_open=1000.0)
        db.mark_closed(tid, exit_price=101.0, pnl_usd=pnl, fees_usd=0.05,
                       close_reason="tp", ts_close=2000.0)
    stats = {s.strategy: s for s in db.stats_by_strategy(since=0.0)}
    assert stats["sweep_fade"].trades == 2
    assert stats["sweep_fade"].wins == 1 and stats["sweep_fade"].losses == 1
    assert stats["sweep_fade"].pnl_usd == pytest.approx(0.6)
    assert stats["sweep_fade"].win_rate == pytest.approx(0.5)
    assert stats["density_bounce"].pnl_usd == pytest.approx(2.0)
    db.close()


def test_db_stats_excludes_reconcile_closes(tmp_path):
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="SOLUSDT", side="long", qty=1.0, entry=100.0,
                         sl=99.0, tp=102.0, score=4, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=1000.0)
    db.mark_closed(tid, exit_price=100.0, pnl_usd=0.0, fees_usd=0.0,
                   close_reason="restart_flat", ts_close=2000.0)
    # реконсил-закрытие не считается торговым исходом
    assert db.stats_by_strategy(since=0.0) == []
    db.close()


def test_last_sl_close_ts(tmp_path):
    """sl_cooldown: last_sl_close_ts отдаёт ts последнего SL по символу+стороне,
    игнорируя не-SL выходы и другую сторону/символ (v0.15.0)."""
    db = ScalpDB(str(tmp_path))

    def closed(symbol, side, reason, ts_close):
        tid = db.insert_open(symbol=symbol, side=side, qty=1.0, entry=100.0,
                             sl=99.0, tp=102.0, score=5, reasons="x", mode="paper",
                             strategy="sweep_fade", ts_open=ts_close - 60)
        db.mark_closed(tid, exit_price=99.0, pnl_usd=-1.0, fees_usd=0.0,
                       close_reason=reason, ts_close=ts_close)

    # нет закрытий → None
    assert db.last_sl_close_ts("XLMUSDT", "long") is None
    # два SL по XLM long — берём максимальный ts
    closed("XLMUSDT", "long", "sl_hit", 1000.0)
    closed("XLMUSDT", "long", "sl_hit", 1500.0)
    assert db.last_sl_close_ts("XLMUSDT", "long") == pytest.approx(1500.0)
    # TP/flow_exit не считаются стопом
    closed("XLMUSDT", "long", "tp_hit", 2000.0)
    closed("XLMUSDT", "long", "flow_exit", 2100.0)
    assert db.last_sl_close_ts("XLMUSDT", "long") == pytest.approx(1500.0)
    # другая сторона и другой символ изолированы
    assert db.last_sl_close_ts("XLMUSDT", "short") is None
    assert db.last_sl_close_ts("ZECUSDT", "long") is None
    closed("XLMUSDT", "short", "sl_hit", 3000.0)
    assert db.last_sl_close_ts("XLMUSDT", "short") == pytest.approx(3000.0)
    assert db.last_sl_close_ts("XLMUSDT", "long") == pytest.approx(1500.0)
    db.close()


def test_last_sl_close_ts_per_strategy(tmp_path):
    """v0.18.21: SL-cooldown пер-стратегийный — стоп ОДНОЙ страты не глушит
    другие по тому же символу+стороне (density_break/bounce теряли сигналы от
    чужого SL sweep_fade на 60-мин окно). strategy=None — старое поведение."""
    db = ScalpDB(str(tmp_path))

    def closed(strategy, ts_close):
        tid = db.insert_open(symbol="NEARUSDT", side="long", qty=1.0, entry=100.0,
                             sl=99.0, tp=102.0, score=5, reasons="x", mode="paper",
                             strategy=strategy, ts_open=ts_close - 60)
        db.mark_closed(tid, exit_price=99.0, pnl_usd=-1.0, fees_usd=0.0,
                       close_reason="sl_hit", ts_close=ts_close)

    closed("sweep_fade", 1000.0)
    # SL фейда виден фейду, но НЕ блокирует пробой/баунс/канон
    assert db.last_sl_close_ts("NEARUSDT", "long",
                               strategy="sweep_fade") == pytest.approx(1000.0)
    assert db.last_sl_close_ts("NEARUSDT", "long", strategy="density_break") is None
    assert db.last_sl_close_ts("NEARUSDT", "long", strategy="density_bounce") is None
    assert db.last_sl_close_ts("NEARUSDT", "long",
                               strategy="sweep_fade_canon") is None
    # своя страта видит только свой последний SL
    closed("density_break", 2000.0)
    assert db.last_sl_close_ts("NEARUSDT", "long",
                               strategy="density_break") == pytest.approx(2000.0)
    assert db.last_sl_close_ts("NEARUSDT", "long",
                               strategy="sweep_fade") == pytest.approx(1000.0)
    # strategy=None — агрегат по всем (обратная совместимость)
    assert db.last_sl_close_ts("NEARUSDT", "long") == pytest.approx(2000.0)
    db.close()


def test_sl_cooldown_for_per_strategy():
    """v0.18.14: sweep_fade имеет расширенное окно SL-cooldown (60м, канон MR +
    sweep n=829), а density_break/bounce — базовый sl_cooldown_sec. Дефолты:
    sweep_fade=3600с, прочие=300с."""
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()
    assert s.sl_cooldown_for("sweep_fade") == 3600.0
    assert s.sl_cooldown_for("density_break") == s.sl_cooldown_sec == 300.0
    assert s.sl_cooldown_for("density_bounce") == 300.0
    # независимая конфигурация окон (sweep_fade не наследует базовый)
    cfg = ScalpSettings().model_copy(update={
        "sl_cooldown_sec": 120.0, "sweep_fade_sl_cooldown_sec": 1800.0})
    assert cfg.sl_cooldown_for("sweep_fade") == 1800.0
    assert cfg.sl_cooldown_for("density_break") == 120.0


def test_no_long_symbols_gate():
    """v0.18.17 (C-07): per-symbol LONG-блок. Лонг на символе из no_long_list
    запрещён ВСЕМ стратегиям, шорт разрешён; символы не из списка не задеты.
    Дефолт прод = ZECUSDT (env SCALP_NO_LONG_SYMBOLS), парсинг CSV + upper-case."""
    from scalp_bot.config.settings import ScalpSettings
    # дефолт класса — пусто (прод-значение приходит из env/compose)
    assert ScalpSettings().no_long_list == []
    cfg = ScalpSettings().model_copy(update={"no_long_symbols": "zecusdt, ENAUSDT"})
    assert cfg.no_long_list == ["ZECUSDT", "ENAUSDT"]

    def blocked(side: str, sym: str) -> bool:
        # та же предикат-логика, что в main.py (гейт перед HTF/DMI)
        return side == "long" and sym in cfg.no_long_list

    assert blocked("long", "ZECUSDT") is True       # лонг по символу — блок
    assert blocked("short", "ZECUSDT") is False      # шорт разрешён
    assert blocked("long", "BTCUSDT") is False       # не из списка — не задет
    assert blocked("long", "ENAUSDT") is True        # второй символ списка


def test_density_break_prod_defaults():
    """v0.18.16 (C-06): прод-дефолты density_break. taker-вход (пробой не наливается
    maker-лимиткой), CVD-confirmation ВКЛ (фильтр grab'ов). Фейды — глобальный maker."""
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()
    assert s.density_break_entry_order_type == "market"   # taker
    assert s.entry_order_type == "post_only_limit"        # фейды — maker
    assert s.density_break_confirm_cvd is True
    assert s.momentum_window_sec == 30.0                  # follow-through = канон-окно
    assert s.density_break_require_ob is True              # канон-гейт абсорбции
    # v0.18.25 (V1): close-confirmation ВКЛ по умолчанию (канон «закрытие за
    # уровнем, не first-touch»); 60с — прецедент v0.11.0.
    assert s.density_break_confirm_bar_sec == 60.0


def test_db_migration_adds_strategy_column(tmp_path):
    import sqlite3
    # старая БД без колонки strategy
    p = str(tmp_path / "scalp_bot.sqlite")
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_open REAL,"
        "symbol TEXT, side TEXT, qty REAL, entry REAL, sl REAL, tp REAL,"
        "score INTEGER, reasons TEXT, mode TEXT, status TEXT DEFAULT 'open',"
        "entry_order_id TEXT, ts_close REAL, exit REAL, pnl_usd REAL,"
        "fees_usd REAL, close_reason TEXT);")
    con.execute("INSERT INTO trades (ts_open,symbol,side,qty,entry,sl,tp,score,"
                "reasons,mode,status) VALUES (1,'SOLUSDT','long',1,100,99,102,4,"
                "'x','paper','closed')")
    con.commit()
    con.close()
    # открытие через ScalpDB должно добавить колонку и проставить дефолт
    db = ScalpDB(str(tmp_path))
    rows = db.open_trades()  # не должно падать на отсутствии strategy
    assert all(hasattr(r, "strategy") for r in rows)
    db.close()


def test_executor_dispatches_exit_to_owning_strategy():
    # executor вызывает should_exit ИМЕННО стратегии-владельца сделки
    calls = []

    class _Strat:
        name = "density_bounce"

        def should_exit(self, tr, snap, now):
            calls.append((tr.id, now))
            return ("density_gone", 100.5)

    ex = Executor(db=None, settings=SimpleNamespace(), client=None,
                  strategies=[_Strat()], now=lambda: 42.0)
    tr = SimpleNamespace(id=7, strategy="density_bounce", side="long", entry=100.0)
    snap = _snap(_long_samples())
    assert ex._strategy_exit(tr, snap) == ("density_gone", 100.5)
    assert calls == [(7, 42.0)]


def test_executor_exit_dispatch_unknown_strategy_returns_none():
    ex = Executor(db=None, settings=SimpleNamespace(), client=None,
                  strategies=[], now=lambda: 1.0)
    tr = SimpleNamespace(id=8, strategy="ghost", side="long", entry=100.0)
    assert ex._strategy_exit(tr, _snap(_long_samples())) is None


# ─── density_bounce (Фаза 2): стена в стакане → отскок ──────────────────────

def _density_cfg(**over):
    base = dict(
        density_wall_mult=8.0, density_round_frac=0.001, density_persist_sec=10.0,
        # v0.18.15: bounce persist == base в тестах (быстро); прод 1200с
        density_bounce_persist_sec=10.0,
        density_absorb_frac=0.30, density_absorb_window_sec=10.0,
        density_near_bps=8.0, density_min_wall_usd=0.0,
        # v0.18.30: редизайн трека (допуск идентичности + grace на пропадание)
        density_wall_tolerance_bps=5.0, density_track_grace_sec=10.0,
        # rolling-baseline (v0.9.0): high min_samples → тесты на fallback
        # (мгновенный baseline), сохраняя прежние ожидания пер-функции.
        density_baseline_sec=900.0, density_baseline_min_samples=30,
        # для build_signal:
        entry_order_type="market", sl_buffer_bps=8.0, take_profit_r=2.0,
        density_break_take_profit_r=3.5,  # v0.18.10 = глобальный канон (Философия B)
        round_trip_fee_frac=0.0011, min_target_fee_mult=3.0,
        active_exit_min_age_sec=10.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _book_with_bid_wall(wall_size=50.0):
    bids = [(100.0, wall_size), (99.99, 1), (99.98, 1), (99.97, 1), (99.96, 1)]
    asks = [(100.10, 1), (100.11, 1), (100.12, 1), (100.13, 1), (100.14, 1)]
    return bids, asks


def test_near_round_scales_with_price():
    assert near_round(100.0, 0.001) is True       # шаг 10 → 100 круглое
    assert near_round(2.4, 0.001) is True          # шаг 0.1 → 2.4 круглое
    assert near_round(518.0, 0.001) is False       # шаг 10 → ближайшее 520
    assert near_round(66.43, 0.001) is False       # шаг 1 → 66, далеко


def test_detect_wall_excludes_self_from_baseline():
    bids, _ = _book_with_bid_wall(50.0)
    w = detect_wall(bids, wall_mult=8.0)
    assert w == (100.0, 50.0)
    # если стена не дотягивает до 8× обычного уровня — не стена
    assert detect_wall([(100.0, 5), (99.9, 1), (99.8, 1), (99.7, 1), (99.6, 1)],
                       wall_mult=8.0) is None


def test_detect_wall_needs_min_levels():
    assert detect_wall([(100.0, 99), (99.9, 1)], wall_mult=8.0) is None


def test_detect_wall_uses_explicit_rolling_baseline():
    # та же книга, но baseline берём СКОЛЬЗЯЩИЙ (меньше мгновенного) → стена
    # квалифицируется при том же size, хотя мгновенный знаменатель не дал бы
    bids = [(100.0, 30.0), (99.99, 10), (99.98, 10), (99.97, 10), (99.96, 10)]
    # мгновенный baseline (без max) = 10 → 30 < 8×10=80 → не стена
    assert detect_wall(bids, wall_mult=8.0) is None
    # rolling baseline низкий (рынок был тонким, типичный уровень ≈3) → 30 ≥ 8×3
    assert detect_wall(bids, wall_mult=8.0, baseline=3.0) == (100.0, 30.0)


def test_rolling_baseline_window_and_warmup():
    rb = RollingBaseline(window_sec=100.0)
    assert rb.value() == 0.0
    assert rb.ready(1) is False
    rb.add(10.0, 4.0)
    rb.add(20.0, 6.0)
    assert rb.value() == pytest.approx(5.0)
    assert rb.ready(2) is True and rb.ready(3) is False
    # окно 100с: add(115) → cut=15 → (10,4) выпадает, (20,6)+(115,12) остаются
    rb.add(115.0, 12.0)
    assert rb.value() == pytest.approx((6.0 + 12.0) / 2)


def test_rolling_baseline_ignores_nonpositive():
    rb = RollingBaseline(window_sec=100.0)
    rb.add(1.0, 0.0)
    rb.add(2.0, -5.0)
    assert rb.value() == 0.0 and rb.ready(1) is False


def test_density_arms_then_fires_after_persist():
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall()
    snap = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                 bids=bids, asks=asks)
    # t=0: стена замечена, но не выстояла persist_sec → входа нет
    assert st.update(snap, now=0.0) is None
    assert st.armed("SOLUSDT") is True
    # t=11: выстояла ≥10с и цена у стены → отскок LONG
    sig = st.update(snap, now=11.0)
    assert sig is not None
    assert sig.side == "long" and sig.strategy == "density_bounce"
    assert "density" in sig.reasons
    assert sig.sl_level < sig.entry_ref < sig.tp_level


def test_density_no_fire_when_price_far_from_wall():
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall()
    # цена в 0.5% от стены (>> near_bps 0.08%) → не входим
    snap = _snap([], last_price=100.6, best_bid=100.5, best_ask=100.6,
                 bids=bids, asks=asks)
    st.update(snap, now=0.0)
    assert st.update(snap, now=11.0) is None


def test_density_absorption_drops_wall():
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    big_bids, asks = _book_with_bid_wall(50.0)
    snap0 = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                  bids=big_bids, asks=asks)
    st.update(snap0, now=0.0)
    assert st.armed("SOLUSDT") is True
    # 40% стены съели за 2с (≥30% за <10с) → снять наблюдение (спуфинг)
    small_bids, _ = _book_with_bid_wall(30.0)
    snap1 = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                  bids=small_bids, asks=asks)
    st.update(snap1, now=2.0)
    assert st.armed("SOLUSDT") is False


def test_density_should_exit_when_wall_gone():
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    tr = SimpleNamespace(id=1, side="long", entry=100.10, sl=99.92, ts_open=0.0)
    bids_present, asks = _book_with_bid_wall(50.0)
    snap_ok = _snap([], last_price=100.05, bids=bids_present, asks=asks)
    # стена ещё на месте (в (sl, entry]) → держим
    assert st.should_exit(tr, snap_ok, now=20.0) is None
    # стена исчезла → density_gone
    flat_bids = [(100.0, 1), (99.99, 1), (99.98, 1), (99.97, 1), (99.96, 1)]
    snap_gone = _snap([], last_price=100.05, bids=flat_bids, asks=asks)
    decision = st.should_exit(tr, snap_gone, now=20.0)
    assert decision is not None and decision[0] == "density_gone"


def test_density_should_exit_respects_min_age():
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    tr = SimpleNamespace(id=1, side="long", entry=100.10, sl=99.92, ts_open=0.0)
    flat_bids = [(100.0, 1), (99.99, 1), (99.98, 1), (99.97, 1), (99.96, 1)]
    snap_gone = _snap([], last_price=100.05, bids=flat_bids,
                      asks=_book_with_bid_wall()[1])
    # возраст 5с < 10с → не дёргаемся даже если стены нет
    assert st.should_exit(tr, snap_gone, now=5.0) is None


# ─── v0.18.30: редизайн трека стены density_bounce (2026-07-02) ─────────────
# Аудит: 0 сигналов за 24 дня после persist 20м — трек требовал «стена =
# максимум книги с float-точностью каждый тик 1200с подряд».

def test_density_track_survives_bigger_wall_elsewhere():
    """Чужая крупная лимитка в другом месте книги НЕ рвёт трек: идентичность
    = якорь ± tolerance, уровень не обязан быть максимумом стороны. Старый код
    (detect_wall каждый тик) сбросил бы first_seen → persist никогда не
    добегал."""
    cfg = _density_cfg(density_wall_mult=3.0)
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall(50.0)
    snap0 = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                  bids=bids, asks=asks)
    st.update(snap0, now=0.0)
    assert st.armed("SOLUSDT") is True
    # t=5: у 99.90 (вне tolerance 5 б.п. от якоря 100.0) встала лимитка КРУПНЕЕ
    # стены — максимум стороны теперь не наш якорь
    bids_flicker = [(100.0, 50.0), (99.99, 1), (99.98, 1), (99.97, 1),
                    (99.90, 60.0)]
    snap1 = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                  bids=bids_flicker, asks=asks)
    st.update(snap1, now=5.0)
    assert st.armed("SOLUSDT") is True  # трек жив, persist НЕ сброшен
    # t=11: persist 10с добежал (от t=0!) → отскок
    sig = st.update(snap1, now=11.0)
    assert sig is not None and sig.side == "long"
    assert sig.strategy == "density_bounce"


def test_density_track_grace_survives_brief_vanish():
    """Кратковременное пропадание уровня (WS-чурн/айсберг-рефил) ≤ grace НЕ
    убивает трек; persist считается от ПЕРВОГО появления."""
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall(50.0)
    snap_wall = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                      bids=bids, asks=asks)
    flat = [(100.0, 1), (99.99, 1), (99.98, 1), (99.97, 1), (99.96, 1)]
    snap_flat = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                      bids=flat, asks=asks)
    st.update(snap_wall, now=0.0)
    st.update(snap_flat, now=5.0)   # уровень мигнул (5с < grace 10с)
    assert st.armed("SOLUSDT") is True
    st.update(snap_wall, now=8.0)   # вернулся → miss снят
    sig = st.update(snap_wall, now=12.0)  # persist 10с от t=0 добежал
    assert sig is not None and sig.side == "long"


def test_density_track_dies_after_grace_exceeded():
    """Пропадание уровня ДОЛЬШЕ grace = реальное снятие/пробой → трек умирает.
    Во время grace-паузы вход не делается (не входим вслепую)."""
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall(50.0)
    snap_wall = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                      bids=bids, asks=asks)
    flat = [(100.0, 1), (99.99, 1), (99.98, 1), (99.97, 1), (99.96, 1)]
    snap_flat = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                      bids=flat, asks=asks)
    st.update(snap_wall, now=0.0)
    assert st.update(snap_flat, now=11.0) is None  # miss (persist прошёл) — не входим
    st.update(snap_flat, now=22.0)  # 11с без уровня > grace 10с → смерть
    assert st.armed("SOLUSDT") is False


def test_density_sliding_absorption_late_in_track_life():
    """Анти-абсорбция СКОЛЬЗЯЩАЯ: ≥30% съедено за ≤10с относительно недавнего
    пика убивает трек В ЛЮБОЙ момент жизни. Старый код сравнивал с size0 и
    только в первые absorb_window от first_seen — при persist 20м проверка
    была мертва после первых 10 секунд."""
    cfg = _density_cfg(density_bounce_persist_sec=1200.0)
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    big, asks = _book_with_bid_wall(50.0)
    small, _ = _book_with_bid_wall(30.0)
    mk = lambda b: _snap([], last_price=100.05, best_bid=100.0,
                         best_ask=100.10, bids=b, asks=asks)
    st.update(mk(big), now=0.0)
    st.update(mk(big), now=300.0)   # 5 минут живёт, размер стабилен
    assert st.armed("SOLUSDT") is True
    st.update(mk(small), now=305.0)  # −40% от пика за 5с → поглощение
    assert st.armed("SOLUSDT") is False


def test_density_bounce_entry_order_type_override():
    """v0.18.30: bounce входит taker (пер-стратегийный override), даже когда
    глобальный вход maker. История: maker не наливался (12/33 = 36%
    entry_Cancelled/timeout)."""
    cfg = _density_cfg(entry_order_type="post_only_limit",
                       density_bounce_entry_order_type="market")
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall(50.0)
    snap = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                 bids=bids, asks=asks)
    st.update(snap, now=0.0)
    sig = st.update(snap, now=11.0)
    assert sig is not None and sig.entry_order_type == "market"


# ─── v0.18.32: lifecycle-телеметрия треков density_bounce → density_tracks ──

def test_density_lifecycle_absorbed_death_emitted():
    """Стена, поглощенная ≥absorb_frac за ≤window → трек умирает с
    death_reason='absorbed'; lifecycle-строка попадает в drain_lifecycle()."""
    cfg = _density_cfg(density_bounce_persist_sec=1200.0)
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    big, asks = _book_with_bid_wall(50.0)
    small, _ = _book_with_bid_wall(30.0)
    mk = lambda b: _snap([], last_price=100.05, best_bid=100.0,
                         best_ask=100.10, bids=b, asks=asks)
    st.update(mk(big), now=0.0)     # старт трека
    st.update(mk(big), now=300.0)   # живёт
    st.drain_lifecycle()            # сброс — старт не эмитится (трек жив)
    st.update(mk(small), now=305.0)  # −40% за 5с → поглощение
    rows = st.drain_lifecycle()
    assert len(rows) == 1
    r = rows[0]
    assert r["death_reason"] == "absorbed"
    assert r["book_side"] == "bid"
    assert r["anchor_price"] == 100.0
    assert r["life_sec"] == pytest.approx(305.0)
    assert r["reached_persist"] == 0   # persist 1200с — не дожил
    assert r["price_start"] == 100.05
    assert r["price_end"] == 100.05
    assert r["max_size"] == 50.0


def test_density_lifecycle_vanished_death_emitted():
    """Стена, исчезнувшая > grace → death_reason об уровне исчезшем."""
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall(50.0)
    snap_wall = _snap([], last_price=100.05, bids=bids, asks=asks)
    flat = [(100.0, 1), (99.99, 1), (99.98, 1), (99.97, 1), (99.96, 1)]
    snap_flat = _snap([], last_price=100.05, bids=flat, asks=asks)
    st.update(snap_wall, now=0.0)
    st.drain_lifecycle()
    st.update(snap_flat, now=11.0)   # miss (persist 10с прошёл, но уровня нет)
    st.update(snap_flat, now=22.0)   # 11с > grace 10с → смерть
    rows = st.drain_lifecycle()
    assert len(rows) == 1
    assert rows[0]["death_reason"].startswith("уровень исчез")
    assert rows[0]["reached_persist"] == 1  # persist=10с, дожил (first_seen 0 → 11с)


def test_density_lifecycle_persist_and_approach_fields():
    """Трек, доживший до persist: reached_persist=1, persisted_ts/price_persist
    заполнены. Цена FAR → did_price_approach=0, выстрела нет (трек жив,
    wallstate инспектируем). Затем убиваем и проверяем lifecycle-строку."""
    cfg = _density_cfg(density_bounce_persist_sec=10.0)
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall(50.0)
    # цена ДАЛЕКО от стены 100.0 (last 101.0 → 100 bps > 8 bps) — подхода нет
    snap_far = _snap([], last_price=101.0, best_bid=100.0, best_ask=100.10,
                     bids=bids, asks=asks)
    st.update(snap_far, now=0.0)     # старт
    st.update(snap_far, now=11.0)    # persist 10с прошёл, цена далеко → ждём
    w = st._track["SOLUSDT"]["bid"]
    assert w is not None
    assert w["persisted_ts"] == 11.0
    assert w["price_persist"] == 101.0
    assert w["did_price_approach"] == 0
    # теперь подгоним цену к стене → подход, выстрел
    snap_near = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                      bids=bids, asks=asks)
    sig = st.update(snap_near, now=12.0)
    assert sig is not None  # выстрелил — подтверждает persisted-трек стреляет


def test_db_density_tracks_insert_and_read(tmp_path):
    db = ScalpDB(str(tmp_path))
    db.insert_density_track({
        "ts_start": 0.0, "ts_end": 305.0, "symbol": "SOLUSDT",
        "book_side": "bid", "anchor_price": 100.0, "life_sec": 305.0,
        "death_reason": "absorbed", "reached_persist": 0, "persisted_ts": None,
        "price_start": 100.05, "price_persist": None, "price_end": 100.05,
        "did_price_approach": 0, "max_size": 50.0, "round_tier": "round00",
    })
    rows = db.density_track_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["death_reason"] == "absorbed"
    assert r["anchor_price"] == 100.0
    assert r["reached_persist"] == 0
    assert r["round_tier"] == "round00"
    db.close()


def test_db_density_tracks_failure_does_not_raise(tmp_path):
    """Телеметрия — не рвёт торговый поток: кривая строка глушится."""
    db = ScalpDB(str(tmp_path))
    db.insert_density_track({})  # нет обязательных полей → NOT NULL fail
    # не бросает, просто rollback
    assert db.density_track_rows() == []
    db.close()


def test_settings_density_track_log_enabled_default():
    from scalp_bot.config.settings import ScalpSettings
    assert ScalpSettings().density_track_log_enabled is True


# ─── v0.18.15: near_round-демоция + пер-стратегийный persist (density_bounce) ──

def _non_round_bid_wall():
    """bid-стена у НЕ-круглой цены 103.7 (near_round_hier → None)."""
    bids = [(103.7, 50.0), (103.69, 1), (103.68, 1), (103.67, 1), (103.66, 1)]
    asks = [(103.80, 1), (103.81, 1), (103.82, 1), (103.83, 1), (103.84, 1)]
    return bids, asks


def test_near_round_hier_recognizes_half_levels():
    """v0.18.15: иерархический детектор — 00 и 50 уровни (Bloomfield-Chin-Craig/
    Osler), в отличие от строгого near_round (только 00). Не ¼ (дискриминативность)."""
    assert near_round_hier(100.0, 0.001) == "round00"     # шаг 10 → 100 = 00
    assert near_round_hier(105.0, 0.001) == "round50"     # ½-шаг 5 → 105, не 00
    assert near_round_hier(103.7, 0.001) is None          # ни 00, ни 50
    # дорогая монета: ½-уровень $63 500 ловится (старый near_round видел лишь $1000)
    assert near_round(63500.0, 0.001) is False            # шаг 1000 → ближ. 64000
    assert near_round_hier(63500.0, 0.001) == "round50"   # ½-шаг 500 → 63500


# ─── v0.18.26: база sweep_fade — skip-round gate + full reclaim (изоляция) ──

def test_detector_skip_round_blocks_arm():
    """round_gate=True → детектор НЕ взводится на свипе у round-уровня (B).
    Артефакт: scalp_backtest_regime --level-decomp (round хуже микро)."""
    det = SweepReclaimDetector("SOLUSDT", _cfg(), round_gate=lambda s: True)
    assert det.update(_snap(_arm_samples(), last_price=96.5), now=100.0) is None
    assert det.armed is False


def test_detector_round_gate_none_arms_normally():
    """round_gate=None (canon/дефолт) → взвод как обычно, gate не вмешивается."""
    det = SweepReclaimDetector("SOLUSDT", _cfg(), round_gate=None)
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)
    assert det.armed is True


def test_detector_reclaim_frac_override():
    """Пер-детекторный reclaim_frac перекрывает глобальный cfg.reclaim_frac."""
    base_cfg = _cfg(reclaim_frac=0.5)
    assert SweepReclaimDetector("S", base_cfg)._rf() == 0.5
    assert SweepReclaimDetector("S", base_cfg, reclaim_frac=1.0)._rf() == 1.0


def test_base_sweep_fade_wires_skip_round_and_reclaim():
    """База sweep_fade: skip_round=True → round_gate стоит; reclaim=1.0."""
    cfg = _cfg(sweep_fade_skip_round=True, sweep_fade_reclaim_frac=1.0,
               density_round_frac=0.003)
    det = SweepFadeStrategy(cfg, ["SOLUSDT"])._det["SOLUSDT"]
    assert det.round_gate is not None
    assert det._rf() == 1.0
    assert det.round_gate(96.5) is True     # round50 → блок
    assert det.round_gate(50.25) is False   # ни 00, ни 50 → пропуск


def test_base_sweep_fade_skip_round_off_rollback():
    """SCALP_SWEEP_FADE_SKIP_ROUND=false → round_gate снят (путь отката)."""
    cfg = _cfg(sweep_fade_skip_round=False, sweep_fade_reclaim_frac=0.5)
    det = SweepFadeStrategy(cfg, ["SOLUSDT"])._det["SOLUSDT"]
    assert det.round_gate is None
    assert det._rf() == 0.5


def test_canon_isolated_from_base_round_and_reclaim():
    """ИЗОЛЯЦИЯ: canon НЕ скипает round и читает СВОЙ reclaim, несмотря на
    включённые base-флаги (skip_round=True, base reclaim=0.5)."""
    cfg = _cfg(sweep_fade_skip_round=True, sweep_fade_reclaim_frac=0.5,
               density_round_frac=0.003,
               sweep_fade_canon_reclaim_frac=1.0,
               sweep_fade_canon_symbol_list=["SOLUSDT"],
               sweep_fade_canon_entry_order_type="market")
    det = SweepFadeCanonStrategy(cfg, ["SOLUSDT"])._det["SOLUSDT"]
    assert det.round_gate is None          # canon фейдит round намеренно
    assert det._rf() == 1.0                # свой reclaim, не base 0.5


def test_density_bounce_fires_on_non_round_wall():
    """v0.18.15: near_round БОЛЬШЕ НЕ ГЕЙТ — стена у НЕ-круглой цены всё равно
    даёт вход (density+persist обязательны, round — опц. бонус). reasons без round,
    score=2. Практики гейтят размером+persist, не круглым уровнем."""
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _non_round_bid_wall()
    snap = _snap([], last_price=103.75, best_bid=103.70, best_ask=103.80,
                 bids=bids, asks=asks)
    assert st.update(snap, now=0.0) is None        # ещё не выстояла
    sig = st.update(snap, now=11.0)                # выстояла ≥10с
    assert sig is not None and sig.side == "long"
    assert sig.reasons == ["density", "persist"]   # round НЕТ (не круглая)
    assert sig.score == 2


def test_density_bounce_round_adds_bonus_reason():
    """v0.18.15: круглый уровень — confluence-бонус (+reason, score 2→3),
    а не обязательное условие."""
    cfg = _density_cfg()
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall()             # стена у 100.0 (круглая)
    snap = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                 bids=bids, asks=asks)
    st.update(snap, now=0.0)
    sig = st.update(snap, now=11.0)
    assert sig is not None
    assert "round00" in sig.reasons and "density" in sig.reasons
    assert sig.score == 3


def test_density_bounce_uses_own_persist_window():
    """v0.18.15: bounce уважает density_bounce_persist_sec, а НЕ базовый
    density_persist_sec (изоляция от density_break). Окно 30с: при t=11 (>10 базы,
    <30 bounce) входа нет; при t=31 — есть."""
    cfg = _density_cfg(density_bounce_persist_sec=30.0, density_persist_sec=10.0)
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall()
    snap = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                 bids=bids, asks=asks)
    st.update(snap, now=0.0)
    assert st.update(snap, now=11.0) is None       # базовое окно прошло, bounce нет
    assert st.update(snap, now=31.0) is not None   # bounce-окно (30с) выстояно


# ─── density_break (Фаза 3): выстоявшая стена пробита → прострел (momentum) ──

def _ask_wall_book(wall_size=50.0):
    """ask-стена (сопротивление) у 100.0; bids плоские (без стены)."""
    asks = [(100.0, wall_size), (100.01, 1), (100.02, 1), (100.03, 1), (100.04, 1)]
    bids = [(99.95, 1), (99.94, 1), (99.93, 1), (99.92, 1), (99.91, 1)]
    return bids, asks


def _flat_book_above():
    """книга без стены, цена ушла выше 100.0 (стену съели)."""
    asks = [(100.05, 1), (100.06, 1), (100.07, 1), (100.08, 1), (100.09, 1)]
    bids = [(100.29, 1), (100.28, 1), (100.27, 1), (100.26, 1), (100.25, 1)]
    return bids, asks


def _persist_then(st, bids, asks, last):
    """Прогон: взвести наблюдение (t=0), дать стене выстоять (t=15)."""
    st.update(_snap([], last_price=last, bids=bids, asks=asks), now=0.0)
    st.update(_snap([], last_price=last, bids=bids, asks=asks), now=15.0)


def test_density_break_fires_long_on_ask_wall_break():
    cfg = _density_cfg()
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)           # ask-стена 100.0 выстояла
    flat_bids, flat_asks = _flat_book_above()           # стену съели, цена пробила
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    sig = st.update(snap, now=16.0)
    assert sig is not None and sig.side == "long"
    assert "wall_break" in sig.reasons and sig.strategy == "density_break"
    assert sig.sl_level < 100.0 < sig.entry_ref          # SL за пробитым уровнем


def test_density_break_fires_short_on_bid_wall_break():
    cfg = _density_cfg()
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall(50.0)               # bid-стена 100.0 (поддержка)
    _persist_then(st, bids, asks, last=100.04)
    flat_bids = [(99.69, 1), (99.68, 1), (99.67, 1), (99.66, 1), (99.65, 1)]
    flat_asks = [(99.71, 1), (99.72, 1), (99.73, 1), (99.74, 1), (99.75, 1)]
    snap = _snap([], last_price=99.7, best_bid=99.69, best_ask=99.71,
                 bids=flat_bids, asks=flat_asks)
    sig = st.update(snap, now=16.0)
    assert sig is not None and sig.side == "short"
    assert sig.sl_level > 100.0 > sig.entry_ref          # SL за пробитым уровнем


def test_density_break_no_fire_on_spoof_wall():
    # стена мелькнула и исчезла ДО persist (t=3 < 10) → спуфинг, не торгуем
    cfg = _density_cfg()
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    st.update(_snap([], last_price=99.96, bids=bids, asks=asks), now=0.0)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    assert st.update(snap, now=3.0) is None              # не выстояла → нет входа


def test_density_break_no_fire_when_price_not_broken():
    # стена выстояла и снята, но цена НЕ пробила уровень (спуфинг-пулл) → пропуск
    cfg = _density_cfg()
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_asks = [(100.06, 1), (100.07, 1), (100.08, 1), (100.09, 1), (100.10, 1)]
    snap = _snap([], last_price=99.96, best_bid=99.95, best_ask=99.97,
                 bids=bids, asks=flat_asks)              # стены нет, но цена < 100.0
    assert st.update(snap, now=16.0) is None


def test_density_break_still_gates_on_near_round():
    """Guard v0.18.15: демоция near_round коснулась ТОЛЬКО density_bounce.
    density_break по-прежнему ГЕЙТит стену строгим near_round — НЕ-круглая стена
    не отслеживается, пробой не торгуется (изоляция страт)."""
    cfg = _density_cfg()
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    asks = [(103.7, 50.0), (103.71, 1), (103.72, 1), (103.73, 1), (103.74, 1)]
    bids = [(103.65, 1), (103.64, 1), (103.63, 1), (103.62, 1), (103.61, 1)]
    _persist_then(st, bids, asks, last=103.66)           # не-круглая → не трекается
    flat_bids = [(103.99, 1), (103.98, 1), (103.97, 1), (103.96, 1), (103.95, 1)]
    flat_asks = [(104.01, 1), (104.02, 1), (104.03, 1), (104.04, 1), (104.05, 1)]
    snap = _snap([], last_price=104.0, best_bid=103.99, best_ask=104.01,
                 bids=flat_bids, asks=flat_asks)
    assert st.update(snap, now=16.0) is None


def test_density_break_ignores_bounce_persist_window():
    """Guard v0.18.15: density_break использует density_persist_sec, а НЕ
    density_bounce_persist_sec. С огромным bounce-окном пробой всё равно срабатывает
    по базовому окну (10с) — окна изолированы."""
    cfg = _density_cfg(density_bounce_persist_sec=99999.0, density_persist_sec=10.0)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    sig = st.update(snap, now=16.0)
    assert sig is not None and sig.side == "long"


# ─── v0.18.16 (C-06): density_break taker-вход + CVD-confirmation ложного пробоя ─

def test_build_signal_order_type_override_taker_vs_maker():
    """v0.18.16: order_type override в build_signal. taker (market) → long на
    best_ask; maker (post_only_limit) → long на best_bid. Signal несёт тип."""
    snap = _snap(_long_samples(), best_bid=96.9, best_ask=97.1)
    sig_t = build_signal(snap, "long", 96.5, _cfg(entry_order_type="post_only_limit"),
                         4, ["x"], order_type="market")
    assert sig_t is not None
    assert sig_t.entry_order_type == "market" and sig_t.entry_ref == 97.1
    sig_m = build_signal(snap, "long", 96.5, _cfg(entry_order_type="market"),
                         4, ["x"], order_type="post_only_limit")
    assert sig_m is not None
    assert sig_m.entry_order_type == "post_only_limit" and sig_m.entry_ref == 96.9


def test_density_break_entry_is_taker_even_if_global_maker():
    """v0.18.16 (C-06): density_break входит TAKER даже при глобальном maker —
    пробой не наливается лимиткой на своей стороне (fill-rate 42.6%)."""
    cfg = _density_cfg(entry_order_type="post_only_limit",
                       density_break_entry_order_type="market")
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    sig = st.update(snap, now=16.0)
    assert sig is not None and sig.side == "long"
    assert sig.entry_order_type == "market"
    assert sig.entry_ref == 100.31          # taker long → best_ask, не best_bid


def test_density_break_confirm_cvd_blocks_grab():
    """v0.18.16 (C-06): пробой БЕЗ follow-through CVD = вероятный liquidity-grab →
    вход блокируется (канон «volume = truth serum»). Фильтр на ВСЕХ монетах."""
    cfg = _density_cfg(density_break_confirm_cvd=True)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    grab_cvd = [CvdSample(14, 100, 10), CvdSample(15, 100, 5), CvdSample(16, 100, 0)]
    snap = _snap(grab_cvd, last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    assert st.update(snap, now=16.0) is None


def test_density_break_confirm_cvd_allows_followthrough():
    """v0.18.16 (C-06): пробой С follow-through CVD (поток в сторону) → вход."""
    cfg = _density_cfg(density_break_confirm_cvd=True)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    ft_cvd = [CvdSample(14, 100, 0), CvdSample(15, 100, 5), CvdSample(16, 100, 10)]
    snap = _snap(ft_cvd, last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    sig = st.update(snap, now=16.0)
    assert sig is not None and sig.side == "long"


def test_density_break_confirm_cvd_off_is_legacy():
    """confirm_cvd=False → вход на пробое без CVD (legacy, обратная совместимость)."""
    cfg = _density_cfg(density_break_confirm_cvd=False)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    assert st.update(snap, now=16.0) is not None


def test_density_break_ob_gate_blocks_absorption():
    """v0.18.16 (C-06 #3, КАНОН): пробой при resting-стакане ПРОТИВ (ob_imb<min) =
    абсорбция глубокой книги / grab → вход блокируется. Едино для всех монет."""
    cfg = _density_cfg(density_break_require_ob=True)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks, ob_imbalance=0.40)  # против long
    assert st.update(snap, now=16.0) is None


def test_density_break_ob_gate_allows_supportive_book():
    """v0.18.16 (C-06 #3): resting-стакан поддерживает пробой (ob_imb≥min) → вход."""
    cfg = _density_cfg(density_break_require_ob=True)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks, ob_imbalance=0.62)  # за long
    sig = st.update(snap, now=16.0)
    assert sig is not None and sig.side == "long"


def test_density_break_ob_gate_off_is_legacy():
    """require_ob=False → вход без ob-гейта (legacy)."""
    cfg = _density_cfg(density_break_require_ob=False)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks, ob_imbalance=0.40)
    assert st.update(snap, now=16.0) is not None


# ─── v0.18.25 (V1): close-confirmation density_break (канон «не first-touch») ──

def test_density_break_no_fire_on_first_touch():
    """V1: с confirm_bar>0 пробой НЕ входит на первом касании — армится и ждёт
    закрытия бара (канон C-06: avoid entering on the first touch)."""
    cfg = _density_cfg(density_break_confirm_bar_sec=60.0)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    assert st.update(snap, now=16.0) is None          # армлен, входа на тике нет
    assert st._pending.get("SOLUSDT") is not None      # ждёт закрытия бара


def test_density_break_fires_after_bar_close_still_beyond():
    """V1: пробой подтверждён — на ЗАКРЫТИИ бара цена всё ещё за уровнем → вход."""
    cfg = _density_cfg(density_break_confirm_bar_sec=60.0)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    arm = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                bids=flat_bids, asks=flat_asks)
    assert st.update(arm, now=16.0) is None            # арм в баре 0
    close = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                  bids=flat_bids, asks=flat_asks)
    sig = st.update(close, now=70.0)                    # бар закрылся (бар 1)
    assert sig is not None and sig.side == "long"
    assert "wall_break" in sig.reasons and sig.strategy == "density_break"
    assert sig.sl_level < 100.0 < sig.entry_ref


def test_density_break_fake_breakout_returns_inside():
    """V1: first-touch фейкаут — к закрытию бара цена вернулась за уровень → отбой,
    входа нет, pending снят."""
    cfg = _density_cfg(density_break_confirm_bar_sec=60.0)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    arm = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                bids=flat_bids, asks=flat_asks)
    assert st.update(arm, now=16.0) is None            # арм в баре 0
    # к закрытию бара цена ушла ОБРАТНО под уровень 100.0 → фейкаут
    back = _snap([], last_price=99.90, best_bid=99.89, best_ask=99.91,
                 bids=bids, asks=asks)
    assert st.update(back, now=70.0) is None
    assert st._pending.get("SOLUSDT") is None          # отбой, наблюдение снято


def test_density_break_confirm_bar_zero_is_legacy_first_touch():
    """V1: confirm_bar=0 → legacy-режим (вход на первом тике пробоя)."""
    cfg = _density_cfg(density_break_confirm_bar_sec=0.0)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    sig = st.update(snap, now=16.0)
    assert sig is not None and sig.side == "long"      # тиковый вход (legacy)


# ─── v0.18.29: per-strategy no-trade blacklist (изолировано от вселенной) ──────

def test_density_break_no_trade_blocks_blacklisted_symbol():
    """Монета в no-trade → density_break НЕ генерит сигнал даже на полном
    валидном пробое (setup, на котором SOLUSDT бы вошёл)."""
    cfg = _density_cfg(density_break_confirm_bar_sec=0.0,
                       density_break_no_trade_list=["BTCUSDT"])
    st = DensityBreakStrategy(cfg, ["BTCUSDT"])
    bids, asks = _ask_wall_book(50.0)
    # persist + пробой с ЯВНЫМ символом BTCUSDT (no-trade должен срезать на входе)
    st.update(_snap([], symbol="BTCUSDT", last_price=99.96, bids=bids, asks=asks), now=0.0)
    st.update(_snap([], symbol="BTCUSDT", last_price=99.96, bids=bids, asks=asks), now=15.0)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], symbol="BTCUSDT", last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    assert st.update(snap, now=16.0) is None            # no-trade → нет входа


def test_density_break_no_trade_isolated_other_symbols_trade():
    """Изоляция: no-trade блокирует ТОЛЬКО чёрные символы — SOLUSDT (не в списке)
    на том же setup входит нормально. Blacklist не калечит остальную вселенную."""
    cfg = _density_cfg(density_break_confirm_bar_sec=0.0,
                       density_break_no_trade_list=["BTCUSDT", "ZECUSDT", "TAOUSDT"])
    st = DensityBreakStrategy(cfg, ["SOLUSDT", "BTCUSDT"])
    # SOLUSDT — не в blacklist → полный валидный пробой должен войти
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)           # persist на дефолтном SOLUSDT
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], symbol="SOLUSDT", last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    sig = st.update(snap, now=16.0)
    assert sig is not None and sig.side == "long" and sig.symbol == "SOLUSDT"
    # BTCUSDT — в blacklist → тот же setup не даёт сигнала (no-trade срезает до трека)
    snap_b = _snap([], symbol="BTCUSDT", last_price=100.3, best_bid=100.29, best_ask=100.31,
                   bids=flat_bids, asks=flat_asks)
    assert st.update(snap_b, now=17.0) is None


def test_density_break_no_trade_empty_is_legacy():
    """Пустой blacklist → нет блокировки (legacy-поведение, reversible via env)."""
    cfg = _density_cfg(density_break_confirm_bar_sec=0.0,
                       density_break_no_trade_list=[])
    st = DensityBreakStrategy(cfg, ["BTCUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                 bids=flat_bids, asks=flat_asks)
    assert st.update(snap, now=16.0) is not None        # пустой список → входит


def test_density_break_no_trade_prod_defaults():
    """Prod-дефолт ScalpSettings содержит BTC/ZEC/TAO (data-driven решение)."""
    from scalp_bot.config.settings import ScalpSettings
    cfg = ScalpSettings()
    assert set(cfg.density_break_no_trade_list) == {"BTCUSDT", "ZECUSDT", "TAOUSDT"}
    st = DensityBreakStrategy(cfg, ["BTCUSDT"])
    assert "BTCUSDT" in st._no_trade


# ─── ИЗОЛЯЦИЯ v0.18.16: sweep_fade и density_bounce НЕ задеты ────────────────

def test_sweep_fade_unaffected_by_v0_18_16():
    """Изоляция: sweep_fade НЕ задет taker/CVD/ob-правками density_break — его сигнал
    несёт entry_order_type=None (executor → глобальный maker), даже когда в cfg есть
    density_break_* флаги."""
    cfg = _cfg(require_ob_imbalance=True, density_break_entry_order_type="market",
               density_break_confirm_cvd=True, density_break_require_ob=True)
    det = SweepReclaimDetector("SOLUSDT", cfg)
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)
    sig = det.update(_snap(_fire_samples(), last_price=97.6, ob_imbalance=0.62),
                     now=130.0)
    assert sig is not None and sig.strategy == "sweep_fade"
    assert sig.entry_order_type is None        # глобальный maker, НЕ taker


def test_density_bounce_unaffected_by_v0_18_16():
    """Изоляция: density_bounce НЕ гейтится CVD/ob-правками density_break
    (пустой CVD + ob против НЕ блокируют вход bounce). v0.18.30: bounce несёт
    СВОЙ entry_order_type (market) — от break-полей по-прежнему не зависит."""
    cfg = _density_cfg(density_break_entry_order_type="market",
                       density_break_confirm_cvd=True, density_break_require_ob=True)
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _non_round_bid_wall()
    # пустой CVD (CVD-гейт заблокировал бы) + ob=0.40 против long (ob-гейт заблокировал бы)
    snap = _snap([], last_price=103.75, best_bid=103.70, best_ask=103.80,
                 bids=bids, asks=asks, ob_imbalance=0.40)
    st.update(snap, now=0.0)
    sig = st.update(snap, now=11.0)
    assert sig is not None and sig.side == "long" and sig.strategy == "density_bounce"
    # v0.18.30: свой taker-override (density_bounce_entry_order_type)
    assert sig.entry_order_type == "market"


def test_build_strategies_two():
    cfg = SimpleNamespace(strategy_list=["sweep_fade", "density_bounce"])
    strats = build_strategies(cfg, ["SOLUSDT"])
    assert [s.name for s in strats] == ["sweep_fade", "density_bounce"]


def test_build_strategies_registers_density_break():
    cfg = SimpleNamespace(
        strategy_list=["sweep_fade", "density_bounce", "density_break"])
    strats = build_strategies(cfg, ["SOLUSDT"])
    assert [s.name for s in strats] == [
        "sweep_fade", "density_bounce", "density_break"]


def test_ensure_symbols_additive_and_idempotent():
    cfg = SimpleNamespace(strategy_list=["sweep_fade", "density_bounce"],
                          **_density_cfg().__dict__)
    strats = build_strategies(cfg, ["AAAUSDT"])
    for s in strats:
        s.ensure_symbols(["BBBUSDT", "AAAUSDT"])  # новый + уже известный
        s.ensure_symbols(["BBBUSDT"])             # повторно — без дублей/ошибок
        assert s.armed("BBBUSDT") is False        # символ известен, не взведён
        assert s.armed("AAAUSDT") is False


# ─── авто-селектор вселенной (data/universe.py) ─────────────────────────────

def _ticker(sym, last, hi, lo, turnover, bid=None, ask=None, pre=""):
    return {"symbol": sym, "lastPrice": str(last), "highPrice24h": str(hi),
            "lowPrice24h": str(lo), "turnover24h": str(turnover),
            "bid1Price": "" if bid is None else str(bid),
            "ask1Price": "" if ask is None else str(ask),
            "curPreListingPhase": pre}


def test_rank_universe_filters_and_sorts_by_range():
    tickers = [
        _ticker("HYPEUSDT", 66.0, 72.0, 60.0, 800e6),     # range 18.2%
        _ticker("NEARUSDT", 2.4, 2.8, 2.4, 250e6),         # range 16.7%
        _ticker("BTCUSDT", 100000, 102500, 100000, 5e9),   # range 2.5% < floor
        _ticker("PUMPUSDT", 1.0, 1.45, 1.0, 200e6),        # range 45% > cap
        _ticker("THINUSDT", 5.0, 6.0, 5.0, 50e6),          # turnover < floor
        _ticker("ETHUSDC", 3000, 3600, 3000, 1e9),         # не USDT
    ]
    picked = rank_universe(tickers, top_n=5, min_turnover=150e6,
                           min_range_pct=6.0, max_range_pct=30.0,
                           max_spread_bps=5.0)
    assert picked == ["HYPEUSDT", "NEARUSDT"]  # по range% убыв.


def test_rank_universe_top_n_cap():
    tickers = [_ticker(f"C{i}USDT", 10, 12, 10, 200e6) for i in range(8)]
    picked = rank_universe(tickers, top_n=3, min_turnover=150e6,
                           min_range_pct=6.0, max_range_pct=30.0,
                           max_spread_bps=0.0)
    assert len(picked) == 3


def test_rank_universe_spread_cap():
    wide = _ticker("WIDEUSDT", 100, 110, 100, 200e6, bid=99.0, ask=100.0)
    assert rank_universe([wide], top_n=5, min_turnover=150e6, min_range_pct=6.0,
                         max_range_pct=30.0, max_spread_bps=5.0) == []
    tight = _ticker("OKUSDT", 100, 110, 100, 200e6, bid=99.99, ask=100.0)
    assert rank_universe([tight], top_n=5, min_turnover=150e6, min_range_pct=6.0,
                         max_range_pct=30.0, max_spread_bps=5.0) == ["OKUSDT"]


def test_rank_universe_skips_pre_listing_and_bad_rows():
    pre = _ticker("NEWUSDT", 10, 12, 10, 200e6, pre="Phase1")
    bad = {"symbol": "BADUSDT", "lastPrice": "0", "highPrice24h": "1",
           "lowPrice24h": "0", "turnover24h": "200000000"}
    assert rank_universe([pre, bad], top_n=5, min_turnover=150e6,
                         min_range_pct=6.0, max_range_pct=30.0,
                         max_spread_bps=5.0) == []


def test_rank_universe_composite_prefers_liquid_over_thin_volatile():
    # A: range 11% но turnover 1000M (ликвидная); B: range 12% но turnover 160M
    # (тоньше). Старая логика (sort by range) дала бы B первой; композит ставит
    # ликвидную A выше — меньше слиппедж/стоп-аутов (research проф-скальперов).
    tickers = [
        _ticker("ALIQUSDT", 100, 111, 100, 1000e6),   # range 11%
        _ticker("BVOLUSDT", 100, 112, 100, 160e6),    # range 12%
        _ticker("CLOWUSDT", 100, 106.5, 100, 200e6),  # range 6.5%
    ]
    picked = rank_universe(tickers, top_n=10, min_turnover=150e6,
                           min_range_pct=6.0, max_range_pct=30.0,
                           max_spread_bps=5.0)
    assert picked == ["ALIQUSDT", "BVOLUSDT", "CLOWUSDT"]


def test_rank_universe_no_cap_when_top_n_zero():
    # top_n<=0 → без лимита: берём ВСЕ прошедшие фильтр (качество, не число)
    tickers = [_ticker(f"C{i}USDT", 10, 11 + i * 0.1, 10, (200 + i) * 1e6)
               for i in range(8)]
    picked = rank_universe(tickers, top_n=0, min_turnover=150e6,
                           min_range_pct=6.0, max_range_pct=30.0,
                           max_spread_bps=0.0)
    assert len(picked) == 8


def _kl5m(ranges_pct: list[float], price: float = 100.0) -> list[list]:
    """Синтетические 5м-свечи (DESC, новые сверху) с заданной %-амплитудой каждого
    бара. ranges_pct[0] — самый СВЕЖИЙ бар. Возвращаем список из window-блоков
    (по 12 баров на «час») — каждый элемент списка задаёт амплитуду блока."""
    bars = []
    # ranges_pct трактуем как амплитуду каждого 12-барного блока: разворачиваем в
    # 12 одинаковых баров на блок (новые сверху)
    for rp in ranges_pct:  # от свежего к старому
        rng = price * rp / 100.0
        for _ in range(12):
            bars.append([0, str(price), str(price + rng / 2), str(price - rng / 2),
                         str(price)])
    return bars  # уже DESC (свежие блоки первыми)


def test_hourly_range_rvol_hot_and_quiet():
    from scalp_bot.data.universe import hourly_range_rvol
    # текущий час амплитуда 4%, исторические по 1% → RVOL ≈ 4
    kl = _kl5m([4.0] + [1.0] * 24)
    v = hourly_range_rvol(kl)
    assert v is not None and v == pytest.approx(4.0, abs=0.2)
    # текущий час тише истории: 0.5% против 2% → RVOL ≈ 0.25 (затихла)
    kl2 = _kl5m([0.5] + [2.0] * 24)
    v2 = hourly_range_rvol(kl2)
    assert v2 is not None and v2 < 1.0


def test_hourly_range_rvol_insufficient_data():
    from scalp_bot.data.universe import hourly_range_rvol
    assert hourly_range_rvol([]) is None
    assert hourly_range_rvol(_kl5m([3.0])) is None  # только текущий блок, нет истории


def test_rank_rows_uses_fresh_vol_metric_over_24h():
    from scalp_bot.data.universe import filter_tickers, rank_rows
    # A: 24h range мал (7%), но СВЕЖИЙ RVOL высокий; B: 24h range больше (12%),
    # но RVOL низкий. Свежая метрика должна вытащить A вперёд.
    rows = filter_tickers(
        [_ticker("AUSDT", 100, 107, 100, 300e6),
         _ticker("BUSDT", 100, 112, 100, 300e6)],
        min_turnover=150e6, min_range_pct=6.0, max_range_pct=30.0, max_spread_bps=5.0)
    picked = rank_rows(rows, top_n=5, vol_metric={"AUSDT": 3.0, "BUSDT": 0.4})
    assert picked[0] == "AUSDT"


def test_apply_pins_force_includes_and_dedups():
    from scalp_bot.data.universe import apply_pins
    # пин впереди, дедуп если уже в ranked, остальное добивает остаток
    out = apply_pins(["XLMUSDT", "BNBUSDT"], ["ALLOUSDT"], top_n=15)
    assert out == ["ALLOUSDT", "XLMUSDT", "BNBUSDT"]
    out2 = apply_pins(["ALLOUSDT", "XLMUSDT"], ["ALLOUSDT"], top_n=15)
    assert out2 == ["ALLOUSDT", "XLMUSDT"]  # без дубля


def test_apply_pins_keeps_pin_under_top_n_cap():
    from scalp_bot.data.universe import apply_pins
    # пин всегда сохраняется, ranked обрезается до остатка
    out = apply_pins(["A", "B", "C"], ["ALLOUSDT"], top_n=2)
    assert out == ["ALLOUSDT", "A"]


def test_apply_pins_empty_is_noop():
    from scalp_bot.data.universe import apply_pins
    assert apply_pins(["XLMUSDT"], [], top_n=15) == ["XLMUSDT"]


def test_pad_universe_fills_to_min_by_range():
    # P-4 (audit A-4): вселенная выродилась в 1 монету → добор из liquidity-pool
    # самых волатильных по range24h, без дублей
    from scalp_bot.data.universe import filter_tickers, pad_universe
    pool = filter_tickers(
        [_ticker("AUSDT", 100, 104, 100, 300e6),    # range 4%
         _ticker("BUSDT", 100, 105, 100, 200e6),    # range 5% — волатильнее
         _ticker("NEARUSDT", 100, 109, 100, 250e6)],  # уже в ranked
        min_turnover=150e6, min_range_pct=0.0, max_range_pct=20.0,
        max_spread_bps=5.0)
    out = pad_universe(["NEARUSDT"], pool, min_symbols=3)
    assert out == ["NEARUSDT", "BUSDT", "AUSDT"]


def test_pad_universe_liquidity_guards_stay_hard():
    # добор ослабляет ТОЛЬКО range-floor: low-turnover и широкий спред в pool
    # не попадают (filter_tickers с min_range_pct=0 — как в _select_universe)
    from scalp_bot.data.universe import filter_tickers, pad_universe
    wide = _ticker("WIDEUSDT", 100, 105, 100, 300e6)
    wide["bid1Price"], wide["ask1Price"] = "99.9", "100.1"  # спред 20bps > cap
    pool = filter_tickers(
        [_ticker("THINUSDT", 100, 105, 100, 50e6),  # turnover ниже floor
         wide,
         _ticker("OKUSDT", 100, 104, 100, 300e6)],
        min_turnover=150e6, min_range_pct=0.0, max_range_pct=20.0,
        max_spread_bps=5.0)
    out = pad_universe(["NEARUSDT"], pool, min_symbols=3)
    assert out == ["NEARUSDT", "OKUSDT"]  # добрали что есть, стражи не ослабли


def test_pad_universe_noop_when_disabled_or_enough():
    from scalp_bot.data.universe import pad_universe
    pool = [{"symbol": "AUSDT", "range_pct": 5.0}]
    # min_symbols=0 → выключено (прежнее поведение «качество, не количество»)
    assert pad_universe(["X"], pool, min_symbols=0) == ["X"]
    # уже достаточно монет → добора нет
    assert pad_universe(["X", "Y", "Z"], pool, min_symbols=3) == ["X", "Y", "Z"]


def test_score_ticker_excludes_stablecoins():
    """v0.18.29: стейблкоины явно исключены в score_ticker (blacklist STABLE_BASES).
    Без него USDCUSDT/USDEUSDT разбирались (range≈0, turnover высокий) и могли
    попасть в вселенную через padding на мёртвом рынке — base sweep_fade на
    стейблкоине бессмысленен (минус на fees)."""
    from scalp_bot.data.universe import score_ticker, STABLE_BASES
    # стейблкоины — отбрасываются независимо от оборота
    for s in ("USDCUSDT", "USDEUSDT", "FDUSDUSDT", "DAIUSDT", "USDDUSDT"):
        t = _ticker(s, 1.0, 1.0001, 0.9999, 5e9, bid=0.9999, ask=1.0001)
        assert score_ticker(t) is None, f"{s} должен быть исключён как стейблкоин"
    # обычная монета — разбирается
    assert score_ticker(_ticker("NEARUSDT", 100, 109, 100, 250e6)) is not None
    # база стейблкоина присутствует в blacklist
    assert "USDC" in STABLE_BASES


# ─── отсев stock-перпов из вселенной (v0.18.35, Bybit demo ErrCode 110126) ──
class _InstrSession:
    """Заглушка pybit HTTP: get_instruments_info отдаёт страницы с курсором."""
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def get_instruments_info(self, **params):
        cur = int(params.get("cursor", "0") or "0")
        self.calls += 1
        page = self.pages[cur] if cur < len(self.pages) else []
        nxt = str(cur + 1) if cur + 1 < len(self.pages) else ""
        return {"result": {"list": page, "nextPageCursor": nxt}}


def _instr(symbol, symbol_type):
    return {"symbol": symbol, "symbolType": symbol_type,
            "contractType": "LinearPerpetual"}


def _mk_instr_client(pages):
    from scalp_bot.trading.client import ScalpBybitClient
    cl = ScalpBybitClient.__new__(ScalpBybitClient)
    cl._category = "linear"
    cl._instr = {}
    cl._stock_syms = None
    cl._stock_syms_ts = 0.0
    cl._session = _InstrSession(pages)
    return cl


def test_non_crypto_type_symbols_pagination_and_filter():
    """v0.18.35: non_crypto_type_symbols собирает ВСЕ неторгуемые перпы через
    пагинацию (>500 linear-символов на Bybit — без cursor API вернёт первую
    страницу и символы после 500 будут пропущены, правило stats-collection.mdc).
    Крипто- и innovation-перпы в множество НЕ попадают."""
    pages = [
        [_instr("BTCUSDT", ""), _instr("SKHYNIXUSDT", "stock"),
         _instr("SOXLUSDT", "stock"), _instr("NEARUSDT", "")],
        [_instr("SOLUSDT", ""), _instr("NVDLUSDT", "stock"),
         _instr("HYPEUSDT", "innovation")],
    ]
    cl = _mk_instr_client(pages)
    stock = cl.non_crypto_type_symbols()
    assert stock == {"SKHYNIXUSDT", "SOXLUSDT", "NVDLUSDT"}
    assert cl._session.calls == 2  # прошли обе страницы, остановились на пустом cursor


def test_non_crypto_type_symbols_include_commodity_perps():
    """v0.18.46: commodity-перпы (CL/BZ нефть, XAU/XAG металлы) требуют того же
    Trading Terms, что и stock (ErrCode 110126), и на demo его принять нельзя —
    отсекаем вместе со stock. Диагноз: 21 отказ по CLUSDT (symbolType=commodity
    проскакивал мимо фильтра, который проверял только 'stock').

    innovation — обычная крипта из innovation-зоны, торгуется нормально и в
    множество попадать НЕ должна."""
    pages = [[_instr("CLUSDT", "commodity"), _instr("XAUUSDT", "commodity"),
              _instr("AAPLUSDT", "stock"), _instr("ENAUSDT", "innovation"),
              _instr("BTCUSDT", "")]]
    cl = _mk_instr_client(pages)
    assert cl.non_crypto_type_symbols() == {"CLUSDT", "XAUUSDT", "AAPLUSDT"}


def test_non_crypto_type_symbols_cached_within_ttl():
    """Повторный вызов в пределах TTL не бьёт по API (листинги редки, селектор
    крутится каждые universe_refresh_sec — кэш 1ч сберегает rate-limit)."""
    cl = _mk_instr_client([[_instr("TSLAUSDT", "stock")]])
    cl.non_crypto_type_symbols()
    assert cl._session.calls == 1
    cl.non_crypto_type_symbols()  # из кэша
    assert cl._session.calls == 1


def test_non_crypto_type_symbols_fail_open_on_api_error():
    """При ошибке API возвращаем пустое множество (fail-open): не блокируем
    вселенную целиком из-за временного хиккапа instruments-info."""
    class _ErrSess:
        def get_instruments_info(self, **p):
            raise RuntimeError("network down")
    from scalp_bot.trading.client import ScalpBybitClient
    cl = ScalpBybitClient.__new__(ScalpBybitClient)
    cl._category = "linear"
    cl._instr = {}
    cl._stock_syms = None
    cl._stock_syms_ts = 0.0
    cl._session = _ErrSess()
    assert cl.non_crypto_type_symbols() == set()


def test_select_universe_drops_stock_and_commodity_perps():
    """v0.18.35/v0.18.46: _select_universe отсекает не-крипто перпы ДО
    фильтра/ранжирования — они не попадают ни в rows, ни в padding-pool. На
    demo Bybit требует по ним Trading Terms (ErrCode 110126), который нельзя
    принять через API; плюс торгуются по сессиям реальных бирж, а не 24/7."""
    from scalp_bot.app.main import _select_universe
    from scalp_bot.config.settings import ScalpSettings

    class _Client:
        def __init__(self):
            self.tickers = [
                _ticker("NEARUSDT", 100, 108, 100, 250e6),       # крипто — годен
                _ticker("SKHYNIXUSDT", 100, 112, 100, 400e6),    # stock — отсечь
                _ticker("SOXLUSDT", 100, 115, 100, 500e6),       # stock — отсечь
                _ticker("CLUSDT", 100, 118, 100, 600e6),         # нефть — отсечь
                _ticker("ZECUSDT", 100, 110, 100, 130e6),        # крипто — годен
            ]

        def get_tickers(self):
            return self.tickers

        def non_crypto_type_symbols(self):
            return {"SKHYNIXUSDT", "SOXLUSDT", "CLUSDT"}

        def get_kline(self, *a, **k):  # для _fresh_rvol (universe_min_rvol>0)
            return []

    cfg = ScalpSettings()
    cfg.universe_min_rvol = 0.0  # отключаем RVOL-гейт — он требует klines
    picked = _select_universe(_Client(), cfg)
    assert "SKHYNIXUSDT" not in picked
    assert "SOXLUSDT" not in picked
    assert "CLUSDT" not in picked
    # крипто-перпы остаются в вселенной
    assert "NEARUSDT" in picked or "ZECUSDT" in picked


def test_density_break_pin_list_parses():
    """v0.18.35: density_break_pin_symbols — per-strategy пины (canon-like
    extra_syms), торгуются ТОЛЬКО density_break. Парсинг CSV → upper."""
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()
    assert s.density_break_pin_list == ["NEARUSDT", "HYPEUSDT", "WLDUSDT", "ENAUSDT"]
    s2 = ScalpSettings(density_break_pin_symbols="  nearusdt , HYPEUSDT  ")
    assert s2.density_break_pin_list == ["NEARUSDT", "HYPEUSDT"]
    assert ScalpSettings(density_break_pin_symbols="").density_break_pin_list == []


def test_density_break_pins_do_not_pollute_auto_universe():
    """v0.18.35: per-strategy пины density_break (в отличие от глобальных
    universe_pin_symbols) НЕ добавляются в авто-вселенную _select_universe —
    они force-include в main как canon-like extra_syms, не выталкивая символы
    из ранжирования sweep_fade. sweep_fade продолжает торговать свою rvol-
    вселенную без изменений."""
    from scalp_bot.app.main import _select_universe
    from scalp_bot.config.settings import ScalpSettings

    class _Client:
        def __init__(self):
            # только ZEC проходит rvol-фильтр (range 10%, turnover 130M);
            # NEAR/HYPE/WLD/ENA — db_pins, но НЕ в тикерах вообще
            self.tickers = [_ticker("ZECUSDT", 100, 110, 100, 130e6)]

        def get_tickers(self):
            return self.tickers

        def non_crypto_type_symbols(self):
            return set()

        def get_kline(self, *a, **k):
            return []

    cfg = ScalpSettings()
    cfg.universe_min_rvol = 0.0
    cfg.density_break_pin_symbols = "NEARUSDT,HYPEUSDT,WLDUSDT,ENAUSDT"
    picked = _select_universe(_Client(), cfg)
    # авто-вселенная = только ZEC (db_pins НЕ должны сюда попасть — они в main)
    assert "ZECUSDT" in picked
    for s in ("NEARUSDT", "HYPEUSDT", "WLDUSDT", "ENAUSDT"):
        assert s not in picked, f"{s} не должен быть в авто-вселенной (per-strategy)"


def test_db_pin_gate_blocks_other_strategies():
    """v0.18.35: db_pin-гейт в main loop — sweep_fade/density_bounce (scope is
    None) НЕ торгуют db_pins; density_break (scope is None) торгует. Воспроиз-
    водим логику гейта напрямую (canon_only + db_pin_set проверки)."""
    # симулируем гейт: для символа из db_pin_set, scope-None стратегия кроме
    # density_break должна пропускаться; density_break — нет.
    db_pin_set = {"NEARUSDT", "HYPEUSDT", "WLDUSDT", "ENAUSDT"}
    canon_only = set()  # упрощаем: canon_syms в авто-вселенной

    def gated(st_name, sym, scope):
        if scope is not None and sym not in scope:
            return True  # skip (canon scope)
        if scope is None and sym in canon_only:
            return True
        if scope is None and sym in db_pin_set and st_name != "density_break":
            return True  # db_pin-гейт
        return False

    # sweep_fade (scope None) на db_pin → блокируется
    assert gated("sweep_fade", "NEARUSDT", None) is True
    assert gated("density_bounce", "HYPEUSDT", None) is True
    # density_break (scope None) на db_pin → НЕ блокируется (торгует)
    assert gated("density_break", "NEARUSDT", None) is False
    # sweep_fade на обычном альт-символе (не db_pin) → НЕ блокируется
    assert gated("sweep_fade", "ZECUSDT", None) is False
    # canon (scope задан) на db_pin → блокируется scope (не торгует вне scope)
    assert gated("sweep_fade_canon", "NEARUSDT", {"BTCUSDT", "ETHUSDT"}) is True
    # canon на своём scope → НЕ блокируется
    assert gated("sweep_fade_canon", "BTCUSDT", {"BTCUSDT", "ETHUSDT"}) is False


# ─── v0.18.48: теневая вселенная (отсечённые оборотом, только наблюдение) ──

def _shadow_client(extra=()):
    class _Client:
        def __init__(self):
            self.tickers = [
                # боевые: оборот выше $100M
                _ticker("ZECUSDT", 100, 110, 100, 130e6, bid=99.99, ask=100.01),
                # отсечены ТОЛЬКО оборотом, спред нормальный → в тень
                _ticker("TAOUSDT", 100, 107, 100, 22e6, bid=99.99, ask=100.01),
                _ticker("ONDOUSDT", 100, 108, 100, 45e6, bid=99.99, ask=100.01),
                _ticker("XPLUSDT", 100, 109, 100, 12e6, bid=99.99, ask=100.01),
                # оборот ниже, но спред ШИРОКИЙ (>5bps) → тенью тоже не берём
                _ticker("WIDEUSDT", 100, 108, 100, 40e6, bid=99.0, ask=100.5),
                # ниже пола теней ($10M) → пыль, не наблюдаем
                _ticker("DUSTUSDT", 100, 108, 100, 2e6, bid=99.99, ask=100.01),
                # range вне коридора 6-20% → не наблюдаем (страж не ослаблен)
                _ticker("FLATUSDT", 100, 102, 100, 30e6, bid=99.99, ask=100.01),
                _ticker("PUMPEDUSDT", 100, 160, 100, 30e6, bid=99.99, ask=100.01),
                *extra,
            ]

        def get_tickers(self):
            return self.tickers

        def non_crypto_type_symbols(self):
            return set()

        def get_kline(self, *a, **k):
            return []

    return _Client()


def _shadow_cfg(**over):
    from scalp_bot.config.settings import ScalpSettings
    cfg = ScalpSettings()
    cfg.universe_min_rvol = 0.0
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_shadow_universe_takes_only_symbols_cut_by_turnover():
    """Тень ослабляет РОВНО один страж — оборот. range/spread остаются боевыми,
    иначе наблюдение смешивало бы три разных вопроса в один."""
    from scalp_bot.app.main import _select_shadow_universe

    picked = _select_shadow_universe(_shadow_client(), _shadow_cfg(),
                                     {"ZECUSDT"})
    assert set(picked) == {"TAOUSDT", "ONDOUSDT", "XPLUSDT"}
    assert "WIDEUSDT" not in picked, "spread-страж должен остаться боевым"
    assert "DUSTUSDT" not in picked, "ниже пола теней — пыль"
    assert "FLATUSDT" not in picked and "PUMPEDUSDT" not in picked, \
        "range-страж (floor и анти-памп cap) должен остаться боевым"
    assert "ZECUSDT" not in picked, "боевую монету наблюдать не нужно"


def test_shadow_universe_respects_symbol_cap():
    """Кэп символов — защита CPU/WS (Bybit: args ≤21000 символов на коннект)."""
    from scalp_bot.app.main import _select_shadow_universe

    picked = _select_shadow_universe(
        _shadow_client(), _shadow_cfg(shadow_universe_max_symbols=2), set())
    assert len(picked) == 2


def test_shadow_universe_disabled_and_zero_cap_return_empty():
    from scalp_bot.app.main import _select_shadow_universe

    assert _select_shadow_universe(
        _shadow_client(), _shadow_cfg(shadow_universe_enabled=False), set()) == []
    assert _select_shadow_universe(
        _shadow_client(), _shadow_cfg(shadow_universe_max_symbols=0), set()) == []


def test_shadow_universe_fail_open_on_client_error():
    """Сбой REST не должен ронять бота: тень необязательна."""
    from scalp_bot.app.main import _select_shadow_universe

    class _Broken:
        def get_tickers(self):
            raise RuntimeError("bybit down")

    assert _select_shadow_universe(_Broken(), _shadow_cfg(), set()) == []


def test_shadow_only_gate_keeps_signal_out_of_trading():
    """Ключевая гарантия: сигнал по наблюдаемому символу НЕ попадает в
    candidates → не доходит до resolve и executor. Воспроизводим гейт."""
    shadow_only = {"TAOUSDT"}
    emitted, candidates = [], []

    def loop(sym, sig):
        if sym in shadow_only:
            emitted.append(sig)
            return
        candidates.append(sig)

    loop("TAOUSDT", "sig-shadow")
    loop("ZECUSDT", "sig-live")
    assert candidates == ["sig-live"], "тень не должна попадать в торговлю"
    assert emitted == ["sig-shadow"]


def test_shadow_candidate_deduped_per_strategy_symbol():
    """Пока кандидат жив, новых по (страта, символ) не эмитим: живой бот держал
    бы позицию и не взводился заново. Без этого — дубли на каждом тике."""
    from scalp_bot.app.main import _add_shadow_universe_candidate

    class _Tracker:
        def __init__(self):
            self.added = []
            self.alive = set()

        def add(self, candidate):
            cid = len(self.added) + 1
            self.added.append(candidate)
            self.alive.add(cid)
            return cid

        def is_active(self, cid):
            return cid in self.alive

    sig = SimpleNamespace(strategy="sweep_fade", symbol="TAOUSDT", side="short",
                          entry_ref=100.0, sl_level=101.0, tp_level=98.0)
    tracker, shadow_open = _Tracker(), {}
    cfg = _shadow_cfg()
    for _ in range(20):
        _add_shadow_universe_candidate(tracker, shadow_open, cfg, sig, 1000.0)
    assert len(tracker.added) == 1, "20 тиков одного сетапа = 1 наблюдение"
    # исход зафиксирован → связка снова свободна
    tracker.alive.clear()
    _add_shadow_universe_candidate(tracker, shadow_open, cfg, sig, 2000.0)
    assert len(tracker.added) == 2
    # другая страта по тому же символу — независимое наблюдение
    other = SimpleNamespace(strategy="density_break", symbol="TAOUSDT",
                            side="long", entry_ref=100.0, sl_level=99.0,
                            tp_level=103.0)
    _add_shadow_universe_candidate(tracker, shadow_open, cfg, other, 2001.0)
    assert len(tracker.added) == 3
    row = tracker.added[0].as_row()
    assert row["setup_type"] == "shadow_universe"
    assert row["variant"] == "sweep_fade"
    assert row["actual_gate"] == "below_turnover_floor"


def test_shadow_candidate_fail_open_on_tracker_error():
    from scalp_bot.app.main import _add_shadow_universe_candidate

    class _Broken:
        def add(self, candidate):
            raise RuntimeError("db down")

        def is_active(self, cid):
            return False

    sig = SimpleNamespace(strategy="sweep_fade", symbol="TAOUSDT", side="short",
                          entry_ref=100.0, sl_level=101.0, tp_level=98.0)
    shadow_open = {}
    _add_shadow_universe_candidate(_Broken(), shadow_open, _shadow_cfg(), sig,
                                   1000.0)
    assert shadow_open == {}


def test_tracker_is_active_reflects_terminal_rows():
    """is_active опирается на _rows: терминальные строки оттуда удаляются."""
    from scalp_bot.analysis.counterfactual import CounterfactualTracker

    tracker = CounterfactualTracker(None, _shadow_cfg())
    assert tracker.is_active(None) is False
    assert tracker.is_active(42) is False
    tracker._rows[42] = {"symbol": "TAOUSDT"}
    assert tracker.is_active(42) is True


def test_pad_pool_respects_range_floor_suitability():
    """v0.18.29 (запрос пользователя 2026-06-28): padding pool использует canon
    range-floor (6%), а не 0.0 — добор не тащит непригодные майоры (BTC/ETH/SOL,
    range 2-5%, fee-guard режет сигналы). Воспроизводим логику _select_universe:
    pool с min_range_pct=floor → майоры НЕ в pool → не добираются."""
    from scalp_bot.data.universe import filter_tickers, pad_universe
    floor = 6.0
    pool = filter_tickers(
        [_ticker("BTCUSDT", 100, 102, 100, 2000e6),   # range 2% — майор, НЕ пригоден
         _ticker("ETHUSDT", 100, 103.1, 100, 900e6),  # range 3.1% — майор
         _ticker("NEARUSDT", 100, 108, 100, 250e6)],  # range 8% — пригоден
        min_turnover=100e6, min_range_pct=floor, max_range_pct=20.0,
        max_spread_bps=5.0)
    # в pool попал ТОЛЬКО NEARUSDT (range≥6%) — майоры отсечены suitability-floor
    assert [m["symbol"] for m in pool] == ["NEARUSDT"]
    # вселенная ниже floor, но добор не может добавить майоры → остаётся как есть
    out = pad_universe([], pool, min_symbols=3)
    assert out == ["NEARUSDT"]  # добрали единственного пригодного, майоры НЕ добавлены


def test_universe_min_symbols_default():
    """v0.18.19 (P-4): floor 3 монеты — минимальная диверсификация против
    вырождения вселенной в 1 символ (концентрация + sl_cooldown-запирание)."""
    from scalp_bot.config.settings import ScalpSettings
    assert ScalpSettings().universe_min_symbols == 3


# ─── momentum-селектор вселенной (метод «как в ролике», momentum_universe.py) ─

def _mticker(sym, last, pcnt, turnover, bid=None, ask=None, pre=""):
    """Тикер для momentum-отбора: price24hPcnt = 24h изменение (доля)."""
    return {"symbol": sym, "lastPrice": str(last), "price24hPcnt": str(pcnt),
            "turnover24h": str(turnover),
            "bid1Price": "" if bid is None else str(bid),
            "ask1Price": "" if ask is None else str(ask),
            "curPreListingPhase": pre}


def test_momentum_ranks_by_abs_change_and_filters_turnover():
    from scalp_bot.data.momentum_universe import select_momentum_universe
    tickers = [
        _mticker("BANANAUSDT", 1.0, 0.44, 85e6),    # +44%, оборот ок
        _mticker("DUMPUSDT", 1.0, -0.60, 120e6),    # −60% (топ по модулю)
        _mticker("MIDUSDT", 1.0, 0.20, 90e6),       # +20%
        _mticker("DUSTUSDT", 1.0, 1.20, 1e6),       # +120% но оборот 1M < floor
        _mticker("ETHUSDC", 3000, 0.30, 1e9),       # не USDT-перп
    ]
    picked = select_momentum_universe(
        tickers, top_n=5, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=0.0)
    # порядок по |24h change| убыв.: DUMP(60) > BANANA(44) > MID(20); DUST/ETHUSDC отсеяны
    assert picked == ["DUMPUSDT", "BANANAUSDT", "MIDUSDT"]


def test_momentum_direction_up_only():
    from scalp_bot.data.momentum_universe import select_momentum_universe
    tickers = [
        _mticker("UPUSDT", 1.0, 0.30, 100e6),
        _mticker("DOWNUSDT", 1.0, -0.50, 100e6),
    ]
    picked = select_momentum_universe(
        tickers, top_n=5, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=0.0, direction="up")
    assert picked == ["UPUSDT"]


def test_momentum_direction_down_only():
    from scalp_bot.data.momentum_universe import select_momentum_universe
    tickers = [
        _mticker("UPUSDT", 1.0, 0.30, 100e6),
        _mticker("DOWNUSDT", 1.0, -0.50, 100e6),
    ]
    picked = select_momentum_universe(
        tickers, top_n=5, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=0.0, direction="down")
    assert picked == ["DOWNUSDT"]


def test_momentum_min_change_pct_floor():
    from scalp_bot.data.momentum_universe import select_momentum_universe
    tickers = [
        _mticker("HOTUSDT", 1.0, 0.15, 100e6),   # +15% проходит
        _mticker("FLATUSDT", 1.0, 0.03, 100e6),  # +3% < floor 10%
    ]
    picked = select_momentum_universe(
        tickers, top_n=5, min_turnover=50e6, min_abs_change_pct=10.0,
        max_spread_bps=0.0)
    assert picked == ["HOTUSDT"]


def test_momentum_top_n_cap_and_no_anti_pump_cap():
    from scalp_bot.data.momentum_universe import select_momentum_universe
    # параболический +90% НЕ режется (в отличие от RVOL range-cap 20%)
    tickers = [_mticker(f"M{i}USDT", 1.0, 0.90 - i * 0.05, 100e6)
               for i in range(6)]
    picked = select_momentum_universe(
        tickers, top_n=3, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=0.0)
    assert picked == ["M0USDT", "M1USDT", "M2USDT"]


def test_momentum_spread_cap_optional():
    from scalp_bot.data.momentum_universe import select_momentum_universe
    wide = _mticker("WIDEUSDT", 100, 0.30, 100e6, bid=99.0, ask=100.0)
    # spread cap выкл (0) → проходит (как в ролике)
    assert select_momentum_universe(
        [wide], top_n=5, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=0.0) == ["WIDEUSDT"]
    # spread cap вкл → режется
    assert select_momentum_universe(
        [wide], top_n=5, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=5.0) == []


def test_momentum_skips_pre_listing_and_bad_rows():
    from scalp_bot.data.momentum_universe import select_momentum_universe
    pre = _mticker("NEWUSDT", 10, 0.30, 100e6, pre="Phase1")
    bad = {"symbol": "BADUSDT", "lastPrice": "0", "price24hPcnt": "0.3",
           "turnover24h": "100000000"}
    assert select_momentum_universe(
        [pre, bad], top_n=5, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=0.0) == []


def test_universe_method_default_is_rvol():
    from scalp_bot.config.settings import ScalpSettings
    cfg = ScalpSettings()
    assert cfg.universe_method == "rvol"
    assert cfg.momentum_min_turnover_usd == 50_000_000.0
    assert cfg.momentum_direction == "both"


# ─── HTF-bias (трендовый фильтр старшего ТФ, v0.9.3) ───────────────────────

from scalp_bot.data.htf import HtfTrend, compute_ema  # noqa: E402


class _FakeKlineClient:
    """get_kline возвращает Bybit-формат DESC: [start,o,h,l,close,v,turnover]."""

    def __init__(self, closes_by_sym: dict[str, list[float]]) -> None:
        self._c = closes_by_sym

    def get_kline(self, symbol, interval, limit=200):
        closes = self._c.get(symbol, [])
        return [[0, 0, 0, 0, c, 0, 0] for c in reversed(closes)]


def test_compute_ema_needs_full_length():
    assert compute_ema([1.0, 2.0, 3.0], 5) is None       # данных мало → None
    assert compute_ema([2.0] * 10, 5) == pytest.approx(2.0)


def test_compute_ema_trends_with_prices():
    # растущий ряд → EMA ниже последней цены, но выше первой
    ema = compute_ema([float(i) for i in range(1, 21)], 5)
    assert ema is not None and 15.0 < ema < 20.0


def test_htf_direction_long_short_by_ema():
    htf = HtfTrend(ema_len=200, interval="60")
    htf.refresh(_FakeKlineClient({"SOLUSDT": [100.0] * 200}), ["SOLUSDT"])
    assert htf.direction("SOLUSDT", 101.0) == "long"   # цена выше EMA → аптренд
    assert htf.direction("SOLUSDT", 99.0) == "short"


def test_htf_aligned_fail_open_when_no_data():
    htf = HtfTrend()
    assert htf.aligned("XXXUSDT", "long", 100.0) is True   # нет данных → разрешаем
    assert htf.direction("XXXUSDT", 100.0) is None


def test_htf_aligned_blocks_counter_trend_fade():
    htf = HtfTrend(ema_len=200)
    htf.refresh(_FakeKlineClient({"SOLUSDT": [100.0] * 200}), ["SOLUSDT"])
    # цена 99 < EMA100 → тренд short: long-fade против тренда блокируется
    assert htf.aligned("SOLUSDT", "long", 99.0) is False
    assert htf.aligned("SOLUSDT", "short", 99.0) is True


def test_htf_default_context_is_15m():
    """v0.16.0: контекст-ТФ скальпа = 15m (research: DYOR/VWAP-guide/ChartScout
    2026; A/B 15д n=6220 gross +0.122R vs +0.087R у 1H). EMA200 сохраняется,
    refresh учащён под более быстрый 15m-бар. Замок на решение."""
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()
    assert s.htf_interval == "15"
    assert s.htf_ema_len == 200
    assert s.htf_refresh_sec == 120.0


def test_htf_has_data_false_until_warmed():
    """v0.18.2: has_data=False для непрогретого символа (fail-closed MR-гейт),
    True после успешного refresh. Канон QuantConnect: не торговать до готовности
    индикатора."""
    htf = HtfTrend(ema_len=200)
    assert htf.has_data("SOLUSDT") is False        # ни разу не считалась
    htf.refresh(_FakeKlineClient({"SOLUSDT": [100.0] * 200}), ["SOLUSDT"])
    assert htf.has_data("SOLUSDT") is True          # прогрет
    assert htf.has_data("XXXUSDT") is False         # другой символ — нет


def test_density_break_tp_r_default_canon_3_5():
    """v0.18.10: density_break_take_profit_r=3.5 = глобальный канон (Философия B
    «winners run»). Откат подгонки 2.5R на n=25 (no-data-fitting): низкий кап
    противоречил философии и не имел канонического источника. Замок на канон."""
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()
    assert s.density_break_take_profit_r == 3.5
    assert s.take_profit_r == 3.5


def test_build_signal_tp_r_override():
    """build_signal с tp_r override ставит TP на нужном R; без override — cfg."""
    from scalp_bot.analysis.signals import build_signal
    from scalp_bot.config.settings import ScalpSettings
    cfg = ScalpSettings().model_copy(update={
        "min_risk_fee_mult": 0.0, "min_target_fee_mult": 0.0, "sl_buffer_bps": 0.0})
    snap = SimpleNamespace(symbol="X", best_bid=100.0, best_ask=100.0,
                           last_price=100.0)
    # swept=99 → long, risk=entry-sl=100-99=1 → TP при 2.5R = 102.5; при 3.5R=103.5
    s25 = build_signal(snap, "long", 99.0, cfg, 3, ["r"], tp_r=2.5)
    s35 = build_signal(snap, "long", 99.0, cfg, 3, ["r"])
    assert s25 is not None and s35 is not None
    assert s25.tp_level == pytest.approx(102.5)
    assert s35.tp_level == pytest.approx(103.5)  # дефолт = cfg.take_profit_r=3.5


def test_build_signal_sl_mult_widens_only_sl_not_tp():
    """Канон decoupling (v0.18.8): sl_mult РАСШИРЯЕТ ТОЛЬКО SL (MAE/Sweeney),
    а TP остаётся на base_risk (MFE-якорь) — синхронен с ×1.0. Иначе цель
    уезжала бы вместе со стопом (старая подгонка)."""
    from scalp_bot.analysis.signals import build_signal
    from scalp_bot.config.settings import ScalpSettings
    cfg = ScalpSettings().model_copy(update={
        "min_risk_fee_mult": 0.0, "min_target_fee_mult": 0.0,
        "sl_buffer_bps": 0.0, "take_profit_r": 3.5})
    snap = SimpleNamespace(symbol="X", best_bid=100.0, best_ask=100.0,
                           last_price=100.0)
    # base_risk = entry-swept = 100-99 = 1.0
    base = build_signal(snap, "long", 99.0, cfg, 3, ["r"])  # sl_mult=None → 1.0
    wide = build_signal(snap, "long", 99.0, cfg, 3, ["r"], sl_mult=1.5)
    assert base is not None and wide is not None
    # SL расширился ×1.5: 100-1.5 = 98.5 (vs 99.0 при ×1.0)
    assert base.sl_level == pytest.approx(99.0)
    assert wide.sl_level == pytest.approx(98.5)
    # TP НЕ изменился (на base_risk): обе = 100 + 3.5×1.0 = 103.5
    assert base.tp_level == pytest.approx(103.5)
    assert wide.tp_level == pytest.approx(103.5)


def test_build_signal_density_break_sl_mult_one_is_noop():
    """density_break (sl_mult=1.0 явно) не задевается глобальным sl_risk_mult:
    структурный стоп неизменен даже при глобальном ×1.5."""
    from scalp_bot.analysis.signals import build_signal
    from scalp_bot.config.settings import ScalpSettings
    g = ScalpSettings().model_copy(update={
        "min_risk_fee_mult": 0.0, "min_target_fee_mult": 0.0,
        "sl_buffer_bps": 0.0, "sl_risk_mult": 1.5})  # глобально ×1.5
    snap = SimpleNamespace(symbol="X", best_bid=100.0, best_ask=100.0,
                           last_price=100.0)
    # density_break передаёт sl_mult=1.0 явно → иммунен к глобальному 1.5
    dbreak = build_signal(snap, "long", 99.0, g, 3, ["r"], tp_r=2.5, sl_mult=1.0)
    assert dbreak is not None
    assert dbreak.sl_level == pytest.approx(99.0)   # SL структурный, не расширен
    assert dbreak.tp_level == pytest.approx(102.5)  # 100 + 2.5×1.0


def test_htf_has_data_false_on_thin_history():
    """Тонкая история (< ema_len свечей) → EMA=None → has_data остаётся False:
    fail-closed не пускает фейд на новом листинге без полной истории."""
    htf = HtfTrend(ema_len=200)
    htf.refresh(_FakeKlineClient({"NEWUSDT": [100.0] * 50}), ["NEWUSDT"])
    assert htf.has_data("NEWUSDT") is False


# ─── ADX режим-гейт (v0.17.0) ──────────────────────────────────────────────

class _FakeOHLCClient:
    """get_kline → Bybit DESC: [start,o,h,l,close,v,turnover] с заданными OHLC."""

    def __init__(self, ohlc_by_sym: dict[str, list[tuple]]) -> None:
        self._d = ohlc_by_sym  # sym -> list[(o,h,l,c)] ПО ВОЗРАСТАНИЮ

    def get_kline(self, symbol, interval, limit=200):
        rows = self._d.get(symbol, [])
        return [[0, o, h, l, c, 0, 0] for (o, h, l, c) in reversed(rows)]


def test_compute_adx_high_in_strong_trend():
    from scalp_bot.data.htf import compute_adx
    n = 200
    highs = [100 + i + 0.5 for i in range(n)]
    lows = [100 + i - 0.5 for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    adx = compute_adx(highs, lows, closes, 14)
    assert adx is not None and adx >= 25.0   # стабильный аптренд → сильный ADX


def test_compute_adx_low_in_range():
    from scalp_bot.data.htf import compute_adx
    n = 200
    closes = [100.0 + (1.0 if i % 2 else 0.0) for i in range(n)]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    adx = compute_adx(highs, lows, closes, 14)
    assert adx is not None and adx < 25.0    # пила → слабый ADX (диапазон)


def test_compute_adx_needs_warmup():
    from scalp_bot.data.htf import compute_adx
    assert compute_adx([1.0, 2.0, 3.0], [0.0, 1.0, 2.0],
                       [0.5, 1.5, 2.5], 14) is None   # <2n+1 свечей → None


def test_htf_adx_gate_blocks_strong_trend():
    n = 200
    rows = [(100.0 + i, 100 + i + 0.5, 100 + i - 0.5, 100.0 + i) for i in range(n)]
    htf = HtfTrend(ema_len=200, adx_len=14)
    htf.refresh(_FakeOHLCClient({"SOLUSDT": rows}), ["SOLUSDT"])
    assert htf.trend_strength("SOLUSDT") >= 25.0
    assert htf.is_strong_trend("SOLUSDT", 25.0) is True   # трендовый день → фейд стоп


def test_htf_adx_gate_fail_open_no_data():
    htf = HtfTrend()
    assert htf.trend_strength("XXXUSDT") is None
    assert htf.is_strong_trend("XXXUSDT", 25.0) is False  # нет ADX → не блокируем


def test_htf_adx_gate_defaults():
    """v0.17.0: ADX режим-гейт поверх EMA (additive). Канон MR: не фейдить сильный
    тренд — «never fade a one-timeframe trending market» (Connors/Raschke «Street
    Smarts» 1995; Wilder ADX 1978). A/B 15д (n=6220→3104, data/scalp_adx_gate.txt):
    ema+adx@25 gross +0.140R/сделку vs +0.122R EMA (+15%). Замок на решение."""
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()
    assert s.htf_adx_gate is True
    assert s.htf_adx_len == 14
    assert s.htf_adx_max == 30.0


def test_compute_di_dir_uptrend_long():
    from scalp_bot.data.htf import compute_di_dir
    n = 60
    highs = [100 + i + 0.5 for i in range(n)]
    lows = [100 + i - 0.5 for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    assert compute_di_dir(highs, lows, closes, 14) == "long"  # +DI доминирует


def test_compute_di_dir_downtrend_short():
    from scalp_bot.data.htf import compute_di_dir
    n = 60
    highs = [100 - i + 0.5 for i in range(n)]
    lows = [100 - i - 0.5 for i in range(n)]
    closes = [100.0 - i for i in range(n)]
    assert compute_di_dir(highs, lows, closes, 14) == "short"  # −DI доминирует


def test_compute_di_dir_needs_warmup():
    from scalp_bot.data.htf import compute_di_dir
    assert compute_di_dir([1.0, 2.0], [0.0, 1.0], [0.5, 1.5], 14) is None


def test_htf_di_long_gate_blocks_counter_trend_long():
    """v0.18.4: даунтренд по DMI → лонг-фейд блокируется, шорт-фейд нет."""
    n = 60
    rows = [(100.0 - i, 100 - i + 0.5, 100 - i - 0.5, 100.0 - i) for i in range(n)]
    htf = HtfTrend(ema_len=200, adx_len=14)
    htf.refresh(_FakeOHLCClient({"SOLUSDT": rows}), ["SOLUSDT"])
    assert htf.di_direction("SOLUSDT") == "short"
    assert htf.di_blocks_long("SOLUSDT") is True    # контртренд-лонг запрещён


def test_htf_di_long_gate_fail_open_no_data():
    htf = HtfTrend()
    assert htf.di_direction("XXXUSDT") is None
    assert htf.di_blocks_long("XXXUSDT") is False   # нет DMI → не блокируем


def test_htf_di_long_gate_default():
    """v0.18.4: асимметричный DMI-гейт направления для лонгов включён по умолчанию.
    Wilder DMI 1978. A/B 3 окна (data/scalp_di_long_gate.txt): лонги avgR
    −0.092/−0.100/−0.098 (EMA) → +0.004/+0.023/−0.006 (C), шорты не тронуты."""
    from scalp_bot.config.settings import ScalpSettings
    assert ScalpSettings().htf_di_long_gate is True


def test_di_long_gate_covers_density_break():
    """v0.18.18 (C-08): density_break под асимметричным DMI long-gate, при этом
    БЕЗ симметричных MR-фильтров (htf_filtered/regime_gated=False — сохраняем
    profitable контртренд-ШОРТЫ, Quant Signals). Live: long WR 5.9% / net −158
    (p<0.02) = bull traps. Селектор di_long_strats: di_long_gated с фолбэком на
    htf_filtered (MR-страты наследуют, как раньше)."""
    from scalp_bot.analysis.strategies import (DensityBreakStrategy,
                                               DensityBounceStrategy,
                                               SweepFadeStrategy)
    # density_break: явный opt-in в long-gate, симметричные фильтры выключены
    assert DensityBreakStrategy.di_long_gated is True
    assert DensityBreakStrategy.htf_filtered is False
    assert DensityBreakStrategy.regime_gated is False
    # селектор из main.py: di_long_gated с фолбэком на htf_filtered
    def in_gate(s) -> bool:
        return bool(getattr(s, "di_long_gated", getattr(s, "htf_filtered", True)))
    assert in_gate(DensityBreakStrategy) is True       # momentum: явный opt-in
    assert in_gate(SweepFadeStrategy) is True          # MR: наследует htf_filtered
    assert in_gate(DensityBounceStrategy) is True      # MR: наследует htf_filtered
    # поведение гейта: блокируется ТОЛЬКО лонг при DMI вниз, шорт свободен
    n = 60
    rows = [(100.0 - i, 100 - i + 0.5, 100 - i - 0.5, 100.0 - i) for i in range(n)]
    htf = HtfTrend(ema_len=200, adx_len=14)
    htf.refresh(_FakeOHLCClient({"ZECUSDT": rows}), ["ZECUSDT"])
    assert htf.di_blocks_long("ZECUSDT") is True
    # предикат гейта из main.py (для density_break)
    def blocked(side: str) -> bool:
        return ("density_break" in {"density_break"} and side == "long"
                and htf.di_blocks_long("ZECUSDT"))
    assert blocked("long") is True     # контртренд-лонг-пробой = bull trap → блок
    assert blocked("short") is False   # шорт-пробой свободен (по тренду)


# ─── adopt-старт без флэта (v0.18.0) ───────────────────────────────────────

class _FakeAdoptClient:
    """Минимальный клиент для _adopt_on_start: позиции/статусы/pnl по словарям."""

    def __init__(self, positions=None, statuses=None, pnls=None):
        self.positions = positions or {}   # sym -> obj(size, side) | None
        self.statuses = statuses or {}     # link -> orderStatus
        self.pnls = pnls or {}             # sym -> closed_pnl
        self.cancelled = []

    def get_position(self, sym):
        return self.positions.get(sym)

    def order_status(self, sym, link):
        return self.statuses.get(link)

    def cancel_order(self, sym, link):
        self.cancelled.append((sym, link)); return {"ok": True}

    def closed_pnl(self, sym, qty=None, since_ms=None):
        return self.pnls.get(sym)


def _row(db, tid):
    return db._conn.execute(
        "SELECT status, pnl_usd, close_reason FROM trades WHERE id=?", (tid,)
    ).fetchone()


def test_adopt_on_start_keeps_live_position(tmp_path):
    """Живая позиция при рестарте НЕ закрывается — даём дойти до TP/SL (кейс #926)."""
    from types import SimpleNamespace
    from scalp_bot.app.main import _adopt_on_start
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="BNBUSDT", side="short", qty=0.26, entry=637.2,
                         sl=641.0, tp=623.8, score=5, reasons="x", mode="live",
                         strategy="density_break", entry_order_id="lnk1")
    cl = _FakeAdoptClient(positions={"BNBUSDT": SimpleNamespace(size=0.26, side="Sell")})
    _adopt_on_start(cl, db)
    assert [t.id for t in db.open_trades()] == [tid]   # осталась открытой
    assert cl.cancelled == []                          # ничего не отменяли


def test_adopt_on_start_cancels_resting_entry(tmp_path):
    """Резящий НЕзаполненный maker-вход при рестарте снимается точечно по link."""
    from scalp_bot.app.main import _adopt_on_start
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="XLMUSDT", side="long", qty=10.0, entry=0.22,
                         sl=0.219, tp=0.223, score=5, reasons="x", mode="live",
                         strategy="sweep_fade", entry_order_id="lnk2")
    cl = _FakeAdoptClient(positions={"XLMUSDT": None}, statuses={"lnk2": "New"})
    _adopt_on_start(cl, db)
    assert db.open_trades() == []
    assert cl.cancelled == [("XLMUSDT", "lnk2")]
    assert _row(db, tid)[2] == "entry_timeout"


def test_adopt_on_start_reconciles_closed_while_down(tmp_path):
    """Позиция закрылась пока бот лежал → реальный PnL (tp/sl), не restart_flat=0."""
    from scalp_bot.app.main import _adopt_on_start
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="BNBUSDT", side="short", qty=0.26, entry=637.2,
                         sl=641.0, tp=623.8, score=5, reasons="x", mode="live",
                         strategy="density_break", entry_order_id="lnk3")
    cl = _FakeAdoptClient(positions={"BNBUSDT": None}, statuses={"lnk3": "Filled"},
                          pnls={"BNBUSDT": 1.05})
    _adopt_on_start(cl, db)
    status, pnl, reason = _row(db, tid)
    assert status == "closed" and reason == "tp_hit" and pnl == pytest.approx(1.05)


def test_flatten_on_start_default_false():
    """v0.18.0: НЕ флэтим открытые позиции при рестарте (биржевые SL/TP защищают,
    manage() продолжает сопровождать). Кейс #926: рестарт-флэт срезал прибыльный
    шорт BNBUSDT (+$1.05) и записал pnl=0. Замок на решение."""
    from scalp_bot.config.settings import ScalpSettings
    assert ScalpSettings().flatten_on_start is False


def test_strategy_filter_applicability_v0181():
    """v0.18.1: MR-стратегии под HTF+ADX фильтрами, momentum density_break — НЕТ.
    Направленный EMA-фильтр режет прибыльные контртренд-пробои (Quant Signals,
    175 backtests: «London Breakout universal failure с трендовым фильтром»);
    ADX-гейт «не торговать в тренд» для пробоя backwards (пробой ХОЧЕТ тренда).
    Замок на решение."""
    from scalp_bot.analysis.strategies import (DensityBounceStrategy,
                                               DensityBreakStrategy,
                                               SweepFadeStrategy)
    assert SweepFadeStrategy.htf_filtered is True
    assert SweepFadeStrategy.regime_gated is True
    assert DensityBounceStrategy.htf_filtered is True
    assert DensityBounceStrategy.regime_gated is True
    # momentum: вне обоих MR-фильтров
    assert DensityBreakStrategy.htf_filtered is False
    assert DensityBreakStrategy.regime_gated is False


# ─── write-ahead вход (анти-«призрак», v0.18.14) ───────────────────────────

class _FakeLiveClient:
    """Минимальный LIVE-клиент для on_signal. Фиксирует, существовала ли строка
    в БД на момент place_entry — это и есть доказательство write-ahead."""

    def __init__(self, db, *, ok=True):
        self._db = db
        self._ok = ok
        self.row_existed_at_place = None
        self.placed = []

    def instrument(self, symbol):
        return SimpleNamespace(qty_step=0.001, min_order_qty=0.001)

    def set_leverage(self, symbol, lev):
        return True

    def round_price(self, symbol, price):
        return round(price, 4)

    def place_entry(self, *, symbol, side, qty, order_link_id, order_type,
                    limit_price, sl_price, tp_price):
        self.row_existed_at_place = bool(self._db.open_trades())
        self.placed.append(order_link_id)
        return {"ok": self._ok, "error": None if self._ok else "rejected"}


def _live_cfg(**over):
    base = dict(
        trading_enabled=True, risk_based_sizing=True, risk_per_trade_usd=15.0,
        min_position_usd=10.0, position_usd=300.0, max_leverage=10,
        entry_order_type="market", entry_fill_timeout_sec=30.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _live_sig():
    return Signal(symbol="SUIUSDT", side="long", entry_ref=0.7025,
                  sl_level=0.6990, tp_level=0.7100, score=5,
                  reasons=["wall_break"], strategy="density_break")


def test_on_signal_write_ahead_row_before_place(tmp_path):
    """v0.18.14: строка БД создаётся ДО place_entry (write-ahead). Позиция на
    бирже без записи в БД («призрак», кейс SUI +$16.30) структурно невозможна."""
    db = ScalpDB(str(tmp_path))
    cl = _FakeLiveClient(db, ok=True)
    ex = Executor(db, _live_cfg(), client=cl)
    tid = ex.on_signal(_live_sig())
    assert tid is not None
    assert cl.row_existed_at_place is True          # строка была ДО постановки ордера
    assert [t.id for t in db.open_trades()] == [tid]


def test_on_signal_rejected_marks_entry_rejected(tmp_path):
    """v0.18.14: ордер отклонён биржей → write-ahead строка помечается
    entry_Rejected (исключена из статы db.recent_closed), трекинг очищается,
    on_signal → None. Дубля-«призрака» нет — намерение зафиксировано и снято."""
    db = ScalpDB(str(tmp_path))
    cl = _FakeLiveClient(db, ok=False)
    ex = Executor(db, _live_cfg(), client=cl)
    tid = ex.on_signal(_live_sig())
    assert tid is None
    assert cl.row_existed_at_place is True          # write-ahead сработал и при reject
    assert db.open_trades() == []                   # строка закрыта, не висит open
    row = db._conn.execute(
        "SELECT status, close_reason FROM trades ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "closed" and row[1] == "entry_Rejected"
    assert ex._link2trade == {} and ex._fills == {}  # трекинг снят (_forget_trade)


# ─── sweep_fade_canon (v0.18.20): значимые уровни + full reclaim + скоуп ────

def _kline_row(ts_sec: float, hi: float, lo: float):
    """Строка Bybit get_kline: [startTime(ms), o, h, l, c, vol, turnover]."""
    mid = (hi + lo) / 2
    return [str(int(ts_sec * 1000)), str(mid), str(hi), str(lo), str(mid),
            "1", "1"]


def test_day_levels_pdh_pdl_and_current_day():
    from scalp_bot.data.levels import day_levels
    day = 86_400.0
    now = 2 * day + 6 * 3600  # 06:00 UTC второго дня
    rows = []
    # предыдущий день (полное покрытие 96 баров)
    for i in range(96):
        ts = day + i * 900
        hi, lo = (110.0, 90.0) if i == 50 else (101.0, 99.0)
        rows.append(_kline_row(ts, hi, lo))
    # текущий день: закрытые бары до 05:45 + ФОРМИРУЮЩИЙСЯ бар (06:00) с
    # экстремальным лоем — должен быть ИСКЛЮЧЁН из day_low
    for i in range(23):
        ts = 2 * day + i * 900
        hi, lo = (105.0, 95.0) if i == 10 else (100.5, 99.5)
        rows.append(_kline_row(ts, hi, lo))
    rows.append(_kline_row(now, 120.0, 80.0))  # формирующийся бар
    lv = day_levels(list(reversed(rows)), now)  # Bybit DESC (новые сверху)
    assert lv is not None
    assert lv["pdh"] == pytest.approx(110.0) and lv["pdl"] == pytest.approx(90.0)
    assert lv["day_high"] == pytest.approx(105.0)
    assert lv["day_low"] == pytest.approx(95.0)  # 80 из формирующегося бара не взят


def test_day_levels_fail_closed_without_full_prev_day():
    from scalp_bot.data.levels import day_levels
    day = 86_400.0
    now = 2 * day + 6 * 3600
    # история начинается с СЕРЕДИНЫ предыдущего дня → PDH/PDL по обрезку врут
    rows = [_kline_row(day + i * 900, 101.0, 99.0) for i in range(48, 96)]
    assert day_levels(list(reversed(rows)), now) is None


def test_day_levels_regime_rolls_across_midnight():
    """Fix 2026-07-02: rolling-regime считается по последним N закрытым барам
    ЛЮБОГО дня. Раньше — только по сегодняшним: сразу после 00:00 UTC баров <2
    → regime_ratio=None → trend-гейт fail-closed на ~30-45 мин каждые сутки,
    а тренд, начавшийся вчера, был для гейта невидим."""
    from scalp_bot.data.levels import day_levels
    day = 86_400.0
    now = 2 * day + 900  # 00:15 UTC — сегодня закрыт ровно 1 бар
    rows = [_kline_row(day + i * 900, 101.0, 99.0) for i in range(96)]
    rows.append(_kline_row(2 * day, 100.5, 99.5))
    lv = day_levels(list(reversed(rows)), now)
    assert lv is not None
    # раньше: сегодняшних закрытых баров 1 (<2) → None; теперь окно катится
    # через полночь и regime считается по вчерашним+сегодняшнему барам
    assert lv["regime_ratio"] is not None


def _kline_row_oc(ts_sec: float, o: float, hi: float, lo: float, c: float):
    return [str(int(ts_sec * 1000)), str(o), str(hi), str(lo), str(c), "1", "1"]


def test_day_levels_regime_lookback_plumbed():
    """Fix 2026-07-02: SCALP_SWEEP_FADE_TREND_LOOKBACK_BARS был мёртвым —
    day_levels всегда считал по хардкоду 8. Теперь параметр прокинут
    (KeyLevels.refresh → day_levels → _rolling_regime): разное окно даёт
    разный ratio."""
    from scalp_bot.data.levels import KeyLevels, day_levels
    day = 86_400.0
    now = 2 * day + 8 * 900  # закрыто 8 сегодняшних баров
    rows = [_kline_row_oc(day + i * 900, 100.0, 101.0, 99.0, 100.0)
            for i in range(96)]
    # сегодня: 6 флэт-баров + 2 трендовых (100→104→108)
    for i in range(6):
        rows.append(_kline_row_oc(2 * day + i * 900, 100.0, 101.0, 99.0, 100.0))
    rows.append(_kline_row_oc(2 * day + 6 * 900, 100.0, 104.0, 100.0, 104.0))
    rows.append(_kline_row_oc(2 * day + 7 * 900, 104.0, 108.0, 104.0, 108.0))
    kline = list(reversed(rows))
    lv8 = day_levels(kline, now, regime_lookback=8)
    lv2 = day_levels(kline, now, regime_lookback=2)
    # lookback=8: move 8 / atr 2.5 = 3.2; lookback=2: move 8 / atr 4 = 2.0
    assert lv8["regime_ratio"] == pytest.approx(3.2)
    assert lv2["regime_ratio"] == pytest.approx(2.0)

    class _FakeClient:
        def get_kline(self, symbol, interval, limit):
            return kline

    kl = KeyLevels(regime_lookback=2)
    kl.refresh(_FakeClient(), ["ETHUSDT"], now=now)
    assert kl.regime_ratio("ETHUSDT") == pytest.approx(2.0)


def test_refresh_key_levels_covers_and_deduplicates_full_universe():
    """v0.18.39: level/regime cache прогревается для auto-universe и density
    pins, а не только для canon whitelist; это telemetry, level_gate не меняется."""
    from scalp_bot.app.main import _refresh_key_levels

    class _Levels:
        def __init__(self):
            self.calls = []

        def refresh(self, client, symbols):
            self.calls.append((client, symbols))

    levels = _Levels()
    client = object()
    _refresh_key_levels(
        client, levels,
        ["BTCUSDT", "ZECUSDT", "HYPEUSDT", "ZECUSDT", ""])
    assert levels.calls == [
        (client, ["BTCUSDT", "ZECUSDT", "HYPEUSDT"])
    ]


def test_key_levels_swept_gate_sides():
    from scalp_bot.data.levels import KeyLevels
    kl = KeyLevels()
    kl._levels["ETHUSDT"] = {"pdh": 110.0, "pdl": 90.0,
                             "day_high": 105.0, "day_low": 95.0}
    # long: свип took out дневной лоу / PDL
    assert kl.swept_key_level("ETHUSDT", "long", 94.9) == "day_low"
    assert kl.swept_key_level("ETHUSDT", "long", 89.5) == "day_low"  # ниже обоих
    assert kl.swept_key_level("ETHUSDT", "long", 96.0) is None  # выше уровней
    # short: свип took out дневной хай / PDH
    assert kl.swept_key_level("ETHUSDT", "short", 105.5) == "day_high"
    assert kl.swept_key_level("ETHUSDT", "short", 104.0) is None
    # fail-closed: символ без данных
    assert kl.swept_key_level("BTCUSDT", "long", 1.0) is None


def test_detector_level_gate_blocks_arm_without_key_level():
    """v0.18.20: с level_gate взвод разрешён только на свипе значимого уровня
    (канон CAP «sweep of liquidity pool»), микро-экстремум не взводит."""
    det = SweepReclaimDetector("ETHUSDT", _cfg(),
                               level_gate=lambda sym, side, swept: None)
    assert det.update(_snap(_arm_samples(), last_price=96.5), now=100.0) is None
    assert det.armed is False  # свип есть, но не значимого уровня


def test_detector_level_gate_arms_and_tags_reason():
    det = SweepReclaimDetector("ETHUSDT", _cfg(),
                               level_gate=lambda sym, side, swept: "pdl")
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)
    assert det.armed is True
    sig = det.update(_snap(_fire_samples(), last_price=97.6), now=130.0)
    assert sig is not None and "key_pdl" in sig.reasons


def _canon_cfg(**over):
    base = _cfg(sweep_fade_canon_reclaim_frac=1.0,
                sweep_fade_canon_symbol_list=["ETHUSDT"],
                sweep_fade_canon_entry_order_type="market",
                sweep_fade_sl_risk_mult=None)
    for k, v in over.items():
        setattr(base, k, v)
    return base


class _FakeKeyLevels:
    def __init__(self, name="pdl"):
        self.name = name

    def swept_key_level(self, symbol, side, swept):
        return self.name


def test_canon_strategy_scope_and_fail_closed_levels():
    from scalp_bot.analysis.strategies import SweepFadeCanonStrategy
    st = SweepFadeCanonStrategy(_canon_cfg(), ["ZECUSDT", "ETHUSDT"])
    # скоуп: детектор только на whitelisted символе
    assert st.update(_snap(_arm_samples(), symbol="ZECUSDT", last_price=96.5),
                     now=100.0) is None
    assert st.armed("ZECUSDT") is False
    # fail-closed: key_levels не инжектнут → не взводимся даже на своём символе
    st.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5), now=100.0)
    assert st.armed("ETHUSDT") is False
    # ensure_symbols не заводит чужие символы
    st.ensure_symbols(["XLMUSDT"])
    assert "XLMUSDT" not in st._det


def test_canon_strategy_full_reclaim_required():
    """reclaim_frac=1.0 (CAP Rule 2 буквально): вход только при ПОЛНОМ возврате
    за свипнутый уровень (early min=98), полпути (97.6, хватало базовому при
    0.5) — мало."""
    from scalp_bot.analysis.strategies import SweepFadeCanonStrategy
    st = SweepFadeCanonStrategy(_canon_cfg(), ["ETHUSDT"])
    st.key_levels = _FakeKeyLevels("pdl")
    st.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5), now=100.0)
    assert st.armed("ETHUSDT") is True
    # 97.6 = reclaim 73% пути (базовому с frac=0.5 хватило бы) → канону мало
    assert st.update(_snap(_fire_samples(), symbol="ETHUSDT", last_price=97.6),
                     now=110.0) is None
    assert st.armed("ETHUSDT") is True
    # полный возврат за уровень 98 → выстрел
    full = [CvdSample(20, 97.8, -1), CvdSample(21, 97.9, 0), CvdSample(22, 98.0, 1),
            CvdSample(23, 98.0, 2), CvdSample(24, 98.05, 3), CvdSample(25, 98.1, 4)]
    sig = st.update(_snap(full, symbol="ETHUSDT", last_price=98.1), now=120.0)
    assert sig is not None and sig.side == "long"
    assert sig.strategy == "sweep_fade_canon" and "key_pdl" in sig.reasons
    # v0.18.24: канон-вход — taker (market), не пассивный maker (A-5: ~70%
    # непролива maker = post-only отмены на full-reclaim). База остаётся maker.
    assert sig.entry_order_type == "market"


def test_canon_entry_taker_base_sweep_fade_maker():
    """v0.18.24: канон ставит entry_order_type=market на детекторах; базовый
    sweep_fade — None (→ глобальный maker), A/B контраст maker vs taker."""
    from scalp_bot.analysis.strategies import (SweepFadeStrategy,
                                               SweepFadeCanonStrategy)
    canon = SweepFadeCanonStrategy(_canon_cfg(), ["ETHUSDT"])
    assert canon._det["ETHUSDT"].entry_order_type == "market"
    base = SweepFadeStrategy(_cfg(sweep_fade_sl_risk_mult=None), ["ETHUSDT"])
    assert base._det["ETHUSDT"].entry_order_type is None
    # пустой/None override → канон-детектор тоже None (фолбэк на глоб. maker)
    c2 = SweepFadeCanonStrategy(_canon_cfg(sweep_fade_canon_entry_order_type=""),
                                ["ETHUSDT"])
    assert c2._det["ETHUSDT"].entry_order_type is None


def test_canon_strategy_in_registry_and_cooldown_family():
    from scalp_bot.analysis.strategies import build_strategies
    from scalp_bot.config.settings import ScalpSettings
    cfg = _canon_cfg()
    cfg.strategy_list = ["sweep_fade_canon"]
    out = build_strategies(cfg, ["ETHUSDT"])
    assert [s.name for s in out] == ["sweep_fade_canon"]
    # v0.18.22: направленные гейты (EMA200 HTF + DMI-лонг) у канона СНЯТЫ —
    # фейд дневного уровня контртрендовый по построению (свип PDH ⇒ HTF=long
    # всегда ⇒ 252/252 сигналов дня 1 резались гейтом). ADX-режим остаётся.
    assert out[0].htf_filtered is False
    assert out[0].di_long_gated is False
    assert out[0].regime_gated is True
    # базовый sweep_fade направленные гейты СОХРАНЯЕТ (A/B не задет)
    from scalp_bot.analysis.strategies import SweepFadeStrategy
    assert getattr(SweepFadeStrategy, "htf_filtered", True) is True
    # SL-cooldown семейства fade (60м) распространяется и на канон
    s = ScalpSettings()
    assert s.sl_cooldown_for("sweep_fade_canon") == s.sweep_fade_sl_cooldown_sec
    assert "sweep_fade_canon" in s.strategy_list  # включён по умолчанию (A/B)


# ─── v0.18.27: sweep_fade_run — УДАЛЕНА v0.18.37 (2026-07-15) ─

def test_run_strategy_removed():
    """v0.18.37 (2026-07-15, решение пользователя): sweep_fade_run удалена —
    форвард n=176 WR 12% net -$327, гипотеза «дай winners бежать» (BE-lock +
    убранный flow_exit + scratch) опровергнута — run хуже canon (n=15 WR 40%)
    и base (n=621 WR 9% но +$120). Возврат к канону: A/B base vs canon, как в
    исходном дизайне v0.18.20. Реестр и дефолтный strategy_list её не знают;
    неизвестное имя — мягкий скип. Артефакт: /tmp/scalp_audit/around_0629.py."""
    from scalp_bot.analysis.strategies import build_strategies
    from scalp_bot.config.settings import ScalpSettings
    assert "sweep_fade_run" not in ScalpSettings().strategy_list
    cfg = _canon_cfg()
    cfg.strategy_list = ["sweep_fade_run", "sweep_fade_canon"]
    out = build_strategies(cfg, ["ETHUSDT"])
    assert [s.name for s in out] == ["sweep_fade_canon"]  # run скипнут




# ─── rolling-regime (v0.18.27; страта sweep_fade_trend удалена v0.18.33,
#     метрика regime_ratio живёт в regime_features-телеметрии) ──────────────

def test_trend_strategy_removed():
    """v0.18.33 (2026-07-06, решение пользователя): sweep_fade_trend удалена —
    форвард n=35 WR 37% net −$121.62, гипотеза range-day-fade опровергнута
    (нужен WR 46% при R:R 1.2). Реестр и дефолтный strategy_list её не знают;
    неизвестное имя в конфиге — мягкий скип (не крэш)."""
    from scalp_bot.analysis.strategies import build_strategies
    from scalp_bot.config.settings import ScalpSettings
    assert "sweep_fade_trend" not in ScalpSettings().strategy_list
    cfg = _canon_cfg()
    cfg.strategy_list = ["sweep_fade_trend", "sweep_fade_canon"]
    out = build_strategies(cfg, ["ETHUSDT"])
    assert [s.name for s in out] == ["sweep_fade_canon"]  # trend скипнут


def test_rolling_regime_no_lookahead():
    """_rolling_regime считает по закрытым барам в прошлом (ts уже были)."""
    from scalp_bot.data.levels import _rolling_regime
    # 4 бара, последние 8 (всё окно). move=|close4-open1|, atr=avg(hi-lo)
    closed = [(1, 100.0, 101.0, 99.0, 100.5),
              (2, 100.5, 101.5, 100.0, 101.0),
              (3, 101.0, 102.0, 100.5, 101.5),
              (4, 101.5, 102.5, 101.0, 102.0)]
    r = _rolling_regime(closed, lookback=8)
    atr = sum(abs(b[2] - b[3]) for b in closed) / 4  # (2+1.5+1.5+1.5)/4=1.625
    assert abs(r - abs(102.0 - 100.0) / atr) < 1e-9
    # мало баров → None
    assert _rolling_regime([(1, 100, 101, 99, 100)], lookback=8) is None
    # lookback обрезает окно
    closed8 = [(i, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i) for i in range(10)]
    r2 = _rolling_regime(closed8, lookback=3)
    win = closed8[-3:]
    atr2 = sum(abs(b[2] - b[3]) for b in win) / 3
    assert abs(r2 - abs(win[-1][4] - win[0][1]) / atr2) < 1e-9


# ─── regime-фичи на входе (meta-labeling, Lopez de Prado AFML Ch3) ──────────

class _RegimeHtf:
    def __init__(self, adx=None):
        self._adx = adx

    def trend_strength(self, symbol):
        return self._adx


class _RegimeKeyLevels:
    """KeyLevels-мок с regime_ratio + levels(day_high/day_low)."""

    def __init__(self, ratio=1.1, day_high=None, day_low=None):
        self._ratio = ratio
        self._lv = {"day_high": day_high, "day_low": day_low,
                    "pdh": day_high, "pdl": day_low, "regime_ratio": ratio}

    def regime_ratio(self, symbol):
        return self._ratio

    def levels(self, symbol):
        return self._lv if self._lv["day_high"] is not None else None


def test_regime_features_full():
    snap = _snap([CvdSample(1, 97, -1), CvdSample(2, 97, 1)],
                 ts=12 * 3600.0,  # 12:00 UTC → europe
                 last_price=100.0, best_bid=99.9, best_ask=100.1,
                 ob_imbalance=0.65, funding_rate=0.0001)
    f = compute_regime_features(snap, _RegimeHtf(adx=28.5),
                                _RegimeKeyLevels(ratio=1.2, day_high=102.0,
                                                 day_low=98.0))
    assert f["adx"] == 28.5
    assert f["regime_ratio"] == 1.2
    # spread = (100.1-99.9)/((100.1+99.9)/2) * 1e4 = 0.2/100 * 1e4 = 20 bps
    assert f["spread_bps"] == pytest.approx(20.0, abs=1e-6)
    assert f["ob_imbalance"] == 0.65
    assert f["funding_bps"] == pytest.approx(1.0, abs=1e-6)  # 0.0001 * 1e4
    assert f["liq_count"] == 1  # _snap кладёт один LiqEvent
    assert f["session"] == "europe"
    # day_range = (102-98)/100 *100 = 4.0
    assert f["day_range_pct"] == pytest.approx(4.0, abs=1e-6)
    assert f["dist_high_pct"] == pytest.approx(2.0, abs=1e-6)   # (102-100)/100
    assert f["dist_low_pct"] == pytest.approx(2.0, abs=1e-6)    # (100-98)/100
    assert f["cvd_slope"] is not None  # 2 точки → slope считается


def test_regime_features_missing_data_none():
    snap = _snap([CvdSample(1, 97, -1)], best_bid=None, best_ask=None)
    # без htf/key_levels → regime-часть None; без bid/ask → spread None
    f = compute_regime_features(snap, htf=None, key_levels=None)
    assert f["adx"] is None
    assert f["regime_ratio"] is None
    assert f["day_range_pct"] is None
    assert f["dist_high_pct"] is None
    assert f["dist_low_pct"] is None
    assert f["spread_bps"] is None
    # всегда есть: ob_imbalance, funding (из snap), liq_count, session, cvd
    assert f["liq_count"] == 1
    assert f["session"] is not None
    assert f["cvd_slope"] is None  # 1 точка → None


def test_regime_features_session_buckets():
    for hour, exp in [(0, "asia"), (7, "asia"), (8, "europe"), (12, "europe"),
                      (13, "us"), (20, "us"), (21, "asia_pm"), (23, "asia_pm")]:
        snap = _snap([CvdSample(1, 97, 0)], ts=hour * 3600.0)
        assert compute_regime_features(snap)["session"] == exp, hour


def test_regime_features_cvd_slope_sign_and_none():
    # монотонный рост CVD → положительный наклон
    up = [CvdSample(t, 100, c) for t, c in [(1, 0), (2, 2), (3, 5), (4, 9)]]
    assert compute_regime_features(_snap(up))["cvd_slope"] > 0
    # монотонное падение → отрицательный
    down = [CvdSample(t, 100, c) for t, c in [(1, 9), (2, 5), (3, 2), (4, 0)]]
    assert compute_regime_features(_snap(down))["cvd_slope"] < 0
    # нулевой разброс времени → None
    flat = [CvdSample(5, 100, c) for c in (0, 1, 2)]
    assert compute_regime_features(_snap(flat))["cvd_slope"] is None


def test_db_regime_table_created_on_init(tmp_path):
    db = ScalpDB(str(tmp_path))
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='regime_features'"
    ).fetchall()
    assert len(rows) == 1
    db.close()


def test_db_insert_regime_and_read(tmp_path):
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="BTCUSDT", side="short", qty=0.1, entry=60000.0,
                         sl=60200.0, tp=59400.0, score=4, reasons="sweep",
                         mode="live", strategy="sweep_fade_canon", ts_open=100.0)
    feat = compute_regime_features(
        _snap([CvdSample(1, 100, -1)], ts=12 * 3600.0, last_price=100.0),
        _RegimeHtf(adx=33.0), _RegimeKeyLevels(ratio=0.7, day_high=101.0,
                                               day_low=99.0))
    db.insert_regime(tid, feat, ts=100.0)
    got = db.regime_for(tid)
    assert got is not None
    assert got["trade_id"] == tid
    assert got["adx"] == 33.0
    assert got["regime_ratio"] == 0.7
    assert got["session"] == "europe"
    assert got["day_range_pct"] == pytest.approx(2.0, abs=1e-6)
    db.close()


def test_db_insert_regime_idempotent_replace(tmp_path):
    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(symbol="ETHUSDT", side="long", qty=1.0, entry=100.0,
                         sl=99.0, tp=103.0, score=3, reasons="x", mode="live",
                         strategy="sweep_fade", ts_open=1.0)
    db.insert_regime(tid, {"adx": 10.0, "session": "asia"}, ts=1.0)
    # повтор с другими значениями — REPLACE, не дубликат
    db.insert_regime(tid, {"adx": 40.0, "session": "us"}, ts=2.0)
    got = db.regime_for(tid)
    assert got["adx"] == 40.0
    assert got["session"] == "us"
    cnt = db._conn.execute(
        "SELECT COUNT(*) FROM regime_features WHERE trade_id=?", (tid,)
    ).fetchone()[0]
    assert cnt == 1
    db.close()


def test_executor_logs_regime_on_live_entry(tmp_path):
    db = ScalpDB(str(tmp_path))
    cl = _FakeLiveClient(db)
    ex = Executor(db, _live_cfg(), client=cl)
    sig = _live_sig()
    sig.regime = {"adx": 25.0, "regime_ratio": 1.0, "day_range_pct": 3.0,
                  "dist_high_pct": 1.5, "dist_low_pct": 1.5, "spread_bps": 12.0,
                  "ob_imbalance": 0.6, "funding_bps": 0.5, "cvd_slope": 2.0,
                  "liq_count": 3, "session": "us"}
    tid = ex.on_signal(sig)
    assert tid is not None
    got = db.regime_for(tid)
    assert got is not None and got["adx"] == 25.0 and got["session"] == "us"
    db.close()


def test_executor_regime_logging_failure_does_not_block_entry(tmp_path):
    """Логирование regime — read-only: даже если insert_regime бросает, вход
    проходит (no-data-fitting.mdc: метрики не влияют на торговлю)."""

    class _BoomDB:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def insert_regime(self, *a, **k):
            raise RuntimeError("boom")

    real = ScalpDB(str(tmp_path))
    db = _BoomDB(real)
    cl = _FakeLiveClient(db)  # place_entry лезет в db.open_trades — делегирует
    ex = Executor(db, _live_cfg(), client=cl)
    sig = _live_sig()
    sig.regime = {"adx": 25.0}
    tid = ex.on_signal(sig)  # insert_regime бросает → _log_regime глушит, вход ок
    assert tid is not None
    assert [t.id for t in real.open_trades()] == [tid]  # сделка открылась
    real.close()


# ─── v0.18.31: расширенные regime-фичи + shadow-лог отвергнутых сигналов ────

def test_regime_columns_match_db_feature_cols():
    """Инвариант: набор фичей в analysis/regime.py и колонок в state/db.py
    совпадает (обе таблицы пишутся по _FEATURE_COLS)."""
    from scalp_bot.analysis.regime import REGIME_COLUMNS
    from scalp_bot.state.db import _FEATURE_COLS
    assert REGIME_COLUMNS == _FEATURE_COLS


def test_regime_session_uses_wall_clock_not_snap_ts():
    """Fix 2026-07-03: session — из wall-clock now (time.time), не из snap.ts
    (monotonic = секунды с загрузки хоста → бакеты сдвинуты на uptime%24h)."""
    snap = _snap([CvdSample(1, 97, 0)], ts=3 * 3600.0)  # monotonic «03:00»
    # wall-clock 14:00 UTC → us, а НЕ asia по monotonic-ts
    f = compute_regime_features(snap, now=14 * 3600.0)
    assert f["session"] == "us"
    # fallback на snap.ts когда wall не передан (юнит-тесты legacy-поведения)
    assert compute_regime_features(snap)["session"] == "asia"


def _cvd_seq(prices, t0=0.0, dt=5.0):
    """CvdSample-серия: цены по одной на 5с-бакет (cvd не важен)."""
    return [CvdSample(t0 + i * dt, p, float(i)) for i, p in enumerate(prices)]


def test_regime_ret_autocorr_sign():
    """Lo & MacKinlay 1988: пила (реверсии) → autocorr<0; монотонный тренд с
    переменным шагом → autocorr>0."""
    # пила вокруг 100: +1/-1 чередуются → ретёрны чередуют знак
    saw = [100.0 + (1.0 if i % 2 else 0.0) for i in range(30)]
    f = compute_regime_features(_snap(_cvd_seq(saw), ts=150.0))
    assert f["ret_autocorr"] is not None and f["ret_autocorr"] < 0
    # тренд с нарастающим шагом (положительная связь соседних ретёрнов)
    trend = [100.0 * (1.0 + 0.001 * i) ** 2 for i in range(30)]
    f2 = compute_regime_features(_snap(_cvd_seq(trend), ts=150.0))
    assert f2["ret_autocorr"] is not None and f2["ret_autocorr"] > 0
    # мало точек → None
    f3 = compute_regime_features(_snap(_cvd_seq(saw[:6]), ts=30.0))
    assert f3["ret_autocorr"] is None


def test_regime_price_slope_bps_min_sign():
    up = _cvd_seq([100.0 + 0.1 * i for i in range(20)])
    f = compute_regime_features(_snap(up, ts=100.0, last_price=102.0))
    assert f["price_slope_bps_min"] is not None and f["price_slope_bps_min"] > 0
    down = _cvd_seq([100.0 - 0.1 * i for i in range(20)])
    f2 = compute_regime_features(_snap(down, ts=100.0, last_price=98.0))
    assert f2["price_slope_bps_min"] < 0


def test_regime_rv_burst_detects_expansion():
    """Тихие первые 2 минуты + вспышка в последней минуте → rv_burst > 1."""
    calm = [100.0 + 0.001 * (i % 2) for i in range(24)]          # t=0..115
    wild = [100.0 + (0.5 if i % 2 else -0.5) for i in range(12)]  # t=120..175
    samples = _cvd_seq(calm) + _cvd_seq(wild, t0=120.0)
    f = compute_regime_features(_snap(samples, ts=180.0))
    assert f["rv_burst"] is not None and f["rv_burst"] > 1.5


def test_regime_tape_accel_speeds_up():
    """Редкие принты в начале окна, плотные в последней минуте → accel > 1."""
    sparse = [CvdSample(t, 100.0, 0.0) for t in (0.0, 30.0, 60.0, 90.0)]
    dense = [CvdSample(120.0 + i, 100.0, 0.0) for i in range(60)]
    f = compute_regime_features(_snap(sparse + dense, ts=180.0))
    assert f["tape_accel"] is not None and f["tape_accel"] > 1.0
    # короткое окно (<90с истории) → None
    f2 = compute_regime_features(_snap(dense, ts=180.0))
    assert f2["tape_accel"] is None


def test_regime_liq_notional_and_side_split():
    liqs = [LiqEvent(1, "Buy", 30_000.0, 97.0),   # ликвидирован лонг
            LiqEvent(2, "Sell", 10_000.0, 97.5)]  # ликвидирован шорт
    f = compute_regime_features(_snap([CvdSample(1, 97, 0)], liq_events=liqs))
    assert f["liq_notional_usd"] == pytest.approx(40_000.0)
    assert f["liq_buy_frac"] == pytest.approx(0.75)
    # без ликвидаций: notional=0, frac=None
    f2 = compute_regime_features(_snap([CvdSample(1, 97, 0)], liq_events=[]))
    assert f2["liq_notional_usd"] == 0.0
    assert f2["liq_buy_frac"] is None


def test_regime_oi_delta_pct():
    hist = [(0.0, 1000.0), (100.0, 1100.0)]
    f = compute_regime_features(_snap([CvdSample(1, 97, 0)], oi_history=hist))
    assert f["oi_delta_pct"] == pytest.approx(10.0)
    # короткий span (<60с) → None
    f2 = compute_regime_features(
        _snap([CvdSample(1, 97, 0)], oi_history=[(0.0, 1000.0), (30.0, 1100.0)]))
    assert f2["oi_delta_pct"] is None


def test_regime_btc_ret_bps_from_btc_snapshot():
    btc = _snap(_cvd_seq([60_000.0, 60_300.0], dt=60.0), symbol="BTCUSDT")
    f = compute_regime_features(_snap([CvdSample(1, 97, 0)]), btc_snap=btc)
    assert f["btc_ret_bps"] == pytest.approx(50.0, rel=1e-3)  # +0.5% = 50 bps
    assert compute_regime_features(_snap([CvdSample(1, 97, 0)]))["btc_ret_bps"] is None


def test_regime_near_depth_imb_top5():
    bids = [(97.0 - i * 0.01, 10.0) for i in range(25)]  # топ-5 bid = 50
    asks = [(97.1 + i * 0.01, 30.0) for i in range(25)]  # топ-5 ask = 150
    f = compute_regime_features(
        _snap([CvdSample(1, 97, 0)], bids=bids, asks=asks))
    assert f["near_depth_imb"] == pytest.approx(0.25)  # 50/(50+150)


def test_symbolstate_oi_history_windowed():
    clock = {"t": 0.0}
    st = SymbolState("BTCUSDT", oi_window_sec=100.0, now=lambda: clock["t"])
    st.on_ticker(None, 1000.0, None)
    clock["t"] = 50.0
    st.on_ticker(None, 1100.0, None)
    clock["t"] = 200.0
    st.on_ticker(None, 1200.0, None)  # первые две точки старше окна 100с
    snap = st.snapshot()
    assert [oi for _, oi in snap.oi_history] == [1200.0]


def test_htf_natr_and_bb_width():
    from scalp_bot.data.htf import compute_bb_width_pct, compute_natr
    n = 60
    highs = [100 + i + 0.5 for i in range(n)]
    lows = [100 + i - 0.5 for i in range(n)]
    closes = [100.0 + i for i in range(n)]
    natr = compute_natr(highs, lows, closes, 14)
    assert natr is not None and natr > 0
    bbw = compute_bb_width_pct(closes)
    assert bbw is not None and bbw > 0
    # флэт → BB схлопнуты (squeeze), ширина ~0
    assert compute_bb_width_pct([100.0] * 30) == pytest.approx(0.0)
    # данных мало → None (fail-soft)
    assert compute_natr(highs[:5], lows[:5], closes[:5], 14) is None
    assert compute_bb_width_pct(closes[:10]) is None


def test_htf_refresh_populates_natr_bb_width():
    n = 200
    rows = [(100.0 + i, 100 + i + 0.5, 100 + i - 0.5, 100.0 + i) for i in range(n)]
    htf = HtfTrend(ema_len=200, adx_len=14)
    htf.refresh(_FakeOHLCClient({"SOLUSDT": rows}), ["SOLUSDT"])
    assert htf.natr_pct("SOLUSDT") is not None and htf.natr_pct("SOLUSDT") > 0
    assert htf.bb_width_pct("SOLUSDT") is not None
    assert htf.natr_pct("XXXUSDT") is None  # нет данных → None


def test_db_shadow_table_insert_and_read(tmp_path):
    db = ScalpDB(str(tmp_path))
    feats = {"adx": 33.0, "session": "us", "ret_autocorr": -0.4,
             "liq_notional_usd": 5000.0, "btc_ret_bps": -12.0}
    db.insert_shadow(symbol="SOLUSDT", side="long", strategy="sweep_fade",
                     blocked_by="adx_strong", features=feats, ts=100.0,
                     entry_ref=97.0, sl_level=96.5, tp_level=98.5, score=4)
    rows = db.shadow_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["blocked_by"] == "adx_strong"
    assert r["strategy"] == "sweep_fade"
    assert r["side"] == "long"
    assert r["entry_ref"] == 97.0 and r["sl_level"] == 96.5
    assert r["adx"] == 33.0 and r["ret_autocorr"] == -0.4
    assert r["btc_ret_bps"] == -12.0
    db.close()


def test_db_regime_migration_adds_new_columns(tmp_path):
    """Старая БД (regime_features без v0.18.31-колонок) мигрирует на новую
    схему через ALTER TABLE — insert с новыми фичами не падает."""
    import sqlite3 as _sq
    path = str(tmp_path / "scalp_bot.sqlite")
    conn = _sq.connect(path)
    conn.executescript("""
        CREATE TABLE regime_features (
            trade_id INTEGER PRIMARY KEY, ts REAL NOT NULL,
            adx REAL, regime_ratio REAL, day_range_pct REAL,
            dist_high_pct REAL, dist_low_pct REAL, spread_bps REAL,
            ob_imbalance REAL, funding_bps REAL, cvd_slope REAL,
            liq_count INTEGER, session TEXT);
    """)
    conn.commit()
    conn.close()
    db = ScalpDB(str(tmp_path))
    db.insert_regime(1, {"adx": 20.0, "ret_autocorr": -0.2,
                         "htf_bb_width_pct": 1.5}, ts=5.0)
    got = db.regime_for(1)
    assert got["ret_autocorr"] == -0.2
    assert got["htf_bb_width_pct"] == 1.5
    db.close()


def test_main_log_shadow_writes_row_and_respects_flag(tmp_path):
    from scalp_bot.app.main import _log_shadow

    class _Cfg:
        shadow_log_enabled = True

    db = ScalpDB(str(tmp_path))
    sig = Signal(symbol="SOLUSDT", side="long", entry_ref=97.0, sl_level=96.5,
                 tp_level=98.5, score=4, reasons=["x"], strategy="sweep_fade")
    snap = _snap([CvdSample(1, 97, 0)])
    _log_shadow(db, _Cfg(), sig, "htf_align", snap, None, None, 14 * 3600.0)
    rows = db.shadow_rows()
    assert len(rows) == 1
    assert rows[0]["blocked_by"] == "htf_align"
    assert rows[0]["session"] == "us"  # из wall-clock now
    # флаг off → не пишем
    _Cfg.shadow_log_enabled = False
    _log_shadow(db, _Cfg(), sig, "htf_align", snap, None, None, 14 * 3600.0)
    assert len(db.shadow_rows()) == 1
    db.close()


def test_main_log_shadow_never_raises(tmp_path):
    """Shadow-лог — телеметрия: сбой БД не рвёт main loop."""
    from scalp_bot.app.main import _log_shadow

    class _Cfg:
        shadow_log_enabled = True

    class _BoomShadowDB:
        def insert_shadow(self, *a, **k):
            raise RuntimeError("boom")

    sig = Signal(symbol="SOLUSDT", side="long", entry_ref=97.0, sl_level=96.5,
                 tp_level=98.5, score=4, reasons=["x"])
    _log_shadow(_BoomShadowDB(), _Cfg(), sig, "dmi_long",
                _snap([CvdSample(1, 97, 0)]), None, None, 100.0)  # не бросает


def test_settings_shadow_log_enabled_default():
    """v0.18.31: shadow-лог отвергнутых сигналов включён по умолчанию
    (телеметрия, на торговлю не влияет)."""
    from scalp_bot.config.settings import ScalpSettings
    assert ScalpSettings().shadow_log_enabled is True


# ─── v0.18.34: гейт «мёртвого рынка» (natr<0.5 & liq=0 & rv<1.1) ────────────

def test_dead_market_blocks_only_full_conjunction():
    """Блок ТОЛЬКО при конъюнкции всех трёх условий (data: одиночные условия
    не сепарируют — liq=0 p=1.0, rv p=1.0; конъюнкция p=0.049)."""
    from scalp_bot.analysis.regime import is_dead_market
    kw = dict(natr_max=0.5, rv_max=1.1)
    dead = {"htf_natr_pct": 0.4, "liq_count": 0, "rv_burst": 0.8}
    assert is_dead_market(dead, **kw) is True
    # каждое условие в отдельности снимает блок
    assert is_dead_market({**dead, "htf_natr_pct": 0.6}, **kw) is False
    assert is_dead_market({**dead, "liq_count": 3}, **kw) is False
    assert is_dead_market({**dead, "rv_burst": 1.3}, **kw) is False


def test_dead_market_boundaries_strict():
    """Границы строгие (<): natr==0.5 / rv==1.1 — НЕ мёртвый (торгуем)."""
    from scalp_bot.analysis.regime import is_dead_market
    kw = dict(natr_max=0.5, rv_max=1.1)
    assert is_dead_market(
        {"htf_natr_pct": 0.5, "liq_count": 0, "rv_burst": 0.8}, **kw) is False
    assert is_dead_market(
        {"htf_natr_pct": 0.4, "liq_count": 0, "rv_burst": 1.1}, **kw) is False


def test_dead_market_fail_open_on_missing_features():
    """Fail-open: None-фича или пустой dict → False (не блокируем вслепую,
    консистентно с ADX-гейтом)."""
    from scalp_bot.analysis.regime import is_dead_market
    kw = dict(natr_max=0.5, rv_max=1.1)
    assert is_dead_market(None, **kw) is False
    assert is_dead_market({}, **kw) is False
    assert is_dead_market(
        {"htf_natr_pct": None, "liq_count": 0, "rv_burst": 0.5}, **kw) is False
    assert is_dead_market(
        {"htf_natr_pct": 0.3, "liq_count": None, "rv_burst": 0.5}, **kw) is False
    assert is_dead_market(
        {"htf_natr_pct": 0.3, "liq_count": 0, "rv_burst": None}, **kw) is False


def test_dead_market_settings_defaults():
    """v0.18.34: гейт включён по умолчанию, пороги из threshold-sweep
    (natr<0.5, rv<1.1); откат через env без деплоя."""
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()
    assert s.dead_market_gate_enabled is True
    assert s.dead_market_natr_max_pct == 0.5
    assert s.dead_market_rv_max == 1.1


def test_main_log_shadow_accepts_precomputed_feats(tmp_path):
    """dead_market-гейт передаёт уже посчитанные фичи — _log_shadow пишет их
    как есть (без пересчёта)."""
    from scalp_bot.app.main import _log_shadow

    class _Cfg:
        shadow_log_enabled = True

    from scalp_bot.analysis.regime import REGIME_COLUMNS
    db = ScalpDB(str(tmp_path))
    sig = Signal(symbol="SOLUSDT", side="long", entry_ref=97.0, sl_level=96.5,
                 tp_level=98.5, score=4, reasons=["x"], strategy="sweep_fade")
    feats = {k: None for k in REGIME_COLUMNS}
    feats.update({"htf_natr_pct": 0.33, "liq_count": 0, "rv_burst": 0.9,
                  "session": "asia"})
    _log_shadow(db, _Cfg(), sig, "dead_market", _snap([CvdSample(1, 97, 0)]),
                None, None, 3 * 3600.0, feats=feats)
    rows = db.shadow_rows()
    assert len(rows) == 1
    assert rows[0]["blocked_by"] == "dead_market"
    assert abs(rows[0]["htf_natr_pct"] - 0.33) < 1e-9
    assert rows[0]["session"] == "asia"  # прекомпьютнутые, без пересчёта
    db.close()


# ─── v0.18.38: live-контрфактуал maker non-fill ─────────────────────────

def _maker_shadow_row(**over):
    row = {
        "id": 1, "trade_id": 10, "ts_signal": 90.0, "ts_nonfill": 100.0,
        "ts_end": None, "symbol": "ZECUSDT", "side": "long",
        "strategy": "sweep_fade", "nonfill_reason": "entry_timeout",
        "entry": 100.0, "sl": 99.0, "tp": 103.5, "risk": 1.0,
        "target_r": 1.5, "status": "pending",
        "outcome_1_5r": None, "ts_outcome_1_5r": None,
        "outcome_tp": None, "ts_outcome_tp": None,
        "mfe_r": 0.0, "mae_r": 0.0,
        "mfe_r_60": None, "mae_r_60": None,
        "mfe_r_180": None, "mae_r_180": None,
        "sample_count": 0, "last_price": None, "last_update": 100.0,
    }
    row.update(over)
    return row


def test_advance_maker_shadow_keeps_independent_1_5r_and_tp_paths():
    """+1.5R достигнут до SL, но полный TP не достигнут и позже первым стал SL."""
    row = _maker_shadow_row()
    assert advance_maker_nonfill_shadow(row, 101.6, 110.0) is True
    assert row["outcome_1_5r"] == "target"
    assert row["outcome_tp"] is None
    assert row["mfe_r"] == pytest.approx(1.6)

    assert advance_maker_nonfill_shadow(row, 98.9, 120.0) is True
    assert row["outcome_1_5r"] == "target"  # first-hit не переписывается
    assert row["outcome_tp"] == "sl"
    assert row["mae_r"] == pytest.approx(1.1)
    assert row["sample_count"] == 2


def test_advance_maker_shadow_checkpoints_and_finalizes():
    row = _maker_shadow_row()
    assert advance_maker_nonfill_shadow(
        row, 100.5, 100.0 + 3600.0,
        checkpoint_sec=3600.0, horizon_sec=10800.0) is True
    assert row["mfe_r_60"] == pytest.approx(0.5)
    assert row["mae_r_60"] == pytest.approx(0.0)
    assert row["status"] == "pending"

    assert advance_maker_nonfill_shadow(
        row, 99.5, 100.0 + 10800.0,
        checkpoint_sec=3600.0, horizon_sec=10800.0) is True
    assert row["status"] == "final"
    assert row["ts_end"] == pytest.approx(10900.0)
    assert row["mfe_r_180"] == pytest.approx(0.5)
    assert row["mae_r_180"] == pytest.approx(0.5)


def test_maker_shadow_db_roundtrip_and_resume(tmp_path):
    db = ScalpDB(str(tmp_path))
    sid = db.insert_maker_nonfill_shadow(
        trade_id=10, ts_signal=90.0, ts_nonfill=100.0,
        symbol="ZECUSDT", side="long", strategy="sweep_fade",
        nonfill_reason="entry_timeout", entry=100.0, sl=99.0, tp=103.5,
        target_r=1.5)
    assert sid is not None
    pending = db.pending_maker_nonfill_shadows()
    assert len(pending) == 1
    assert pending[0]["risk"] == pytest.approx(1.0)

    row = pending[0]
    advance_maker_nonfill_shadow(
        row, 101.6, 110.0, checkpoint_sec=3600.0, horizon_sec=10800.0)
    db.update_maker_nonfill_shadow(row)
    got = db.maker_nonfill_shadow_rows()[0]
    assert got["outcome_1_5r"] == "target"
    assert got["mfe_r"] == pytest.approx(1.6)
    assert got["sample_count"] == 1
    db.close()


def test_executor_starts_shadow_only_for_base_sweep_fade(tmp_path):
    db = ScalpDB(str(tmp_path))
    cfg = SimpleNamespace(
        maker_nonfill_shadow_enabled=True, flow_exit_activate_r=1.5)
    ex = Executor(db, cfg, client=None, now=lambda: 100.0)
    base = SimpleNamespace(
        id=1, ts_open=90.0, symbol="ZECUSDT", side="long",
        strategy="sweep_fade", entry=100.0, sl=99.0, tp=103.5)
    canon = SimpleNamespace(
        id=2, ts_open=90.0, symbol="BTCUSDT", side="long",
        strategy="sweep_fade_canon", entry=100.0, sl=99.0, tp=103.5)

    ex._start_maker_nonfill_shadow(base, "entry_timeout", 100.0)
    ex._start_maker_nonfill_shadow(canon, "entry_timeout", 100.0)
    rows = db.maker_nonfill_shadow_rows()
    assert len(rows) == 1
    assert rows[0]["trade_id"] == 1
    assert rows[0]["target_r"] == pytest.approx(1.5)
    db.close()


# ─── v0.18.40: Evidence-first setup-specific observational telemetry ────────

def test_signal_setup_is_optional_and_detector_populates_sweep_geometry():
    plain = Signal("SOLUSDT", "long", 100.0, 99.0, 103.0, 4, ["x"])
    assert plain.setup is None

    det = SweepReclaimDetector("SOLUSDT", _cfg())
    det.update(_snap(_arm_samples(), last_price=96.5), now=100.0)
    sig = det.update(_snap(_fire_samples(), last_price=97.6), now=130.0)
    assert sig is not None
    f = sig.setup
    assert f["setup_type"] == "sweep_reclaim"
    assert f["level_type"] == "micro_extreme"
    assert f["prior_price"] == pytest.approx(98.0)
    assert f["swept_price"] == pytest.approx(96.5)
    assert f["sweep_depth_bps"] == pytest.approx((1.5 / 98.0) * 1e4)
    assert f["reclaim_duration_sec"] == pytest.approx(30.0)
    assert f["level_age_sec"] is None and f["level_touches"] is None
    assert f["cvd_divergence_magnitude"] > 0
    assert f["cvd_reversal_magnitude"] > 0


def test_density_setups_capture_wall_and_break_geometry():
    bounce = DensityBounceStrategy(_density_cfg(), ["SOLUSDT"])
    bids, asks = _book_with_bid_wall()
    snap = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                 bids=bids, asks=asks)
    bounce.update(snap, now=0.0)
    bsig = bounce.update(snap, now=11.0)
    assert bsig is not None and bsig.setup["setup_type"] == "density_bounce"
    assert bsig.setup["wall_age_sec"] == pytest.approx(11.0)
    assert bsig.setup["wall_initial_size"] == pytest.approx(50.0)
    assert bsig.setup["wall_max_size"] == pytest.approx(50.0)
    assert bsig.setup["wall_baseline"] == pytest.approx(1.0)
    assert bsig.setup["wall_ratio"] == pytest.approx(50.0)
    assert bsig.setup["retest_delay_sec"] is None

    brk = DensityBreakStrategy(
        _density_cfg(density_break_confirm_bar_sec=60.0), ["SOLUSDT"])
    abids, aasks = _ask_wall_book(50.0)
    _persist_then(brk, abids, aasks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    arm = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                bids=flat_bids, asks=flat_asks)
    assert brk.update(arm, now=16.0) is None
    sig = brk.update(arm, now=70.0)
    assert sig is not None and sig.setup["setup_type"] == "density_break"
    assert sig.setup["break_depth_bps"] == pytest.approx(30.0)
    assert sig.setup["confirm_duration_sec"] == pytest.approx(54.0)
    assert sig.setup["wall_removal_speed"] is not None


def test_setup_features_db_typed_xor_and_idempotent(tmp_path):
    import sqlite3

    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(
        symbol="SOLUSDT", side="long", qty=1.0, entry=100.0, sl=99.0,
        tp=103.0, score=4, reasons="x", mode="paper", ts_open=1.0)
    features = {
        "setup_type": "sweep_reclaim", "level_type": "pdl",
        "level_price": 99.0, "level_touches": None,
        "sweep_depth_bps": 12.5, "cvd_reversal_magnitude": 42.0,
    }
    first = db.insert_setup_features(
        trade_id=tid, strategy="sweep_fade", features=features, ts=1.0)
    second = db.insert_setup_features(
        trade_id=tid, strategy="sweep_fade",
        features={**features, "sweep_depth_bps": 20.0}, ts=2.0)
    assert first is not None and second is not None
    rows = db.setup_feature_rows()
    assert len(rows) == 1
    assert rows[0]["trade_id"] == tid and rows[0]["shadow_signal_id"] is None
    assert rows[0]["sweep_depth_bps"] == 20.0
    types = {r["name"]: r["type"] for r in
             db._conn.execute("PRAGMA table_info(setup_features)")}
    assert types["level_touches"] == "INTEGER"
    assert types["setup_type"] == "TEXT"
    assert types["sweep_depth_bps"] == "REAL"
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO setup_features "
            "(ts,strategy,trade_id,shadow_signal_id,setup_type) "
            "VALUES (1,'x',NULL,NULL,'x')")
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO setup_features "
            "(ts,strategy,trade_id,shadow_signal_id,setup_type) "
            "VALUES (1,'x',1,1,'x')")
    db.close()


def test_setup_features_migration_adds_missing_typed_columns(tmp_path):
    import sqlite3

    path = str(tmp_path / "scalp_bot.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE setup_features (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, strategy TEXT NOT NULL,
            trade_id INTEGER UNIQUE, shadow_signal_id INTEGER UNIQUE,
            setup_type TEXT);
    """)
    conn.close()
    db = ScalpDB(str(tmp_path))
    cols = {r["name"]: r["type"] for r in
            db._conn.execute("PRAGMA table_info(setup_features)")}
    assert cols["wall_age_sec"] == "REAL"
    assert cols["level_touches"] == "INTEGER"
    db._migrate()  # повторный запуск идемпотентен
    db.close()


def test_executor_and_shadow_log_setup_features_with_fail_open(tmp_path):
    db = ScalpDB(str(tmp_path))
    sig = _live_sig()
    sig.setup = {"setup_type": "density_break", "wall_age_sec": 15.0}
    tid = Executor(
        db, _live_cfg(setup_features_log_enabled=True),
        client=_FakeLiveClient(db)).on_signal(sig)
    assert tid is not None
    row = db.setup_feature_rows()[0]
    assert row["trade_id"] == tid and row["shadow_signal_id"] is None

    from scalp_bot.app.main import _log_shadow
    cfg = SimpleNamespace(
        shadow_log_enabled=True, setup_features_log_enabled=True)
    blocked = Signal("SOLUSDT", "long", 100.0, 99.0, 103.0, 4, ["x"],
                     strategy="density_bounce",
                     setup={"setup_type": "density_bounce", "wall_age_sec": 20.0})
    _log_shadow(db, cfg, blocked, "htf_align",
                _snap([CvdSample(1, 100, 0)]), None, None, 100.0)
    rows = db.setup_feature_rows()
    assert len(rows) == 2
    assert rows[1]["trade_id"] is None
    assert rows[1]["shadow_signal_id"] == db.shadow_rows()[0]["id"]

    class _BoomDB:
        def insert_open(self, **kwargs):
            return 77

        def insert_setup_features(self, **kwargs):
            raise RuntimeError("telemetry down")

    paper_cfg = SimpleNamespace(
        trading_enabled=False, risk_based_sizing=False, position_usd=100.0,
        min_position_usd=0.0, setup_features_log_enabled=True)
    assert Executor(_BoomDB(), paper_cfg, now=lambda: 1.0).on_signal(blocked) == 77
    db.close()


def test_setup_features_flag_default_enabled():
    from scalp_bot.config.settings import ScalpSettings
    assert ScalpSettings().setup_features_log_enabled is True


# ─── v0.18.41: preregistered shadow meta-labels (никаких trading effects) ───

def test_fade_exhaustion_pure_score_and_missing_is_unknown():
    regime = {
        "ret_autocorr": -0.20,
        "price_slope_bps_min": -3.0,  # adverse для long
        "tape_accel": 1.4,
    }
    setup = {"cvd_reversal_magnitude": 12.0}
    out = fade_exhaustion(regime, setup, "long")
    assert out["label_type"] == "fade_exhaustion"
    assert out["aligned_adverse_slope_bps_min"] == pytest.approx(3.0)
    assert out["component_count"] == 4
    assert out["meta_score"] == 4 and out["would_keep"] == 1
    # Pure: входы не мутируются.
    assert regime["price_slope_bps_min"] == -3.0
    missing = fade_exhaustion({}, {}, "long")
    assert missing["component_count"] == 0
    assert missing["meta_score"] == 0 and missing["would_keep"] is None


def test_breakout_fuel_side_aligns_cvd_and_scores_components():
    regime = {
        "htf_natr_pct": 0.8,
        "htf_bb_width_pct": 1.5,
        "oi_delta_pct": 0.2,
        "cvd_slope": -4.0,
    }
    short = breakout_fuel(regime, None, "short")
    assert short["cvd_follow_through_value"] == pytest.approx(4.0)
    assert short["meta_score"] == 4 and short["would_keep"] == 1
    long = breakout_fuel(regime, None, "long")
    assert long["cvd_follow_through_component"] == 0
    assert long["meta_score"] == 3 and long["would_keep"] == 1


def test_meta_label_db_typed_xor_idempotence_and_migration(tmp_path):
    import sqlite3

    db = ScalpDB(str(tmp_path))
    tid = db.insert_open(
        symbol="SOLUSDT", side="long", qty=1.0, entry=100.0, sl=99.0,
        tp=103.0, score=4, reasons="x", mode="paper", ts_open=1.0)
    first = db.insert_meta_label_features(
        trade_id=tid, strategy="sweep_fade",
        features={"label_type": "fade_exhaustion", "meta_score": 2,
                  "would_keep": 0}, ts=1.0)
    second = db.insert_meta_label_features(
        trade_id=tid, strategy="sweep_fade",
        features={"label_type": "fade_exhaustion", "meta_score": 4,
                  "would_keep": 1}, ts=2.0)
    rows = db.meta_label_feature_rows()
    assert first is not None and second is not None and len(rows) == 1
    assert rows[0]["meta_score"] == 4 and rows[0]["would_keep"] == 1
    types = {r["name"]: r["type"] for r in
             db._conn.execute("PRAGMA table_info(meta_label_features)")}
    assert types["meta_score"] == "INTEGER"
    assert types["aligned_adverse_slope_bps_min"] == "REAL"
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO meta_label_features "
            "(ts,strategy,trade_id,shadow_signal_id,label_type) "
            "VALUES (1,'x',NULL,NULL,'x')")
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO meta_label_features "
            "(ts,strategy,trade_id,shadow_signal_id,label_type) "
            "VALUES (1,'x',1,1,'x')")
    db._migrate()
    db._migrate()  # повторная migration идемпотентна
    db.close()

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    conn = sqlite3.connect(legacy / "scalp_bot.sqlite")
    conn.execute(
        "CREATE TABLE meta_label_features ("
        "id INTEGER PRIMARY KEY, ts REAL NOT NULL, strategy TEXT NOT NULL,"
        "trade_id INTEGER UNIQUE, shadow_signal_id INTEGER UNIQUE,"
        "label_type TEXT)")
    conn.commit()
    conn.close()
    migrated = ScalpDB(str(legacy))
    migrated_cols = {r["name"]: r["type"] for r in migrated._conn.execute(
        "PRAGMA table_info(meta_label_features)")}
    assert migrated_cols["meta_score"] == "INTEGER"
    assert migrated_cols["cvd_follow_through_value"] == "REAL"
    migrated._migrate()
    migrated.close()


def test_meta_label_actual_shadow_and_failure_are_fail_open(tmp_path):
    from scalp_bot.app.main import _log_shadow

    db = ScalpDB(str(tmp_path))
    sig = _live_sig()
    sig.meta_label = breakout_fuel(
        {"htf_natr_pct": 1.0, "htf_bb_width_pct": 2.0,
         "oi_delta_pct": 0.1, "cvd_slope": 1.0},
        None, "long")
    tid = Executor(
        db, _live_cfg(meta_label_log_enabled=True),
        client=_FakeLiveClient(db)).on_signal(sig)
    assert tid is not None
    assert db.meta_label_feature_rows()[0]["trade_id"] == tid

    blocked = Signal(
        "SOLUSDT", "long", 100.0, 99.0, 103.0, 4, ["x"],
        strategy="sweep_fade",
        setup={"cvd_reversal_magnitude": 2.0},
    )
    cfg = SimpleNamespace(
        shadow_log_enabled=True, setup_features_log_enabled=False,
        meta_label_log_enabled=True)
    _log_shadow(
        db, cfg, blocked, "htf_align",
        _snap([CvdSample(1, 100, 0)]), None, None, 100.0,
        feats={"ret_autocorr": -0.2, "price_slope_bps_min": -2.0,
               "tape_accel": 1.2})
    shadow_meta = db.meta_label_feature_rows()[1]
    assert shadow_meta["trade_id"] is None
    assert shadow_meta["shadow_signal_id"] == db.shadow_rows()[0]["id"]

    class _BoomDB:
        def insert_open(self, **kwargs):
            return 88

        def insert_meta_label_features(self, **kwargs):
            raise RuntimeError("telemetry down")

    paper_cfg = SimpleNamespace(
        trading_enabled=False, risk_based_sizing=False, position_usd=100.0,
        min_position_usd=0.0, setup_features_log_enabled=False,
        meta_label_log_enabled=True)
    assert Executor(_BoomDB(), paper_cfg, now=lambda: 1.0).on_signal(sig) == 88
    db.close()


def test_meta_score_cannot_change_resolve_or_entry_size(tmp_path):
    from scalp_bot.analysis.strategies import resolve, resolve_reset_state

    low_trading = Signal(
        "SOLUSDT", "long", 100.0, 99.0, 103.0, 4, ["a"],
        strategy="sweep_fade",
        meta_label={"meta_score": 4, "would_keep": 1},
    )
    high_trading = Signal(
        "SOLUSDT", "long", 100.0, 99.0, 103.0, 5, ["b"],
        strategy="density_break",
        meta_label={"meta_score": 0, "would_keep": 0},
    )
    resolve_reset_state()
    assert resolve([low_trading, high_trading]) is high_trading

    db = ScalpDB(str(tmp_path))
    cfg = SimpleNamespace(
        trading_enabled=False, risk_based_sizing=True, risk_per_trade_usd=10.0,
        min_position_usd=0.0, setup_features_log_enabled=False,
        meta_label_log_enabled=True)
    first = Executor(db, cfg, now=lambda: 1.0).on_signal(low_trading)
    second = Executor(db, cfg, now=lambda: 2.0).on_signal(
        Signal(
            "ETHUSDT", "long", 100.0, 99.0, 103.0, 4, ["a"],
            strategy="sweep_fade",
            meta_label={"meta_score": 0, "would_keep": 0},
        ))
    qtys = [row["qty"] for row in db._conn.execute(
        "SELECT qty FROM trades WHERE id IN (?,?) ORDER BY id", (first, second))]
    assert qtys == pytest.approx([10.0, 10.0])
    db.close()


def test_meta_label_flag_default_enabled():
    from scalp_bot.config.settings import ScalpSettings
    assert ScalpSettings().meta_label_log_enabled is True


# ─── v0.18.42: общий causal counterfactual tracker ──────────────────────

def _counter_candidate(**over):
    base = dict(
        candidate_key="test:1", setup_type="test_shadow", variant="v1",
        strategy="test", symbol="SOLUSDT", side="long",
        ts_candidate=90.0, ts_entry=100.0, entry=100.0, sl=99.0, tp=103.5,
        target_r=1.5, horizon_sec=10_800.0, checkpoint_sec=3_600.0,
    )
    base.update(over)
    return CounterfactualCandidate(**base)


def test_counterfactual_is_causal_and_deduplicates_snapshots():
    row = _counter_candidate().as_row()
    # До hypothetical entry никакой sample не может изменить outcome.
    assert advance_counterfactual(row, 102.0, sample_ts=99.0, now=101.0) is False
    assert row.get("sample_count") is None
    assert row.get("outcome_target") is None
    # Первый будущий snapshot засчитывается.
    assert advance_counterfactual(row, 101.6, sample_ts=101.0, now=101.0) is True
    assert row["outcome_target"] == "target"
    assert row["sample_count"] == 1
    # Повтор того же WS snapshot не раздувает sample_count/MFE.
    assert advance_counterfactual(row, 102.5, sample_ts=101.0, now=102.0) is False
    assert row["sample_count"] == 1
    assert row["mfe_r"] == pytest.approx(1.6)


def test_counterfactual_db_idempotent_typed_and_resume(tmp_path):
    db = ScalpDB(str(tmp_path))
    tracker = CounterfactualTracker(
        db, SimpleNamespace(counterfactual_enabled=True,
                            counterfactual_max_active=10,
                            counterfactual_flush_sec=60.0))
    first = tracker.add(_counter_candidate())
    second = tracker.add(_counter_candidate())
    assert first == second
    assert len(db.counterfactual_rows()) == 1
    types = {r["name"]: r["type"] for r in
             db._conn.execute("PRAGMA table_info(counterfactual_setups)")}
    assert types["level_touches"] == "INTEGER"
    assert types["retest_delay_sec"] == "REAL"
    db.close()

    db2 = ScalpDB(str(tmp_path))
    resumed = CounterfactualTracker(
        db2, SimpleNamespace(counterfactual_enabled=True,
                             counterfactual_max_active=10,
                             counterfactual_flush_sec=60.0),
        now=lambda: 110.0)
    assert resumed.active_count == 1
    resumed.update_snapshot(SimpleNamespace(
        symbol="SOLUSDT", stale=False, last_price=101.6, ts=110.0), now=110.0)
    got = db2.counterfactual_rows()[0]
    assert got["outcome_target"] == "target" and got["sample_count"] == 1
    db2.close()


def test_counterfactual_advances_when_snapshot_clock_is_monotonic(tmp_path):
    """v0.18.46 регрессия: SymbolSnapshot.ts идёт по time.monotonic (окна
    CVD/liq защищены от прыжков NTP), а ts_entry — по wall-clock. Трекер брал
    sample_ts из snap.ts, поэтому guard `sample_ts < ts_entry` резал КАЖДЫЙ
    sample: 4927 строк зависли в pending с нулём наблюдений при горизонте 3ч.

    Момент наблюдения обязан браться из wall-clock `now`, а не из часов снимка.
    """
    wall = 1_785_100_000.0
    db = ScalpDB(str(tmp_path))
    tracker = CounterfactualTracker(
        db, SimpleNamespace(counterfactual_enabled=True,
                            counterfactual_max_active=10,
                            counterfactual_flush_sec=60.0),
        now=lambda: wall)
    tracker.add(_counter_candidate(
        ts_candidate=wall - 10.0, ts_entry=wall, candidate_key="mono:1"))
    # snap.ts — монотонные «11 млн секунд аптайма», на 9 порядков меньше epoch.
    tracker.update_snapshot(
        SimpleNamespace(symbol="SOLUSDT", stale=False, last_price=101.6,
                        ts=11_221_865.4),
        now=wall + 5.0)
    got = db.counterfactual_rows()[0]
    assert got["sample_count"] == 1, "sample с монотонного снимка отброшен"
    assert got["outcome_target"] == "target"
    # Записанный таймстемп исхода — в wall-clock, иначе отчёты по датам врут.
    assert got["ts_outcome_target"] == pytest.approx(wall + 5.0)
    db.close()


def test_counterfactual_reaches_final_after_horizon(tmp_path):
    """Полный путь до терминального состояния: без фикса часов строка не
    доходила даже до checkpoint, поэтому forward-checkpoint вечно показывал
    n=0 и READY_FOR_STATS был недостижим в принципе."""
    wall = 1_785_100_000.0
    db = ScalpDB(str(tmp_path))
    tracker = CounterfactualTracker(
        db, SimpleNamespace(counterfactual_enabled=True,
                            counterfactual_max_active=10,
                            counterfactual_flush_sec=60.0),
        now=lambda: wall)
    tracker.add(_counter_candidate(
        ts_candidate=wall, ts_entry=wall, candidate_key="mono:2",
        horizon_sec=3_600.0))
    tracker.update_snapshot(
        SimpleNamespace(symbol="SOLUSDT", stale=False, last_price=100.2,
                        ts=5.0),
        now=wall + 3_601.0)
    got = db.counterfactual_rows()[0]
    assert got["state"] == "final"
    assert tracker.active_count == 0  # терминальные выселяются из памяти
    db.close()


def test_counterfactual_abandons_candidates_of_rotated_out_symbols(tmp_path):
    """Символ мог уйти из вселенной при ротации — тогда update_snapshot по нему
    не вызовется уже никогда. Такие строки закрываем как abandoned после
    горизонта: иначе они копятся и вытесняют живых кандидатов из лимита
    counterfactual_max_active. outcome_* остаются NULL → в статистику не идут."""
    wall = 1_785_100_000.0
    db = ScalpDB(str(tmp_path))
    tracker = CounterfactualTracker(
        db, SimpleNamespace(counterfactual_enabled=True,
                            counterfactual_max_active=10,
                            counterfactual_flush_sec=60.0),
        now=lambda: wall)
    tracker.add(_counter_candidate(
        ts_candidate=wall, ts_entry=wall, candidate_key="rot:1",
        horizon_sec=3_600.0))
    # SOLUSDT больше не в states — наблюдать некому, но горизонт ещё не вышел.
    tracker.update_states({"BTCUSDT": object()}, now=wall + 100.0)
    assert tracker.active_count == 1
    tracker.update_states({"BTCUSDT": object()}, now=wall + 3_601.0)
    got = db.counterfactual_rows()[0]
    assert got["state"] == "abandoned"
    assert got["outcome_target"] is None and got["outcome_tp"] is None
    assert tracker.active_count == 0
    db.close()


def test_clock_bug_rows_voided_once_and_live_rows_untouched(tmp_path):
    """v0.18.46: одноразовый ремонт помечает осиротевшие строки, но не рисует
    им исходы (no-data-fitting) и не трогает строки с наблюдениями.

    Защёлка user_version обязательна: без неё ремонт срабатывал бы при каждом
    старте и гасил живых кандидатов, которые просто ещё не набрали sample.
    """
    db = ScalpDB(str(tmp_path))
    old = ScalpDB._CLOCK_BUG_CUTOFF_TS - 3_600.0
    for key, samples in (("dead:1", 0), ("alive:1", 4)):
        db.insert_counterfactual_setup(_counter_candidate(
            candidate_key=key, ts_candidate=old, ts_entry=old).as_row())
        if samples:
            db._conn.execute(
                "UPDATE counterfactual_setups SET sample_count=? "
                "WHERE candidate_key=?", (samples, key))
    # Строка уже после cutoff — свежая, ремонт её не касается.
    db.insert_counterfactual_setup(_counter_candidate(
        candidate_key="fresh:1", ts_candidate=ScalpDB._CLOCK_BUG_CUTOFF_TS + 60,
        ts_entry=ScalpDB._CLOCK_BUG_CUTOFF_TS + 60).as_row())
    db._conn.execute("PRAGMA user_version = 0")  # эмулируем БД до ремонта
    db._conn.commit()
    db.close()

    db2 = ScalpDB(str(tmp_path))
    states = {r["candidate_key"]: r["state"] for r in db2.counterfactual_rows()}
    assert states["dead:1"] == "void_clock_bug"
    assert states["alive:1"] == "pending"
    assert states["fresh:1"] == "pending"
    voided = [r for r in db2.counterfactual_rows()
              if r["candidate_key"] == "dead:1"][0]
    # Исходы НЕ дорисованы: строка выпадает из отчётов по фильтру outcome_*.
    assert voided["outcome_target"] is None and voided["outcome_tp"] is None
    assert int(db2._conn.execute("PRAGMA user_version").fetchone()[0]) == 1
    db2.close()

    # Повторный старт: ремонт не перезапускается, живые строки целы.
    db3 = ScalpDB(str(tmp_path))
    db3._conn.execute(
        "UPDATE counterfactual_setups SET sample_count=0 "
        "WHERE candidate_key='alive:1'")
    db3.close()
    db4 = ScalpDB(str(tmp_path))
    again = {r["candidate_key"]: r["state"] for r in db4.counterfactual_rows()}
    assert again["alive:1"] == "pending"
    db4.close()


def test_legacy_maker_rows_migrate_and_dual_write(tmp_path):
    db = ScalpDB(str(tmp_path))
    db.insert_maker_nonfill_shadow(
        trade_id=77, ts_signal=90.0, ts_nonfill=100.0,
        symbol="ZECUSDT", side="long", strategy="sweep_fade",
        nonfill_reason="entry_timeout", entry=100.0, sl=99.0, tp=103.5,
        target_r=1.5)
    db.close()
    # Reopen запускает безопасную INSERT OR IGNORE migration.
    db = ScalpDB(str(tmp_path))
    rows = db.counterfactual_rows()
    assert len(rows) == 1 and rows[0]["candidate_key"] == "maker_nonfill:77"
    tracker = CounterfactualTracker(
        db, SimpleNamespace(counterfactual_enabled=True,
                            counterfactual_max_active=10,
                            counterfactual_flush_sec=60.0),
        now=lambda: 110.0)
    tracker.update_snapshot(SimpleNamespace(
        symbol="ZECUSDT", stale=False, last_price=101.6, ts=110.0), now=110.0)
    assert db.maker_nonfill_shadow_rows()[0]["outcome_1_5r"] == "target"
    db.close()


def test_density_bounce_shadow_grid_uses_one_track_without_real_signal():
    cfg = _density_cfg(
        density_bounce_persist_sec=300.0,
        density_bounce_shadow_enabled=True,
        density_bounce_shadow_persist_grid=(60, 90, 120, 180),
        counterfactual_horizon_sec=10_800.0,
        counterfactual_checkpoint_sec=3_600.0)
    st = DensityBounceStrategy(cfg, ["SOLUSDT"])
    bids, asks = _book_with_bid_wall()
    snap = _snap([], last_price=100.05, best_bid=100.0, best_ask=100.10,
                 bids=bids, asks=asks)
    assert st.update(snap, now=0.0) is None
    assert st.update(snap, now=61.0) is None  # production persist=300 unchanged
    first = st.drain_shadow_candidates()
    assert [c.wall_persist_sec for c in first] == [60.0]
    assert st.update(snap, now=181.0) is None
    rest = st.drain_shadow_candidates()
    assert [c.wall_persist_sec for c in rest] == [90.0, 120.0, 180.0]
    assert len({c.source_track_key for c in first + rest}) == 1
    # Повторный update не создаёт duplicate candidates.
    st.update(snap, now=182.0)
    assert st.drain_shadow_candidates() == []


def test_density_break_v2_waits_future_retest_even_when_v1_gate_blocks(tmp_path):
    cfg = _density_cfg(
        density_break_confirm_bar_sec=60.0,
        density_break_confirm_cvd=True, density_break_require_ob=False,
        density_break_v2_shadow_enabled=True,
        density_break_v2_retest_timeout_sec=180.0,
        counterfactual_horizon_sec=10_800.0,
        counterfactual_checkpoint_sec=3_600.0)
    st = DensityBreakStrategy(cfg, ["SOLUSDT"])
    bids, asks = _ask_wall_book(50.0)
    _persist_then(st, bids, asks, last=99.96)
    flat_bids, flat_asks = _flat_book_above()
    arm = _snap(
        [CvdSample(14, 100.3, 10), CvdSample(15, 100.3, 5)],
        ts=16.0, last_price=100.3, best_bid=100.29, best_ask=100.31,
        bids=flat_bids, asks=flat_asks)
    assert st.update(arm, now=16.0) is None
    close = _snap(
        [CvdSample(60, 100.3, 10), CvdSample(70, 100.3, 0)],
        ts=70.0, last_price=100.3, best_bid=100.29, best_ask=100.31,
        bids=flat_bids, asks=flat_asks)
    # V1 заблокирован CVD gate, но V2 retest state всё равно создан.
    assert st.update(close, now=70.0) is None
    waiting = st.drain_shadow_candidates()
    assert len(waiting) == 1 and waiting[0].state == "waiting_retest"
    db = ScalpDB(str(tmp_path))
    tracker = CounterfactualTracker(
        db, SimpleNamespace(counterfactual_enabled=True,
                            counterfactual_max_active=10,
                            counterfactual_flush_sec=60.0))
    tracker.add(waiting[0])
    touch = _snap(
        [CvdSample(70, 100.02, 0), CvdSample(71, 100.02, 0)],
        ts=71.0, last_price=100.02, bids=flat_bids, asks=flat_asks)
    tracker.update_snapshot(touch, now=71.0)
    assert db.counterfactual_rows()[0]["state"] == "holding"
    db.close()
    db = ScalpDB(str(tmp_path))
    tracker = CounterfactualTracker(
        db, SimpleNamespace(counterfactual_enabled=True,
                            counterfactual_max_active=10,
                            counterfactual_flush_sec=60.0))
    hold = _snap(
        [CvdSample(71, 100.02, 0), CvdSample(72, 100.03, 10)],
        ts=72.0, last_price=100.03, bids=flat_bids, asks=flat_asks)
    tracker.update_snapshot(hold, now=72.0)
    candidate = db.counterfactual_rows()[0]
    # CVD подтвердился после первого касания, когда цена уже выше wall.
    # LIMIT@wall ещё не существовал на прошлом касании → ретро-fill запрещён.
    assert candidate["state"] == "waiting_entry_fill"
    assert candidate["retest_hold_sec"] == pytest.approx(1.0)
    db.close()

    # Restart сохраняет armed limit; только БУДУЩЕЕ касание реально «наполняет».
    db = ScalpDB(str(tmp_path))
    tracker = CounterfactualTracker(
        db, SimpleNamespace(counterfactual_enabled=True,
                            counterfactual_max_active=10,
                            counterfactual_flush_sec=60.0))
    fill = _snap(
        [CvdSample(72, 100.03, 10), CvdSample(73, 99.99, 11)],
        ts=73.0, last_price=99.99, bids=flat_bids, asks=flat_asks)
    tracker.update_snapshot(fill, now=73.0)
    candidate = db.counterfactual_rows()[0]
    assert candidate["state"] == "pending"
    assert candidate["setup_type"] == "density_break_v2_shadow"
    assert candidate["entry"] == pytest.approx(100.0)  # hypothetical LIMIT@wall
    assert candidate["v1_signal_created"] == 0
    assert candidate["retest_delay_sec"] == pytest.approx(3.0)
    assert candidate["retest_hold_sec"] == pytest.approx(1.0)
    assert candidate["sample_count"] == 0  # entry snapshot не стал outcome
    db.close()


def test_density_break_v1_signal_isolated_from_v2_shadow_flag():
    def fire(enabled):
        cfg = _density_cfg(
            density_break_confirm_bar_sec=60.0,
            density_break_confirm_cvd=False,
            density_break_v2_shadow_enabled=enabled)
        st = DensityBreakStrategy(cfg, ["SOLUSDT"])
        bids, asks = _ask_wall_book(50.0)
        _persist_then(st, bids, asks, last=99.96)
        flat_bids, flat_asks = _flat_book_above()
        snap = _snap([], last_price=100.3, best_bid=100.29, best_ask=100.31,
                     bids=flat_bids, asks=flat_asks)
        st.update(snap, now=16.0)
        return st.update(snap, now=70.0)

    enabled, disabled = fire(True), fire(False)
    assert enabled is not None and disabled is not None
    assert (enabled.side, enabled.entry_ref, enabled.sl_level, enabled.tp_level,
            enabled.reasons) == (
        disabled.side, disabled.entry_ref, disabled.sl_level, disabled.tp_level,
        disabled.reasons)


def test_canon_rejection_shadow_preserves_typed_geometry():
    cfg = _canon_cfg(canon_rejection_shadow_enabled=True)
    st = SweepFadeCanonStrategy(cfg, ["ETHUSDT"])
    sig = Signal(
        "ETHUSDT", "short", 100.0, 101.0, 96.5, 5, ["x"],
        strategy="sweep_fade_canon",
        setup={
            "level_type": "pdh", "level_price": 100.0,
            "level_age_sec": 7200.0, "level_touches": 3,
            "swept_price": 100.2, "sweep_depth_bps": 20.0,
            "outside_duration_sec": 4.0, "reclaim_duration_sec": 9.0,
            "cvd_reversal_magnitude": 42.0,
        })
    st._det["ETHUSDT"].update = lambda snap, now: sig
    assert st.update(_snap([], symbol="ETHUSDT"), now=100.0) is sig
    candidate = st.drain_shadow_candidates()[0]
    assert candidate.variant == "pdh"
    assert candidate.level_age_sec == pytest.approx(7200.0)
    assert candidate.level_touches == 3
    assert candidate.sweep_depth_bps == pytest.approx(20.0)


def _canon_with_signal(**over):
    """Канон-страта, детектор которой отдаёт один и тот же сетап на любом тике."""
    from scalp_bot.analysis.strategies import SweepFadeCanonStrategy
    cfg = _canon_cfg(canon_rejection_shadow_enabled=True,
                     sweep_fade_sl_cooldown_sec=3600.0, **over)
    st = SweepFadeCanonStrategy(cfg, ["ETHUSDT"])
    sig = Signal(
        "ETHUSDT", "short", 100.0, 101.0, 96.5, 5, ["x"],
        strategy="sweep_fade_canon",
        setup={"level_type": "pdh", "level_price": 100.0,
               "level_age_sec": 7200.0, "level_touches": 3,
               "swept_price": 100.2, "sweep_depth_bps": 20.0})
    st._det["ETHUSDT"].update = lambda snap, now: sig
    return st


def test_canon_rejection_shadow_emits_once_per_sweep_episode():
    """v0.18.47: детектор отдаёт сигнал на каждом тике, тень — раз на эпизод.

    Замер 2026-07-28: 3076 строк = 120 эпизодов (25.6 дубля на событие), из-за
    чего WR по строкам 48.1% против 33.7% по эпизодам.
    """
    st = _canon_with_signal()
    snap = _snap([], symbol="ETHUSDT")
    for tick in range(0, 60, 2):  # 30 тиков живого сетапа за минуту
        st.update(snap, now=100.0 + tick)
    assert len(st.drain_shadow_candidates()) == 1


def test_canon_rejection_shadow_rearms_after_cooldown_window():
    """Повторный свип того же уровня спустя окно — НОВОЕ независимое событие."""
    st = _canon_with_signal()
    snap = _snap([], symbol="ETHUSDT")
    st.update(snap, now=100.0)
    st.update(snap, now=100.0 + 3599.0)   # внутри окна — дубль
    st.update(snap, now=100.0 + 3601.0)   # за окном — свежая возможность
    candidates = st.drain_shadow_candidates()
    assert len(candidates) == 2
    assert candidates[0].candidate_key != candidates[1].candidate_key


def test_canon_rejection_shadow_separates_distinct_levels():
    """Разные уровни и стороны — независимые эпизоды даже в один тик."""
    from scalp_bot.analysis.strategies import SweepFadeCanonStrategy
    cfg = _canon_cfg(canon_rejection_shadow_enabled=True,
                     sweep_fade_sl_cooldown_sec=3600.0)
    st = SweepFadeCanonStrategy(cfg, ["ETHUSDT"])
    setups = [("pdh", "short", 100.0), ("pdl", "long", 90.0),
              ("pdh", "short", 105.0)]
    snap = _snap([], symbol="ETHUSDT")
    for i, (level_type, side, price) in enumerate(setups):
        sig = Signal("ETHUSDT", side, price, price + 1, price - 3, 5, ["x"],
                     strategy="sweep_fade_canon",
                     setup={"level_type": level_type, "level_price": price,
                            "swept_price": price})
        st._det["ETHUSDT"].update = lambda snap, now, s=sig: s
        st.update(snap, now=100.0 + i)
    assert len(st.drain_shadow_candidates()) == 3


def test_canon_rejection_shadow_key_stable_within_window_after_restart():
    """После рестарта _shadow_last пуст — от задвоения спасает UNIQUE-ключ."""
    keys = []
    for _ in range(2):  # два «процесса» видят один и тот же эпизод
        st = _canon_with_signal()
        st.update(_snap([], symbol="ETHUSDT"), now=100.0)
        keys.append(st.drain_shadow_candidates()[0].candidate_key)
    assert keys[0] == keys[1]


def test_collapse_episodes_only_touches_duplicated_setup_types():
    from scripts.scalp_episodes import (DEDUPED_SETUP_TYPES,
                                        collapse_episodes, episode_counts)

    # Один свип, записанный тиками по 2с, плюс повторный свип за окном.
    rows = [{"symbol": "ETHUSDT", "side": "short", "level_type": "pdh",
             "level_price": 100.0, "ts_candidate": 1000.0 + 2 * i}
            for i in range(25)]
    rows.append({"symbol": "ETHUSDT", "side": "short", "level_type": "pdh",
                 "level_price": 100.0, "ts_candidate": 1000.0 + 7200.0})
    # Другой уровень в то же время — самостоятельное событие.
    rows.append({"symbol": "ETHUSDT", "side": "long", "level_type": "pdl",
                 "level_price": 90.0, "ts_candidate": 1004.0})
    raw, episodes = episode_counts(rows)
    assert (raw, episodes) == (27, 3)
    assert [r["ts_candidate"] for r in collapse_episodes(rows)] == [
        1000.0, 1004.0, 8200.0]
    # Схлопывание применяем ТОЛЬКО к canon: у остальных ключ уже событийный.
    assert DEDUPED_SETUP_TYPES == ("canon_rejection_shadow",)


def test_forward_checkpoint_counts_canon_episodes_not_rows(tmp_path):
    """Порог MIN_OUTCOMES не должен набираться дублями одного свипа."""
    import sqlite3
    from scripts.scalp_forward_checkpoint import collect_readiness

    db = tmp_path / "cp.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE counterfactual_setups (setup_type TEXT,variant TEXT,"
        "symbol TEXT,side TEXT,level_type TEXT,level_price REAL,"
        "ts_candidate REAL,outcome_target TEXT,outcome_tp TEXT,"
        "source_trade_id INTEGER)")
    con.execute("CREATE TABLE trades (id INTEGER,ts_open REAL,strategy TEXT,"
                "status TEXT,pnl_usd REAL,close_reason TEXT)")
    con.execute("CREATE TABLE meta_label_features (trade_id INTEGER,"
                "label_type TEXT,would_keep INTEGER)")
    con.executemany(
        "INSERT INTO counterfactual_setups VALUES "
        "('canon_rejection_shadow','pdh','ETHUSDT','short','pdh',100.0,?,"
        "'target',NULL,NULL)",
        [(2_000.0 + 2 * i,) for i in range(40)])
    con.commit()
    readiness = {r.hypothesis: r for r in collect_readiness(con, 0.0)}
    con.close()
    assert readiness["canon_rejection_redesign"].outcomes == 1


def test_counterfactual_defaults_and_bounce_grid_do_not_change_production_persist():
    from scalp_bot.config.settings import ScalpSettings
    cfg = ScalpSettings()
    assert cfg.counterfactual_enabled is True
    assert cfg.density_bounce_shadow_persist_grid == (60, 90, 120, 180)
    assert cfg.density_bounce_persist_sec == pytest.approx(300.0)


def test_forward_checkpoint_requires_both_sample_and_time():
    from scripts.scalp_forward_checkpoint import Readiness

    cutoff = 1_000_000.0
    assert not Readiness("x", 99, cutoff, cutoff + 20 * 86_400).ready
    assert not Readiness("x", 100, cutoff, cutoff + 13.99 * 86_400).ready
    ready = Readiness("x", 100, cutoff, cutoff + 14 * 86_400)
    assert ready.ready
    assert ready.span_days == pytest.approx(14.0)


# ─── v0.18.45: shadow-grid ширины стопа (observational) ────────────────────
# Аудит 2026-07-26: комиссия в R = fee_rate / SL%, при SL 0.300% это 0.34R и
# она съедает gross edge +0.114R. Ветки меряют, что даёт более широкий стоп
# при фиксированном $-риске. Торговлю не трогают.

def _sl_widen_cfg(**over):
    base = dict(sl_widen_shadow_enabled=True, flow_exit_activate_r=1.5,
                sl_widen_shadow_multipliers=(1.0, 2.0),
                sl_widen_shadow_horizon_sec=21_600.0,
                counterfactual_enabled=True, counterfactual_max_active=100)
    base.update(over)
    return SimpleNamespace(**base)


def test_sl_widen_grid_keeps_control_arm_and_rejects_garbage():
    """×1.0 обязана остаться в сетке (контроль), мусор и ≤0 отбрасываются."""
    from scalp_bot.config.settings import ScalpSettings
    cfg = ScalpSettings()
    assert cfg.sl_widen_shadow_multipliers == (1.0, 1.5, 2.0, 3.0)

    parsed = ScalpSettings(sl_widen_shadow_grid="2.0, 1.0,оп, -1, 0, 2.0")
    assert parsed.sl_widen_shadow_multipliers == (1.0, 2.0)


def test_sl_widen_shadow_geometry_scales_risk_and_keeps_rr(tmp_path):
    db = ScalpDB(str(tmp_path))
    ex = Executor(db, _sl_widen_cfg(), client=None, now=lambda: 500.0)
    # risk=1.0, TP=3.5R — боевая геометрия scalp_bot
    sig = Signal("ZECUSDT", "long", 100.0, 99.0, 103.5, 4, ["x"],
                 strategy="density_break")
    ex._start_sl_widen_shadows(7, sig, 100.0)

    rows = {r["variant"]: r for r in db.counterfactual_rows()
            if r["setup_type"] == "sl_widen"}
    assert set(rows) == {"x1", "x2"}
    control, wide = rows["x1"], rows["x2"]
    # контроль воспроизводит боевые уровни ровно
    assert control["sl"] == pytest.approx(99.0)
    assert control["tp"] == pytest.approx(103.5)
    # ×2: риск вдвое шире, R:R сохранён (TP тоже уезжает вдвое)
    assert wide["sl"] == pytest.approx(98.0)
    assert wide["tp"] == pytest.approx(107.0)
    assert wide["risk"] == pytest.approx(2.0)
    for row in rows.values():
        assert row["source_trade_id"] == 7
        assert row["horizon_sec"] == pytest.approx(21_600.0)
        assert (abs(row["tp"] - row["entry"])
                / row["risk"] == pytest.approx(3.5))
    db.close()


def test_sl_widen_shadow_mirrors_geometry_for_short(tmp_path):
    db = ScalpDB(str(tmp_path))
    ex = Executor(db, _sl_widen_cfg(), client=None, now=lambda: 500.0)
    sig = Signal("ETHUSDT", "short", 100.0, 101.0, 96.5, 4, ["x"],
                 strategy="sweep_fade")
    ex._start_sl_widen_shadows(9, sig, 100.0)

    rows = {r["variant"]: r for r in db.counterfactual_rows()
            if r["setup_type"] == "sl_widen"}
    assert rows["x1"]["sl"] == pytest.approx(101.0)
    assert rows["x1"]["tp"] == pytest.approx(96.5)
    # шорт: стоп уезжает ВВЕРХ, цель — ВНИЗ
    assert rows["x2"]["sl"] == pytest.approx(102.0)
    assert rows["x2"]["tp"] == pytest.approx(93.0)
    db.close()


def test_sl_widen_shadow_is_idempotent_and_can_be_disabled(tmp_path):
    db = ScalpDB(str(tmp_path))
    sig = Signal("ZECUSDT", "long", 100.0, 99.0, 103.5, 4, ["x"],
                 strategy="density_break")

    off = Executor(db, _sl_widen_cfg(sl_widen_shadow_enabled=False),
                   client=None, now=lambda: 500.0)
    off._start_sl_widen_shadows(1, sig, 100.0)
    assert [r for r in db.counterfactual_rows()
            if r["setup_type"] == "sl_widen"] == []

    on = Executor(db, _sl_widen_cfg(), client=None, now=lambda: 500.0)
    on._start_sl_widen_shadows(2, sig, 100.0)
    on._start_sl_widen_shadows(2, sig, 100.0)   # повтор того же tid
    rows = [r for r in db.counterfactual_rows() if r["setup_type"] == "sl_widen"]
    assert len(rows) == 2      # по одной строке на множитель, дублей нет
    db.close()


def test_sl_widen_shadow_never_blocks_trade_on_failure(tmp_path):
    """Падение shadow-ветки не должно ронять открытие сделки (fail-open)."""
    db = ScalpDB(str(tmp_path))
    ex = Executor(db, _sl_widen_cfg(), client=None, now=lambda: 500.0)

    class _Boom:
        def add(self, candidate):
            raise RuntimeError("counterfactual down")

    ex._counterfactual = _Boom()
    sig = Signal("ZECUSDT", "long", 100.0, 99.0, 103.5, 4, ["x"],
                 strategy="density_break")
    ex._start_sl_widen_shadows(3, sig, 100.0)   # не должно бросить
    db.close()


def test_sl_widen_report_expectancy_and_pairing():
    """Ключевая арифметика отчёта: комиссия в R = fee% / SL%, netR =
    grossR − комиссия, а парное сравнение считает исходы на ОДНИХ сделках."""
    from scripts.scalp_sl_widen_report import _summarise, _paired

    def row(variant, risk, outcome, tid):
        # entry=100, TP=3.5R — боевая геометрия
        return {"variant": variant, "entry": 100.0, "risk": risk,
                "tp": 100.0 + 3.5 * risk, "state": "final",
                "outcome_tp": outcome, "source_trade_id": tid}

    # x1: SL 0.3% цены; x2 вдвое шире. По 4 исхода в каждой ветке.
    rows = [
        row("x1", 0.3, "tp", 1), row("x1", 0.3, "sl", 2),
        row("x1", 0.3, "sl", 3), row("x1", 0.3, "sl", 4),
        row("x2", 0.6, "tp", 1), row("x2", 0.6, "tp", 2),
        row("x2", 0.6, "sl", 3), row("x2", 0.6, "sl", 4),
    ]
    arms = _summarise(rows, fee_pct=0.1016)

    control = arms["x1"]
    assert control["decided"] == 4 and control["tp"] == 1
    assert control["sl_pct"] == pytest.approx(0.3)
    assert control["fee_r"] == pytest.approx(0.1016 / 0.3)
    # gross = 0.25×3.5 − 0.75×1.0 = 0.125
    assert control["gross_r"] == pytest.approx(0.125)
    assert control["net_r"] == pytest.approx(0.125 - 0.1016 / 0.3)

    wide = arms["x2"]
    # вдвое шире стоп ⇒ вдвое меньше комиссия в R
    assert wide["fee_r"] == pytest.approx(control["fee_r"] / 2)
    # gross = 0.5×3.5 − 0.5×1.0 = 1.25
    assert wide["gross_r"] == pytest.approx(1.25)

    pairs = _paired(rows, control="x1")
    both, wins, losses = pairs["x2"]
    assert both == 4        # все четыре сделки решены в обеих ветках
    assert wins == 1        # сделка #2: x2 взяла TP там, где x1 словила SL
    assert losses == 0


def _shadow_universe_db(tmp_path, rows, trades=()):
    """Мини-БД со схемой, достаточной для read-only отчётов."""
    import sqlite3
    path = tmp_path / "shadow.sqlite"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE counterfactual_setups (
        id INTEGER PRIMARY KEY, setup_type TEXT, variant TEXT, symbol TEXT,
        side TEXT, ts_candidate REAL, outcome_tp TEXT, outcome_target TEXT,
        mfe_r REAL, mae_r REAL, state TEXT, level_type TEXT, level_price REAL,
        source_trade_id INTEGER)""")
    con.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY, ts_open REAL, strategy TEXT, pnl_usd REAL,
        entry REAL, sl REAL, qty REAL, close_reason TEXT, status TEXT)""")
    # нужна checkpoint-скрипту: он считает и meta-гейты
    con.execute("""CREATE TABLE meta_label_features (
        trade_id INTEGER, ts REAL, label_type TEXT, would_keep INTEGER)""")
    con.executemany(
        "INSERT INTO counterfactual_setups (setup_type,variant,symbol,side,"
        "ts_candidate,outcome_tp,mfe_r,mae_r,state) VALUES (?,?,?,?,?,?,?,?,?)",
        rows)
    con.executemany(
        "INSERT INTO trades (ts_open,strategy,pnl_usd,entry,sl,qty,"
        "close_reason) VALUES (?,?,?,?,?,?,?)", trades)
    con.commit()
    con.close()
    return str(path)


def test_shadow_universe_report_separates_arms_and_control(tmp_path, capsys):
    """Отчёт считает TP-rate по решённым исходам и сравнивает с боевыми
    сделками ТОЙ ЖЕ стратегии: у страт разные R:R, агрегат смешал бы их."""
    from scripts.scalp_shadow_universe_report import (live_control,
                                                      shadow_arms)
    import sqlite3

    base = 1_785_000_000.0
    rows = [
        ("shadow_universe", "sweep_fade", "TAOUSDT", "short", base, "tp",
         2.0, -0.4, "final"),
        ("shadow_universe", "sweep_fade", "TAOUSDT", "short", base + 3600,
         "sl", 0.3, -1.0, "final"),
        ("shadow_universe", "sweep_fade", "ONDOUSDT", "long", base + 7200,
         "sl", 0.2, -1.0, "final"),
        # ещё не решён — в decided не попадает, но в N попадает
        ("shadow_universe", "sweep_fade", "XPLUSDT", "long", base + 10800,
         None, 0.5, -0.2, "pending"),
        ("shadow_universe", "density_break", "TAOUSDT", "long", base + 1800,
         "tp", 3.0, -0.5, "final"),
        # чужой setup_type не должен просочиться
        ("sl_widen", "x2", "ZECUSDT", "long", base, "tp", 1.0, -0.1, "final"),
    ]
    trades = [
        (base + 100, "sweep_fade", 20.0, 100.0, 99.0, 10.0, "tp_hit"),
        (base + 200, "sweep_fade", -10.0, 100.0, 99.0, 10.0, "sl_hit"),
        # отклонённый вход — не сделка, в контроль не берём
        (base + 300, "sweep_fade", 0.0, 100.0, 99.0, 10.0, "entry_Rejected"),
    ]
    db = _shadow_universe_db(tmp_path, rows, trades)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    arms = shadow_arms(con, base - 1)
    control = live_control(con, base - 1)
    con.close()

    assert set(arms) == {"sweep_fade", "density_break"}, "только свой setup_type"
    sweep = arms["sweep_fade"]
    assert sweep["n"] == 4 and sweep["decided"] == 3
    assert sweep["tp"] == 1 and sweep["sl"] == 2
    assert sweep["symbols"]["TAOUSDT"] == 2
    assert sweep["last"] - sweep["first"] == pytest.approx(10800.0)

    assert control["sweep_fade"]["n"] == 2, "entry_Rejected не сделка"
    assert control["sweep_fade"]["wins"] == 1
    # R = pnl / (|entry-sl| × qty): +20/10 = +2.0 и −10/10 = −1.0
    assert sorted(control["sweep_fade"]["r"]) == pytest.approx([-1.0, 2.0])


def test_forward_checkpoint_tracks_shadow_universe_per_strategy(tmp_path):
    """Чекпоинт видит теневую вселенную и группирует по стратегии: порог мог
    быть вреден одной страте и полезен другой."""
    from scripts.scalp_forward_checkpoint import collect_readiness
    import sqlite3

    base = 1_785_000_000.0
    rows = [("shadow_universe", "sweep_fade", "TAOUSDT", "short",
             base + i * 86_400, "tp" if i % 2 else "sl", 1.0, -0.5, "final")
            for i in range(20)]
    rows += [("shadow_universe", "density_break", "ONDOUSDT", "long",
              base, "tp", 1.0, -0.5, "final")]
    db = _shadow_universe_db(tmp_path, rows)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    got = {r.hypothesis: r for r in collect_readiness(con, base - 1)}
    con.close()
    assert got["shadow_universe_sweep_fade"].outcomes == 20
    assert got["shadow_universe_sweep_fade"].span_days == pytest.approx(19.0)
    assert got["shadow_universe_sweep_fade"].ready is False, "19 дней, но n<100"
    assert got["shadow_universe_density_break"].outcomes == 1
    # ветка без исходов не должна выдумывать строку
    assert "shadow_universe_x2" not in got


def test_sl_widen_shadow_skips_degenerate_geometry(tmp_path):
    """Нулевой риск или нулевая цель → ветку не заводим (нечего мерить)."""
    db = ScalpDB(str(tmp_path))
    ex = Executor(db, _sl_widen_cfg(), client=None, now=lambda: 500.0)
    ex._start_sl_widen_shadows(
        4, Signal("XUSDT", "long", 100.0, 100.0, 103.5, 4, ["x"]), 100.0)
    ex._start_sl_widen_shadows(
        5, Signal("XUSDT", "long", 100.0, 99.0, 100.0, 4, ["x"]), 100.0)
    assert [r for r in db.counterfactual_rows()
            if r["setup_type"] == "sl_widen"] == []
    db.close()

