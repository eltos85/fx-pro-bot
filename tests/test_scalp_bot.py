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
    flow_invalidated,
    ob_supportive,
    reclaimed,
    reversal_momentum,
)
from scalp_bot.data.aggregates import CvdSample, LiqEvent, SymbolSnapshot, SymbolState
from scalp_bot.safety import killswitch
from scalp_bot.trading.executor import (
    Executor, bracket_exit_reason, paper_pnl, position_size,
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
    всегда первая по порядку страта. canon/run/trend дают идентичные сигналы —
    без вращения canon забирал бы 100%, варианты 0. Каждому новому кластеру
    (отличный fingerprint = другой уровень входа) — следующая страта."""
    resolve_reset_state()
    canon = _sig("short", 6, "sweep_fade_canon", symbol="BTCUSDT")
    run = _sig("short", 6, "sweep_fade_run", symbol="BTCUSDT")
    trend = _sig("short", 6, "sweep_fade_trend", symbol="BTCUSDT")
    # кластер 1 (entry 100.0): первый раз — canon (idx 0)
    c1 = [_sig("short", 6, "sweep_fade_canon", symbol="BTCUSDT"),
          _sig("short", 6, "sweep_fade_run", symbol="BTCUSDT"),
          _sig("short", 6, "sweep_fade_trend", symbol="BTCUSDT")]
    assert resolve(list(c1)).strategy == "sweep_fade_canon"
    # тот же кластер (тот же fp) — стабильный победитель, canon снова
    assert resolve(list(c1)).strategy == "sweep_fade_canon"
    # кластер 2 (другой уровень entry=200.0) — следующий по ротации: run (idx 1)
    c2 = [Signal(symbol="BTCUSDT", side="short", entry_ref=200.0, sl_level=99.0,
                 tp_level=202.0, score=6, reasons=["x"], strategy=s)
          for s in ("sweep_fade_canon", "sweep_fade_run", "sweep_fade_trend")]
    assert resolve(c2).strategy == "sweep_fade_run"
    # кластер 3 (entry=300.0) — trend (idx 2)
    c3 = [Signal(symbol="BTCUSDT", side="short", entry_ref=300.0, sl_level=99.0,
                 tp_level=302.0, score=6, reasons=["x"], strategy=s)
          for s in ("sweep_fade_canon", "sweep_fade_run", "sweep_fade_trend")]
    assert resolve(c3).strategy == "sweep_fade_trend"
    # кластер 4 — снова canon (idx 3 % 3 = 0)
    c4 = [Signal(symbol="BTCUSDT", side="short", entry_ref=400.0, sl_level=99.0,
                 tp_level=402.0, score=6, reasons=["x"], strategy=s)
          for s in ("sweep_fade_canon", "sweep_fade_run", "sweep_fade_trend")]
    assert resolve(c4).strategy == "sweep_fade_canon"


def test_resolve_round_robin_independent_per_symbol():
    """Ротация per-symbol: BTCUSDT и ETHUSDT ведут независимые счётчики."""
    resolve_reset_state()
    mk = lambda sym, entry, strat: Signal(symbol=sym, side="short",
            entry_ref=entry, sl_level=99.0, tp_level=entry + 2.0, score=6,
            reasons=["x"], strategy=strat)
    # BTC кластер1 → canon
    assert resolve([mk("BTCUSDT", 100.0, "sweep_fade_canon"),
                    mk("BTCUSDT", 100.0, "sweep_fade_run")]).strategy == "sweep_fade_canon"
    # ETH кластер1 → canon (свой счётчик, не继承 BTC)
    assert resolve([mk("ETHUSDT", 100.0, "sweep_fade_canon"),
                    mk("ETHUSDT", 100.0, "sweep_fade_run")]).strategy == "sweep_fade_canon"
    # BTC кластер2 → run; ETH кластер2 → run (независимо)
    assert resolve([mk("BTCUSDT", 200.0, "sweep_fade_canon"),
                    mk("BTCUSDT", 200.0, "sweep_fade_run")]).strategy == "sweep_fade_run"
    assert resolve([mk("ETHUSDT", 200.0, "sweep_fade_canon"),
                    mk("ETHUSDT", 200.0, "sweep_fade_run")]).strategy == "sweep_fade_run"


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
    """Изоляция: density_bounce НЕ задет — фейр НЕ гейтится CVD/ob-правками
    density_break (пустой CVD + ob против НЕ блокируют), сигнал несёт
    entry_order_type=None (глобальный maker)."""
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
    assert sig.entry_order_type is None        # глобальный maker, не задет


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


# ─── v0.18.27: sweep_fade_run (изолированная гипотеза «дай winners бежать») ─

def _run_cfg(**over):
    """cfg для SweepFadeRunStrategy: canon-вход + run-exit параметры."""
    base = _canon_cfg(
        sweep_fade_run_symbol_list=["ETHUSDT"],
        sweep_fade_run_take_profit_r=3.0,
        sweep_fade_run_be_activate_r=1.0,
        sweep_fade_run_scratch_on_flow_flip=True,
        # should_exit опирается на active_exit_enabled + scratch_* + momentum
        active_exit_enabled=True,
        active_exit_min_age_sec=0.0,
        scratch_min_adverse_r=0.7,
        scratch_min_age_sec=0.0,
        take_profit_r=3.0,
    )
    for k, v in over.items():
        setattr(base, k, v)
    return base


class _FakeClient:
    """Минимальный client-мок для manage_levels: round_price + set_trading_stop."""

    def __init__(self, *, ok=True):
        self._ok = ok
        self.calls = []

    def round_price(self, symbol, price):
        return round(price, 4)

    def set_trading_stop(self, symbol, *, sl_price=None, tp_price=None):
        self.calls.append({"symbol": symbol, "sl": sl_price, "tp": tp_price})
        return {"ok": self._ok, "error": "" if self._ok else "boom"}


class _FakeLevelsDB:
    def __init__(self):
        self.updates = []

    def update_levels(self, trade_id, *, sl, tp):
        self.updates.append({"id": trade_id, "sl": sl, "tp": tp})


def _tr(*, side="long", entry=100.0, sl=99.0, tp=103.0, ts_open=0.0,
         strategy="sweep_fade_run"):
    """Лёгкий trade-объект: атрибуты, которые читают should_exit/manage_levels.
    side='long': entry=100, sl=99 → risk=1 (|entry-sl|); tp=103 → base_risk=1
    при tpr=3. Проверяем favourable в R."""
    t = SimpleNamespace(id=1, symbol="ETHUSDT", side=side, entry=entry,
                        sl=sl, tp=tp, ts_open=ts_open, strategy=strategy)
    return t


def _snap_at(price, *, momentum_for="long"):
    """snap с last_price и cvd_samples, дающими flow_invalidated по стороне.
    momentum_for='long' → CVD растёт (flow_invalidated long=False, short=True).
    'short' → CVD падает (flow_invalidated short=False, long=True).
    'none' → плоский (ни одна сторона не инвалидирована)."""
    if momentum_for == "long":
        s = [CvdSample(1, price, -2), CvdSample(2, price, -1),
             CvdSample(3, price, 0), CvdSample(4, price, 1), CvdSample(5, price, 2)]
    elif momentum_for == "short":
        s = [CvdSample(1, price, 2), CvdSample(2, price, 1),
             CvdSample(3, price, 0), CvdSample(4, price, -1), CvdSample(5, price, -2)]
    else:
        s = [CvdSample(1, price, 0), CvdSample(2, price, 0),
             CvdSample(3, price, 0), CvdSample(4, price, 0), CvdSample(5, price, 0)]
    return _snap(s, symbol="ETHUSDT", last_price=price)


def test_run_in_registry_and_defaults():
    """sweep_fade_run зарегистрирован, в дефолтном strategy_list, наследует
    canon-вход (htf_filtered=False, regime_gated=True, taker)."""
    from scalp_bot.analysis.strategies import (build_strategies, SweepFadeRunStrategy,
                                               SweepFadeCanonStrategy)
    cfg = _run_cfg()
    cfg.strategy_list = ["sweep_fade_run"]
    out = build_strategies(cfg, ["ETHUSDT"])
    assert [s.name for s in out] == ["sweep_fade_run"]
    r = out[0]
    assert isinstance(r, SweepFadeRunStrategy)
    assert isinstance(r, SweepFadeCanonStrategy)  # наследует canon-вход
    assert r.htf_filtered is False and r.regime_gated is True  # как canon
    assert r.symbol_scope == {"ETHUSDT"}
    # дефолтные settings включают run в strategy_list
    from scalp_bot.config.settings import ScalpSettings
    assert "sweep_fade_run" in ScalpSettings().strategy_list
    # cooldown семейства fade распространяется на run
    s = ScalpSettings()
    assert s.sl_cooldown_for("sweep_fade_run") == s.sweep_fade_sl_cooldown_sec


def test_run_symbol_scope_defaults_to_canon():
    """Пустой SCALP_SWEEP_FADE_RUN_SYMBOLS → canon-список (чистый A/B)."""
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()  # sweep_fade_run_symbols="" по дефолту
    assert s.sweep_fade_run_symbol_list == s.sweep_fade_canon_symbol_list


def test_run_breakeven_lock_long_moves_sl_to_entry():
    """manage_levels: long favourable≥1.0R → SL переносится к entry+буфер,
    биржа амендится (set_trading_stop), БД обновляется, повторный перенос
    запрещён (_be_locked). entry=100, sl=99 → risk=1, be_activate=1.0R →
    favourable≥1.0 (price≥101) триггерит."""
    from scalp_bot.analysis.strategies import SweepFadeRunStrategy
    st = SweepFadeRunStrategy(_run_cfg(), ["ETHUSDT"])
    cl = _FakeClient()
    db = _FakeLevelsDB()
    tr = _tr(side="long", entry=100.0, sl=99.0, tp=103.0)
    # favourable 0.5R — не достиг порога → SL не трогаем
    st.manage_levels(tr, _snap_at(100.5), cl, db)
    assert tr.sl == 99.0 and cl.calls == [] and db.updates == []
    assert not getattr(tr, "_be_locked", False)
    # favourable 1.0R (price=101) → перенос
    st.manage_levels(tr, _snap_at(101.0), cl, db)
    assert tr._be_locked is True
    assert cl.calls and cl.calls[0]["sl"] > 100.0  # к entry+буфер (буфер 8bps)
    assert tr.sl > 100.0 and tr.sl < 101.0  # буфер малый
    assert db.updates and db.updates[0]["id"] == tr.id
    # повторный вызов — no-op (защита уже стоит)
    n_before = len(cl.calls)
    st.manage_levels(tr, _snap_at(102.0), cl, db)
    assert len(cl.calls) == n_before


def test_run_breakeven_lock_short_moves_sl_down():
    """short: entry=100, sl=101 → risk=1; favourable≥1.0R (price≤99) → SL
    вниз к entry−буфер."""
    from scalp_bot.analysis.strategies import SweepFadeRunStrategy
    st = SweepFadeRunStrategy(_run_cfg(), ["ETHUSDT"])
    cl = _FakeClient()
    db = _FakeLevelsDB()
    tr = _tr(side="short", entry=100.0, sl=101.0, tp=97.0)
    st.manage_levels(tr, _snap_at(99.0), cl, db)  # favourable=1.0R
    assert tr._be_locked is True
    assert tr.sl < 100.0 and tr.sl > 99.0
    assert cl.calls and cl.calls[0]["sl"] < 100.0


def test_run_breakeven_not_weakening_sl():
    """Если be-уровень НЕ уменьшает убыток (long: new_sl ≤ текущего SL) —
    не ослабляем защиту, просто фиксируем _be_locked. Кейс: SL уже подтянут
    выше entry вручную/предыдущим циклом."""
    from scalp_bot.analysis.strategies import SweepFadeRunStrategy
    st = SweepFadeRunStrategy(_run_cfg(), ["ETHUSDT"])
    cl = _FakeClient()
    tr = _tr(side="long", entry=100.0, sl=100.5, tp=103.0)  # SL уже выше entry
    st.manage_levels(tr, _snap_at(101.0), cl, _FakeLevelsDB())
    assert cl.calls == []  # не амендим (new_sl ≈ entry+buf < 100.5)
    assert tr._be_locked is True  # но защиту не откатываем


def test_run_should_exit_no_flow_exit_for_winners():
    """ГЛАВНОЕ отличие от base: winner (favorable>0) при развороте ленты НЕ
    режется flow_exit. base срезал бы на 1.5R; run держит — winner защищён
    breakeven-стопом и бежит к TP. Должен вернуть None."""
    from scalp_bot.analysis.strategies import SweepFadeRunStrategy
    st = SweepFadeRunStrategy(_run_cfg(), ["ETHUSDT"])
    tr = _tr(side="long", entry=100.0, sl=99.0, tp=103.0)
    # price=102 → favourable=2.0R, лента развернулась против (momentum short)
    # → base срезал бы flow_exit; run возвращает None (winner бежит)
    assert st.should_exit(tr, _snap_at(102.0, momentum_for="short"), now=10.0) is None


def test_run_should_exit_scratch_losing_side():
    """Losing-side scratch: favourable<0 (в минусе) + лента против + убыток
    достиг scratch_min_adverse_r (0.7R) → режем. price=99.3 → favourable=-0.7R."""
    from scalp_bot.analysis.strategies import SweepFadeRunStrategy
    st = SweepFadeRunStrategy(_run_cfg(), ["ETHUSDT"])
    tr = _tr(side="long", entry=100.0, sl=99.0, tp=103.0)
    # лента против long (momentum short), favourable=-0.7R
    res = st.should_exit(tr, _snap_at(99.3, momentum_for="short"), now=10.0)
    assert res is not None and res[0] == "flow_scratch"


def test_run_should_exit_no_scratch_when_flow_still_with_position():
    """Лента ещё за позицию (не инвалидирована) → держим, даже в минусе.
    Полагаемся на биржевой SL."""
    from scalp_bot.analysis.strategies import SweepFadeRunStrategy
    st = SweepFadeRunStrategy(_run_cfg(), ["ETHUSDT"])
    tr = _tr(side="long", entry=100.0, sl=99.0, tp=103.0)
    # momentum long → flow_invalidated(long)=False → держим
    assert st.should_exit(tr, _snap_at(99.3, momentum_for="long"), now=10.0) is None


def test_run_should_exit_scratch_disabled():
    """sweep_fade_run_scratch_on_flow_flip=False → scratch выключен, держим до
    биржевого SL (только breakeven + TP)."""
    from scalp_bot.analysis.strategies import SweepFadeRunStrategy
    st = SweepFadeRunStrategy(_run_cfg(sweep_fade_run_scratch_on_flow_flip=False),
                              ["ETHUSDT"])
    tr = _tr(side="long", entry=100.0, sl=99.0, tp=103.0)
    assert st.should_exit(tr, _snap_at(99.3, momentum_for="short"), now=10.0) is None


def test_run_entry_identical_to_canon():
    """Вход — идентичен canon (изоляция exit-переменной): тот же signal side,
    strategy name, key_pdl в reasons, taker-вход. Разница только name."""
    from scalp_bot.analysis.strategies import SweepFadeRunStrategy, SweepFadeCanonStrategy
    cfg = _run_cfg()
    run = SweepFadeRunStrategy(cfg, ["ETHUSDT"])
    run.key_levels = _FakeKeyLevels("pdl")
    # взвод
    assert run.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5),
                      now=100.0) is None
    assert run.armed("ETHUSDT") is True
    # полный reclaim → выстрел, как у canon
    full = [CvdSample(20, 97.8, -1), CvdSample(21, 97.9, 0), CvdSample(22, 98.0, 1),
            CvdSample(23, 98.0, 2), CvdSample(24, 98.05, 3), CvdSample(25, 98.1, 4)]
    sig = run.update(_snap(full, symbol="ETHUSDT", last_price=98.1), now=120.0)
    assert sig is not None and sig.side == "long"
    assert sig.strategy == "sweep_fade_run"  # отличие только в имени
    assert "key_pdl" in sig.reasons and sig.entry_order_type == "market"
    # TP = 3.0R (run-override), не глобальный 2.0 из _cfg
    assert abs(sig.tp_level - sig.entry_ref - 3.0 * abs(sig.entry_ref - sig.sl_level)) < 1e-9


def test_run_isolated_from_base_and_canon():
    """run-страта не трогает поведение base/canon: у них нет manage_levels,
    их should_exit остался canon-контрактом (None для canon)."""
    from scalp_bot.analysis.strategies import (SweepFadeStrategy,
                                               SweepFadeCanonStrategy,
                                               SweepFadeRunStrategy)
    assert not hasattr(SweepFadeStrategy, "manage_levels")
    assert not hasattr(SweepFadeCanonStrategy, "manage_levels")
    assert hasattr(SweepFadeRunStrategy, "manage_levels")
    # canon should_exit наследует base → None (run переопределён на scratch)
    canon = SweepFadeCanonStrategy(_canon_cfg(), ["ETHUSDT"])
    tr = _tr(side="long", entry=100.0, sl=99.0, tp=103.0, strategy="sweep_fade_canon")
    assert canon.should_exit(tr, _snap_at(102.0, momentum_for="short"), now=10.0) is None


# ─── v0.18.27: sweep_fade_trend (canon + rolling-trend-day-gate) ──────────

def _trend_cfg(**over):
    """cfg для SweepFadeTrendStrategy: canon + trend-gate параметры."""
    base = _canon_cfg(
        sweep_fade_trend_symbol_list=["ETHUSDT"],
        sweep_fade_trend_max=1.5,
        sweep_fade_trend_lookback_bars=8,
    )
    for k, v in over.items():
        setattr(base, k, v)
    return base


class _FakeKeyLevelsRegime:
    """KeyLevels-мок с regime_ratio (для trend-gate)."""

    def __init__(self, ratio=None, level_name="pdl"):
        self._ratio = ratio  # dict symbol -> ratio, или scalar, или None
        self.name = level_name

    def regime_ratio(self, symbol):
        if self._ratio is None:
            return None
        if isinstance(self._ratio, dict):
            return self._ratio.get(symbol)
        return self._ratio

    def swept_key_level(self, symbol, side, swept):
        return self.name


def test_trend_in_registry_and_defaults():
    """sweep_fade_trend зарегистрирован, в дефолтном strategy_list, наследует
    canon-вход."""
    from scalp_bot.analysis.strategies import (build_strategies, SweepFadeTrendStrategy,
                                               SweepFadeCanonStrategy)
    cfg = _trend_cfg()
    cfg.strategy_list = ["sweep_fade_trend"]
    out = build_strategies(cfg, ["ETHUSDT"])
    assert [s.name for s in out] == ["sweep_fade_trend"]
    t = out[0]
    assert isinstance(t, SweepFadeTrendStrategy)
    assert isinstance(t, SweepFadeCanonStrategy)  # наследует canon-вход
    assert t.htf_filtered is False and t.regime_gated is True  # как canon
    assert t.symbol_scope == {"ETHUSDT"}
    from scalp_bot.config.settings import ScalpSettings
    assert "sweep_fade_trend" in ScalpSettings().strategy_list
    s = ScalpSettings()
    assert s.sl_cooldown_for("sweep_fade_trend") == s.sweep_fade_sl_cooldown_sec


def test_trend_symbol_scope_defaults_to_canon():
    """Пустой SCALP_SWEEP_FADE_TREND_SYMBOLS → canon-список (чистый A/B)."""
    from scalp_bot.config.settings import ScalpSettings
    s = ScalpSettings()
    assert s.sweep_fade_trend_symbol_list == s.sweep_fade_canon_symbol_list


def test_trend_gate_blocks_signal_in_active_trend():
    """regime_ratio > trend_max (активный тренд) → сигнал НЕ берётся, даже при
    canon-валидном взводе+выстреле. Главная гипотеза: не фейдить в тренде."""
    from scalp_bot.analysis.strategies import SweepFadeTrendStrategy
    st = SweepFadeTrendStrategy(_trend_cfg(sweep_fade_trend_max=1.5), ["ETHUSDT"])
    st.key_levels = _FakeKeyLevelsRegime(ratio=2.5)  # тренд
    st.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5), now=100.0)
    assert st.armed("ETHUSDT") is False  # gate блокнул взвод
    full = [CvdSample(20, 97.8, -1), CvdSample(21, 97.9, 0), CvdSample(22, 98.0, 1),
            CvdSample(23, 98.0, 2), CvdSample(24, 98.05, 3), CvdSample(25, 98.1, 4)]
    assert st.update(_snap(full, symbol="ETHUSDT", last_price=98.1), now=120.0) is None


def test_trend_gate_allows_signal_in_range():
    """regime_ratio ≤ trend_max (range/mix) → canon-вход работает, signal
    берётся с strategy='sweep_fade_trend'. Доказательство: gate не ломает
    canon-вход в не-тренде."""
    from scalp_bot.analysis.strategies import SweepFadeTrendStrategy
    st = SweepFadeTrendStrategy(_trend_cfg(sweep_fade_trend_max=1.5), ["ETHUSDT"])
    st.key_levels = _FakeKeyLevelsRegime(ratio=0.6)  # range
    st.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5), now=100.0)
    assert st.armed("ETHUSDT") is True  # canon-взвод прошёл
    full = [CvdSample(20, 97.8, -1), CvdSample(21, 97.9, 0), CvdSample(22, 98.0, 1),
            CvdSample(23, 98.0, 2), CvdSample(24, 98.05, 3), CvdSample(25, 98.1, 4)]
    sig = st.update(_snap(full, symbol="ETHUSDT", last_price=98.1), now=120.0)
    assert sig is not None and sig.side == "long"
    assert sig.strategy == "sweep_fade_trend"
    assert "key_pdl" in sig.reasons and sig.entry_order_type == "market"


def test_trend_gate_fail_closed_without_regime_data():
    """Нет key_levels / regime_ratio=None → fail-closed: не торгуем (как
    canon level_gate). Не фейдим вслепую без regime-данных."""
    from scalp_bot.analysis.strategies import SweepFadeTrendStrategy
    st = SweepFadeTrendStrategy(_trend_cfg(), ["ETHUSDT"])
    assert st.key_levels is None  # не инжектнут
    st.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5), now=100.0)
    assert st.armed("ETHUSDT") is False
    # с key_levels, но regime None (данные не прогреты)
    st2 = SweepFadeTrendStrategy(_trend_cfg(), ["ETHUSDT"])
    st2.key_levels = _FakeKeyLevelsRegime(ratio=None)
    st2.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5), now=100.0)
    assert st2.armed("ETHUSDT") is False


def test_trend_gate_threshold_boundary():
    """regime_ratio == trend_max → ещё торгуем (≤); чуть выше → блок. Граница
    включается в range (canon-вход берётся)."""
    from scalp_bot.analysis.strategies import SweepFadeTrendStrategy
    full = [CvdSample(20, 97.8, -1), CvdSample(21, 97.9, 0), CvdSample(22, 98.0, 1),
            CvdSample(23, 98.0, 2), CvdSample(24, 98.05, 3), CvdSample(25, 98.1, 4)]
    # точно на пороге 1.5 → range → берём
    st = SweepFadeTrendStrategy(_trend_cfg(sweep_fade_trend_max=1.5), ["ETHUSDT"])
    st.key_levels = _FakeKeyLevelsRegime(ratio=1.5)
    st.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5), now=100.0)
    assert st.armed("ETHUSDT") is True
    sig = st.update(_snap(full, symbol="ETHUSDT", last_price=98.1), now=120.0)
    assert sig is not None
    # 1.5001 → тренд → блок
    st2 = SweepFadeTrendStrategy(_trend_cfg(sweep_fade_trend_max=1.5), ["ETHUSDT"])
    st2.key_levels = _FakeKeyLevelsRegime(ratio=1.5001)
    st2.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5), now=100.0)
    assert st2.armed("ETHUSDT") is False


def test_trend_exit_inherited_from_canon():
    """should_exit — наследуется от canon (flow_exit@1.5R для winners). Exit
    НЕ переопределён (MFE canon: flow_exit не виноват, winners мелкие)."""
    from scalp_bot.analysis.strategies import SweepFadeTrendStrategy, SweepFadeCanonStrategy
    # trend не определяет свой should_exit → берёт canon (через base)
    assert "should_exit" not in SweepFadeTrendStrategy.__dict__
    assert "should_exit" not in SweepFadeCanonStrategy.__dict__  # canon тоже


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


def test_trend_gate_log_throttle_per_symbol():
    """v0.18.27 hotfix: gate-лог троттлится ПО СИМВОЛУ, а не одним флагом на
    стратегию. Был спам: main loop зовёт update поочерёдно для 5 символов;
    один флаг сбрасывался когда хотя бы один символ проходил gate, и
    заблокированный символ логировался каждый цикл (в проде 1290/час). Per-
    symbol dict: каждый символ логируется 1 раз пока заблокирован."""
    from scalp_bot.analysis.strategies import SweepFadeTrendStrategy
    # scope берётся из sweep_fade_trend_symbol_list (есть в cfg) — кладём оба
    # символа, иначе детектор создастся только для ETHUSDT и gate по BTCUSDT
    # не отработает (update вернётся по det is None до gate).
    st = SweepFadeTrendStrategy(
        _trend_cfg(sweep_fade_trend_max=1.5,
                   sweep_fade_trend_symbol_list=["ETHUSDT", "BTCUSDT"]),
        ["ETHUSDT", "BTCUSDT"])
    st.key_levels = _FakeKeyLevelsRegime(ratio={"ETHUSDT": 0.6, "BTCUSDT": 2.5})
    logs = []

    def _cap(msg, *a):
        logs.append(msg % a if a else msg)
    import scalp_bot.analysis.strategies as strat_mod
    orig = strat_mod.play.info
    strat_mod.play.info = _cap
    try:
        for _ in range(3):
            st.update(_snap(_arm_samples(), symbol="ETHUSDT", last_price=96.5), now=100.0)
            st.update(_snap(_arm_samples(), symbol="BTCUSDT", last_price=96000.0), now=100.0)
    finally:
        strat_mod.play.info = orig
    btc_logs = [l for l in logs if "BTCUSDT" in l and "trend-gate" in l]
    assert len(btc_logs) == 1, f"ожидал 1 лог BTCUSDT, got {len(btc_logs)}: {btc_logs}"
    assert not any("ETHUSDT" in l and "trend-gate" in l for l in logs)
