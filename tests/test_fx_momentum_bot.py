from datetime import datetime, timezone

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
from fx_momentum_bot.strategy.event_guard import high_impact_event_near


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


# ─── Event-guard: блок входов вокруг HIGH-impact релизов ─────────────────
# Кейс 2026-06-10: VP-шорт золота за 8 мин до US CPI (12:30 UTC) → −$24.70,
# ре-вход в 13:28 после релиза → −$32.61. US CPI 2026-06-10 08:30 ET =
# 12:30 UTC (EDT) — реальная дата из static-календаря (bls.gov).

_CPI_UTC = datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc)


def _at(hh: int, mm: int) -> datetime:
    return datetime(2026, 6, 10, hh, mm, tzinfo=timezone.utc)


def test_event_guard_blocks_before_release() -> None:
    # 12:22 — реальное время входа VP-шорта (за 8 минут до CPI).
    blocked = high_impact_event_near(_at(12, 22), before_min=60, after_min=60)
    assert blocked is not None
    assert "CPI" in blocked


def test_event_guard_blocks_after_release() -> None:
    # 13:28 — реальное время ре-входа (58 минут после CPI).
    blocked = high_impact_event_near(_at(13, 28), before_min=60, after_min=60)
    assert blocked is not None
    assert "CPI" in blocked


def test_event_guard_open_outside_window() -> None:
    # За 2 часа до релиза и через 2 часа после — торговля разрешена.
    assert high_impact_event_near(_at(10, 30), before_min=60, after_min=60) is None
    assert high_impact_event_near(_at(14, 31), before_min=60, after_min=60) is None


def test_event_guard_window_edges() -> None:
    # Ровно на границах окна ±60 мин — ещё заблокировано.
    assert high_impact_event_near(_at(11, 30), before_min=60, after_min=60) is not None
    assert high_impact_event_near(_at(13, 30), before_min=60, after_min=60) is not None


def test_event_guard_fomc_blocked() -> None:
    # FOMC decision 2026-06-17 14:00 ET = 18:00 UTC (EDT).
    fomc = datetime(2026, 6, 17, 17, 30, tzinfo=timezone.utc)
    blocked = high_impact_event_near(fomc, before_min=60, after_min=60)
    assert blocked is not None
    assert "FOMC" in blocked


def test_event_guard_quiet_day_not_blocked() -> None:
    # Обычный день без HIGH-impact релизов поблизости.
    quiet = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)
    assert high_impact_event_near(quiet, before_min=60, after_min=60) is None


def test_event_guard_symbol_scoping_ecb_boj() -> None:
    # ECB decision 2026-06-11 14:15 CEST = 12:15 UTC (ecb.europa.eu):
    # блокирует EUR-пары, но НЕ золото и НЕ JPY.
    ecb_near = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    assert high_impact_event_near(ecb_near, symbol="EURUSD=X") is not None
    assert high_impact_event_near(ecb_near, symbol="GC=F") is None
    assert high_impact_event_near(ecb_near, symbol="USDJPY=X") is None
    # BoJ MPM 2026-06-16, номинал 12:00 JST = 03:00 UTC (boj.or.jp):
    # блокирует JPY-пары, но НЕ EUR и НЕ золото.
    boj_near = datetime(2026, 6, 16, 2, 30, tzinfo=timezone.utc)
    assert high_impact_event_near(boj_near, symbol="USDJPY=X") is not None
    assert high_impact_event_near(boj_near, symbol="EURUSD=X") is None
    assert high_impact_event_near(boj_near, symbol="GC=F") is None


def test_event_guard_us_events_block_all_symbols() -> None:
    # US CPI (symbols=()) блокирует и FX, и золото.
    cpi_near = datetime(2026, 6, 10, 12, 22, tzinfo=timezone.utc)
    for sym in ("EURUSD=X", "USDJPY=X", "GC=F"):
        assert high_impact_event_near(cpi_near, symbol=sym) is not None


# ─── ATR-scaled sizing (Tharp, reuse advisor calc_lot_size) ──────────────


def _sizing_settings(risk: float, max_lot: float = 0.05):
    from types import SimpleNamespace
    return SimpleNamespace(risk_per_trade_usd=risk, max_lot_size=max_lot)


def test_position_lot_scales_fx_up_and_caps() -> None:
    from fx_momentum_bot.app.main import _position_lot
    # EURUSD: SL 25 pips (0.0025), pip=$0.10/0.01lot → риск $2.5 на 0.01.
    # Для $15 нужно 0.06 → cap 0.05 (MAX после инцидента 23.04).
    lot = _position_lot(_sizing_settings(15.0), "EURUSD=X", 0.0025, 0.01)
    assert lot == 0.05


def test_position_lot_gold_clamped_to_min() -> None:
    from fx_momentum_bot.app.main import _position_lot
    # GC=F: SL 24 пункта → риск $24 на 0.01 лоте > $15 → кламп на min 0.01
    # (меньше минимального лота уменьшить риск нельзя).
    lot = _position_lot(_sizing_settings(15.0), "GC=F", 24.0, 0.01)
    assert lot == 0.01


def test_position_lot_disabled_falls_back_to_fixed() -> None:
    from fx_momentum_bot.app.main import _position_lot
    assert _position_lot(_sizing_settings(0.0), "EURUSD=X", 0.0025, 0.03) == 0.03


# ─── VP: окно входов (ликвидная сессия, Dalton day-timeframe) ────────────


def _vp_window_settings():
    from types import SimpleNamespace
    return SimpleNamespace(
        vp_session_tz="America/New_York",
        vp_entry_start="07:00",
        vp_entry_end="17:00",
    )


def test_vp_entry_window_open_during_ny_session() -> None:
    from fx_momentum_bot.app.main import _vp_entry_window_open
    # 14:00 UTC июнь = 10:00 NY (EDT) — внутри окна.
    now = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
    assert _vp_entry_window_open(_vp_window_settings(), now) is True


def test_vp_entry_window_closed_overnight() -> None:
    from fx_momentum_bot.app.main import _vp_entry_window_open
    # 23:54 UTC = 19:54 NY — реальное время ночных VP-попыток 06-09
    # (тонкий рынок, мгновенные стоп-ауты в выписке).
    night = datetime(2026, 6, 9, 23, 54, tzinfo=timezone.utc)
    assert _vp_entry_window_open(_vp_window_settings(), night) is False
    # 00:44 UTC = 20:44 NY — тоже закрыто.
    night2 = datetime(2026, 6, 10, 0, 44, tzinfo=timezone.utc)
    assert _vp_entry_window_open(_vp_window_settings(), night2) is False


def test_vp_entry_window_edge_17et_closed() -> None:
    from fx_momentum_bot.app.main import _vp_entry_window_open
    # Ровно 17:00 ET (CME settlement) — окно уже закрыто.
    at_17 = datetime(2026, 6, 10, 21, 0, tzinfo=timezone.utc)
    assert _vp_entry_window_open(_vp_window_settings(), at_17) is False


# ─── Spread-guard (cost-to-risk, Harris 2003) ────────────────────────────


class _FakeSpotClient:
    def __init__(self, bid: float | None, ask: float | None):
        self._bid, self._ask = bid, ask

    def get_spot_price(self, symbol_id: int, max_age_sec: float | None = None):
        if self._bid is None and self._ask is None:
            return None
        return {"bid": self._bid, "ask": self._ask, "mid": None, "ts": 0, "age_sec": 0}


class _FakeSymbols:
    def resolve_yfinance(self, symbol: str):
        from types import SimpleNamespace
        return SimpleNamespace(symbol_id=1)


def _fake_executor(bid: float | None, ask: float | None):
    from types import SimpleNamespace
    return SimpleNamespace(symbols=_FakeSymbols(), client=_FakeSpotClient(bid, ask))


def test_spread_guard_blocks_wide_spread() -> None:
    from fx_momentum_bot.app.main import _spread_too_wide
    # SL 25 pips, спред 5 pips = 20% риска > 10% → блок (ночь/роллувер).
    err = _spread_too_wide(_fake_executor(1.15500, 1.15550), "EURUSD=X", 0.0025, 0.10)
    assert err is not None and "20%" in err


def test_spread_guard_passes_normal_spread() -> None:
    from fx_momentum_bot.app.main import _spread_too_wide
    # Спред 1.5 pips от SL 25 pips = 6% < 10% → вход разрешён.
    assert _spread_too_wide(_fake_executor(1.15500, 1.15515), "EURUSD=X", 0.0025, 0.10) is None


def test_spread_guard_no_data_does_not_block() -> None:
    from fx_momentum_bot.app.main import _spread_too_wide
    # Нет spot-данных — guard защита, не зависимость: НЕ блокируем.
    assert _spread_too_wide(_fake_executor(None, None), "EURUSD=X", 0.0025, 0.10) is None


def test_spread_guard_disabled() -> None:
    from fx_momentum_bot.app.main import _spread_too_wide
    assert _spread_too_wide(_fake_executor(1.0, 2.0), "EURUSD=X", 0.0025, 0.0) is None


# ─── VP friday-flat: не несём day-timeframe позицию через выходные ───────


def _flat_settings(enabled: bool = True):
    from types import SimpleNamespace
    return SimpleNamespace(
        vp_friday_flat_enabled=enabled,
        vp_session_tz="America/New_York",
        vp_friday_flat_start="16:45",
        vp_friday_flat_end="17:30",
    )


def test_vp_friday_flat_due_in_window() -> None:
    from fx_momentum_bot.app.main import _vp_friday_flat_due
    # 2026-06-12 — пятница; 20:50 UTC = 16:50 NY (EDT) — внутри окна.
    fri = datetime(2026, 6, 12, 20, 50, tzinfo=timezone.utc)
    assert _vp_friday_flat_due(_flat_settings(), fri) is True


def test_vp_friday_flat_not_due_earlier_friday() -> None:
    from fx_momentum_bot.app.main import _vp_friday_flat_due
    # Пятница 12:00 NY — рано, обычная торговля.
    fri_noon = datetime(2026, 6, 12, 16, 0, tzinfo=timezone.utc)
    assert _vp_friday_flat_due(_flat_settings(), fri_noon) is False


def test_vp_friday_flat_not_due_other_days() -> None:
    from fx_momentum_bot.app.main import _vp_friday_flat_due
    # Четверг 16:50 NY — не пятница.
    thu = datetime(2026, 6, 11, 20, 50, tzinfo=timezone.utc)
    assert _vp_friday_flat_due(_flat_settings(), thu) is False


def test_vp_friday_flat_window_closes_after_market() -> None:
    from fx_momentum_bot.app.main import _vp_friday_flat_due
    # Пятница 17:35 NY — рынок закрыт, не спамим close-ордерами.
    late = datetime(2026, 6, 12, 21, 35, tzinfo=timezone.utc)
    assert _vp_friday_flat_due(_flat_settings(), late) is False


def test_vp_friday_flat_disabled() -> None:
    from fx_momentum_bot.app.main import _vp_friday_flat_due
    fri = datetime(2026, 6, 12, 20, 50, tzinfo=timezone.utc)
    assert _vp_friday_flat_due(_flat_settings(enabled=False), fri) is False


# ─── Market-closed дедуп (баг-фикс 2026-06-15: спам DECAY CLOSE) ──────────


def test_is_market_closed_error_detects() -> None:
    from fx_momentum_bot.app.main import _is_market_closed_error
    assert _is_market_closed_error(
        "cTrader error MARKET_CLOSED: Trading is not available: Market is closed."
    ) is True


def test_is_market_closed_error_other_errors_false() -> None:
    from fx_momentum_bot.app.main import _is_market_closed_error
    assert _is_market_closed_error("SLIPPAGE guard rejected") is False
    assert _is_market_closed_error(None) is False
    assert _is_market_closed_error("") is False
