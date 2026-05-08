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
from ai_trader.analysis.positioning import (
    PositioningSnapshot,
    build_positioning_snapshot,
    format_positioning,
)
from ai_trader.macro.external import MacroProvider, MacroSnapshot, format_macro
from ai_trader.macro.options import (
    OptionsIvProvider,
    OptionsIvSnapshot,
    format_options_iv,
)
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
    # i2/7 (2026-05-07): positioning-фичи (OI delta + funding history).
    # None если эндпоинты недоступны или мало истории — формат не падает,
    # просто пропускает блок POSITIONING для символа.
    positioning: PositioningSnapshot | None = None


@dataclass
class MarketContext:
    snapshots: list[SymbolSnapshot]
    open_positions: list[AiPosition]
    virtual_capital_usd: float
    real_equity_usd: float
    news: list[NewsItem] = field(default_factory=list)
    # i3/7 (2026-05-07): глобальный macro/sentiment snapshot. None если
    # macro_provider не передан или сетевой fetch упал — формат
    # деградирует gracefully.
    macro: MacroSnapshot | None = None
    # i6/7 (2026-05-07): Deribit DVOL/IV для BTC и ETH (options market).
    options_iv: OptionsIvSnapshot | None = None


def collect_market_context(
    client: AiBybitClient,
    store: AiTraderStore,
    symbols: tuple[str, ...],
    virtual_capital_usd: float,
    news_provider=None,
    macro_provider: MacroProvider | None = None,
    options_iv_provider: OptionsIvProvider | None = None,
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
                volumes=[b.volume for b in bars_1h],
                # 1H VWAP: rolling по последним 24 барам = «daily VWAP-aware»
                # (institutional intraday). RV: 24 returns ≈ 1 сутки,
                # аннуализация 24×365 = 8760.
                vwap_window=24,
                rv_window=24,
                bars_per_year=24 * 365,
            )
        if len(bars_4h) >= 30:
            ind_4h = compute_snapshot(
                [b.high for b in bars_4h],
                [b.low for b in bars_4h],
                [b.close for b in bars_4h],
                volumes=[b.volume for b in bars_4h],
                # 4H VWAP: rolling 30 баров ≈ 5 суток (weekly fair-value
                # benchmark). RV: 30 returns ≈ 5 суток, аннуализация
                # 6×365=2190 (6 четырёхчасовых баров в сутки).
                vwap_window=30,
                rv_window=30,
                bars_per_year=6 * 365,
            )

        # i2/7 (2026-05-07): positioning-фичи (institutional 2026 primary).
        # 25 OI точек × 1h = текущая + 24 hours back; funding 10 событий = ~3.3 дня.
        # При None / коротких массивах build_positioning_snapshot выдаёт snapshot
        # с None-полями — формат потом просто пропустит этот блок.
        oi_hist = client.get_open_interest_history(sym, interval="1h", limit=25)
        funding_hist = client.get_funding_rate_history(sym, limit=21)
        # i4/7 (2026-05-07): retail Long/Short ratio + L2 orderbook imbalance.
        # LSR limit=2 = текущая + предыдущая 1h-точка (для Δ).
        # Orderbook depth=50 = depth-50 microstructure standard.
        ls_hist = client.get_long_short_ratio(sym, period="1h", limit=2)
        orderbook = client.get_orderbook(sym, limit=50)
        # i5/7: closes_1h нужны для liquidation-cascade proxy (выравниваются
        # с oi_hist по индексу). Используем уже собранные bars_1h.
        closes_1h_for_liq = [b.close for b in bars_1h] if bars_1h else None
        positioning = build_positioning_snapshot(
            oi_history=oi_hist,
            funding_history=funding_hist,
            funding_now=ticker.funding_rate if ticker is not None else None,
            ls_history=ls_hist,
            orderbook=orderbook,
            closes_1h=closes_1h_for_liq,
        )

        snapshots.append(
            SymbolSnapshot(
                symbol=sym,
                ticker=ticker,
                bars_1h=bars_1h,
                bars_4h=bars_4h,
                ind_1h=ind_1h,
                ind_4h=ind_4h,
                positioning=positioning,
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

    macro: MacroSnapshot | None = None
    if macro_provider is not None:
        try:
            macro = macro_provider.get_snapshot()
        except Exception:
            log.exception("macro_provider failed (продолжаю без macro)")
            macro = None

    options_iv: OptionsIvSnapshot | None = None
    if options_iv_provider is not None:
        try:
            options_iv = options_iv_provider.get_snapshot()
        except Exception:
            log.exception("options_iv_provider failed (продолжаю без IV)")
            options_iv = None

    return MarketContext(
        snapshots=snapshots,
        open_positions=open_positions,
        virtual_capital_usd=virtual_capital_usd,
        real_equity_usd=real_equity,
        news=news,
        macro=macro,
        options_iv=options_iv,
    )


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

    # i3/7 — GLOBAL MACRO / SENTIMENT (CoinGecko + alternative.me).
    # Точные глобальные значения BTC dominance + Fear&Greed. Эта секция
    # появляется в начале контекста, до per-symbol blocks.
    if ctx.macro is not None:
        macro_text = format_macro(ctx.macro)
        if macro_text:
            parts.append("=== GLOBAL MACRO / SENTIMENT ===")
            parts.append(macro_text)

    # i6/7 — OPTIONS MARKET IV (Deribit DVOL для BTC и ETH).
    if ctx.options_iv is not None:
        iv_text = format_options_iv(ctx.options_iv)
        parts.append("=== OPTIONS MARKET IV (Deribit DVOL, annualised) ===")
        parts.append(iv_text)
        parts.append(
            "  Note: compare DVOL to per-symbol RV — IV>>RV signals options-"
            "market priced for bigger move; IV<<RV signals complacency."
        )

    # Эвристика BTC vs alts (на основе наших же тикеров) — мягкое
    # дополнение к точным данным CoinGecko: показывает ротацию между
    # BTC и теми пары, что мы реально торгуем.
    dom_line = _btc_dominance_estimate(ctx.snapshots)
    if dom_line is not None:
        parts.append(f"BTC vs traded alts: {dom_line}")
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
        # Метка funding убрана из тикера (раньше была через
        # `_funding_band_label`) — она дублировала и противоречила метке
        # в POSITIONING.Funding. Теперь funding interpretation в одном
        # месте — POSITIONING block (см. _funding_label в positioning.py).
        parts.append(
            f"\n[{s.symbol}] price=${t.last_price:.6g} "
            f"24h={t.price_change_pct_24h:+.2f}% "
            f"funding={t.funding_rate * 100:+.4f}% "
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
        # i2/7 — POSITIONING (institutional 2026 primary): OI delta + funding
        # history. Печатаем перед классическими индикаторами, чтобы LLM видел
        # их первыми и приоритезировал в reasoning.
        if s.positioning is not None and (
            s.positioning.oi_now is not None
            or s.positioning.funding_24h_cumulative is not None
        ):
            parts.append("  POSITIONING (institutional 2026):")
            parts.append(format_positioning(s.positioning))
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
