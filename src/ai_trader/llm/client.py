"""DeepSeek-V4 клиент через Anthropic-compatible API.

Используем `anthropic` SDK с переопределённым base_url, как описано в
https://api-docs.deepseek.com/guides/anthropic_api

Возвращаем сырой текст ответа + использованные токены.
Парсинг JSON живёт в trading/executor.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

log = logging.getLogger(__name__)


# Грубые цены DeepSeek-V4-Flash (на момент release apr 2026, могут уточниться).
# Цель — приблизительный учёт стоимости в БД, не биллинг.
COST_PER_M_INPUT_USD = 0.14
COST_PER_M_OUTPUT_USD = 0.28


@dataclass
class LlmResponse:
    text: str
    tokens_input: int
    tokens_output: int
    cost_usd: float
    error: str | None = None


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/anthropic",
        model: str = "deepseek-v4-flash",
        max_tokens: int = 2000,
        thinking_enabled: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is empty")
        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        self._model = model
        self._max_tokens = max_tokens
        self._thinking_enabled = thinking_enabled

    def ask(self, system_prompt: str, user_prompt: str) -> LlmResponse:
        try:
            kwargs: dict = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            if self._thinking_enabled:
                kwargs["thinking"] = {"type": "enabled"}
            msg = self._client.messages.create(**kwargs)
        except Exception as e:
            log.exception("DeepSeek API call failed")
            return LlmResponse(text="", tokens_input=0, tokens_output=0, cost_usd=0, error=str(e))

        text_parts: list[str] = []
        for block in msg.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))
            elif block_type == "thinking":
                # thinking-блок не является ответом для парсера, но логируем
                tk = getattr(block, "thinking", "")
                if tk:
                    log.debug("LLM thinking: %s", tk[:300])
        text = "\n".join(text_parts).strip()

        usage = getattr(msg, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0)) if usage else 0
        tokens_out = int(getattr(usage, "output_tokens", 0)) if usage else 0
        cost = tokens_in / 1_000_000 * COST_PER_M_INPUT_USD + tokens_out / 1_000_000 * COST_PER_M_OUTPUT_USD

        return LlmResponse(
            text=text,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            cost_usd=cost,
        )
