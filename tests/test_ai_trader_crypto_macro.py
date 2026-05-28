"""Tests for ai_trader v0.30 crypto_macro module.

BTC.D + Total crypto market cap через CoinGecko /global, без сетевых
вызовов (requests.get полностью замокан через unittest.mock.patch).

См. ``src/ai_trader/data/crypto_macro.py`` docstring для compliance
с ``api-docs.mdc`` (CoinGecko free tier, 10K calls/month, 100/min).
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from ai_trader.data.crypto_macro import (
    CryptoMacroProvider,
    CryptoMacroSnapshot,
    format_crypto_macro_snapshot,
)


# ─── format_crypto_macro_snapshot ────────────────────────────────────────


class TestFormatCryptoMacroSnapshot:
    def test_returns_none_for_none(self):
        assert format_crypto_macro_snapshot(None) is None

    def test_returns_none_when_all_fields_empty(self):
        snap = CryptoMacroSnapshot(
            btc_dominance_pct=None,
            total_market_cap_usd=None,
            total_market_cap_change_24h_pct=None,
            eth_dominance_pct=None,
            fetched_at_utc="2026-05-28T08:00:00+00:00",
        )
        assert format_crypto_macro_snapshot(snap) is None

    def test_renders_full_block(self):
        snap = CryptoMacroSnapshot(
            btc_dominance_pct=60.32,
            total_market_cap_usd=3_450_000_000_000.0,
            total_market_cap_change_24h_pct=-1.23,
            eth_dominance_pct=12.45,
            fetched_at_utc="2026-05-28T08:00:00+00:00",
        )
        out = format_crypto_macro_snapshot(snap)
        assert out is not None
        assert "CRYPTO MACRO" in out
        assert "BTC.D=60.32%" in out
        assert "ETH.D=12.45%" in out
        assert "Total crypto cap=$3.45T" in out
        assert "24h=-1.23%" in out
        assert "BTC.D support 59.63%" in out  # reference levels
        assert "Altcoin Season Index" in out
        assert "2026-05-28" in out

    def test_renders_only_btc_dominance(self):
        snap = CryptoMacroSnapshot(
            btc_dominance_pct=60.32,
            total_market_cap_usd=None,
            total_market_cap_change_24h_pct=None,
            eth_dominance_pct=None,
            fetched_at_utc="2026-05-28T08:00:00+00:00",
        )
        out = format_crypto_macro_snapshot(snap)
        assert out is not None
        assert "BTC.D=60.32%" in out
        assert "Total crypto cap" not in out
        assert "ETH.D" not in out

    def test_total_cap_in_billions(self):
        snap = CryptoMacroSnapshot(
            btc_dominance_pct=60.0,
            total_market_cap_usd=500_000_000_000.0,
            total_market_cap_change_24h_pct=None,
            eth_dominance_pct=None,
            fetched_at_utc="2026-05-28T08:00:00+00:00",
        )
        out = format_crypto_macro_snapshot(snap)
        assert out is not None
        assert "Total crypto cap=$500.0B" in out


# ─── CryptoMacroProvider with mocked requests ────────────────────────────


def _good_response(
    btc_d: float = 60.3,
    eth_d: float = 12.5,
    total_usd: float = 3_450_000_000_000.0,
    change_24h: float = -1.2,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "data": {
            "market_cap_percentage": {"btc": btc_d, "eth": eth_d},
            "total_market_cap": {"usd": total_usd},
            "market_cap_change_percentage_24h_usd": change_24h,
            "active_cryptocurrencies": 12345,
        }
    }
    return resp


class TestCryptoMacroProviderFetch:
    def test_fetch_happy_path(self):
        provider = CryptoMacroProvider(cache_ttl_sec=3600)
        with patch("requests.get", return_value=_good_response()):
            snap = provider.get_snapshot()
        assert snap is not None
        assert snap.btc_dominance_pct == pytest.approx(60.3)
        assert snap.eth_dominance_pct == pytest.approx(12.5)
        assert snap.total_market_cap_usd == pytest.approx(3_450_000_000_000.0)
        assert snap.total_market_cap_change_24h_pct == pytest.approx(-1.2)

    def test_cache_hit_within_ttl_no_refetch(self):
        provider = CryptoMacroProvider(cache_ttl_sec=3600)
        call_counter = {"count": 0}

        def fake_get(url, timeout=None):
            call_counter["count"] += 1
            return _good_response()

        with patch("requests.get", side_effect=fake_get):
            snap1 = provider.get_snapshot()
            snap2 = provider.get_snapshot()
        assert snap1 is snap2  # тот же объект из кэша
        assert call_counter["count"] == 1

    def test_http_failure_returns_cache_or_none(self):
        provider = CryptoMacroProvider()
        with patch("requests.get", side_effect=RuntimeError("net down")):
            snap = provider.get_snapshot()
        assert snap is None

    def test_cache_fallback_on_failure(self):
        provider = CryptoMacroProvider(cache_ttl_sec=0)
        with patch("requests.get", return_value=_good_response()):
            snap1 = provider.get_snapshot()
        assert snap1 is not None
        time.sleep(0.01)
        with patch("requests.get", side_effect=RuntimeError("fail")):
            snap2 = provider.get_snapshot()
        assert snap2 is snap1

    def test_malformed_json_returns_none(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.side_effect = ValueError("not json")
        provider = CryptoMacroProvider()
        with patch("requests.get", return_value=resp):
            snap = provider.get_snapshot()
        assert snap is None

    def test_missing_data_field_returns_none(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"foo": "bar"}
        provider = CryptoMacroProvider()
        with patch("requests.get", return_value=resp):
            snap = provider.get_snapshot()
        assert snap is None

    def test_partial_data_btc_only(self):
        """Если CoinGecko возвращает только btc dominance — snapshot
        с None'ами для остального, но валидный.
        """
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "data": {
                "market_cap_percentage": {"btc": 60.0},
                # нет total_market_cap, нет change_percentage
            }
        }
        provider = CryptoMacroProvider()
        with patch("requests.get", return_value=resp):
            snap = provider.get_snapshot()
        assert snap is not None
        assert snap.btc_dominance_pct == pytest.approx(60.0)
        assert snap.total_market_cap_usd is None
        assert snap.eth_dominance_pct is None
