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

from ai_arena.llm.pricing import (
    DEFAULT_PRICING,
    MODEL_PRICES,
    ModelPricing,
    TokenUsage,
    extract_token_usage,
    get_pricing,
)
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


# ─── DeepSeek pricing + context-caching extraction ──────────────────────


class TestModelPricing:
    """Источник правды по ценам: api-docs.deepseek.com/quick_start/pricing.

    Зашитые в MODEL_PRICES числа — то что **реально списывается** со
    счёта на 2026-05-18 (V4-Pro = 75% off до 2026-05-31). После expiry
    discount тесты упадут — это намеренно, чтобы заметить смену цены.
    """

    def test_v4_pro_uses_discounted_prices(self):
        """V4-Pro: $0.003625/$0.435/$0.87 = list × 0.25 (75% off)."""
        p = MODEL_PRICES["deepseek-v4-pro"]
        assert p.cache_hit_per_m == pytest.approx(0.003625)
        assert p.cache_miss_per_m == pytest.approx(0.435)
        assert p.output_per_m == pytest.approx(0.87)

    def test_v4_flash_prices(self):
        p = MODEL_PRICES["deepseek-v4-flash"]
        assert p.cache_hit_per_m == pytest.approx(0.0028)
        assert p.cache_miss_per_m == pytest.approx(0.14)
        assert p.output_per_m == pytest.approx(0.28)

    def test_cache_hit_is_at_least_50x_cheaper_than_miss(self):
        """Sanity: cache_hit ВСЕГДА должен быть кратно дешевле miss
        (DeepSeek говорит ~100×). Если кто-то по ошибке заменил
        одно на другое — поймаем."""
        for name, p in MODEL_PRICES.items():
            ratio = p.cache_miss_per_m / max(p.cache_hit_per_m, 1e-9)
            assert ratio >= 50, (
                f"{name}: cache_miss/{p.cache_hit_per_m}={ratio:.1f}× — "
                "ожидаем >=50× (DeepSeek docs: ~100×). "
                "Возможно местами перепутаны цены."
            )

    def test_legacy_aliases_map_to_flash(self):
        """deepseek-chat / deepseek-reasoner сейчас роутятся в v4-flash
        (см. changelog 2026-04-24, retire 2026-07-24)."""
        flash = MODEL_PRICES["deepseek-v4-flash"]
        assert MODEL_PRICES["deepseek-chat"] == flash
        assert MODEL_PRICES["deepseek-reasoner"] == flash

    def test_get_pricing_unknown_falls_back_to_default(self, caplog):
        result = get_pricing("deepseek-v5-experimental-xyz")
        assert result == DEFAULT_PRICING
        assert any("Unknown model" in r.message for r in caplog.records)

    def test_get_pricing_none_returns_default(self):
        assert get_pricing(None) == DEFAULT_PRICING

    def test_get_pricing_case_insensitive(self):
        assert get_pricing("DeepSeek-V4-Pro") == MODEL_PRICES["deepseek-v4-pro"]

    def test_cost_calculation_v4_pro_full_miss(self):
        """Без context caching: 10k input miss, 200 output = расчёт по
        реальным ценам (а не нашему старому $0.27/$1.10)."""
        p = MODEL_PRICES["deepseek-v4-pro"]
        cost = p.cost(
            cache_hit_tokens=0,
            cache_miss_tokens=10_000,
            output_tokens=200,
        )
        # 10_000 * 0.435 / 1M + 200 * 0.87 / 1M = 0.00435 + 0.000174 = 0.004524
        assert cost == pytest.approx(0.004524, abs=1e-6)

    def test_cost_calculation_v4_pro_with_caching(self):
        """С caching: 80% input из кэша — стоимость ниже почти в 5×."""
        p = MODEL_PRICES["deepseek-v4-pro"]
        cost_full_miss = p.cost(
            cache_hit_tokens=0, cache_miss_tokens=10_000, output_tokens=200,
        )
        cost_with_cache = p.cost(
            cache_hit_tokens=8_000, cache_miss_tokens=2_000, output_tokens=200,
        )
        # 8000*0.003625/M + 2000*0.435/M + 200*0.87/M = 0.000029 + 0.00087 + 0.000174 = 0.001073
        assert cost_with_cache == pytest.approx(0.001073, abs=1e-6)
        # Кэш экономит существенно — должно быть в 3+ раза дешевле
        assert cost_full_miss / cost_with_cache >= 3.0


class TestExtractTokenUsage:
    """Defensive parser usage-блока: DeepSeek Anthropic-compat не
    декларирует явно имя cache-полей. Пробуем оба стиля (свой OpenAI
    style + Anthropic native).
    """

    def test_none_usage_returns_zeros(self):
        u = extract_token_usage(None)
        assert u == TokenUsage(0, 0, 0, 0)

    def test_deepseek_native_field_names_dict(self):
        """OpenAI-style имена (DeepSeek's native кэширования)."""
        u = extract_token_usage({
            "input_tokens": 5000,
            "output_tokens": 200,
            "prompt_cache_hit_tokens": 4000,
            "prompt_cache_miss_tokens": 1000,
        })
        assert u.input_tokens == 5000
        assert u.output_tokens == 200
        assert u.cache_hit_tokens == 4000
        assert u.cache_miss_tokens == 1000

    def test_anthropic_native_semantics(self):
        """Anthropic-style semantics: ``input_tokens`` это **non-cache**
        (по факту miss), ``cache_read_input_tokens`` суммируется СВЕРХУ.

        DeepSeek через Anthropic-compat endpoint использует именно эту
        семантику (эмпирически подтверждено на live-запросе 2026-05-18:
        input_tokens=4113 и cache_read=2304 пришли одновременно — для
        DeepSeek-native это значило бы что hit это **часть** от 4113).
        """
        u = extract_token_usage({
            "input_tokens": 1000,             # non-cache input (= miss)
            "output_tokens": 200,
            "cache_read_input_tokens": 4000,  # hit
            "cache_creation_input_tokens": 0,
        })
        assert u.cache_hit_tokens == 4000, "hit берётся из cache_read"
        assert u.cache_miss_tokens == 1000, "miss = input_tokens + cache_creation"
        assert u.input_tokens == 5000, (
            "total input = miss + hit (для совместимости с DeepSeek-style)"
        )

    def test_anthropic_native_with_cache_creation(self):
        """cache_creation_input_tokens — это miss-tokens, которые ПРЯМО
        СЕЙЧАС создают cache entry (billable как обычный input)."""
        u = extract_token_usage({
            "input_tokens": 500,
            "output_tokens": 200,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 1500,
        })
        assert u.cache_hit_tokens == 2000
        assert u.cache_miss_tokens == 2000, "500 non-cache + 1500 cache_creation"
        assert u.input_tokens == 4000

    def test_deepseek_native_takes_priority_over_anthropic(self):
        """Если случайно пришли поля обоих стилей — приоритет у
        DeepSeek-native (т.к. там точная семантика hit+miss=total)."""
        u = extract_token_usage({
            "input_tokens": 5000,
            "output_tokens": 200,
            "prompt_cache_hit_tokens": 3000,
            "prompt_cache_miss_tokens": 2000,
            "cache_read_input_tokens": 999,    # должно игнорироваться
            "cache_creation_input_tokens": 999,
        })
        assert u.cache_hit_tokens == 3000
        assert u.cache_miss_tokens == 2000

    def test_no_cache_fields_treats_all_as_miss(self):
        """Если cache-полей нет — безопасный fallback: весь input = miss
        (цена не занижается)."""
        u = extract_token_usage({
            "input_tokens": 5000,
            "output_tokens": 200,
        })
        assert u.cache_hit_tokens == 0
        assert u.cache_miss_tokens == 5000

    def test_works_with_object_attributes(self):
        """SDK обычно отдаёт объект, не dict."""

        class _Usage:
            input_tokens = 5000
            output_tokens = 200
            prompt_cache_hit_tokens = 3000
            prompt_cache_miss_tokens = 2000

        u = extract_token_usage(_Usage())
        assert u.cache_hit_tokens == 3000
        assert u.cache_miss_tokens == 2000

    def test_cache_hit_rate_property(self):
        u = TokenUsage(
            input_tokens=5000, output_tokens=200,
            cache_hit_tokens=4000, cache_miss_tokens=1000,
        )
        assert u.cache_hit_rate == pytest.approx(0.8)

    def test_cache_hit_rate_zero_input(self):
        u = TokenUsage(0, 0, 0, 0)
        assert u.cache_hit_rate == 0.0

    def test_invalid_values_dont_crash(self):
        """SDK может вернуть строку или None в редких случаях."""
        u = extract_token_usage({
            "input_tokens": "garbage",
            "output_tokens": None,
        })
        assert u.input_tokens == 0
        assert u.output_tokens == 0
