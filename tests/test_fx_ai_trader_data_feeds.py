"""Tests для Enhancements A/B/C (2026-05-29): COT / FRED real-yields / VIX.

Покрывает чистую логику (форматтеры, dataclass-проперти, symbol-map,
parsing) без сетевых вызовов. Сетевые fetch'и мокаются monkeypatch'ем
requests/yfinance, где нужно проверить парсинг.

Compliance: no-data-fitting.mdc (новые data-feeds, не подгонка стратегии),
api-docs.mdc (CFTC resource 72hh-3qpy + FRED series_observations —
официальные endpoint'ы, проверены против live API).
"""
from __future__ import annotations

import pytest


# ─── B: FRED real-yields (macro_rates) ─────────────────────────────────


class TestMacroRatesFred:
    def test_format_includes_real_yield_first(self):
        from fx_ai_trader.data.macro_rates import (
            MacroRatesSnapshot,
            format_macro_rates_snapshot,
        )
        snap = MacroRatesSnapshot(
            dxy_last=104.2, dxy_change_24h_pct=0.1, dxy_change_5d_pct=-0.3,
            ust10y_last_pct=4.31, ust10y_change_24h_bps=-2.6,
            ust10y_change_5d_bps=-11.7,
            tip_last=110.5, tip_change_24h_pct=0.27, tip_change_5d_pct=0.73,
            real_yield_10y_pct=2.09, real_yield_10y_change_24h_bps=-1.0,
            real_yield_10y_change_5d_bps=-7.0,
            breakeven_10y_pct=2.22, breakeven_10y_change_24h_bps=0.5,
            fetched_at_utc="2026-05-29T08:00:00+00:00",
        )
        out = format_macro_rates_snapshot(snap)
        assert "10Y REAL YIELD" in out
        assert "DFII10" in out
        assert "2.09%" in out
        assert "BREAKEVEN" in out
        # real yield идёт ПЕРЕД DXY-телом (gold-driver #1)
        assert out.index("10Y REAL YIELD") < out.index("DXY (US Dollar Index")

    def test_format_without_fred_keeps_tip_proxy(self):
        """Без FRED (real_yield None) блок всё равно строится на DXY/TIP."""
        from fx_ai_trader.data.macro_rates import (
            MacroRatesSnapshot,
            format_macro_rates_snapshot,
        )
        snap = MacroRatesSnapshot(
            dxy_last=104.2, dxy_change_24h_pct=0.1, dxy_change_5d_pct=-0.3,
            ust10y_last_pct=4.31, ust10y_change_24h_bps=-2.6,
            ust10y_change_5d_bps=-11.7,
            tip_last=110.5, tip_change_24h_pct=0.27, tip_change_5d_pct=0.73,
            fetched_at_utc="2026-05-29T08:00:00+00:00",
        )
        out = format_macro_rates_snapshot(snap)
        assert "10Y REAL YIELD" not in out
        assert "DXY" in out
        assert "TIP" in out

    def test_provider_without_key_skips_fred(self, monkeypatch):
        """Пустой fred_api_key → _fetch_fred_bps не вызывается."""
        from fx_ai_trader.data import macro_rates

        calls = []
        monkeypatch.setattr(
            macro_rates, "_fetch_fred_bps",
            lambda sid, key: calls.append(sid) or {"last": None, "bps_24h": None, "bps_5d": None},
        )
        monkeypatch.setattr(
            macro_rates, "_fetch_pct_series",
            lambda t: {"last": 100.0, "pct_24h": 0.1, "pct_5d": 0.2,
                       "prev_close_24h": 99.9, "prev_close_5d": 99.8},
        )
        prov = macro_rates.MacroRatesProvider(fred_api_key="")
        prov._fetch_fresh()
        assert calls == []  # FRED не дёргали

    def test_fetch_fred_bps_parses_desc_and_skips_dots(self, monkeypatch):
        from fx_ai_trader.data import macro_rates

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                # desc order: новейшее первым; "." = пропуск
                return {"observations": [
                    {"value": "2.09"}, {"value": "."}, {"value": "2.10"},
                    {"value": "2.12"}, {"value": "2.14"}, {"value": "2.16"},
                ]}

        # requests импортируется внутри функции → патчим sys.modules
        import sys
        import types
        fake = types.ModuleType("requests")
        fake.get = lambda *a, **k: _Resp()
        monkeypatch.setitem(sys.modules, "requests", fake)
        out = macro_rates._fetch_fred_bps("DFII10", "KEY")
        assert out["last"] == 2.09
        # 24h vs следующая валидная (2.10): (2.09-2.10)*100 = -1.0 bps
        assert out["bps_24h"] == pytest.approx(-1.0, abs=0.01)


# ─── C: VIX risk regime ────────────────────────────────────────────────


class TestRiskRegime:
    def test_format_includes_vix(self):
        from fx_ai_trader.data.risk_regime import (
            RiskRegimeSnapshot,
            format_risk_regime_snapshot,
        )
        snap = RiskRegimeSnapshot(
            vix_last=17.3, vix_change_24h_pct=-4.1, vix_change_5d_pct=8.0,
            fetched_at_utc="2026-05-29T08:00:00+00:00",
        )
        out = format_risk_regime_snapshot(snap)
        assert "RISK REGIME" in out
        assert "VIX: 17.30" in out
        assert "safe-haven" in out

    def test_format_none_when_no_data(self):
        from fx_ai_trader.data.risk_regime import format_risk_regime_snapshot
        assert format_risk_regime_snapshot(None) is None


# ─── A: CFTC COT ───────────────────────────────────────────────────────


class TestCot:
    def _snap(self, **kw):
        from fx_ai_trader.data.cot import CotSnapshot
        base = dict(
            symbol="XAUUSD", market_name="GOLD - COMMODITY EXCHANGE INC.",
            report_date="2026-05-19", open_interest=379325,
            mm_long=122894, mm_short=29354, mm_net=93540, mm_net_prev=98015,
        )
        base.update(kw)
        return CotSnapshot(**base)

    def test_net_change_and_share(self):
        s = self._snap()
        assert s.mm_net_change == 93540 - 98015  # -4475
        assert s.net_share_of_oi_pct == pytest.approx(93540 / 379325 * 100, abs=0.01)

    def test_net_change_none_when_no_prev(self):
        s = self._snap(mm_net_prev=None)
        assert s.mm_net_change is None

    def test_format_net_long_short_and_brent_proxy(self):
        from fx_ai_trader.data.cot import format_cot_snapshots
        gold = self._snap()
        ng = self._snap(
            symbol="NG=F", market_name="NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE",
            mm_long=188978, mm_short=285281, mm_net=-96303, mm_net_prev=-120065,
            open_interest=1599429,
        )
        brent = self._snap(
            symbol="BZ=F", market_name="BRENT LAST DAY - NEW YORK MERCANTILE EXCHANGE",
            mm_long=16948, mm_short=4018, mm_net=12930, mm_net_prev=15063,
            open_interest=243770,
        )
        out = format_cot_snapshots({"XAUUSD": gold, "BZ=F": brent, "NG=F": ng})
        assert "MANAGED MONEY" in out
        assert "net-LONG +93540" in out
        assert "net-SHORT -96303" in out
        assert "[NYMEX Brent proxy]" in out  # только Brent помечен proxy
        assert "Δwk=" in out

    def test_format_none_when_empty(self):
        from fx_ai_trader.data.cot import format_cot_snapshots
        assert format_cot_snapshots({}) is None

    def test_symbol_map_has_three_instruments(self):
        from fx_ai_trader.data.cot import _SYMBOL_TO_MARKET
        assert set(_SYMBOL_TO_MARKET) == {"XAUUSD", "BZ=F", "NG=F"}

    def test_fetch_one_parses_two_rows(self, monkeypatch):
        import sys
        import types
        from fx_ai_trader.data import cot

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return [
                    {"report_date_as_yyyy_mm_dd": "2026-05-19T00:00:00.000",
                     "open_interest_all": "379325",
                     "m_money_positions_long_all": "122894",
                     "m_money_positions_short_all": "29354"},
                    {"report_date_as_yyyy_mm_dd": "2026-05-12T00:00:00.000",
                     "open_interest_all": "376496",
                     "m_money_positions_long_all": "127242",
                     "m_money_positions_short_all": "29227"},
                ]

        fake = types.ModuleType("requests")
        fake.get = lambda *a, **k: _Resp()
        monkeypatch.setitem(sys.modules, "requests", fake)
        prov = cot.CotProvider()
        snap = prov._fetch_one("XAUUSD", "GOLD - COMMODITY EXCHANGE INC.")
        assert snap.mm_net == 122894 - 29354
        assert snap.mm_net_prev == 127242 - 29227
        assert snap.report_date == "2026-05-19"


# ─── D: GDELT news tone ────────────────────────────────────────────────


class TestGdelt:
    def test_classify_trend(self):
        from fx_ai_trader.news.gdelt import _classify_trend
        assert _classify_trend([0, 0, 0, 1, 2, 3]) == "improving"
        assert _classify_trend([3, 2, 1, 0, -1, -2]) == "deteriorating"
        assert _classify_trend([1, 1, 1, 1, 1, 1]) == "stable"
        assert _classify_trend([1, 2]) == "stable"  # < 6 точек

    def test_format_includes_tone(self):
        from fx_ai_trader.news.gdelt import (
            GdeltToneSnapshot,
            format_gdelt_snapshots,
        )
        snaps = {
            "XAUUSD": GdeltToneSnapshot("XAUUSD", 1.2, 0.8, 200, "improving"),
            "NG=F": GdeltToneSnapshot("NG=F", -0.5, -1.1, 150, "deteriorating"),
        }
        out = format_gdelt_snapshots(snaps)
        assert "GDELT NEWS TONE" in out
        assert "[XAUUSD] avg=+1.20 latest=+0.80 trend=improving" in out
        assert "[NG=F] avg=-0.50 latest=-1.10 trend=deteriorating" in out

    def test_format_none_when_empty(self):
        from fx_ai_trader.news.gdelt import format_gdelt_snapshots
        assert format_gdelt_snapshots({}) is None

    def test_fetch_one_parses_timeline(self, monkeypatch):
        import sys
        import types
        from fx_ai_trader.news import gdelt

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"timeline": [{"series": "Average Tone", "data": [
                    {"date": "20260526T100000Z", "value": 0.5},
                    {"date": "20260526T101500Z", "value": 1.0},
                    {"date": "20260526T103000Z", "value": 1.5},
                ]}]}

        fake = types.ModuleType("requests")
        fake.get = lambda *a, **k: _Resp()
        monkeypatch.setitem(sys.modules, "requests", fake)
        snap = gdelt.GdeltProvider()._fetch_one("XAUUSD", "q")
        assert snap.latest_tone == 1.5
        assert snap.avg_tone == pytest.approx(1.0, abs=0.01)
        assert snap.n_points == 3


# ─── E: economic calendar ──────────────────────────────────────────────


class TestEconCalendar:
    def _now(self, y, m, d, hh=0, mm=0):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("UTC"))

    def test_next_nfp_is_first_friday(self):
        from fx_ai_trader.data.econ_calendar import _next_nfp
        # 2026-06-01 (Mon) → первая пятница июня = 2026-06-05
        nxt = _next_nfp(self._now(2026, 6, 1))
        assert nxt.astimezone(__import__("zoneinfo").ZoneInfo(
            "America/New_York")).date().isoformat() == "2026-06-05"

    def test_upcoming_picks_next_fomc_and_cpi(self):
        from fx_ai_trader.data.econ_calendar import upcoming_events
        # now = 2026-05-29 → ближайший CPI 2026-06-10, FOMC 2026-06-17
        events = upcoming_events(
            self._now(2026, 5, 29, 12), ("XAUUSD",), horizon_hours=24 * 30
        )
        names = [e.name for e in events]
        assert "US CPI" in names
        assert "FOMC decision" in names
        # отсортировано по времени
        times = [e.when_utc for e in events]
        assert times == sorted(times)

    def test_horizon_filters_far_events(self):
        from fx_ai_trader.data.econ_calendar import upcoming_events
        # узкое окно 2h на спокойную дату → пусто
        events = upcoming_events(
            self._now(2026, 5, 29, 3), ("XAUUSD",), horizon_hours=2
        )
        assert events == []

    def test_eia_petroleum_only_for_brent(self):
        from fx_ai_trader.data.econ_calendar import upcoming_events
        events = upcoming_events(
            self._now(2026, 5, 25, 0), ("BZ=F",), horizon_hours=24 * 7
        )
        names = [e.name for e in events]
        assert "EIA Weekly Petroleum Status" in names
        # для XAUUSD-only нефтяного EIA быть не должно
        gold_events = upcoming_events(
            self._now(2026, 5, 25, 0), ("XAUUSD",), horizon_hours=24 * 7
        )
        assert "EIA Weekly Petroleum Status" not in [
            e.name for e in gold_events
        ]

    def test_format_block(self):
        from fx_ai_trader.data.econ_calendar import (
            EconCalendarProvider,
        )
        prov = EconCalendarProvider(horizon_hours=24 * 30)
        block = prov.get_block(("XAUUSD",), now_utc=self._now(2026, 5, 29, 12))
        assert "ECONOMIC CALENDAR" in block
        assert "in " in block


# ─── Context wiring ────────────────────────────────────────────────────


class TestContextBlocks:
    def test_format_context_includes_new_blocks(self):
        from fx_ai_trader.trading.context import (
            MarketContext,
            format_context_for_prompt,
        )
        ctx = MarketContext(
            snapshots=[], open_positions=[], virtual_capital_usd=500.0,
            macro_rates_block="=== US MACRO RATES ===\n10Y REAL YIELD ...",
            risk_regime_block="=== RISK REGIME (CBOE VIX) ===\nVIX: 17.30",
            cot_block="=== CFTC COT — MANAGED MONEY ===\n[XAUUSD] net-LONG +93540",
            gdelt_block="=== GDELT NEWS TONE ===\n[XAUUSD] avg=+1.20",
            econ_calendar_block="=== ECONOMIC CALENDAR ===\n[HIGH] US CPI in 11.5h",
        )
        out = format_context_for_prompt(ctx)
        assert "US MACRO RATES" in out
        assert "RISK REGIME" in out
        assert "CFTC COT" in out
        assert "GDELT NEWS TONE" in out
        assert "ECONOMIC CALENDAR" in out

    def test_format_context_omits_absent_blocks(self):
        from fx_ai_trader.trading.context import (
            MarketContext,
            format_context_for_prompt,
        )
        ctx = MarketContext(
            snapshots=[], open_positions=[], virtual_capital_usd=500.0,
        )
        out = format_context_for_prompt(ctx)
        assert "RISK REGIME" not in out
        assert "CFTC COT" not in out
        assert "GDELT NEWS TONE" not in out
        assert "ECONOMIC CALENDAR" not in out
