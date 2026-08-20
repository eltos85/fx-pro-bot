"""Арифметика выбора порога закрытия (STRATEGY_HYBRID.md §17.5).

Проверяется то, что определяет цифры в отчёте: срабатывание порога по
максимуму свечи, размер денег в закрытии, обратный вход по цене закрытия,
комиссии по числу ног и закрытие по трендовому правилу. Сети не требует.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / \
    "hybrid_fix_threshold.py"
_spec = importlib.util.spec_from_file_location("hybrid_fix_threshold", _SRC)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

H4 = 4 * 3_600_000
FEE = mod.TAKER


def _bars(rows):
    """rows: (open, high, low, close) по 4-часовым свечам."""
    return [(i * H4, o, h, l, c) for i, (o, h, l, c) in enumerate(rows)]


def test_threshold_fires_on_the_high_not_on_the_close():
    """Заявка стоит в стакане, значит важен максимум свечи, а не закрытие."""
    bars = _bars([(100, 100, 100, 100), (100, 102, 99, 99)])
    states = {bars[0][0]: 1, bars[1][0]: 1}
    r = mod.run(bars, states, threshold_pct=1.0, notional=1000.0)
    assert len(r["events"]) == 1
    assert r["events"][0]["price"] == pytest.approx(101.0)


def test_threshold_not_reached_leaves_no_event():
    bars = _bars([(100, 100, 100, 100), (100, 100.5, 99, 100)])
    states = {b[0]: 1 for b in bars}
    r = mod.run(bars, states, threshold_pct=1.0, notional=1000.0)
    assert r["events"] == []


def test_money_in_one_event_matches_hand_arithmetic():
    bars = _bars([(100, 100, 100, 100), (100, 110, 99, 105)])
    states = {b[0]: 1 for b in bars}
    r = mod.run(bars, states, threshold_pct=5.0, notional=1000.0)
    ev = r["events"][0]
    qty = 10.0                      # 1000 / 100
    assert ev["qty"] == pytest.approx(qty)
    assert ev["gross"] == pytest.approx((105.0 - 100.0) * qty)


def test_threshold_can_fire_several_times_inside_one_candle():
    """Свеча выросла на 10%, порог 1% — уровень достигался много раз."""
    bars = _bars([(100, 100, 100, 100), (100, 110, 99, 110)])
    states = {b[0]: 1 for b in bars}
    r = mod.run(bars, states, threshold_pct=1.0, notional=1000.0)
    # 100 × 1.01^n ≤ 110 держится до n = 9 (1.01^9 ≈ 1.0937).
    assert len(r["events"]) == 9
    assert r["events"][0]["price"] == pytest.approx(101.0)
    assert r["events"][-1]["price"] == pytest.approx(100 * 1.01 ** 9)


def test_reentry_starts_from_the_closing_price():
    """После закрытия средняя цена — это цена закрытия, а не старый вход."""
    bars = _bars([(100, 100, 100, 100), (100, 110, 99, 105),
                  (105, 116, 104, 116)])
    states = {b[0]: 1 for b in bars}
    r = mod.run(bars, states, threshold_pct=5.0, notional=1000.0)
    # Третья свеча (максимум 116) успевает достать два уровня подряд.
    assert len(r["events"]) == 3
    assert r["events"][1]["avg"] == pytest.approx(105.0)
    assert r["events"][1]["price"] == pytest.approx(105.0 * 1.05)
    assert r["events"][2]["avg"] == pytest.approx(105.0 * 1.05)


def test_two_fees_per_cycle():
    bars = _bars([(100, 100, 100, 100), (100, 110, 99, 105)])
    states = {b[0]: 1 for b in bars}
    r = mod.run(bars, states, threshold_pct=5.0, notional=1000.0)
    # Вход + закрытие по порогу + обратный вход + закрытие в конце истории.
    assert r["fees"] == pytest.approx(
        1000.0 * FEE                      # первый вход
        + 10.0 * 105.0 * FEE              # закрытие по порогу
        + 1000.0 * FEE                    # обратный вход
        + (1000.0 / 105.0) * 105.0 * FEE  # закрытие в конце истории
    )


def test_rule_exit_closes_at_next_open_and_is_counted_separately():
    bars = _bars([(100, 100, 100, 100), (90, 91, 89, 90)])
    states = {bars[0][0]: 1, bars[1][0]: 0}
    r = mod.run(bars, states, threshold_pct=5.0, notional=1000.0)
    assert r["events"] == []
    assert len(r["rule_exits"]) == 1
    assert r["rule_exits"][0]["price"] == pytest.approx(90.0)
    assert r["rule_exits"][0]["gross"] == pytest.approx((90.0 - 100.0) * 10.0)


def test_no_position_without_buy_signal():
    bars = _bars([(100, 200, 50, 100)] * 3)
    r = mod.run(bars, {b[0]: 0 for b in bars}, threshold_pct=1.0,
                notional=1000.0)
    assert r["events"] == [] and r["rule_exits"] == []
    assert r["fees"] == 0.0


def test_core_states_decide_on_close_and_apply_next_bar():
    prices = [10.0] * 60 + [50.0] * 5
    bars = _bars([(p, p, p, p) for p in prices])
    states = mod.core_states(bars)
    assert bars[mod.CORE_SLOW - 1][0] not in states
    # Пока цены ровные, быстрая средняя не выше медленной → не покупаем.
    assert states[bars[mod.CORE_SLOW][0]] == 0
    # После скачка быстрая уходит выше — покупаем.
    assert states[bars[-1][0]] == 1


def test_net_is_gross_minus_fees():
    bars = _bars([(100, 100, 100, 100), (100, 110, 99, 105),
                  (105, 116, 104, 116)])
    states = {b[0]: 1 for b in bars}
    r = mod.run(bars, states, threshold_pct=5.0, notional=1000.0)
    assert r["net"] == pytest.approx(r["gross"] - r["fees"])
