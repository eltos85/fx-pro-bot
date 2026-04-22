"""Session Opening Range Breakout + News Fade.

ORB: первые 15 минут (3 бара M5) London/NY формируют "коробку".
Пробой коробки с EMA-фильтром и volume-подтверждением = вход.

News Fade: если за 15 мин цена прошла > 2 ATR против EMA — вход
против спайка на откат 50%.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone

from fx_pro_bot.analysis.signals import TrendDirection, _atr, _ema, compute_adx
from fx_pro_bot.config.settings import SCALPING_CRYPTO_ALLOWED, display_name, is_crypto, pip_size
from fx_pro_bot.market_data.models import Bar
from fx_pro_bot.stats.cost_model import estimate_entry_cost
from fx_pro_bot.stats.store import StatsStore
from fx_pro_bot.strategies.scalping.indicators import avg_volume, ema_slope, htf_ema_trend, is_liquid_session, session_range

log = logging.getLogger(__name__)

ORB_BARS = 3
BREAKOUT_FILTER_ATR = 0.3
VOLUME_MULT = 1.3
SL_ATR_MULT = 2.0
TP_RANGE_MULT = 2.0
NEWS_SPIKE_ATR = 2.0
NEWS_SPIKE_ATR_CRYPTO = 3.0
ADX_MAX = 25.0

LONDON_OPEN = time(8, 0)
LONDON_ORB_END = time(8, 15)
LONDON_CLOSE = time(12, 0)
NY_OPEN = time(14, 30)
NY_ORB_END = time(14, 45)
NY_CLOSE = time(17, 0)


@dataclass(frozen=True, slots=True)
class OrbSignal:
    instrument: str
    direction: TrendDirection
    source: str
    box_high: float
    box_low: float
    atr: float
    detail: str


class SessionOrbStrategy:
    """Session ORB + News Fade скальпер."""

    def __init__(
        self,
        store: StatsStore,
        *,
        max_positions: int = 15,
        max_per_instrument: int = 2,
    ) -> None:
        self._store = store
        self._max_positions = max_positions
        self._max_per_instrument = max_per_instrument

    def scan(
        self,
        bars_map: dict[str, list[Bar]],
        prices: dict[str, float],
        events: tuple = (),
    ) -> list[OrbSignal]:
        signals: list[OrbSignal] = []

        for symbol, bars in bars_map.items():
            if len(bars) < 51:
                continue

            if is_crypto(symbol) and symbol not in SCALPING_CRYPTO_ALLOWED:
                continue

            price = prices.get(symbol)
            if price is None or price <= 0:
                continue

            atr = _atr(bars)
            if atr <= 0:
                continue

            adx = compute_adx(bars)
            if adx > ADX_MAX:
                continue

            closes = [b.close for b in bars]
            ema_vals = _ema(closes, 50)
            slope = ema_slope(ema_vals, 5)
            htf_slope = htf_ema_trend(bars)

            orb_sig = self._check_orb(symbol, bars, price, atr, slope, htf_slope)
            if orb_sig:
                signals.append(orb_sig)

            fade_sig = self._check_news_fade(symbol, bars, price, atr, slope, htf_slope)
            if fade_sig:
                signals.append(fade_sig)

        return signals

    def process_signals(
        self,
        signals: list[OrbSignal],
        prices: dict[str, float],
    ) -> int:
        opened = 0
        current = self._store.count_open_positions(strategy="session_orb")

        for sig in signals:
            if current >= self._max_positions:
                break

            instr_count = self._store.count_open_positions(
                strategy="session_orb", instrument=sig.instrument,
            )
            if instr_count >= self._max_per_instrument:
                continue

            price = prices.get(sig.instrument)
            if price is None or price <= 0:
                continue

            sl_dist = SL_ATR_MULT * sig.atr
            if is_crypto(sig.instrument):
                from fx_pro_bot.strategies.monitor import CRYPTO_SCALP_SL_MIN_PCT
                sl_dist = max(sl_dist, price * CRYPTO_SCALP_SL_MIN_PCT)
            if sig.direction == TrendDirection.LONG:
                sl = price - sl_dist
            else:
                sl = price + sl_dist

            pid = self._store.open_position(
                strategy="session_orb",
                source=sig.source,
                instrument=sig.instrument,
                direction=sig.direction.value,
                entry_price=price,
                stop_loss_price=sl,
            )

            ps = pip_size(sig.instrument)
            cost = estimate_entry_cost(sig.instrument, sig.source, sig.atr, ps)
            self._store.set_estimated_cost(pid, cost.round_trip_pips)

            log.info(
                "  ORB OPEN: %s %s @ %.5f (%s, box=[%.5f..%.5f], SL=%.5f)",
                display_name(sig.instrument),
                sig.direction.value.upper(),
                price, sig.detail, sig.box_high, sig.box_low, sl,
            )
            opened += 1
            current += 1

        return opened

    def _check_orb(
        self,
        symbol: str,
        bars: list[Bar],
        price: float,
        atr: float,
        slope: float,
        htf_slope: float | None = None,
    ) -> OrbSignal | None:
        session_bars = self._get_session_bars(bars)
        if not session_bars or len(session_bars) < ORB_BARS + 1:
            return None

        box_high, box_low = session_range(session_bars, ORB_BARS)
        if box_high == 0 or box_low == 0:
            return None

        post_orb = session_bars[ORB_BARS:]
        if not post_orb:
            return None

        filt = BREAKOUT_FILTER_ATR * atr
        vol = avg_volume(bars, 20)
        cur_vol = bars[-1].volume if bars else 0.0
        if vol > 0 and cur_vol < VOLUME_MULT * vol:
            return None

        if price > box_high + filt and slope > 0:
            if htf_slope is not None and htf_slope < 0:
                return None
            return OrbSignal(
                instrument=symbol,
                direction=TrendDirection.LONG,
                source="orb_breakout",
                box_high=box_high,
                box_low=box_low,
                atr=atr,
                detail=f"breakout above {box_high:.5f}",
            )

        if price < box_low - filt and slope < 0:
            if htf_slope is not None and htf_slope > 0:
                return None
            return OrbSignal(
                instrument=symbol,
                direction=TrendDirection.SHORT,
                source="orb_breakout",
                box_high=box_high,
                box_low=box_low,
                atr=atr,
                detail=f"breakout below {box_low:.5f}",
            )

        return None

    def _check_news_fade(
        self,
        symbol: str,
        bars: list[Bar],
        price: float,
        atr: float,
        slope: float,
        htf_slope: float | None = None,
    ) -> OrbSignal | None:
        if len(bars) < 4:
            return None

        # Liquid session filter — News Fade это mean-reversion, в Asian (23-07 UTC)
        # тонкий рынок не абсорбирует спайк, а продлевает тренд. Диагностика
        # 22.04.2026: 4 входа 23:23-02:50 UTC, WR=0%, NET −$1.40 (см. BUILDLOG).
        # Research: [BIS Triennial FX Survey 2022](https://www.bis.org/publ/rpfx22.htm)
        # пик FX-ликвидности = London-NY overlap; [Dacorogna et al. «High-Frequency
        # Finance» 2001] — spreads и volatility после NY close становятся токсичными
        # для mean-reversion.
        if not is_liquid_session(bars[-1]):
            return None

        recent = bars[-3:]
        move = recent[-1].close - recent[0].open
        abs_move = abs(move)

        spike_threshold = NEWS_SPIKE_ATR_CRYPTO if is_crypto(symbol) else NEWS_SPIKE_ATR
        if abs_move < spike_threshold * atr:
            return None

        spike_up = move > 0
        if spike_up and slope > 0:
            return None
        if not spike_up and slope < 0:
            return None

        box_h = max(b.high for b in recent)
        box_l = min(b.low for b in recent)

        if spike_up:
            if htf_slope is not None and htf_slope > 0:
                log.debug(
                    "%s: SHORT news_fade against H1 trend (htf_slope=%.6f) — proceeding (warning-only)",
                    symbol, htf_slope,
                )
            return OrbSignal(
                instrument=symbol,
                direction=TrendDirection.SHORT,
                source="news_fade",
                box_high=box_h,
                box_low=box_l,
                atr=atr,
                detail=f"fade spike +{abs_move:.5f} (>{NEWS_SPIKE_ATR}xATR)",
            )
        else:
            if htf_slope is not None and htf_slope < 0:
                log.debug(
                    "%s: LONG news_fade against H1 trend (htf_slope=%.6f) — proceeding (warning-only)",
                    symbol, htf_slope,
                )
            return OrbSignal(
                instrument=symbol,
                direction=TrendDirection.LONG,
                source="news_fade",
                box_high=box_h,
                box_low=box_l,
                atr=atr,
                detail=f"fade spike -{abs_move:.5f} (>{NEWS_SPIKE_ATR}xATR)",
            )

    @staticmethod
    def _get_session_bars(bars: list[Bar]) -> list[Bar]:
        """Найти бары текущей торговой сессии (London или NY)."""
        if not bars:
            return []

        last_ts = bars[-1].ts
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        cur_time = last_ts.time()

        if LONDON_OPEN <= cur_time <= time(12, 0):
            session_start = LONDON_OPEN
        elif NY_OPEN <= cur_time <= time(17, 0):
            session_start = NY_OPEN
        else:
            return []

        session_bars = [
            b for b in bars
            if b.ts.time() >= session_start and b.ts.date() == last_ts.date()
        ]
        return session_bars
