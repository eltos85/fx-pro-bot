"""Тесты автопроверки сигналов и расчёта профита."""

from datetime import UTC, datetime, timedelta

from fx_pro_bot.stats.store import StatsStore


def _make_signal(store: StatsStore, direction: str, price: float, instrument: str = "EURUSD=X") -> str:
    return store.record_suggestion(
        instrument=instrument,
        direction=direction,
        advice_text="тест",
        reasons=("ma_cross_up",),
        price_at_signal=price,
        events_context=None,
    )


def test_verification_long_profit(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.sqlite")
    sid = _make_signal(store, "long", 1.08000)

    store.record_verification(
        suggestion_id=sid,
        horizon_minutes=15,
        price_at_check=1.08050,
        profit_pips=5.0,
        verdict="right",
    )

    vrows = store.verifications_for(sid)
    assert len(vrows) == 1
    assert vrows[0].profit_pips == 5.0
    assert vrows[0].verdict == "right"
    assert vrows[0].horizon_minutes == 15


def test_verification_short_loss(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.sqlite")
    sid = _make_signal(store, "short", 1.08000)

    store.record_verification(
        suggestion_id=sid,
        horizon_minutes=30,
        price_at_check=1.08020,
        profit_pips=-2.0,
        verdict="wrong",
    )

    vrows = store.verifications_for(sid)
    assert len(vrows) == 1
    assert vrows[0].profit_pips == -2.0
    assert vrows[0].verdict == "wrong"


def test_verification_summary(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.sqlite")

    sid1 = _make_signal(store, "long", 1.08000)
    sid2 = _make_signal(store, "short", 1.08100)

    store.record_verification(
        suggestion_id=sid1, horizon_minutes=15,
        price_at_check=1.08050, profit_pips=5.0, verdict="right",
    )
    store.record_verification(
        suggestion_id=sid2, horizon_minutes=15,
        price_at_check=1.08120, profit_pips=-2.0, verdict="wrong",
    )

    vs = store.verification_summary(15)
    assert vs["total"] == 2
    assert vs["right"] == 1
    assert vs["wrong"] == 1
    assert vs["win_rate"] == 0.5
    assert vs["total_profit"] == 3.0


def test_verification_summary_by_instrument(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.sqlite")

    sid1 = _make_signal(store, "long", 1.08000, "EURUSD=X")
    sid2 = _make_signal(store, "long", 1800.0, "GC=F")

    store.record_verification(
        suggestion_id=sid1, horizon_minutes=15,
        price_at_check=1.08050, profit_pips=5.0, verdict="right",
    )
    store.record_verification(
        suggestion_id=sid2, horizon_minutes=15,
        price_at_check=1801.0, profit_pips=10.0, verdict="right",
    )

    by_instr = store.verification_summary_by_instrument(15)
    assert len(by_instr) == 2

    symbols = {row["instrument"] for row in by_instr}
    assert "EURUSD=X" in symbols
    assert "GC=F" in symbols


def test_pending_for_verification(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.sqlite")
    sid = _make_signal(store, "long", 1.08000)

    now = datetime.now(tz=UTC)
    pending = store.pending_for_verification(15, now + timedelta(minutes=20))
    assert any(s.id == sid for s in pending)

    pending_too_early = store.pending_for_verification(15, now + timedelta(minutes=5))
    assert not any(s.id == sid for s in pending_too_early)


def test_no_duplicate_verification(tmp_path) -> None:
    store = StatsStore(tmp_path / "t.sqlite")
    sid = _make_signal(store, "long", 1.08000)

    store.record_verification(
        suggestion_id=sid, horizon_minutes=15,
        price_at_check=1.08050, profit_pips=5.0, verdict="right",
    )
    store.record_verification(
        suggestion_id=sid, horizon_minutes=15,
        price_at_check=1.08060, profit_pips=6.0, verdict="right",
    )

    vrows = store.verifications_for(sid)
    assert len(vrows) == 1
    assert vrows[0].profit_pips == 5.0
