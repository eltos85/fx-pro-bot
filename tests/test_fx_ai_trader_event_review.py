"""Phase 2 (2026-05-29): event-driven locked-profit датчик.

Контекст (BUILDLOG_AI_FX_TRADER.md 2026-05-29): поверх живого spot-стрима
(Phase 1) датчик будит внеплановый review когда позиция входит в зону
≥ threshold_r — ловит спайк в прибыль, который иначе откатился бы внутри
планового 5-мин окна. НЕ меняет exit-правила (решает LLM-guardian Phase 0).

Тесты:
- ``compute_unrealised_r``: BUY/SELL, None при отсутствии SL/цены, вырожд. риск.
- ``LockedProfitSensor``: rising-edge (один fire на вход в зону),
  re-arm с гистерезисом, cooldown, rate-cap, prune закрытых позиций.
"""

from __future__ import annotations

import pytest

from fx_ai_trader.trading.price_sensor import (
    LockedProfitSensor,
    compute_unrealised_r,
)


class TestComputeUnrealisedR:
    def test_buy_profit(self):
        # entry 100, SL 90 → risk 10. price 115 → +1.5R
        assert compute_unrealised_r("BUY", 100.0, 90.0, 115.0) == pytest.approx(1.5)

    def test_buy_loss(self):
        assert compute_unrealised_r("BUY", 100.0, 90.0, 95.0) == pytest.approx(-0.5)

    def test_sell_profit(self):
        # entry 100, SL 110 → risk 10. price 85 → +1.5R (short)
        assert compute_unrealised_r("SELL", 100.0, 110.0, 85.0) == pytest.approx(1.5)

    def test_sell_loss(self):
        assert compute_unrealised_r("SELL", 100.0, 110.0, 105.0) == pytest.approx(-0.5)

    def test_none_when_no_price(self):
        assert compute_unrealised_r("BUY", 100.0, 90.0, None) is None

    def test_none_when_no_sl(self):
        assert compute_unrealised_r("BUY", 100.0, None, 115.0) is None

    def test_none_when_degenerate_risk(self):
        assert compute_unrealised_r("BUY", 100.0, 100.0, 115.0) is None

    def test_none_when_bad_entry(self):
        assert compute_unrealised_r("BUY", 0.0, -5.0, 115.0) is None


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _sensor(clock: FakeClock, **kw) -> LockedProfitSensor:
    params = dict(
        threshold_r=1.5, hysteresis_r=0.3, cooldown_sec=120.0,
        max_events_per_hour=6, now=clock,
    )
    params.update(kw)
    return LockedProfitSensor(**params)


class TestRisingEdge:
    def test_fires_once_on_entering_zone(self):
        clk = FakeClock()
        s = _sensor(clk)
        # ниже зоны → no fire
        assert not s.evaluate([(1, 1.2)]).fire
        # вошли в зону → fire
        d = s.evaluate([(1, 1.6)])
        assert d.fire
        assert d.positions == [(1, 1.6)]
        # всё ещё в зоне, но уже disarmed → no re-fire
        clk.advance(200)  # cooldown прошёл
        assert not s.evaluate([(1, 1.7)]).fire

    def test_rearm_after_dropping_below_hysteresis(self):
        clk = FakeClock()
        s = _sensor(clk)
        assert s.evaluate([(1, 1.6)]).fire
        clk.advance(200)
        # упали ниже (threshold - hysteresis) = 1.2 → re-arm
        assert not s.evaluate([(1, 1.0)]).fire
        clk.advance(200)
        # снова вошли в зону → fire опять
        assert s.evaluate([(1, 1.6)]).fire

    def test_no_rearm_within_hysteresis_band(self):
        clk = FakeClock()
        s = _sensor(clk)
        assert s.evaluate([(1, 1.6)]).fire
        clk.advance(200)
        # 1.3 в полосе (1.2, 1.5) → НЕ перевзводим
        assert not s.evaluate([(1, 1.3)]).fire
        clk.advance(200)
        assert not s.evaluate([(1, 1.6)]).fire  # всё ещё disarmed


class TestCooldown:
    def test_cooldown_blocks_second_position(self):
        clk = FakeClock()
        s = _sensor(clk, cooldown_sec=120.0)
        assert s.evaluate([(1, 1.6)]).fire
        # другая позиция входит в зону через 30с — cooldown не прошёл
        clk.advance(30)
        d = s.evaluate([(1, 1.6), (2, 1.6)])
        assert not d.fire
        assert d.throttled
        # позиция 2 осталась armed → стреляет после cooldown
        clk.advance(100)  # суммарно 130 > 120
        d2 = s.evaluate([(1, 1.6), (2, 1.6)])
        assert d2.fire
        assert d2.positions == [(2, 1.6)]  # поз.1 уже disarmed


class TestRateCap:
    def test_max_events_per_hour(self):
        clk = FakeClock()
        s = _sensor(clk, cooldown_sec=1.0, max_events_per_hour=3)
        fires = 0
        for i in range(10):
            # каждый раз новая позиция входит в зону
            d = s.evaluate([(i, 1.6)])
            if d.fire:
                fires += 1
            clk.advance(60)  # 60с между событиями, 10 событий = 600с < 1h
        assert fires == 3

    def test_rate_cap_window_slides(self):
        clk = FakeClock()
        s = _sensor(clk, cooldown_sec=1.0, max_events_per_hour=2)
        assert s.evaluate([(1, 1.6)]).fire
        clk.advance(10)
        assert s.evaluate([(2, 1.6)]).fire
        clk.advance(10)
        assert not s.evaluate([(3, 1.6)]).fire  # cap=2 в окне
        # сдвигаем окно на >1ч от первых двух
        clk.advance(3700)
        assert s.evaluate([(4, 1.6)]).fire


class TestPruneAndNone:
    def test_closed_positions_pruned(self):
        clk = FakeClock()
        s = _sensor(clk)
        s.evaluate([(1, 1.6)])  # fire, disarm 1
        # позиция 1 закрылась, исчезла из списка
        s.evaluate([])
        assert 1 not in s._armed
        # новая позиция с тем же id начинает armed заново
        clk.advance(200)
        assert s.evaluate([(1, 1.6)]).fire

    def test_none_r_ignored(self):
        clk = FakeClock()
        s = _sensor(clk)
        d = s.evaluate([(1, None), (2, None)])
        assert not d.fire

    def test_empty_positions_no_fire(self):
        clk = FakeClock()
        s = _sensor(clk)
        assert not s.evaluate([]).fire
