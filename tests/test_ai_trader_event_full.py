"""v0.34 (2026-05-29): event-driven analyst для AI-Trader (Bybit).

Порт fx Фаз 1-3 на крипту. Тестируем инструмент-независимую логику
датчиков (rising-edge, гистерезис, cooldown, rate-cap, prune, slots-gate)
+ snapshot/delta merge живого WebSocket-кэша цены.

- ``compute_unrealised_r``: BUY/SELL, None при отсутствии SL/цены/риска.
- ``LockedProfitSensor`` → внеплановый REVIEW (locked-profit ≥1.5R).
- ``AdverseMoveSensor`` → внеплановый FULL (≤−1.0R, тезис судит full).
- ``EntryBreakoutSensor`` → внеплановый FULL (Donchian-пробой, Faith 2003).
- ``BybitPriceStream``: snapshot заполняет, delta мёржит, стейл → None.
"""

from __future__ import annotations

import pytest

from ai_trader.llm.prompts import build_user_prompt, build_user_prompt_review
from ai_trader.trading.price_sensor import (
    AdverseMoveSensor,
    EntryBreakoutSensor,
    LockedProfitSensor,
    compute_unrealised_r,
)
from ai_trader.trading.price_stream import BybitPriceStream


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ─── compute_unrealised_r ───────────────────────────────────────────────


class TestComputeUnrealisedR:
    def test_buy_profit(self):
        assert compute_unrealised_r("Buy", 100.0, 90.0, 115.0) == pytest.approx(1.5)

    def test_buy_loss(self):
        assert compute_unrealised_r("Buy", 100.0, 90.0, 95.0) == pytest.approx(-0.5)

    def test_sell_profit(self):
        assert compute_unrealised_r("Sell", 100.0, 110.0, 85.0) == pytest.approx(1.5)

    def test_sell_loss(self):
        assert compute_unrealised_r("Sell", 100.0, 110.0, 105.0) == pytest.approx(-0.5)

    def test_none_when_no_price(self):
        assert compute_unrealised_r("Buy", 100.0, 90.0, None) is None

    def test_none_when_no_sl(self):
        assert compute_unrealised_r("Buy", 100.0, None, 115.0) is None

    def test_none_when_degenerate_risk(self):
        assert compute_unrealised_r("Buy", 100.0, 100.0, 115.0) is None

    def test_none_when_bad_entry(self):
        assert compute_unrealised_r("Buy", 0.0, -5.0, 115.0) is None


# ─── LockedProfitSensor (→ review) ──────────────────────────────────────


def _lp(clk: FakeClock, **kw) -> LockedProfitSensor:
    params = dict(
        threshold_r=1.5, hysteresis_r=0.3, cooldown_sec=120.0,
        max_events_per_hour=6, now=clk,
    )
    params.update(kw)
    return LockedProfitSensor(**params)


class TestLockedProfitSensor:
    def test_fires_once_on_entering_zone(self):
        clk = FakeClock()
        s = _lp(clk)
        d = s.evaluate([(1, 1.6)])
        assert d.fire and d.positions == [(1, 1.6)]
        assert "locked-profit" in d.triggers[0]
        # повторно в зоне (disarmed) — не стреляет даже после cooldown
        clk.advance(200)
        assert not s.evaluate([(1, 1.7)]).fire

    def test_rearm_after_dropping_below_hysteresis(self):
        clk = FakeClock()
        s = _lp(clk)
        assert s.evaluate([(1, 1.6)]).fire
        clk.advance(200)
        # упали ниже 1.5−0.3=1.2 → re-arm
        assert not s.evaluate([(1, 1.1)]).fire
        clk.advance(200)
        assert s.evaluate([(1, 1.6)]).fire

    def test_no_rearm_within_hysteresis_band(self):
        clk = FakeClock()
        s = _lp(clk)
        assert s.evaluate([(1, 1.6)]).fire
        clk.advance(200)
        # 1.3 в полосе гистерезиса (>1.2) → НЕ re-arm
        assert not s.evaluate([(1, 1.3)]).fire
        clk.advance(200)
        assert not s.evaluate([(1, 1.6)]).fire

    def test_cooldown_blocks_second_position(self):
        clk = FakeClock()
        s = _lp(clk, cooldown_sec=120.0)
        assert s.evaluate([(1, 1.6)]).fire
        clk.advance(50)
        d = s.evaluate([(1, 1.6), (2, 1.6)])
        assert not d.fire and d.throttled
        clk.advance(80)  # 130 > 120
        assert s.evaluate([(2, 1.6)]).fire

    def test_max_events_per_hour(self):
        clk = FakeClock()
        s = _lp(clk, cooldown_sec=1.0, max_events_per_hour=2)
        assert s.evaluate([(1, 1.6)]).fire
        clk.advance(10)
        assert s.evaluate([(2, 1.6)]).fire
        clk.advance(10)
        d = s.evaluate([(3, 1.6)])
        assert not d.fire and d.rate_capped

    def test_closed_positions_pruned(self):
        clk = FakeClock()
        s = _lp(clk)
        s.evaluate([(1, 1.6)])
        s.evaluate([])
        assert 1 not in s._armed

    def test_none_r_ignored(self):
        clk = FakeClock()
        s = _lp(clk)
        assert not s.evaluate([(1, None)]).fire

    def test_empty_positions_no_fire(self):
        clk = FakeClock()
        s = _lp(clk)
        assert not s.evaluate([]).fire


# ─── AdverseMoveSensor (→ full) ─────────────────────────────────────────


def _adv(clk: FakeClock, **kw) -> AdverseMoveSensor:
    params = dict(
        threshold_r=1.0, hysteresis_r=0.3, cooldown_sec=300.0,
        max_events_per_hour=4, now=clk,
    )
    params.update(kw)
    return AdverseMoveSensor(**params)


class TestAdverseMoveSensor:
    def test_no_fire_above_threshold(self):
        clk = FakeClock()
        s = _adv(clk)
        assert not s.evaluate([(1, -0.5)]).fire
        assert not s.evaluate([(1, 0.8)]).fire

    def test_fires_on_crossing_below_negative_threshold(self):
        clk = FakeClock()
        s = _adv(clk)
        d = s.evaluate([(1, -1.2)])
        assert d.fire and d.positions == [(1, -1.2)]
        assert "adverse" in d.triggers[0]

    def test_no_refire_while_disarmed(self):
        clk = FakeClock()
        s = _adv(clk)
        assert s.evaluate([(1, -1.2)]).fire
        clk.advance(400)
        assert not s.evaluate([(1, -1.5)]).fire

    def test_rearm_after_recovery(self):
        clk = FakeClock()
        s = _adv(clk)
        assert s.evaluate([(1, -1.2)]).fire
        clk.advance(400)
        assert not s.evaluate([(1, -0.5)]).fire  # re-arm (−0.5 > −0.7)
        clk.advance(400)
        assert s.evaluate([(1, -1.2)]).fire

    def test_cooldown(self):
        clk = FakeClock()
        s = _adv(clk, cooldown_sec=300.0)
        assert s.evaluate([(1, -1.2)]).fire
        clk.advance(100)
        d = s.evaluate([(1, -1.2), (2, -1.2)])
        assert not d.fire and d.throttled
        clk.advance(250)
        assert s.evaluate([(2, -1.2)]).fire

    def test_none_r_ignored_and_prune(self):
        clk = FakeClock()
        s = _adv(clk)
        assert not s.evaluate([(1, None)]).fire
        s.evaluate([(1, -1.2)])
        s.evaluate([])
        assert 1 not in s._armed


# ─── EntryBreakoutSensor (→ full) ───────────────────────────────────────


def _ent(clk: FakeClock, **kw) -> EntryBreakoutSensor:
    params = dict(buffer_atr=0.0, cooldown_sec=300.0, max_events_per_hour=4, now=clk)
    params.update(kw)
    return EntryBreakoutSensor(**params)


class TestEntryBreakoutSensor:
    def test_up_break_fires(self):
        clk = FakeClock()
        s = _ent(clk)
        s.update_reference("BTCUSDT", hi=70000.0, lo=68000.0, atr=500.0)
        assert not s.evaluate({"BTCUSDT": 69500.0}, slots_free=True).fire
        d = s.evaluate({"BTCUSDT": 70010.0}, slots_free=True)
        assert d.fire and "up-break" in d.triggers[0]

    def test_down_break_fires(self):
        clk = FakeClock()
        s = _ent(clk)
        s.update_reference("ETHUSDT", hi=3800.0, lo=3600.0, atr=50.0)
        d = s.evaluate({"ETHUSDT": 3590.0}, slots_free=True)
        assert d.fire and "down-break" in d.triggers[0]

    def test_buffer_atr_requires_confirmation(self):
        clk = FakeClock()
        s = _ent(clk, buffer_atr=0.1)
        s.update_reference("BTCUSDT", hi=70000.0, lo=68000.0, atr=100.0)
        # буфер = 0.1*100 = 10 → нужно > 70010
        assert not s.evaluate({"BTCUSDT": 70005.0}, slots_free=True).fire
        assert s.evaluate({"BTCUSDT": 70015.0}, slots_free=True).fire

    def test_no_fire_when_no_slots(self):
        clk = FakeClock()
        s = _ent(clk)
        s.update_reference("BTCUSDT", hi=70000.0, lo=68000.0, atr=500.0)
        assert not s.evaluate({"BTCUSDT": 70010.0}, slots_free=False).fire
        assert s.evaluate({"BTCUSDT": 70010.0}, slots_free=True).fire

    def test_rearm_after_return_inside_channel(self):
        clk = FakeClock()
        s = _ent(clk)
        s.update_reference("BTCUSDT", hi=70000.0, lo=68000.0, atr=500.0)
        assert s.evaluate({"BTCUSDT": 70010.0}, slots_free=True).fire
        clk.advance(400)
        assert not s.evaluate({"BTCUSDT": 70020.0}, slots_free=True).fire
        s.evaluate({"BTCUSDT": 69900.0}, slots_free=True)  # внутрь → re-arm
        clk.advance(400)
        assert s.evaluate({"BTCUSDT": 70010.0}, slots_free=True).fire

    def test_cooldown_and_rate_cap(self):
        clk = FakeClock()
        s = _ent(clk, cooldown_sec=60.0, max_events_per_hour=2)
        for sym in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
            s.update_reference(sym, hi=10.0, lo=5.0, atr=1.0)
        assert s.evaluate({"AAAUSDT": 11.0}, slots_free=True).fire
        clk.advance(30)
        assert not s.evaluate({"BBBUSDT": 11.0}, slots_free=True).fire
        clk.advance(40)  # 70 > 60
        assert s.evaluate({"BBBUSDT": 11.0}, slots_free=True).fire
        clk.advance(70)
        assert not s.evaluate({"CCCUSDT": 11.0}, slots_free=True).fire

    def test_missing_live_price_skipped(self):
        clk = FakeClock()
        s = _ent(clk)
        s.update_reference("BTCUSDT", hi=70000.0, lo=68000.0, atr=500.0)
        assert not s.evaluate({"BTCUSDT": None}, slots_free=True).fire
        assert not s.evaluate({}, slots_free=True).fire

    def test_no_reference_no_fire(self):
        clk = FakeClock()
        s = _ent(clk)
        assert not s.evaluate({"BTCUSDT": 70010.0}, slots_free=True).fire


# ─── BybitPriceStream (snapshot/delta merge) ────────────────────────────


class TestBybitPriceStream:
    def _stream(self, clk: FakeClock, **kw) -> BybitPriceStream:
        params = dict(
            symbols=["BTCUSDT"], max_age_sec=60.0,
            ws_factory=lambda: None, now=clk,
        )
        params.update(kw)
        return BybitPriceStream(**params)

    def test_snapshot_then_delta_merge(self):
        clk = FakeClock()
        s = self._stream(clk)
        # snapshot: оба поля
        s._on_tick({
            "topic": "tickers.BTCUSDT", "type": "snapshot",
            "data": {"symbol": "BTCUSDT", "lastPrice": "70000", "markPrice": "70010"},
        })
        assert s.get_live_mid("BTCUSDT") == pytest.approx(70010.0)  # mark приоритет
        # delta: только lastPrice — markPrice сохраняется из snapshot
        s._on_tick({
            "topic": "tickers.BTCUSDT", "type": "delta",
            "data": {"symbol": "BTCUSDT", "lastPrice": "70100"},
        })
        assert s.get_live_mid("BTCUSDT") == pytest.approx(70010.0)  # mark не менялся
        # delta: обновили markPrice
        s._on_tick({
            "topic": "tickers.BTCUSDT", "type": "delta",
            "data": {"symbol": "BTCUSDT", "markPrice": "70200"},
        })
        assert s.get_live_mid("BTCUSDT") == pytest.approx(70200.0)

    def test_last_price_fallback_when_no_mark(self):
        clk = FakeClock()
        s = self._stream(clk)
        s._on_tick({
            "type": "snapshot",
            "data": {"symbol": "ETHUSDT", "lastPrice": "3500"},
        })
        assert s.get_live_mid("ETHUSDT") == pytest.approx(3500.0)

    def test_stale_returns_none(self):
        clk = FakeClock()
        s = self._stream(clk, max_age_sec=30.0)
        s._on_tick({
            "type": "snapshot",
            "data": {"symbol": "BTCUSDT", "markPrice": "70000"},
        })
        assert s.get_live_mid("BTCUSDT") == pytest.approx(70000.0)
        clk.advance(31)  # старше max_age
        assert s.get_live_mid("BTCUSDT") is None

    def test_unknown_symbol_none(self):
        clk = FakeClock()
        s = self._stream(clk)
        assert s.get_live_mid("DOGEUSDT") is None

    def test_malformed_message_ignored(self):
        clk = FakeClock()
        s = self._stream(clk)
        s._on_tick({"type": "snapshot", "data": None})
        s._on_tick({"type": "snapshot", "data": {"lastPrice": "1"}})  # нет symbol
        s._on_tick({})
        assert s.get_live_mid("BTCUSDT") is None

    def test_zero_price_rejected(self):
        clk = FakeClock()
        s = self._stream(clk)
        s._on_tick({
            "type": "snapshot",
            "data": {"symbol": "BTCUSDT", "markPrice": "0", "lastPrice": "0"},
        })
        assert s.get_live_mid("BTCUSDT") is None


# ─── Event note → prompt (датчик доходит до аналитика) ──────────────────


class TestEventNoteReachesPrompt:
    """Регресс: сработавший датчик должен ПОПАСТЬ В ПРОМПТ LLM, а не
    только в лог. event_note вставляется в начало user-prompt.
    """

    def test_full_prompt_without_note_unchanged(self):
        base = build_user_prompt("CTX")
        assert "UNSCHEDULED" not in base
        assert base.startswith("Current market state")

    def test_full_prompt_with_note_prepends_event(self):
        note = "⚡ UNSCHEDULED EVENT CYCLE\n  - SUIUSDT up-break"
        out = build_user_prompt("CTX", event_note=note)
        assert out.startswith(note)
        assert "SUIUSDT up-break" in out
        assert "CTX" in out  # рыночный контекст по-прежнему присутствует

    def test_review_prompt_without_note_unchanged(self):
        base = build_user_prompt_review("CTX")
        assert "UNSCHEDULED" not in base
        assert base.startswith("Mid-cycle review")

    def test_review_prompt_with_note_prepends_event(self):
        note = "⚡ UNSCHEDULED GUARDIAN CHECK\n  - #27 +1.60R locked-profit"
        out = build_user_prompt_review("CTX", event_note=note)
        assert out.startswith(note)
        assert "locked-profit" in out
        assert "GUARDIAN" in out  # task-restatement сохранён

    def test_format_event_note_full_lists_triggers_and_discipline(self):
        from ai_trader.app.main import _format_event_note

        note = _format_event_note(
            ["SUIUSDT up-break @3.45 > Donchian hi", "#27 -1.20R adverse"],
            kind="full",
        )
        assert "UNSCHEDULED EVENT CYCLE" in note
        assert "SUIUSDT up-break @3.45 > Donchian hi" in note
        assert "#27 -1.20R adverse" in note
        # discipline: не форсировать сделку.
        assert "Do NOT force a trade" in note
        assert "MFP" in note

    def test_format_event_note_review_is_guardian(self):
        from ai_trader.app.main import _format_event_note

        note = _format_event_note(["#27 +1.60R locked-profit"], kind="review")
        assert "GUARDIAN" in note
        assert "#27 +1.60R locked-profit" in note
        assert "close_net" in note

    def test_format_event_note_empty_triggers_safe(self):
        from ai_trader.app.main import _format_event_note

        assert "(n/a)" in _format_event_note([], kind="full")
