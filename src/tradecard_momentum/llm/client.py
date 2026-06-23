"""DeepSeek-клиент tradecard_momentum (Anthropic-compatible API), read-only.

Изолированная копия клиента fx_ai_trader — пакет самостоятельный, без
кросс-зависимости (TASKSPEC §9). Используется **только** для аналитики 5 Why,
ничего не меняет в торговле.

DeepSeek через Anthropic-compat: https://api-docs.deepseek.com/guides/anthropic_api
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import anthropic

log = logging.getLogger("tradecard_momentum.llm")

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
    def __init__(self, api_key: str,
                 base_url: str = "https://api.deepseek.com/anthropic",
                 model: str = "deepseek-v4-flash", max_tokens: int = 8192,
                 thinking_enabled: bool = True, retry_on_empty: int = 1,
                 retry_sleep_sec: float = 5.0) -> None:
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is empty")
        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        self._model = model
        self._max_tokens = max_tokens
        self._thinking_enabled = thinking_enabled
        self._retry_on_empty = max(0, retry_on_empty)
        self._retry_sleep_sec = max(0.0, retry_sleep_sec)

    def ask(self, system_prompt: str, user_prompt: str) -> LlmResponse:
        attempts = self._retry_on_empty + 1
        last: LlmResponse | None = None
        for attempt in range(1, attempts + 1):
            resp = self._call(system_prompt, user_prompt,
                              with_thinking=self._thinking_enabled)
            last = resp
            if resp.error or resp.text:
                return resp
            if attempt < attempts:
                log.warning("LLM empty (attempt %d/%d), retry in %.1fs",
                            attempt, attempts, self._retry_sleep_sec)
                time.sleep(self._retry_sleep_sec)
        if (self._thinking_enabled and last is not None and not last.text
                and last.error is None):
            log.warning("LLM still empty — final fallback без thinking")
            fb = self._call(system_prompt, user_prompt, with_thinking=False)
            if fb.text or fb.error:
                return fb
            last = fb
        if last is None:
            return LlmResponse("", 0, 0, 0, error="no attempts")
        if not last.text and last.error is None:
            return LlmResponse("", last.tokens_input, last.tokens_output,
                               last.cost_usd, error="empty response")
        return last

    def _call(self, system_prompt: str, user_prompt: str, *,
              with_thinking: bool) -> LlmResponse:
        try:
            kwargs: dict = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            if with_thinking:
                kwargs["thinking"] = {"type": "enabled"}
            msg = self._client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            log.exception("DeepSeek API call failed")
            return LlmResponse("", 0, 0, 0, error=str(e))

        text_parts: list[str] = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))
        text = "\n".join(text_parts).strip()
        usage = getattr(msg, "usage", None)
        tin = int(getattr(usage, "input_tokens", 0)) if usage else 0
        tout = int(getattr(usage, "output_tokens", 0)) if usage else 0
        cost = (tin / 1_000_000 * COST_PER_M_INPUT_USD
                + tout / 1_000_000 * COST_PER_M_OUTPUT_USD)
        return LlmResponse(text=text, tokens_input=tin, tokens_output=tout,
                           cost_usd=cost)
