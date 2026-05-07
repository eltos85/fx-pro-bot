"""Тесты positioning-фич: Open Interest delta + funding rate dynamics."""
from __future__ import annotations

import pytest

from ai_trader.analysis.positioning import (
    PositioningSnapshot,
    build_positioning_snapshot,
    format_positioning,
)
from ai_trader.trading.client import FundingPoint, OpenInterestPoint


def _oi(values: list[float], step_ms: int = 60 * 60 * 1000) -> list[OpenInterestPoint]:
    """Helper: список OI-точек с возрастающими ts (хочется явно тестить порядок)."""
    base_ts = 1_700_000_000_000
    return [OpenInterestPoint(ts=base_ts + i * step_ms, value=v) for i, v in enumerate(values)]


def _funding(rates: list[float], step_ms: int = 8 * 60 * 60 * 1000) -> list[FundingPoint]:
    base_ts = 1_700_000_000_000
    return [FundingPoint(ts=base_ts + i * step_ms, rate=r) for i, r in enumerate(rates)]


class TestBuildPositioning:
    """Чистые формулы: oi_delta_4h_pct, oi_delta_24h_pct, funding-агрегаты."""

    def test_empty_inputs_all_none(self):
        s = build_positioning_snapshot(oi_history=None, funding_history=None)
        assert s.oi_now is None
        assert s.oi_delta_4h_pct is None
        assert s.oi_delta_24h_pct is None
        assert s.funding_24h_cumulative is None
        assert s.funding_24h_mean is None
        assert s.funding_7d_mean is None
        assert s.funding_prev_period is None

    def test_oi_short_history_no_deltas(self):
        # 3 точки — не хватит ни на 4h (нужно 5), ни на 24h (нужно 25).
        s = build_positioning_snapshot(
            oi_history=_oi([100.0, 105.0, 110.0]),
            funding_history=None,
        )
        assert s.oi_now == 110.0
        assert s.oi_4h_ago is None
        assert s.oi_24h_ago is None
        assert s.oi_delta_4h_pct is None
        assert s.oi_delta_24h_pct is None

    def test_oi_4h_delta_known_value(self):
        # 5 точек: индекс -5 = первая точка, current = последняя.
        # Цены: 100, 102, 104, 105, 110 → 4h-ago = 100, now = 110, delta = +10%.
        s = build_positioning_snapshot(
            oi_history=_oi([100.0, 102.0, 104.0, 105.0, 110.0]),
            funding_history=None,
        )
        assert s.oi_now == 110.0
        assert s.oi_4h_ago == 100.0
        assert s.oi_delta_4h_pct == pytest.approx(10.0)

    def test_oi_24h_delta_known_value(self):
        # 25 точек: -25 = первая, текущая = последняя.
        # Линейный рост 100..148 (шаг 2): первая=100, последняя=148, delta=+48%
        values = [100.0 + i * 2 for i in range(25)]
        s = build_positioning_snapshot(
            oi_history=_oi(values),
            funding_history=None,
        )
        assert s.oi_now == 148.0
        assert s.oi_24h_ago == 100.0
        assert s.oi_delta_24h_pct == pytest.approx(48.0)
        # 4h-ago = индекс -5 = 100 + 20*2 = 140 → delta_4h = (148-140)/140 *100 ≈ 5.71%
        assert s.oi_4h_ago == 140.0
        assert s.oi_delta_4h_pct == pytest.approx(5.7142857, rel=1e-5)

    def test_oi_zero_anchor_returns_none(self):
        # Нулевой OI 4h назад → деление на ноль → None
        values = [0.0] + [100.0] * 4
        s = build_positioning_snapshot(
            oi_history=_oi(values),
            funding_history=None,
        )
        assert s.oi_4h_ago == 0.0
        assert s.oi_delta_4h_pct is None  # нельзя посчитать %

    def test_funding_24h_cumulative_3_events(self):
        # 5 событий funding, последние 3 = [0.0001, 0.0002, 0.0003], сумма 0.0006
        s = build_positioning_snapshot(
            oi_history=None,
            funding_history=_funding([
                0.0005, 0.0004, 0.0001, 0.0002, 0.0003,
            ]),
        )
        assert s.funding_24h_cumulative == pytest.approx(0.0006)
        assert s.funding_24h_mean == pytest.approx(0.0002)
        assert s.funding_prev_period == pytest.approx(0.0002)
        # 7d mean берёт все 5 точек (если их меньше 21)
        assert s.funding_7d_mean == pytest.approx(0.0003)

    def test_funding_only_one_event(self):
        # 1 событие → 24h cum это 1 событие, prev_period None
        s = build_positioning_snapshot(
            oi_history=None,
            funding_history=_funding([0.0010]),
        )
        # last3 = последняя точка (1 элемент) — sum = 0.001
        assert s.funding_24h_cumulative == pytest.approx(0.0010)
        assert s.funding_prev_period is None

    def test_funding_now_passthrough(self):
        s = build_positioning_snapshot(
            oi_history=None,
            funding_history=None,
            funding_now=0.000123,
        )
        assert s.funding_now == 0.000123


class TestFormatPositioning:
    """Текстовый вывод для system-prompt: метки и обработка None."""

    def test_format_with_full_data_shows_all_labels(self):
        s = PositioningSnapshot(
            oi_now=1_234_567.0,
            oi_4h_ago=1_200_000.0,
            oi_24h_ago=1_000_000.0,
            oi_delta_4h_pct=2.881,
            oi_delta_24h_pct=23.46,
            funding_now=0.0006,           # +0.06% per 8h → mild long bias
            funding_24h_cumulative=0.0018,
            funding_24h_mean=0.0006,
            funding_7d_mean=0.0003,
            funding_prev_period=0.0005,
        )
        out = format_positioning(s)
        # OI: моментальные значения и метки
        assert "OI:" in out
        assert "Δ4h=" in out
        assert "Δ24h=" in out
        assert "[moderate]" in out          # +2.88% попадает в 2-5%
        assert "[EXTREME buildup]" in out   # +23.46% > 15%
        # Funding: % и метка mild long bias
        assert "Funding:" in out
        assert "mild long bias" in out

    def test_format_funding_strong_short_bias(self):
        s = PositioningSnapshot(
            oi_now=None, oi_4h_ago=None, oi_24h_ago=None,
            oi_delta_4h_pct=None, oi_delta_24h_pct=None,
            funding_now=-0.0030,             # -0.30% > 0.20% → STRONG short
            funding_24h_cumulative=-0.0090,
            funding_24h_mean=-0.0030,
            funding_7d_mean=-0.0020,
            funding_prev_period=-0.0025,
        )
        out = format_positioning(s)
        assert "STRONG short bias" in out

    def test_format_with_all_none_no_crash(self):
        s = PositioningSnapshot(
            oi_now=None, oi_4h_ago=None, oi_24h_ago=None,
            oi_delta_4h_pct=None, oi_delta_24h_pct=None,
            funding_now=None,
            funding_24h_cumulative=None,
            funding_24h_mean=None,
            funding_7d_mean=None,
            funding_prev_period=None,
        )
        out = format_positioning(s)
        assert "n/a" in out
        assert "OI:" in out and "Funding:" in out

    def test_format_oi_unwind_label(self):
        s = PositioningSnapshot(
            oi_now=900_000.0, oi_4h_ago=1_000_000.0, oi_24h_ago=1_200_000.0,
            oi_delta_4h_pct=-10.0, oi_delta_24h_pct=-25.0,
            funding_now=None,
            funding_24h_cumulative=None,
            funding_24h_mean=None,
            funding_7d_mean=None,
            funding_prev_period=None,
        )
        out = format_positioning(s)
        # -10% попадает в strong (≥10%), -25% в EXTREME
        assert "strong unwind" in out
        assert "EXTREME unwind" in out
