"""Тесты parser'а Nof1 schema для AI Arena.

Source: gist § OUTPUT FORMAT SPECIFICATION + Output Validation Rules.
Только sanity-валидация — никаких capital safety hard-checks (их нет
в source, см. .cursor/rules/ai-arena-sources.mdc).

Coin enum в LLM-ответе — Nof1-формат БЕЗ USDT (`BTC`, `ETH`, …),
1-в-1 с gist L168. Bybit-symbol появляется только при API-вызовах
(executor `_apply_open` / `_apply_close` через `arena_to_bybit`).
"""
from __future__ import annotations

import json

from ai_arena.trading.executor import (
    ALLOWED_SIGNALS,
    ParsedAction,
    parse_action,
)


# Bybit-формат для allowed_symbols — parser сам маппит в Nof1-формат
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")


def _wrap_json(d: dict) -> str:
    """Эмулируем LLM-ответ: текстовый commentary + JSON в конце."""
    return f"Quick analysis: BTC looks fine.\n```json\n{json.dumps(d)}\n```"


# ─── Базовые happy-path кейсы ────────────────────────────────────────────


class TestValidActions:
    def test_buy_to_enter_valid(self):
        text = _wrap_json({
            "signal": "buy_to_enter", "coin": "BTC",  # Nof1 format, no USDT
            "quantity": 0.001, "leverage": 3,
            "stop_loss": 60000.0, "profit_target": 65000.0,
            "invalidation_condition": "BTC below 59000",
            "confidence": 0.7, "risk_usd": 5.0,
            "justification": "trend up + OI rising",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.signal == "buy_to_enter"
        assert result.raw["coin"] == "BTC"  # храним как пришло от LLM

    def test_sell_to_enter_valid(self):
        text = _wrap_json({
            "signal": "sell_to_enter", "coin": "ETH",
            "quantity": 0.05, "leverage": 2,
            "stop_loss": 3300.0, "profit_target": 3000.0,
            "invalidation_condition": "ETH above 3350",
            "confidence": 0.65, "risk_usd": 8.0,
            "justification": "RSI extreme + funding strong",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.signal == "sell_to_enter"

    def test_hold_minimal(self):
        text = _wrap_json({"signal": "hold"})
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.signal == "hold"

    def test_hold_with_placeholders(self):
        text = _wrap_json({
            "signal": "hold", "coin": "BTC",
            "quantity": 0, "leverage": 1,
            "justification": "no edge",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.signal == "hold"

    def test_close(self):
        text = _wrap_json({
            "signal": "close", "coin": "BTC",
            "justification": "TP reached early",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.signal == "close"

    def test_high_leverage_15x_accepted(self):
        """Source Nof1 разрешает 1-20x — серверного cap'а нет.

        Раньше у нас был max_leverage=5 hard-cap (отсебятина), теперь
        убран. LLM решает сам по своей conviction.
        """
        text = _wrap_json({
            "signal": "buy_to_enter", "coin": "BTC",
            "quantity": 0.001, "leverage": 15,
            "stop_loss": 60000.0, "profit_target": 65000.0,
            "invalidation_condition": "BTC below 59000",
            "confidence": 0.85, "risk_usd": 5.0,
            "justification": "high conviction breakout",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.raw["leverage"] == 15

    def test_low_rr_accepted(self):
        """R:R < 2:1 теперь не отбраковывается parser'ом.

        Source Nof1 говорит «minimum 2:1 reward-to-risk» как guidance в
        prompt'е, но не как server-side cap. LLM решает сам.
        """
        text = _wrap_json({
            "signal": "buy_to_enter", "coin": "ETH",
            "quantity": 0.05, "leverage": 2,
            "stop_loss": 3290.0, "profit_target": 3310.0,  # R:R = 1:1
            "invalidation_condition": "ETH below 3290",
            "confidence": 0.5, "risk_usd": 0.5,
            "justification": "scalp mean-reversion",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.signal == "buy_to_enter"

    def test_coin_with_usdt_suffix_rejected(self):
        """Source говорит coin без USDT (gist L168). USDT-вариант — старый
        формат, не из source. Должен отвергаться parser'ом, чтобы LLM
        не привыкал писать с USDT.
        """
        text = _wrap_json({
            "signal": "buy_to_enter", "coin": "BTCUSDT",  # старый формат
            "quantity": 0.001, "leverage": 3,
            "stop_loss": 60000.0, "profit_target": 65000.0,
            "invalidation_condition": "x", "justification": "y",
            "confidence": 0.5, "risk_usd": 1.0,
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, str)
        assert "not in allowed" in result


# ─── Edge cases / robustness ─────────────────────────────────────────────


class TestParserRobustness:
    def test_empty_text(self):
        assert parse_action("", SYMBOLS) == "empty response"

    def test_no_json(self):
        result = parse_action("Just commentary, no JSON here.", SYMBOLS)
        assert isinstance(result, str)
        assert "no JSON" in result or "parse error" in result.lower()

    def test_malformed_json(self):
        result = parse_action("```json\n{signal: buy_to_enter,\n```", SYMBOLS)
        assert isinstance(result, str)

    def test_picks_last_json_when_multiple(self):
        # LLM сначала пишет пример {"foo": 1}, потом реальное решение
        text = (
            'Example schema: {"foo": 1}\n\n'
            'My decision:\n```json\n{"signal": "hold"}\n```'
        )
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.signal == "hold"

    def test_json_without_fence(self):
        text = 'Decision below:\n{"signal": "hold"}'
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, ParsedAction)
        assert result.signal == "hold"


# ─── Validation errors (sanity-only) ─────────────────────────────────────


class TestValidationErrors:
    def test_invalid_signal(self):
        text = _wrap_json({"signal": "OPEN_LONG", "coin": "BTC"})
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, str)
        assert "invalid signal" in result

    def test_coin_not_in_whitelist(self):
        text = _wrap_json({
            "signal": "buy_to_enter", "coin": "LTC",  # вне Nof1 6-coin universe
            "quantity": 1, "leverage": 1,
            "stop_loss": 100, "profit_target": 200,
            "confidence": 0.5, "risk_usd": 1.0,
            "invalidation_condition": "x", "justification": "y",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, str)
        assert "not in allowed" in result

    def test_quantity_zero_for_entry(self):
        text = _wrap_json({
            "signal": "buy_to_enter", "coin": "BTC",
            "quantity": 0, "leverage": 3,
            "stop_loss": 60000, "profit_target": 65000,
            "confidence": 0.5, "risk_usd": 1.0,
            "invalidation_condition": "x", "justification": "y",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, str)
        assert "quantity" in result.lower()

    def test_negative_leverage(self):
        text = _wrap_json({
            "signal": "sell_to_enter", "coin": "BTC",
            "quantity": 0.01, "leverage": -1,
            "stop_loss": 65000, "profit_target": 60000,
            "confidence": 0.5, "risk_usd": 1.0,
            "invalidation_condition": "x", "justification": "y",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, str)

    def test_confidence_above_one(self):
        text = _wrap_json({
            "signal": "buy_to_enter", "coin": "BTC",
            "quantity": 0.001, "leverage": 2,
            "stop_loss": 60000, "profit_target": 65000,
            "confidence": 1.5, "risk_usd": 1.0,
            "invalidation_condition": "x", "justification": "y",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, str)
        assert "confidence" in result.lower()

    def test_missing_required_field(self):
        # quantity missing
        text = _wrap_json({
            "signal": "buy_to_enter", "coin": "BTC",
            "leverage": 2, "stop_loss": 60000, "profit_target": 65000,
            "confidence": 0.5, "risk_usd": 1.0,
            "invalidation_condition": "x", "justification": "y",
        })
        result = parse_action(text, SYMBOLS)
        assert isinstance(result, str)


class TestAllowedSignals:
    def test_canonical_set(self):
        assert ALLOWED_SIGNALS == {
            "buy_to_enter", "sell_to_enter", "hold", "close"
        }
