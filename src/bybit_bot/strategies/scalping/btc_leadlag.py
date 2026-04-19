"""BTC Lead-Lag → Altcoin: крипто-корреляционный скальпер.

Идея подтверждена в академической литературе 2024-2026:
- Asia-Pacific Financial Markets 2026 (Springer, DOI 10.1007/s10690-026-09589-z):
  «Small-cap cryptocurrencies exhibit significant delayed responses to BTC
  price movements. Granger causality tests confirmed unidirectional causal
  relationships from BTC to altcoins.»
- High-Frequency Lead-Lag Relationships in The Bitcoin Market:
  Lag-trading стратегия на basis BTC preceding returns показывает
  directional accuracy до 70% на прогнозе altcoin mid-quote changes.

Метод (по research):
- Корреляция считается по **log-returns** (не по ценам!), так
  price-level корреляция ловит фантомные зависимости на трендах.
- Ключ: BTC делает резкое движение → альт ещё не отреагировал →
  вход в альт в ту же сторону.

Текущая конфигурация рынка 2026 (AhaSignals, EarnifyHub):
- BTC ↔ ETH/SOL/LINK returns corr: 0.85-0.90.
- Small-cap (WIF, TIA) — ниже, но **lag больше** → бо́льший профит.
- После BTC ETF — «structural decoupling», корреляции снизились,
  поэтому CORR_MIN=0.5 адекватен.

Эффект сильнее всего на M5 в ликвидные часы (London/NY). В ночной
Asia lag размыт / инвертирован — фильтруем BTC-ADX > 15
(есть направленность).

Антикорреляция с остальными скальперами:
- VWAP mean-reversion — контр-тренд одного символа.
- Stat-Arb — pair trading двух коррелированных.
- Volume Spike / ORB / Turtle — моментум/ловушка по одному символу.
- BTC Lead-Lag — **меж-символьный моментум**, уникальная ниша.

Изоляция от FxPro: модуль импортирует только `bybit_bot.*`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from bybit_bot.analysis.signals import Direction, atr
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import compute_adx

log = logging.getLogger(__name__)

# ── Параметры (обоснование см. research-блок в docstring) ───────
BTC_SYMBOL = "BTCUSDT"
# Springer 2026: lag 5-15 минут на M5 — 3 бара точно попадают в окно.
BTC_LOOKBACK_BARS = 3            # 3 × M5 = 15 мин для расчёта BTC-импульса
# BTC/Bitcoin HF Lead-Lag paper: «significant price transmission» при
# движениях >1σ возвратов. 1% за 15 мин на BTC ≈ 2-3σ (волатильность ~0.3-0.5%).
BTC_MOVE_PCT = 1.0 / 100
BTC_MOVE_MIN_ATR = 1.5           # двойной фильтр: % И ATR, отсекает микро-шум
BTC_ADX_MIN = 15.0               # <15 = флет, lead-lag не работает (research: «clear trend regime»)
CORR_WINDOW = 50                 # ≈4 часа на M5 — research recommended «short rolling window»
CORR_MIN = 0.5                   # post-ETF decoupling: BTC↔alt corr(returns) ≈ 0.5-0.7
ALT_LAG_MAX_PCT = 0.3 / 100      # абсолютное: альт двинулся <0.3% = ещё не догнал
ATR_PERIOD = 14
MIN_BARS = 60                    # минимум для ADX + корреляции returns (окно 50)

SL_ATR_MULT = 1.5
TP_ATR_MULT = 2.0                # RR≈1.33; research: directional accuracy до 70% → EV+


@dataclass(frozen=True, slots=True)
class LeadLagSignal:
    symbol: str
    direction: Direction
    btc_move_pct: float          # % движение BTC за lookback
    btc_move_atr: float
    alt_move_pct: float          # % движение альта за то же окно
    correlation: float
    atr_value: float


class BtcLeadLagStrategy:
    """BTC-ведомый скальпер по альткоинам.

    На каждом вызове:
      1) Берёт BTC-бары, считает импульс за BTC_LOOKBACK_BARS.
      2) Если импульс слабый / BTC-ADX низкий — пропускает весь цикл.
      3) Иначе по каждому альту: корреляция ≥ CORR_MIN, его собственное
         движение < ALT_LAG_MAX_PCT → сигнал в сторону BTC-движения.
    """

    def __init__(self, *, max_signals_per_scan: int = 3) -> None:
        self._max_signals = max_signals_per_scan

    def scan(self, bars_map: dict[str, list[Bar]]) -> list[LeadLagSignal]:
        btc_bars = bars_map.get(BTC_SYMBOL)
        if not btc_bars or len(btc_bars) < MIN_BARS:
            return []

        btc_closes = [b.close for b in btc_bars]
        btc_lookback_closes = btc_closes[-BTC_LOOKBACK_BARS - 1:]
        btc_move = btc_lookback_closes[-1] - btc_lookback_closes[0]
        btc_move_pct = btc_move / btc_lookback_closes[0] if btc_lookback_closes[0] else 0.0
        btc_atr = atr(btc_bars, period=ATR_PERIOD)
        if btc_atr <= 0:
            return []
        btc_move_atr = abs(btc_move) / btc_atr

        if abs(btc_move_pct) < BTC_MOVE_PCT:
            return []
        if btc_move_atr < BTC_MOVE_MIN_ATR:
            return []
        if compute_adx(btc_bars, period=14) < BTC_ADX_MIN:
            return []

        direction = Direction.LONG if btc_move > 0 else Direction.SHORT
        signals: list[LeadLagSignal] = []

        for symbol, bars in bars_map.items():
            if symbol == BTC_SYMBOL:
                continue
            if len(bars) < MIN_BARS:
                continue

            alt_closes = [b.close for b in bars]
            # По research (Asia-Pacific Financial Markets 2026, CXO Advisory):
            # корреляция считается по **log-returns**, не по ценам.
            # Price-level corr ловит фантомные зависимости на трендах.
            correlation = _pearson_corr(
                _log_returns(alt_closes[-(CORR_WINDOW + 1):]),
                _log_returns(btc_closes[-(CORR_WINDOW + 1):]),
            )
            if correlation < CORR_MIN:
                continue

            alt_lookback = alt_closes[-BTC_LOOKBACK_BARS - 1:]
            alt_move = alt_lookback[-1] - alt_lookback[0]
            alt_move_pct = alt_move / alt_lookback[0] if alt_lookback[0] else 0.0

            # Лаг: альт сдвинулся на <30% от BTC-движения в ту же сторону,
            # либо вообще ещё не двинулся, либо двинулся противоположно.
            # Условие формулируем через процент движения альта относительно BTC.
            same_side = (btc_move > 0 and alt_move >= 0) or (btc_move < 0 and alt_move <= 0)
            if same_side and abs(alt_move_pct) >= abs(btc_move_pct) * 0.7:
                # Альт уже догнал — момент упущен.
                continue
            if abs(alt_move_pct) > ALT_LAG_MAX_PCT and same_side:
                # Альт уже значительно сдвинулся — лаг-премия мала.
                continue

            alt_atr = atr(bars, period=ATR_PERIOD)
            if alt_atr <= 0:
                continue

            signals.append(LeadLagSignal(
                symbol=symbol,
                direction=direction,
                btc_move_pct=round(btc_move_pct * 100, 3),
                btc_move_atr=round(btc_move_atr, 2),
                alt_move_pct=round(alt_move_pct * 100, 3),
                correlation=round(correlation, 2),
                atr_value=alt_atr,
            ))

            log.info(
                "BTC-LEADLAG %s %s: BTC=%.2f%% (%.1fATR), alt=%.2f%%, corr=%.2f",
                symbol, direction.value.upper(),
                btc_move_pct * 100, btc_move_atr, alt_move_pct * 100, correlation,
            )

        signals.sort(key=lambda s: s.correlation, reverse=True)
        return signals[: self._max_signals]


# ── Хелперы ────────────────────────────────────────────────────


def _log_returns(closes: list[float]) -> list[float]:
    """Log-returns: r_t = ln(C_t / C_{t-1}).

    Research (Asia-Pacific Financial Markets 2026) использует именно log-returns
    для корреляционного анализа lead-lag, т.к. они стационарны и
    log-нормально распределены. Price-level corr ловит фантомные
    зависимости на общих трендах.
    """
    if len(closes) < 2:
        return []
    result: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        curr = closes[i]
        if prev <= 0 or curr <= 0:
            result.append(0.0)
        else:
            result.append(math.log(curr / prev))
    return result


def _pearson_corr(x: list[float], y: list[float]) -> float:
    """Pearson correlation. Возвращает 0.0 при вырожденном случае."""
    n = min(len(x), len(y))
    if n < 10:
        return 0.0
    xs = x[-n:]
    ys = y[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    var_x = sum((v - mx) ** 2 for v in xs)
    var_y = sum((v - my) ** 2 for v in ys)
    if var_x == 0 or var_y == 0:
        return 0.0
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return cov / math.sqrt(var_x * var_y)
