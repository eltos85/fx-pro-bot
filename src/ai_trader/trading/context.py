"""Сборщик market context для LLM.

Wave 2: добавлены технические индикаторы на двух TF (1h и 4h):
- 1h × 100 свечей: RSI(14), MACD(12/26/9), ATR(14), EMA20/50, BB(20,2)
- 4h × 50 свечей: те же индикаторы, для оценки крупного тренда

Wave 3: добавляется блок NEWS (последние 1-3 ч заголовков, фильтр по
символам). Если news-feed недоступен — блок пропускается.

На каждом цикле:
- Текущая цена + 24h изменение + funding rate по каждому символу
- 1h × 12 closes (для краткой картины внутри дня)
- Полный набор индикаторов на 1h и 4h
- 24h high/low
- Открытые позиции AI (из БД)
- Реальный equity (для контекста, qty считается от virtual_capital)
- Новости за последний час (если включены)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ai_trader.analysis.indicators import IndicatorSnapshot, compute_snapshot, format_snapshot
from ai_trader.news.rss import NewsItem
from ai_trader.state.db import AiPosition, AiTraderStore
from ai_trader.trading.client import AiBybitClient, Bar, Ticker

log = logging.getLogger(__name__)


@dataclass
class SymbolSnapshot:
    symbol: str
    ticker: Ticker | None
    bars_1h: list[Bar]
    bars_4h: list[Bar]
    ind_1h: IndicatorSnapshot | None = None
    ind_4h: IndicatorSnapshot | None = None


@dataclass
class MarketContext:
    snapshots: list[SymbolSnapshot]
    open_positions: list[AiPosition]
    virtual_capital_usd: float
    real_equity_usd: float
    news: list[NewsItem] = field(default_factory=list)


def collect_market_context(
    client: AiBybitClient,
    store: AiTraderStore,
    symbols: tuple[str, ...],
    virtual_capital_usd: float,
    news_provider=None,
) -> MarketContext:
    snapshots: list[SymbolSnapshot] = []
    for sym in symbols:
        ticker = client.get_ticker(sym)
        bars_1h = client.get_klines(sym, interval="60", limit=100)
        bars_4h = client.get_klines(sym, interval="240", limit=50)
        ind_1h = ind_4h = None
        if len(bars_1h) >= 50:
            ind_1h = compute_snapshot(
                [b.high for b in bars_1h],
                [b.low for b in bars_1h],
                [b.close for b in bars_1h],
            )
        if len(bars_4h) >= 30:
            ind_4h = compute_snapshot(
                [b.high for b in bars_4h],
                [b.low for b in bars_4h],
                [b.close for b in bars_4h],
            )
        snapshots.append(
            SymbolSnapshot(
                symbol=sym,
                ticker=ticker,
                bars_1h=bars_1h,
                bars_4h=bars_4h,
                ind_1h=ind_1h,
                ind_4h=ind_4h,
            )
        )

    open_positions = store.get_open_positions()
    real_equity = client.get_wallet_balance()

    news: list[NewsItem] = []
    if news_provider is not None:
        try:
            news = news_provider.get_recent_news(symbols)
        except Exception:
            log.exception("news_provider failed (продолжаю без новостей)")
            news = []

    return MarketContext(
        snapshots=snapshots,
        open_positions=open_positions,
        virtual_capital_usd=virtual_capital_usd,
        real_equity_usd=real_equity,
        news=news,
    )


def _funding_band_label(rate: float) -> str:
    """2026 framework (Lambda Finance): bands для funding rate.

    Возвращает короткую читаемую метку ([NEUTRAL]/[mild long]/[STRONG short] и т.п.)
    для встраивания рядом с числовым значением. Помогает LLM не пропускать
    зону «strong one-sided positioning».
    """
    abs_rate = abs(rate)
    if abs_rate < 0.0005:  # 0.05%
        return " [NEUTRAL]"
    if abs_rate < 0.0020:  # 0.20%
        side = "longs paying" if rate > 0 else "shorts paying"
        return f" [mild lean: {side}]"
    side = "longs paying" if rate > 0 else "shorts paying"
    return f" [STRONG: {side}, contrarian risk]"


def _btc_dominance_estimate(snapshots: list[SymbolSnapshot]) -> str | None:
    """Грубая оценка BTC-силы относительно остальных allowed pairs за 24h.

    Если у нас есть BTCUSDT + ≥ 1 alt — возвращаем строку:
    «BTC vs alts (24h): BTC=+1.2% avg-alt=-0.8% → BTC outperforming».
    Истинный BTC dominance % требует глобального market cap, чего в context-е нет;
    эта эвристика fits в наш набор и достаточна как macro-trigger.
    """
    btc_change: float | None = None
    alt_changes: list[float] = []
    for s in snapshots:
        if s.ticker is None:
            continue
        if s.symbol == "BTCUSDT":
            btc_change = s.ticker.price_change_pct_24h
        else:
            alt_changes.append(s.ticker.price_change_pct_24h)
    if btc_change is None or not alt_changes:
        return None
    avg_alt = sum(alt_changes) / len(alt_changes)
    delta = btc_change - avg_alt
    if abs(delta) < 0.5:
        verdict = "BTC and alts moving together"
    elif delta > 0:
        verdict = "BTC outperforming alts (alt-weakness)"
    else:
        verdict = "alts outperforming BTC (alt-season hint)"
    return (
        f"BTC vs alts (24h): BTC={btc_change:+.2f}% "
        f"avg-alt={avg_alt:+.2f}% → {verdict}"
    )


def format_context_for_prompt(ctx: MarketContext) -> str:
    """Превращает MarketContext в текст для LLM."""
    parts: list[str] = []
    parts.append(f"VIRTUAL CAPITAL: ${ctx.virtual_capital_usd:.2f}")
    parts.append(f"OPEN POSITIONS: {len(ctx.open_positions)}")
    dom_line = _btc_dominance_estimate(ctx.snapshots)
    if dom_line is not None:
        parts.append(f"MACRO: {dom_line}")
    parts.append("")

    if ctx.news:
        parts.append("=== RECENT CRYPTO NEWS ===")
        for n in ctx.news:
            tags = f" [{','.join(n.symbols)}]" if n.symbols else ""
            parts.append(f"  • [{n.source}] {n.title}{tags}")
            if n.summary and n.summary != n.title:
                summary = n.summary[:200].replace("\n", " ")
                parts.append(f"    {summary}")
        parts.append("")

    parts.append("=== MARKET DATA ===")
    for s in ctx.snapshots:
        if s.ticker is None:
            parts.append(f"\n[{s.symbol}] TICKER UNAVAILABLE")
            continue
        t = s.ticker
        funding_label = _funding_band_label(t.funding_rate)
        parts.append(
            f"\n[{s.symbol}] price=${t.last_price:.6g} "
            f"24h={t.price_change_pct_24h:+.2f}% "
            f"funding={t.funding_rate * 100:+.4f}%{funding_label} "
            f"vol24h={t.volume_24h:.0f}"
        )
        if s.bars_1h:
            recent = s.bars_1h[-12:]
            closes = [f"{b.close:.6g}" for b in recent]
            parts.append("  1h closes (last 12h, oldest→newest):")
            parts.append("  " + " ".join(closes))
            high24 = max(b.high for b in s.bars_1h[-24:])
            low24 = min(b.low for b in s.bars_1h[-24:])
            parts.append(f"  24h range: low=${low24:.6g} high=${high24:.6g}")
        if s.ind_1h is not None:
            parts.append("  1H INDICATORS:")
            parts.append(format_snapshot(s.ind_1h))
        if s.ind_4h is not None:
            parts.append("  4H INDICATORS (bigger trend):")
            parts.append(format_snapshot(s.ind_4h))

    parts.append("")
    parts.append("=== OPEN POSITIONS ===")
    if not ctx.open_positions:
        parts.append("(none)")
    else:
        for p in ctx.open_positions:
            parts.append(
                f"  id={p.id} {p.side} {p.symbol} qty={p.qty} entry=${p.entry_price:.6g} "
                f"sl=${p.sl_price or 0:.6g} tp=${p.tp_price or 0:.6g} "
                f"lev={p.leverage}x linkid={p.order_link_id}"
            )

    return "\n".join(parts)
