import pandas as pd
import pytest

from fx_momentum_bot.app.main import (
    ManagedPosition,
    _calc_partial_close_volume,
    _drop_forming_bar,
    _flip_close_targets,
    _momentum_sign_direction,
    _r_multiple,
    _should_record_direction,
)


def _pos(pid: int, side: str) -> ManagedPosition:
    return ManagedPosition(
        position_id=pid,
        symbol="EURUSD=X",
        side=side,
        volume=1000,
        entry_price=1.1,
        stop_loss=None,
        digits=5,
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


def test_flip_close_targets_selects_only_opposite_side() -> None:
    positions = [_pos(1, "long"), _pos(2, "short"), _pos(3, "long")]
    # Флип на short → закрываем лонги, шорт не трогаем.
    targets = _flip_close_targets(positions, "short")
    assert [p.position_id for p in targets] == [1, 3]


def test_flip_close_targets_empty_on_flat_or_no_positions() -> None:
    assert _flip_close_targets([_pos(1, "long")], "flat") == []
    assert _flip_close_targets([], "long") == []


def test_flip_close_targets_nothing_when_same_direction() -> None:
    assert _flip_close_targets([_pos(1, "long")], "long") == []


def test_momentum_sign_direction() -> None:
    # TSMOM sign rule: знак momentum определяет, какая сторона «жива».
    assert _momentum_sign_direction(0.0008) == "long"
    assert _momentum_sign_direction(-0.0001) == "short"
    assert _momentum_sign_direction(0.0) == ""


def test_decay_close_selection_via_sign() -> None:
    # momentum слегка отрицательный (< 0, но > -threshold): лонг закрывается
    # по затуханию, шорт остаётся жить.
    positions = [_pos(1, "long"), _pos(2, "short")]
    sign_dir = _momentum_sign_direction(-0.0004)
    targets = _flip_close_targets(positions, sign_dir)
    assert [p.position_id for p in targets] == [1]


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
