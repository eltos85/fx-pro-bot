"""Сборщик и форматтер market context для FX AI Trader.

Full-cycle (15 мин):
- per symbol: 1H × 24 свечи + индикаторы, 4H × 30 свечей + индикаторы
- per symbol: top-5 news (12h window, weighted)
- macro: EIA petroleum snapshot (если API доступен; для oil)
- open positions (filtered by label="ai-fx-trader")

Review-cycle (5 мин):
- только символы с открытыми позициями
- 1H × 12 свечей + индикаторы
- без news, EIA, 4H
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fx_ai_trader.analysis.indicators import (
    IndicatorSnapshot,
    compute_snapshot,
    format_snapshot,
)
from fx_ai_trader.data.cot import CotProvider, format_cot_snapshots
from fx_ai_trader.data.econ_calendar import EconCalendarProvider
from fx_ai_trader.data.macro_rates import (
    MacroRatesProvider,
    format_macro_rates_snapshot,
)
from fx_ai_trader.data.risk_regime import (
    RiskRegimeProvider,
    format_risk_regime_snapshot,
)
from fx_ai_trader.news.eia import EiaProvider, format_eia_by_symbol
from fx_ai_trader.news.gdelt import GdeltProvider, format_gdelt_snapshots
from fx_ai_trader.news.rss import CommodityRssNewsProvider, NewsItem
from fx_ai_trader.news.weather import NoaaOutlookProvider, format_noaa_snapshot
from fx_ai_trader.state.db import AiFxPosition, AiFxTraderStore
from fx_ai_trader.trading.client_adapter import Bar, CTraderFxAdapter
from fx_ai_trader.trading.price_sensor import compute_unrealised_r

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
    # Per-symbol macro blocks (BUILDLOG 2026-05-22 — изоляция macro между
    # инструментами): {'BZ=F': 'EIA Weekly Petroleum: ...', 'NG=F': 'EIA
    # Weekly NG + STEO + NOAA discussion: ...'}. XAUUSD обычно пустой
    # ключ (нет EIA-релевантного macro для gold).
    macro_per_symbol: dict[str, str] = field(default_factory=dict)
    # Cross-symbol macro rates block (BUILDLOG 2026-05-27 D1 — DXY +
    # UST10Y + TIP; 2026-05-29 Enh.B — FRED real-yield/breakeven). Применим
    # ко всем инструментам. None если провайдер недоступен.
    macro_rates_block: str | None = None
    # Risk regime (VIX) — Enhancement C (2026-05-29). Cross-symbol.
    risk_regime_block: str | None = None
    # CFTC COT managed-money positioning — Enhancement A (2026-05-29).
    cot_block: str | None = None
    # GDELT global media tone — Enhancement D (2026-05-29). Per-symbol.
    gdelt_block: str | None = None
    # Economic calendar / event-proximity — Enhancement E (2026-05-29).
    econ_calendar_block: str | None = None


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
    store: AiFxTraderStore,
    symbols: tuple[str, ...],
    virtual_capital_usd: float,
    *,
    news_provider: CommodityRssNewsProvider | None = None,
    eia_provider: EiaProvider | None = None,
    noaa_provider: NoaaOutlookProvider | None = None,
    macro_rates_provider: MacroRatesProvider | None = None,
    risk_regime_provider: RiskRegimeProvider | None = None,
    cot_provider: CotProvider | None = None,
    gdelt_provider: GdeltProvider | None = None,
    econ_calendar_provider: EconCalendarProvider | None = None,
) -> MarketContext:
    snapshots: list[SymbolSnapshot] = []
    for sym in symbols:
        bars_1h = adapter.get_bars(sym, period_minutes=60, count=100)
        bars_4h = adapter.get_bars(sym, period_minutes=240, count=50)
        # Цена для LLM = ЖИВОЙ spot mid (Phase 1), как у executor и датчиков
        # (2026-06-02). Раньше тут стоял bars_1h[-1].close — формирующийся
        # H1 close, отстающий до ~60 мин: LLM и брокер видели разные цены.
        # get_current_price сам фолбэчит на M1-close при отсутствии spot.
        current = adapter.get_current_price(sym)
        if current is None:
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

    macro_per_symbol: dict[str, str] = {}
    if eia_provider is not None and eia_provider.enabled:
        # EIA per-symbol routing (BUILDLOG 2026-05-22): petroleum block
        # уходит в BZ=F, NG storage + STEO — в NG=F. XAUUSD не получает.
        if any(s in ("BZ=F", "CL=F", "NG=F") for s in symbols):
            try:
                snap = eia_provider.get_snapshot()
                eia_blocks = format_eia_by_symbol(snap)
                for sym, block in eia_blocks.items():
                    if sym in symbols:
                        macro_per_symbol[sym] = block
            except Exception:
                log.exception("eia_provider failed (продолжаю без EIA)")

    if noaa_provider is not None and "NG=F" in symbols:
        try:
            noaa_snap = noaa_provider.get_snapshot()
            noaa_block = format_noaa_snapshot(noaa_snap)
            if noaa_block:
                # NOAA — drivers ТОЛЬКО для NG=F (HDD/CDD demand).
                # Прикрепляем к NG=F macro (если уже есть EIA — конкатенируем).
                existing = macro_per_symbol.get("NG=F", "")
                macro_per_symbol["NG=F"] = (
                    f"{existing}\n\n{noaa_block}" if existing else noaa_block
                )
        except Exception:
            log.exception("noaa_provider failed (продолжаю без NOAA)")

    # Macro rates (BUILDLOG 2026-05-27 D1): cross-symbol — DXY / UST10Y /
    # TIP, primary driver для gold, secondary для oil. SYSTEM_PROMPT
    # уже строит на этих рядах canonical gold hierarchy
    # ("real yields → DXY"). Граф degrade: yfinance failure → None
    # → блок не появится в prompt, остальные данные остаются.
    macro_rates_block: str | None = None
    if macro_rates_provider is not None and macro_rates_provider.enabled:
        try:
            rates_snap = macro_rates_provider.get_snapshot()
            macro_rates_block = format_macro_rates_snapshot(rates_snap)
        except Exception:
            log.exception(
                "macro_rates_provider failed (продолжаю без US rates)"
            )

    # Risk regime (VIX) — Enhancement C. Cross-symbol, graceful degrade.
    risk_regime_block: str | None = None
    if risk_regime_provider is not None and risk_regime_provider.enabled:
        try:
            risk_regime_block = format_risk_regime_snapshot(
                risk_regime_provider.get_snapshot()
            )
        except Exception:
            log.exception("risk_regime_provider failed (продолжаю без VIX)")

    # CFTC COT — Enhancement A. Managed-money positioning, graceful degrade.
    cot_block: str | None = None
    if cot_provider is not None and cot_provider.enabled:
        try:
            cot_block = format_cot_snapshots(
                cot_provider.get_snapshots(symbols)
            )
        except Exception:
            log.exception("cot_provider failed (продолжаю без COT)")

    # GDELT — Enhancement D. Global media tone, graceful degrade.
    gdelt_block: str | None = None
    if gdelt_provider is not None and gdelt_provider.enabled:
        try:
            gdelt_block = format_gdelt_snapshots(
                gdelt_provider.get_snapshots(symbols)
            )
        except Exception:
            log.exception("gdelt_provider failed (продолжаю без GDELT)")

    # Economic calendar — Enhancement E. Pure-compute, event-proximity.
    econ_calendar_block: str | None = None
    if econ_calendar_provider is not None and econ_calendar_provider.enabled:
        try:
            econ_calendar_block = econ_calendar_provider.get_block(symbols)
        except Exception:
            log.exception("econ_calendar failed (продолжаю без календаря)")

    return MarketContext(
        snapshots=snapshots,
        open_positions=store.get_open_positions(),
        virtual_capital_usd=virtual_capital_usd,
        news_per_symbol=news,
        macro_per_symbol=macro_per_symbol,
        macro_rates_block=macro_rates_block,
        risk_regime_block=risk_regime_block,
        cot_block=cot_block,
        gdelt_block=gdelt_block,
        econ_calendar_block=econ_calendar_block,
    )


def collect_review_context(
    adapter: CTraderFxAdapter,
    store: AiFxTraderStore,
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
        # Живой spot mid вместо H1-close (2026-06-02) — см. collect_market_context.
        current = adapter.get_current_price(sym)
        if current is None:
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


def _ng_weather_season(month: int) -> str:
    """NG demand-season ярлык по месяцу — детерминирует знак погодного гайда.

    Источник правды: SYSTEM_PROMPT секция NATURAL GAS, SEASONAL SIGN RULE
    (May–Sep = CDD cooling, Oct–Mar = HDD heating, Apr/Sep shoulder).
    Делаем явным, чтобы LLM не инвертировал знак (баг 2026-06: «above-normal
    temps bearish» летом — зимняя логика). См. BUILDLOG_AI_FX_TRADER.md.
    """
    if month in (5, 6, 7, 8, 9):
        return (
            "CDD cooling season — above-normal/warm temps = BULLISH gas "
            "(more A/C burn), cool anomaly = bearish"
        )
    if month in (11, 12, 1, 2, 3):
        return (
            "HDD heating season — below-normal/cold temps = BULLISH gas "
            "(more heating), warm/mild anomaly = bearish"
        )
    # Apr (4), Oct (10) — shoulder/transition: знак слабый.
    return (
        "shoulder/transition month — weather demand sign is weak; treat "
        "temperature anomalies as low-conviction unless extreme"
    )


def format_context_for_prompt(ctx: MarketContext) -> str:
    parts: list[str] = []
    now = datetime.now(tz=UTC)
    parts.append(
        f"AS OF: {now.isoformat(timespec='minutes')} "
        f"(month={now.strftime('%B')})"
    )
    parts.append(f"NG WEATHER SEASON: {_ng_weather_season(now.month)}")
    parts.append(f"VIRTUAL CAPITAL: ${ctx.virtual_capital_usd:.2f}")
    parts.append(f"OPEN POSITIONS: {len(ctx.open_positions)}")
    parts.append("")

    # Cross-symbol macro rates (2026-05-27 D1): DXY / UST10Y / TIP.
    # Применимо ко всем инструментам, особенно XAUUSD. Выводим первым
    # блоком (canonical gold hierarchy "real yields → DXY → …").
    if ctx.macro_rates_block:
        parts.append(ctx.macro_rates_block)
        parts.append("")

    # Economic calendar — Enhancement E. Event-proximity (sizing-critical),
    # выводим высоко чтобы LLM учёл близость FOMC/CPI до сайзинга.
    if ctx.econ_calendar_block:
        parts.append(ctx.econ_calendar_block)
        parts.append("")

    # Risk regime (VIX) — Enhancement C. Cross-symbol risk-on/off.
    if ctx.risk_regime_block:
        parts.append(ctx.risk_regime_block)
        parts.append("")

    # CFTC COT positioning — Enhancement A. Managed-money net per symbol.
    if ctx.cot_block:
        parts.append(ctx.cot_block)
        parts.append("")

    # GDELT global media tone — Enhancement D. Structural sentiment.
    if ctx.gdelt_block:
        parts.append(ctx.gdelt_block)
        parts.append("")

    # Per-symbol macro (с 2026-05-22): EIA + NOAA маршрутизированы по
    # инструментам, чтобы LLM не смешивал oil/gas/gold macro.
    if ctx.macro_per_symbol:
        parts.append(
            "=== PER-SYMBOL MACRO CONTEXT (each block ONLY applies to "
            "the labelled symbol — do NOT cross-apply) ==="
        )
        for sym, block in ctx.macro_per_symbol.items():
            if not block:
                continue
            parts.append(f"\n[{sym}] macro:")
            parts.append(block)
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
                published_tag = "published=unknown age=unknown"
                if n.published_iso:
                    try:
                        published = datetime.fromisoformat(n.published_iso)
                        if published.tzinfo is None:
                            published = published.replace(tzinfo=UTC)
                        age_hours = max(
                            0.0, (now - published).total_seconds() / 3600.0
                        )
                        published_tag = (
                            f"published={published.astimezone(UTC).isoformat(timespec='minutes')} "
                            f"age={age_hours:.1f}h"
                        )
                    except ValueError:
                        published_tag = (
                            f"published={n.published_iso} age=unparseable"
                        )
                parts.append(
                    f"  • [{n.source}]{weight_tag} [{published_tag}] {n.title}"
                )
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
        current_by_symbol = {
            snapshot.symbol: snapshot.current_price for snapshot in ctx.snapshots
        }
        for p in ctx.open_positions:
            mode = "PAPER" if p.is_paper else "LIVE"
            sl_str = f"${p.sl_price:.6g}" if p.sl_price else "—"
            tp_str = f"${p.tp_price:.6g}" if p.tp_price else "—"
            unrealised_r = compute_unrealised_r(
                p.side,
                p.entry_price,
                p.sl_price,
                current_by_symbol.get(p.symbol),
            )
            r_str = f" unrealised_R={unrealised_r:+.2f}R" if unrealised_r is not None else ""
            parts.append(
                f"  id={p.id} [{mode}] {p.side} {p.symbol} lots={p.volume_lots} "
                f"entry=${p.entry_price:.6g} SL={sl_str} TP={tp_str} "
                f"label={p.broker_order_label}{r_str}"
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
        current_by_symbol = {
            snapshot.symbol: snapshot.current_price for snapshot in ctx.snapshots
        }
        for p in ctx.open_positions:
            mode = "PAPER" if p.is_paper else "LIVE"
            sl_str = f"${p.sl_price:.6g}" if p.sl_price else "—"
            tp_str = f"${p.tp_price:.6g}" if p.tp_price else "—"
            unrealised_r = compute_unrealised_r(
                p.side,
                p.entry_price,
                p.sl_price,
                current_by_symbol.get(p.symbol),
            )
            r_str = f" unrealised_R={unrealised_r:+.2f}R" if unrealised_r is not None else ""
            parts.append(
                f"  id={p.id} [{mode}] {p.side} {p.symbol} lots={p.volume_lots} "
                f"entry=${p.entry_price:.6g} SL={sl_str} TP={tp_str}{r_str}"
            )

    return "\n".join(parts)
