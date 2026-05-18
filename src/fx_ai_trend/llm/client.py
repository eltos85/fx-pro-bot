"""DeepSeek-V4 клиент для FX AI Trader.

Чистый shim над ``ai_trader.llm.client`` — это DeepSeek через
Anthropic-compatible endpoint (https://api-docs.deepseek.com/guides/anthropic_api),
универсальный для обоих ботов.
"""
from __future__ import annotations

from ai_trader.llm.client import (
    COST_PER_M_INPUT_USD,
    COST_PER_M_OUTPUT_USD,
    DeepSeekClient,
    LlmResponse,
)

__all__ = [
    "COST_PER_M_INPUT_USD",
    "COST_PER_M_OUTPUT_USD",
    "DeepSeekClient",
    "LlmResponse",
]
