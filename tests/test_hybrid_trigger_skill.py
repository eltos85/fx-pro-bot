"""Счётчик угадывания триггера (scripts/hybrid_trigger_skill.py).

Главное здесь — проверка самого измерителя на заведомо известных случаях:
если он не отличает закрытие на пике от закрытия на дне, его показания по
живым сделкам ничего не значат.
"""

from __future__ import annotations

import importlib.util
import math
import os
import random

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "hybrid_trigger_skill.py")
_spec = importlib.util.spec_from_file_location("hybrid_trigger_skill", _PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover
    pytest.skip("счётчик триггера не найден", allow_module_level=True)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

H = mod.HOUR_MS


def _wave(n_hours: int = 400, period: int = 24) -> dict[int, float]:
    """Пила с периодом 24ч: пики на 0, 24, 48…, дно на 12, 36…"""
    return {i * H: 100.0 + 10.0 * math.cos(2 * math.pi * i / period)
            for i in range(n_hours)}


def _fill(ts, side, qty, px, *, link="", stop="", etype="Trade"):
    return {"execTime": str(ts), "side": side, "execQty": str(qty),
            "execPrice": str(px), "orderLinkId": link, "stopOrderType": stop,
            "execType": etype}


def test_fixation_is_only_a_close_made_by_someone_else():
    """Выход самого ядра — не фиксация: это его решение, а не внешний триггер."""
    fills = [
        _fill(1_000, "Buy", 3, 100, link="swing_a"),
        _fill(2_000, "Sell", 3, 110, link="scalp_x"),      # фиксация скальпа
        _fill(3_000, "Buy", 3, 110, link="swing_b"),
        _fill(4_000, "Sell", 3, 120, stop="StopLoss"),     # фиксация брекетом
        _fill(5_000, "Buy", 3, 120, link="swing_c"),
        _fill(6_000, "Sell", 3, 130, link="swing_c"),      # выход ядра
    ]
    fx = mod.find_fixations(fills)
    assert [f["actor"] for f in fx] == ["tactic", "bracket"]
    assert [f["price"] for f in fx] == [110.0, 120.0]


def test_fixation_ignores_funding_rows_and_flat_book():
    """Funding-запись несёт размер позиции в execQty — в фиксации не попадает."""
    fills = [
        _fill(1_000, "Buy", 2, 100, link="swing_a"),
        _fill(1_500, "Sell", 2, 100, etype="Funding"),
        _fill(2_000, "Sell", 2, 105, link="scalp_x"),
        _fill(3_000, "Sell", 1, 106, link="scalp_y"),  # книга уже пуста
    ]
    fx = mod.find_fixations(fills)
    assert len(fx) == 1
    assert fx[0]["qty"] == pytest.approx(2.0)


def test_forward_move_uses_the_hour_the_event_fell_into():
    bars = {0: 100.0, H: 110.0, 2 * H: 90.0}
    assert mod.forward_move(bars, 30 * 60 * 1000, 1) == pytest.approx(0.10)
    assert mod.forward_move(bars, 0, 2) == pytest.approx(-0.10)
    assert mod.forward_move(bars, 0, 5) is None


def test_close_at_peak_scores_high_and_at_bottom_scores_low():
    """Проверка измерителя на заведомых случаях.

    Закрытие ровно на пике: через 12ч цена на дне, то есть лучше почти любого
    соседнего часа. На дне — наоборот.
    """
    bars = _wave()
    peak = mod.rank_vs_neighbours(bars, 96 * H, 12)
    bottom = mod.rank_vs_neighbours(bars, 108 * H, 12)
    assert peak is not None and bottom is not None
    assert peak[0] > 0.95
    assert bottom[0] < 0.05


def test_random_moments_score_around_a_half():
    """Случайные моменты не должны выглядеть угадыванием."""
    bars = _wave(n_hours=600)
    rng = random.Random(7)
    shares = []
    for _ in range(60):
        ts = rng.randrange(60, 500) * H
        r = mod.rank_vs_neighbours(bars, ts, 12)
        if r:
            shares.append(r[0])
    assert 0.4 < sum(shares) / len(shares) < 0.6


def test_sign_test_matches_hand_computed_binomial():
    assert mod.sign_test_p(5, 5) == pytest.approx(2 * 1 / 32)
    assert mod.sign_test_p(3, 5) == pytest.approx(1.0)
    assert mod.sign_test_p(0, 0) == 1.0


def test_sign_test_flags_skew_toward_bad_moments():
    """Перекос вниз так же значим, как перекос вверх: берётся меньший хвост."""
    assert mod.sign_test_p(0, 5) == pytest.approx(2 * 1 / 32)
    assert mod.sign_test_p(1, 12) == pytest.approx(2 * 13 / 4096)
    assert mod.sign_test_p(1, 12) == mod.sign_test_p(11, 12)


def test_clustering_counts_closes_standing_too_near():
    """Закрытия внутри 12ч смотрят на одно движение — считаем такие пары."""
    rows = [{"ts": 0}, {"ts": 2 * H}, {"ts": 40 * H}, {"ts": 41 * H}]
    assert mod.clustering(rows) == (2, 4)
    assert mod.clustering([{"ts": 0}, {"ts": 40 * H}]) == (0, 2)
    assert mod.clustering([]) == (0, 0)


def test_summary_counts_money_saved_by_waiting():
    """Цена после закрытия упала → пауза сберегла деньги (плюс)."""
    bars = _wave()
    fx = [{"ts": 96 * H, "price": 110.0, "qty": 2.0, "actor": "bracket",
           "detail": "StopLoss"}]
    rows = []
    for f in fx:
        moves = {h: mod.forward_move(bars, f["ts"], h) for h in mod.HORIZONS_H}
        rows.append({
            **f,
            "moves": moves,
            "ranks": {h: mod.rank_vs_neighbours(bars, f["ts"], h)
                      for h in mod.HORIZONS_H},
            "saved": {h: (None if moves[h] is None
                          else -moves[h] * f["price"] * f["qty"])
                      for h in mod.HORIZONS_H},
        })
    s = mod.summarize(rows, 12)
    assert s is not None
    assert s["n"] == 1
    assert s["mean_move"] < 0
    assert s["saved"] > 0
