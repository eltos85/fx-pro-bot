"""Опциональный LLM-комментарий к дайджесту (DeepSeek OpenAI-compatible)."""
from __future__ import annotations

import logging

import requests

from ru_stocks_analyst.analysis.screener import SwingIdea

log = logging.getLogger("ru_stocks.llm")


def summarize_ideas(
    *,
    api_key: str,
    base_url: str,
    model: str,
    ideas: list[SwingIdea],
    portfolio_total_rub: float,
    timeout: float = 60.0,
) -> str:
    if not api_key or not ideas:
        return ""

    bullets = []
    for i in ideas[:5]:
        bullets.append(
            f"{i.ticker} {i.direction}: close={i.last_close}, SL={i.stop}, "
            f"TP={i.target}, RSI={i.rsi14}, {i.reason}"
        )
    prompt = (
        "Ты аналитик российского фондового рынка. Кратко (до 1200 символов), "
        "по-русски: 1) общий риск-контекст для swing 1-3 дня; 2) по каждой идее — "
        "одно предложение «за» и одно «против»; 3) напомни что это не инсайд и не "
        "инвестрекомендация. Не выдумывай цифры P/E и новости — только переданные факты.\n\n"
        f"Портфель ~{portfolio_total_rub:,.0f} RUB.\n"
        "Идеи:\n" + "\n".join(bullets)
    )
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "Краткий фактический комментарий."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 800,
                "temperature": 0.3,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        log.exception("LLM brief failed")
        return ""
