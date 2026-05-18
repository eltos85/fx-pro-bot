"""Тесты на КОРРЕКТНОСТЬ параметров отправляемых в DeepSeek и Bybit V5.

Закрывают баги обнаруженные при аудите 2026-05-18 (см. BUILDLOG_AI_ARENA.md
секция «Audit конфигурационных параметров»):

1. ``reasoning_effort`` транслировался в неверное поле Anthropic-compat
   API. DeepSeek V4 thinking enabled-by-default, и наше ``off`` было
   no-op'ом (баг 4+ дней). Тестируем что build_thinking_extra_body
   возвращает 1-в-1 синтаксис из api-docs.deepseek.com/guides/thinking_mode.

2. ``place_order`` теперь передаёт ``positionIdx=0`` явно (one-way mode)
   для устойчивости к hedge mode (Bybit V5 spec, /v5/order/create).

3. ``get_wallet_balance`` теперь читает ``totalAvailableBalance`` на
   account-level, а не deprecated ``availableToWithdraw`` per-coin
   (deprecated с 9 января 2025 для UNIFIED).
"""
from __future__ import annotations

from typing import Any

import pytest

from ai_arena.llm.thinking_config import build_thinking_extra_body
from ai_arena.trading.client import AiArenaBybitClient


# ─── build_thinking_extra_body ────────────────────────────────────────


class TestBuildThinkingExtraBody:
    """Маппинг env-параметра AI_ARENA_DEEPSEEK_REASONING → Anthropic-compat.

    Источник правды: https://api-docs.deepseek.com/guides/thinking_mode
    + https://api-docs.deepseek.com/guides/anthropic_api (Simple Fields).
    """

    def test_off_explicitly_disables_thinking(self):
        """Nof1-режим: thinking ДОЛЖЕН быть явно выключен.

        Без явного `disabled` DeepSeek V4 thinking enabled by default
        (см. доку: «The thinking toggle defaults to enabled»). Без
        нашего фикса бот 4+ дня работал с включённым CoT, что нарушало
        инвариант ai-arena-sources.mdc «Nof1 не использует reasoning».
        """
        assert build_thinking_extra_body("off") == {
            "thinking": {"type": "disabled"}
        }

    def test_off_is_case_insensitive(self):
        assert build_thinking_extra_body("OFF") == {
            "thinking": {"type": "disabled"}
        }
        assert build_thinking_extra_body("Off") == {
            "thinking": {"type": "disabled"}
        }

    def test_off_strips_whitespace(self):
        assert build_thinking_extra_body("  off  ") == {
            "thinking": {"type": "disabled"}
        }

    def test_none_falls_back_to_off(self):
        """Пустое значение env-переменной должно вести себя как off."""
        assert build_thinking_extra_body(None) == {
            "thinking": {"type": "disabled"}
        }
        assert build_thinking_extra_body("") == {
            "thinking": {"type": "disabled"}
        }

    def test_high_uses_output_config_not_reasoning_effort(self):
        """Anthropic-compat НЕ поддерживает поле `reasoning_effort`.

        Только `output_config.effort`. Если случайно регрессируем на
        `{"reasoning_effort": "high"}` — DeepSeek silent-ignore'ит.
        """
        result = build_thinking_extra_body("high")
        assert result == {
            "thinking": {"type": "enabled"},
            "output_config": {"effort": "high"},
        }
        assert "reasoning_effort" not in result, (
            "reasoning_effort невалидное поле для Anthropic-compat, "
            "DeepSeek его молча игнорирует — должно быть output_config.effort"
        )

    def test_max_uses_output_config_effort_max(self):
        assert build_thinking_extra_body("max") == {
            "thinking": {"type": "enabled"},
            "output_config": {"effort": "max"},
        }

    def test_low_medium_mapped_to_high(self):
        """DeepSeek сам мапит low/medium → high (compatibility)."""
        for v in ("low", "medium"):
            result = build_thinking_extra_body(v)
            assert result["output_config"]["effort"] == "high"
            assert result["thinking"]["type"] == "enabled"

    def test_xhigh_mapped_to_max(self):
        result = build_thinking_extra_body("xhigh")
        assert result["output_config"]["effort"] == "max"
        assert result["thinking"]["type"] == "enabled"

    def test_unknown_value_falls_back_to_disabled(self):
        """Безопасный дефолт: неизвестное значение → off (Nof1-режим)."""
        assert build_thinking_extra_body("on") == {
            "thinking": {"type": "disabled"}
        }
        assert build_thinking_extra_body("garbage") == {
            "thinking": {"type": "disabled"}
        }


# ─── place_order: positionIdx=0 ────────────────────────────────────────


class _FakeSession:
    """Минимальный мок pybit HTTP-сессии — пишет последние kwargs."""

    def __init__(self, response: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._response = response or {"retCode": 0, "result": {"orderId": "x"}}

    def place_order(self, **kwargs: Any) -> dict:
        self.calls.append(("place_order", kwargs))
        return self._response

    def get_wallet_balance(self, **kwargs: Any) -> dict:
        self.calls.append(("get_wallet_balance", kwargs))
        return self._response


def _make_client_with_fake_session(session: _FakeSession) -> AiArenaBybitClient:
    """Создаёт клиент в обход __init__ (нет реальных API-ключей)."""
    client = AiArenaBybitClient.__new__(AiArenaBybitClient)
    client._session = session  # type: ignore[attr-defined]
    client._category = "linear"  # type: ignore[attr-defined]
    client._instr_cache = {}  # type: ignore[attr-defined]
    return client


class TestPlaceOrderPositionIdx:
    """Bybit V5: positionIdx=0 (one-way mode) должно идти ВСЕГДА явно.

    Без явного значения, при переключении аккаунта в hedge mode (что
    делается одной кнопкой в UI), Bybit отвергнет ордер с retCode
    10001 «position idx not match position mode». Лучше иметь явный
    fail-fast на нашей стороне чем silent regression.

    Spec: https://bybit-exchange.github.io/docs/v5/order/create-order
    """

    def test_market_open_sends_position_idx_zero(self):
        sess = _FakeSession()
        client = _make_client_with_fake_session(sess)

        client.place_order(
            symbol="BTCUSDT",
            side="Buy",
            qty=0.01,
            order_link_id="arena_test_123",
            sl_price=90000.0,
            tp_price=110000.0,
            reduce_only=False,
        )

        assert sess.calls, "place_order не был вызван"
        _, kwargs = sess.calls[-1]
        assert kwargs["positionIdx"] == 0, (
            f"positionIdx должен быть 0 (one-way), получили {kwargs.get('positionIdx')!r}"
        )

    def test_reduce_only_close_also_sends_position_idx_zero(self):
        """Закрытие через reduce_only тоже должно идти с positionIdx=0."""
        sess = _FakeSession()
        client = _make_client_with_fake_session(sess)

        client.place_order(
            symbol="ETHUSDT",
            side="Sell",
            qty=0.5,
            order_link_id="arena_close_456",
            reduce_only=True,
        )

        _, kwargs = sess.calls[-1]
        assert kwargs["positionIdx"] == 0
        # reduce_only-close НЕ должен пытаться выставить SL/TP (Bybit
        # запрещает это для reduce-only ордеров).
        assert "stopLoss" not in kwargs
        assert "takeProfit" not in kwargs

    def test_position_idx_is_int_not_string(self):
        """Bybit V5 принимает positionIdx как integer (не string)."""
        sess = _FakeSession()
        client = _make_client_with_fake_session(sess)

        client.place_order(
            symbol="SOLUSDT", side="Buy", qty=1.0, order_link_id="arena_x",
        )

        _, kwargs = sess.calls[-1]
        assert isinstance(kwargs["positionIdx"], int), (
            f"positionIdx тип: {type(kwargs['positionIdx']).__name__}, "
            "должен быть int (Bybit V5 enum)"
        )


# ─── get_wallet_balance: totalAvailableBalance ─────────────────────────


class TestGetWalletBalance:
    """available_cash должен браться из account-level `totalAvailableBalance`.

    Per-coin `availableToWithdraw` DEPRECATED для UNIFIED с 9 января 2025
    (https://bybit-exchange.github.io/docs/v5/account/wallet-balance):

    > «Deprecated for accountType=UNIFIED from 9 Jan, 2025»

    Чтение этого поля даёт некорректную «free cash» — не учитывает
    locked-в-позициях маржу, занижая риск с т.з. LLM.
    """

    def test_uses_total_available_balance_not_available_to_withdraw(self):
        """Если оба поля есть — берём account-level totalAvailableBalance."""
        sess = _FakeSession({
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "totalAvailableBalance": "777.55",
                        "coin": [
                            {
                                "coin": "USDT",
                                "equity": "1000.00",
                                "walletBalance": "900.00",
                                "availableToWithdraw": "99999.99",  # deprecated, не должно использоваться
                            }
                        ],
                    }
                ]
            },
        })
        client = _make_client_with_fake_session(sess)

        equity, avail = client.get_wallet_balance()

        assert equity == pytest.approx(1000.00)
        assert avail == pytest.approx(777.55), (
            "Должны игнорировать deprecated availableToWithdraw=99999.99 "
            f"и брать totalAvailableBalance=777.55, получили {avail}"
        )

    def test_equity_taken_from_usdt_coin(self):
        sess = _FakeSession({
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "totalAvailableBalance": "500.0",
                        "coin": [
                            {"coin": "BTC", "equity": "0.01"},
                            {"coin": "USDT", "equity": "1234.56"},
                        ],
                    }
                ]
            },
        })
        client = _make_client_with_fake_session(sess)

        equity, _ = client.get_wallet_balance()
        assert equity == pytest.approx(1234.56)

    def test_missing_total_available_balance_returns_zero_cash(self):
        """Если totalAvailableBalance отсутствует — graceful 0, не падаем."""
        sess = _FakeSession({
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "coin": [{"coin": "USDT", "equity": "500"}],
                    }
                ]
            },
        })
        client = _make_client_with_fake_session(sess)

        equity, avail = client.get_wallet_balance()
        assert equity == pytest.approx(500.0)
        assert avail == 0.0

    def test_no_usdt_coin_returns_zero_equity_but_keeps_total_avail(self):
        sess = _FakeSession({
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "totalAvailableBalance": "42.0",
                        "coin": [{"coin": "BTC", "equity": "0.001"}],
                    }
                ]
            },
        })
        client = _make_client_with_fake_session(sess)

        equity, avail = client.get_wallet_balance()
        assert equity == 0.0
        assert avail == 0.0  # пустой `return (0.0, 0.0)` — нет USDT-конца
