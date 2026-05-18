"""Сборщик и форматтер market context для FX AI Trader.

Full-cycle (15 мин):
- per symbol: 1H × 24 свечи + индикаторы, 4H × 30 свечей + индикаторы
- per symbol: top-5 news (12h window, weighted)
- macro: EIA petroleum snapshot (если API доступен; для oil)
- open positions (filtered by label="ai-fx-trend")

Review-cycle (5 мин):
- только символы с открытыми позициями
- 1H × 12 свечей + индикаторы
- без news, EIA, 4H
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fx_ai_trend.analysis.indicators import (
    IndicatorSnapshot,
    compute_snapshot,
    format_snapshot,
)
from fx_ai_trend.news.eia import EiaProvider, format_eia_snapshot
from fx_ai_trend.news.rss import CommodityRssNewsProvider, NewsItem
from fx_ai_trend.state.db import AiFxPosition, AiFxTrendStore
from fx_ai_trend.trading.client_adapter import Bar, CTraderFxAdapter

log = logging.getLogger(__name__)


@dataclass
class SymbolSnapshot:
    symbol: str
    current_price: float | None
    bars_1h: list[Bar]
    bars_4h: list[Bar]
    ind_1h: IndicatorSnapshot | None = None
    ind_4h: IndicatorSnapshot | None = None
    price_change_pct_24h: float | None = None


@dataclass
class MarketContext:
    snapshots: list[SymbolSnapshot]
    open_positions: list[AiFxPosition]
    virtual_capital_usd: float
    news_per_symbol: dict[str, list[NewsItem]] = field(default_factory=dict)
    eia_block_text: str | None = None


def _price_change_pct_24h(bars_1h: list[Bar]) -> float | None:
    """Percent change last close vs close 24 bars (24 H1) ago."""
    if len(bars_1h) < 25:
        return None
    last = bars_1h[-1].close
    prev = bars_1h[-25].close
    if prev <= 0:
        return None
    return (last - prev) / prev * 100.0


def collect_market_context(
    adapter: CTraderFxAdapter,
    store: AiFxTrendStore,
    symbols: tuple[str, ...],
    virtual_capital_usd: float,
    *,
    news_provider: CommodityRssNewsProvider | None = None,
    eia_provider: EiaProvider | None = None,
) -> MarketContext:
    snapshots: list[SymbolSnapshot] = []
    for sym in symbols:
        bars_1h = adapter.get_bars(sym, period_minutes=60, count=100)
        bars_4h = adapter.get_bars(sym, period_minutes=240, count=50)
        current = bars_1h[-1].close if bars_1h else None
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
                current_price=current,
                bars_1h=bars_1h,
                bars_4h=bars_4h,
                ind_1h=ind_1h,
                ind_4h=ind_4h,
                price_change_pct_24h=_price_change_pct_24h(bars_1h),
            )
        )

    news: dict[str, list[NewsItem]] = {}
    if news_provider is not None:
        try:
            news = news_provider.get_recent_news(symbols)
        except Exception:
            log.exception("news_provider failed (продолжаю без новостей)")
            news = {}

    eia_block: str | None = None
    if eia_provider is not None and eia_provider.enabled:
        # EIA только для oil — пропускаем если в symbols нет BZ=F/USOIL.
        if any(s in ("BZ=F", "CL=F") for s in symbols):
            try:
                snap = eia_provider.get_snapshot()
                eia_block = format_eia_snapshot(snap)
            except Exception:
                log.exception("eia_provider failed (продолжаю без EIA)")

    return MarketContext(
        snapshots=snapshots,
        open_positions=store.get_open_positions(),
        virtual_capital_usd=virtual_capital_usd,
        news_per_symbol=news,
        eia_block_text=eia_block,
    )


def collect_review_context(
    adapter: CTraderFxAdapter,
    store: AiFxTrendStore,
    virtual_capital_usd: float,
) -> MarketContext:
    open_positions = store.get_open_positions()
    if not open_positions:
        return MarketContext(
            snapshots=[], open_positions=[],
            virtual_capital_usd=virtual_capital_usd,
        )

    review_symbols = sorted({p.symbol for p in open_positions})
    snapshots: list[SymbolSnapshot] = []
    for sym in review_symbols:
        bars_1h = adapter.get_bars(sym, period_minutes=60, count=50)
        current = bars_1h[-1].close if bars_1h else None
        ind_1h = None
        if len(bars_1h) >= 30:
            ind_1h = compute_snapshot(
                [b.high for b in bars_1h],
                [b.low for b in bars_1h],
                [b.close for b in bars_1h],
            )
        snapshots.append(
            SymbolSnapshot(
                symbol=sym,
                current_price=current,
                bars_1h=bars_1h,
                bars_4h=[],
                ind_1h=ind_1h,
                price_change_pct_24h=_price_change_pct_24h(bars_1h),
            )
        )
    return MarketContext(
        snapshots=snapshots,
        open_positions=open_positions,
        virtual_capital_usd=virtual_capital_usd,
    )


# ─── Format for LLM ──────────────────────────────────────────────────────


def format_context_for_prompt(ctx: MarketContext) -> str:
    parts: list[str] = []
    parts.append(f"VIRTUAL CAPITAL: ${ctx.virtual_capital_usd:.2f}")
    parts.append(f"OPEN POSITIONS: {len(ctx.open_positions)}")
    parts.append("")

    if ctx.eia_block_text:
        parts.append("=== EIA MACRO (oil) ===")
        parts.append(ctx.eia_block_text)
        parts.append("")

    if any(items for items in ctx.news_per_symbol.values()):
        parts.append("=== RECENT NEWS (top-N per symbol, 12h window) ===")
        for sym, items in ctx.news_per_symbol.items():
            if not items:
                continue
            parts.append(f"\n[{sym}] news ({len(items)} items):")
            for n in items:
                weight_tag = (
                    f" (weight {n.source_weight:.1f})"
                    if n.source_weight < 1.0
                    else ""
                )
                parts.append(f"  • [{n.source}]{weight_tag} {n.title}")
                if n.summary and n.summary != n.title:
                    summary = n.summary[:240].replace("\n", " ")
                    parts.append(f"    {summary}")
        parts.append("")

    parts.append("=== MARKET DATA ===")
    for s in ctx.snapshots:
        if s.current_price is None:
            parts.append(f"\n[{s.symbol}] DATA UNAVAILABLE")
            continue
        change_str = (
            f"24h={s.price_change_pct_24h:+.2f}%"
            if s.price_change_pct_24h is not None
            else "24h=n/a"
        )
        parts.append(
            f"\n[{s.symbol}] price=${s.current_price:.6g} {change_str}"
        )
        if s.bars_1h:
            recent = s.bars_1h[-12:]
            closes = [f"{b.close:.6g}" for b in recent]
            parts.append("  1H closes (last 12h, oldest→newest):")
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
            mode = "PAPER" if p.is_paper else "LIVE"
            sl_str = f"${p.sl_price:.6g}" if p.sl_price else "—"
            tp_str = f"${p.tp_price:.6g}" if p.tp_price else "—"
            parts.append(
                f"  id={p.id} [{mode}] {p.side} {p.symbol} lots={p.volume_lots} "
                f"entry=${p.entry_price:.6g} SL={sl_str} TP={tp_str} "
                f"label={p.broker_order_label}"
            )

    return "\n".join(parts)


def format_context_for_review(ctx: MarketContext) -> str:
    """Lite-форматтер для review-cycle: позиции + текущее состояние."""
    parts: list[str] = []
    parts.append(f"VIRTUAL CAPITAL: ${ctx.virtual_capital_usd:.2f}")
    parts.append(f"OPEN POSITIONS: {len(ctx.open_positions)}")
    parts.append("")
    parts.append("=== MARKET DATA (positions only, lite review cycle) ===")
    for s in ctx.snapshots:
        if s.current_price is None:
            parts.append(f"\n[{s.symbol}] DATA UNAVAILABLE")
            continue
        change_str = (
            f"24h={s.price_change_pct_24h:+.2f}%"
            if s.price_change_pct_24h is not None
            else "24h=n/a"
        )
        parts.append(
            f"\n[{s.symbol}] price=${s.current_price:.6g} {change_str}"
        )
        if s.bars_1h:
            recent = s.bars_1h[-6:]
            closes = [f"{b.close:.6g}" for b in recent]
            parts.append("  1H closes (last 6h, oldest→newest):")
            parts.append("  " + " ".join(closes))
        if s.ind_1h is not None:
            parts.append("  1H INDICATORS:")
            parts.append(format_snapshot(s.ind_1h))

    parts.append("")
    parts.append("=== OPEN POSITIONS ===")
    if not ctx.open_positions:
        parts.append("(none)")
    else:
        for p in ctx.open_positions:
            mode = "PAPER" if p.is_paper else "LIVE"
            sl_str = f"${p.sl_price:.6g}" if p.sl_price else "—"
            tp_str = f"${p.tp_price:.6g}" if p.tp_price else "—"
            parts.append(
                f"  id={p.id} [{mode}] {p.side} {p.symbol} lots={p.volume_lots} "
                f"entry=${p.entry_price:.6g} SL={sl_str} TP={tp_str}"
            )

    return "\n".join(parts)
