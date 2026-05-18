"""DeepSeek pricing + token-usage extraction (pure, без `anthropic` SDK).

Вынесено в отдельный модуль чтобы:
- Тесты могли импортировать без `anthropic` (test-env только pure deps).
- Цены / парсинг usage не размазывались по бизнес-логике.

Источник правды (правило api-docs.mdc):
- https://api-docs.deepseek.com/quick_start/pricing
- https://api-docs.deepseek.com/guides/kv_cache

Цены в долларах за 1M tokens. **Context caching включён по умолчанию**
для всех V4-моделей, прозрачно для клиента (кэш hit input в ~100×
дешевле miss). Чтобы реально измерять цену надо разделять hit/miss
из ``usage``.

ВАЖНО: V4-Pro на 75% скидке до 2026-05-31 15:59 UTC. После этой даты
цены должны быть переключены на list (флаг ``v4_pro_discount_active``
в ``MODEL_PRICES`` или ручная правка). Сейчас зашиты discounted-цены
как «то что реально платим».
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPricing:
    """Цены в $$ за 1M tokens.

    DeepSeek pricing structure:
    - ``cache_hit``: input tokens, попавшие в KV-cache (на ~100×
      дешевле). Доступно автоматически для повторяющихся prefix'ов.
    - ``cache_miss``: input tokens, НЕ попавшие в кэш.
    - ``output``: completion tokens.
    """

    cache_hit_per_m: float
    cache_miss_per_m: float
    output_per_m: float

    def cost(
        self,
        *,
        cache_hit_tokens: int,
        cache_miss_tokens: int,
        output_tokens: int,
    ) -> float:
        return (
            cache_hit_tokens / 1_000_000 * self.cache_hit_per_m
            + cache_miss_tokens / 1_000_000 * self.cache_miss_per_m
            + output_tokens / 1_000_000 * self.output_per_m
        )


# Действующие цены на 2026-05-18 (см. api-docs.deepseek.com/quick_start/pricing).
# V4-Pro: 75% off до 2026-05-31 15:59 UTC. Цены ниже — это **то что
# реально списывается со счёта** (discounted). После 31 мая переключить
# на list-prices ($0.0145 / $1.74 / $3.48).
#
# Тестовые fixtures используют эти же значения чтобы избежать дрейфа
# (см. tests/test_ai_arena_api_params.py::TestPricing).
MODEL_PRICES: dict[str, ModelPricing] = {
    "deepseek-v4-pro": ModelPricing(
        cache_hit_per_m=0.003625,   # 75% off от $0.0145
        cache_miss_per_m=0.435,     # 75% off от $1.74
        output_per_m=0.87,          # 75% off от $3.48
    ),
    "deepseek-v4-flash": ModelPricing(
        cache_hit_per_m=0.0028,
        cache_miss_per_m=0.14,
        output_per_m=0.28,
    ),
    # Legacy aliases — DeepSeek сейчас роутит их в v4-flash
    # (см. changelog 2026-04-24). Удалятся 2026-07-24, после этой
    # даты можно убрать.
    "deepseek-chat": ModelPricing(
        cache_hit_per_m=0.0028,
        cache_miss_per_m=0.14,
        output_per_m=0.28,
    ),
    "deepseek-reasoner": ModelPricing(
        cache_hit_per_m=0.0028,
        cache_miss_per_m=0.14,
        output_per_m=0.28,
    ),
}

# Fallback для неизвестных моделей — V4-Pro как safer-upper-bound:
# его цены выше Flash, лучше переоценить чем недооценить расходы.
DEFAULT_PRICING: ModelPricing = MODEL_PRICES["deepseek-v4-pro"]


def get_pricing(model: str | None) -> ModelPricing:
    """Цены для модели. Неизвестная модель → ``DEFAULT_PRICING`` + warning."""
    if not model:
        return DEFAULT_PRICING
    price = MODEL_PRICES.get(model.strip().lower())
    if price is None:
        log.warning(
            "Unknown model %r in MODEL_PRICES, using DEFAULT (v4-pro). "
            "Обнови llm/pricing.py если это новая модель.",
            model,
        )
        return DEFAULT_PRICING
    return price


@dataclass(frozen=True)
class TokenUsage:
    """Распарсенный usage-блок ответа DeepSeek.

    Инварианты:
    - ``cache_hit_tokens + cache_miss_tokens == input_tokens`` (если
      provider честно отдаёт раздельные числа). Иначе попадаем в
      fallback (всё считается miss).
    - ``cache_hit_tokens`` может быть 0 (первый запрос) или >0
      (последующие с тем же prefix).
    """

    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int

    @property
    def cache_hit_rate(self) -> float:
        """Доля input, обслуженная из кэша (0.0–1.0)."""
        if self.input_tokens <= 0:
            return 0.0
        return self.cache_hit_tokens / self.input_tokens


def extract_token_usage(usage: Any) -> TokenUsage:
    """Достаёт raw-числа из ``msg.usage`` (объект Anthropic SDK или dict).

    Defensive: DeepSeek через свой Anthropic-compat endpoint может
    возвращать поля кэша в двух стилях:

    1. **DeepSeek native** (OpenAI-style):
       ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
       (https://api-docs.deepseek.com/guides/kv_cache).

    2. **Anthropic native**:
       ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
       (https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching).

    Какой именно используется DeepSeek в Anthropic-compat — в их доке не
    указано явно. Пробуем оба варианта; если ни один не найден — fallback
    в безопасный режим (всё считается cache_miss, цена не занижается).

    Принимает либо объект (с attr-доступом, как Anthropic SDK), либо
    dict — для устойчивости к тестам и возможной смене SDK.
    """
    if usage is None:
        return TokenUsage(0, 0, 0, 0)

    def _get(key: str, default: int = 0) -> int:
        if isinstance(usage, dict):
            v = usage.get(key, default)
        else:
            v = getattr(usage, key, default)
        try:
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    input_tokens = _get("input_tokens")
    output_tokens = _get("output_tokens")

    cache_hit = _get("prompt_cache_hit_tokens") or _get("cache_read_input_tokens")
    cache_miss = (
        _get("prompt_cache_miss_tokens")
        or _get("cache_creation_input_tokens")
    )

    if cache_hit == 0 and cache_miss == 0:
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=0,
            cache_miss_tokens=input_tokens,
        )

    if cache_hit + cache_miss > 0 and abs((cache_hit + cache_miss) - input_tokens) > 2:
        log.warning(
            "cache_hit + cache_miss = %d, input_tokens = %d (расхождение). "
            "Возможно поля несинхронны — используем как есть.",
            cache_hit + cache_miss, input_tokens,
        )

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
    )
