from fx_pro_bot.stats.store import StatsStore


def test_stats_record_and_verdict(tmp_path) -> None:
    db = tmp_path / "t.sqlite"
    store = StatsStore(db)
    sid = store.record_suggestion(
        instrument="EURUSD=X",
        direction="long",
        advice_text="тест",
        reasons=("ma_cross_up",),
        price_at_signal=1.1,
        events_context=None,
    )
    assert len(sid) == 36
    assert store.set_verdict(sid, "right", notes="ok")
    s = store.summary()
    assert s["right"] == 1
    assert s["wrong"] == 0
    assert s["accuracy"] == 1.0
