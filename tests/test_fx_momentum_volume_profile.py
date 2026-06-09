"""Тесты Volume Profile стратегии fx_momentum_bot.

Проверяем КОРРЕКТНОСТЬ АЛГОРИТМА (Steidlmayer/Dalton mechanics), а не
edge стратегии: профиль (POC/value area), детекторы failed-auction /
breakout на схематичных барах, target/RR, оконную нарезку сессии.
Это валидация реализации, не curve-fitting порогов
(.cursor/rules/no-data-fitting.mdc).
"""
from __future__ import annotations

import pandas as pd
import pytest

from fx_momentum_bot.config.settings import MomentumBotSettings
from fx_momentum_bot.strategy.volume_profile import (
    Profile,
    _detect_breakout,
    _detect_failed_auction,
    _target,
    build_signal,
    compute_profile,
    split_session_live,
)


def _bars(rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """rows = [(Open, High, Low, Close, Volume), ...] с RangeIndex."""
    return pd.DataFrame(
        rows, columns=["Open", "High", "Low", "Close", "Volume"]
    )


def _utc_bars(
    start: str, rows: list[tuple[float, float, float, float, float]], freq_min: int = 5
) -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=len(rows), freq=f"{freq_min}min", tz="UTC")
    df = _bars(rows)
    df.index = idx
    return df


# ─── compute_profile ────────────────────────────────────────────────────


def test_compute_profile_poc_and_value_area() -> None:
    # Объём сконцентрирован вокруг 105, редкие бары задают границы 100/110.
    rows = [(105.0, 105.2, 104.8, 105.0, 1000.0) for _ in range(40)]
    rows.append((100.1, 100.2, 100.0, 100.1, 10.0))   # session low
    rows.append((109.9, 110.0, 109.8, 109.9, 10.0))   # session high
    df = _bars(rows)
    prof = compute_profile(df, value_area_pct=0.70, num_bins=50)
    assert prof is not None
    assert prof.session_low == pytest.approx(100.0)
    assert prof.session_high == pytest.approx(110.0)
    # POC в кластере 105
    assert 104.7 <= prof.poc <= 105.3
    # value area окружает POC и узкая (почти весь объём в кластере)
    assert prof.val < prof.poc < prof.vah
    assert prof.val >= 104.0 and prof.vah <= 106.0


def test_compute_profile_empty_returns_none() -> None:
    assert compute_profile(_bars([]), value_area_pct=0.70, num_bins=50) is None


def test_compute_profile_zero_volume_returns_none() -> None:
    rows = [(105.0, 105.2, 104.8, 105.0, 0.0) for _ in range(10)]
    assert compute_profile(_bars(rows), value_area_pct=0.70, num_bins=50) is None


# ─── detectors ──────────────────────────────────────────────────────────


_PROF = Profile(
    poc=105.0, vah=105.2, val=104.8, shape="D",
    session_low=100.0, session_high=110.0, total_volume=1000.0,
)


def test_failed_auction_long_on_reclaim() -> None:
    live = _bars([
        (105.0, 105.1, 104.9, 105.0, 100.0),   # inside
        (104.7, 104.7, 104.4, 104.5, 100.0),   # breach below VAL
        (104.6, 105.0, 104.6, 104.95, 100.0),  # reclaim above VAL, < POC
    ])
    out = _detect_failed_auction(live, _PROF, breach_lookback=6, atr=0.1)
    assert out is not None
    direction, entry, sl = out
    assert direction == "long"
    assert entry == pytest.approx(104.95)
    assert sl < entry  # стоп ниже входа (за хвостом свипа)


def test_failed_auction_short_on_reclaim() -> None:
    live = _bars([
        (105.0, 105.1, 104.9, 105.0, 100.0),
        (105.3, 105.6, 105.3, 105.5, 100.0),   # breach above VAH
        (105.4, 105.4, 105.0, 105.05, 100.0),  # reclaim below VAH, > POC
    ])
    out = _detect_failed_auction(live, _PROF, breach_lookback=6, atr=0.1)
    assert out is not None
    direction, entry, sl = out
    assert direction == "short"
    assert sl > entry


def test_no_failed_auction_when_inside_value() -> None:
    live = _bars([
        (105.0, 105.1, 104.9, 105.0, 100.0),
        (104.95, 105.05, 104.9, 105.0, 100.0),
    ])
    assert _detect_failed_auction(live, _PROF, breach_lookback=6, atr=0.1) is None


def test_breakout_long_on_acceptance_then_break() -> None:
    live = _bars([
        (105.3, 105.55, 105.3, 105.5, 100.0),  # cons above VAH
        (105.5, 105.65, 105.45, 105.6, 100.0),
        (105.6, 105.75, 105.55, 105.7, 100.0),
        (105.7, 106.0, 105.7, 105.95, 100.0),  # breaks consolidation high
    ])
    out = _detect_breakout(live, _PROF, consolidation_bars=3, atr=0.1)
    assert out is not None
    direction, entry, sl = out
    assert direction == "long"
    assert entry == pytest.approx(105.95)
    assert sl < entry


def test_breakout_short_on_acceptance_then_break() -> None:
    live = _bars([
        (104.7, 104.7, 104.45, 104.5, 100.0),  # cons below VAL
        (104.5, 104.55, 104.35, 104.4, 100.0),
        (104.4, 104.45, 104.25, 104.3, 100.0),
        (104.3, 104.3, 104.0, 104.05, 100.0),  # breaks consolidation low
    ])
    out = _detect_breakout(live, _PROF, consolidation_bars=3, atr=0.1)
    assert out is not None
    direction, _entry, sl = out
    assert direction == "short"
    assert sl > _entry


# ─── target / RR ────────────────────────────────────────────────────────


def test_target_long_uses_va_edge_and_respects_rr() -> None:
    tp = _target("long", entry=104.95, sl=104.40, profile=_PROF, min_rr=1.5)
    assert tp is not None and tp > 104.95


def test_target_measured_move_fallback_meets_min_rr() -> None:
    # вход выше VAH (breakout): VA-цели нет → measured-move = entry + min_rr*risk
    entry, sl, min_rr = 105.15, 104.40, 1.5
    tp = _target("long", entry=entry, sl=sl, profile=_PROF, min_rr=min_rr)
    assert tp is not None
    assert (tp - entry) / (entry - sl) >= min_rr - 1e-9


# ─── session window split ───────────────────────────────────────────────


def test_split_session_live_windows_by_ny_time() -> None:
    # NY EDT = UTC-4 (июнь). 03:00 NY = 07:00 UTC, 07:00 NY = 11:00 UTC.
    rows = [(105.0, 105.1, 104.9, 105.0, 100.0) for _ in range(60)]
    df = _utc_bars("2026-06-09 06:30", rows)  # 02:30 NY .. далее
    session, live = split_session_live(
        df, tz="America/New_York", session_start="03:00", session_end="07:00"
    )
    assert not session.empty
    assert not live.empty
    # session целиком в [03:00, 07:00) NY
    s_local_times = session.index.tz_convert("America/New_York").time
    import datetime as _dt
    assert all(_dt.time(3, 0) <= t < _dt.time(7, 0) for t in s_local_times)
    # live строго после 07:00 NY
    l_local_times = live.index.tz_convert("America/New_York").time
    assert all(t >= _dt.time(7, 0) for t in l_local_times)


# ─── build_signal end-to-end ────────────────────────────────────────────


def test_build_signal_flat_before_session_formed() -> None:
    # только session-бары, live нет → flat / no_profile_or_live
    rows = [(105.0, 105.2, 104.8, 105.0, 1000.0) for _ in range(48)]
    df = _utc_bars("2026-06-09 07:00", rows)  # 03:00 NY, 4h → до 07:00 NY, live пуст
    sig = build_signal(
        df, tz="America/New_York", session_start="03:00", session_end="07:00",
        value_area_pct=0.70, num_bins=50, atr_period=14, min_rr=1.5,
        breach_lookback=6, consolidation_bars=6,
    )
    assert sig is not None
    assert sig.direction == "flat"


def test_build_signal_empty_returns_none() -> None:
    sig = build_signal(
        pd.DataFrame(), tz="America/New_York", session_start="03:00",
        session_end="07:00", value_area_pct=0.70, num_bins=50, atr_period=14,
        min_rr=1.5, breach_lookback=6, consolidation_bars=6,
    )
    assert sig is None


# ─── settings ───────────────────────────────────────────────────────────


def test_vp_disabled_by_default() -> None:
    s = MomentumBotSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.vp_symbols == ()
    assert s.vp_value_area_pct == 0.70
    assert s.vp_session_start == "03:00"
    assert s.vp_max_trades_per_dir_per_day == 2


def test_position_label_default() -> None:
    s = MomentumBotSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.position_label == "momentum-bot"


# ─── label isolation (общий счёт с fx_ai_trader) ────────────────────────

from types import SimpleNamespace  # noqa: E402

from fx_momentum_bot.app.main import (  # noqa: E402
    _collect_managed_positions,
    _count_open_positions_for_symbols,
)


class _FakeSymbols:
    def resolve_yfinance(self, sym: str):  # noqa: ANN001
        return SimpleNamespace(symbol_id=41, digits=2)  # GC=F → XAUUSD id=41


def _fake_pos(label: str, pos_id: int):
    # label/comment живут в ProtoOATradeData, НЕ на самой ProtoOAPosition.
    return SimpleNamespace(
        positionId=pos_id,
        price=4360.0,
        stopLoss=0.0,
        tradeData=SimpleNamespace(
            symbolId=41, tradeSide=1, volume=10000, label=label
        ),
    )


class _FakeExecutor:
    def __init__(self, positions: list) -> None:
        self.symbols = _FakeSymbols()
        self._positions = positions

    def get_open_positions(self) -> list:
        return self._positions


_OWN = frozenset({"momentum-bot", "fx-pro-bot"})


def test_momentum_ignores_foreign_label_positions_in_management() -> None:
    # На общем счёте: своя (momentum-bot) и чужая XAUUSD от AI (ai-fx-trader).
    ex = _FakeExecutor([
        _fake_pos("momentum-bot", 111),
        _fake_pos("ai-fx-trader", 222),  # позиция fx_ai_trader — не трогать!
    ])
    grouped = _collect_managed_positions(ex, ("GC=F",), labels=_OWN)  # type: ignore[arg-type]
    ids = [p.position_id for p in grouped["GC=F"]]
    assert ids == [111]  # позиция AI НЕ попала в управление


def test_momentum_adopts_legacy_label_but_not_ai() -> None:
    # legacy позиция бота (fx-pro-bot, открыта до миграции) берётся в
    # управление; позиция AI (ai-fx-trader) — нет.
    ex = _FakeExecutor([
        _fake_pos("momentum-bot", 111),
        _fake_pos("fx-pro-bot", 999),     # legacy своя — вести
        _fake_pos("ai-fx-trader", 222),   # чужая — игнор
    ])
    grouped = _collect_managed_positions(ex, ("GC=F",), labels=_OWN)  # type: ignore[arg-type]
    assert sorted(p.position_id for p in grouped["GC=F"]) == [111, 999]


def test_momentum_counts_only_own_labels() -> None:
    ex = _FakeExecutor([
        _fake_pos("momentum-bot", 111),
        _fake_pos("fx-pro-bot", 999),
        _fake_pos("ai-fx-trader", 222),
        _fake_pos("ai-fx-trader", 333),
    ])
    assert _count_open_positions_for_symbols(ex, ("GC=F",), labels=_OWN) == 2  # type: ignore[arg-type]


def test_managed_labels_property() -> None:
    s = MomentumBotSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.managed_labels == frozenset({"momentum-bot", "fx-pro-bot"})


def test_all_symbols_unions_momentum_and_vp() -> None:
    s = MomentumBotSettings(
        _env_file=None,  # type: ignore[call-arg]
        MOMENTUM_BOT_SYMBOLS="EURUSD=X,GBPUSD=X",
        MOMENTUM_BOT_VP_SYMBOLS="GC=F",
    )
    assert s.vp_symbols == ("GC=F",)
    assert set(s.all_symbols) == {"EURUSD=X", "GBPUSD=X", "GC=F"}
