"""Turtle Soup fade — анти-пробойная стратегия Larry Connors.

Идея: ловим **ложный пробой** 20-барного экстремума. Если цена сделала
новый 20-барный low/high, но через 1-4 бара вернулась обратно внутрь
диапазона — это stop-hunt / liquidity grab. Входим **против пробоя**:
long после провала и возврата снизу, short после всплеска и возврата
сверху.

─── Research basis ───────────────────────────────────────────────
Оригинальная стратегия разработана Larry Connors и Linda Bradford Raschke
(«Street Smarts», 1995). Developed to fade the 20-day breakout system
used by Richard Dennis's Turtle traders (отсюда «Turtle Soup» = поедание
Turtle-трейдеров, торгующих пробой).

Параметры из оригинала (Traders.com, Forex Academy, ICT 2024):
- **LOOKBACK = 20 периодов** — «Market must make a 20-period low/high»
  (Connors & Raschke).
- **RECLAIM_WINDOW** — у нас 4 бара (в оригинале right after break):
  адаптировано под M5 (≈20 мин) для крипты с её wick-спайками.
- **SL = 1.5×ATR** — в оригинале «stop-loss placed under the current
  period low». Мы используем ATR-based как более универсальный вариант
  (криптовалютная волатильность сильно меняется во времени).

Дополнительные фильтры у нас (из Enhanced версии, Medium/Sword Red 2024):
- **RSI-экстремум** (<30 long, >70 short) — «Multiple Price Action
  Confirmations» из Turtle Soup Enhanced. Играет роль original правила
  «previous low must have occurred 4 periods earlier» — подтверждает
  «вымывание слабых рук» перед входом.
- **ADX < 30** — отсекает сильный тренд, где sweep = продолжение, а
  не ловушка.

Работает на крипте как mean-reversion после wick-манипуляций whales
(ловля ликвидности вокруг круглых уровней / предыдущих экстремумов).

Принципиальная изоляция от FxPro: модуль импортирует только `bybit_bot.*`.

Антикорреляция с другими скальпинг-стратегиями:
- ORB: пробой + продолжение (тренд).
- Volume Spike: моментум от объёма.
- Turtle Soup: анти-пробой — ловим **разворот** после ложного движения.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bybit_bot.analysis.signals import Direction, atr, rsi
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import compute_adx

log = logging.getLogger(__name__)

# ── Параметры ──────────────────────────────────────────────────
LOOKBACK = 20                 # Окно для определения 20-барного экстремума
BREAK_DEPTH_ATR = 0.3         # Пробой должен быть осязаемым (отсекает wick-тычки)
RECLAIM_WINDOW = 4            # Сколько баров даём цене вернуться в диапазон
RECLAIM_BUFFER_ATR = 0.1      # Возврат должен быть «внутрь» на ATR-буфер
RSI_OVERSOLD = 30.0           # Long: RSI был ниже 30 на пробое вниз
RSI_OVERBOUGHT = 70.0         # Short: RSI был выше 70 на пробое вверх
ADX_MAX = 30.0                # Выше — сильный тренд, sweep не ловушка
ATR_PERIOD = 14
MIN_BARS = 50                 # Минимум для стабильного ADX + RSI

SL_ATR_MULT = 1.5             # SL за внешний экстремум (жёсткий)
TP_ATR_MULT = 2.5             # TP = 2.5 ATR (RR ≈ 1.67)


@dataclass(frozen=True, slots=True)
class TurtleSoupSignal:
    symbol: str
    direction: Direction
    extreme_price: float      # Точка ложного пробоя (max high или min low)
    reclaim_price: float      # Цена возврата внутрь диапазона
    break_depth_atr: float    # Насколько глубоко пробил (в ATR)
    rsi_at_break: float       # RSI в момент пробоя
    atr_value: float


class TurtleSoupStrategy:
    """Turtle Soup scalper.

    Скан идёт по последним `RECLAIM_WINDOW + 1` барам: ищем бар с
    ложным пробоем и проверяем, что один из последующих баров (до
    текущего включительно) вернулся внутрь `LOOKBACK`-экстремума
    с буфером.
    """

    def __init__(self, *, max_signals_per_scan: int = 3) -> None:
        self._max_signals = max_signals_per_scan

    def scan(self, bars_map: dict[str, list[Bar]]) -> list[TurtleSoupSignal]:
        signals: list[TurtleSoupSignal] = []
        for symbol, bars in bars_map.items():
            sig = self._scan_symbol(symbol, bars)
            if sig is not None:
                signals.append(sig)
        signals.sort(key=lambda s: s.break_depth_atr, reverse=True)
        return signals[: self._max_signals]

    def _scan_symbol(self, symbol: str, bars: list[Bar]) -> TurtleSoupSignal | None:
        if len(bars) < MIN_BARS + LOOKBACK + RECLAIM_WINDOW + 1:
            return None

        atr_val = atr(bars, period=ATR_PERIOD)
        if atr_val <= 0:
            return None

        adx_val = compute_adx(bars, period=14)
        if adx_val > ADX_MAX:
            log.debug("%s turtle: ADX=%.1f > %.1f — тренд, sweep = продолжение",
                      symbol, adx_val, ADX_MAX)
            return None

        last = bars[-1]
        break_filter = BREAK_DEPTH_ATR * atr_val
        reclaim_buf = RECLAIM_BUFFER_ATR * atr_val

        # Ищем бар-ловушку в окне последних RECLAIM_WINDOW+1 баров.
        # Сам бар-ловушка не может быть последним (нужно хотя бы 1 бар
        # после него для reclaim).
        trap_zone = bars[-(RECLAIM_WINDOW + 1):-1]
        for trap_idx, trap_bar in enumerate(trap_zone):
            # Глобальный индекс trap_bar
            global_trap_idx = len(bars) - (RECLAIM_WINDOW + 1) + trap_idx

            # История ДО trap_bar (не включая его) — для расчёта LOOKBACK-экстремума
            history = bars[global_trap_idx - LOOKBACK:global_trap_idx]
            if len(history) < LOOKBACK:
                continue

            hist_high = max(b.high for b in history)
            hist_low = min(b.low for b in history)

            # RSI на момент пробоя (closes до trap_bar включительно)
            rsi_at_break = rsi([b.close for b in bars[:global_trap_idx + 1]])

            # Попытка long: ловушка пробила вниз, last вернулся выше hist_low
            if trap_bar.low < hist_low - break_filter and rsi_at_break < RSI_OVERSOLD:
                if last.close > hist_low + reclaim_buf:
                    return TurtleSoupSignal(
                        symbol=symbol,
                        direction=Direction.LONG,
                        extreme_price=trap_bar.low,
                        reclaim_price=last.close,
                        break_depth_atr=round((hist_low - trap_bar.low) / atr_val, 2),
                        rsi_at_break=round(rsi_at_break, 1),
                        atr_value=atr_val,
                    )

            # Попытка short: ловушка пробила вверх, last вернулся ниже hist_high
            if trap_bar.high > hist_high + break_filter and rsi_at_break > RSI_OVERBOUGHT:
                if last.close < hist_high - reclaim_buf:
                    return TurtleSoupSignal(
                        symbol=symbol,
                        direction=Direction.SHORT,
                        extreme_price=trap_bar.high,
                        reclaim_price=last.close,
                        break_depth_atr=round((trap_bar.high - hist_high) / atr_val, 2),
                        rsi_at_break=round(rsi_at_break, 1),
                        atr_value=atr_val,
                    )

        return None
