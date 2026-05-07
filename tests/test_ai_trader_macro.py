"""Тесты macro/sentiment провайдера (Fear & Greed + BTC dominance).

Сетевые вызовы заменяются on-the-fly через инжект `get_json` в
`MacroProvider`, чтобы тесты не дёргали публичные эндпоинты.
"""
from __future__ import annotations

import time

import pytest

from ai_trader.macro.external import (
    MacroProvider,
    MacroSnapshot,
    _COINGECKO_GLOBAL_URL,
    _FNG_URL,
    format_macro,
)


# ─── Стаб-ответы публичных API (реальные структуры) ─────────────────


_FNG_OK = {
    "name": "Fear and Greed Index",
    "data": [
        {"value": "47", "value_classification": "Neutral", "timestamp": "1778112000"},
        {"value": "46", "value_classification": "Fear", "timestamp": "1778025600"},
    ],
    "metadata": {"error": None},
}

_FNG_EXTREME_FEAR = {
    "data": [
        {"value": "12", "value_classification": "Extreme Fear", "timestamp": "1"},
        {"value": "20", "value_classification": "Fear", "timestamp": "2"},
    ]
}

_FNG_EXTREME_GREED = {
    "data": [
        {"value": "85", "value_classification": "Extreme Greed", "timestamp": "1"},
        {"value": "80", "value_classification": "Greed", "timestamp": "2"},
    ]
}

_COINGECKO_OK = {
    "data": {
        "market_cap_percentage": {
            "btc": 58.482,
            "eth": 10.146,
            "usdt": 6.847,
            "usdc": 2.825,
            "bnb": 3.149,
            "xrp": 3.147,
        },
        "market_cap_change_percentage_24h_usd": -1.632,
    }
}


def _make_get_json(responses: dict[str, dict | None]):
    """Helper: возвращает callable, имитирующий http_get_json."""
    def _stub(url: str) -> dict | None:
        return responses.get(url)
    return _stub


class TestMacroProviderFetch:
    """get_snapshot() агрегирует поля из обоих эндпоинтов."""

    def test_full_data(self):
        get_json = _make_get_json({_FNG_URL: _FNG_OK, _COINGECKO_GLOBAL_URL: _COINGECKO_OK})
        provider = MacroProvider(ttl_seconds=600, get_json=get_json)
        snap = provider.get_snapshot()
        assert isinstance(snap, MacroSnapshot)
        assert snap.fng_value == 47
        assert snap.fng_classification == "Neutral"
        assert snap.fng_delta_24h == 1  # 47 - 46
        assert snap.btc_dominance_pct == pytest.approx(58.482)
        assert snap.eth_dominance_pct == pytest.approx(10.146)
        # stables = USDT + USDC = 6.847 + 2.825
        assert snap.stables_dominance_pct == pytest.approx(9.672, abs=1e-3)
        assert snap.market_cap_change_24h_pct == pytest.approx(-1.632)

    def test_fng_failure_partial_data(self):
        get_json = _make_get_json({_FNG_URL: None, _COINGECKO_GLOBAL_URL: _COINGECKO_OK})
        provider = MacroProvider(ttl_seconds=600, get_json=get_json)
        snap = provider.get_snapshot()
        assert snap.fng_value is None
        assert snap.fng_classification is None
        assert snap.fng_delta_24h is None
        # CoinGecko заполняется
        assert snap.btc_dominance_pct == pytest.approx(58.482)

    def test_coingecko_failure_partial_data(self):
        get_json = _make_get_json({_FNG_URL: _FNG_OK, _COINGECKO_GLOBAL_URL: None})
        provider = MacroProvider(ttl_seconds=600, get_json=get_json)
        snap = provider.get_snapshot()
        assert snap.fng_value == 47
        assert snap.btc_dominance_pct is None
        assert snap.stables_dominance_pct is None

    def test_both_fail_no_crash(self):
        get_json = _make_get_json({_FNG_URL: None, _COINGECKO_GLOBAL_URL: None})
        provider = MacroProvider(ttl_seconds=600, get_json=get_json)
        snap = provider.get_snapshot()
        assert all(getattr(snap, f) is None for f in (
            "fng_value", "fng_classification", "fng_delta_24h",
            "btc_dominance_pct", "eth_dominance_pct",
            "stables_dominance_pct", "market_cap_change_24h_pct",
        ))

    def test_malformed_fng_value_returns_none(self):
        bad = {"data": [{"value": "not-a-number", "value_classification": "?"}]}
        get_json = _make_get_json({_FNG_URL: bad, _COINGECKO_GLOBAL_URL: None})
        provider = MacroProvider(ttl_seconds=600, get_json=get_json)
        snap = provider.get_snapshot()
        assert snap.fng_value is None

    def test_only_one_fng_event_no_delta(self):
        only_one = {"data": [{"value": "50", "value_classification": "Neutral"}]}
        get_json = _make_get_json({_FNG_URL: only_one, _COINGECKO_GLOBAL_URL: None})
        provider = MacroProvider(ttl_seconds=600, get_json=get_json)
        snap = provider.get_snapshot()
        assert snap.fng_value == 50
        assert snap.fng_delta_24h is None


class TestMacroProviderCache:
    """TTL-кэш не делает повторный fetch до истечения срока."""

    def test_cache_hits_same_call(self):
        call_counts: dict[str, int] = {}

        def get_json(url: str):
            call_counts[url] = call_counts.get(url, 0) + 1
            return _FNG_OK if url == _FNG_URL else _COINGECKO_OK

        provider = MacroProvider(ttl_seconds=600, get_json=get_json)
        for _ in range(3):
            provider.get_snapshot()
        # Должен быть ровно 1 fetch на каждый URL
        assert call_counts[_FNG_URL] == 1
        assert call_counts[_COINGECKO_GLOBAL_URL] == 1

    def test_cache_expires_and_refetches(self):
        call_counts: dict[str, int] = {}

        def get_json(url: str):
            call_counts[url] = call_counts.get(url, 0) + 1
            return _FNG_OK if url == _FNG_URL else _COINGECKO_OK

        # ttl=0 → каждый вызов = новый fetch
        provider = MacroProvider(ttl_seconds=0, get_json=get_json)
        provider.get_snapshot()
        time.sleep(0.01)
        provider.get_snapshot()
        assert call_counts[_FNG_URL] == 2
        assert call_counts[_COINGECKO_GLOBAL_URL] == 2


class TestFormatMacro:
    """Метки-режимы и graceful degradation."""

    def test_format_full_data_includes_labels(self):
        s = MacroSnapshot(
            fng_value=47, fng_classification="Neutral", fng_delta_24h=1,
            btc_dominance_pct=58.48, eth_dominance_pct=10.14,
            stables_dominance_pct=9.67, market_cap_change_24h_pct=-1.63,
        )
        out = format_macro(s)
        assert "Fear & Greed: 47 (Neutral, +1 vs 24h)" in out
        assert "[Neutral]" in out
        assert "BTC dom: 58.48%" in out
        assert "ETH dom: 10.14%" in out
        # 9.67% попадает в [elevated stables — caution]
        assert "elevated stables" in out
        assert "Total mcap 24h: -1.63%" in out

    def test_format_extreme_fear_label(self):
        s = MacroSnapshot(
            fng_value=12, fng_classification="Extreme Fear", fng_delta_24h=-8,
            btc_dominance_pct=None, eth_dominance_pct=None,
            stables_dominance_pct=None, market_cap_change_24h_pct=None,
        )
        out = format_macro(s)
        assert "Extreme Fear" in out
        assert "contrarian-buy zone" in out
        assert "-8 vs 24h" in out

    def test_format_extreme_greed_label(self):
        s = MacroSnapshot(
            fng_value=85, fng_classification="Extreme Greed", fng_delta_24h=5,
            btc_dominance_pct=None, eth_dominance_pct=None,
            stables_dominance_pct=None, market_cap_change_24h_pct=None,
        )
        out = format_macro(s)
        assert "Extreme Greed" in out
        assert "contrarian-sell zone" in out

    def test_format_high_stables_label(self):
        s = MacroSnapshot(
            fng_value=None, fng_classification=None, fng_delta_24h=None,
            btc_dominance_pct=50.0, eth_dominance_pct=10.0,
            stables_dominance_pct=14.5, market_cap_change_24h_pct=None,
        )
        out = format_macro(s)
        # 14.5% > 12% → HIGH stables
        assert "HIGH stables" in out

    def test_format_all_none_falls_back(self):
        s = MacroSnapshot(
            fng_value=None, fng_classification=None, fng_delta_24h=None,
            btc_dominance_pct=None, eth_dominance_pct=None,
            stables_dominance_pct=None, market_cap_change_24h_pct=None,
        )
        out = format_macro(s)
        assert "data unavailable" in out
