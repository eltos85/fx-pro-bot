"""ИИ-аналитика по переданным заголовкам RSS (без выдуманных фактов)."""
from __future__ import annotations

import logging

import requests

from ru_stocks_analyst.analysis.screener import SwingIdea
from ru_stocks_analyst.news.article import NewsArticle

log = logging.getLogger("ru_stocks.llm")


def _headlines_block(
    portfolio_tickers: list[str],
    by_ticker: dict[str, list[NewsArticle]],
    market: list[NewsArticle],
    max_lines: int = 35,
) -> str:
    lines: list[str] = []
    n = 0
    for art in market[:6]:
        lines.append(f"[Рынок] {art.source}: {art.title}")
        n += 1
        if n >= max_lines:
            return "\n".join(lines)
    for t in portfolio_tickers:
        for art in by_ticker.get(t, [])[:3]:
            lines.append(f"[{t}] {art.source}: {art.title}")
            n += 1
            if n >= max_lines:
                return "\n".join(lines)
    return "\n".join(lines) if lines else "(свежих заголовков по фильтру нет)"


def build_market_analysis(
    *,
    api_key: str,
    base_url: str,
    model: str,
    portfolio_tickers: list[str],
    portfolio_total_rub: float,
    by_ticker: dict[str, list[NewsArticle]],
    market: list[NewsArticle],
    tech_ideas: list[SwingIdea],
    timeout: float = 90.0,
) -> str:
    if not api_key:
        return ""

    headlines = _headlines_block(portfolio_tickers, by_ticker, market)
    tech_lines = [
        f"{i.ticker} {i.direction} RSI={i.rsi14} — {i.reason}"
        for i in tech_ideas[:5]
    ]
    tech_block = "\n".join(tech_lines) if tech_lines else "(техскринер: сигналов нет)"

    prompt = f"""Ты аналитик фондового рынка России. Горизонт: 1–3 торговых дня.

ПРАВИЛА:
- Используй ТОЛЬКО заголовки ниже. Не выдумывай новости, цифры, P/E, инсайды.
- Если по тикеру нет заголовка — напиши «в ленте за сутки не выделено».
- Это не инвестрекомендация.
- Ответ до 1500 символов, структура:
  1) Контекст рынка РФ (2–4 предложения)
  2) По каждой бумаге из портфеля: {", ".join(portfolio_tickers) or "—"}
  3) Согласование с техскринером (согласен / нейтрально / противоречит)
  4) Главные риски на 1–3 дня

Портфель ~{portfolio_total_rub:,.0f} ₽.

ЗАГОЛОВКИ RSS:
{headlines}

ТЕХСКРИНЕР:
{tech_block}
"""

    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Фактологичный краткий аналитик MOEX. Только переданные заголовки.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 1200,
                "temperature": 0.25,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        log.exception("market analysis LLM failed")
        return ""
