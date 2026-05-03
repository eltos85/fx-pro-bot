"""Сборщик market context для LLM.

На каждом цикле собираем:
- Текущая цена + 24h изменение + funding rate по каждому символу
- 24 последних 1h свечей (24 часа истории) — для краткого ТА в LLM
- Открытые позиции AI (из нашей БД, привязка по orderLinkId)
- Виртуальный баланс / реальный equity

Контекст форматируется в компактный текст ~3-5K tokens, не разбухает.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ai_trader.state.db import AiPosition, AiTraderStore
from ai_trader.trading.client import AiBybitClient, Bar, Ticker

log = logging.getLogger(__name__)


@dataclass
class SymbolSnapshot:
    symbol: str
    ticker: Ticker | None
    bars_1h: list[Bar]


@dataclass
class MarketContext:
    snapshots: list[SymbolSnapshot]
    open_positions: list[AiPosition]
    virtual_capital_usd: float
    real_equity_usd: float


def collect_market_context(
    client: AiBybitClient,
    store: AiTraderStore,
    symbols: tuple[str, ...],
    virtual_capital_usd: float,
) -> MarketContext:
    snapshots: list[SymbolSnapshot] = []
    for sym in symbols:
        ticker = client.get_ticker(sym)
        bars = client.get_klines(sym, interval="60", limit=24)
        snapshots.append(SymbolSnapshot(symbol=sym, ticker=ticker, bars_1h=bars))

    open_positions = store.get_open_positions()
    real_equity = client.get_wallet_balance()

    return MarketContext(
        snapshots=snapshots,
        open_positions=open_positions,
        virtual_capital_usd=virtual_capital_usd,
        real_equity_usd=real_equity,
    )


def format_context_for_prompt(ctx: MarketContext) -> str:
    """Превращает MarketContext в текст для LLM (~3K tokens на 5 символов)."""
    parts: list[str] = []
    parts.append(f"VIRTUAL CAPITAL: ${ctx.virtual_capital_usd:.2f}")
    parts.append(f"OPEN POSITIONS: {len(ctx.open_positions)}")
    parts.append("")

    parts.append("=== MARKET DATA ===")
    for s in ctx.snapshots:
        if s.ticker is None:
            parts.append(f"\n[{s.symbol}] TICKER UNAVAILABLE")
            continue
        t = s.ticker
        parts.append(
            f"\n[{s.symbol}] price=${t.last_price:.6g} "
            f"24h={t.price_change_pct_24h:+.2f}% "
            f"funding={t.funding_rate * 100:+.4f}% "
            f"vol24h={t.volume_24h:.0f}"
        )
        if s.bars_1h:
            recent = s.bars_1h[-12:]  # последние 12 часов
            parts.append("  1h closes (last 12h, oldest→newest):")
            closes = [f"{b.close:.6g}" for b in recent]
            parts.append("  " + " ".join(closes))
            high24 = max(b.high for b in s.bars_1h)
            low24 = min(b.low for b in s.bars_1h)
            parts.append(f"  24h range: low=${low24:.6g} high=${high24:.6g}")

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
