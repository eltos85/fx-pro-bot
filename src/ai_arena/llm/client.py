"""DeepSeek V4-Pro клиент через Anthropic-compatible API.

Используем `anthropic` SDK с переопределённым base_url
(см. https://api-docs.deepseek.com/guides/anthropic_api).

Архитектурное отличие от ai_trader/llm/client.py: **БЕЗ thinking-блоков**.
Nof1 не использует reasoning-mode (см. gist nof1-prompt.md, ответ автора
на вопрос ForeverInLaw: «No, they don't use reasoning mode. … Chain-of-thought
is implemented through prompt engineering: structured JSON output with
required fields (justification, confidence, invalidation_condition)
forces the model to show reasoning»). Это инвариант правила
`ai-arena-sources.mdc`.

DeepSeek thinking-mode управление (Anthropic-compat, см.
https://api-docs.deepseek.com/guides/thinking_mode и /guides/anthropic_api):

- **По умолчанию для V4-моделей thinking ENABLED** (default effort=high).
  Чтобы получить поведение Nof1 (без CoT-блока) — нужно ЯВНО передать
  ``extra_body={"thinking": {"type": "disabled"}}``. Без этого даже
  reasoning_effort=off на нашей стороне игнорируется и thinking всё
  равно работает (баг до 2026-05-18 — см. BUILDLOG_AI_ARENA.md).

- Toggle: ``{"thinking": {"type": "enabled" | "disabled"}}``
- Effort: ``{"output_config": {"effort": "high" | "max"}}``
  (low/medium → high, xhigh → max — мапинг DeepSeek).

OpenAI-format ``reasoning_effort`` через ``extra_body`` НЕ работает в
Anthropic-compat endpoint — это поле отсутствует в списке supported
fields доки. Использовать ТОЛЬКО ``thinking`` / ``output_config``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import anthropic

from ai_arena.llm.thinking_config import build_thinking_extra_body

log = logging.getLogger(__name__)


# Цены DeepSeek V4-Pro на момент 2026-04-24 (release date) — могут
# уточниться, см. api-docs.deepseek.com. Цель — приблизительный учёт
# в БД (cost_usd в decisions), не биллинг.
COST_PER_M_INPUT_USD = 0.27
COST_PER_M_OUTPUT_USD = 1.10


@dataclass
class LlmResponse:
    text: str
    tokens_input: int
    tokens_output: int
    cost_usd: float
    error: str | None = None


class DeepSeekArenaClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/anthropic",
        model: str = "deepseek-v4-pro",
        max_tokens: int = 8192,
        reasoning_effort: str = "off",
        retry_on_empty: int = 1,
        retry_sleep_sec: float = 5.0,
    ) -> None:
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is empty")
        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        self._model = model
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._retry_on_empty = max(0, retry_on_empty)
        self._retry_sleep_sec = max(0.0, retry_sleep_sec)

    def ask(self, system_prompt: str, user_prompt: str) -> LlmResponse:
        attempts = self._retry_on_empty + 1
        last: LlmResponse | None = None
        for attempt in range(1, attempts + 1):
            resp = self._call(system_prompt, user_prompt)
            last = resp
            if resp.error:
                return resp
            if resp.text:
                return resp
            if attempt < attempts:
                log.warning(
                    "LLM empty response (attempt %d/%d), retrying in %.1fs",
                    attempt, attempts, self._retry_sleep_sec,
                )
                time.sleep(self._retry_sleep_sec)
        if last is None:
            return LlmResponse(text="", tokens_input=0, tokens_output=0, cost_usd=0, error="no attempts")
        if not last.text and last.error is None:
            return LlmResponse(
                text="",
                tokens_input=last.tokens_input,
                tokens_output=last.tokens_output,
                cost_usd=last.cost_usd,
                error=f"empty response after {attempts} attempts",
            )
        return last

    def _call(self, system_prompt: str, user_prompt: str) -> LlmResponse:
        try:
            kwargs: dict = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            extra = build_thinking_extra_body(self._reasoning_effort)
            if extra:
                kwargs["extra_body"] = extra
            msg = self._client.messages.create(**kwargs)
        except Exception as e:
            log.exception("DeepSeek API call failed")
            return LlmResponse(text="", tokens_input=0, tokens_output=0, cost_usd=0, error=str(e))

        text_parts: list[str] = []
        for block in msg.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))
            # Игнорируем любые thinking-блоки если случайно прилетят:
            # Nof1-style единственный канал CoT — required JSON-поля.
        text = "\n".join(text_parts).strip()

        usage = getattr(msg, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0)) if usage else 0
        tokens_out = int(getattr(usage, "output_tokens", 0)) if usage else 0
        cost = (
            tokens_in / 1_000_000 * COST_PER_M_INPUT_USD
            + tokens_out / 1_000_000 * COST_PER_M_OUTPUT_USD
        )
        return LlmResponse(text=text, tokens_input=tokens_in, tokens_output=tokens_out, cost_usd=cost)
