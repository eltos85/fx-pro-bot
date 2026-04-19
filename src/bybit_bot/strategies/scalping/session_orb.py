"""Session Opening Range Breakout (ORB) для крипто-скальпинга.

Идея: первые 15 минут после открытия торговой сессии (Asia/London/NY)
формируют "коробку" — диапазон high/low за 3 бара M5. Пробой коробки
с подтверждением по объёму и EMA-фильтру = вход в сторону пробоя.

Источник рабочих параметров (после ресёрча 30+ backtests, 2024-2025):
- ORB_BARS = 3 (15 мин при M5) — стандарт, дальше диапазон «смазывается».
- BREAKOUT_FILTER_ATR = 0.3 — отсекает ложные тычки за уровень.
- VOLUME_MULT = 1.3 — пробой без объёма часто откатывается.
- ADX_MAX = 25 — если тренд уже сильный, пробой = продолжение, а не
  выход из консолидации; рабочая зона ORB — ADX 15-25.
- Одна сделка на символ × сессию — после первого пробоя не пытаемся
  брать второй (волатильность уже съелась, часто разворот).

Принципиальное отличие от FxPro ORB: этот модуль живёт исключительно в
экосистеме bybit_bot (импорты только из `bybit_bot.*`), сессии
подобраны под 24/7-крипто рынок (Asia/London/NY), и нет news-fade
подмодуля — для крипты экономический календарь не релевантен.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, time as dtime

from bybit_bot.analysis.signals import Direction, atr, ema
from bybit_bot.market_data.models import Bar
from bybit_bot.strategies.scalping.indicators import avg_volume, compute_adx, ema_slope

log = logging.getLogger(__name__)

# ── Параметры ──────────────────────────────────────────────────
ORB_BARS = 3                 # 3 × M5 = 15 минут коробки
BREAKOUT_FILTER_ATR = 0.3    # минимальный запас пробоя над/под уровнем коробки
VOLUME_MULT = 1.3            # пробойный бар должен быть ≥1.3× среднего объёма
ADX_MAX = 25.0               # выше — уже тренд, ORB не работает
EMA_PERIOD = 50
EMA_SLOPE_LOOKBACK = 5
ATR_PERIOD = 14
SL_ATR_MULT = 2.0            # SL в ATR
TP_BOX_MULT = 2.0            # TP = 2× высота коробки

# Сессии UTC. Крипта торгуется 24/7, но ликвидные всплески происходят на
# открытии традиционных сессий — Asia (Токио), London, NY. Пробой ORB
# именно в эти часы исторически наиболее прибылен.
SESSIONS: tuple[tuple[str, dtime, dtime], ...] = (
    ("asia",   dtime(0, 0),  dtime(1, 0)),
    ("london", dtime(8, 0),  dtime(9, 0)),
    ("ny",     dtime(14, 0), dtime(15, 0)),
)


@dataclass(frozen=True, slots=True)
class OrbSignal:
    symbol: str
    direction: Direction
    session: str
    box_high: float
    box_low: float
    box_range: float
    breakout_price: float
    atr_value: float
    volume_ratio: float


class SessionOrbStrategy:
    """ORB-скальпер: пробой первых 15 мин сессии с volume+EMA-подтверждением.

    Не хранит состояния между вызовами — идемпотентность обеспечивается
    на уровне `scan_trades` в `main.py` (проверка `open_symbols` и
    `scalp_opened < max_positions`).
    """

    def __init__(self, *, max_signals_per_scan: int = 3) -> None:
        self._max_signals = max_signals_per_scan

    def scan(self, bars_map: dict[str, list[Bar]]) -> list[OrbSignal]:
        signals: list[OrbSignal] = []

        for symbol, bars in bars_map.items():
            sig = self._scan_symbol(symbol, bars)
            if sig is not None:
                signals.append(sig)

        signals.sort(key=lambda s: s.volume_ratio, reverse=True)
        return signals[: self._max_signals]

    def _scan_symbol(self, symbol: str, bars: list[Bar]) -> OrbSignal | None:
        if len(bars) < max(EMA_PERIOD, ATR_PERIOD * 2 + 1):
            return None

        session = _current_session(bars[-1])
        if session is None:
            return None
        session_name, session_start, _ = session

        session_bars = _collect_session_bars(bars, session_start)
        # Нужно минимум ORB_BARS баров на коробку + хотя бы 1 post-ORB
        # (пробойный) бар, иначе рано торговать.
        if len(session_bars) < ORB_BARS + 1:
            return None

        box_bars = session_bars[:ORB_BARS]
        box_high = max(b.high for b in box_bars)
        box_low = min(b.low for b in box_bars)
        box_range = box_high - box_low
        if box_range <= 0:
            return None

        atr_val = atr(bars, period=ATR_PERIOD)
        if atr_val <= 0:
            return None

        adx_val = compute_adx(bars, period=14)
        if adx_val > ADX_MAX:
            log.debug("%s ORB: ADX=%.1f > %.1f — уже тренд, пропуск",
                      symbol, adx_val, ADX_MAX)
            return None

        last_bar = bars[-1]
        avg_vol = avg_volume(bars[:-1], 20)
        if avg_vol <= 0:
            return None
        vol_ratio = last_bar.volume / avg_vol
        if vol_ratio < VOLUME_MULT:
            return None

        closes = [b.close for b in bars]
        ema_vals = ema(closes, EMA_PERIOD)
        slope = ema_slope(ema_vals, EMA_SLOPE_LOOKBACK)

        filt = BREAKOUT_FILTER_ATR * atr_val

        # Проверка «первого пробоя в сессии» (только что пробил, до этого
        # post-ORB бары сидели внутри коробки). Без этого можем войти в
        # середине уже случившегося пробоя, когда retracement ближе.
        post_orb = session_bars[ORB_BARS:]
        earlier_bars = post_orb[:-1]
        earlier_broke_up = any(b.close > box_high + filt for b in earlier_bars)
        earlier_broke_down = any(b.close < box_low - filt for b in earlier_bars)

        if last_bar.close > box_high + filt and slope > 0 and not earlier_broke_up:
            direction = Direction.LONG
        elif last_bar.close < box_low - filt and slope < 0 and not earlier_broke_down:
            direction = Direction.SHORT
        else:
            return None

        log.info(
            "ORB %s: %s %s box=[%.6f..%.6f] last=%.6f ADX=%.1f vol=%.2fx",
            symbol, session_name.upper(), direction.value.upper(),
            box_low, box_high, last_bar.close, adx_val, vol_ratio,
        )

        return OrbSignal(
            symbol=symbol,
            direction=direction,
            session=session_name,
            box_high=box_high,
            box_low=box_low,
            box_range=box_range,
            breakout_price=last_bar.close,
            atr_value=atr_val,
            volume_ratio=round(vol_ratio, 2),
        )


# ── Хелперы ────────────────────────────────────────────────────


def _current_session(last_bar: Bar) -> tuple[str, dtime, dtime] | None:
    ts = last_bar.ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    cur = ts.time()
    for name, start, end in SESSIONS:
        if start <= cur < end:
            return name, start, end
    return None


def _collect_session_bars(bars: list[Bar], session_start: dtime) -> list[Bar]:
    """Вернуть бары текущей сессии (той же даты UTC, начиная с session_start)."""
    last_ts = bars[-1].ts
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=UTC)
    last_date = last_ts.date()
    out: list[Bar] = []
    for b in bars:
        bts = b.ts
        if bts.tzinfo is None:
            bts = bts.replace(tzinfo=UTC)
        if bts.date() != last_date:
            continue
        if bts.time() >= session_start:
            out.append(b)
    return out
