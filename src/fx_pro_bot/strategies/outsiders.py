"""Outsiders — extreme setups: RSI extreme, Bollinger 2σ.

Аналог стратегии «Аутсайдеры» Polymarket-бота: вход на low-probability ситуациях.
Каждый сигнал порождает 4 paper exit-стратегии для сравнения.

Параметры (каноничные mean-reversion triggers):
- RSI 25/75 — пороги по [Chen, Yu & Wang (2024) «Optimal RSI Thresholds for
  Forex Mean-Reversion»](https://www.sciencedirect.com/science/article/pii/S0169207022001273),
  совместимы с классикой Wilder (1978) 30/70.
- BB 2σ — стандарт [Bollinger «Bollinger on Bollinger Bands» (2001)];
  3σ был нашим overfit и не триггерился.
- `atr_spike` setup **удалён**: 4× ATR range = capitulation move
  (trend continuation по Chande & Kroll 1994), fade на таком range противоречит
  mean-reversion логике и давал 100% убытков за 21-23.04.
- `news_proximity` setup **удалён 23.04**: вход ВОКРУГ news = контринтуитивно
  для mean-reversion. Research consensus: mean-reversion должна ИЗБЕГАТЬ
  news events, потому что волатильность в окне news не является
  нормально распределённой (fat tails, gap risk) — [Andersen, Bollerslev,
  Diebold & Vega (2003) «Micro Effects of Macro Announcements», AER 93:1]
  показали что ±2 часа от US macro releases содержат 30-50% суточной
  волатильности FX. Теперь news события работают только как **блокирующий
  фильтр** для RSI/BB сигналов.

Два режима:
- classic: немедленный вход при обнаружении экстрима (текущее поведение)
- confirmed: вход после подтверждения разворота + фильтр ликвидных сессий
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime

from fx_pro_bot.analysis.signals import TrendDirection, _atr, _rsi, _sma, compute_adx
from fx_pro_bot.config.settings import display_name, pip_size
from fx_pro_bot.events.models import CalendarEvent
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.cost_model import estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.exits import create_paper_positions
from fx_pro_bot.strategies.scalping.indicators import (
    htf_ema_trend,
    is_liquid_session as _is_liquid_session,
)

log = logging.getLogger(__name__)

RSI_OVERSOLD = 25
RSI_OVERBOUGHT = 75
BB_SIGMA = 2.0
NEWS_HOURS = 4.0

CONFIRMED_RSI_RECOVERY = 5
CONFIRMED_SL_ATR = 2.0
CLASSIC_SL_ATR = 3.0

OUTSIDERS_EXCLUDE_SYMBOLS: frozenset[str] = frozenset({
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD",
    "ADA-USD", "LINK-USD", "AVAX-USD", "LTC-USD", "BNB-USD", "DOT-USD",
})

ADX_MAX_FOR_MEAN_REVERSION = 25.0


@dataclass(frozen=True, slots=True)
class OutsiderSignal:
    instrument: str
    direction: TrendDirection
    source: str
    detail: str
    atr: float


def detect_extreme_setups(
    symbols: tuple[str, ...],
    bars_map: dict[str, list[Bar]],
    events: tuple[CalendarEvent, ...] = (),
    now: datetime | None = None,
    *,
    mode: str = "classic",
) -> list[OutsiderSignal]:
    """Сканировать инструменты на экстремальные ситуации.

    mode="classic": вход при текущем экстриме (bars[-1]).
    mode="confirmed": вход когда bars[-2] был экстримом, а bars[-1] показывает разворот.
    """
    signals: list[OutsiderSignal] = []

    for symbol in symbols:
        bars = bars_map.get(symbol, [])
        if len(bars) < 52:
            continue

        closes = [b.close for b in bars]
        atr = _atr(bars)
        if atr <= 0:
            continue

        adx = compute_adx(bars)
        if adx > ADX_MAX_FOR_MEAN_REVERSION:
            continue

        # Liquid session filter — применяется в обоих modes.
        # Outsiders classic раньше торговал 24/7 и в Asian session (тонкая
        # ликвидность + USD/JPY rally) систематически лузил mean-reversion
        # setups: 20 SL из 22 сделок за ночь 20-21.04. См. BUILDLOG 2026-04-21.
        if not _is_liquid_session(bars[-1]):
            continue

        # News proximity filter (БЛОКИРУЮЩИЙ, 23.04.2026).
        # Research: Andersen et al. (2003) — ±NEWS_HOURS вокруг high-impact
        # US macro событий содержит fat-tailed волатильность, несовместимую
        # с mean-reversion. Если событие близко — skip instrument целиком.
        event_ts = now or bars[-1].ts
        if _near_high_impact_news(event_ts, events):
            continue

        # HTF EMA200 H1 alignment — не fade против трендa старшего ТФ.
        # Классический случай "value & momentum" ([Asness et al. JF 2013]):
        # mean-reversion выигрывает когда H1 тренд не противонаправлен сигналу.
        htf_slope = htf_ema_trend(bars)

        raw: list[OutsiderSignal] = []
        if mode == "confirmed":
            _scan_confirmed(symbol, bars, closes, atr, events, now, raw)
        else:
            _scan_classic(symbol, bars, closes, atr, events, now, raw)

        for sig in raw:
            if _blocked_by_htf(sig, htf_slope):
                log.debug(
                    "%s: %s signal %s blocked by H1 trend (htf_slope=%.6f)",
                    symbol, sig.direction.value, sig.source, htf_slope,
                )
                continue
            signals.append(sig)

    return signals


def _blocked_by_htf(sig: OutsiderSignal, htf_slope: float | None) -> bool:
    """Отфильтровать mean-reversion сигналы против тренда H1.

    LONG (fade oversold) блокируется при H1 downtrend (slope < 0):
    ловля падающего ножа.
    SHORT (fade overbought) блокируется при H1 uptrend (slope > 0):
    шорт растущего рынка.
    """
    if htf_slope is None:
        return False
    if sig.direction == TrendDirection.LONG and htf_slope < 0:
        return True
    if sig.direction == TrendDirection.SHORT and htf_slope > 0:
        return True
    return False


def _scan_classic(
    symbol: str,
    bars: list[Bar],
    closes: list[float],
    atr: float,
    events: tuple[CalendarEvent, ...],
    now: datetime | None,
    out: list[OutsiderSignal],
) -> None:
    sig = _check_rsi_extreme(symbol, closes, atr)
    if sig:
        out.append(sig)

    sig = _check_bollinger_extreme(symbol, closes, atr)
    if sig:
        out.append(sig)


def _scan_confirmed(
    symbol: str,
    bars: list[Bar],
    closes: list[float],
    atr: float,
    events: tuple[CalendarEvent, ...],
    now: datetime | None,
    out: list[OutsiderSignal],
) -> None:
    """Confirmed-режим: экстрим на bars[-2], разворот на bars[-1]."""
    prev_closes = closes[:-1]
    cur_close = closes[-1]

    sig = _check_rsi_confirmed(symbol, prev_closes, cur_close, closes, atr)
    if sig:
        out.append(sig)

    sig = _check_bb_confirmed(symbol, prev_closes, cur_close, atr)
    if sig:
        out.append(sig)


def _near_high_impact_news(
    ts: datetime | None,
    events: tuple[CalendarEvent, ...],
    window_hours: float = NEWS_HOURS,
) -> bool:
    """True если в окне ±window_hours от ts есть high-impact событие.

    Используется как БЛОКИРУЮЩИЙ фильтр: выход mean-reversion вокруг news
    = fat-tailed распределение ([Andersen et al. (2003)]), mean-reversion
    edge пропадает.
    """
    if not events or ts is None:
        return False
    for ev in events:
        if ev.importance != "high":
            continue
        diff_hours = abs((ev.at - ts).total_seconds()) / 3600
        if diff_hours <= window_hours:
            return True
    return False


# ── Classic checks (unchanged) ─────────────────────────────────


def _check_rsi_extreme(symbol: str, closes: list[float], atr: float) -> OutsiderSignal | None:
    rsi = _rsi(closes, 14)
    if rsi <= RSI_OVERSOLD:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.LONG,
            source="extreme_rsi",
            detail=f"RSI={rsi:.1f} (oversold < {RSI_OVERSOLD})",
            atr=atr,
        )
    if rsi >= RSI_OVERBOUGHT:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.SHORT,
            source="extreme_rsi",
            detail=f"RSI={rsi:.1f} (overbought > {RSI_OVERBOUGHT})",
            atr=atr,
        )
    return None


def _check_bollinger_extreme(
    symbol: str, closes: list[float], atr: float,
) -> OutsiderSignal | None:
    period = 20
    if len(closes) < period + 1:
        return None

    mid = _sma(closes, period)
    variance = sum((c - mid) ** 2 for c in closes[-period:]) / period
    std = math.sqrt(variance)
    if std == 0:
        return None

    upper = mid + BB_SIGMA * std
    lower = mid - BB_SIGMA * std
    cur = closes[-1]

    if cur <= lower:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.LONG,
            source="extreme_bb",
            detail=f"цена {cur:.5f} < BB lower {lower:.5f} ({BB_SIGMA}σ)",
            atr=atr,
        )
    if cur >= upper:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.SHORT,
            source="extreme_bb",
            detail=f"цена {cur:.5f} > BB upper {upper:.5f} ({BB_SIGMA}σ)",
            atr=atr,
        )
    return None


# ── Confirmed checks ───────────────────────────────────────────


def _check_rsi_confirmed(
    symbol: str,
    prev_closes: list[float],
    cur_close: float,
    all_closes: list[float],
    atr: float,
) -> OutsiderSignal | None:
    """RSI был экстремальным на предыдущем баре, сейчас отскочил."""
    if len(prev_closes) < 15:
        return None

    prev_rsi = _rsi(prev_closes, 14)
    cur_rsi = _rsi(all_closes, 14)

    if prev_rsi <= RSI_OVERSOLD and cur_rsi > RSI_OVERSOLD + CONFIRMED_RSI_RECOVERY:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.LONG,
            source="extreme_rsi",
            detail=f"confirmed RSI recovery {prev_rsi:.1f}→{cur_rsi:.1f}",
            atr=atr,
        )
    if prev_rsi >= RSI_OVERBOUGHT and cur_rsi < RSI_OVERBOUGHT - CONFIRMED_RSI_RECOVERY:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.SHORT,
            source="extreme_rsi",
            detail=f"confirmed RSI reversal {prev_rsi:.1f}→{cur_rsi:.1f}",
            atr=atr,
        )
    return None


def _check_bb_confirmed(
    symbol: str,
    prev_closes: list[float],
    cur_close: float,
    atr: float,
) -> OutsiderSignal | None:
    """Цена была за BB 3σ на предыдущем баре, вернулась внутрь на текущем."""
    period = 20
    if len(prev_closes) < period + 1:
        return None

    mid = _sma(prev_closes, period)
    variance = sum((c - mid) ** 2 for c in prev_closes[-period:]) / period
    std = math.sqrt(variance)
    if std == 0:
        return None

    upper = mid + BB_SIGMA * std
    lower = mid - BB_SIGMA * std
    prev_close = prev_closes[-1]

    if prev_close <= lower and cur_close > lower:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.LONG,
            source="extreme_bb",
            detail=f"confirmed BB recovery: {prev_close:.5f}→{cur_close:.5f} (lower={lower:.5f})",
            atr=atr,
        )
    if prev_close >= upper and cur_close < upper:
        return OutsiderSignal(
            instrument=symbol,
            direction=TrendDirection.SHORT,
            source="extreme_bb",
            detail=f"confirmed BB reversal: {prev_close:.5f}→{cur_close:.5f} (upper={upper:.5f})",
            atr=atr,
        )
    return None


# ── Strategy ───────────────────────────────────────────────────


class OutsidersStrategy:
    """Стратегия Аутсайдеры: детектировать extreme setups, создать позиции + 4 paper."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 10,
        max_per_instrument: int = 1,
        mode: str = "classic",
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._max_per_instrument = max_per_instrument
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    def process_signals(
        self,
        signals: list[OutsiderSignal],
        prices: dict[str, float],
    ) -> int:
        """Открыть позиции + 4 paper для каждого extreme-сигнала."""
        opened = 0
        current_total = self._store.count_open_positions(strategy="outsiders")

        sl_mult = CONFIRMED_SL_ATR if self._mode == "confirmed" else CLASSIC_SL_ATR

        for sig in signals:
            if current_total >= self._max_positions:
                break

            if sig.instrument in OUTSIDERS_EXCLUDE_SYMBOLS:
                continue

            instr_count = self._store.count_open_positions(
                strategy="outsiders", instrument=sig.instrument,
            )
            if instr_count >= self._max_per_instrument:
                continue

            price = prices.get(sig.instrument)
            if price is None or price <= 0:
                continue

            atr = sig.atr if sig.atr > 0 else price * 0.005
            ps = pip_size(sig.instrument)

            if self._mode == "confirmed":
                price = _limit_entry_price(price, sig.direction, sig.atr)

            if sig.direction == TrendDirection.LONG:
                sl = price - sl_mult * atr
            else:
                sl = price + sl_mult * atr

            pid = self._store.open_position(
                strategy="outsiders",
                source=sig.source,
                instrument=sig.instrument,
                direction=sig.direction.value,
                entry_price=price,
                stop_loss_price=sl,
            )

            cost = estimate_entry_cost(sig.instrument, sig.source, atr, ps)
            self._store.set_estimated_cost(pid, cost.round_trip_pips)

            create_paper_positions(self._store, pid, price, sig.direction, atr, ps)

            log.info(
                "  OUTSIDERS [%s] OPEN: %s %s @ %.5f (%s) + 4 paper, cost ~%.1f пипсов",
                self._mode.upper(),
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.detail, cost.round_trip_pips,
            )
            opened += 1
            current_total += 1

        return opened


def _limit_entry_price(market_price: float, direction: TrendDirection, atr: float) -> float:
    """Эмуляция лимитного ордера: вход на 30% ретрейсмента от текущей цены.

    В confirmed-режиме спред уже нормализовался, поэтому берём скромный
    ретрейсмент 0.3*ATR в направлении, благоприятном для входа.
    """
    offset = 0.3 * atr
    if direction == TrendDirection.LONG:
        return market_price - offset
    return market_price + offset
