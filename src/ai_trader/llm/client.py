"""DeepSeek-V4 клиент через Anthropic-compatible API.

Используем `anthropic` SDK с переопределённым base_url, как описано в
https://api-docs.deepseek.com/guides/anthropic_api

Возвращаем сырой текст ответа + использованные токены.
Парсинг JSON живёт в trading/executor.py.
"""
from __future__ import annotations

import logging
import time
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
        max_tokens: int = 4096,
        thinking_enabled: bool = True,
        retry_on_empty: int = 1,
        retry_sleep_sec: float = 5.0,
    ) -> None:
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
        last_response: LlmResponse | None = None
        for attempt in range(1, attempts + 1):
            resp = self._call(system_prompt, user_prompt, with_thinking=self._thinking_enabled)
            last_response = resp
            if resp.error:
                return resp
            if resp.text:
                return resp
            if attempt < attempts:
                log.warning(
                    "LLM empty response (attempt %d/%d), retrying in %.1fs",
                    attempt,
                    attempts,
                    self._retry_sleep_sec,
                )
                time.sleep(self._retry_sleep_sec)
        # Fallback: thinking-блоки забирают весь бюджет max_tokens, на
        # text-блоки ничего не остаётся. Делаем последнюю попытку БЕЗ
        # thinking — это reliable выход без `thinking_budget`-tax.
        if (
            self._thinking_enabled
            and last_response is not None
            and not last_response.text
            and last_response.error is None
        ):
            log.warning("LLM still empty — final fallback без thinking")
            fallback = self._call(system_prompt, user_prompt, with_thinking=False)
            if fallback.text or fallback.error:
                return fallback
            last_response = fallback
        if last_response is None:
            return LlmResponse(text="", tokens_input=0, tokens_output=0, cost_usd=0, error="no attempts")
        if not last_response.text and last_response.error is None:
            return LlmResponse(
                text="",
                tokens_input=last_response.tokens_input,
                tokens_output=last_response.tokens_output,
                cost_usd=last_response.cost_usd,
                error=f"empty response after {attempts} attempts (+1 no-thinking fallback)",
            )
        return last_response

    def _call(
        self, system_prompt: str, user_prompt: str, *, with_thinking: bool,
    ) -> LlmResponse:
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
