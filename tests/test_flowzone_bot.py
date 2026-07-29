"""Тесты flowzone_bot (фазы 1-2): агрегаты, Volume Profile, контекст аукциона.

Позитивные сценарии — на ЧЕСТНОЙ синтетике с известным распределением объёма
(no-data-fitting.mdc: данные не рисуются «под результат», проверяется логика
формул VP/контекста).
"""
from __future__ import annotations

from flowzone_bot.analysis.auction import AuctionTracker
from flowzone_bot.analysis.context import (BALANCE, BALANCE_SHAPE,
                                          DOUBLE_DISTRIBUTION, NORMAL, P_SHAPE_DOWN,
                                          P_SHAPE_UP, TREND_DOWN, TREND_UP, classify,
                                          classify_shape)
from flowzone_bot.analysis.orderflow import (big_trade_threshold,
                                             detect_absorption, detect_big_trades,
                                             detect_exhaustion, detect_initiative,
                                             size_percentile, zone_delta)
from flowzone_bot.analysis.swings import (Swing, find_swings,
                                          nearest_swing_target, swing_targets)
from flowzone_bot.analysis.volume_profile import (build_profile, find_hvn_lvn,
                                                  find_ledges, merge_profiles,
                                                  value_areas_overlap)
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


def test_symbolstate_incremental_vp_session_anchored():
    """Per-SESSION профиль (A2, канон §2): якорь — старт London/NY окна.
    Вне сессии профиль не строится; смена session-окна сбрасывает профиль."""
    # day 100, 10:00 UTC — внутри London окна 07:00-16:00
    wall = {"t": 86400.0 * 100 + 10 * 3600 + 10}
    wins = [(7.0, 16.0), (12.0, 21.0)]
    st = SymbolState("BTCUSDT", trade_window_sec=999.0, vp_bucket_size=1.0,
                     session_windows=wins, wall_now=lambda: wall["t"])
    st.on_trade(100.4, 2.0, "Buy")   # idx 100
    st.on_trade(100.9, 1.0, "Sell")  # idx 100
    st.on_trade(102.1, 4.0, "Buy")   # idx 102
    snap = st.snapshot()
    assert snap.vp_bucket_size == 1.0
    assert snap.vp_buckets[100] == (2.0, 1.0)
    assert snap.vp_buckets[102] == (4.0, 0.0)
    assert snap.vp_session_start is not None
    # вне сессии (04:00 UTC day 101) → профиль сбрасывается, якорь None
    wall["t"] = 86400.0 * 101 + 4 * 3600
    st.on_trade(103.0, 1.0, "Buy")
    snap2 = st.snapshot()
    assert snap2.vp_buckets == {}
    assert snap2.vp_session_start is None
    # возврат в сессию (08:00 UTC day 101) → новый якорь, профиль с нуля
    wall["t"] = 86400.0 * 101 + 8 * 3600
    st.on_trade(105.0, 1.0, "Buy")
    snap3 = st.snapshot()
    assert set(snap3.vp_buckets) == {105}
    assert snap3.vp_session_start is not None


# ─── Фаза 2: Volume Profile engine ───────────────────────────────────────

def _triangular_buckets() -> dict[int, tuple[float, float]]:
    """Симметричный треугольный профиль, пик на idx 10. (buy=vol, sell=0)."""
    vols = {10: 100, 9: 80, 11: 80, 8: 60, 12: 60, 7: 40, 13: 40,
            6: 20, 14: 20, 5: 10, 15: 10}
    return {i: (float(v), 0.0) for i, v in vols.items()}


def test_build_profile_poc_and_value_area():
    prof = build_profile(_triangular_buckets(), bucket_size=1.0,
                         value_area_pct=0.68)
    assert prof is not None
    assert prof.poc_idx == 10
    assert prof.poc_price == 10.5
    assert prof.total_volume == 520.0
    # двухрядное расширение от POC до ≥68% (канон-автор): VA idx 8..12 (vol=380)
    assert prof.va_lo_idx == 8
    assert prof.va_hi_idx == 12
    assert prof.val == 8.0
    assert prof.vah == 13.0
    assert prof.value_area_volume >= 0.68 * prof.total_volume


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
    """Профиль, элонгированный ВНИЗ: тяжёлое ядро у idx20 + хвост ниже VAL.

    Хвост SELL-доминантный: канон-день с acceptance ниже VAL — это P-shape down,
    где направление принято агрессивными ПРОДАВЦАМИ (26:49 *«P-shapes where the
    buyers are really aggressive»*, зеркально вниз). Раньше весь профиль был
    buy-only ради краткости — такой «нисходящий» день с покупательской дельтой
    в хвосте канон трактует как indecision (34:32), и гейт формы (C3) его
    справедливо отвергает.
    """
    core = {20: 100, 21: 70, 19: 70, 22: 30, 18: 30}           # ≈80% → value area
    tail = {10: 15, 11: 15, 12: 15, 13: 15, 14: 15}            # принято НИЖЕ VAL
    buckets = {i: (float(v), 0.0) for i, v in core.items()}
    buckets.update({i: (0.0, float(v)) for i, v in tail.items()})
    return buckets


def _up_elongated_buckets() -> dict[int, tuple[float, float]]:
    """Профиль, элонгированный ВВЕРХ: ядро у idx20 + хвост выше VAH."""
    core = {20: 100, 21: 70, 19: 70, 22: 30, 18: 30}
    tail = {26: 15, 27: 15, 28: 15, 29: 15, 30: 15}            # принято ВЫШЕ VAH
    return {i: (float(v), 0.0) for i, v in {**core, **tail}.items()}


def test_classify_trend_up_on_acceptance_above_vah():
    prof = build_profile(_up_elongated_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=20.5, accept_frac=0.68)
    assert ctx.state == TREND_UP
    assert ctx.trade_side == "long"
    assert ctx.accept_above >= 0.68


def test_classify_trend_down_on_acceptance_below_val():
    prof = build_profile(_down_elongated_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=20.5, accept_frac=0.68)
    assert ctx.state == TREND_DOWN
    assert ctx.trade_side == "short"
    assert ctx.accept_below >= 0.68


def test_classify_balance_symmetric_profile():
    # симметричный треугольник: хвосты вне VA равны → нет направленного принятия.
    prof = build_profile(_triangular_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=10.5, accept_frac=0.68)
    assert ctx.state == BALANCE
    assert ctx.trade_side is None


def test_classify_balance_when_acceptance_below_threshold():
    # хвосты есть с обеих сторон, но перекос < 0.68 (60/40) → не acceptance.
    core = {20: 100, 21: 70, 19: 70, 22: 30, 18: 30}
    buckets = {i: (float(v), 0.0) for i, v in core.items()}
    buckets.update({12: (20.0, 0.0), 13: (20.0, 0.0), 14: (20.0, 0.0)})  # ↓ 60
    buckets.update({26: (20.0, 0.0), 27: (20.0, 0.0)})                   # ↑ 40
    prof = build_profile(buckets, bucket_size=1.0)
    ctx = classify(prof, last_price=20.5, accept_frac=0.68)
    assert ctx.state == BALANCE
    assert 0.55 <= ctx.accept_below <= 0.65


def test_classify_balance_when_outside_volume_immaterial():
    """Фикс 2026-07-02: пара случайных принтов за VA (≈0.4% объёма) при колоколе
    — НЕ acceptance. Доминирующий хвост обязан держать ≥ (1−VA%)/2 общего объёма
    (нейтральная одно-сторонняя вне-VA масса, при VA 68% → 16%). Раньше
    accept_below=1.0 давал ложный trend_down по шуму."""
    buckets = {10: (100.0, 0.0), 9: (90.0, 0.0), 11: (90.0, 0.0),
               3: (1.0, 0.0)}   # хвост = 1/281 ≈ 0.4% объёма
    prof = build_profile(buckets, bucket_size=1.0)
    ctx = classify(prof, last_price=10.5, accept_frac=0.68)
    assert ctx.accept_below == 1.0      # весь вне-VA объём на одной стороне…
    assert ctx.state == BALANCE         # …но он нематериален → не тренд


# ─── Sticky-направление аукциона (AuctionTracker, §2 «второе движение») ───

# мгновенные контексты (state зависит только от формы профиля, не от цены):
_DOWN_INST = classify(build_profile(_down_elongated_buckets(), bucket_size=1.0),
                      last_price=100.0)
_UP_INST = classify(build_profile(_up_elongated_buckets(), bucket_size=1.0),
                    last_price=100.0)
_BAL_INST = classify(build_profile(_triangular_buckets(), bucket_size=1.0),
                     last_price=100.0)
# «предыдущие уровни» (swing-экстремумы): low=95, high=105.
_SW = [Swing(0, 95.0, "low"), Swing(1, 105.0, "high")]


def test_auction_establish_requires_structural_breakout():
    tr = AuctionTracker()
    # acceptance вниз есть, но цена 96 НЕ пробила swing low 95 → не торгуем
    assert tr.update("X", _DOWN_INST, 96.0, _SW, now=0.0).state == BALANCE
    # цена 94 < 95 (пробой предыдущего уровня) + acceptance → устанавливаем down
    assert tr.update("X", _DOWN_INST, 94.0, _SW, now=0.0).state == TREND_DOWN
    assert tr.peek("X") == TREND_DOWN


def test_auction_no_breakout_without_swings():
    tr = AuctionTracker()
    # нет swing-структуры → пробой не подтвердить → не устанавливаем
    assert tr.update("Y", _DOWN_INST, 50.0, [], now=0.0).state == BALANCE


def test_auction_sticky_through_pullback_and_balance():
    tr = AuctionTracker()
    tr.update("X", _DOWN_INST, 94.0, _SW, now=0.0)  # down латч
    # встречный мгновенный trend_up, но цена 100 < swing high 105 (нет пробоя) → держим down
    assert tr.update("X", _UP_INST, 100.0, _SW, now=0.0).state == TREND_DOWN
    # баланс на откате тоже НЕ сбрасывает направление
    assert tr.update("X", _BAL_INST, 100.0, _SW, now=0.0).state == TREND_DOWN


def test_auction_flip_only_on_opposite_breakout():
    tr = AuctionTracker()
    tr.update("X", _DOWN_INST, 94.0, _SW, now=0.0)  # down латч
    # trend_up + цена 106 > swing high 105 → встречный структурный пробой → flip
    assert tr.update("X", _UP_INST, 106.0, _SW, now=0.0).state == TREND_UP
    assert tr.peek("X") == TREND_UP


def test_auction_resets_on_new_utc_day():
    tr = AuctionTracker()
    tr.update("X", _DOWN_INST, 94.0, _SW, now=0.0)  # day 0: down
    assert tr.peek("X") == TREND_DOWN
    # новый UTC-день (профиль сброшен) + без подтверждённого пробоя → латч сброшен
    ctx = tr.update("X", _DOWN_INST, 96.0, _SW, now=86400.0)
    assert ctx.state == BALANCE
    assert tr.peek("X") is None


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
    sl_zone_mult = 1.0               # канон «1-2-3» (far_edge + 1× ширина зоны)
    min_rr = 2.0                     # канон Fabervaale R:R ≥ 1:2 (флор «1 to 2»)
    be_lock_enabled = True           # канон Trade Management (видео 39:00)
    be_lock_break_structure = True   # канон «break this level» (swing-пробой)
    be_lock_cvd_gate = True          # tradezella «CVD strong pressure»
    trail_enabled = True             # канон «bring your stop loss here» (стадия 2)
    trail_window_sec = 300.0         # окно absorption-принтов trail = тело M5


def _evictless_state_snapshot(buckets, bucket_size, trades, last_price, ts):
    from flowzone_bot.data.aggregates import SymbolSnapshot
    return SymbolSnapshot(
        symbol="BTCUSDT", ts=ts, last_price=last_price, best_bid=None,
        best_ask=None, ob_imbalance=None, trades=trades, stale=False,
        vp_bucket_size=bucket_size, vp_buckets=buckets)


def _short_reload_profile() -> dict[int, tuple[float, float]]:
    """Профиль элонгирован ВНИЗ (trend_down) + тяжёлое ядро idx118-122 как зона
    reload-резистанса ВЫШЕ цены для шорта.

    Ядро buy-only (покупатели, которых будут поглощать в зоне), хвост ниже VAL
    sell-доминантный — канон-P-shape вниз (см. `_down_elongated_buckets`).
    """
    core = {120: 100, 121: 70, 119: 70, 122: 30, 118: 30}   # VA≈119-122, POC=120
    tail = {100: 15, 102: 15, 104: 15, 106: 15, 108: 15}    # принято НИЖЕ VAL
    buckets = {i: (float(v), 0.0) for i, v in core.items()}
    buckets.update({i: (0.0, float(v)) for i, v in tail.items()})
    return buckets


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
    ctx = classify(prof, snap.last_price, accept_frac=0.68)
    assert ctx.state == TREND_DOWN
    # канон §5.3: цель = ближайший swing (без swing-цели сделки нет).
    # swing low = 95.0 далеко от entry 119.5, чтобы R:R ≥ 2.0 (канон Fabervaale,
    # sl≈127.5 → risk≈8, reward=24.5 → rr=3.06). Тест проверяет чеклист, не R:R.
    swings = [type("S", (), {"kind": "low", "price": 95.0})()]

    from flowzone_bot.analysis.strategy import evaluate
    sig = evaluate(snap, ctx, prof, cfg=_Cfg(), swings=swings)
    assert sig is not None
    assert sig.side == "short"
    assert sig.sl_level > sig.entry_ref > sig.tp_level  # геометрия шорта
    assert sig.tp_level == 95.0                           # ближайший swing
    assert sig.score >= 3                                 # super strong (§3.4)
    # канон §5.2 «1-2-3»: стоп = far_edge зоны + 1× ширина зоны (+буфер).
    zone_width = sig.zone_high - sig.zone_low
    assert sig.sl_level >= sig.zone_high + zone_width * _Cfg.sl_zone_mult


def test_evaluate_rr_filter_rejects_close_swing():
    """Канон Fabervaale R:R ≥ 1:2 (ролик cUTsoU-15Tc «1 to 2», chartfanatics).
    Если swing-цель ближе к entry чем risk × min_rr — сделка не берётся (TP не
    окупает риск/fees, кейс #468 live: tp_hit с убытком)."""
    prof = build_profile(_short_reload_profile(), bucket_size=1.0)
    now = 1000.0
    snap = _evictless_state_snapshot(prof.buckets, 1.0, _short_reload_trades(now),
                                     last_price=119.5, ts=now)
    ctx = classify(prof, snap.last_price, accept_frac=0.68)
    from flowzone_bot.analysis.strategy import evaluate
    # swing low = 117 (близко: reward 2.5, risk ~8 → rr 0.31 < 2.0) → None
    near = [type("S", (), {"kind": "low", "price": 117.0})()]
    assert evaluate(snap, ctx, prof, cfg=_Cfg(), swings=near) is None
    # swing low = 95 (далеко: rr ~3.06 ≥ 2.0) → сигнал
    far = [type("S", (), {"kind": "low", "price": 95.0})()]
    sig = evaluate(snap, ctx, prof, cfg=_Cfg(), swings=far)
    assert sig is not None
    assert any(r.startswith("rr=") for r in sig.reasons)


def test_evaluate_none_when_balance_context():
    # симметричный профиль → BALANCE → нет входа.
    prof = build_profile(_triangular_buckets(), bucket_size=1.0)
    snap = _evictless_state_snapshot(prof.buckets, 1.0, [], 10.5, 1000.0)
    ctx = classify(prof, snap.last_price, accept_frac=0.68)
    assert ctx.state == BALANCE
    from flowzone_bot.analysis.strategy import evaluate
    assert evaluate(snap, ctx, prof, cfg=_Cfg()) is None


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


def test_evaluate_uses_swing_target_only():
    """Канон §5.3: цель = только swing point. Структурного фолбэка на POC/VAL
    больше нет (A5 — не в ролике). Без swing-цели сделки НЕ будет."""
    prof = build_profile(_short_reload_profile(), bucket_size=1.0)
    now = 1000.0
    snap = _evictless_state_snapshot(prof.buckets, 1.0, _short_reload_trades(now),
                                     last_price=119.5, ts=now)
    ctx = classify(prof, snap.last_price, accept_frac=0.68)
    from flowzone_bot.analysis.strategy import evaluate
    # без swings → нет swing-цели → сделка не берётся (канон: цель всегда swing)
    assert evaluate(snap, ctx, prof, cfg=_Cfg(), swings=None) is None
    # со swings → tp = ближайший swing, tp2 не существует (частичная фиксация
    # удалена, A6). swing low = 95.0 далеко, чтобы R:R ≥ 2.0 (канон Fabervaale).
    swings = [type("S", (), {"kind": "low", "price": 95.0})(),
              type("S", (), {"kind": "low", "price": 90.0})()]
    sig = evaluate(snap, ctx, prof, cfg=_Cfg(), swings=swings)
    assert sig is not None
    assert sig.tp_level == 95.0     # ближайший swing
    assert not hasattr(sig, "tp2_level")  # частичная фиксация удалена
    assert "tp=swing" in sig.reasons


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


# ─── A2: per-session якорь профиля (session_start_ts) ────────────────────

def test_session_start_ts_london_window():
    import calendar
    from flowzone_bot.analysis.session import session_start_ts

    wins = [(7.0, 16.0), (12.0, 21.0)]
    # 09:00 UTC — внутри London; старт = 07:00 того же дня
    ts = calendar.timegm((2026, 6, 16, 9, 0, 0, 0, 0, 0))
    expected_start = calendar.timegm((2026, 6, 16, 7, 0, 0, 0, 0, 0))
    assert session_start_ts(ts, wins) == expected_start


def test_session_start_ts_outside_session_returns_none():
    import calendar
    from flowzone_bot.analysis.session import session_start_ts
    wins = [(7.0, 16.0), (12.0, 21.0)]
    # 03:00 UTC — азиатская сессия, вне окон → None (профиль не строим)
    ts = calendar.timegm((2026, 6, 16, 3, 0, 0, 0, 0, 0))
    assert session_start_ts(ts, wins) is None


def test_session_start_ts_overnight_window():
    import calendar
    from flowzone_bot.analysis.session import session_start_ts
    wins = [(22.0, 2.0)]
    # 01:00 → старт был вчера 22:00
    ts = calendar.timegm((2026, 6, 16, 1, 0, 0, 0, 0, 0))
    expected = calendar.timegm((2026, 6, 15, 22, 0, 0, 0, 0, 0))
    assert session_start_ts(ts, wins) == expected
    # 23:00 → старт сегодня 22:00
    ts2 = calendar.timegm((2026, 6, 16, 23, 0, 0, 0, 0, 0))
    expected2 = calendar.timegm((2026, 6, 16, 22, 0, 0, 0, 0, 0))
    assert session_start_ts(ts2, wins) == expected2


def test_session_start_ts_overlap_anchors_to_block_start():
    """Фикс 2026-07-02: London 07-16 + NY 12-21 = непрерывный блок 07-21 →
    якорь 07:00 на весь блок. Раньше якорь брался от ПЕРВОГО совпавшего окна:
    в 16:00 прыгал 07:00 → 12:00, профиль обнулялся (терялся объём 12-16) и
    контекст ежедневно уходил в warming посреди NY-сессии."""
    import calendar
    from flowzone_bot.analysis.session import merged_segments, session_start_ts
    wins = [(7.0, 16.0), (12.0, 21.0)]
    assert merged_segments(wins) == [(7.0, 21.0)]
    expected = calendar.timegm((2026, 6, 16, 7, 0, 0, 0, 0, 0))
    for hh, mm in ((12, 1), (15, 59), (16, 1), (20, 59)):
        ts = calendar.timegm((2026, 6, 16, hh, mm, 0, 0, 0, 0))
        assert session_start_ts(ts, wins) == expected, (hh, mm)
    # вне блока — по-прежнему None
    ts_out = calendar.timegm((2026, 6, 16, 21, 30, 0, 0, 0, 0))
    assert session_start_ts(ts_out, wins) is None


# ─── A2: per-swing профиль из принтов (build_profile_from_prints) ────────

def test_build_profile_from_prints_aggregates_by_price():
    from flowzone_bot.analysis.volume_profile import build_profile_from_prints
    # принты на цене 120 (buy 8, sell 2) и 121 (buy 1, sell 5); bucket=1.
    prints = [(1000.0, 120.0, 8.0, "Buy"), (1001.0, 120.0, 2.0, "Sell"),
              (1002.0, 121.0, 1.0, "Buy"), (1003.0, 121.0, 5.0, "Sell")]
    prof = build_profile_from_prints(prints, bucket_size=1.0)
    assert prof is not None
    assert prof.bucket_delta(120) == 6.0    # 8 buy − 2 sell
    assert prof.bucket_delta(121) == -4.0   # 1 buy − 5 sell


def test_build_profile_from_prints_empty_returns_none():
    from flowzone_bot.analysis.volume_profile import build_profile_from_prints
    assert build_profile_from_prints([], bucket_size=1.0) is None
    assert build_profile_from_prints([(1, 100, 1, "Buy")], bucket_size=0.0) is None


# ─── A2: persist принтов в SQLite + PrintStore batched flush ──────────────

def test_db_insert_and_read_prints(tmp_path):
    from flowzone_bot.state.db import FlowzoneDB
    db = FlowzoneDB(str(tmp_path))
    rows = [(1000.0, "BTCUSDT", 100.0, 1.5, "Buy"),
            (1001.0, "BTCUSDT", 100.5, 2.0, "Sell"),
            (1002.0, "ETHUSDT", 3000.0, 1.0, "Buy")]
    assert db.insert_prints(rows) == 3
    got = db.prints_since("BTCUSDT", 1000.0)
    assert len(got) == 2
    assert got[0] == (1000.0, 100.0, 1.5, "Buy")
    # until-фильтр
    got_until = db.prints_since("BTCUSDT", 1000.0, until_ts=1001.0)
    assert len(got_until) == 1
    db.close()


def test_db_prune_prints(tmp_path):
    from flowzone_bot.state.db import FlowzoneDB
    db = FlowzoneDB(str(tmp_path))
    db.insert_prints([(500.0, "X", 1.0, 1.0, "Buy"),
                      (1500.0, "X", 2.0, 1.0, "Sell")])
    # удаляем всё старше 1000
    n = db.prune_prints_before(1000.0)
    assert n == 1
    assert db.prints_count() == 1
    db.close()


def test_print_store_flushes_buffer_to_db(tmp_path):
    from flowzone_bot.data.print_store import PrintStore
    from flowzone_bot.state.db import FlowzoneDB
    db = FlowzoneDB(str(tmp_path))
    store = PrintStore(db, flush_interval_sec=0.05, prune_older_than_sec=0.0)
    store.ingest(1000.0, "BTCUSDT", 100.0, 1.5, "Buy")
    store.ingest(1001.0, "BTCUSDT", 100.5, 2.0, "Sell")
    store.start()
    import time as _t
    _t.sleep(0.2)
    store.stop(timeout=2.0)
    got = db.prints_since("BTCUSDT", 999.0)
    assert len(got) == 2
    db.close()


def test_swing_profile_for_builds_from_db_prints(tmp_path):
    """Per-swing профиль (A2, канон §3): окно [ts prev swing, now] из БД.
    Принты старше swing-якоря НЕ попадают в профиль (окно от swing)."""
    from flowzone_bot.analysis.swings import Swing
    from flowzone_bot.app.main import _swing_profile_for
    from flowzone_bot.state.db import FlowzoneDB
    db = FlowzoneDB(str(tmp_path))
    # принты: до swing-якоря (ts=900) и после (ts>=1000)
    db.insert_prints([(900.0, "X", 100.0, 99.0, "Buy"),    # старее swing → выкинуть
                      (1000.0, "X", 120.0, 8.0, "Buy"),
                      (1001.0, "X", 120.0, 2.0, "Sell"),
                      (1002.0, "X", 121.0, 1.0, "Buy")])
    cfg = type("C", (), {"value_area_pct": 0.68})()
    swings = [Swing(0, 122.0, "high", ts=1000.0)]  # предыдущий swing high (шорт)
    prof = _swing_profile_for(db, cfg, "X", swings, "short", bucket_size=1.0,
                              now=1100.0)
    assert prof is not None
    assert prof.bucket_delta(120) == 6.0     # 8 buy − 2 sell (после swing)
    # принт 900 (старее swing-якоря 1000) не попал
    assert prof.bucket_volume(int(100.0)) == 0.0
    db.close()


def test_swings_for_reverses_desc_kline():
    """Фикс 2026-07-02: Bybit get_kline отдаёт DESC (новые сверху) — _swings_for
    обязан развернуть в хронологию. Иначе Swing.idx инвертируется и «последний
    swing» (max idx в _last_swing_price/_recent_extreme) — самый СТАРЫЙ бар окна:
    BE-lock у шортов срабатывал сразу после филла, у лонгов — никогда; латч
    AuctionTracker сверял пробой с уровнем ~16ч давности."""
    from flowzone_bot.app.main import _swings_for
    from flowzone_bot.trading.executor import _last_swing_price
    # хронология: старый максимум 30 (idx2), недавний swing high 12 (idx6)
    highs = [5.0, 6.0, 30.0, 6.0, 5.0, 4.0, 12.0, 4.0, 3.0, 2.0]
    lows = [h - 1.0 for h in highs]
    rows_chrono = [[str(1_000_000 + i * 300_000), "0", str(h), str(l), "0", "0"]
                   for i, (h, l) in enumerate(zip(highs, lows))]
    rows_desc = list(reversed(rows_chrono))  # как отдаёт Bybit (DESC)
    client = type("C", (), {
        "get_kline": lambda self, sym, interval, limit: rows_desc})()
    cfg = type("Cfg", (), {"swing_kline_interval": "5", "swing_kline_limit": 200,
                           "swing_cache_sec": 60.0, "swing_left": 2,
                           "swing_right": 2})()
    swings = _swings_for(client, cfg, "X", {}, now=0.0)
    assert [s.price for s in swings if s.kind == "high"] == [30.0, 12.0]
    assert all(a.ts < b.ts for a, b in zip(swings, swings[1:]))  # хронология
    # «последний» swing high — недавний 12, а не старый максимум 30
    assert _last_swing_price(swings, "high") == 12.0


def test_symbol_state_seed_vp_restores_session_profile():
    """Фикс 2026-07-02: после рестарта mid-session per-session профиль
    восстанавливается из persisted-принтов (seed_vp), а не копится с нуля."""
    from flowzone_bot.data.aggregates import SymbolState
    st = SymbolState("X")
    st.set_session_windows([(0.0, 24.0)])
    st.set_vp_bucket_size(1.0)
    st.seed_vp([(1000.0, 100.5, 2.0, "Buy"), (1001.0, 100.7, 1.0, "Sell"),
                (1002.0, 101.2, 3.0, "Buy")], anchor=900.0)
    snap = st.snapshot()
    assert snap.vp_session_start == 900.0
    assert snap.vp_buckets[100] == (2.0, 1.0)
    assert snap.vp_buckets[101] == (3.0, 0.0)
    # идемпотентность: при уже непустом профиле повторный seed — no-op
    st.seed_vp([(1003.0, 100.5, 50.0, "Buy")], anchor=900.0)
    assert st.snapshot().vp_buckets[100] == (2.0, 1.0)


def test_seed_session_vp_backfills_from_db(tmp_path):
    """Интеграция: _seed_session_vp читает prints из SQLite и заполняет профиль
    символа с пустым VP (рестарт/ротация mid-session)."""
    import time as _t
    from flowzone_bot.app.main import _seed_session_vp
    from flowzone_bot.data.aggregates import SymbolState
    from flowzone_bot.state.db import FlowzoneDB
    db = FlowzoneDB(str(tmp_path))
    now = _t.time()
    db.insert_prints([(now - 60.0, "X", 100.5, 2.0, "Buy"),
                      (now - 30.0, "X", 101.2, 1.0, "Sell")])
    st = SymbolState("X")
    st.set_session_windows([(0.0, 24.0)])
    st.set_vp_bucket_size(1.0)
    _seed_session_vp(db, {"X": st}, [(0.0, 24.0)])
    snap = st.snapshot()
    assert snap.vp_buckets.get(100) == (2.0, 0.0)
    assert snap.vp_buckets.get(101) == (0.0, 1.0)
    assert snap.vp_session_start is not None
    db.close()


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


def test_reconcile_keeps_sl_hit_for_be_trail_close_in_small_profit(tmp_path):
    """E3 в пути reconciliation: закрытие по BE/trail-SL в МАЛЫЙ ПЛЮС не должно
    перебиваться на tp_hit по знаку net (кейсы #489/#496: exit=SL в плюс,
    close_reason ошибочно tp_hit). Канон-классификация — по пересечению sl/tp."""
    import sqlite3
    from flowzone_bot.trading.executor import Executor

    class _FakeClient:
        def closed_pnl_detail(self, *a, **k):
            return {"pnl": 0.31, "exit": 58503.0, "order_id": "x",
                    "created": 0.0}
        def closed_pnl_position(self, *a, **k):
            return None

    db = _fz_db(tmp_path)
    tid = db.insert_open(symbol="BTCUSDT", side="short", qty=0.00668,
                         entry=58549.40, sl=58502.6, tp=58275.0,
                         score=4, reasons="x", mode="live", ts_open=9000.0)
    # WS закрыл по traил-SL в малый плюс: exit=58503 ≈ sl=58502.6 → sl_hit
    db.mark_closed(tid, exit_price=58503.0, pnl_usd=0.31, fees_usd=0.0,
                   close_reason="sl_hit", ts_close=9000.0, provisional=False)
    ex = Executor(db=db, settings=_Cfg(), client=_FakeClient(), now=lambda: 10000.0)
    ex.reconcile()
    con = sqlite3.connect(str(tmp_path / "flowzone_bot.sqlite"))
    row = con.execute("SELECT close_reason FROM trades WHERE id=?", (tid,)).fetchone()
    con.close()
    assert row[0] == "sl_hit"   # REST-сверка НЕ перебила на tp_hit по знаку +0.31
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
    # §2: acceptance вне VA = Value-Area-доля канон-автора 0.68 (68% объёма).
    assert cfg.context_accept_frac == 0.68
    # 2026-06-29: R:R-флор 1:2 (канон «1 to 2»). 2026-06-30: BE-lock + trail
    # возвращены к канону 39:00 (BE по пробою swing-уровня + CVD pressure,
    # trail за absorption-принтом; не [НАШЕ] zone_width-триггер).
    assert cfg.min_rr == 2.0
    assert cfg.be_lock_enabled is True
    assert cfg.be_lock_break_structure is True
    assert cfg.be_lock_cvd_gate is True
    assert cfg.trail_enabled is True
    assert cfg.trail_window_sec == 300.0
    assert not hasattr(cfg, "be_lock_zone_mult")  # удалён — не канон


# ─── R:R-флор 1:2 (канон «1 to 2», 2026-06-29) ───────────────────────────

def test_evaluate_rr_floor_2_lets_rr_between_2_and_2_5():
    """Канон-флор «1 to 2» (Fabervaale): R:R в [2.0, 2.5) теперь проходит
    (раньше при min_rr=2.5 отбрасывалось → бот встал на крипто). rr≈2.3 → сигнал."""
    prof = build_profile(_short_reload_profile(), bucket_size=1.0)
    now = 1000.0
    snap = _evictless_state_snapshot(prof.buckets, 1.0, _short_reload_trades(now),
                                     last_price=119.5, ts=now)
    ctx = classify(prof, snap.last_price, accept_frac=0.68)
    from flowzone_bot.analysis.strategy import evaluate
    # entry≈119.5, sl≈127.5 → risk≈8.0; swing low=101 → reward≈18.5 → rr≈2.31
    swing = [type("S", (), {"kind": "low", "price": 101.0})()]
    sig = evaluate(snap, ctx, prof, cfg=_Cfg(), swings=swing)
    assert sig is not None
    # rr в reasons должен быть ≥ 2.0 (точное значение считаем для гарантии)
    rr = next(float(r.split("=")[1]) for r in sig.reasons if r.startswith("rr="))
    assert 2.0 <= rr < 2.5


# ─── BE-lock + trail (канон Trade Management, видео 39:00, 2026-06-30) ──────

def _be_cfg():
    c = _Cfg()
    c.be_lock_enabled = True
    c.be_lock_break_structure = True
    c.be_lock_cvd_gate = True
    c.trail_enabled = True
    return c


def _swing(kind, price, idx=5, ts=100.0):
    # ts=100 > ts_open=0 (_be_trade): swing «подтверждён после входа» —
    # BE-триггер берёт только post-entry структуры (фикс 2026-07-02).
    return type("S", (), {"kind": kind, "price": price, "idx": idx, "ts": ts})()


def _snap(price, trades, ts=1000.0):
    return type("Snap", (), {"last_price": price, "ts": ts, "trades": trades})()


class _FakeClientBE:
    """Клиент для BE/trail тестов: round_price без округления, set_trading_stop
    всегда ok, instrument() — None."""
    def __init__(self):
        self.stops = []  # журнал вызовов set_trading_stop
    def round_price(self, symbol, price):
        return price
    def set_trading_stop(self, symbol, *, sl_price, tp_price):
        self.stops.append((symbol, sl_price, tp_price))
        return {"ok": True}
    def instrument(self, symbol):
        return None


class _FakeDBBE:
    """БД-стаб: open_trades возвращает mutable TradeRow-подобные объекты;
    update_levels мутирует in-memory sl."""
    def __init__(self, trs):
        self._trs = trs
        self.updates = []
    def open_trades(self):
        return list(self._trs)
    def update_levels(self, tid, *, sl, tp):
        self.updates.append((tid, sl, tp))
        for tr in self._trs:
            if tr.id == tid:
                tr.sl = sl


def _be_trade(side, entry, sl, tp, mode="live"):
    return type("T", (), {
        "id": 1, "symbol": "BTCUSDT", "side": side, "mode": mode,
        "entry": entry, "sl": sl, "tp": tp, "qty": 1.0, "ts_open": 0.0,
        "zone_low": 0.0, "zone_high": 0.0,
    })()


def test_be_lock_long_after_swing_break():
    """Канон «when you break this level»: LONG, swing high=110, цена 111 > 110 +
    CVD buy-доминирует → SL 90 → BE ≈100.08 (не zone_width-триггер)."""
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("long", entry=100.0, sl=90.0, tp=120.0)
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    swings = [_swing("high", 110.0)]
    trades = [TradePrint(900, 110.5, 5.0, "Buy"),
              TradePrint(910, 110.8, 5.0, "Buy"),
              TradePrint(920, 111.0, 1.0, "Sell")]  # buy_vol 10 > sell_vol 1
    ex._maybe_be_lock(tr, price=111.0, swings=swings, trades=trades)
    assert len(cl.stops) == 1
    new_sl = cl.stops[0][1]
    assert abs(new_sl - 100.08) < 0.01              # BE = entry + 8 bps
    assert tr.sl == new_sl
    assert db.updates[0][1] == new_sl


def test_be_lock_short_after_swing_break():
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("short", entry=100.0, sl=110.0, tp=80.0)
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    swings = [_swing("low", 90.0)]
    trades = [TradePrint(900, 89.5, 5.0, "Sell"),
              TradePrint(910, 89.2, 5.0, "Sell"),
              TradePrint(920, 89.0, 1.0, "Buy")]  # sell_vol 10 > buy_vol 1
    ex._maybe_be_lock(tr, price=89.0, swings=swings, trades=trades)
    assert len(cl.stops) == 1
    new_sl = cl.stops[0][1]
    assert abs(new_sl - 99.92) < 0.01              # BE = entry − 8 bps


def test_be_lock_no_trigger_before_swing_break():
    """Цена НЕ пробила swing-уровень (109 < 110) → BE не срабатывает."""
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("long", entry=100.0, sl=90.0, tp=120.0)
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    swings = [_swing("high", 110.0)]
    trades = [TradePrint(900, 108.5, 5.0, "Buy")]
    ex._maybe_be_lock(tr, price=109.0, swings=swings, trades=trades)
    assert cl.stops == []
    assert tr.sl == 90.0


def test_be_lock_no_trigger_without_swings():
    """Нет swing-данных → нельзя подтвердить «break this level» → no BE
    (канон: BE по структурному пробою, не по zone_width)."""
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("long", entry=100.0, sl=90.0, tp=120.0)
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    ex._maybe_be_lock(tr, price=200.0, swings=[], trades=[])
    assert cl.stops == []
    assert tr.sl == 90.0


def test_be_lock_ignores_pre_entry_swings():
    """Фикс 2026-07-02: swing, подтверждённый ДО входа (ts ≤ ts_open), не
    триггерит BE — ближайший пред-entry swing в сторону сделки совпадает с
    TP-целью (тот же набор фракталов, что у nearest_swing_target), т.е. его
    «пробой» = момент исполнения TP."""
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("long", entry=100.0, sl=90.0, tp=120.0)  # ts_open=0
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    swings = [_swing("high", 110.0, ts=0.0)]  # подтверждён ДО входа
    trades = [TradePrint(900, 110.5, 5.0, "Buy")]
    ex._maybe_be_lock(tr, price=111.0, swings=swings, trades=trades)
    assert cl.stops == []
    assert tr.sl == 90.0


def test_be_lock_ignores_swing_at_or_beyond_tp():
    """Swing на уровне TP (или дальше) не триггерит BE: триггер обязан лежать
    строго МЕЖДУ entry и TP (пробой TP-уровня = исполнение биржевого TP)."""
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("long", entry=100.0, sl=90.0, tp=120.0)
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    swings = [_swing("high", 120.0)]  # == TP
    trades = [TradePrint(900, 120.5, 5.0, "Buy")]
    ex._maybe_be_lock(tr, price=120.5, swings=swings, trades=trades)
    assert cl.stops == []
    assert tr.sl == 90.0


def test_be_lock_cvd_gate_blocks_when_counter_dominates():
    """tradezella «If CVD shows strong pressure»: swing пробит, но контр-сторона
    доминирует (long, sell_vol > buy_vol) → BE НЕ срабатывает (нет pressure)."""
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("long", entry=100.0, sl=90.0, tp=120.0)
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    swings = [_swing("high", 110.0)]
    trades = [TradePrint(900, 111.0, 5.0, "Sell"),
              TradePrint(910, 111.0, 5.0, "Sell"),
              TradePrint(920, 111.0, 1.0, "Buy")]  # sell 10 > buy 1 → no pressure
    ex._maybe_be_lock(tr, price=111.0, swings=swings, trades=trades)
    assert cl.stops == []
    assert tr.sl == 90.0


def test_be_lock_idempotent_when_sl_already_at_be():
    """Повторный тик после BE: SL уже в BE (tr.sl == be_sl) → не ослабляем
    защиту (long: be_sl <= tr.sl) → silent no-op. Канон cross-tick idempotency
    через persisted SL (executor rebuilds tr from DB each cycle)."""
    from flowzone_bot.trading.executor import Executor
    be_sl = 100.08
    tr = _be_trade("long", entry=100.0, sl=be_sl, tp=120.0)  # уже в BE
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    swings = [_swing("high", 110.0)]
    trades = [TradePrint(900, 115.0, 5.0, "Buy")]
    ex._maybe_be_lock(tr, price=115.0, swings=swings, trades=trades)
    assert cl.stops == []                           # no-op — SL уже в BE
    assert db.updates == []


def test_be_lock_disabled_via_config():
    """FLOWZONE_BE_LOCK_ENABLED=false → BE не применяется (reversible)."""
    from flowzone_bot.trading.executor import Executor
    cfg = _be_cfg()
    cfg.be_lock_enabled = False
    tr = _be_trade("long", entry=100.0, sl=90.0, tp=120.0)
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=cfg, client=cl, now=lambda: 1.0)
    swings = [_swing("high", 110.0)]
    ex._maybe_be_lock(tr, price=200.0, swings=swings, trades=[])
    assert cl.stops == []
    assert tr.sl == 90.0


# ─── Trail (стадия 2, канон «this print a new one, you bring your stop loss
#     here and you continue», видео 39:00) ───────────────────────────────────

def test_trail_long_moves_sl_behind_absorption_print():
    """После BE (SL в BE 100.08) — deep SELL print @112 ниже цены 115 = поддержка
    → SL подтягивается сразу ПОД неё (111.92, ЗА уровнем — конвенция «стоп за
    зоной» §5.2; фикс 2026-07-02: буфер внутрь уровня выбивал на ретесте)."""
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("long", entry=100.0, sl=100.08, tp=120.0)  # уже в BE
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    trades = [TradePrint(880, 114.0, 1.0, "Buy"),
              TradePrint(890, 114.5, 1.0, "Buy"),
              TradePrint(900, 112.0, 10.0, "Sell"),   # deep SELL @112 (под ценой)
              TradePrint(910, 112.0, 10.0, "Sell")]
    snap = _snap(115.0, trades)
    ex._maybe_trail(tr, snap)
    assert len(cl.stops) == 1
    new_sl = cl.stops[0][1]
    assert abs(new_sl - 111.92) < 0.01              # anchor 112 − buf 0.08 (ЗА уровнем)
    assert new_sl > 100.08                           # в сторону сделки (вверх)
    assert tr.sl == new_sl


def test_trail_short_moves_sl_behind_absorption_print():
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("short", entry=100.0, sl=99.92, tp=80.0)  # уже в BE
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    trades = [TradePrint(880, 86.0, 1.0, "Sell"),
              TradePrint(890, 85.5, 1.0, "Sell"),
              TradePrint(900, 88.0, 10.0, "Buy"),    # deep BUY @88 (над ценой 85)
              TradePrint(910, 88.0, 10.0, "Buy")]
    snap = _snap(85.0, trades)
    ex._maybe_trail(tr, snap)
    assert len(cl.stops) == 1
    new_sl = cl.stops[0][1]
    assert abs(new_sl - 88.08) < 0.01              # anchor 88 + buf 0.08 (ЗА уровнем)
    assert new_sl < 99.92                           # в сторону сделки (вниз)


def test_trail_skipped_before_be():
    """SL ещё не в BE (initial SL < entry для long) → стадия 2 не запускается
    (канон: trail ПОСЛЕ «break this level → BE»)."""
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("long", entry=100.0, sl=90.0, tp=120.0)  # initial SL, не BE
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    trades = [TradePrint(900, 112.0, 10.0, "Sell"),
              TradePrint(910, 112.0, 10.0, "Sell"),
              TradePrint(920, 114.0, 1.0, "Buy")]
    snap = _snap(115.0, trades)
    ex._maybe_trail(tr, snap)
    assert cl.stops == []
    assert tr.sl == 90.0


def test_trail_never_re_widen():
    """Канон forex.in.rs «never re-widen a stop»: новый absorption-уровень ДАЛЬШЕ
    от сделки чем текущий SL → SL не двигается (не ослабляем защиту)."""
    from flowzone_bot.trading.executor import Executor
    tr = _be_trade("long", entry=100.0, sl=113.0, tp=120.0)  # SL уже выше anchor
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=_be_cfg(), client=cl, now=lambda: 1.0)
    trades = [TradePrint(880, 114.0, 1.0, "Buy"),
              TradePrint(890, 114.5, 1.0, "Buy"),
              TradePrint(900, 112.0, 10.0, "Sell"),   # anchor 112 → new_sl 111.92
              TradePrint(910, 112.0, 10.0, "Sell")]   # 111.92 < tr.sl 113 → no-op
    snap = _snap(115.0, trades)
    ex._maybe_trail(tr, snap)
    assert cl.stops == []
    assert tr.sl == 113.0


def test_trail_disabled_via_config():
    """FLOWZONE_TRAIL_ENABLED=false → стадия 2 выключена (reversible)."""
    from flowzone_bot.trading.executor import Executor
    cfg = _be_cfg()
    cfg.trail_enabled = False
    tr = _be_trade("long", entry=100.0, sl=100.08, tp=120.0)
    db = _FakeDBBE([tr])
    cl = _FakeClientBE()
    ex = Executor(db=db, settings=cfg, client=cl, now=lambda: 1.0)
    trades = [TradePrint(900, 112.0, 10.0, "Sell"),
              TradePrint(910, 112.0, 10.0, "Sell"),
              TradePrint(920, 114.0, 1.0, "Buy")]
    snap = _snap(115.0, trades)
    ex._maybe_trail(tr, snap)
    assert cl.stops == []


# ─── close_reason: классификация по tp/sl (E3, 2026-06-30) ──────────────────

def test_bracket_exit_reason_uses_tp_sl_not_entry_sign():
    """После BE/trail SL стоит в стороне прибыли — классификация по знаку (exit−
    entry) ломается (#489: exit=SL, pnl +0.25, reason=tp_hit). Канон-корректно:
    по пересечению tr.tp / tr.sl."""
    from flowzone_bot.trading.executor import bracket_exit_reason
    # long, BE-SL=100.08 (выше entry 100), TP=120: закрытие по BE-SL @100.08 →
    # exit <= sl → sl_hit (НЕ tp_hit, хотя exit > entry).
    assert bracket_exit_reason("long", 100.0, 100.08, sl=100.08, tp=120.0) == "sl_hit"
    # long, закрытие по TP @120 → tp_hit
    assert bracket_exit_reason("long", 100.0, 120.0, sl=90.0, tp=120.0) == "tp_hit"
    # short, BE-SL=99.92 (ниже entry 100), TP=80: закрытие по BE-SL @99.92 →
    # exit >= sl → sl_hit (НЕ tp_hit, хотя exit < entry).
    assert bracket_exit_reason("short", 100.0, 99.92, sl=99.92, tp=80.0) == "sl_hit"
    # short, TP @80 → tp_hit
    assert bracket_exit_reason("short", 100.0, 80.0, sl=110.0, tp=80.0) == "tp_hit"


def test_bracket_exit_reason_fallback_without_sl_tp():
    """Без sl/tp — фолбэк на знак (совместимость со старыми вызовами)."""
    from flowzone_bot.trading.executor import bracket_exit_reason
    assert bracket_exit_reason("long", 100.0, 103.5) == "tp_hit"
    assert bracket_exit_reason("long", 100.0, 99.0) == "sl_hit"
    assert bracket_exit_reason("long", 100.0, None) == "tp_sl"


# ─── DB: zone_low/zone_high persist (2026-06-29) ──────────────────────────

def test_db_persists_zone_low_high(tmp_path):
    from flowzone_bot.state.db import FlowzoneDB
    db = FlowzoneDB(str(tmp_path))
    tid = db.insert_open(symbol="BTCUSDT", side="long", qty=0.1, entry=100.0,
                         sl=90.0, tp=120.0, score=3, reasons="x", mode="live",
                         zone_low=90.0, zone_high=100.0)
    trs = db.open_trades()
    assert len(trs) == 1
    assert trs[0].zone_low == 90.0
    assert trs[0].zone_high == 100.0
    db.close()


def test_db_migrates_existing_trades_add_zone_columns(tmp_path):
    """Существующая БД (без zone_low/zone_high) мигрируется: колонки
    добавляются, старые сделки читаются с zone_low=None."""
    import sqlite3
    from flowzone_bot.state.db import FlowzoneDB
    path = str(tmp_path / "flowzone_bot.sqlite")  # путь, который откроет FlowzoneDB
    # создаём «старую» БД без zone-колонок
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts_open REAL, symbol TEXT,
        side TEXT, qty REAL, entry REAL, sl REAL, tp REAL, score INTEGER,
        reasons TEXT, mode TEXT, strategy TEXT, status TEXT,
        entry_order_id TEXT, ts_close REAL, exit REAL, pnl_usd REAL,
        fees_usd REAL, close_reason TEXT, pnl_provisional INTEGER,
        pnl_verified INTEGER);
    INSERT INTO trades (ts_open,symbol,side,qty,entry,sl,tp,score,reasons,
        mode,strategy,status) VALUES (1,'X','long',1,100,90,120,3,'r','live',
        'flowzone','open');
    """)
    con.commit()
    con.close()
    # открытие через FlowzoneDB запускает миграцию
    db = FlowzoneDB(str(tmp_path))
    trs = db.open_trades()
    assert len(trs) == 1
    assert trs[0].zone_low is None        # старая сделка — без зоны
    assert trs[0].zone_high is None
    db.close()


# ─── D1/D4/D3/D7: канон-аудит 2026-06-30 ────────────────────────────────

# D4: classify_shape — форма профиля (P-shape / double-distribution / balance)

def _pshape_up_buckets():
    """Тяжёлый верхний хвост с buy-доминантой → P_SHAPE_UP (канон: aggressive
    buyers → directional). Ядро у idx20, хвост выше VAH из buy-принтов."""
    core = {20: 100, 21: 70, 19: 70, 22: 30, 18: 30}
    # хвост ВЫШЕ VAH — ТОЛЬКО buy (sell=0) → buy-доминанта в хвосте
    tail = {26: (15, 0.0), 27: (15, 0.0), 28: (15, 0.0), 29: (15, 0.0), 30: (15, 0.0)}
    out = {i: (float(v), 0.0) for i, v in core.items()}
    out.update(tail)
    return out


def _pshape_down_buckets():
    core = {20: 100, 21: 70, 19: 70, 22: 30, 18: 30}
    # хвост НИЖЕ VAL — ТОЛЬКО sell (buy=0) → sell-доминанта в нижнем хвосте
    tail = {10: (0.0, 15), 11: (0.0, 15), 12: (0.0, 15), 13: (0.0, 15), 14: (0.0, 15)}
    out = {i: (float(v), 0.0) for i, v in core.items()}
    out.update(tail)
    return out


def test_classify_shape_pshape_up_on_buy_dominant_upper_tail():
    prof = build_profile(_pshape_up_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=20.5, accept_frac=0.68)
    assert ctx.state == TREND_UP
    assert ctx.shape == P_SHAPE_UP


def test_classify_shape_pshape_down_on_sell_dominant_lower_tail():
    prof = build_profile(_pshape_down_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=20.5, accept_frac=0.68)
    assert ctx.state == TREND_DOWN
    assert ctx.shape == P_SHAPE_DOWN


def test_classify_shape_balance_on_symmetric_profile():
    prof = build_profile(_triangular_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=10.5, accept_frac=0.68)
    assert ctx.state == BALANCE
    assert ctx.shape == BALANCE_SHAPE


def test_classify_shape_double_distribution_two_clusters_with_lvn_neck():
    # два HVN-кластера (idx 10 и 20) с LVN-перешейком (idx 15) между ними.
    buckets = {i: (float(v), 0.0) for i, v in {
        10: 100, 9: 60, 11: 60, 8: 20, 12: 20,
        15: 5,                      # LVN-перешеек
        20: 100, 19: 60, 21: 60, 18: 20, 22: 20,
    }.items()}
    prof = build_profile(buckets, bucket_size=1.0)
    shape = classify_shape(prof, accept_above=0.0, accept_below=0.0,
                           accept_frac=0.68)
    assert shape == DOUBLE_DISTRIBUTION


def test_classify_shape_unknown_when_profile_none():
    assert classify_shape(None, 0.0, 0.0) == "unknown"


# D3: merge_profiles — composite / double-day profile

def test_merge_profiles_sums_buckets_and_recomputes_poc():
    p1 = build_profile({10: (100.0, 0.0), 11: (50.0, 0.0)}, bucket_size=1.0)
    p2 = build_profile({11: (0.0, 50.0), 12: (80.0, 0.0)}, bucket_size=1.0)
    merged = merge_profiles([p1, p2])
    assert merged is not None
    # idx 10 и 11 суммарно по 100 → POC тай-брейк к меньшему idx (10)
    assert merged.poc_idx == 10
    assert merged.bucket_volume(10) == 100.0
    assert merged.bucket_volume(11) == 100.0
    assert merged.bucket_volume(12) == 80.0
    assert merged.total_volume == 280.0


def test_merge_profiles_rejects_mismatched_bucket_size():
    p1 = build_profile({10: (100.0, 0.0)}, bucket_size=1.0)
    p2 = build_profile({10: (100.0, 0.0)}, bucket_size=2.0)
    assert merge_profiles([p1, p2]) is None


def test_merge_profiles_empty_returns_none():
    assert merge_profiles([]) is None
    assert merge_profiles([None, None]) is None


# D7: detect_initiative / detect_exhaustion

def _prints(seq):
    """seq = [(price, size, side), ...] → список TradePrint (ts = index)."""
    return [TradePrint(i, p, s, sd) for i, (p, s, sd) in enumerate(seq)]


def test_detect_initiative_long_on_strong_buy_delta_and_up_move():
    # 20 принтов: все Buy, цена растёт 100→110 → сильная buy-агрессия + close up
    seq = [(100 + i * 0.5, 2.0, "Buy") for i in range(20)]
    trades = _prints(seq)
    thr = big_trade_threshold(trades, pct=0.90, min_samples=10)
    res = detect_initiative(trades, "long", big_threshold=thr)
    assert res.confirmed is True
    assert res.net_delta > 0
    assert res.delta_frac >= 0.30


def test_detect_initiative_rejected_when_price_against_delta():
    # buy-доминанта, но цена ПАДАЕТ → не initiative (no close in direction)
    seq = [(100 - i * 0.5, 2.0, "Buy") for i in range(20)]
    trades = _prints(seq)
    thr = big_trade_threshold(trades, pct=0.90, min_samples=10)
    res = detect_initiative(trades, "long", big_threshold=thr)
    assert res.confirmed is False


def test_detect_initiative_short_on_strong_sell_delta_and_down_move():
    seq = [(100 - i * 0.5, 2.0, "Sell") for i in range(20)]
    trades = _prints(seq)
    thr = big_trade_threshold(trades, pct=0.90, min_samples=10)
    res = detect_initiative(trades, "short", big_threshold=thr)
    assert res.confirmed is True
    assert res.net_delta < 0


def test_detect_exhaustion_up_trend_with_decay_and_contrarian_sellers():
    # первая половина — тяжёлые buy (объём высокий), вторая — затухание + sell-хвост
    first = [(100 + i * 0.2, 10.0, "Buy") for i in range(10)]
    second = [(102 + i * 0.1, 3.0, "Sell") for i in range(10)]  # объём ниже + sell
    trades = _prints(first + second)
    res = detect_exhaustion(trades, "up")
    assert res.confirmed is True
    assert res.vol_decay <= 0.80
    assert res.contrarian_frac >= 0.60


def test_detect_exhaustion_rejected_without_volume_decay():
    # объём постоянный (нет затухания), хвост sell — но decay не пройден
    seq = [(100 + i * 0.1, 10.0, "Sell") for i in range(20)]
    trades = _prints(seq)
    res = detect_exhaustion(trades, "up")
    assert res.confirmed is False


def test_detect_exhaustion_down_trend_with_contrarian_buyers():
    first = [(100 - i * 0.2, 10.0, "Sell") for i in range(10)]
    second = [(98 - i * 0.1, 3.0, "Buy") for i in range(10)]
    trades = _prints(first + second)
    res = detect_exhaustion(trades, "down")
    assert res.confirmed is True


def test_detect_initiative_and_exhaustion_no_data_on_short_input():
    assert detect_initiative([], "long").confirmed is False
    assert detect_exhaustion([TradePrint(0, 100.0, 1.0, "Buy")], "up").confirmed is False


# ─── DirectionTelemetry: наблюдаемость устойчивости направления (06.07) ───
# Не гейтит вход; пишет init/dwell/day-extremes/shock в reasons для mining.

def _tele_snap(price, trades, session_anchor=700.0):
    return type("S", (), {"last_price": price, "trades": trades,
                          "vp_session_start": session_anchor})()


def _make_telemetry():
    from flowzone_bot.analysis.telemetry import DirectionTelemetry
    return DirectionTelemetry(big_trade_pct=0.90)


def test_telemetry_day_extremes_distance_bps():
    t = _make_telemetry()
    t.update("X", 1000.0, _tele_snap(100.0, []), [])
    t.update("X", 1010.0, _tele_snap(110.0, []), [])   # day high 110
    t.update("X", 1020.0, _tele_snap(95.0, []), [])    # day low 95
    f = t.features("X", 1030.0, 100.0)
    assert abs(f["day_hi_bps"] - (110 - 100) / 100 * 1e4) < 1e-6   # 1000 bp
    assert abs(f["day_lo_bps"] - (100 - 95) / 100 * 1e4) < 1e-6    # 500 bp
    # новый UTC-день → экстремумы сбрасываются
    t.update("X", 90000.0, _tele_snap(100.0, []), [])
    f2 = t.features("X", 90001.0, 100.0)
    assert f2["day_hi_bps"] == 0.0 and f2["day_lo_bps"] == 0.0


def test_telemetry_dwell_tracks_time_beyond_major_structural_extreme():
    t = _make_telemetry()
    # Ближайший high=105 уже пробит, но значимая структура=max highs=110.
    swings = [Swing(0, 110.0, "high", ts=100.0),
              Swing(1, 90.0, "low", ts=200.0),
              Swing(2, 105.0, "high", ts=300.0)]
    t.update("X", 990.0, _tele_snap(106.0, []), swings)
    assert "dwell_struct_up_sec" not in t.features("X", 991.0, 106.0)
    t.update("X", 1000.0, _tele_snap(111.0, []), swings)   # выше major high
    t.update("X", 1030.0, _tele_snap(112.0, []), swings)   # держится
    f = t.features("X", 1060.0, 112.0)
    assert abs(f["dwell_struct_up_sec"] - 60.0) < 1e-6
    assert "dwell_struct_dn_sec" not in f
    assert f["struct_hi_bps"] < 0   # цена уже выше major high
    # возврат под уровень → dwell сбрасывается
    t.update("X", 1090.0, _tele_snap(109.0, []), swings)
    assert "dwell_struct_up_sec" not in t.features("X", 1091.0, 109.0)


def test_telemetry_shock_detected_on_tick_burst_and_ages():
    t = _make_telemetry()
    calm = [TradePrint(i, 100.0, 1.0, "Buy") for i in range(100)]
    t.update("X", 0.0, _tele_snap(100.0, calm), [])       # ema=100 (прогрев)
    t.update("X", 130.0, _tele_snap(100.0, calm), [])     # warmed, без шока
    assert "shock_dir" not in t.features("X", 131.0, 100.0)
    # burst ×12 от базы, цена падает → shock:down
    burst = [TradePrint(i, 100.0 - i * 0.001, 1.0, "Sell") for i in range(1200)]
    t.update("X", 140.0, _tele_snap(98.8, burst), [])
    f = t.features("X", 200.0, 98.8)
    assert f["shock_dir"] == "down"
    assert abs(f["shock_age_sec"] - 60.0) < 1e-6


def test_telemetry_shock_expires_after_ttl_and_resets_on_session_change():
    t = _make_telemetry()
    calm = [TradePrint(i, 100.0, 1.0, "Buy") for i in range(100)]
    burst = [TradePrint(i, 100.0 - i * 0.001, 1.0, "Sell") for i in range(1200)]
    t.update("X", 0.0, _tele_snap(100.0, calm), [])
    t.update("X", 130.0, _tele_snap(100.0, calm), [])
    t.update("X", 140.0, _tele_snap(98.8, burst), [])
    assert t.features("X", 3740.0, 98.8)["shock_age_sec"] == 3600.0
    assert "shock_dir" not in t.features("X", 3740.1, 98.8)
    # Новый session anchor очищает shock немедленно, даже если TTL не истёк.
    t.update("X", 5000.0, _tele_snap(100.0, calm, session_anchor=4900.0), [])
    t.update("X", 5130.0, _tele_snap(100.0, calm, session_anchor=4900.0), [])
    t.update("X", 5140.0, _tele_snap(98.8, burst, session_anchor=4900.0), [])
    assert "shock_dir" in t.features("X", 5200.0, 98.8)
    t.update("X", 5210.0, _tele_snap(99.0, calm, session_anchor=5200.0), [])
    assert "shock_dir" not in t.features("X", 5211.0, 99.0)


def test_telemetry_initiative_uses_explicit_preceding_window():
    t = _make_telemetry()
    # Текущее absorption-окно = Sell, но initiative должен прийти только из
    # явно переданной предыдущей ноги = Buy.
    current = [TradePrint(1001 + i, 112.0 - i * 0.1, 2.0, "Sell")
               for i in range(25)]
    previous = [TradePrint(976 + i, 100.0 + i * 0.5, 2.0, "Buy")
                for i in range(25)]
    t.update("X", 1000.0, _tele_snap(112.0, current), [])
    assert "init_dir" not in t.features("X", 1010.0, 112.0)
    t.refresh_preceding_initiative("X", previous)
    f = t.features("X", 1010.0, 112.0)
    assert f["init_dir"] == "up"
    assert abs(f["init_age_sec"] - 10.0) < 1e-6
    s_long = t.fmt("X", 1010.0, "long", 112.0)
    s_short = t.fmt("X", 1010.0, "short", 112.0)
    assert "init_prev:up:100%:conf:10s:same" in s_long
    assert "init_prev:up:100%:conf:10s:counter" in s_short
    assert s_long.startswith("tele=")


def test_telemetry_initiative_written_even_when_not_confirmed():
    """Непрерывный скаляр: направление и сила дельты пишутся всегда.

    Бинарная защёлка «только confirmed» давала 2/16 покрытия на живых сделках —
    выборку в 100 сделок на таком темпе не набрать. Здесь дельта слабая
    (buy/sell почти поровну), initiative НЕ подтверждён, но поле есть.
    """
    t = _make_telemetry()
    previous = []
    for i in range(24):
        side = "Buy" if i % 2 == 0 else "Sell"
        previous.append(TradePrint(976 + i, 100.0, 2.0, side))
    t.refresh_preceding_initiative("X", previous)
    f = t.features("X", 1010.0, 100.0)
    assert f["init_confirmed"] is False
    assert f["init_frac"] == 0.0
    assert "init_prev:" in t.fmt("X", 1010.0, "long", 100.0)
    assert ":conf:" not in t.fmt("X", 1010.0, "long", 100.0)


def test_telemetry_vratio_is_continuous_after_warmup():
    """vratio пишется всегда после прогрева, а дискретный shock — только ×4.

    Реплей по тикам за 5.5ч дал максимум ×2.1-3.0 при пороге ×4: дискретное
    поле не набирает статистику, непрерывное отношение доступно постоянно.
    """
    t = _make_telemetry()
    base = [TradePrint(0.0, 100.0, 1.0, "Buy")] * 200
    t.update("X", 0.0, _tele_snap(100.0, base), [])
    t.update("X", 200.0, _tele_snap(100.0, base), [])
    f = t.features("X", 200.0, 100.0)
    assert "vratio" in f
    assert 0.9 <= f["vratio"] <= 1.1     # лента ровная → отношение около 1
    assert "shock_dir" not in f          # ×4 не достигнут
    assert "vratio:" in t.fmt("X", 200.0, "long", 100.0)


def test_telemetry_fmt_none_when_no_data():
    t = _make_telemetry()
    assert t.fmt("X", 0.0, None, None) == "tele=none"


def test_refresh_preceding_initiative_reads_previous_window_only():
    from flowzone_bot.app.main import _refresh_preceding_initiative

    class _DB:
        def __init__(self):
            self.args = None

        def prints_since(self, symbol, since, until):
            self.args = (symbol, since, until)
            return [(ts, 100.0 + i * 0.5, 2.0, "Buy")
                    for i, ts in enumerate(range(676, 701))]

    class _Telemetry:
        def __init__(self):
            self.trades = None

        def refresh_preceding_initiative(self, symbol, trades):
            self.symbol = symbol
            self.trades = trades

    db, tele = _DB(), _Telemetry()
    cfg = type("C", (), {"absorption_window_sec": 300.0})()
    _refresh_preceding_initiative(db, cfg, tele, "BTCUSDT", now=1000.0)
    # Предыдущая нога [now-600, now-300), текущее окно [700,1000] исключено.
    assert db.args == ("BTCUSDT", 400.0, 700.0)
    assert tele.symbol == "BTCUSDT"
    assert all(isinstance(t, TradePrint) for t in tele.trades)
    assert tele.trades[-1].ts == 700.0

# ─── Строгий канон 2026-07-29 (C1-C5): merge, shape-гейт, initiative, hook ──


class _CfgCanon(_Cfg):
    """cfg с включённым полным каноном (C2/C5) — дефолты settings.py."""
    value_area_pct = 0.68
    initiative_exhaustion_enabled = True
    initiative_min_delta_frac = 0.30
    exhaustion_window_sec = 300.0
    exhaustion_min_decay = 0.80
    exhaustion_min_contrarian_frac = 0.60
    hook_enabled = True
    hook_lookback_sec = 3600.0


# C3: форма профиля гейтит направление (канон 34:32 «Is not a P shape. So it's
# still balance. You can use this as indecision.»)

def _down_tail_without_sell_delta() -> dict[int, tuple[float, float]]:
    """Acceptance ниже VAL есть, но хвост НАБРАН ПОКУПАТЕЛЯМИ — не P-shape."""
    core = {20: 100, 21: 70, 19: 70, 22: 30, 18: 30}
    tail = {10: 15, 11: 15, 12: 15, 13: 15, 14: 15}
    return {i: (float(v), 0.0) for i, v in {**core, **tail}.items()}


def test_shape_gate_rejects_heavy_tail_without_directional_delta():
    prof = build_profile(_down_tail_without_sell_delta(), bucket_size=1.0)
    gated = classify(prof, last_price=20.5, accept_frac=0.68)
    assert gated.state == BALANCE          # канон: indecision, не тренд
    assert gated.trade_side is None
    # без гейта (старое поведение) тот же профиль давал trend_down
    ungated = classify(prof, last_price=20.5, accept_frac=0.68, shape_gate=False)
    assert ungated.state == TREND_DOWN


def test_shape_gate_keeps_trend_when_tail_delta_confirms():
    prof = build_profile(_down_elongated_buckets(), bucket_size=1.0)
    ctx = classify(prof, last_price=20.5, accept_frac=0.68)
    assert ctx.state == TREND_DOWN
    assert ctx.shape in (P_SHAPE_DOWN, DOUBLE_DISTRIBUTION)


# C1: merge перекрывающихся сессионных профилей (канон 31:14)

def test_value_areas_overlap_detects_same_horizontal_level():
    a = build_profile({20: (100.0, 0.0), 21: (70.0, 0.0), 19: (70.0, 0.0)}, 1.0)
    same = build_profile({20: (90.0, 0.0), 21: (60.0, 0.0), 19: (60.0, 0.0)}, 1.0)
    far = build_profile({80: (90.0, 0.0), 81: (60.0, 0.0), 79: (60.0, 0.0)}, 1.0)
    assert value_areas_overlap(a, same)
    assert not value_areas_overlap(a, far)


def test_session_profile_survives_db_roundtrip(tmp_path):
    from flowzone_bot.state.db import FlowzoneDB
    db = FlowzoneDB(str(tmp_path))
    buckets = {20: (100.0, 5.0), 21: (70.0, 1.0)}
    db.save_session_profile("BTCUSDT", 1000.0, 0.5, buckets, ts=1100.0)
    db.save_session_profile("BTCUSDT", 2000.0, 0.5, {30: (10.0, 0.0)}, ts=2100.0)
    rows = db.recent_session_profiles("BTCUSDT", before_start=2000.0, limit=3)
    assert len(rows) == 1                      # текущая сессия исключена
    start, bucket_size, restored = rows[0]
    assert start == 1000.0 and bucket_size == 0.5
    assert restored == buckets                 # объёмы не потерялись
    assert db.prune_session_profiles_before(1500.0) == 1
    assert db.recent_session_profiles("BTCUSDT", before_start=9e9) != []
    db.close()


def test_merged_profile_sharpens_value_area_low():
    """Канон 31:59: склейка перекрывающихся дней даёт «really precise VAL»."""
    day1 = build_profile({20: (100.0, 0.0), 21: (60.0, 0.0), 19: (60.0, 0.0),
                          18: (20.0, 0.0)}, 1.0)
    day2 = build_profile({20: (90.0, 0.0), 21: (55.0, 0.0), 19: (80.0, 0.0),
                          18: (25.0, 0.0)}, 1.0)
    assert value_areas_overlap(day1, day2)
    merged = merge_profiles([day1, day2])
    assert merged is not None
    assert merged.total_volume == day1.total_volume + day2.total_volume
    assert merged.val <= day1.val or merged.val <= day2.val


# C5: hook / failed auction (канон 26:17, 27:20)

def _hook_prints_long(beyond_size: float) -> list[TradePrint]:
    """Торговля внутри VA + вылазка ниже VAL заданного объёма."""
    inside = [TradePrint(float(i), 105.0, 1.0, "Buy") for i in range(20)]
    below = [TradePrint(20.0 + i, 98.0, beyond_size, "Sell") for i in range(3)]
    return inside + below


def test_detect_hook_confirms_rejected_excursion_below_val():
    from flowzone_bot.analysis.hook import detect_hook
    hook = detect_hook(_hook_prints_long(1.0), "long", vah=110.0, val=100.0,
                       last_price=101.0)
    assert hook.confirmed
    assert hook.boundary == 100.0
    assert hook.extreme == 98.0
    assert hook.beyond_frac < 0.32          # не приняли снаружи


def test_detect_hook_rejects_accepted_breakout():
    """Если за границей наторговали много — это принятие, а не failed auction."""
    from flowzone_bot.analysis.hook import detect_hook
    hook = detect_hook(_hook_prints_long(20.0), "long", vah=110.0, val=100.0,
                       last_price=101.0)
    assert not hook.confirmed
    assert "accepted_outside" in hook.reasons


def test_detect_hook_requires_return_inside_value_area():
    from flowzone_bot.analysis.hook import detect_hook
    hook = detect_hook(_hook_prints_long(1.0), "long", vah=110.0, val=100.0,
                       last_price=97.0)   # всё ещё снаружи
    assert not hook.confirmed
    assert "not_back_inside" in hook.reasons


def test_evaluate_takes_hook_setup_when_no_confluence_zone():
    from flowzone_bot.analysis.context import Context
    from flowzone_bot.analysis.strategy import evaluate
    ctx = Context(TREND_UP, vah=110.0, val=100.0, poc=105.0, last_price=101.0,
                  shape=P_SHAPE_UP)
    snap = _evictless_state_snapshot({}, 1.0, [], last_price=101.0, ts=1000.0)
    swings = [type("S", (), {"kind": "high", "price": 115.0})()]
    sig = evaluate(snap, ctx, None, cfg=_CfgCanon(), swings=swings,
                   hook_prints=_hook_prints_long(1.0))
    assert sig is not None
    assert sig.side == "long"
    assert "setup=hook" in sig.reasons
    # стоп за экстремумом неудачной вылазки: принятие снаружи убивает тезис
    assert sig.sl_level < 98.0
    assert sig.tp_level == 115.0


def test_evaluate_no_hook_when_disabled():
    from flowzone_bot.analysis.context import Context
    from flowzone_bot.analysis.strategy import evaluate
    ctx = Context(TREND_UP, vah=110.0, val=100.0, poc=105.0, last_price=101.0,
                  shape=P_SHAPE_UP)
    snap = _evictless_state_snapshot({}, 1.0, [], last_price=101.0, ts=1000.0)
    swings = [type("S", (), {"kind": "high", "price": 115.0})()]
    assert evaluate(snap, ctx, None, cfg=_Cfg(), swings=swings,
                    hook_prints=_hook_prints_long(1.0)) is None


# C2: initiative — второй триггер входа, exhaustion — фиксация (канон 37:03)

def _initiative_short_trades(now: float) -> list[TradePrint]:
    """Momentum-вход: продавцы агрессируют И получают результат (цена падает).

    Absorption здесь НЕ подтвердится — контр-сторона (Buy) не давила, её нечего
    поглощать. Канон «The Simplest Orderflow Trading Model»: рынок не даёт
    теста зоны с поглощением, берём momentum-триггер.
    """
    return [TradePrint(now - 100, 120.0, 8.0, "Sell"),
            TradePrint(now - 60, 119.8, 8.0, "Sell"),
            TradePrint(now - 10, 119.5, 2.0, "Buy")]


def test_initiative_confirms_entry_when_absorption_absent():
    from flowzone_bot.analysis.orderflow import detect_absorption
    from flowzone_bot.analysis.strategy import evaluate
    prof = build_profile(_short_reload_profile(), bucket_size=1.0)
    now = 1000.0
    trades = _initiative_short_trades(now)
    snap = _evictless_state_snapshot(prof.buckets, 1.0, trades,
                                     last_price=119.5, ts=now)
    ctx = classify(prof, snap.last_price, accept_frac=0.68)
    assert ctx.state == TREND_DOWN
    thr = big_trade_threshold(trades, pct=0.90, min_samples=3)
    assert not detect_absorption(trades, "short", big_threshold=thr).confirmed
    swings = [type("S", (), {"kind": "low", "price": 95.0})()]
    assert evaluate(snap, ctx, prof, cfg=_Cfg(), swings=swings) is None
    sig = evaluate(snap, ctx, prof, cfg=_CfgCanon(), swings=swings)
    assert sig is not None
    assert "trigger=initiative" in sig.reasons


def test_absorption_still_preferred_over_initiative():
    """Основной сетап §4 не должен подменяться momentum-вариантом."""
    from flowzone_bot.analysis.strategy import evaluate
    prof = build_profile(_short_reload_profile(), bucket_size=1.0)
    now = 1000.0
    snap = _evictless_state_snapshot(prof.buckets, 1.0, _short_reload_trades(now),
                                     last_price=119.5, ts=now)
    ctx = classify(prof, snap.last_price, accept_frac=0.68)
    swings = [type("S", (), {"kind": "low", "price": 95.0})()]
    sig = evaluate(snap, ctx, prof, cfg=_CfgCanon(), swings=swings)
    assert sig is not None
    assert "trigger=absorption" in sig.reasons


class _ExhaustTrade:
    def __init__(self, side="long", entry=100.0):
        self.id = 1
        self.side = side
        self.entry = entry
        self.qty = 1.0
        self.mode = "paper"


def _exhaustion_ready(cfg, trade, snap):
    """Вызвать проверку стадии 3 без конструирования полного Executor."""
    from flowzone_bot.trading.executor import Executor
    ex = Executor.__new__(Executor)
    ex._cfg = cfg
    return Executor._exhaustion_exit_ready(ex, trade, snap)


def _decaying_up_move(now: float) -> list[TradePrint]:
    """Аптренд выдыхается: объём падает + продавцы забирают хвост окна."""
    first = [TradePrint(now - 250 + i, 100.0 + i * 0.1, 4.0, "Buy")
             for i in range(12)]
    second = [TradePrint(now - 100 + i, 101.2, 1.0, "Buy") for i in range(6)]
    tail = [TradePrint(now - 40 + i, 101.1, 3.0, "Sell") for i in range(9)]
    return first + second + tail


def test_exhaustion_exit_fires_on_fading_move_in_profit():
    now = 1000.0
    snap = type("S", (), {"last_price": 101.1, "ts": now,
                          "trades": _decaying_up_move(now)})()
    assert _exhaustion_ready(_CfgCanon(), _ExhaustTrade(entry=100.0), snap)


def test_exhaustion_exit_skipped_when_position_in_loss():
    """Канон фиксирует ПРИБЫЛЬ; убыток отдаём стопу, а не exhaustion-выходу."""
    now = 1000.0
    snap = type("S", (), {"last_price": 101.1, "ts": now,
                          "trades": _decaying_up_move(now)})()
    assert not _exhaustion_ready(_CfgCanon(), _ExhaustTrade(entry=105.0), snap)


def test_exhaustion_exit_disabled_by_flag():
    now = 1000.0
    snap = type("S", (), {"last_price": 101.1, "ts": now,
                          "trades": _decaying_up_move(now)})()
    assert not _exhaustion_ready(_Cfg(), _ExhaustTrade(entry=100.0), snap)
