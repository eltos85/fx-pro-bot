"""Сборщик market context для AI Arena (Nof1 layout).

Per-symbol layout 1-в-1 c gist nof1-prompt.md (User Prompt § per-coin):
- Header `### ALL <COIN> DATA` — голый тикер без USDT-суффикса
  (gist line 345, 383, 421, …). Конвертация Bybit `BTCUSDT`→`BTC`
  через `bybit_to_arena` (см. `trading/symbols.py`).
- Current Snapshot: current_price, current_ema20, current_macd,
  current_rsi (7 period)
- Perpetual Futures Metrics: Open Interest (Latest + Average), Funding Rate
- Intraday Series (3-minute, oldest → latest): Mid prices, EMA(20),
  MACD, RSI(7-Period), RSI(14-Period)
- Longer-term Context (4-hour timeframe): 20-Period EMA vs 50-Period
  EMA, 3-Period ATR vs 14-Period ATR, Current Volume vs Average Volume,
  MACD indicators (4h), RSI indicators (14-Period, 4h)

Open positions block — Python repr-style list-of-dicts (gist line
457-478): single quotes, без double-quotes как в JSON. Source
форматирует как Python literal, не как JSON.

Никаких новостей / sentiment / orderflow — Nof1 явно пишет «no news,
no social media, no narratives». См. правило `ai-arena-sources.mdc`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ai_arena.analysis.indicators import (
    IntradaySnapshot,
    LongerTermSnapshot,
    build_intraday_snapshot,
    build_longer_term_snapshot,
)
from ai_arena.state.db import ArenaPosition, AiArenaStore
from ai_arena.trading.client import (
    AiArenaBybitClient,
    Bar,
    OpenInterestPoint,
    Ticker,
)
from ai_arena.trading.symbols import bybit_to_arena

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
            # OHLC4 = (O+H+L+C)/4 — каноническая «typical bar price»,
            # ближайшая аппроксимация mid-price за период бара. Используется
            # ТОЛЬКО для display массива «Mid prices» (gist L361). Индикаторы
            # (RSI/MACD/EMA) остаются на close-prices — финансово-математический
            # инвариант. Подробности в BUILDLOG_AI_ARENA.md (v2.x bug-fix).
            ohlc4_prices = [
                (b.open + b.high + b.low + b.close) / 4.0 for b in bars_3m
            ]
            intraday = build_intraday_snapshot(
                [b.close for b in bars_3m],
                take_n=10,
                display_prices=ohlc4_prices,
            )

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
    """Per-symbol блок 1-в-1 c gist nof1-prompt.md («### ALL BTC DATA …»).

    Все labels, markdown-bold (`**...:**`), formatting — буквальная копия
    из gist'а. Любая правка должна сохранять byte-level совместимость с
    source layout (см. правило `ai-arena-sources.mdc`).
    """
    sym_arena = bybit_to_arena(block.symbol)  # `BTCUSDT` → `BTC` (gist L345)
    if block.ticker is None:
        return f"### ALL {sym_arena} DATA\n(ticker unavailable, skipping)\n"

    t = block.ticker
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
    parts.append(f"### ALL {sym_arena} DATA\n")
    parts.append("**Current Snapshot:**")
    parts.append(f"- current_price = {_fmt_n(t.last_price)}")
    parts.append(f"- current_ema20 = {_fmt_n(cur_ema20)}")
    parts.append(f"- current_macd = {_fmt_n(cur_macd)}")
    parts.append(f"- current_rsi (7 period) = {_fmt_n(cur_rsi7)}")
    parts.append("")

    parts.append("**Perpetual Futures Metrics:**")
    parts.append(
        f"- Open Interest: Latest: {_fmt_n(block.oi_latest)} | "
        f"Average: {_fmt_n(block.oi_avg)}"
    )
    # Funding rate — сырое число с %-форматом (gist L355: «Funding Rate: 0.0123%»).
    # Без `+` модификатора — source даёт нейтральный формат, знак показывается
    # сам через минус для negative.
    parts.append(f"- Funding Rate: {t.funding_rate * 100:.4f}%")
    parts.append("")

    if block.intraday is not None:
        parts.append("**Intraday Series (3-minute intervals, oldest → latest):**")
        parts.append("")
        parts.append(f"Mid prices: {_fmt_arr(block.intraday.prices)}")
        parts.append("")
        parts.append(f"EMA indicators (20-period): {_fmt_arr(block.intraday.ema20)}")
        parts.append("")
        parts.append(f"MACD indicators: {_fmt_arr(block.intraday.macd)}")
        parts.append("")
        parts.append(f"RSI indicators (7-Period): {_fmt_arr(block.intraday.rsi7, '{:.2f}')}")
        parts.append("")
        parts.append(f"RSI indicators (14-Period): {_fmt_arr(block.intraday.rsi14, '{:.2f}')}")
        parts.append("")
    else:
        parts.append("Intraday Series: insufficient data for this cycle\n")

    if block.longer_term is not None:
        lt = block.longer_term
        parts.append("**Longer-term Context (4-hour timeframe):**")
        parts.append("")
        parts.append(
            f"20-Period EMA: {_fmt_n(lt.ema20)} vs. 50-Period EMA: {_fmt_n(lt.ema50)}"
        )
        parts.append("")
        parts.append(
            f"3-Period ATR: {_fmt_n(lt.atr3)} vs. 14-Period ATR: {_fmt_n(lt.atr14)}"
        )
        parts.append("")
        parts.append(
            f"Current Volume: {_fmt_n(lt.volume_current, '{:.2f}')} vs. "
            f"Average Volume: {_fmt_n(lt.volume_avg, '{:.2f}')}"
        )
        parts.append("")
        parts.append(f"MACD indicators (4h): {_fmt_arr(lt.macd)}")
        parts.append("")
        parts.append(f"RSI indicators (14-Period, 4h): {_fmt_arr(lt.rsi14, '{:.2f}')}")
    else:
        parts.append("**Longer-term Context (4-hour timeframe):** insufficient data")

    return "\n".join(parts) + "\n"


def format_open_positions_block(
    positions: list[ArenaPosition],
    *,
    current_prices: dict[str, float],
    liquidation_prices: dict[str, float],
    notional_by_symbol: dict[str, float],
    unrealized_by_symbol: dict[str, float],
) -> str:
    """Открытые позиции 1-в-1 c gist nof1-prompt.md line 457-478 — Python
    list-of-dicts repr (single quotes, не JSON).

    Source форматирует как Python literal, не как JSON:

        [
          {
            'symbol': 'BTC',
            'quantity': 0.5,
            ...
            'exit_plan': {
              'profit_target': 105000,
              ...
            },
            ...
          },
        ]

    Раньше у нас был ``json.dumps(...)`` — двойные кавычки и `null`,
    что отступление от source. Теперь — Python ``repr(arr)``-style.

    Поля идентичны source: symbol, quantity, entry_price, current_price,
    liquidation_price, unrealized_pnl, leverage, exit_plan, confidence,
    risk_usd, notional_usd.

    - ``symbol`` — Nof1-формат (`BTC`, не `BTCUSDT`); Bybit-формат
      конвертируется через ``bybit_to_arena``.
    - ``quantity`` — **signed** (positive=long, negative=short), как
      в Hyperliquid. Поле ``'side'`` отсутствует (gist).
    """
    if not positions:
        return "[]"
    items: list[dict] = []
    for p in positions:
        cur = current_prices.get(p.symbol, p.entry_price)
        liq = liquidation_prices.get(p.symbol, 0.0)
        unrl = unrealized_by_symbol.get(p.symbol, 0.0)
        notional = notional_by_symbol.get(p.symbol, p.qty * cur)
        signed_qty = p.qty if p.side == "Buy" else -p.qty
        items.append(
            {
                "symbol": bybit_to_arena(p.symbol),
                "quantity": signed_qty,
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
    return _python_repr_list(items)


def _python_repr_list(items: list[dict]) -> str:
    """Format list[dict] as Python literal (single quotes, indent=2).

    Используется для open positions block — source (gist L457-478)
    показывает блок как Python repr, не как JSON. Стандартный
    ``repr()`` даёт всё в одну строку без переносов; нам нужен
    indent=2 для читаемости (как в gist'е). Поэтому ручной writer.

    Поведение для типов:
    - str  → одинарные кавычки с экранированием `'` и `\\`
    - None → `None`
    - bool → `True`/`False`
    - int/float → стандартный repr
    - dict → рекурсивно с indent
    """
    return _repr_value(items, indent=0)


def _repr_value(v, *, indent: int) -> str:
    pad = "  " * indent
    pad_inner = "  " * (indent + 1)
    if v is None:
        return "None"
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _py_str_literal(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        body = ",\n".join(pad_inner + _repr_value(it, indent=indent + 1) for it in v)
        return "[\n" + body + "\n" + pad + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        lines = []
        for k, val in v.items():
            lines.append(
                f"{pad_inner}{_py_str_literal(str(k))}: "
                + _repr_value(val, indent=indent + 1)
            )
        return "{\n" + ",\n".join(lines) + "\n" + pad + "}"
    return _py_str_literal(str(v))


def _py_str_literal(s: str) -> str:
    """Python single-quoted string literal с экранированием `'` и `\\`."""
    escaped = s.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def format_per_symbol_blocks(ctx: MarketContext) -> str:
    return "\n---\n\n".join(format_symbol_block(b) for b in ctx.blocks)
