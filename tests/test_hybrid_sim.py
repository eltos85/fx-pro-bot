"""Арифметика симулятора контура (scripts/hybrid_contour_sim.py).

Проверяется учёт, а не торговая логика: если сайзинг, комиссии, funding или
перезаход посчитаны неверно, шаг 1 плана STRATEGY_HYBRID.md даст неправильный
вывод про гейт §8.1, и ошибку не поймать глазами по сводке.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "hybrid_contour_sim.py")
_spec = importlib.util.spec_from_file_location("hybrid_contour_sim", _PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover
    pytest.skip("симулятор контура не найден", allow_module_level=True)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

H = mod.HOUR_MS
T = mod.TAKER


def _bars(prices: list[float], start: int = 0) -> list[tuple]:
    """Бары с open == close: цена внутри бара не блуждает, учёт проверяем."""
    return [(start + i * H, p, p, p, p) for i, p in enumerate(prices)]


class _FakeTrigger:
    """Фиксация в заданных индексах — чтобы проверять учёт, а не случайность."""

    def __init__(self, at: set[int]):
        self.at = at

    def on_entry(self, price: float) -> None:
        pass

    def fires(self, bars: list[tuple], i: int) -> bool:
        return i in self.at


def test_hold_arm_matches_hand_calculation():
    bars = _bars([100.0] * 3 + [110.0] * 7)
    sched = {bars[2][0]: 1, bars[8][0]: 0}
    r = mod.simulate(bars, sched, [], equity=1000.0, frac=0.1,
                     trigger=None, flat_hours=0.0)
    qty = 1000.0 * 0.1 / 100.0
    assert r["legs"] == 2
    assert r["gross"] == pytest.approx((110.0 - 100.0) * qty)
    assert r["fees"] == pytest.approx(qty * 100.0 * T + qty * 110.0 * T)
    assert r["funding"] == 0.0
    assert r["net"] == pytest.approx(r["gross"] - r["fees"])


def test_core_signal_executes_on_next_bar_open():
    """Сигнал на close бара i — сделка на open бара i+1 (протокол research)."""
    bars4h = _bars([100.0 + i for i in range(60)])
    sched = mod.core_schedule(bars4h)
    first_ts = min(sched)
    # SMA20 и SMA50 определены впервые на close бара 49 → исполнение на 50-м.
    assert first_ts == bars4h[50][0]
    assert sched[first_ts] == 1


def test_immediate_reentry_costs_exactly_two_extra_legs():
    """Перезаход по цене фиксации не меняет экспозицию — только ноги.

    Это и есть причина отставания контура, замеренного live (§5 канона):
    цена одна, значит gross нулевой у обеих ветвей, а разница = комиссии.
    """
    bars = _bars([100.0] * 12)
    sched = {bars[1][0]: 1, bars[11][0]: 0}
    hold = mod.simulate(bars, sched, [], equity=1000.0, frac=0.1,
                        trigger=None, flat_hours=0.0)
    cont = mod.simulate(bars, sched, [], equity=1000.0, frac=0.1,
                        trigger=_FakeTrigger({4, 7}), flat_hours=0.0)
    qty = 1000.0 * 0.1 / 100.0
    assert cont["fixations"] == 2
    assert cont["legs"] == hold["legs"] + 4
    assert cont["gross"] == pytest.approx(hold["gross"])
    assert cont["net"] - hold["net"] == pytest.approx(-4 * qty * 100.0 * T)


def test_sizing_drag_shrinks_lot_after_reentry_at_higher_price():
    """Лот = frac*equity/цена: перезаход дороже → лот меньше → недобор хода.

    Вход 100 (лот 1.0), фиксация на 200, перезаход на 200 (лот 0.5), выход 300.
    Контур берёт 100 + 50 = 150 gross против 200 у холда: −50 это просадка лота,
    а не издержки.
    """
    bars = _bars([100.0, 100.0, 200.0, 200.0, 300.0, 300.0])
    sched = {bars[1][0]: 1, bars[5][0]: 0}
    hold = mod.simulate(bars, sched, [], equity=1000.0, frac=0.1,
                        trigger=None, flat_hours=0.0)
    cont = mod.simulate(bars, sched, [], equity=1000.0, frac=0.1,
                        trigger=_FakeTrigger({2}), flat_hours=0.0)
    assert hold["gross"] == pytest.approx(200.0)
    assert cont["gross"] == pytest.approx(150.0)


def test_flat_pause_blocks_reentry_and_skips_funding():
    """Пауза вне рынка — единственный способ контура изменить экспозицию.

    Фиксация на баре 2, пауза 3ч: перезаход не раньше бара 5, а funding,
    начисленный на баре 3, контур не платит.
    """
    bars = _bars([100.0] * 10)
    sched = {bars[1][0]: 1, bars[9][0]: 0}
    funding = [(bars[3][0], 0.0001)]
    hold = mod.simulate(bars, sched, funding, equity=1000.0, frac=0.1,
                        trigger=None, flat_hours=0.0)
    cont = mod.simulate(bars, sched, funding, equity=1000.0, frac=0.1,
                        trigger=_FakeTrigger({2}), flat_hours=3.0)
    qty = 1000.0 * 0.1 / 100.0
    assert hold["funding"] == pytest.approx(0.0001 * qty * 100.0)
    assert cont["funding"] == 0.0
    assert cont["legs"] == hold["legs"] + 2


def test_pct_trail_fires_only_after_drawdown_from_peak():
    bars = _bars([100.0, 105.0, 103.0, 102.5])
    trig = mod.Trigger("pct_trail", 2.0)
    trig.on_entry(100.0)
    assert trig.fires(bars, 0) is False
    assert trig.fires(bars, 1) is False
    # Пик 105 → порог 102.9 (ровно на границе не проверяем: в double это
    # 102.8999…, и тик, равный порогу, там неопределён).
    assert trig.fires(bars, 2) is False
    assert trig.fires(bars, 3) is True


def test_random_trigger_rate_is_per_day_not_per_bar():
    """λ задаётся в сутки: на часовом баре вероятность λ/24."""
    trig = mod.Trigger("random", 24.0, seed=1)
    bars = _bars([100.0] * 50)
    assert all(trig.fires(bars, i) for i in range(50))
    rare = mod.Trigger("random", 0.0, seed=1)
    assert not any(rare.fires(bars, i) for i in range(50))


def test_regime_is_classified_ex_ante():
    """Режим берётся из прошлого эпизода, иначе это подглядывание в исход."""
    bars = _bars([100.0] * (30 * 24) + [200.0] * 5)
    ts = bars[30 * 24][0]
    assert mod.regime_of(bars, ts) == "тренд вверх"
    assert mod.regime_of(bars, bars[10][0]) == "n/a"


def test_episode_delta_pairs_arms_by_episode_start():
    bars = _bars([100.0] * 12)
    sched = {bars[1][0]: 1, bars[11][0]: 0}
    hold = mod.simulate(bars, sched, [], equity=1000.0, frac=0.1,
                        trigger=None, flat_hours=0.0)
    cont = mod.simulate(bars, sched, [], equity=1000.0, frac=0.1,
                        trigger=_FakeTrigger({5}), flat_hours=0.0)
    buckets = mod.episode_delta(hold, cont, bars)
    assert sum(b["n"] for b in buckets.values()) == 1
    assert sum(b["fix"] for b in buckets.values()) == 1
    assert sum(sum(b["deltas"]) for b in buckets.values()) < 0
