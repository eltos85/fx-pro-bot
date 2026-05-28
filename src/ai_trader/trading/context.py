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
import time
from dataclasses import dataclass, field
from datetime import datetime

from ai_trader.analysis.indicators import IndicatorSnapshot, compute_snapshot, format_snapshot
from ai_trader.news.rss import NewsItem
from ai_trader.state.db import AiPosition, AiTraderStore
from ai_trader.trading.client import AiBybitClient, Bar, Position, Ticker

log = logging.getLogger(__name__)


def _drop_incomplete_bar(bars: list[Bar], interval_minutes: int) -> list[Bar]:
    """Отбрасывает последний бар если он ещё не закрыт.

    Bybit `get_klines` возвращает массив свечей **включая текущую формирующуюся**.
    Каноничные индикаторы (RSI/MACD/BB по Wilder/Appel/Bollinger) определены на
    closed candles; использование partial-бара даёт look-ahead bias и flickering
    сигналы при tick-by-tick движении цены внутри текущего интервала.

    Бар считается закрытым если `ts (start) + interval_ms` строго в прошлом.
    Если массив пуст или последний бар уже закрыт — возвращаем без изменений.

    Источники (2026 best practice):
    - Freqtrade Look-Ahead Analysis docs.
    - StratBase «Look-Ahead Bias: The Hidden Backtest Killer» 2026 —
      «indicators like RSI/MACD should be calculated using only completed
      (closed) candles ... Including the current bar creates look-ahead bias».
    """
    if not bars:
        return bars
    now_ms = int(time.time() * 1000)
    interval_ms = max(1, interval_minutes) * 60 * 1000
    if bars[-1].ts + interval_ms > now_ms:
        return bars[:-1]
    return bars


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
    # v0.17 (2026-05-25, Шаг 2a): live данные с биржи по открытым
    # позициям — mapping ``symbol -> Position`` (Position dataclass
    # из ``trading.client``). Используется в format_context_for_prompt
    # / format_context_for_review чтобы LLM видел реальный
    # ``unrealised_pnl`` (USD), ``mark_price``, ``liq_price`` — а не
    # только наш расчётный ``current_pnl_r`` из 1H бар.
    # ``None`` означает что API-вызов не получился (transient outage)
    # — отличается от ``{}`` (запрос ОК, открытых позиций на бирже нет).
    live_positions: dict[str, Position] | None = None
    # v0.20 (2026-05-28): Bybit taker fee per side в долях (default 0 —
    # backward-compat для тестов, не пробрасывающих fee). В main.py
    # передаётся из settings.taker_fee_pct (0.00055 для VIP-0 demo).
    # Format-функции используют для расчёта:
    # - peak_pnl_r_net / current_pnl_r_net рядом с gross R-units
    # - LIVE: net unrealised после closeFee (то что реально получим
    #   при закрытии прямо сейчас)
    # Если 0.0 — net-строки не показываются (старое поведение).
    taker_fee_pct: float = 0.0


def collect_market_context(
    client: AiBybitClient,
    store: AiTraderStore,
    symbols: tuple[str, ...],
    virtual_capital_usd: float,
    news_provider=None,
    *,
    taker_fee_pct: float = 0.0,
) -> MarketContext:
    snapshots: list[SymbolSnapshot] = []
    for sym in symbols:
        ticker = client.get_ticker(sym)
        bars_1h = _drop_incomplete_bar(
            client.get_klines(sym, interval="60", limit=100), 60
        )
        bars_4h = _drop_incomplete_bar(
            client.get_klines(sym, interval="240", limit=50), 240
        )
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

    # v0.17 Шаг 2a: live данные с биржи по открытым позициям. Если
    # запрос упал (None) — пишем None в context, форматтер покажет
    # «live: API unavailable». Если биржа вернула пустой список ([]),
    # значит наших позиций сейчас на ней нет (reconcile pending).
    live_positions: dict[str, Position] | None = None
    if open_positions:
        exchange_positions = client.get_positions()
        if exchange_positions is None:
            live_positions = None
        else:
            live_positions = {p.symbol: p for p in exchange_positions}

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
        live_positions=live_positions,
        taker_fee_pct=taker_fee_pct,
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


def _compute_position_r_stats(
    position: AiPosition,
    bars_1h: list[Bar],
    current_price: float | None,
) -> tuple[float | None, float | None]:
    """Считает (peak_pnl_r, current_pnl_r) — gross-only.

    Wrapper для backward-compat. См. ``_compute_position_pnl_stats`` —
    он возвращает полный набор полей включая ``_net`` варианты с учётом
    round-trip fees (v0.20).
    """
    stats = _compute_position_pnl_stats(
        position, bars_1h, current_price, taker_fee_pct=0.0
    )
    return (stats.peak_r, stats.current_r)


@dataclass
class PositionPnlStats:
    """v0.20 (2026-05-28): расширенная PnL-статистика per-position.

    Поля _gross — чисто price-based (как было до v0.20). Поля _net —
    после оценки round-trip taker fees:
      fee_RT_est = (entry_value + close_value) * taker_fee_pct
                ≈ entry * qty * taker_fee_pct * 2  (для оценки)
    net_pnl_usd = gross_pnl_usd - fee_RT_est
    net_r = net_pnl_usd / risk_dist / qty

    Используется для подсветки ИИ-у реальной картинки: триггер
    PEAK-DRAWDOWN на gross-R может показать peak=+0.8R, но **net**
    после fee = +0.5R — то есть локированная прибыль гораздо меньше
    чем кажется. Аналогично LOCKED-PROFIT (1.5R gross ≈ 1.2R net).

    Все поля могут быть None если расчёт невозможен (нет SL, нет цены,
    нет qty).
    """
    peak_r: float | None
    current_r: float | None
    peak_r_net: float | None
    current_r_net: float | None
    fee_round_trip_usd: float | None


def _compute_position_pnl_stats(
    position: AiPosition,
    bars_1h: list[Bar],
    current_price: float | None,
    *,
    taker_fee_pct: float = 0.0,
) -> PositionPnlStats:
    """v0.20: полный расчёт peak/current R-units gross И net (after fees).

    Логика gross-расчёта идентична прежней _compute_position_r_stats:
      - Buy:  peak_r = (max(high)   - entry) / risk_dist
      - Sell: peak_r = (entry - min(low))   / risk_dist
      - current_r от ticker.last_price.

    Net-расчёт (v0.20):
      gross_pnl_usd_at_R = R × risk_dist × qty
      fee_RT_usd ≈ entry × qty × taker_fee_pct × 2 (приближение close=entry)
        (для точности peak: можно использовать peak_price × qty × pct + entry_value × pct,
         но разница на 1-2% от feeRT в типичных trade — не существенно)
      net_pnl_usd = gross - fee_RT
      net_r = net_pnl_usd / (risk_dist × qty)

    Если ``taker_fee_pct == 0`` → ``_net`` поля равны ``_gross``.
    """
    empty = PositionPnlStats(None, None, None, None, None)
    if position.sl_price is None or position.entry_price is None:
        return empty
    risk_dist = abs(position.entry_price - position.sl_price)
    if risk_dist <= 0:
        return empty
    qty = position.qty
    if qty is None or qty <= 0:
        return empty

    try:
        opened_dt = datetime.fromisoformat(position.opened_at.replace("Z", "+00:00"))
        opened_ms = int(opened_dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        opened_ms = 0

    relevant = [b for b in bars_1h if b.ts >= opened_ms]

    peak_r: float | None = None
    if relevant:
        if position.side == "Buy":
            peak_price = max(b.high for b in relevant)
            peak_r = (peak_price - position.entry_price) / risk_dist
        else:
            peak_price = min(b.low for b in relevant)
            peak_r = (position.entry_price - peak_price) / risk_dist

    current_r: float | None = None
    if current_price is not None:
        if position.side == "Buy":
            current_r = (current_price - position.entry_price) / risk_dist
        else:
            current_r = (position.entry_price - current_price) / risk_dist

    if peak_r is not None and current_r is not None:
        peak_r = max(peak_r, current_r)
    elif peak_r is None and current_r is not None:
        peak_r = current_r

    fee_RT_usd: float | None = None
    peak_r_net: float | None = peak_r
    current_r_net: float | None = current_r
    if taker_fee_pct > 0:
        fee_RT_usd = abs(position.entry_price) * qty * taker_fee_pct * 2
        denom = risk_dist * qty
        if denom > 0:
            if peak_r is not None:
                peak_r_net = peak_r - (fee_RT_usd / denom)
            if current_r is not None:
                current_r_net = current_r - (fee_RT_usd / denom)

    return PositionPnlStats(
        peak_r=peak_r,
        current_r=current_r,
        peak_r_net=peak_r_net,
        current_r_net=current_r_net,
        fee_round_trip_usd=fee_RT_usd,
    )


def collect_review_context(
    client: AiBybitClient,
    store: AiTraderStore,
    virtual_capital_usd: float,
    *,
    taker_fee_pct: float = 0.0,
) -> MarketContext:
    """Lite-сборка контекста для review-цикла (5 мин). v0.10-backport на v0.3.

    В отличие от `collect_market_context` (full): фетчит только символы
    с open positions, не дёргает 4H бары, news. Цель: дёшево обновить
    LLM-у картинку по уже открытым позициям, чтобы она могла принять
    early-close решение между full-cycle'ами (раз в 5 мин против 15 мин).

    Упрощения относительно v0.10-оригинала (под v0.3-базу без i1–i6):
    - Без positioning (нет OI / L-S / funding_history — это i2/i4).
    - Без VWAP/RV в indicators (i1) — compute_snapshot вызывается без
      vwap_window/rv_window.

    Если open positions == 0 → возвращает MarketContext с пустыми
    snapshots (caller обычно пропускает review в этом случае).
    """
    open_positions = store.get_open_positions()
    real_equity = client.get_wallet_balance()
    if not open_positions:
        return MarketContext(
            snapshots=[],
            open_positions=[],
            virtual_capital_usd=virtual_capital_usd,
            real_equity_usd=real_equity,
            news=[],
            taker_fee_pct=taker_fee_pct,
        )

    review_symbols = sorted({p.symbol for p in open_positions})
    snapshots: list[SymbolSnapshot] = []
    for sym in review_symbols:
        ticker = client.get_ticker(sym)
        bars_1h = _drop_incomplete_bar(
            client.get_klines(sym, interval="60", limit=50), 60
        )
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
                ticker=ticker,
                bars_1h=bars_1h,
                bars_4h=[],
                ind_1h=ind_1h,
                ind_4h=None,
            )
        )

    # v0.17 Шаг 2a: live данные биржи для review-цикла (тот же
    # principle что в collect_market_context).
    exchange_positions = client.get_positions()
    live_positions: dict[str, Position] | None
    if exchange_positions is None:
        live_positions = None
    else:
        live_positions = {p.symbol: p for p in exchange_positions}

    return MarketContext(
        snapshots=snapshots,
        open_positions=open_positions,
        virtual_capital_usd=virtual_capital_usd,
        real_equity_usd=real_equity,
        news=[],
        live_positions=live_positions,
        taker_fee_pct=taker_fee_pct,
    )


def _format_live_position_line(
    position: AiPosition,
    live_positions: dict[str, Position] | None,
    *,
    taker_fee_pct: float = 0.0,
) -> str | None:
    """v0.17 Шаг 2a: форматирует live данные биржи по позиции.

    Возвращает строку вида:
      ``LIVE: mark=$77205.4 unrealised=+$0.39 liq=$58400 (29% buffer)
      margin=$231.41``
    либо ``"LIVE: API unavailable"`` если запрос упал, либо
    ``"LIVE: not found on exchange (reconcile pending)"`` если БД
    говорит что позиция открыта, а биржа её не вернула.

    None — если ``live_positions={}`` и позиция не открыта на бирже
    (тогда выше уже выведена нормальная "no live data" ситуация —
    скорее всего БД stale и close-reconcile вот-вот отработает).

    v0.20 (2026-05-28): если ``taker_fee_pct > 0`` — добавляется
    `close_net=+$X.XX (after -$Y.YY close fee)` (что реально получим
    при закрытии прямо сейчас по mark price). Bybit ``unrealised_pnl`` =
    (mark - entry) × size — НЕ учитывает закрывающий taker fee, который
    спишется при reduce-only ордере. openFee уже списан при открытии
    (sunk cost, не возвращается).
    """
    if live_positions is None:
        return "     LIVE: API unavailable (cannot verify live PnL)"
    live = live_positions.get(position.symbol)
    if live is None:
        return "     LIVE: not found on exchange (reconcile pending)"
    mark = live.mark_price or 0.0
    unreal = live.unrealised_pnl or 0.0
    liq = live.liq_price or 0.0
    pos_val = live.position_value or 0.0
    margin = pos_val / live.leverage if live.leverage > 0 else pos_val
    buf_pct = ""
    if liq > 0 and mark > 0:
        # Расстояние до liq в %, направленное (по side).
        if position.side == "Buy":
            buf = (mark - liq) / mark * 100 if mark > liq else 0
        else:
            buf = (liq - mark) / mark * 100 if liq > mark else 0
        buf_pct = f" ({buf:.0f}% buffer)"
    net_suffix = ""
    if taker_fee_pct > 0 and mark > 0:
        close_fee = abs(mark) * abs(live.size or position.qty) * taker_fee_pct
        net_close = unreal - close_fee
        net_suffix = (
            f" close_net={net_close:+.2f}$ (after -${close_fee:.2f} close fee)"
        )
    return (
        f"     LIVE: mark=${mark:.6g} unrealised={unreal:+.2f}$ "
        f"liq=${liq:.6g}{buf_pct} margin=${margin:.2f}{net_suffix}"
    )


def format_context_for_review(ctx: MarketContext) -> str:
    """Lite-форматтер: только текущее состояние открытых позиций. v0.3-вариант.

    Без macro/news/options/4H/positioning — review-цикл фокусируется на
    быстрых сигналах для exit-решения. Каждая позиция получает блок с её
    символом + ticker + 1H closes + 1H индикаторы.
    """
    parts: list[str] = []
    parts.append(f"VIRTUAL CAPITAL: ${ctx.virtual_capital_usd:.2f}")
    parts.append(f"OPEN POSITIONS: {len(ctx.open_positions)}")
    parts.append("")
    parts.append("=== MARKET DATA (positions only, lite review cycle) ===")
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
            recent = s.bars_1h[-6:]
            closes = [f"{b.close:.6g}" for b in recent]
            parts.append("  1h closes (last 6h, oldest→newest):")
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
            parts.append(
                f"  id={p.id} {p.side} {p.symbol} qty={p.qty} entry=${p.entry_price:.6g} "
                f"sl=${p.sl_price or 0:.6g} tp=${p.tp_price or 0:.6g} "
                f"lev={p.leverage}x linkid={p.order_link_id}"
            )
            sym_snap = next((s for s in ctx.snapshots if s.symbol == p.symbol), None)
            bars = sym_snap.bars_1h if sym_snap else []
            cur_price = sym_snap.ticker.last_price if sym_snap and sym_snap.ticker else None
            stats = _compute_position_pnl_stats(
                p, bars, cur_price, taker_fee_pct=ctx.taker_fee_pct
            )
            if stats.peak_r is not None and stats.current_r is not None:
                line = (
                    f"     peak_pnl_r={stats.peak_r:+.2f}R "
                    f"current_pnl_r={stats.current_r:+.2f}R"
                )
                # v0.20: net-R только если fee известен (taker_fee_pct > 0)
                if (
                    ctx.taker_fee_pct > 0
                    and stats.peak_r_net is not None
                    and stats.current_r_net is not None
                    and stats.fee_round_trip_usd is not None
                ):
                    line += (
                        f" | NET (after est. RT fees ${stats.fee_round_trip_usd:.2f}): "
                        f"peak={stats.peak_r_net:+.2f}R "
                        f"cur={stats.current_r_net:+.2f}R"
                    )
                parts.append(line)
            live_line = _format_live_position_line(
                p, ctx.live_positions, taker_fee_pct=ctx.taker_fee_pct
            )
            if live_line is not None:
                parts.append(live_line)

    return "\n".join(parts)


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
            sym_snap = next((s for s in ctx.snapshots if s.symbol == p.symbol), None)
            bars = sym_snap.bars_1h if sym_snap else []
            cur_price = sym_snap.ticker.last_price if sym_snap and sym_snap.ticker else None
            stats = _compute_position_pnl_stats(
                p, bars, cur_price, taker_fee_pct=ctx.taker_fee_pct
            )
            if stats.peak_r is not None and stats.current_r is not None:
                line = (
                    f"     peak_pnl_r={stats.peak_r:+.2f}R "
                    f"current_pnl_r={stats.current_r:+.2f}R"
                )
                if (
                    ctx.taker_fee_pct > 0
                    and stats.peak_r_net is not None
                    and stats.current_r_net is not None
                    and stats.fee_round_trip_usd is not None
                ):
                    line += (
                        f" | NET (after est. RT fees ${stats.fee_round_trip_usd:.2f}): "
                        f"peak={stats.peak_r_net:+.2f}R "
                        f"cur={stats.current_r_net:+.2f}R"
                    )
                parts.append(line)
            live_line = _format_live_position_line(
                p, ctx.live_positions, taker_fee_pct=ctx.taker_fee_pct
            )
            if live_line is not None:
                parts.append(live_line)

    return "\n".join(parts)
