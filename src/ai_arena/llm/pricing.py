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

    Поддерживает ДВЕ разные семантики usage-блока (важная разница в том,
    что значит ``input_tokens``):

    1. **DeepSeek native** (OpenAI-style,
       https://api-docs.deepseek.com/guides/kv_cache):
       - ``prompt_tokens`` (= ``input_tokens`` в нашем SDK-маппинге) —
         **ВСЁ** input total
       - ``prompt_cache_hit_tokens`` — часть из total, обслужена кэшом
       - ``prompt_cache_miss_tokens`` — остальная часть
       - Инвариант: ``hit + miss == prompt_tokens``

    2. **Anthropic native**
       (https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching):
       - ``input_tokens`` — **только non-cache** (по факту = miss)
       - ``cache_read_input_tokens`` — hit (отдельно, **прибавляется**)
       - ``cache_creation_input_tokens`` — tokens создавшие cache entry
         сейчас (платятся как input, и в будущем могут стать hit)
       - Инвариант: total billable input = input_tokens +
         cache_read_input_tokens + cache_creation_input_tokens

    DeepSeek через свой Anthropic-compat endpoint использует
    **Anthropic-семантику** (эмпирически: `input_tokens=4113`,
    `cache_read_input_tokens=2304` приходят одновременно — для DeepSeek-
    native это было бы `prompt_tokens=4113` и hit как **часть** от него).

    Защита: автоматически определяем семантику по наличию полей, чтобы
    не привязываться к одному endpoint'у. Если ничего не нашли —
    fallback в безопасный режим (всё считается cache_miss, цена не
    занижается).
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

    raw_input = _get("input_tokens")
    output_tokens = _get("output_tokens")

    ds_hit = _get("prompt_cache_hit_tokens")
    ds_miss = _get("prompt_cache_miss_tokens")
    if ds_hit > 0 or ds_miss > 0:
        if abs((ds_hit + ds_miss) - raw_input) > 2:
            log.warning(
                "DeepSeek-native usage: hit+miss=%d != input_tokens=%d",
                ds_hit + ds_miss, raw_input,
            )
        return TokenUsage(
            input_tokens=raw_input,
            output_tokens=output_tokens,
            cache_hit_tokens=ds_hit,
            cache_miss_tokens=ds_miss,
        )

    anth_read = _get("cache_read_input_tokens")
    anth_create = _get("cache_creation_input_tokens")
    if anth_read > 0 or anth_create > 0:
        miss_billable = raw_input + anth_create
        total_input = miss_billable + anth_read
        return TokenUsage(
            input_tokens=total_input,
            output_tokens=output_tokens,
            cache_hit_tokens=anth_read,
            cache_miss_tokens=miss_billable,
        )

    return TokenUsage(
        input_tokens=raw_input,
        output_tokens=output_tokens,
        cache_hit_tokens=0,
        cache_miss_tokens=raw_input,
    )
