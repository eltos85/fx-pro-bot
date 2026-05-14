"""Сборщик market context для AI Arena (Nof1 layout).

Per-symbol layout (см. gist nof1-prompt.md):
- Current snapshot: price, EMA20, MACD, RSI(7)
- Perp metrics: OI latest + avg(20×5min), Funding rate + band label
- Intraday (3m × 10, oldest→newest): prices, EMA20, MACD, RSI(7), RSI(14)
- Longer-term (4h):
  - 20-EMA vs 50-EMA
  - 3-ATR vs 14-ATR
  - Volume current vs Volume avg(20)
  - MACD ×10
  - RSI(14) ×10

Никаких новостей / sentiment / orderflow — Nof1 явно пишет «no news,
no social media, no narratives». См. правило `ai-arena-sources.mdc`.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from ai_arena.analysis.indicators import (
    IntradaySnapshot,
    LongerTermSnapshot,
    build_intraday_snapshot,
    build_longer_term_snapshot,
    funding_band_label,
)
from ai_arena.state.db import ArenaPosition, AiArenaStore
from ai_arena.trading.client import (
    AiArenaBybitClient,
    Bar,
    OpenInterestPoint,
    Ticker,
)

log = logging.getLogger(__name__)


def _drop_incomplete_bar(bars: list[Bar], interval_minutes: int) -> list[Bar]:
    """Отбрасывает незакрытый последний бар (avoid look-ahead bias).

    Bybit `get_kline` возвращает массив включая текущую формирующуюся
    свечу. Каноничные индикаторы (RSI/MACD) определены на closed candles.
    """
    if not bars:
        return bars
    now_ms = int(time.time() * 1000)
    interval_ms = max(1, interval_minutes) * 60 * 1000
    if bars[-1].ts + interval_ms > now_ms:
        return bars[:-1]
    return bars


@dataclass
class SymbolBlock:
    symbol: str
    ticker: Ticker | None
    intraday: IntradaySnapshot | None
    longer_term: LongerTermSnapshot | None
    oi_latest: float | None
    oi_avg: float | None


@dataclass
class MarketContext:
    blocks: list[SymbolBlock]
    open_positions: list[ArenaPosition]
    virtual_capital_usd: float
    real_equity_usd: float
    available_cash_usd: float


# ─── Сбор данных ─────────────────────────────────────────────────────────


def collect_market_context(
    client: AiArenaBybitClient,
    store: AiArenaStore,
    symbols: tuple[str, ...],
    virtual_capital_usd: float,
) -> MarketContext:
    blocks: list[SymbolBlock] = []
    for sym in symbols:
        ticker = client.get_ticker(sym)
        bars_3m = _drop_incomplete_bar(
            client.get_klines(sym, interval="3", limit=50), 3
        )
        bars_4h = _drop_incomplete_bar(
            client.get_klines(sym, interval="240", limit=60), 240
        )
        oi_points: list[OpenInterestPoint] = client.get_open_interest(
            sym, interval_time="5min", limit=20
        )

        intraday = None
        if len(bars_3m) >= 27:  # need ≥ slow(26) + 1 для MACD; берём с запасом
            intraday = build_intraday_snapshot([b.close for b in bars_3m], take_n=10)

        longer = None
        if len(bars_4h) >= 50:  # need ≥ EMA50
            longer = build_longer_term_snapshot(
                [b.high for b in bars_4h],
                [b.low for b in bars_4h],
                [b.close for b in bars_4h],
                [b.volume for b in bars_4h],
                take_n=10,
            )

        oi_latest = oi_points[-1].open_interest if oi_points else None
        oi_avg = (
            sum(p.open_interest for p in oi_points) / len(oi_points)
            if oi_points
            else None
        )
        blocks.append(
            SymbolBlock(
                symbol=sym,
                ticker=ticker,
                intraday=intraday,
                longer_term=longer,
                oi_latest=oi_latest,
                oi_avg=oi_avg,
            )
        )

    open_positions = store.get_open_positions()
    equity, available = client.get_wallet_balance()
    return MarketContext(
        blocks=blocks,
        open_positions=open_positions,
        virtual_capital_usd=virtual_capital_usd,
        real_equity_usd=equity,
        available_cash_usd=available,
    )


# ─── Форматирование под prompt ───────────────────────────────────────────


def _fmt_n(x: float | None, pat: str = "{:.6g}") -> str:
    return pat.format(x) if x is not None else "n/a"


def _fmt_arr(arr: list[float | None] | list[float], pat: str = "{:.6g}") -> str:
    """Возвращает '[v1, v2, …]' с n/a для None."""
    parts: list[str] = []
    for v in arr:
        parts.append(pat.format(v) if isinstance(v, (int, float)) else "n/a")
    return "[" + ", ".join(parts) + "]"


def format_symbol_block(block: SymbolBlock) -> str:
    """Per-symbol блок в стиле gist'а Nof1 («### ALL BTC DATA …»)."""
    sym = block.symbol
    if block.ticker is None:
        return f"### ALL {sym} DATA\n(ticker unavailable, skipping)\n"

    t = block.ticker
    fr_band = funding_band_label(t.funding_rate)
    cur_ema20 = (
        block.intraday.ema20[-1] if block.intraday and block.intraday.ema20 else None
    )
    cur_macd = (
        block.intraday.macd[-1] if block.intraday and block.intraday.macd else None
    )
    cur_rsi7 = (
        block.intraday.rsi7[-1] if block.intraday and block.intraday.rsi7 else None
    )

    parts: list[str] = []
    parts.append(f"### ALL {sym} DATA\n")
    parts.append("Current Snapshot:")
    parts.append(f"- current_price = {_fmt_n(t.last_price)}")
    parts.append(f"- current_ema20 = {_fmt_n(cur_ema20)}")
    parts.append(f"- current_macd  = {_fmt_n(cur_macd)}")
    parts.append(f"- current_rsi (7 period) = {_fmt_n(cur_rsi7)}")
    parts.append("")

    parts.append("Perpetual Futures Metrics:")
    parts.append(
        f"- Open Interest: Latest: {_fmt_n(block.oi_latest)} | "
        f"Average (20×5min): {_fmt_n(block.oi_avg)}"
    )
    parts.append(
        f"- Funding Rate: {t.funding_rate * 100:+.4f}%  (band: {fr_band})"
    )
    parts.append("")

    if block.intraday is not None:
        parts.append("Intraday Series (3-minute intervals, oldest → latest):")
        parts.append(f"  Mid prices:                {_fmt_arr(block.intraday.prices)}")
        parts.append(f"  EMA indicators (20-period): {_fmt_arr(block.intraday.ema20)}")
        parts.append(f"  MACD indicators:            {_fmt_arr(block.intraday.macd)}")
        parts.append(f"  RSI indicators (7-period):  {_fmt_arr(block.intraday.rsi7, '{:.2f}')}")
        parts.append(f"  RSI indicators (14-period): {_fmt_arr(block.intraday.rsi14, '{:.2f}')}")
        parts.append("")
    else:
        parts.append("Intraday Series: insufficient data for this cycle\n")

    if block.longer_term is not None:
        lt = block.longer_term
        parts.append("Longer-term Context (4-hour timeframe):")
        parts.append(
            f"  20-Period EMA: {_fmt_n(lt.ema20)}  vs.  50-Period EMA: {_fmt_n(lt.ema50)}"
        )
        parts.append(
            f"   3-Period ATR: {_fmt_n(lt.atr3)}  vs.  14-Period ATR: {_fmt_n(lt.atr14)}"
        )
        parts.append(
            f"  Current Volume: {_fmt_n(lt.volume_current, '{:.2f}')}  vs.  Average Volume (20): {_fmt_n(lt.volume_avg, '{:.2f}')}"
        )
        parts.append(f"  MACD indicators (4h):       {_fmt_arr(lt.macd)}")
        parts.append(f"  RSI indicators (14, 4h):    {_fmt_arr(lt.rsi14, '{:.2f}')}")
    else:
        parts.append("Longer-term Context (4-hour timeframe): insufficient data")

    return "\n".join(parts) + "\n"


def format_open_positions_block(
    positions: list[ArenaPosition],
    *,
    current_prices: dict[str, float],
    liquidation_prices: dict[str, float],
    notional_by_symbol: dict[str, float],
    unrealized_by_symbol: dict[str, float],
) -> str:
    """Компактный JSON-блок открытых позиций (формат из gist'а)."""
    if not positions:
        return "[]"
    arr = []
    for p in positions:
        cur = current_prices.get(p.symbol, p.entry_price)
        liq = liquidation_prices.get(p.symbol, 0.0)
        unrl = unrealized_by_symbol.get(p.symbol, 0.0)
        notional = notional_by_symbol.get(p.symbol, p.qty * cur)
        arr.append(
            {
                "symbol": p.symbol,
                "side": "long" if p.side == "Buy" else "short",
                "quantity": p.qty,
                "entry_price": p.entry_price,
                "current_price": cur,
                "liquidation_price": liq,
                "unrealized_pnl": round(unrl, 4),
                "leverage": p.leverage,
                "exit_plan": {
                    "profit_target": p.tp_price,
                    "stop_loss": p.sl_price,
                    "invalidation_condition": p.invalidation_condition,
                },
                "confidence": p.confidence,
                "risk_usd": p.risk_usd,
                "notional_usd": round(notional, 4),
            }
        )
    return json.dumps(arr, indent=2, default=str)


def format_per_symbol_blocks(ctx: MarketContext) -> str:
    return "\n---\n\n".join(format_symbol_block(b) for b in ctx.blocks)
