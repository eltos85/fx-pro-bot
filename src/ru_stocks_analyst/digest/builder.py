"""Сборка Telegram-сообщений: портфель + идеи скринера."""
from __future__ import annotations

from typing import Any

from ru_stocks_analyst.analysis.screener import SwingIdea
from ru_stocks_analyst.news.article import NewsArticle
from ru_stocks_analyst.tinkoff.accounts import account_type_name
from ru_stocks_analyst.tinkoff.rest_client import quotation_to_float


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_portfolio_section(
    portfolio: dict[str, Any],
    account: dict[str, Any],
) -> str:
    total = quotation_to_float(portfolio.get("totalAmountPortfolio"))
    positions = portfolio.get("positions") or []
    lines = [
        f"<b>Портфель</b> ({_escape_html(account.get('name', '?'))}, "
        f"{account_type_name(account.get('type'))})",
        f"Оценка: <b>{total:,.0f} ₽</b>",
    ]
    if not positions:
        lines.append("Позиции: пусто")
        return "\n".join(lines)

    lines.append("<b>Позиции:</b>")
    for p in positions[:15]:
        ticker = p.get("ticker") or p.get("figi") or "?"
        qty = quotation_to_float(p.get("quantity"))
        avg = quotation_to_float(
            p.get("averagePositionPrice") or p.get("averagePositionPriceFifo")
        )
        cur = quotation_to_float(p.get("currentPrice"))
        pnl = quotation_to_float(p.get("expectedYield"))
        lines.append(
            f"• {_escape_html(str(ticker))}: {qty:.0f} шт, ср. {avg:.2f} ₽, "
            f"сейчас {cur:.2f} ₽, P&amp;L {pnl:+,.0f} ₽"
        )
    if len(positions) > 15:
        lines.append(f"… ещё {len(positions) - 15} позиций")
    return "\n".join(lines)


def format_news_section(
    portfolio_tickers: list[str],
    by_ticker: dict[str, list[NewsArticle]],
    market: list[NewsArticle],
    *,
    max_per_ticker: int = 2,
) -> str:
    lines = ["<b>Новости</b> (RSS: РБК, Интерфакс, Коммерсантъ, Ведомости, ТАСС)", ""]
    if market:
        lines.append("<b>Рынок:</b>")
        for art in market[:5]:
            lines.append(f"• {_escape_html(art.title[:120])} — <i>{_escape_html(art.source)}</i>")
        lines.append("")

    if portfolio_tickers:
        lines.append("<b>Ваш портфель:</b>")
        any_news = False
        for t in portfolio_tickers:
            arts = by_ticker.get(t, [])[:max_per_ticker]
            if not arts:
                continue
            any_news = True
            lines.append(f"<b>{_escape_html(t)}</b>")
            for art in arts:
                lines.append(f"  • {_escape_html(art.title[:110])} ({_escape_html(art.source)})")
        if not any_news:
            lines.append("<i>За последние сутки в ленте нет явных заголовков по вашим тикерам.</i>")
    else:
        lines.append("<i>Портфель пуст — только общий рынок.</i>")

    return "\n".join(lines)


def format_ideas_section(
    ideas: list[SwingIdea],
    *,
    max_ideas: int,
    risk_pct: float,
    deposit_hint_rub: float | None = None,
) -> str:
    if not ideas:
        return (
            "<b>Идеи скринера</b>\n"
            "Сейчас нет сигналов с заданными фильтрами (1–3 дня, MOEX)."
        )

    lines = [
        f"<b>Идеи скринера</b> (горизонт 1–3 дня, топ {min(max_ideas, len(ideas))})",
        "<i>Не рекомендация; решение за вами. Сделки — на брокерском счёте.</i>",
        "",
    ]
    for idea in ideas[:max_ideas]:
        arrow = "🟢 LONG" if idea.direction == "long" else "🔴 SHORT"
        risk_rub = ""
        if deposit_hint_rub and deposit_hint_rub > 0:
            risk_rub = f" | риск ~{deposit_hint_rub * risk_pct / 100:,.0f} ₽ ({risk_pct:.0f}%)"
        lines.append(
            f"{arrow} <b>{_escape_html(idea.ticker)}</b> — {_escape_html(idea.name[:40])}\n"
            f"Вход ~{idea.entry_hint:.2f} ₽ | SL {idea.stop:.2f} | TP {idea.target:.2f}\n"
            f"RSI {idea.rsi14} | ATR% {idea.atr_pct} | {idea.reason}{risk_rub}"
        )
        lines.append("")
    return "\n".join(lines).strip()


def build_morning_digest(
    *,
    portfolio: dict[str, Any],
    account: dict[str, Any],
    ideas: list[SwingIdea],
    max_ideas: int,
    risk_pct: float,
    portfolio_tickers: list[str] | None = None,
    news_by_ticker: dict[str, list[NewsArticle]] | None = None,
    news_market: list[NewsArticle] | None = None,
    llm_note: str = "",
    include_news: bool = True,
) -> str:
    tickers = portfolio_tickers or []
    parts = [
        "📊 <b>RU Stocks — дайджест</b>",
        "",
        format_portfolio_section(portfolio, account),
    ]
    if include_news and news_by_ticker is not None and news_market is not None:
        parts.extend([
            "",
            format_news_section(tickers, news_by_ticker, news_market),
        ])
    if llm_note.strip():
        parts.extend(["", "<b>ИИ-аналитика</b> (только по заголовкам выше)", _escape_html(llm_note[:2500])])
    parts.extend([
        "",
        format_ideas_section(
            ideas,
            max_ideas=max_ideas,
            risk_pct=risk_pct,
            deposit_hint_rub=quotation_to_float(portfolio.get("totalAmountPortfolio")),
        ),
        "",
        "<i>Tinkoff API + RSS. Не инвестрекомендация.</i>",
    ])
    return "\n".join(parts)
