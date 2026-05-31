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
from scalp_bot.trading.executor import (
    Executor, paper_pnl, position_size, position_size_by_risk, taker_pnl,
)


# ─── helpers ─────────────────────────────────────────────────────────────

def _cfg(**over):
    base = dict(
        min_confluence=3, liq_flush_usd=50000.0,
        funding_extreme_pos=0.0005, funding_extreme_neg=0.0003,
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


# ─── fee-aware дискреционный выход sweep_fade (через should_exit) ───────────

def _sweep_strat(now_t=None):
    from scalp_bot.analysis.strategies import SweepFadeStrategy
    cfg = SimpleNamespace(active_exit_enabled=True, active_exit_min_age_sec=10.0,
                          momentum_window_sec=3.0, round_trip_fee_frac=0.0011,
                          scratch_on_flow_flip=True, scratch_min_age_sec=20.0,
                          flow_exit_activate_r=1.0)  # v0.7.1: профит-лок ≥1R
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
    # ход −0.20 (≥ round-trip 97×0.0011≈0.107) + поток против + созрела (25с) →
    # режем убыток рано (flow_scratch), не ждём SL/тайм-стоп
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=96.80)
    tr = SimpleNamespace(id=5, side="long", entry=97.0, sl=96.80, ts_open=80.0)
    decision = st.should_exit(tr, snap, now=105.0)
    assert decision is not None and decision[0] == "flow_scratch"
    assert decision[1] == pytest.approx(96.80)


def test_flow_scratch_skips_small_underwater():
    # мелкий минус −0.05 < комиссии → НЕ скретчим (иначе −fee на шуме)
    st = _sweep_strat()
    snap = _snap(_flow_flip_samples(), last_price=96.95)
    tr = SimpleNamespace(id=6, side="long", entry=97.0, sl=96.80, ts_open=80.0)
    assert st.should_exit(tr, snap, now=105.0) is None


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
    SweepFadeStrategy,
    build_strategies,
    detect_wall,
    near_round,
    resolve,
)
from scalp_bot.data.universe import rank_universe  # noqa: E402
from scalp_bot.state.db import ScalpDB  # noqa: E402


def _sig(side, score, strategy="sweep_fade", symbol="SOLUSDT"):
    return Signal(symbol=symbol, side=side, entry_ref=100.0, sl_level=99.0,
                  tp_level=102.0, score=score, reasons=["x"], strategy=strategy)


def test_resolve_none_when_empty():
    assert resolve([]) is None


def test_resolve_same_side_picks_highest_score():
    a = _sig("long", 4, "sweep_fade")
    b = _sig("long", 6, "density_bounce")
    assert resolve([a, b]) is b  # выше score


def test_resolve_conflicting_sides_skips():
    # long и short по одному символу → неоднозначность → не берём ничего
    assert resolve([_sig("long", 5), _sig("short", 9, "density_bounce")]) is None


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
        density_absorb_frac=0.30, density_absorb_window_sec=10.0,
        density_near_bps=8.0, density_min_wall_usd=0.0,
        # для build_signal:
        entry_order_type="market", sl_buffer_bps=8.0, take_profit_r=2.0,
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
