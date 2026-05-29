"""Phase 3 (2026-05-29): event-driven FULL-цикл (аналитик по датчикам).

Контекст (BUILDLOG_AI_FX_TRADER.md 2026-05-29): идея пользователя —
«график показал сетап → позвали аналитика». Дешёвые датчики будят
дорогой full-цикл (macro+news) по событиям:
- EntryBreakoutSensor: пробой Donchian-канала живой ценой → аналитик
  решает open/hold (Donchian/Turtle 20-period; Lopez de Prado event-
  based sampling).
- AdverseMoveSensor: позиция ушла в минус ≥ threshold_r → стратег с
  macro пересматривает тезис (Phase 0: тезис судит full, не review).

Тесты: rising-edge, re-arm с гистерезисом, slots-free gate (entry),
cooldown, rate-cap, prune.
"""

from __future__ import annotations

from fx_ai_trader.trading.price_sensor import (
    AdverseMoveSensor,
    EntryBreakoutSensor,
)


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ─── AdverseMoveSensor ──────────────────────────────────────────────────


class TestAdverseMoveSensor:
    def _s(self, clk, **kw):
        params = dict(
            threshold_r=1.0, hysteresis_r=0.3, cooldown_sec=300.0,
            max_events_per_hour=4, now=clk,
        )
        params.update(kw)
        return AdverseMoveSensor(**params)

    def test_no_fire_above_threshold(self):
        clk = FakeClock()
        s = self._s(clk)
        assert not s.evaluate([(1, -0.5)]).fire
        assert not s.evaluate([(1, 0.8)]).fire

    def test_fires_on_crossing_below_negative_threshold(self):
        clk = FakeClock()
        s = self._s(clk)
        d = s.evaluate([(1, -1.2)])
        assert d.fire
        assert d.positions == [(1, -1.2)]
        assert "adverse" in d.triggers[0]

    def test_no_refire_while_disarmed(self):
        clk = FakeClock()
        s = self._s(clk)
        assert s.evaluate([(1, -1.2)]).fire
        clk.advance(400)  # cooldown прошёл
        assert not s.evaluate([(1, -1.5)]).fire  # всё ещё disarmed

    def test_rearm_after_recovery(self):
        clk = FakeClock()
        s = self._s(clk)
        assert s.evaluate([(1, -1.2)]).fire
        clk.advance(400)
        # вернулись выше (−1.0 + 0.3 = −0.7) → re-arm
        assert not s.evaluate([(1, -0.5)]).fire
        clk.advance(400)
        assert s.evaluate([(1, -1.2)]).fire

    def test_cooldown(self):
        clk = FakeClock()
        s = self._s(clk, cooldown_sec=300.0)
        assert s.evaluate([(1, -1.2)]).fire
        clk.advance(100)
        d = s.evaluate([(1, -1.2), (2, -1.2)])
        assert not d.fire and d.throttled
        clk.advance(250)  # 350 > 300
        assert s.evaluate([(1, -1.2), (2, -1.2)]).fire

    def test_none_r_ignored_and_prune(self):
        clk = FakeClock()
        s = self._s(clk)
        assert not s.evaluate([(1, None)]).fire
        s.evaluate([(1, -1.2)])  # fire, disarm
        s.evaluate([])  # prune
        assert 1 not in s._armed


# ─── EntryBreakoutSensor ────────────────────────────────────────────────


class TestEntryBreakoutSensor:
    def _s(self, clk, **kw):
        params = dict(
            buffer_atr=0.0, cooldown_sec=300.0, max_events_per_hour=4, now=clk,
        )
        params.update(kw)
        return EntryBreakoutSensor(**params)

    def test_up_break_fires(self):
        clk = FakeClock()
        s = self._s(clk)
        s.update_reference("XAUUSD", hi=2000.0, lo=1980.0, atr=5.0)
        # цена внутри канала → no fire
        assert not s.evaluate({"XAUUSD": 1995.0}, slots_free=True).fire
        # пробой вверх
        d = s.evaluate({"XAUUSD": 2001.0}, slots_free=True)
        assert d.fire
        assert "up-break" in d.triggers[0]

    def test_down_break_fires(self):
        clk = FakeClock()
        s = self._s(clk)
        s.update_reference("BZ=F", hi=85.0, lo=80.0, atr=1.0)
        d = s.evaluate({"BZ=F": 79.5}, slots_free=True)
        assert d.fire
        assert "down-break" in d.triggers[0]

    def test_buffer_atr_requires_confirmation(self):
        clk = FakeClock()
        s = self._s(clk, buffer_atr=0.1)
        s.update_reference("XAUUSD", hi=2000.0, lo=1980.0, atr=10.0)
        # буфер = 0.1*10 = 1.0 → нужно > 2001.0
        assert not s.evaluate({"XAUUSD": 2000.5}, slots_free=True).fire
        assert s.evaluate({"XAUUSD": 2001.5}, slots_free=True).fire

    def test_no_fire_when_no_slots(self):
        clk = FakeClock()
        s = self._s(clk)
        s.update_reference("XAUUSD", hi=2000.0, lo=1980.0, atr=5.0)
        # пробой есть, но слотов нет
        assert not s.evaluate({"XAUUSD": 2001.0}, slots_free=False).fire
        # арминг обновился; когда слот появился — стреляет
        d = s.evaluate({"XAUUSD": 2001.0}, slots_free=True)
        assert d.fire

    def test_rearm_after_return_inside_channel(self):
        clk = FakeClock()
        s = self._s(clk)
        s.update_reference("XAUUSD", hi=2000.0, lo=1980.0, atr=5.0)
        assert s.evaluate({"XAUUSD": 2001.0}, slots_free=True).fire
        clk.advance(400)
        # всё ещё выше hi, disarmed → no re-fire
        assert not s.evaluate({"XAUUSD": 2002.0}, slots_free=True).fire
        # вернулись внутрь канала → re-arm
        s.evaluate({"XAUUSD": 1999.0}, slots_free=True)
        clk.advance(400)
        assert s.evaluate({"XAUUSD": 2001.0}, slots_free=True).fire

    def test_cooldown_and_rate_cap(self):
        clk = FakeClock()
        s = self._s(clk, cooldown_sec=60.0, max_events_per_hour=2)
        s.update_reference("A", hi=10.0, lo=5.0, atr=1.0)
        s.update_reference("B", hi=10.0, lo=5.0, atr=1.0)
        s.update_reference("C", hi=10.0, lo=5.0, atr=1.0)
        assert s.evaluate({"A": 11.0}, slots_free=True).fire
        clk.advance(30)
        # cooldown не прошёл
        assert not s.evaluate({"B": 11.0}, slots_free=True).fire
        clk.advance(40)  # 70 > 60
        assert s.evaluate({"B": 11.0}, slots_free=True).fire
        clk.advance(70)
        # rate cap = 2 в окне
        assert not s.evaluate({"C": 11.0}, slots_free=True).fire

    def test_missing_live_price_skipped(self):
        clk = FakeClock()
        s = self._s(clk)
        s.update_reference("XAUUSD", hi=2000.0, lo=1980.0, atr=5.0)
        assert not s.evaluate({"XAUUSD": None}, slots_free=True).fire
        assert not s.evaluate({}, slots_free=True).fire

    def test_no_reference_no_fire(self):
        clk = FakeClock()
        s = self._s(clk)
        assert not s.evaluate({"XAUUSD": 2001.0}, slots_free=True).fire
