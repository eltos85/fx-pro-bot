"""Tests for ai_trader v0.30 THESIS DISCIPLINE + 5-DIM SENTIMENT validation.

Покрывает:
- ``parse_action(strict_v030_schema=True)`` для action="open":
  * macro_thesis обязательное (50-500 chars)
  * sentiment блок обязательный с 5 полями
  * hard-gate aggregate_uncertainty > 0.7 → reject
- ``parse_action(strict_v030_schema=True)`` для action="close":
  * thesis_status в {"broken", "intact", "partial"}
  * thesis_invalidator обязательное non-empty (≤500 chars)
- Backward-compat: ``strict_v030_schema=False`` (default) пропускает
  все новые поля (legacy промпт продолжает работать).
- Custom threshold через ``news_uncertainty_block`` parameter.

См. ``BUILDLOG_AI_TRADER.md`` v0.30, NEWS SENTIMENT блок в
SYSTEM_PROMPT.
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
            "ETF net inflow $1.2B last 5 days + DXY -0.8% testing "
            "98.5 support + Fed dovish minutes"
        ),
        "sentiment": {
            "aggregate_relevance": 0.7,
            "aggregate_polarity": 0.4,
            "aggregate_intensity": 0.5,
            "aggregate_uncertainty": 0.2,
            "aggregate_forwardness": 0.6,
        },
        "reason": "trend + ETF inflow + DXY softening",
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

    def test_missing_sentiment_rejected(self):
        payload = json.loads(_open_action())
        del payload["sentiment"]
        result = parse_action(
            json.dumps(payload), ALLOWED, strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "sentiment required" in result

    def test_sentiment_missing_field_rejected(self):
        payload = json.loads(_open_action())
        del payload["sentiment"]["aggregate_uncertainty"]
        result = parse_action(
            json.dumps(payload), ALLOWED, strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "sentiment.aggregate_uncertainty required" in result

    def test_sentiment_non_numeric_field_rejected(self):
        payload = json.loads(_open_action())
        payload["sentiment"]["aggregate_polarity"] = "bullish"
        result = parse_action(
            json.dumps(payload), ALLOWED, strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "sentiment.aggregate_polarity required" in result

    def test_uncertainty_hard_gate_blocks_open(self):
        """aggregate_uncertainty=0.85 > default 0.7 threshold → reject."""
        payload = json.loads(_open_action())
        payload["sentiment"]["aggregate_uncertainty"] = 0.85
        result = parse_action(
            json.dumps(payload), ALLOWED, strict_v030_schema=True,
        )
        assert isinstance(result, str)
        assert "open_blocked_by_uncertainty" in result
        assert "0.85" in result and "0.70" in result

    def test_uncertainty_at_exact_threshold_passes(self):
        """0.70 == 0.70 → passes (strict >, not >=)."""
        payload = json.loads(_open_action())
        payload["sentiment"]["aggregate_uncertainty"] = 0.70
        result = parse_action(
            json.dumps(payload), ALLOWED, strict_v030_schema=True,
        )
        assert isinstance(result, ParsedAction)

    def test_custom_uncertainty_threshold(self):
        """News-uncertainty-block=0.5 → 0.55 теперь блокирует open."""
        payload = json.loads(_open_action())
        payload["sentiment"]["aggregate_uncertainty"] = 0.55
        result = parse_action(
            json.dumps(payload),
            ALLOWED,
            strict_v030_schema=True,
            news_uncertainty_block=0.5,
        )
        assert isinstance(result, str)
        assert "open_blocked_by_uncertainty" in result


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
        """Legacy: без macro_thesis/sentiment open проходит default."""
        payload = json.loads(_open_action())
        del payload["macro_thesis"]
        del payload["sentiment"]
        result = parse_action(json.dumps(payload), ALLOWED)
        assert isinstance(result, ParsedAction)

    def test_close_without_thesis_status_passes_legacy(self):
        payload = json.loads(_close_action())
        del payload["thesis_status"]
        del payload["thesis_invalidator"]
        result = parse_action(json.dumps(payload), ALLOWED)
        assert isinstance(result, ParsedAction)

    def test_high_uncertainty_passes_legacy(self):
        """Без strict_v030_schema=True hard-gate выключен."""
        payload = json.loads(_open_action())
        payload["sentiment"]["aggregate_uncertainty"] = 0.95
        result = parse_action(json.dumps(payload), ALLOWED)
        assert isinstance(result, ParsedAction)
