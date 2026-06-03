"""Swing-скринер 1–3 дня: детерминированные правила (без подгонки под P&L).

Research: EMA trend (Murphy 1999), RSI 14 (Wilder 1978), ATR stops (Wilder 1978).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_trader.analysis.indicators import compute_snapshot
from ru_stocks_analyst.data.universe import ShareInstrument
from ru_stocks_analyst.tinkoff.rest_client import TinkoffRestClient, quotation_to_float


@dataclass
class SwingIdea:
    ticker: str
    name: str
    direction: str  # "long" | "short"
    last_close: float
    entry_hint: float
    stop: float
    target: float
    horizon_days: int
    rsi14: float
    atr_pct: float
    reason: str
    score: float


def _candles_to_ohlc(candles: list[dict[str, Any]]) -> tuple[list[float], list[float], list[float], list[int]]:
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[int] = []
    for c in candles:
        closes.append(quotation_to_float(c.get("close")))
        highs.append(quotation_to_float(c.get("high")))
        lows.append(quotation_to_float(c.get("low")))
        vol = c.get("volume")
        volumes.append(int(vol) if vol is not None else 0)
    return closes, highs, lows, volumes


def _avg_volume(volumes: list[int], period: int = 10) -> float | None:
    if len(volumes) < period + 1:
        return None
    return sum(volumes[-period - 1 : -1]) / period


def evaluate_share(
    inst: ShareInstrument,
    candles: list[dict[str, Any]],
) -> SwingIdea | None:
    if len(candles) < 55:
        return None
    closes, highs, lows, volumes = _candles_to_ohlc(candles)
    close = closes[-1]
    if close <= 0:
        return None

    snap = compute_snapshot(closes, highs, lows)
    r = snap.rsi14
    e20 = snap.ema20
    e50 = snap.ema50
    atr_v = snap.atr14
    if r is None or e20 is None or e50 is None or atr_v is None:
        return None
    atr_pct = (atr_v / close) * 100

    vol_avg = _avg_volume(volumes)
    vol_ok = True
    if vol_avg and vol_avg > 0:
        vol_ok = volumes[-1] >= 1.1 * vol_avg

    # Long: восходящий тренд + не перекуплен
    if close > e20 > e50 and 45 <= r <= 68 and vol_ok and atr_pct >= 0.8:
        stop = close - 1.5 * atr_v
        target = close + 2.0 * atr_v
        score = (close - e50) / e50 * 100 + (r - 50) * 0.1
        return SwingIdea(
            ticker=inst.ticker,
            name=inst.name,
            direction="long",
            last_close=close,
            entry_hint=close,
            stop=round(stop, 2),
            target=round(target, 2),
            horizon_days=3,
            rsi14=round(r, 1),
            atr_pct=round(atr_pct, 2),
            reason="тренд EMA20>EMA50, RSI в зоне продолжения, объём выше среднего",
            score=score,
        )

    # Short / фиксация: нисходящий тренд
    if close < e20 < e50 and r < 42 and vol_ok:
        stop = close + 1.5 * atr_v
        target = close - 2.0 * atr_v
        score = (e50 - close) / e50 * 100 + (50 - r) * 0.1
        return SwingIdea(
            ticker=inst.ticker,
            name=inst.name,
            direction="short",
            last_close=close,
            entry_hint=close,
            stop=round(stop, 2),
            target=round(target, 2),
            horizon_days=3,
            rsi14=round(r, 1),
            atr_pct=round(atr_pct, 2),
            reason="тренд EMA20<EMA50, слабый RSI — риск продолжения снижения",
            score=score,
        )

    return None


def scan_universe(
    client: TinkoffRestClient,
    ranked: list[tuple[ShareInstrument, float]],
    *,
    candle_days: int,
) -> list[SwingIdea]:
    now = datetime.now(UTC)
    start = now - timedelta(days=candle_days + 5)
    from_iso = start.strftime("%Y-%m-%dT00:00:00Z")
    to_iso = now.strftime("%Y-%m-%dT23:59:59Z")

    ideas: list[SwingIdea] = []
    for inst, _px in ranked:
        try:
            candles = client.get_candles(
                inst.uid,
                from_iso=from_iso,
                to_iso=to_iso,
                interval="CANDLE_INTERVAL_DAY",
            )
        except Exception:
            continue
        idea = evaluate_share(inst, candles)
        if idea:
            ideas.append(idea)

    ideas.sort(key=lambda x: x.score, reverse=True)
    return ideas
