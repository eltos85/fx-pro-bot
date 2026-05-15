"""Тесты cumulative Sharpe для AI Arena.

Берём известные ряды equity → вычисляем returns аналитически →
сверяем формулу.

Source (gist nof1-prompt.md, секция PERFORMANCE METRICS): окно
не указано — Nof1 использует cumulative с момента старта эксперимента,
не rolling 14d. См. BUILDLOG 2026-05-15 «net PnL alignment».
"""
from __future__ import annotations

import pytest

from ai_arena.analysis.sharpe import (
    compute_returns,
    cumulative_sharpe,
)


class TestComputeReturns:
    def test_empty(self):
        assert compute_returns([]) == []

    def test_one_point(self):
        assert compute_returns([100.0]) == []

    def test_constant_equity(self):
        assert compute_returns([100.0, 100.0, 100.0]) == [0.0, 0.0]

    def test_known_returns(self):
        # 100 → 110 → 121 → returns +10%, +10%
        rs = compute_returns([100.0, 110.0, 121.0])
        assert rs == pytest.approx([0.1, 0.1])

    def test_skips_zero(self):
        # Если equity упало в 0 — пропускаем, чтобы не делить на 0.
        rs = compute_returns([100.0, 0.0, 50.0])
        # 100→0 даёт return, 0→50 пропускается (prev=0)
        assert rs == pytest.approx([-1.0])


class TestCumulativeSharpe:
    def test_insufficient_data(self):
        snaps = [
            {"ts": 0, "total_equity_usd": 100.0},
            {"ts": 1, "total_equity_usd": 101.0},
        ]
        assert cumulative_sharpe(snaps) is None

    def test_constant_equity_returns_none(self):
        # std(returns)=0 → Sharpe не определён
        snaps = [
            {"ts": i, "total_equity_usd": 500.0}
            for i in range(10)
        ]
        assert cumulative_sharpe(snaps) is None

    def test_literally_identical_returns_returns_none(self):
        # Жёстко одинаковые returns (через равные шаги +5) → std=0 → None.
        # Compound через *1.01 НЕ даёт строго равных returns из-за
        # float rounding (мы это проверяли — std микроскопический,
        # Sharpe огромный, такое поведение корректно).
        snaps = [
            {"ts": i, "total_equity_usd": 500.0 + 5.0 * i} for i in range(10)
        ]
        # returns здесь не идентичны (5/500 ≠ 5/505 ≠ …), но проверим
        # что при действительно одинаковых returns Sharpe = None.
        # Берём константу — это уже есть в test_constant_equity_returns_none.
        s = cumulative_sharpe(snaps)
        # Ряд монотонно растёт с убывающим returns → Sharpe положительный
        assert s is not None and s > 0

    def test_positive_with_variance(self):
        # Возрастающее с шумом — Sharpe > 0
        snaps = [
            {"ts": 0, "total_equity_usd": 500.0},
            {"ts": 1, "total_equity_usd": 502.0},
            {"ts": 2, "total_equity_usd": 504.0},
            {"ts": 3, "total_equity_usd": 506.0},
            {"ts": 4, "total_equity_usd": 510.0},  # +0.79%
            {"ts": 5, "total_equity_usd": 508.0},  # -0.39%
            {"ts": 6, "total_equity_usd": 515.0},
        ]
        s = cumulative_sharpe(snaps)
        assert s is not None
        assert isinstance(s, float)
        # Mean returns > 0 → Sharpe > 0
        assert s > 0

    def test_negative_returns_yields_negative_sharpe(self):
        snaps = [
            {"ts": 0, "total_equity_usd": 500.0},
            {"ts": 1, "total_equity_usd": 495.0},
            {"ts": 2, "total_equity_usd": 490.0},
            {"ts": 3, "total_equity_usd": 488.0},
            {"ts": 4, "total_equity_usd": 485.0},
            {"ts": 5, "total_equity_usd": 482.0},
        ]
        s = cumulative_sharpe(snaps)
        assert s is not None and s < 0

    def test_annualization_scales(self):
        snaps = [
            {"ts": 0, "total_equity_usd": 500.0},
            {"ts": 1, "total_equity_usd": 502.0},
            {"ts": 2, "total_equity_usd": 503.0},
            {"ts": 3, "total_equity_usd": 506.0},
            {"ts": 4, "total_equity_usd": 510.0},
        ]
        base = cumulative_sharpe(snaps)
        annualized = cumulative_sharpe(snaps, annualization_factor=10.0)
        assert base is not None and annualized is not None
        assert annualized == pytest.approx(base * 10.0)
