"""Тесты Deribit DVOL/IV провайдера."""
from __future__ import annotations

import time

import pytest

from ai_trader.macro.options import (
    OptionsIvProvider,
    OptionsIvSnapshot,
    format_options_iv,
)


# Реальный формат Deribit /public/get_volatility_index_data:
# data: список [ts_ms, open, high, low, close], отсортированный по ts asc.

_BTC_DATA_OK = {
    "result": {
        "data": [
            [1, 40.0, 40.5, 39.8, 40.3],
            [2, 40.3, 40.4, 39.7, 39.7],
            [3, 39.7, 39.7, 38.5, 38.8],   # последняя точка
        ]
    }
}

_ETH_DATA_OK = {
    "result": {
        "data": [
            [1, 55.0, 55.5, 54.5, 55.0],
            [2, 55.0, 56.0, 54.8, 55.2],
            [3, 55.2, 55.5, 54.0, 54.5],
        ]
    }
}


def _make_get_json(btc: dict | None, eth: dict | None):
    """get_json-стуб, отвечает по содержимому URL."""
    def _stub(url: str) -> dict | None:
        if "currency=BTC" in url:
            return btc
        if "currency=ETH" in url:
            return eth
        return None
    return _stub


class TestOptionsIvProviderFetch:
    def test_full_data_btc_and_eth(self):
        provider = OptionsIvProvider(
            ttl_seconds=600,
            get_json=_make_get_json(_BTC_DATA_OK, _ETH_DATA_OK),
        )
        snap = provider.get_snapshot()
        # BTC: closes=[40.3, 39.7, 38.8] → now=38.8
        # highs=[40.5, 40.4, 39.7], lows=[39.8, 39.7, 38.5]
        # iv_low = min(lows) = 38.5; iv_high = max(highs) = 40.5
        # change = (38.8 - 40.3) / 40.3 * 100 ≈ -3.7%
        assert snap.btc_iv_now == pytest.approx(38.8)
        assert snap.btc_iv_24h_low == pytest.approx(38.5)
        assert snap.btc_iv_24h_high == pytest.approx(40.5)
        assert snap.btc_iv_24h_change_pct == pytest.approx((38.8 - 40.3) / 40.3 * 100, rel=1e-6)
        # ETH: now=54.5, low=54.0, high=56.0, change ≈ -0.91%
        assert snap.eth_iv_now == pytest.approx(54.5)
        assert snap.eth_iv_24h_low == pytest.approx(54.0)
        assert snap.eth_iv_24h_high == pytest.approx(56.0)

    def test_btc_only(self):
        provider = OptionsIvProvider(
            ttl_seconds=600,
            get_json=_make_get_json(_BTC_DATA_OK, None),
        )
        snap = provider.get_snapshot()
        assert snap.btc_iv_now == pytest.approx(38.8)
        assert snap.eth_iv_now is None

    def test_both_failures_no_crash(self):
        provider = OptionsIvProvider(
            ttl_seconds=600,
            get_json=_make_get_json(None, None),
        )
        snap = provider.get_snapshot()
        assert snap.btc_iv_now is None
        assert snap.eth_iv_now is None

    def test_empty_data_array(self):
        empty = {"result": {"data": []}}
        provider = OptionsIvProvider(
            ttl_seconds=600,
            get_json=_make_get_json(empty, empty),
        )
        snap = provider.get_snapshot()
        assert snap.btc_iv_now is None
        assert snap.eth_iv_now is None

    def test_malformed_bar_returns_none(self):
        bad = {"result": {"data": [["not-a-number"]]}}
        provider = OptionsIvProvider(
            ttl_seconds=600,
            get_json=_make_get_json(bad, _ETH_DATA_OK),
        )
        snap = provider.get_snapshot()
        assert snap.btc_iv_now is None
        # ETH всё равно заполняется
        assert snap.eth_iv_now == pytest.approx(54.5)

    def test_single_bar_no_change_pct(self):
        single = {"result": {"data": [[1, 40.0, 41.0, 39.0, 40.5]]}}
        provider = OptionsIvProvider(
            ttl_seconds=600,
            get_json=_make_get_json(single, None),
        )
        snap = provider.get_snapshot()
        assert snap.btc_iv_now == pytest.approx(40.5)
        assert snap.btc_iv_24h_change_pct is None  # нужно >=2 bar


class TestOptionsIvCache:
    def test_ttl_caches(self):
        counts: dict[str, int] = {}

        def get_json(url: str):
            counts[url] = counts.get(url, 0) + 1
            return _BTC_DATA_OK if "currency=BTC" in url else _ETH_DATA_OK

        provider = OptionsIvProvider(ttl_seconds=600, get_json=get_json)
        for _ in range(3):
            provider.get_snapshot()
        # Каждый URL уникален (содержит timestamp), но все 3 вызова идут под
        # одним кэшем → должен быть ровно 1 fetch на BTC и 1 на ETH.
        btc_calls = sum(1 for k in counts if "currency=BTC" in k)
        eth_calls = sum(1 for k in counts if "currency=ETH" in k)
        assert btc_calls == 1
        assert eth_calls == 1

    def test_ttl_zero_refetches(self):
        counts: dict[str, int] = {}

        def get_json(url: str):
            counts[url] = counts.get(url, 0) + 1
            return _BTC_DATA_OK if "currency=BTC" in url else _ETH_DATA_OK

        provider = OptionsIvProvider(ttl_seconds=0, get_json=get_json)
        provider.get_snapshot()
        time.sleep(0.01)
        provider.get_snapshot()
        # 2 цикла → 2 fetch'а каждой валюты (URLs разные за счёт ts).
        btc_calls = sum(1 for k in counts if "currency=BTC" in k)
        eth_calls = sum(1 for k in counts if "currency=ETH" in k)
        assert btc_calls == 2
        assert eth_calls == 2


class TestFormatOptionsIv:
    def test_format_full_data_with_labels(self):
        s = OptionsIvSnapshot(
            btc_iv_now=38.74, btc_iv_24h_low=38.36, btc_iv_24h_high=40.54,
            btc_iv_24h_change_pct=-3.94,
            eth_iv_now=54.55, eth_iv_24h_low=52.82, eth_iv_24h_high=56.24,
            eth_iv_24h_change_pct=-1.50,
        )
        out = format_options_iv(s)
        # 38.74 < 50 → [normal IV]
        assert "BTC IV: 38.74%" in out
        assert "[normal IV]" in out
        # 54.55 в (50, 80) → [elevated IV]
        assert "ETH IV: 54.55%" in out
        assert "[elevated IV]" in out
        # Range и change parts
        assert "38.36 → 40.54" in out
        assert "-3.94%" in out

    def test_format_low_iv_complacency(self):
        s = OptionsIvSnapshot(
            btc_iv_now=25.0, btc_iv_24h_low=24.0, btc_iv_24h_high=26.0,
            btc_iv_24h_change_pct=-1.0,
            eth_iv_now=None, eth_iv_24h_low=None, eth_iv_24h_high=None,
            eth_iv_24h_change_pct=None,
        )
        out = format_options_iv(s)
        assert "LOW IV — complacency" in out

    def test_format_extreme_iv(self):
        s = OptionsIvSnapshot(
            btc_iv_now=95.0, btc_iv_24h_low=80.0, btc_iv_24h_high=100.0,
            btc_iv_24h_change_pct=+15.0,
            eth_iv_now=None, eth_iv_24h_low=None, eth_iv_24h_high=None,
            eth_iv_24h_change_pct=None,
        )
        out = format_options_iv(s)
        assert "EXTREME IV — panic / shock" in out

    def test_format_all_none_falls_back(self):
        s = OptionsIvSnapshot(
            btc_iv_now=None, btc_iv_24h_low=None, btc_iv_24h_high=None,
            btc_iv_24h_change_pct=None,
            eth_iv_now=None, eth_iv_24h_low=None, eth_iv_24h_high=None,
            eth_iv_24h_change_pct=None,
        )
        out = format_options_iv(s)
        assert "data unavailable" in out
