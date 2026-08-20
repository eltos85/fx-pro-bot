"""Арифметика выбора ядра и сайзинга (шаги 4-5 STRATEGY_HYBRID.md).

Проверяется то, что легко сломать незаметно: момент исполнения сигнала,
попадание межбарных разрывов в счёт, комиссии по ногам, funding только на
открытой позиции, три схемы сайзинга и метрики сравнения с холдом.
Сети и ключей тесты не требуют.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / \
    "hybrid_core_select.py"
_spec = importlib.util.spec_from_file_location("hybrid_core_select", _SRC)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

D = mod.DAY_MS
FEE = mod.TAKER


def _bars(prices, *, gap=None):
    """(ts, o, h, l, c) по дням. `gap[i]` задаёт открытие бара i отдельно."""
    out = []
    for i, c in enumerate(prices):
        op = c if gap is None or gap[i] is None else gap[i]
        out.append((i * D, op, max(op, c), min(op, c), c))
    return out


# ─── сигналы: считаем на close, исполняем на следующем открытии ─────────────


def test_sma_cross_signal_lands_on_next_bar():
    bars = _bars([10, 10, 10, 10, 20, 20])
    sched = mod.sma_cross(bars, 2, 4)
    # Разогрев: до 4-го бара средних нет, расписания тоже.
    assert 0 not in sched and bars[3][0] not in sched
    # На close бара 3 быстрая = медленная → не выше, состояние 0 на бар 4.
    assert sched[bars[4][0]] == 0
    # На close бара 4 (цена 20) быстрая ушла выше → лонг с бара 5.
    assert sched[bars[5][0]] == 1


def test_sma_price_compares_close_with_its_own_average():
    bars = _bars([10, 10, 10, 30, 30])
    sched = mod.sma_price(bars, 3)
    # На баре 2 close 10 равен своей SMA3 → не выше, флет с бара 3.
    assert sched[bars[3][0]] == 0
    # На баре 3 close 30 выше SMA3 (≈16.7) → лонг с бара 4.
    assert sched[bars[4][0]] == 1


def test_donchian_holds_state_between_breakouts():
    # Канал 2/2: пробой вверх на баре 3, выход по минимуму на баре 5.
    bars = _bars([10, 10, 10, 20, 20, 5, 5])
    sched = mod.donchian(bars, 2, 2)
    assert sched[bars[4][0]] == 1   # вошли после пробоя
    assert sched[bars[5][0]] == 1   # держим, пока канал не пробит вниз
    assert sched[bars[6][0]] == 0   # 5 ниже минимума прошлых двух → выход


def test_tsmom_keeps_position_between_rebalances():
    """Смысл ребаланса: сигнал не пересчитывается каждый бар."""
    bars = _bars([10, 10, 10, 10, 20, 20, 5, 5, 5, 5])
    sched = mod.tsmom(bars, lookback=4, rebalance=4)
    # Пересмотр на баре 4: 20 > 10 → лонг с бара 5.
    assert sched[bars[5][0]] == 1
    # Бары 5-7 не пересмотр: цена рухнула до 5, а состояние держится.
    assert sched[bars[6][0]] == 1
    assert sched[bars[7][0]] == 1
    assert sched[bars[8][0]] == 1
    # Пересмотр на баре 8: 5 < 20 → выход с бара 9.
    assert sched[bars[9][0]] == 0


# ─── исполнение ─────────────────────────────────────────────────────────────


def test_hold_arm_counts_gap_between_bars():
    """Открытие следующего бара выше закрытия текущего — этот ход наш."""
    bars = _bars([100, 110], gap=[100, 110])
    sched = {bars[0][0]: 1, bars[1][0]: 1}
    r = mod.simulate(bars, sched, [], equity0=10_000.0, frac=1.0)
    qty = 100.0
    expect = 10_000.0 - 10_000 * FEE + qty * (110 - 100) - qty * 110 * FEE
    assert r["equity"] == pytest.approx(expect)
    assert r["legs"] == 2


def test_exit_happens_at_open_and_later_move_is_not_ours():
    bars = _bars([100, 90], gap=[100, 120])
    sched = {bars[0][0]: 1, bars[1][0]: 0}
    r = mod.simulate(bars, sched, [], equity0=10_000.0, frac=1.0)
    qty = 100.0
    expect = (10_000.0 - 10_000 * FEE - qty * 120 * FEE
              + qty * (120 - 100))
    assert r["equity"] == pytest.approx(expect)
    assert r["time_in"] == pytest.approx(0.5)


def test_funding_charged_only_while_position_open():
    bars = _bars([100, 100, 100])
    rate = 0.0001
    fnd = [(bars[i][0] + 1, rate) for i in range(3)]
    long_all = mod.simulate(bars, {b[0]: 1 for b in bars}, fnd,
                            equity0=10_000.0, frac=1.0)
    # Позиция открыта на всех трёх барах → три платежа.
    assert long_all["funding"] == pytest.approx(3 * rate * 100 * 100)
    flat = mod.simulate(bars, {b[0]: 0 for b in bars}, fnd,
                        equity0=10_000.0, frac=1.0)
    assert flat["funding"] == 0.0
    assert flat["legs"] == 0


def test_funding_buckets_into_the_bar_that_contains_it():
    bars = _bars([100, 100])
    per = mod._funding_per_bar(bars, [(bars[0][0], 0.001),
                                      (bars[1][0] + 5, 0.002),
                                      (bars[1][0] + 6, 0.003)])
    assert per == pytest.approx([0.001, 0.005])


# ─── сайзинг ────────────────────────────────────────────────────────────────


def test_fixed_notional_does_not_grow_with_the_account():
    bars = _bars([100, 200, 400])
    sched = {b[0]: 1 for b in bars}
    frac = mod.simulate(bars, sched, [], equity0=10_000.0,
                        sizing="frac", frac=0.15)
    fixed = mod.simulate(bars, sched, [], equity0=10_000.0,
                         sizing="notional", frac=0.15)
    # Вход один и тот же (счёт ещё не вырос), поэтому итог совпадает: разница
    # схем видна только при повторных входах.
    assert frac["equity"] == pytest.approx(fixed["equity"])
    # Второй эпизод: после выхода счёт другой, и размеры расходятся.
    bars2 = _bars([100, 200, 200, 400])
    sched2 = {bars2[0][0]: 1, bars2[1][0]: 0, bars2[2][0]: 1,
              bars2[3][0]: 1}
    a = mod.simulate(bars2, sched2, [], equity0=10_000.0,
                     sizing="frac", frac=0.15)
    b = mod.simulate(bars2, sched2, [], equity0=10_000.0,
                     sizing="notional", frac=0.15)
    assert a["equity"] > b["equity"]


def test_vol_sizing_shrinks_when_volatility_is_high():
    mod_vol = mod._realized_vol
    calm = [100.0 + 0.01 * i for i in range(400)]
    wild = [100.0 * (1.5 if i % 2 else 1.0) for i in range(400)]
    v_calm = mod_vol(calm, 399, 1.0)
    v_wild = mod_vol(wild, 399, 1.0)
    assert v_calm is not None and v_wild is not None
    assert v_wild > v_calm


def test_vol_sizing_respects_the_leverage_cap():
    """При исчезающей волатильности формула просит бесконечность — не даём."""
    bars = _bars([100.0 * (1.00001 ** (i % 4)) for i in range(200)])
    sched = {b[0]: 1 for b in bars}
    r = mod.simulate(bars, sched, [], equity0=10_000.0, sizing="vol",
                     frac=0.15, bars_per_day=1.0)
    cap = 10_000.0 * 0.15 * mod.VOL_MAX_MULT
    # Цена стоит на месте, поэтому нотионал входа и выхода почти совпадают, и
    # обе комиссии считаются от потолка: fees ≈ 2 × cap × taker.
    assert r["legs"] == 2
    assert r["fees"] == pytest.approx(2 * cap * FEE, rel=1e-4)


def test_vol_sizing_stays_flat_when_volatility_is_unmeasurable():
    """Ровная как стол цена: волу оценить нельзя, размер не назначаем."""
    bars = _bars([100.0] * 200)
    r = mod.simulate(bars, {b[0]: 1 for b in bars}, [], equity0=10_000.0,
                     sizing="vol", frac=0.15, bars_per_day=1.0)
    assert r["legs"] == 0
    assert r["equity"] == 10_000.0


# ─── разорение ──────────────────────────────────────────────────────────────


def test_price_crash_alone_cannot_ruin_an_unlevered_long():
    """Плечо 1: цена −99.6% съедает почти всё, но счёт остаётся выше нуля."""
    bars = _bars([100.0, 50.0, 1.0, 0.5, 0.4])
    r = mod.simulate(bars, {b[0]: 1 for b in bars}, [], equity0=10_000.0,
                     frac=1.0)
    assert 0.0 < r["equity"] < 100.0
    assert r["ruined"] is False
    assert min(eq for _, eq in r["curve"]) >= 0.0


def test_funding_alone_can_ruin_a_long_and_trading_then_stops():
    bars = _bars([100.0] * 10)
    fnd = [(b[0], 0.2) for b in bars]     # заведомо разорительная ставка
    r = mod.simulate(bars, {b[0]: 1 for b in bars}, fnd, equity0=10_000.0,
                     frac=1.0)
    assert r["ruined"] is True
    assert r["equity"] == 0.0
    # После разорения новых ног не появляется.
    assert r["legs"] == 1


def test_ruin_note_marks_which_arm_blew_up():
    assert "ХОЛД РАЗОРЁН" in mod._ruin_note({"ruined": False}, {"ruined": True})
    assert "ПРАВИЛО РАЗОРЕНО" in mod._ruin_note({"ruined": True},
                                                {"ruined": False})
    assert mod._ruin_note({"ruined": False}, {"ruined": False}) == ""


def test_daily_returns_skip_days_after_ruin():
    """День обнуления даёт −100%, дальше делить не на что — дней больше нет."""
    curve = [(0, 100.0), (D, 0.0), (2 * D, 0.0)]
    assert mod.daily_returns(curve) == [(1, -1.0)]


# ─── метрики ────────────────────────────────────────────────────────────────


def test_daily_returns_use_end_of_day_equity():
    curve = [(0, 100.0), (D // 2, 110.0), (D, 121.0), (2 * D, 121.0)]
    rets = mod.daily_returns(curve)
    assert [round(x, 6) for _, x in rets] == [0.1, 0.0]


def test_max_drawdown_measured_from_the_peak():
    curve = [(0, 100.0), (D, 200.0), (2 * D, 150.0), (3 * D, 180.0)]
    assert mod._max_dd(curve) == pytest.approx(-25.0)


def test_sharpe_is_zero_without_variation():
    assert mod._sharpe([0.01, 0.01, 0.01]) == 0.0
    assert mod._sharpe([0.01]) == 0.0


def test_compare_splits_edge_into_two_halves():
    rule = {"equity0": 100.0, "equity": 130.0,
            "curve": [(0, 100.0), (D, 110.0), (2 * D, 120.0),
                      (3 * D, 130.0), (4 * D, 130.0)]}
    hold = {"equity0": 100.0, "equity": 100.0,
            "curve": [(0, 100.0), (D, 100.0), (2 * D, 100.0),
                      (3 * D, 100.0), (4 * D, 100.0)]}
    rule["time_in"], rule["legs"] = 1.0, 2
    rule["fees"], rule["funding"] = 0.0, 0.0
    c = mod.compare(rule, hold)
    assert c["edge_bp"] > 0
    assert c["is_edge"] > 0 and c["oos_edge"] >= 0
    assert c["n_days"] == 4


def test_two_sided_p_matches_known_normal_values():
    assert mod._norm_p(0.0) == pytest.approx(1.0)
    assert mod._norm_p(1.96) == pytest.approx(0.05, abs=0.001)
    assert mod._norm_p(2.576) == pytest.approx(0.01, abs=0.001)


def test_hold_schedule_starts_after_the_same_warmup():
    bars = _bars(list(range(100, 110)))
    sched = mod.hold_schedule(bars, 4)
    assert bars[3][0] not in sched
    assert sched[bars[4][0]] == 1
    assert len(sched) == 6
