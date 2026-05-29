"""Tests for ai_trader THESIS DISCIPLINE validation (v0.40).

Покрывает:
- ``parse_action(strict_v030_schema=True)`` для action="open":
  * macro_thesis обязательное (50-500 chars) — PRICE-ACTION trade-thesis
- ``parse_action(strict_v030_schema=True)`` для action="close":
  * thesis_status в {"broken", "intact", "partial"}
  * thesis_invalidator обязательное non-empty (≤500 chars)
- Backward-compat: ``strict_v030_schema=False`` (default) пропускает
  все новые поля (legacy промпт продолжает работать).

v0.40 (2026-05-29): 5-DIM NEWS SENTIMENT + uncertainty hard-gate
УДАЛЕНЫ (нет news-фида). Соответствующие тесты сняты.

См. ``BUILDLOG_AI_TRADER.md`` v0.40.
"""
from __future__ import annotations

import json

import pytest

from ai_trader.trading.executor import ParsedAction, parse_action


ALLOWED = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def _open_action(**overrides) -> str:
    base = {
        "action": "open",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "leverage": 5,
        "position_size_usd": 500.0,
        "stop_loss": 59400.0,
        "take_profit": 61200.0,
        "confidence": 0.6,
        "invalidation_condition": "1H close below 59400 with vol spike",
        "risk_usd": 6.0,
        "macro_thesis": (
            "1H broke 24h high $60,000 on ATR% expansion, 4H EMA20>EMA50 "
            "trend up + MACD positive; risk-on regime; SL below broken high"
        ),
        "reason": "trend + breakout + supportive regime",
    }
    base.update(overrides)
    return json.dumps(base)


def _close_action(**overrides) -> str:
    base = {
        "action": "close",
        "position_id": 42,
        "thesis_status": "broken",
        "thesis_invalidator": "DXY broke 99.5 with hawkish Fed speak",
        "reason": "thesis broken",
    }
    base.update(overrides)
    return json.dumps(base)


# ─── strict_v030_schema=True: open ─────────────────────────────────────


class TestOpenStrictSchema:
    def test_valid_open_passes(self):
        result = parse_action(
            _open_action(),
            ALLOWED,
            strict_v030_schema=True,
        )
        assert isinstance(result, ParsedAction)
        assert result.action == "open"

    def test_missing_macro_thesis_rejected(self):
        payload = json.loads(_open_action())
        del payload["macro_thesis"]
        result = parse_action(
            json.dumps(payload), ALLOWED, strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "macro_thesis required" in result

    def test_short_macro_thesis_rejected(self):
        result = parse_action(
            _open_action(macro_thesis="too short"),
            ALLOWED,
            strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "macro_thesis too short" in result

    def test_long_macro_thesis_rejected(self):
        result = parse_action(
            _open_action(macro_thesis="A" * 501),
            ALLOWED,
            strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "macro_thesis too long" in result

    def test_open_without_sentiment_passes(self):
        """v0.40: sentiment-блок больше не требуется (news убраны)."""
        payload = json.loads(_open_action())
        payload.pop("sentiment", None)
        result = parse_action(
            json.dumps(payload), ALLOWED, strict_v030_schema=True,
        )
        assert isinstance(result, ParsedAction)
        assert result.action == "open"


# ─── strict_v030_schema=True: close ────────────────────────────────────


class TestCloseStrictSchema:
    def test_valid_close_passes(self):
        result = parse_action(
            _close_action(),
            ALLOWED,
            strict_v030_schema=True,
        )
        assert isinstance(result, ParsedAction)
        assert result.action == "close"

    def test_invalid_thesis_status_rejected(self):
        result = parse_action(
            _close_action(thesis_status="invalid"),
            ALLOWED,
            strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "thesis_status required" in result

    def test_missing_thesis_status_rejected(self):
        payload = json.loads(_close_action())
        del payload["thesis_status"]
        result = parse_action(
            json.dumps(payload), ALLOWED, strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "thesis_status required" in result

    def test_empty_thesis_invalidator_rejected(self):
        result = parse_action(
            _close_action(thesis_invalidator=""),
            ALLOWED,
            strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "thesis_invalidator required" in result

    def test_long_thesis_invalidator_rejected(self):
        result = parse_action(
            _close_action(thesis_invalidator="X" * 501),
            ALLOWED,
            strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "thesis_invalidator too long" in result

    def test_all_three_thesis_statuses_accepted(self):
        for ts in ("broken", "intact", "partial"):
            result = parse_action(
                _close_action(thesis_status=ts),
                ALLOWED,
                strict_v030_schema=True,
            )
            assert isinstance(result, ParsedAction), f"failed for {ts}"


# ─── Backward-compat: strict_v030_schema=False (default) ───────────────


class TestBackwardCompat:
    def test_open_without_macro_thesis_passes_legacy(self):
        """Legacy: без macro_thesis open проходит при default-схеме."""
        payload = json.loads(_open_action())
        del payload["macro_thesis"]
        result = parse_action(json.dumps(payload), ALLOWED)
        assert isinstance(result, ParsedAction)

    def test_close_without_thesis_status_passes_legacy(self):
        payload = json.loads(_close_action())
        del payload["thesis_status"]
        del payload["thesis_invalidator"]
        result = parse_action(json.dumps(payload), ALLOWED)
        assert isinstance(result, ParsedAction)
