"""LLM-perspective simulation: что увидит DeepSeek в реальном цикле.

Цель: prove что промпт + контекст внутренне непротиворечивы. Этот тест
не валидирует значения — он рендерит полный prompt+context и сохраняет
в файл для humanual review (или для будущего LLM-based консистент-чека).

Использование:
    pytest tests/test_ai_trader_llm_perspective.py -s
    cat /tmp/ai_trader_llm_simulation.txt
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_trader.config.settings import AiTraderSettings
from ai_trader.llm.prompts import build_system_prompt, build_user_prompt
from ai_trader.news.rss import NewsItem
from ai_trader.state.db import AiPosition
from ai_trader.trading.client import Bar, Position, Ticker
from ai_trader.trading.context import (
    MarketContext,
    SymbolSnapshot,
    format_context_for_prompt,
)
from ai_trader.analysis.indicators import compute_snapshot


def _build_bars(
    start_ts_ms: int, n: int, base_price: float, interval_ms: int,
) -> list[Bar]:
    """Synthetic ascending bars (price drifts +0.5% per bar with noise)."""
    bars: list[Bar] = []
    price = base_price
    for i in range(n):
        ts = start_ts_ms + i * interval_ms
        close = price * (1.0 + 0.005 * (1 if i % 3 != 2 else -1))
        high = max(price, close) * 1.003
        low = min(price, close) * 0.997
        bars.append(Bar(
            ts=ts, open=price, high=high, low=low, close=close, volume=1_000_000.0,
        ))
        price = close
    return bars


def test_render_full_llm_input(tmp_path: Path):
    """Прогон полного цикла. Сохраняет SYSTEM+USER prompt в файл для
    human review.
    """
    settings = AiTraderSettings()

    # ─── Build realistic MarketContext ─────────────────────────────────
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    one_h_ms = 60 * 60 * 1000
    four_h_ms = 4 * one_h_ms

    # 3 symbols: BTC (open BUY), ETH (no pos), SOL (open SELL)
    snapshots: list[SymbolSnapshot] = []
    for sym, price, has_pos in [
        ("BTCUSDT", 95_000.0, True),
        ("ETHUSDT", 3_200.0, False),
        ("SOLUSDT", 180.0, True),
    ]:
        bars_1h = _build_bars(now_ms - 100 * one_h_ms, 100, price * 0.95, one_h_ms)
        bars_4h = _build_bars(now_ms - 50 * four_h_ms, 50, price * 0.92, four_h_ms)
        ind_1h = compute_snapshot(
            [b.high for b in bars_1h],
            [b.low for b in bars_1h],
            [b.close for b in bars_1h],
        )
        ind_4h = compute_snapshot(
            [b.high for b in bars_4h],
            [b.low for b in bars_4h],
            [b.close for b in bars_4h],
        )
        ticker = Ticker(
            symbol=sym,
            last_price=price,
            bid=price * 0.9998,
            ask=price * 1.0002,
            funding_rate=0.0001 if sym == "BTCUSDT" else (0.0008 if sym == "SOLUSDT" else 0.00002),
            volume_24h=1_000_000.0,
            price_change_pct_24h=0.025 if sym == "BTCUSDT" else (-0.012 if sym == "SOLUSDT" else 0.005),
            next_funding_time_ms=int((datetime.now(tz=UTC) + timedelta(minutes=22)).timestamp() * 1000),
        )
        snapshots.append(SymbolSnapshot(
            symbol=sym, ticker=ticker,
            bars_1h=bars_1h, bars_4h=bars_4h,
            ind_1h=ind_1h, ind_4h=ind_4h,
        ))

    # Open positions: BTC long (in profit), SOL short (slightly underwater).
    open_positions = [
        AiPosition(
            id=27, symbol="BTCUSDT", side="Buy", qty=0.005,
            entry_price=93_500.0, sl_price=92_100.0, tp_price=96_300.0,
            leverage=3, order_link_id="ai_btc_27",
            opened_at=(datetime.now(tz=UTC) - timedelta(hours=8)).isoformat(),
            closed_at=None, exit_price=None, realized_pnl_usd=None,
            close_reason=None,
            llm_reason="BTC long: 4H trend up + ETF news supportive + BB-Z fade buy.",
            confidence=0.65,
            invalidation_condition="1H close below 92500 with vol > 1.5x avg",
            risk_usd_declared=7.0,
            pnl_source=None, funding_usd=None,
            macro_thesis=(
                "Hierarchy driver #1: ETF inflow regime (recent $1.2B 5d "
                "reporting per supportive news), DXY softening at 99.0 support "
                "test (recent 5d -0.6%), Fed minutes dovish — institutional "
                "flow + macro tailwind for BTC long bias."
            ),
        ),
        AiPosition(
            id=31, symbol="SOLUSDT", side="Sell", qty=0.5,
            entry_price=185.0, sl_price=192.0, tp_price=174.0,
            leverage=3, order_link_id="ai_sol_31",
            opened_at=(datetime.now(tz=UTC) - timedelta(hours=3)).isoformat(),
            closed_at=None, exit_price=None, realized_pnl_usd=None,
            close_reason=None,
            llm_reason="SOL short: extreme overbought + funding 0.08% strong long bias contrarian.",
            confidence=0.55,
            invalidation_condition="1H close above 188 with MACD bullish flip",
            risk_usd_declared=4.0,
            pnl_source=None, funding_usd=None,
            macro_thesis=(
                "SOL/BTC ratio breaking down (5d -2.3%), Alpenglow narrative "
                "fading post-launch, BTC.D rising 60.1→60.4% = alt rotation "
                "negative — contrarian short setup against funding extreme."
            ),
        ),
    ]

    live_positions = {
        "BTCUSDT": Position(
            symbol="BTCUSDT", side="Buy", size=0.005,
            entry_price=93_500.0, leverage=3,
            unrealised_pnl=7.5, position_value=475.0,
        ),
        "SOLUSDT": Position(
            symbol="SOLUSDT", side="Sell", size=0.5,
            entry_price=185.0, leverage=3,
            unrealised_pnl=2.5, position_value=92.5,
        ),
    }

    # 3 news items: mixed sentiment.
    news = [
        NewsItem(
            title="BTC Spot ETF Sees $890M Net Inflow on Tuesday, Largest in 3 Weeks",
            summary="BlackRock IBIT led with $620M of inflows; institutional appetite returning amid Fed dovish pivot.",
            source="Coindesk", published_iso=datetime.now(tz=UTC).isoformat(),
            url="https://example.com/btc-etf", symbols=["BTCUSDT"],
        ),
        NewsItem(
            title="Solana DeFi TVL Tops $9B as Alpenglow Upgrade Launches",
            summary="Bullish for SOL DeFi but unclear if sufficient to reverse short-term BTC.D rise.",
            source="CryptoSlate",
            published_iso=(datetime.now(tz=UTC) - timedelta(minutes=45)).isoformat(),
            url="https://example.com/sol", symbols=["SOLUSDT"],
        ),
        NewsItem(
            title="ECB Surprise: Holds Rates, Signals Possible 2026 Cut",
            summary="DXY -0.4% on the news; risk-on rally in equities. Crypto reaction muted so far.",
            source="Reuters",
            published_iso=(datetime.now(tz=UTC) - timedelta(hours=2)).isoformat(),
            url="https://example.com/ecb", symbols=[],
        ),
    ]

    # SELF-REFLECTION mock data.
    per_symbol_pnl = [
        {"symbol": "BTCUSDT", "n": 8, "wins": 5, "win_rate_pct": 62.5,
         "avg_pnl_usd": 1.85, "sum_pnl_usd": 14.80},
        {"symbol": "ETHUSDT", "n": 0, "wins": 0, "win_rate_pct": 0.0,
         "avg_pnl_usd": 0.0, "sum_pnl_usd": 0.0},
        {"symbol": "SOLUSDT", "n": 3, "wins": 1, "win_rate_pct": 33.3,
         "avg_pnl_usd": -2.10, "sum_pnl_usd": -6.30},
    ]
    per_symbol_side_pnl = [
        {"symbol": "BTCUSDT", "side": "Buy", "n": 6, "wins": 4, "win_rate_pct": 66.7,
         "avg_pnl_usd": 2.10, "sum_pnl_usd": 12.60},
        {"symbol": "BTCUSDT", "side": "Sell", "n": 2, "wins": 1, "win_rate_pct": 50.0,
         "avg_pnl_usd": 1.10, "sum_pnl_usd": 2.20},
        {"symbol": "ETHUSDT", "side": "Buy", "n": 0, "wins": 0, "win_rate_pct": 0.0,
         "avg_pnl_usd": 0.0, "sum_pnl_usd": 0.0},
        {"symbol": "ETHUSDT", "side": "Sell", "n": 0, "wins": 0, "win_rate_pct": 0.0,
         "avg_pnl_usd": 0.0, "sum_pnl_usd": 0.0},
        {"symbol": "SOLUSDT", "side": "Buy", "n": 1, "wins": 0, "win_rate_pct": 0.0,
         "avg_pnl_usd": -3.20, "sum_pnl_usd": -3.20},
        {"symbol": "SOLUSDT", "side": "Sell", "n": 2, "wins": 1, "win_rate_pct": 50.0,
         "avg_pnl_usd": -1.55, "sum_pnl_usd": -3.10},
    ]
    recent_closed_trades = [
        {
            "id": 22, "symbol": "BTCUSDT", "side": "Buy", "qty": 0.005,
            "entry_price": 91_200.0, "exit_price": 92_400.0,
            "realized_pnl_usd": 5.85,
            "opened_at": (datetime.now(tz=UTC) - timedelta(days=2, hours=3)).isoformat(),
            "closed_at": (datetime.now(tz=UTC) - timedelta(days=2)).isoformat(),
            "duration_minutes": 180,
            "llm_reason": "BTC: 4H EMA20>50 trend, news ETF inflow.",
            "close_reason": "TP hit at +1.5R.",
            "macro_thesis": "ETF flow regime + DXY 99.5 support hold.",
        },
        {
            "id": 24, "symbol": "SOLUSDT", "side": "Buy", "qty": 0.5,
            "entry_price": 190.0, "exit_price": 186.8,
            "realized_pnl_usd": -3.20,
            "opened_at": (datetime.now(tz=UTC) - timedelta(days=1, hours=10)).isoformat(),
            "closed_at": (datetime.now(tz=UTC) - timedelta(days=1, hours=4)).isoformat(),
            "duration_minutes": 360,
            "llm_reason": "SOL bounce off VWAP zone.",
            "close_reason": "trigger 4 PEAK-DRAWDOWN: peak 0.85R cur 0.40R.",
            "macro_thesis": (
                "BTC.D stalling at 60% pre-Alpenglow, SOL relative strength "
                "+3% vs ETH last 24h."
            ),
        },
    ]

    ctx = MarketContext(
        snapshots=snapshots,
        open_positions=open_positions,
        virtual_capital_usd=settings.virtual_capital_usd,
        real_equity_usd=487.50,
        news=news,
        live_positions=live_positions,
        taker_fee_pct=settings.taker_fee_pct,
        macro_rates_block=(
            "=== US MACRO RATES (crypto drivers; BTC↔DXY corr -0.72..-0.90, "
            "BTC↔10Y -0.55) ===\n"
            "DXY (US Dollar Index, ICE futures DX-Y.NYB): "
            "99.12 (24h=-0.18%, 5d=-0.71%)\n"
            "UST10Y nominal yield (CBOE TNX): "
            "4.31% (24h=-3.0bps, 5d=-12.0bps)\n"
            "(fetched 2026-05-28T08:00:00+00:00 UTC)"
        ),
        crypto_macro_block=(
            "=== CRYPTO MACRO (BTC dominance + total cap) ===\n"
            "BTC.D=60.32% | ETH.D=12.45% | Total crypto cap=$3.45T (24h=-1.23%)\n"
            "Reference levels (May 2026): BTC.D support 59.63% / resistance "
            "66.06% (AInvest); Altcoin Season Index threshold >75 currently "
            "~35-45 (Bitrue)\n"
            "(fetched 2026-05-28T08:00:00+00:00 UTC)"
        ),
        per_symbol_pnl=per_symbol_pnl,
        per_symbol_side_pnl=per_symbol_side_pnl,
        recent_closed_trades=recent_closed_trades,
        stats_window_start="2026-05-15T00:00:00+00:00",
        realized_pnl_total_usd=8.50,
        unrealised_pnl_total_usd=10.0,
        peak_equity_usd=514.30,
    )

    system_prompt = build_system_prompt(settings)
    user_prompt = build_user_prompt(format_context_for_prompt(ctx))

    out_path = Path("/tmp/ai_trader_llm_simulation.txt")
    out_path.write_text(
        "=" * 80 + "\nSYSTEM PROMPT\n" + "=" * 80 + "\n" + system_prompt +
        "\n\n" + "=" * 80 + "\nUSER PROMPT\n" + "=" * 80 + "\n" + user_prompt + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote simulation to {out_path}")
    print(f"  SYSTEM_PROMPT: {len(system_prompt):,} chars")
    print(f"  USER_PROMPT:   {len(user_prompt):,} chars")
    print(f"  TOTAL:         {len(system_prompt) + len(user_prompt):,} chars")

    assert len(system_prompt) > 1000
    assert len(user_prompt) > 1000
    assert "BTCUSDT" in user_prompt
    assert "PER-ASSET MACRO DRIVER HIERARCHY" in system_prompt
    assert "WHAT YOU DO NOT SEE" in system_prompt
    assert "SELF-REFLECTION" in user_prompt
    assert "macro_thesis" in user_prompt  # должен быть рядом с открытыми позициями
