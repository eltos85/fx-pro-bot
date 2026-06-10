"""Тесты на пакет правок аудита 2026-06-10 (BUILDLOG_AI_FX_TRADER).

Покрытие:
1. Server-side верификация intact-закрытий (executor):
   - галлюцинированный locked-profit R (кейс id=45: «25.8R» при +0.26R)
   - галлюцинированный age-триггер (кейс id=32: «>24h» через 47 минут)
   - легитимные intact-закрытия проходят
2. Анти-churn: _position_age_sec (main) — фильтр свежих позиций.
3. EIA: NG storage 5y average (расчёт + формат surplus/deficit),
   refinery series = WPULEUS3 (процент, а не gross inputs).
4. NOAA: temperature-summary фильтр (_summarize_for_llm).
5. RSS: title-level exclude (gold-заголовок не попадает в BZ=F/NG=F).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fx_ai_trader.app.main import _position_age_sec
from fx_ai_trader.news.eia import (
    _SERIES_REFINERY_UTIL,
    EiaProvider,
    EiaSnapshot,
    format_eia_by_symbol,
)
from fx_ai_trader.news.rss import _classify_symbols
from fx_ai_trader.news.weather import NoaaOutlookProvider
from fx_ai_trader.trading.executor import (
    _position_age_hours,
    _verify_intact_close_claims,
)


@dataclass
class _Pos:
    side: str
    entry_price: float
    sl_price: float | None
    opened_at: str


def _iso_ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat()


# ─── 1. Server-side верификация intact-закрытий ─────────────────────────


class TestVerifyIntactClose:
    def test_hallucinated_locked_profit_rejected(self):
        # Кейс id=45: BUY, фактический R ≈ +0.26, LLM заявил «25.8R».
        pos = _Pos("BUY", 100.0, 98.0, _iso_ago(hours=2))
        err = _verify_intact_close_claims(
            claim_text="locked-profit 25.8R", pos=pos,
            current_price=100.5,  # actual R = 0.5/2.0 = +0.25
        )
        assert err is not None
        assert "locked-profit" in err
        assert "+0.25" in err

    def test_real_locked_profit_passes(self):
        pos = _Pos("BUY", 100.0, 98.0, _iso_ago(hours=2))
        err = _verify_intact_close_claims(
            claim_text="locked-profit 1.6R", pos=pos,
            current_price=103.2,  # actual R = +1.6
        )
        assert err is None

    def test_locked_profit_tolerance_band(self):
        # R = 1.45 — в пределах допуска 0.1 от порога 1.5 → пропускаем.
        pos = _Pos("BUY", 100.0, 98.0, _iso_ago(hours=2))
        err = _verify_intact_close_claims(
            claim_text="locked-profit 1.5R", pos=pos, current_price=102.9,
        )
        assert err is None

    def test_locked_profit_sell_side(self):
        # SELL в минусе (цена выше входа) с заявленным locked-profit.
        pos = _Pos("SELL", 100.0, 102.0, _iso_ago(hours=2))
        err = _verify_intact_close_claims(
            claim_text="locked profit 2R", pos=pos,
            current_price=101.0,  # actual R = -0.5
        )
        assert err is not None

    def test_no_sl_cannot_verify_passes(self):
        pos = _Pos("BUY", 100.0, None, _iso_ago(hours=2))
        err = _verify_intact_close_claims(
            claim_text="locked-profit 2R", pos=pos, current_price=100.1,
        )
        assert err is None

    def test_hallucinated_age_rejected(self):
        # Кейс id=32: «Position age >24h» через 47 минут после входа.
        pos = _Pos("BUY", 100.0, 98.0, _iso_ago(minutes=47))
        err = _verify_intact_close_claims(
            claim_text="time decay 24h+", pos=pos, current_price=100.2,
        )
        assert err is not None
        assert "age" in err or "time-decay" in err

    def test_real_age_passes(self):
        pos = _Pos("BUY", 100.0, 98.0, _iso_ago(hours=30))
        err = _verify_intact_close_claims(
            claim_text="time decay 24h+, contango carry", pos=pos,
            current_price=100.2,
        )
        assert err is None

    def test_age_word_boundary_not_triggered_by_manage(self):
        # Подстрока «age» в «manage» не должна включать age-проверку.
        pos = _Pos("BUY", 100.0, 98.0, _iso_ago(minutes=10))
        err = _verify_intact_close_claims(
            claim_text="manage exposure into adverse headline", pos=pos,
            current_price=100.2,
        )
        assert err is None

    def test_news_trigger_not_verifiable_passes(self):
        pos = _Pos("BUY", 100.0, 98.0, _iso_ago(minutes=10))
        err = _verify_intact_close_claims(
            claim_text="adverse high-severity news: OPEC surprise",
            pos=pos, current_price=99.5,
        )
        assert err is None

    def test_position_age_hours_parses_naive_as_utc(self):
        naive = (datetime.now(UTC) - timedelta(hours=3)).replace(
            tzinfo=None
        ).isoformat()
        age = _position_age_hours(naive)
        assert age is not None
        assert 2.9 < age < 3.1

    def test_position_age_hours_bad_input(self):
        assert _position_age_hours("not-a-date") is None


# ─── 2. Анти-churn: возраст позиции для датчиков ────────────────────────


class TestPositionAgeSec:
    def test_fresh_position(self):
        age = _position_age_sec(_iso_ago(minutes=5))
        assert age is not None
        assert 250 < age < 350

    def test_old_position(self):
        age = _position_age_sec(_iso_ago(hours=2))
        assert age is not None
        assert age > 7000

    def test_bad_input(self):
        assert _position_age_sec("garbage") is None


# ─── 3. EIA: refinery series + NG storage 5y avg ────────────────────────


class TestEiaFixes:
    def test_refinery_series_is_percent_utilization(self):
        # Bug-fix: WGIRIUS2 (gross inputs, kb/d) → WPULEUS3 (percent).
        assert _SERIES_REFINERY_UTIL == "PET.WPULEUS3.W"

    def test_ng_5y_avg_computed_from_weekly_offsets(self):
        provider = EiaProvider(api_key="test")
        # 262 недельных точки: значение = 1000 + index (новые первыми).
        pts = [(f"2026-W{i:03d}", 1000.0 + i) for i in range(262)]
        with patch.object(provider, "_fetch_series_desc", return_value=pts):
            avg = provider._fetch_ng_storage_5y_avg()
        # offsets 52/104/156/208/260 → values 1052..1260, mean = 1156.
        assert avg == (1052 + 1104 + 1156 + 1208 + 1260) / 5

    def test_ng_5y_avg_insufficient_history(self):
        provider = EiaProvider(api_key="test")
        pts = [(f"p{i}", 1000.0) for i in range(100)]
        with patch.object(provider, "_fetch_series_desc", return_value=pts):
            assert provider._fetch_ng_storage_5y_avg() is None

    def test_format_includes_5y_surplus(self):
        snap = EiaSnapshot(
            crude_stocks_kbarrels=None,
            crude_stocks_change_kbarrels=None,
            crude_stocks_date=None,
            refinery_util_pct=None,
            refinery_util_date=None,
            spr_kbarrels=None,
            spr_date=None,
            ng_storage_bcf=2750.0,
            ng_storage_change_bcf=98.0,
            ng_storage_date="2026-05-29",
            ng_storage_5y_avg_bcf=2500.0,
        )
        blocks = format_eia_by_symbol(snap)
        gas = blocks.get("NG=F", "")
        assert "vs 5y average: 2500 Bcf" in gas
        assert "+250 Bcf" in gas
        assert "+10.0%" in gas
        assert "surplus" in gas

    def test_format_deficit(self):
        snap = EiaSnapshot(
            crude_stocks_kbarrels=None,
            crude_stocks_change_kbarrels=None,
            crude_stocks_date=None,
            refinery_util_pct=None,
            refinery_util_date=None,
            spr_kbarrels=None,
            spr_date=None,
            ng_storage_bcf=2400.0,
            ng_storage_change_bcf=-50.0,
            ng_storage_date="2026-05-29",
            ng_storage_5y_avg_bcf=2500.0,
        )
        gas = format_eia_by_symbol(snap).get("NG=F", "")
        assert "deficit" in gas
        assert "-100 Bcf" in gas

    def test_format_without_5y_avg_still_works(self):
        snap = EiaSnapshot(
            crude_stocks_kbarrels=None,
            crude_stocks_change_kbarrels=None,
            crude_stocks_date=None,
            refinery_util_pct=None,
            refinery_util_date=None,
            spr_kbarrels=None,
            spr_date=None,
            ng_storage_bcf=2400.0,
            ng_storage_change_bcf=-50.0,
            ng_storage_date="2026-05-29",
        )
        gas = format_eia_by_symbol(snap).get("NG=F", "")
        assert "Working gas in storage" in gas
        assert "5y average" not in gas


# ─── 4. NOAA temperature summary ────────────────────────────────────────


class TestNoaaSummary:
    _RAW = (
        "Prognostic Discussion for 6 to 10 and 8 to 14 day outlooks. "
        "The MJO remains in phase 4 with model spread among GEFS members. "
        "Above-normal temperatures are favored across the central CONUS. "
        "Heavy precipitation is expected over the Pacific Northwest. "
        "Much of Alaska leans toward below-normal temperatures. "
        "\n\n8-14 DAY OUTLOOK: Below-normal temperatures are likely over "
        "the East, increasing HDD demand. "
        "Ensemble means disagree on the ridge placement over Hawaii."
    )

    def test_keeps_temperature_sentences(self):
        out = NoaaOutlookProvider._summarize_for_llm(self._RAW)
        assert "Above-normal temperatures are favored" in out
        assert "Below-normal temperatures are likely over the East" in out

    def test_drops_precip_alaska_and_model_talk(self):
        out = NoaaOutlookProvider._summarize_for_llm(self._RAW)
        assert "precipitation" not in out.lower()
        assert "Alaska" not in out
        assert "MJO" not in out

    def test_keeps_section_headers(self):
        out = NoaaOutlookProvider._summarize_for_llm(self._RAW)
        assert "8-14 DAY OUTLOOK" in out

    def test_fallback_when_filter_eats_everything(self):
        raw = "Some text without any relevant keywords at all."
        assert NoaaOutlookProvider._summarize_for_llm(raw) == raw

    def test_default_max_chars_lowered(self):
        assert NoaaOutlookProvider()._max_chars == 2000


# ─── 5. RSS title-level exclude ─────────────────────────────────────────


class TestRssTitleExclude:
    _SYMBOLS = ("XAUUSD", "BZ=F", "NG=F")

    def test_gold_headline_not_in_oil_bucket(self):
        # Кейс аудита 2026-06-10: gold-заголовок попадал в BZ=F через
        # keyword-match в summary («crude», «inventories»).
        title = "Gold stays under pressure as dollar firms"
        summary = (
            "Bullion slips while traders watch weekly inventories data "
            "and broader energy market direction."
        )
        matched = _classify_symbols(
            f"{title}\n{summary}", self._SYMBOLS, title=title
        )
        assert "BZ=F" not in matched
        assert "NG=F" not in matched
        assert "XAUUSD" in matched

    def test_oil_headline_mentioning_gold_in_body_kept(self):
        title = "OPEC+ extends output cuts into Q3"
        summary = "Crude rallied; gold was little changed on the day."
        matched = _classify_symbols(
            f"{title}\n{summary}", self._SYMBOLS, title=title
        )
        assert "BZ=F" in matched

    def test_gold_in_oil_title_excluded_word_boundary(self):
        # «Goldman» не должен дисквалифицировать oil bucket (word boundary).
        title = "Goldman Sachs raises Brent forecast"
        summary = "The bank lifted its crude outlook for 2026."
        matched = _classify_symbols(
            f"{title}\n{summary}", self._SYMBOLS, title=title
        )
        assert "BZ=F" in matched

    def test_no_title_arg_backward_compatible(self):
        text = "OPEC+ extends output cuts\nCrude rallied."
        matched = _classify_symbols(text, self._SYMBOLS)
        assert "BZ=F" in matched
