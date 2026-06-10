import pandas as pd
import pytest

from fx_momentum_bot.app.main import (
    _calc_partial_close_volume,
    _drop_forming_bar,
    _r_multiple,
    _should_record_direction,
)


def _make_ohlcv(index: pd.DatetimeIndex) -> pd.DataFrame:
    n = len(index)
    return pd.DataFrame(
        {
            "Open": [1.0] * n,
            "High": [1.1] * n,
            "Low": [0.9] * n,
            "Close": [1.0] * n,
            "Volume": [100] * n,
        },
        index=index,
    )


def test_drop_forming_bar_removes_incomplete_last_bar() -> None:
    now = pd.Timestamp.now(tz="UTC").floor("h")
    idx = pd.date_range(end=now, periods=5, freq="1h", tz="UTC")
    df = _make_ohlcv(idx)
    # Последний бар открыт в текущем часе → ещё формируется → отброшен.
    out = _drop_forming_bar(df, "1h")
    assert len(out) == 4
    assert out.index[-1] == idx[-2]


def test_drop_forming_bar_keeps_closed_bars() -> None:
    end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=2)
    idx = pd.date_range(end=end, periods=5, freq="1h", tz="UTC")
    df = _make_ohlcv(idx)
    out = _drop_forming_bar(df, "1h")
    assert len(out) == 5


def test_drop_forming_bar_handles_none_and_unknown_interval() -> None:
    assert _drop_forming_bar(None, "1h") is None
    now = pd.Timestamp.now(tz="UTC")
    df = _make_ohlcv(pd.date_range(end=now, periods=3, freq="1h", tz="UTC"))
    # Неизвестный интервал — без изменений (не рискуем отбрасывать валидное).
    assert len(_drop_forming_bar(df, "4h")) == 3


def test_should_record_direction_paper_mode_always_records() -> None:
    assert _should_record_direction(live=False, wants_open=True, executed=False)


def test_should_record_direction_keeps_signal_when_blocked() -> None:
    # Live: вход хотели, но не состоялся (max_positions/ошибка) → НЕ фиксируем,
    # сигнал должен повториться в следующем цикле.
    assert not _should_record_direction(live=True, wants_open=True, executed=False)


def test_should_record_direction_records_on_execute_or_no_intent() -> None:
    assert _should_record_direction(live=True, wants_open=True, executed=True)
    assert _should_record_direction(live=True, wants_open=False, executed=False)


def test_r_multiple_for_long_and_short() -> None:
    assert _r_multiple(
        "long", entry_price=1.2000, current_price=1.2030, risk_price=0.0010
    ) == pytest.approx(3.0)
    assert _r_multiple(
        "short", entry_price=1.2000, current_price=1.1970, risk_price=0.0010
    ) == pytest.approx(3.0)


def test_r_multiple_zero_on_nonpositive_risk() -> None:
    assert _r_multiple("long", entry_price=1.2, current_price=1.3, risk_price=0.0) == 0.0


def test_partial_close_respects_step_and_min_volume() -> None:
    # 100000 volume, 50% partial, step=1000 => closes 50000.
    assert _calc_partial_close_volume(
        current_volume=100000,
        fraction=0.5,
        step_volume=1000,
        min_volume=1000,
    ) == 50000


def test_partial_close_keeps_minimum_runner() -> None:
    # Requested 90% would close 90000, but we must leave at least min_volume (20000).
    assert _calc_partial_close_volume(
        current_volume=100000,
        fraction=0.9,
        step_volume=1000,
        min_volume=20000,
    ) == 80000


def test_partial_close_disabled_when_position_too_small() -> None:
    # Close would violate "leave at least min volume".
    assert _calc_partial_close_volume(
        current_volume=1500,
        fraction=0.5,
        step_volume=1000,
        min_volume=1000,
    ) == 0
