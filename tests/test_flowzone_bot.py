"""Тесты flowzone_bot (фазы 1-2): агрегаты, Volume Profile, контекст аукциона.

Позитивные сценарии — на ЧЕСТНОЙ синтетике с известным распределением объёма
(no-data-fitting.mdc: данные не рисуются «под результат», проверяется логика
формул VP/контекста).
"""
from __future__ import annotations

from flowzone_bot.analysis.context import BALANCE, TREND_DOWN, TREND_UP, classify
from flowzone_bot.analysis.orderflow import (big_trade_threshold,
                                             detect_absorption,
                                             detect_big_trades, size_percentile,
                                             zone_delta)
from flowzone_bot.analysis.swings import (find_swings, nearest_swing_target,
                                          swing_targets)
from flowzone_bot.analysis.volume_profile import (build_profile, find_hvn_lvn,
                                                  find_ledges)
from flowzone_bot.analysis.zone import build_zones
from flowzone_bot.data.aggregates import SymbolState, TradePrint


# ─── Фаза 1: агрегаты (footprint-принты + дневной VP) ────────────────────

def test_symbolstate_keeps_raw_prints_and_evicts():
    t = {"now": 1000.0}
    st = SymbolState("BTCUSDT", trade_window_sec=10.0, ob_levels=5,
                     now=lambda: t["now"])
    st.on_trade(100.0, 1.5, "Buy")
    st.on_trade(100.5, 2.0, "Sell")
    snap = st.snapshot()
    assert len(snap.trades) == 2
    assert snap.last_price == 100.5
    assert snap.trades[0].signed_delta == 1.5
    assert snap.trades[1].signed_delta == -2.0
    # вытеснение за окном
    t["now"] = 1011.0
    st.on_trade(101.0, 1.0, "Buy")
    assert len(st.snapshot().trades) == 1


def test_symbolstate_ob_imbalance():
    st = SymbolState("ETHUSDT", ob_levels=5)
    st.on_orderbook([(99.0, 3.0), (98.0, 2.0)], [(101.0, 1.0), (102.0, 1.0)])
    snap = st.snapshot()
    assert abs(snap.ob_imbalance - 5.0 / 7.0) < 1e-9


def test_symbolstate_incremental_vp_day_anchored():
    wall = {"t": 86400.0 * 100 + 10}  # внутри дня 100
    st = SymbolState("BTCUSDT", trade_window_sec=999.0, vp_bucket_size=1.0,
                     wall_now=lambda: wall["t"])
    st.on_trade(100.4, 2.0, "Buy")   # idx 100
    st.on_trade(100.9, 1.0, "Sell")  # idx 100
    st.on_trade(102.1, 4.0, "Buy")   # idx 102
    snap = st.snapshot()
    assert snap.vp_bucket_size == 1.0
    assert snap.vp_buckets[100] == (2.0, 1.0)
    assert snap.vp_buckets[102] == (4.0, 0.0)
    # смена дня сбрасывает профиль
    wall["t"] = 86400.0 * 101 + 5
    st.on_trade(103.0, 1.0, "Buy")
    snap2 = st.snapshot()
    assert set(snap2.vp_buckets) == {103}


# ─── Фаза 2: Volume Profile engine ───────────────────────────────────────

def _triangular_buckets() -> dict[int, tuple[float, float]]:
    """Симметричный треугольный профиль, пик на idx 10. (buy=vol, sell=0)."""
    vols = {10: 100, 9: 80, 11: 80, 8: 60, 12: 60, 7: 40, 13: 40,
            6: 20, 14: 20, 5: 10, 15: 10}
    return {i: (float(v), 0.0) for i, v in vols.items()}


def test_build_profile_poc_and_value_area():
    prof = build_profile(_triangular_buckets(), bucket_size=1.0,
                         value_area_pct=0.70)
    assert prof is not None
    assert prof.poc_idx == 10
    assert prof.poc_price == 10.5
    assert prof.total_volume == 520.0
    # двухрядное расширение от POC до ≥70% (364): VA idx 8..12
    assert prof.va_lo_idx == 8
    assert prof.va_hi_idx == 12
    assert prof.val == 8.0
    assert prof.vah == 13.0
    assert prof.value_area_volume >= 0.70 * prof.total_volume


def test_build_profile_empty_returns_none():
    assert build_profile({}, bucket_size=1.0) is None
    assert build_profile({1: (0.0, 0.0)}, bucket_size=1.0) is None
    assert build_profile({1: (5.0, 0.0)}, bucket_size=0.0) is None


def test_build_profile_delta_at_price():
    buckets = {10: (8.0, 2.0), 11: (1.0, 5.0)}
    prof = build_profile(buckets, bucket_size=1.0)
    assert prof.bucket_delta(10) == 6.0    # 8 buy − 2 sell
    assert prof.delta_at_price(11.5) == -4.0


def test_find_hvn_lvn_triangular():
    prof = build_profile(_triangular_buckets(), bucket_size=1.0)
    hvn, lvn = find_hvn_lvn(prof)
    assert hvn == [10]   # единственный пик
    assert lvn == []     # монотонные склоны → нет внутренних минимумов


def test_find_ledges_sharp_drop():
    # объём 100,100, затем обрыв до 10 (10% от пика) — резкий ledge.
    buckets = {0: (100.0, 0.0), 1: (100.0, 0.0), 2: (10.0, 0.0)}
    prof = build_profile(buckets, bucket_size=1.0)
    ledges = find_ledges(prof, drop_frac=0.5)
    assert len(ledges) == 1
    assert ledges[0].side == "above"
    assert ledges[0].price == 2.0
    assert ledges[0].drop_ratio == 0.1


# ─── Фаза 2: контекст аукциона (тренд vs баланс) ─────────────────────────
# classify v2 (STRATEGY §2, Steidlmayer/Dalton): режим по ФОРМЕ профиля —
# направленный acceptance ВНЕ value area (объём в хвостах за границами VA).
# Честная синтетика: ядро (≈80% объёма) даёт VA, дальний хвост = принятие вне VA.

def _down_elongated_buckets() -> dict[int, tuple[float, float]]:
    """Профиль, элонгированный ВНИЗ: тяжёлое ядро у idx20 + хвост ниже VAL."""
    core = {20: 100, 21: 70, 19: 70, 22: 30, 18: 30}           # ≈80% → value area
    tail = {10: 15, 11: 15, 12: 15, 13: 15, 14: 15}            # принято НИЖЕ VAL
    return {i: (float(v), 0.0) for i, v in {**core, **tail}.items()}


def _up_elongated_buckets() -> dict[int, tuple[float, float]]:
    """Профиль, элонгированный ВВЕРХ: ядро у idx20 + хвост выше VAH."""
    core = {20: 100, 21: 70, 19: 70, 22: 30, 18: 30}
    tail = {26: 15, 27: 15, 28: 15, 29: 15, 30: 15}            # принято ВЫШЕ VAH
    return {i: (float(v), 0.0) for i, v in {**core, **tail}.items()}


def test_classify_trend_up_on_acceptance_above_vah():
    prof = build_profile(_up_elongated_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=20.5, accept_frac=0.70)
    assert ctx.state == TREND_UP
    assert ctx.trade_side == "long"
    assert ctx.accept_above >= 0.70


def test_classify_trend_down_on_acceptance_below_val():
    prof = build_profile(_down_elongated_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=20.5, accept_frac=0.70)
    assert ctx.state == TREND_DOWN
    assert ctx.trade_side == "short"
    assert ctx.accept_below >= 0.70


def test_classify_balance_symmetric_profile():
    # симметричный треугольник: хвосты вне VA равны → нет направленного принятия.
    prof = build_profile(_triangular_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=10.5, accept_frac=0.70)
    assert ctx.state == BALANCE
    assert ctx.trade_side is None


def test_classify_balance_when_acceptance_below_threshold():
    # хвосты есть с обеих сторон, но перекос < 0.70 (60/40) → не acceptance.
    core = {20: 100, 21: 70, 19: 70, 22: 30, 18: 30}
    buckets = {i: (float(v), 0.0) for i, v in core.items()}
    buckets.update({12: (20.0, 0.0), 13: (20.0, 0.0), 14: (20.0, 0.0)})  # ↓ 60
    buckets.update({26: (20.0, 0.0), 27: (20.0, 0.0)})                   # ↑ 40
    prof = build_profile(buckets, bucket_size=1.0)
    ctx = classify(prof, last_price=20.5, accept_frac=0.70)
    assert ctx.state == BALANCE
    assert 0.55 <= ctx.accept_below <= 0.65


# ─── Фаза 3: order-flow (big trades + absorption) ────────────────────────

def test_size_percentile_and_big_threshold():
    assert size_percentile([], 0.9) is None
    assert size_percentile([5.0], 0.9) == 5.0
    assert size_percentile([0.0, 10.0], 0.5) == 5.0
    # мало сэмплов → None (sample-size)
    assert big_trade_threshold([TradePrint(0, 1, 1, "Buy")] * 5,
                               pct=0.9, min_samples=20) is None
    trades = [TradePrint(0, 1, float(i), "Buy") for i in range(1, 101)]
    thr = big_trade_threshold(trades, pct=0.90, min_samples=20)
    assert thr is not None and 89.0 <= thr <= 92.0


def test_detect_big_trades_side_filter():
    trades = [TradePrint(0, 1, 10.0, "Buy"), TradePrint(1, 1, 2.0, "Sell"),
              TradePrint(2, 1, 8.0, "Sell")]
    assert len(detect_big_trades(trades, 5.0)) == 2
    assert len(detect_big_trades(trades, 5.0, side="Sell")) == 1
    assert len(detect_big_trades(trades, 5.0, side="Buy")) == 1


def test_zone_delta_band():
    trades = [TradePrint(0, 100.0, 5.0, "Buy"), TradePrint(1, 100.5, 3.0, "Sell"),
              TradePrint(2, 101.0, 2.0, "Buy"), TradePrint(3, 99.0, 4.0, "Sell")]
    # полоса [100, 100.6] ловит первые два: +5 (buy) −3 (sell) = +2
    assert zone_delta(trades, 100.0, 100.6) == 2.0


def test_absorption_short_failed_buyers_confirmed():
    # агрессивные покупатели (включая крупного) давят, но цена не растёт.
    trades = [TradePrint(0, 100.0, 8.0, "Buy"),   # крупный buy (порог 5)
              TradePrint(1, 100.0, 2.0, "Sell"),
              TradePrint(2, 99.5, 1.0, "Sell")]   # price_move = −0.5
    res = detect_absorption(trades, "short", big_threshold=5.0,
                            min_counter_frac=0.5)
    assert res.confirmed
    assert res.big_counter == 1
    assert "price_absorbed" in res.reasons


def test_absorption_long_failed_sellers_confirmed():
    trades = [TradePrint(0, 100.0, 8.0, "Sell"),  # крупный sell
              TradePrint(1, 100.0, 2.0, "Buy"),
              TradePrint(2, 100.5, 1.0, "Buy")]   # price_move = +0.5
    res = detect_absorption(trades, "long", big_threshold=5.0,
                            min_counter_frac=0.5)
    assert res.confirmed


def test_absorption_rejected_when_price_follows_counter():
    # короткий сетап, но покупатели ПРОДАВИЛИ цену вверх → не поглощены.
    trades = [TradePrint(0, 100.0, 8.0, "Buy"),
              TradePrint(1, 100.0, 2.0, "Sell"),
              TradePrint(2, 100.6, 1.0, "Buy")]   # price_move = +0.6
    res = detect_absorption(trades, "short", big_threshold=5.0)
    assert not res.confirmed


def test_absorption_rejected_without_deep_trade():
    # нет крупной сделки контр-стороны (порог недостижим) → не absorption.
    trades = [TradePrint(0, 100.0, 1.0, "Buy"), TradePrint(1, 99.5, 1.0, "Sell")]
    res = detect_absorption(trades, "short", big_threshold=100.0)
    assert not res.confirmed


# ─── Фаза 4: zone builder (confluence) ───────────────────────────────────

def test_build_zones_confluence_poc_and_delta():
    # односторонний (buy) треугольный профиль: POC и delta-уровень совпадают на
    # одной корзине → конфлюэнс {poc, delta}. cluster_ticks=1 разводит дальние
    # (но края профиля дают ledge-зону у VAH — её не проверяем здесь).
    prof = build_profile(_triangular_buckets(), bucket_size=1.0)  # POC idx10
    zones = build_zones(prof, "short", ref_price=5.0, recent_trades=[],
                        big_threshold=None, min_confluence=2, cluster_ticks=1,
                        delta_min_frac=0.6)
    poc_delta = [z for z in zones if set(z.factors) == {"poc", "delta"}]
    assert len(poc_delta) == 1
    z = poc_delta[0]
    assert z.score == 2
    assert z.low <= 10.5 <= z.high
    assert z.side == "short"
    assert all(z.side == "short" for z in zones)


def test_build_zones_side_filter_keeps_only_continuation_side():
    prof = build_profile(_triangular_buckets(), bucket_size=1.0)
    # для шорта зоны только ВЫШЕ ref; ref выше всего профиля → ничего
    assert build_zones(prof, "short", ref_price=99.0, recent_trades=[],
                       big_threshold=None, min_confluence=2) == []
    # для лонга зоны только НИЖЕ ref; ref ниже всего → ничего
    assert build_zones(prof, "long", ref_price=0.0, recent_trades=[],
                       big_threshold=None, min_confluence=2) == []


def test_build_zones_below_min_confluence_dropped():
    # один изолированный фактор (cluster_ticks=0) не дотягивает до ≥2.
    prof = build_profile(_triangular_buckets(), bucket_size=1.0)
    zones = build_zones(prof, "short", ref_price=5.0, recent_trades=[],
                        big_threshold=None, min_confluence=3, cluster_ticks=0)
    assert zones == []


# ─── Фаза 4: strategy.evaluate (контекст → зона → absorption → Signal) ───

class _Cfg:
    """Минимальный cfg-стаб для evaluate (только нужные поля; дефолты канона)."""
    big_trade_pct = 0.90
    big_trade_min_samples = 3
    zone_min_confluence = 3          # «super strong area» (§3.4)
    zone_cluster_ticks = 5
    zone_delta_min_frac = 0.6
    absorption_window_sec = 300.0    # тело M5-свечи (§4, §6.3)
    absorption_min_counter_frac = 0.5
    sl_buffer_bps = 8.0
    min_sl_bps = 10.0


def _evictless_state_snapshot(buckets, bucket_size, trades, last_price, ts):
    from flowzone_bot.data.aggregates import SymbolSnapshot
    return SymbolSnapshot(
        symbol="BTCUSDT", ts=ts, last_price=last_price, best_bid=None,
        best_ask=None, ob_imbalance=None, trades=trades, stale=False,
        vp_bucket_size=bucket_size, vp_buckets=buckets)


def _short_reload_profile() -> dict[int, tuple[float, float]]:
    """Профиль элонгирован ВНИЗ (trend_down) + тяжёлое ядро idx118-122 как зона
    reload-резистанса ВЫШЕ цены для шорта. Все корзины buy-only (delta=vol)."""
    core = {120: 100, 121: 70, 119: 70, 122: 30, 118: 30}   # VA≈119-122, POC=120
    tail = {100: 15, 102: 15, 104: 15, 106: 15, 108: 15}    # принято НИЖЕ VAL
    return {i: (float(v), 0.0) for i, v in {**core, **tail}.items()}


def _short_reload_trades(now: float) -> list[TradePrint]:
    """Бёрст КРУПНЫХ покупателей у зоны (deep trades), поглощённых — цена не
    выросла (price_move ≤ 0). Внутри окна тела M5-свечи (300с)."""
    return [TradePrint(now - 100, 120.0, 8.0, "Buy"),
            TradePrint(now - 60, 120.0, 8.0, "Buy"),
            TradePrint(now - 10, 119.6, 2.0, "Sell")]


def test_evaluate_short_continuation_full_checklist():
    prof = build_profile(_short_reload_profile(), bucket_size=1.0)
    assert prof.poc_price == 120.5
    now = 1000.0
    snap = _evictless_state_snapshot(prof.buckets, 1.0, _short_reload_trades(now),
                                     last_price=119.5, ts=now)
    # контекст trend_down — по ФОРМЕ профиля (хвост ниже VAL), не по потоку.
    ctx = classify(prof, snap.last_price, accept_frac=0.70)
    assert ctx.state == TREND_DOWN

    from flowzone_bot.analysis.strategy import evaluate
    sig = evaluate(snap, prof, ctx, cfg=_Cfg())
    assert sig is not None
    assert sig.side == "short"
    assert sig.sl_level > sig.entry_ref > sig.tp_level  # геометрия шорта
    assert sig.score >= 3                               # super strong (§3.4)


def test_evaluate_none_when_balance_context():
    # симметричный профиль → BALANCE → нет входа.
    prof = build_profile(_triangular_buckets(), bucket_size=1.0)
    snap = _evictless_state_snapshot(prof.buckets, 1.0, [], 10.5, 1000.0)
    ctx = classify(prof, snap.last_price, accept_frac=0.70)
    assert ctx.state == BALANCE
    from flowzone_bot.analysis.strategy import evaluate
    assert evaluate(snap, prof, ctx, cfg=_Cfg()) is None


# ─── Фаза 5: swing-точки (фракталы) + цели/частичная фиксация ────────────

def test_find_swings_williams_fractal():
    # пик на idx3 (=10), впадина на idx7 (=1); left=right=2.
    highs = [3, 4, 6, 10, 6, 5, 4, 3, 4, 5]
    lows = [2, 3, 5, 8, 4, 3, 2, 1, 3, 4]
    sw = find_swings(highs, lows, left=2, right=2)
    highs_sw = [s for s in sw if s.kind == "high"]
    lows_sw = [s for s in sw if s.kind == "low"]
    assert any(s.idx == 3 and s.price == 10 for s in highs_sw)
    assert any(s.idx == 7 and s.price == 1 for s in lows_sw)


def test_find_swings_edges_not_classified():
    # экстремум на краю (idx0) без left-окна → не фрактал.
    highs = [10, 1, 2, 3, 4]
    lows = [9, 0, 1, 2, 3]
    sw = find_swings(highs, lows, left=2, right=2)
    assert all(s.idx != 0 for s in sw)


def test_nearest_and_list_swing_targets():
    swings = [
        # swing lows ниже 100: 98, 95 (для шорта)
        type("S", (), {"kind": "low", "price": 98.0})(),
        type("S", (), {"kind": "low", "price": 95.0})(),
        type("S", (), {"kind": "high", "price": 105.0})(),  # выше (для лонга)
        type("S", (), {"kind": "high", "price": 110.0})(),
    ]
    # шорт: ближайший low ниже входа = 98, список по близости [98, 95]
    assert nearest_swing_target(swings, "short", 100.0) == 98.0
    assert swing_targets(swings, "short", 100.0) == [98.0, 95.0]
    # лонг: ближайший high выше входа = 105, список [105, 110]
    assert nearest_swing_target(swings, "long", 100.0) == 105.0
    assert swing_targets(swings, "long", 100.0) == [105.0, 110.0]


def test_evaluate_uses_swing_target_over_structural():
    prof = build_profile(_short_reload_profile(), bucket_size=1.0)
    now = 1000.0
    snap = _evictless_state_snapshot(prof.buckets, 1.0, _short_reload_trades(now),
                                     last_price=119.5, ts=now)
    ctx = classify(prof, snap.last_price, accept_frac=0.70)
    # swing lows ниже входа: 110.0 (ближняя) и 106.0 (дальняя)
    swings = [type("S", (), {"kind": "low", "price": 110.0})(),
              type("S", (), {"kind": "low", "price": 106.0})()]
    from flowzone_bot.analysis.strategy import evaluate
    sig = evaluate(snap, prof, ctx, cfg=_Cfg(), swings=swings)
    assert sig is not None
    assert sig.tp_level == 110.0     # ближайший swing, не VP-структура
    assert sig.tp2_level == 106.0    # цель 2 для частичной фиксации
    assert "tp=swing" in sig.reasons


def test_partial_exchange_tp_decision():
    from flowzone_bot.trading.executor import partial_exchange_tp
    # частичная фиксация вкл (fraction>0) + есть цель 2 → биржевой TP = цель 2
    assert partial_exchange_tp(98.5, 96.0, 0.5) == (96.0, True)
    # нет цели 2 → биржевой TP = цель 1, частичной фиксации нет
    assert partial_exchange_tp(98.5, None, 0.5) == (98.5, False)
    # частичная фиксация выкл (fraction=0) → биржевой TP = цель 1
    assert partial_exchange_tp(98.5, 96.0, 0.0) == (98.5, False)


# ─── Фаза 6: session gate (London/NY, UTC-окна) ──────────────────────────

def test_parse_windows():
    from flowzone_bot.analysis.session import parse_windows
    assert parse_windows("07:00-16:00,12:00-21:00") == [(7.0, 16.0), (12.0, 21.0)]
    assert parse_windows("07:30-08:00") == [(7.5, 8.0)]
    assert parse_windows("") == []
    assert parse_windows("garbage,9-10") == [(9.0, 10.0)]


def test_in_session_london_ny_windows():
    import calendar

    from flowzone_bot.analysis.session import in_session

    def ts_at(hour: float) -> float:
        # 2026-06-16 — произвольный день; считаем по UTC-часу
        h = int(hour)
        m = int(round((hour - h) * 60))
        return calendar.timegm((2026, 6, 16, h, m, 0, 0, 0, 0))

    wins = [(7.0, 16.0), (12.0, 21.0)]  # London + NY
    assert in_session(ts_at(9.0), wins)    # London
    assert in_session(ts_at(14.0), wins)   # перекрытие
    assert in_session(ts_at(20.0), wins)   # NY
    assert not in_session(ts_at(3.0), wins)   # азиатская сессия — вне
    assert not in_session(ts_at(22.0), wins)  # после NY — вне
    assert in_session(ts_at(3.0), [])      # пустые окна → круглосуточно


def test_can_open_rate_limit_disabled_when_zero():
    from flowzone_bot.safety import killswitch

    class _DB:
        def __init__(self, ntrades):
            self._n = ntrades
        def realized_pnl_since(self, ts):
            return 0.0
        def total_realized_pnl(self):
            return 0.0
        def open_count(self):
            return 0
        def trades_since(self, ts):
            return self._n

    class _Set:
        max_daily_loss_usd = 0.0
        max_total_loss_usd = 0.0
        max_open_positions = 2
        max_trades_per_hour = 5

    db = _DB(ntrades=10)  # уже 10 сделок за час
    # лимит 5/ч → блок
    assert not killswitch.can_open(db, _Set(), now=1000.0).allowed
    # лимит 0 = выключен → НЕ блокируем (canon reload)
    s = _Set()
    s.max_trades_per_hour = 0
    assert killswitch.can_open(db, s, now=1000.0).allowed


def test_in_session_overnight_window():
    import calendar

    from flowzone_bot.analysis.session import in_session

    def ts_at(hour: int) -> float:
        return calendar.timegm((2026, 6, 16, hour, 0, 0, 0, 0, 0))

    wins = [(22.0, 2.0)]  # окно через полночь
    assert in_session(ts_at(23), wins)
    assert in_session(ts_at(1), wins)
    assert not in_session(ts_at(12), wins)


# ─── сведение P&L на партиалах (DB == Bybit closedPnl) ───────────────────

def _client_no_http():
    """FlowzoneBybitClient без HTTP-сессии (для юнит-тестов REST-разбора)."""
    from flowzone_bot.trading.client import FlowzoneBybitClient
    cl = object.__new__(FlowzoneBybitClient)
    cl._category = "linear"
    cl._instr = {}
    return cl


class _FakeSession:
    def __init__(self, records):
        self._records = records
    def get_closed_pnl(self, **kwargs):
        return {"result": {"list": list(self._records), "nextPageCursor": ""}}


def test_closed_pnl_position_sums_partials():
    # позиция qty=10: партиал 5 (+3.0) на цели 1 + остаток 5 (+5.0) на цели 2.
    # Точечный матч по closedSize≈10 не нашёл бы ни одной записи → нужна сумма.
    cl = _client_no_http()
    cl._session = _FakeSession([
        {"closedPnl": "3.0", "closedSize": "5", "avgEntryPrice": "100.0",
         "avgExitPrice": "98.0", "createdTime": "1000"},
        {"closedPnl": "5.0", "closedSize": "5", "avgEntryPrice": "100.0",
         "avgExitPrice": "96.0", "createdTime": "2000"},
    ])
    d = cl.closed_pnl_position("ZECUSDT", qty=10.0, entry_price=100.0,
                              since_ms=0, until_ms=10_000)
    assert d is not None
    assert d["pnl"] == 8.0          # Σ closedPnl (уже net)
    assert d["count"] == 2
    assert d["exit"] == 97.0        # qty-взвешенный выход


def test_closed_pnl_position_incomplete_returns_none():
    # собрана лишь половина позиции (Σ closedSize=5 != qty=10) → None (не врём).
    cl = _client_no_http()
    cl._session = _FakeSession([
        {"closedPnl": "3.0", "closedSize": "5", "avgEntryPrice": "100.0",
         "avgExitPrice": "98.0", "createdTime": "1000"},
    ])
    d = cl.closed_pnl_position("ZECUSDT", qty=10.0, entry_price=100.0,
                              since_ms=0, until_ms=10_000)
    assert d is None


def test_closed_pnl_position_filters_by_entry_price():
    # запись чужой позиции (другой avgEntryPrice) в окне не должна попасть в сумму.
    cl = _client_no_http()
    cl._session = _FakeSession([
        {"closedPnl": "3.0", "closedSize": "5", "avgEntryPrice": "100.0",
         "avgExitPrice": "98.0", "createdTime": "1000"},
        {"closedPnl": "5.0", "closedSize": "5", "avgEntryPrice": "100.0",
         "avgExitPrice": "96.0", "createdTime": "2000"},
        {"closedPnl": "99.0", "closedSize": "5", "avgEntryPrice": "200.0",
         "avgExitPrice": "190.0", "createdTime": "1500"},
    ])
    d = cl.closed_pnl_position("ZECUSDT", qty=10.0, entry_price=100.0,
                              since_ms=0, until_ms=10_000)
    assert d is not None
    assert d["pnl"] == 8.0          # запись с entry=200 отфильтрована


def test_realized_or_estimate_partial_uses_remaining_qty():
    from flowzone_bot.trading.executor import Executor, taker_pnl
    ex = Executor(db=None, settings=_Cfg(), client=None, now=lambda: 1000.0)
    tr = type("T", (), {"id": 7, "symbol": "ZECUSDT", "side": "long",
                        "entry": 100.0, "qty": 10.0})()
    # партиал 5 уже реально зафиксирован на цели 1 (+10.0 net), остаток 5 едет
    # на цель 2 (104, более выгодную). close_val — по цене партиала (102).
    ex._fills[7] = {"fee": 0.0, "pnl": 10.0, "close_val": 5 * 102.0,
                    "close_qty": 5.0, "open_val": 0.0, "open_qty": 0.0}
    pnl, exitp, is_real = ex._realized_or_estimate(tr, 104.0)
    assert not is_real
    # оценка = реальный партиал(10.0) + taker на ОСТАТОК 5 (не на полные 10)
    expected = 10.0 + taker_pnl("long", 100.0, 104.0, 5.0)
    assert abs(pnl - expected) < 1e-9
    # старое (ошибочное) поведение завышало профит: taker на полные 10 по 104
    assert pnl < taker_pnl("long", 100.0, 104.0, 10.0)


def test_rest_finalize_partial_falls_back_to_position_sum():
    from flowzone_bot.trading.executor import Executor

    class _FakeClient:
        def closed_pnl_detail(self, *a, **k):
            return None  # точечный матч по qty не нашёл (партиал)
        def closed_pnl_position(self, symbol, *, qty, entry_price,
                                since_ms, until_ms):
            return {"pnl": 8.0, "exit": 97.0, "count": 2}

    class _FakeDB:
        def __init__(self):
            self.verified = None
        def verify_pnl(self, tid, *, pnl_usd, exit_price, close_reason):
            self.verified = (tid, pnl_usd, exit_price, close_reason)

    db = _FakeDB()
    ex = Executor(db=db, settings=_Cfg(), client=_FakeClient(), now=lambda: 5000.0)
    tr = type("T", (), {"id": 7, "symbol": "ZECUSDT", "qty": 10.0,
                        "entry": 100.0, "ts_open": 1000.0, "ts_close": 2000.0,
                        "close_reason": "tp_sl", "pnl_usd": 99.0})()
    ok = ex._rest_finalize(tr, 2000.0)
    assert ok
    assert db.verified[0] == 7
    assert db.verified[1] == 8.0    # суммарный net, не завышенная оценка
    # REST авторитетен → сразу verified (не нужен второй запрос на true-up)


def test_closed_pnl_position_skips_funding_settle_records():
    # запись funding (execType=Settle) не должна попадать в матч по объёму.
    cl = _client_no_http()
    cl._session = _FakeSession([
        {"closedPnl": "3.0", "closedSize": "5", "avgEntryPrice": "100.0",
         "avgExitPrice": "98.0", "createdTime": "1000", "execType": "Trade"},
        {"closedPnl": "5.0", "closedSize": "5", "avgEntryPrice": "100.0",
         "avgExitPrice": "96.0", "createdTime": "2000", "execType": "Trade"},
        {"closedPnl": "0.7", "closedSize": "10", "avgEntryPrice": "100.0",
         "avgExitPrice": "0", "createdTime": "1500", "execType": "Settle"},
    ])
    d = cl.closed_pnl_position("ZECUSDT", qty=10.0, entry_price=100.0,
                              since_ms=0, until_ms=10_000)
    assert d is not None
    assert d["pnl"] == 8.0          # funding-запись исключена из суммы и объёма


# ─── канон: REST closedPnl = источник правды для ВСЕХ закрытых live ───────

def _fz_db(tmp_path):
    from flowzone_bot.state.db import FlowzoneDB
    return FlowzoneDB(str(tmp_path))


def test_verify_pnl_marks_verified_and_clears_provisional(tmp_path):
    db = _fz_db(tmp_path)
    tid = db.insert_open(symbol="XLMUSDT", side="long", qty=100.0, entry=0.22,
                         sl=0.21, tp=0.24, score=3, reasons="x", mode="live")
    db.mark_closed(tid, exit_price=0.225, pnl_usd=-10.60, fees_usd=0.0,
                   close_reason="tp_hit", provisional=False)
    rows = db.unverified_closed_live_since(0.0)
    assert [r.id for r in rows] == [tid]      # ещё не сверена
    db.verify_pnl(tid, pnl_usd=-11.10, exit_price=0.2249, close_reason="sl_hit")
    assert db.unverified_closed_live_since(0.0) == []   # сверена — ушла из выборки
    assert db.total_realized_pnl() == -11.10
    db.close()


def test_unverified_selector_excludes_paper_tech_and_verified(tmp_path):
    db = _fz_db(tmp_path)
    # live торговое закрытие — должно попасть
    t1 = db.insert_open(symbol="A", side="long", qty=1, entry=1, sl=1, tp=2,
                        score=1, reasons="", mode="live")
    db.mark_closed(t1, exit_price=1.1, pnl_usd=0.5, fees_usd=0,
                   close_reason="tp_hit")
    # технические закрытия (нет closedPnl) — НЕ должны попасть
    t2 = db.insert_open(symbol="B", side="long", qty=1, entry=1, sl=1, tp=2,
                        score=1, reasons="", mode="live")
    db.mark_closed(t2, exit_price=1.0, pnl_usd=0.0, fees_usd=0,
                   close_reason="entry_timeout")
    # paper — не live, не должно попасть
    t3 = db.insert_open(symbol="C", side="long", qty=1, entry=1, sl=1, tp=2,
                        score=1, reasons="", mode="paper")
    db.mark_closed(t3, exit_price=1.1, pnl_usd=0.5, fees_usd=0,
                   close_reason="tp")
    ids = {r.id for r in db.unverified_closed_live_since(0.0)}
    assert ids == {t1}
    db.close()


def test_reconcile_trues_up_ws_drift_against_closedpnl(tmp_path):
    """Сделка закрыта через WS (non-provisional) с заниженной комиссией —
    универсальный true-up чинит её до биржевого closedPnl и метит verified."""
    from flowzone_bot.trading.executor import Executor

    class _FakeClient:
        def closed_pnl_detail(self, *a, **k):
            return {"pnl": -11.10, "exit": 0.2249, "order_id": "x",
                    "created": 0.0}
        def closed_pnl_position(self, *a, **k):
            return None

    db = _fz_db(tmp_path)
    tid = db.insert_open(symbol="XLMUSDT", side="long", qty=100.0, entry=0.22,
                         sl=0.21, tp=0.24, score=3, reasons="x", mode="live",
                         ts_open=9000.0)
    # WS закрыл с дрейфом: −10.60 вместо реальных −11.10
    db.mark_closed(tid, exit_price=0.225, pnl_usd=-10.60, fees_usd=0.0,
                   close_reason="tp_hit", ts_close=9000.0, provisional=False)
    ex = Executor(db=db, settings=_Cfg(), client=_FakeClient(), now=lambda: 10000.0)
    ex.reconcile()
    assert db.total_realized_pnl() == -11.10           # дрейф исправлен
    assert db.unverified_closed_live_since(0.0) == []  # помечена verified
    db.close()


def test_rest_verify_gives_up_after_max_fails(tmp_path):
    """Неоднозначную сделку (REST не матчится) после N попыток принимаем как
    есть (WS-net) и метим verified — не зацикливаем бюджет."""
    from flowzone_bot.trading.executor import Executor

    class _FakeClient:
        def closed_pnl_detail(self, *a, **k):
            return None
        def closed_pnl_position(self, *a, **k):
            return None

    db = _fz_db(tmp_path)
    tid = db.insert_open(symbol="JTOUSDT", side="short", qty=500.0, entry=0.77,
                         sl=0.78, tp=0.76, score=3, reasons="x", mode="live",
                         ts_open=9000.0)
    db.mark_closed(tid, exit_price=0.769, pnl_usd=0.5, fees_usd=0.0,
                   close_reason="tp_hit", ts_close=9000.0, provisional=False)
    ex = Executor(db=db, settings=_Cfg(), client=_FakeClient(), now=lambda: 10000.0)
    tr = db.unverified_closed_live_since(0.0)[0]
    assert ex._rest_verify(tr, 9000.0) is False    # попытка 1
    assert ex._rest_verify(tr, 9000.0) is False    # попытка 2
    assert db.unverified_closed_live_since(0.0)    # ещё не сдались
    assert ex._rest_verify(tr, 9000.0) is False    # попытка 3 → сдаёмся
    assert db.unverified_closed_live_since(0.0) == []   # verified (WS-net 0.5)
    assert db.total_realized_pnl() == 0.5
    db.close()


# ─── momentum-селектор вселенной (метод «как в ролике», momentum_universe.py) ─

def _fz_mticker(sym, last, pcnt, turnover, bid=None, ask=None, pre=""):
    return {"symbol": sym, "lastPrice": str(last), "price24hPcnt": str(pcnt),
            "turnover24h": str(turnover),
            "bid1Price": "" if bid is None else str(bid),
            "ask1Price": "" if ask is None else str(ask),
            "curPreListingPhase": pre}


def test_flowzone_momentum_ranks_by_abs_change_and_filters_turnover():
    from flowzone_bot.data.momentum_universe import select_momentum_universe
    tickers = [
        _fz_mticker("BANANAUSDT", 1.0, 0.44, 85e6),
        _fz_mticker("DUMPUSDT", 1.0, -0.60, 120e6),
        _fz_mticker("MIDUSDT", 1.0, 0.20, 90e6),
        _fz_mticker("DUSTUSDT", 1.0, 1.20, 1e6),    # оборот < floor
        _fz_mticker("ETHUSDC", 3000, 0.30, 1e9),    # не USDT-перп
    ]
    picked = select_momentum_universe(
        tickers, top_n=5, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=0.0)
    assert picked == ["DUMPUSDT", "BANANAUSDT", "MIDUSDT"]


def test_flowzone_momentum_direction_and_no_anti_pump_cap():
    from flowzone_bot.data.momentum_universe import select_momentum_universe
    tickers = [
        _fz_mticker("UPUSDT", 1.0, 0.90, 100e6),    # +90% НЕ режется (нет range-cap)
        _fz_mticker("DOWNUSDT", 1.0, -0.50, 100e6),
    ]
    assert select_momentum_universe(
        tickers, top_n=5, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=0.0, direction="up") == ["UPUSDT"]
    assert select_momentum_universe(
        tickers, top_n=5, min_turnover=50e6, min_abs_change_pct=0.0,
        max_spread_bps=0.0, direction="down") == ["DOWNUSDT"]


def test_flowzone_universe_method_default_is_rvol():
    from flowzone_bot.config.settings import FlowzoneSettings
    cfg = FlowzoneSettings()
    assert cfg.universe_method == "rvol"
    assert cfg.momentum_min_turnover_usd == 50_000_000.0
    assert cfg.momentum_direction == "both"


def test_flowzone_canon_defaults():
    """Дефолты приведены строго к канону ролика (2026-06-22, v0.2.0)."""
    from flowzone_bot.config.settings import FlowzoneSettings
    cfg = FlowzoneSettings()
    # §6.1/§6.3: ликвидность — авто-ротация альтов выкл, торгуем глубокие перпы.
    assert cfg.auto_universe_enabled is False
    assert cfg.symbol_list == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    # §3.4 «super strong area» = конфлюэнс 3 факторов.
    assert cfg.zone_min_confluence == 3
    # §4 + §6.3: absorption на теле M5-свечи (300с).
    assert cfg.absorption_window_sec == 300.0
    # §2: acceptance вне VA = каноничная Value-Area-доля 0.70.
    assert cfg.context_accept_frac == 0.70
