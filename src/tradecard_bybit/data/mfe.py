"""Post-exit MFE (Maximum Favorable Excursion) для детектора exit_left_money.

Sweeney 1988 (MFE): измеряем благоприятный ход цены ПОСЛЕ выхода — сколько
движения «оставлено на столе». Read-only (Bybit klines). Это свойство **правила
выхода**, не психологии (TASKSPEC §3.3/§4).

Окно после выхода — структурный параметр наблюдения (не торговый порог); берём
1 час M1-свечей. Fail-open: нет свечей → None (детектор пропустит сделку).
"""
from __future__ import annotations

from tradecard_bybit.analysis.trade import Trade
from tradecard_bybit.data.bybit_client import TradecardBybitReadOnly

# Окно наблюдения после выхода (мс) и ТФ klines.
_WINDOW_MS = 60 * 60 * 1000
_INTERVAL = "1"


def make_mfe_provider(client: TradecardBybitReadOnly):
    """Возвращает mfe_fn(trade) -> favorable price excursion после выхода | None."""

    def mfe_fn(t: Trade) -> float | None:
        if t.ts_close is None or t.exit is None or t.exit <= 0:
            return None
        start_ms = int(t.ts_close * 1000)
        end_ms = start_ms + _WINDOW_MS
        kl = client.get_kline(t.symbol, _INTERVAL, start_ms=start_ms,
                              end_ms=end_ms, limit=200)
        if not kl:
            return None
        highs: list[float] = []
        lows: list[float] = []
        for row in kl:
            try:
                highs.append(float(row[2]))
                lows.append(float(row[3]))
            except (ValueError, TypeError, IndexError):
                continue
        if not highs or not lows:
            return None
        if t.side == "long":
            return max(0.0, max(highs) - t.exit)
        return max(0.0, t.exit - min(lows))

    return mfe_fn
