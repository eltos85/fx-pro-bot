from datetime import datetime, timezone

import pandas as pd
import pytest

from fx_momentum_bot.app.main import (
    ManagedPosition,
    _calc_partial_close_volume,
    _drop_forming_bar,
    _flip_close_targets,
    _has_same_side_position,
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


def test_has_same_side_position_blocks_duplicate_entry() -> None:
    # Per-symbol гард (BUILDLOG 2026-07-10): при живой long-позиции повторный
    # long-сигнал (дребезг threshold / retry после slippage-guard) — дубль.
    assert _has_same_side_position([_pos(1, "long")], "long")
    assert _has_same_side_position([_pos(1, "short"), _pos(2, "long")], "long")


def test_has_same_side_position_allows_opposite_or_empty() -> None:
    # Противоположная позиция не блокирует: её закроет sign-decay/флип.
    assert not _has_same_side_position([_pos(1, "short")], "long")
    assert not _has_same_side_position([], "long")
    assert not _has_same_side_position([_pos(1, "long")], "flat")


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


def test_sign_direction_hysteresis_dead_zone_keeps_position() -> None:
    # Гистерезис (BUILDLOG 2026-07-24): momentum в мёртвой зоне (-T, +T) →
    # ни одна сторона не «жива» → sign-decay НЕ закрывает ничего. Победитель
    # получает room до реального разворота за -T.
    positions = [_pos(1, "long"), _pos(2, "short")]
    T = 0.0015
    # momentum = -0.0004: |m| < T → мёртвая зона → "" → ничего не закрывается.
    sign_dir = _momentum_sign_direction(-0.0004, threshold=T)
    assert sign_dir == ""
    assert _flip_close_targets(positions, sign_dir) == []


def test_sign_direction_hysteresis_closes_only_beyond_threshold() -> None:
    # momentum < -T → «short» жива → закрываем лонги (side != short).
    # momentum > +T → «long» жива → закрываем шорты.
    positions = [_pos(1, "long"), _pos(2, "short")]
    T = 0.0015
    assert _momentum_sign_direction(-0.0020, threshold=T) == "short"
    assert _momentum_sign_direction(0.0020, threshold=T) == "long"
    # За -T: закрываются лонги (id=1), шорт (id=2) живёт.
    targets = _flip_close_targets(positions, _momentum_sign_direction(-0.0020, T))
    assert [p.position_id for p in targets] == [1]
    # За +T: закрываются шорты (id=2), лонг (id=1) живёт.
    targets = _flip_close_targets(positions, _momentum_sign_direction(0.0020, T))
    assert [p.position_id for p in targets] == [2]


def test_sign_direction_hysteresis_zero_mult_preserves_old_behavior() -> None:
    # mult=0 (threshold=0) → чистый sign-rule: любое ненулевое momentum
    # определяет «живую» сторону (старое поведение до гистерезиса).
    assert _momentum_sign_direction(0.0001, threshold=0.0) == "long"
    assert _momentum_sign_direction(-0.0001, threshold=0.0) == "short"
    assert _momentum_sign_direction(0.0, threshold=0.0) == ""


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


# ─── Gap-защита: high_impact_event_upcoming (BUILDLOG 2026-07-24) ───────────

def test_news_close_upcoming_within_window() -> None:
    from fx_momentum_bot.strategy.event_guard import high_impact_event_upcoming
    # CPI 2026-06-10 12:30 UTC. За 3 мин до релиза, before_min=5 → upcoming.
    reason = high_impact_event_upcoming(_at(12, 27), before_min=5)
    assert reason is not None and "CPI" in reason and "in 3min" in reason


def test_news_close_upcoming_outside_window() -> None:
    from fx_momentum_bot.strategy.event_guard import high_impact_event_upcoming
    # За 10 мин до релиза, before_min=5 → НЕ upcoming (слишком далеко).
    assert high_impact_event_upcoming(_at(12, 20), before_min=5) is None
    # После релиза (12:40) → НЕ upcoming (окно строго [now, now+before]).
    assert high_impact_event_upcoming(_at(12, 40), before_min=5) is None


def test_news_close_upcoming_symbol_scoping() -> None:
    from fx_momentum_bot.strategy.event_guard import high_impact_event_upcoming
    # ECB 2026-06-11 12:15 UTC: upcoming для EUR-пар, не для JPY/золота.
    ecb_near = datetime(2026, 6, 11, 12, 10, tzinfo=timezone.utc)
    assert high_impact_event_upcoming(ecb_near, symbol="EURUSD=X", before_min=10) is not None
    assert high_impact_event_upcoming(ecb_near, symbol="USDJPY=X", before_min=10) is None
    assert high_impact_event_upcoming(ecb_near, symbol="GC=F", before_min=10) is None


def test_news_close_upcoming_disabled_when_before_zero() -> None:
    from fx_momentum_bot.strategy.event_guard import high_impact_event_upcoming
    # before_min=0 → окно пустое → никогда не upcoming (выключено).
    assert high_impact_event_upcoming(_at(12, 29), before_min=0) is None


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


# ─── Session-фильтр (liquid sessions only, 2026-06-26) ─────────────────


def test_session_filter_blocks_asian_session() -> None:
    from fx_momentum_bot.strategy.session_filter import session_skip_reason
    # Asia 00–07 UTC вне ликвидного окна [07,21) → блок
    for h in (0, 1, 3, 5, 6):
        assert session_skip_reason(
            hour_utc=h, enabled=True, start_hour_utc=7, end_hour_utc=21
        ) is not None, f"hour {h} должен блокироваться"


def test_session_filter_blocks_late_session() -> None:
    from fx_momentum_bot.strategy.session_filter import session_skip_reason
    # Late 21–24 UTC вне окна
    for h in (21, 22, 23):
        assert session_skip_reason(
            hour_utc=h, enabled=True, start_hour_utc=7, end_hour_utc=21
        ) is not None


def test_session_filter_allows_liquid_sessions() -> None:
    from fx_momentum_bot.strategy.session_filter import session_skip_reason
    # London 07–12 + NY 12–21 (полуоткрытый [7,21)) → разрешено
    for h in (7, 10, 12, 15, 19, 20):
        assert session_skip_reason(
            hour_utc=h, enabled=True, start_hour_utc=7, end_hour_utc=21
        ) is None, f"hour {h} должен быть разрешён"


def test_session_filter_disabled() -> None:
    from fx_momentum_bot.strategy.session_filter import session_skip_reason
    # enabled=False → не блокирует никогда
    assert session_skip_reason(
        hour_utc=3, enabled=False, start_hour_utc=7, end_hour_utc=21
    ) is None


def test_session_filter_degenerate_window_disables() -> None:
    from fx_momentum_bot.strategy.session_filter import session_skip_reason
    # start==end → фильтр выключен (не блокирует)
    assert session_skip_reason(
        hour_utc=3, enabled=True, start_hour_utc=7, end_hour_utc=7
    ) is None


def test_session_filter_wraparound_window() -> None:
    from fx_momentum_bot.strategy.session_filter import session_skip_reason
    # Обёртка через полночь: окно [21,7) → night-only, день блокируется
    assert session_skip_reason(
        hour_utc=23, enabled=True, start_hour_utc=21, end_hour_utc=7
    ) is None
    assert session_skip_reason(
        hour_utc=2, enabled=True, start_hour_utc=21, end_hour_utc=7
    ) is None
    assert session_skip_reason(
        hour_utc=12, enabled=True, start_hour_utc=21, end_hour_utc=7
    ) is not None


# ─── NY-open block: блок входов в конкретные часы UTC (BUILDLOG 2026-07-24) ──

def test_ny_open_block_blocks_listed_hours() -> None:
    from fx_momentum_bot.strategy.session_filter import hour_blocklist_skip_reason
    # 14,15,16 UTC — враждебные NY-open часы (loss-audit 34 сделки, WR 0-20%).
    for h in (14, 15, 16):
        assert hour_blocklist_skip_reason(
            hour_utc=h, enabled=True, blocked_hours=(14, 15, 16)
        ) is not None, f"hour {h} должен блокироваться"


def test_ny_open_block_allows_other_hours() -> None:
    from fx_momentum_bot.strategy.session_filter import hour_blocklist_skip_reason
    # London-open 08h, mid-London 12h — разрешены.
    for h in (7, 8, 10, 12, 17, 19, 20):
        assert hour_blocklist_skip_reason(
            hour_utc=h, enabled=True, blocked_hours=(14, 15, 16)
        ) is None, f"hour {h} должен быть разрешён"


def test_ny_open_block_disabled_and_empty() -> None:
    from fx_momentum_bot.strategy.session_filter import hour_blocklist_skip_reason
    # enabled=False → не блокирует даже listed часы.
    assert hour_blocklist_skip_reason(
        hour_utc=15, enabled=False, blocked_hours=(14, 15, 16)
    ) is None
    # Пустой список → не блокирует.
    assert hour_blocklist_skip_reason(
        hour_utc=15, enabled=True, blocked_hours=()
    ) is None


# ─── Friday-flat (закрытие перед выходными, 2026-06-26) ─────────────────


def _fri(hour: int, minute: int = 0) -> datetime:
    # Пятница 2026-06-26 (weekday()==4)
    return datetime(2026, 6, 26, hour, minute, tzinfo=timezone.utc)


def _other_day(hour: int, minute: int = 0) -> datetime:
    # Четверг 2026-06-25 (weekday()==3)
    return datetime(2026, 6, 25, hour, minute, tzinfo=timezone.utc)


def test_friday_flat_due_inside_window() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_flat_due
    for h, m in [(20, 0), (20, 15), (20, 44)]:
        assert friday_flat_due(
            enabled=True, flat_start="20:00", flat_end="20:45",
            now_utc=_fri(h, m),
        ) is True, f"{h}:{m} должен быть в окне"


def test_friday_flat_due_outside_window() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_flat_due
    # До окна и после
    assert friday_flat_due(
        enabled=True, flat_start="20:00", flat_end="20:45",
        now_utc=_fri(19, 59),
    ) is False
    assert friday_flat_due(
        enabled=True, flat_start="20:00", flat_end="20:45",
        now_utc=_fri(20, 45),
    ) is False
    assert friday_flat_due(
        enabled=True, flat_start="20:00", flat_end="20:45",
        now_utc=_fri(12, 0),
    ) is False


def test_friday_flat_due_only_friday() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_flat_due
    # Тот же час, но другой день недели → не срабатывает
    assert friday_flat_due(
        enabled=True, flat_start="20:00", flat_end="20:45",
        now_utc=_other_day(20, 15),
    ) is False


def test_friday_flat_due_disabled() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_flat_due
    assert friday_flat_due(
        enabled=False, flat_start="20:00", flat_end="20:45",
        now_utc=_fri(20, 15),
    ) is False


def test_friday_flat_due_degenerate_window() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_flat_due
    # start==end → правило выключено
    assert friday_flat_due(
        enabled=True, flat_start="20:00", flat_end="20:00",
        now_utc=_fri(20, 15),
    ) is False


def test_friday_flat_due_bad_config_disables() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_flat_due
    # Плохой формат → False (правило выключается, не блокирует торговлю)
    assert friday_flat_due(
        enabled=True, flat_start="bad", flat_end="20:45",
        now_utc=_fri(20, 15),
    ) is False
    assert friday_flat_due(
        enabled=True, flat_start="20:00", flat_end="not-a-time",
        now_utc=_fri(20, 15),
    ) is False


# ─── friday_entry_blocked: блок входов от flat_start до конца пятницы ────────
# Дыра 2026-06-26 (BUILDLOG 2026-07-02): окно flat [20:00, 20:45) запрещало
# входы только внутри себя — вход в 20:45–21:00 уезжал в выходные, а после
# 21:00 бот спамил MARKET_CLOSED каждые 5 минут.


def test_friday_entry_blocked_from_flat_start_to_midnight() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_entry_blocked
    # Внутри окна flat, в дыре 20:45–21:00 и после закрытия рынка — блок
    for h, m in [(20, 0), (20, 44), (20, 45), (20, 59), (21, 3), (23, 59)]:
        assert friday_entry_blocked(
            enabled=True, flat_start="20:00", now_utc=_fri(h, m),
        ) is True, f"{h}:{m} пятницы должен блокировать вход"


def test_friday_entry_blocked_before_flat_start() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_entry_blocked
    for h, m in [(0, 0), (12, 0), (19, 59)]:
        assert friday_entry_blocked(
            enabled=True, flat_start="20:00", now_utc=_fri(h, m),
        ) is False, f"{h}:{m} пятницы до flat_start вход разрешён"


def test_friday_entry_blocked_only_friday() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_entry_blocked
    assert friday_entry_blocked(
        enabled=True, flat_start="20:00", now_utc=_other_day(21, 0),
    ) is False


def test_friday_entry_blocked_disabled_or_bad_config() -> None:
    from fx_momentum_bot.strategy.friday_flat import friday_entry_blocked
    assert friday_entry_blocked(
        enabled=False, flat_start="20:00", now_utc=_fri(21, 0),
    ) is False
    assert friday_entry_blocked(
        enabled=True, flat_start="oops", now_utc=_fri(21, 0),
    ) is False


# ─── context_metrics: метрики контекста входа (observability) ────────────────
# BUILDLOG 2026-07-03: проверяем МАТЕМАТИКУ метрик (детерминированные ряды),
# не торговое поведение — метрики на торговлю не влияют по построению.


def _trend_df(n: int = 300, step: float = 0.001) -> pd.DataFrame:
    """Монотонный ап-тренд: close заведомо выше EMA200, ADX высокий."""
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    close = pd.Series([1.0 + step * i for i in range(n)], index=idx)
    return pd.DataFrame({
        "Open": close - step / 2,
        "High": close + step,
        "Low": close - step,
        "Close": close,
        "Volume": [100] * n,
    })


def test_entry_context_uptrend_long_is_with_htf() -> None:
    from fx_momentum_bot.strategy.context_metrics import compute_entry_context
    ctx = compute_entry_context(_trend_df(), "long")
    assert ctx is not None
    assert ctx.ema_dist_atr > 0          # цена выше EMA200
    assert ctx.with_htf is True          # long по стороне тренда
    assert ctx.adx > 25                  # монотонный тренд → высокий ADX


def test_entry_context_uptrend_short_is_counter() -> None:
    from fx_momentum_bot.strategy.context_metrics import compute_entry_context
    ctx = compute_entry_context(_trend_df(), "short")
    assert ctx is not None
    assert ctx.with_htf is False


def test_entry_context_flat_direction_has_no_htf_flag() -> None:
    from fx_momentum_bot.strategy.context_metrics import compute_entry_context
    ctx = compute_entry_context(_trend_df(), "flat")
    assert ctx is not None
    assert ctx.with_htf is None


def test_entry_context_not_enough_bars_returns_none() -> None:
    from fx_momentum_bot.strategy.context_metrics import compute_entry_context
    assert compute_entry_context(_trend_df(n=150), "long") is None
    assert compute_entry_context(None, "long") is None


# ─── ADX-фильтр входа (BUILDLOG 2026-07-24) ─────────────────────────────────

def test_adx_block_reason_blocks_range() -> None:
    from fx_momentum_bot.strategy.context_metrics import (
        EntryContext,
        adx_block_reason,
    )
    # ADX=15 < 20 → рейндж → вход блокируется.
    ctx = EntryContext(ema_dist_atr=0.5, adx=15.0, with_htf=True)
    reason = adx_block_reason(ctx, enabled=True, adx_min=20.0)
    assert reason is not None and "low_adx" in reason and "15.0" in reason


def test_adx_block_reason_allows_trend() -> None:
    from fx_momentum_bot.strategy.context_metrics import (
        EntryContext,
        adx_block_reason,
    )
    # ADX=25 >= 20 → трендовость есть → вход разрешён.
    ctx = EntryContext(ema_dist_atr=0.5, adx=25.0, with_htf=True)
    assert adx_block_reason(ctx, enabled=True, adx_min=20.0) is None


def test_adx_block_reason_none_ctx_not_blocked() -> None:
    from fx_momentum_bot.strategy.context_metrics import adx_block_reason
    # ctx=None (холодный старт / мало данных) → НЕ блокировать.
    assert adx_block_reason(None, enabled=True, adx_min=20.0) is None


def test_adx_block_reason_disabled() -> None:
    from fx_momentum_bot.strategy.context_metrics import (
        EntryContext,
        adx_block_reason,
    )
    # enabled=False → не блокирует даже в рейндже.
    ctx = EntryContext(ema_dist_atr=0.5, adx=10.0, with_htf=True)
    assert adx_block_reason(ctx, enabled=False, adx_min=20.0) is None


# ─── store: миграция ctx_* колонок и персист контекста ────────────────────────


def test_store_ctx_columns_migrate_and_persist(tmp_path) -> None:
    import sqlite3

    from fx_momentum_bot.state.store import MomentumStore

    db = tmp_path / "momentum_bot.sqlite"
    # Старая схема БЕЗ ctx_* — как на VPS до деплоя
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE momentum_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), symbol TEXT NOT NULL, "
        "direction TEXT NOT NULL, momentum_value REAL NOT NULL, atr REAL NOT NULL, "
        "close_price REAL NOT NULL, executed INTEGER NOT NULL, "
        "note TEXT NOT NULL DEFAULT '')")
    conn.execute(
        "INSERT INTO momentum_decisions(symbol,direction,momentum_value,atr,"
        "close_price,executed,note) VALUES ('EURUSD=X','long',0.005,0.0004,1.1,1,'old')")
    conn.commit()
    conn.close()

    store = MomentumStore(db)  # _init_db мигрирует схему
    store.add_decision(
        symbol="GBPUSD=X", direction="short", momentum_value=-0.004,
        atr=0.0009, close_price=1.27, executed=True, note="live_open:ok",
        ctx_ema_dist_atr=-3.2, ctx_adx=27.5, ctx_with_htf=True,
        ctx_spread_pips=0.6,
    )
    # Повторная инициализация не падает (колонки уже есть)
    MomentumStore(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM momentum_decisions ORDER BY id").fetchall()
    conn.close()
    assert rows[0]["ctx_ema_dist_atr"] is None  # старая строка — NULL
    new = rows[1]
    assert new["ctx_ema_dist_atr"] == pytest.approx(-3.2)
    assert new["ctx_adx"] == pytest.approx(27.5)
    assert new["ctx_with_htf"] == 1
    assert new["ctx_spread_pips"] == pytest.approx(0.6)


def test_store_ctx_defaults_are_null(tmp_path) -> None:
    import sqlite3

    from fx_momentum_bot.state.store import MomentumStore

    db = tmp_path / "momentum_bot.sqlite"
    store = MomentumStore(db)
    store.add_decision(
        symbol="EURUSD=X", direction="flat", momentum_value=0.0,
        atr=0.0, close_price=0.0, executed=False, note="not_enough_data",
    )
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM momentum_decisions").fetchone()
    conn.close()
    assert row["ctx_ema_dist_atr"] is None
    assert row["ctx_with_htf"] is None



# ─── Гейт «позиции неизвестны» + монотонность SL (BUILDLOG 2026-07-31) ──


class _StubSymbolInfo:
    def __init__(self, symbol_id: int, digits: int = 5) -> None:
        self.symbol_id = symbol_id
        self.digits = digits


class _StubSymbols:
    def resolve_yfinance(self, yf_symbol: str):
        return _StubSymbolInfo(symbol_id=1)


class _StubExecutor:
    """Executor-заглушка: reconcile либо отдаёт позиции, либо «не знаю»."""

    def __init__(self, positions: list | None) -> None:
        self._positions = positions
        self.symbols = _StubSymbols()

    def try_get_open_positions(self) -> list | None:
        return self._positions


def test_collect_managed_positions_none_on_failed_reconcile() -> None:
    # reconcile не ответил → None, НЕ пустой dict: иначе cleanup сотрёт
    # state живых позиций (VPS 2026-07-31 00:19, таймаут type=2125).
    from fx_momentum_bot.app.main import _collect_managed_positions

    result = _collect_managed_positions(
        _StubExecutor(None), ("EURUSD=X",), labels=frozenset({"momentum-bot"})
    )
    assert result is None


def test_collect_managed_positions_empty_dict_when_no_positions() -> None:
    from fx_momentum_bot.app.main import _collect_managed_positions

    result = _collect_managed_positions(
        _StubExecutor([]), ("EURUSD=X",), labels=frozenset({"momentum-bot"})
    )
    assert result == {"EURUSD=X": []}


def test_count_open_positions_none_on_failed_reconcile() -> None:
    from fx_momentum_bot.app.main import _count_open_positions_for_symbols

    counted = _count_open_positions_for_symbols(
        _StubExecutor(None), ("EURUSD=X",), labels=frozenset({"momentum-bot"})
    )
    assert counted is None


def test_count_open_positions_zero_when_no_positions() -> None:
    from fx_momentum_bot.app.main import _count_open_positions_for_symbols

    counted = _count_open_positions_for_symbols(
        _StubExecutor([]), ("EURUSD=X",), labels=frozenset({"momentum-bot"})
    )
    assert counted == 0


@pytest.mark.parametrize(
    "side,current_sl,target_sl,expected",
    [
        # long: трейленный SL в профите выше entry → BE не откатывает
        ("long", 1.34410, 1.33397, True),
        ("long", 1.32000, 1.33397, False),
        ("long", 1.33397, 1.33397, True),
        # short: SL в профите НИЖЕ entry
        ("short", 160.10, 163.52, True),
        ("short", 164.00, 163.52, False),
        # стопа нет → ставить можно
        ("long", None, 1.33397, False),
        ("short", 0.0, 163.52, False),
    ],
)
def test_sl_at_least_as_good(side, current_sl, target_sl, expected) -> None:
    from fx_momentum_bot.app.main import _sl_at_least_as_good

    assert (
        _sl_at_least_as_good(side, current_sl=current_sl, target_sl=target_sl)
        is expected
    )
