"""[DEPRECATED V1] Funding Rate Scalp — стратегия на перекосе funding rate.

Отключена в V2 (13.04.2026). Заменена на EmaTrendStrategy.

Bybit perpetual контракты имеют funding каждые 8 часов (00:00, 08:00, 16:00 UTC).
Когда funding rate сильно отклоняется:
- rate > threshold → рынок перегружен лонгами → short перед funding
- rate < -threshold → рынок перегружен шортами → long перед funding

Вход: за ENTRY_MINUTES_BEFORE минут до funding.
Выход: сразу после funding или по SL/TP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from bybit_bot.analysis.signals import Direction, atr
from bybit_bot.market_data.models import Bar
from bybit_bot.trading.client import BybitClient

log = logging.getLogger(__name__)

# Средний funding rate BTC ≈ 0.005%, ETH ≈ 0.01% (Bybit 7-day avg).
# Порог 0.03% = ~6x от среднего BTC — значимый перекос.
# Источник: CoinPerps, KangaAnalytics (2025 live data).
FUNDING_RATE_THRESHOLD = 0.0003  # 0.03%
FUNDING_RATE_STRONG = 0.0008  # 0.08% — сильный перекос
ENTRY_MINUTES_BEFORE = 30
# Из документации Bybit: не входить за 5 секунд до/после funding timestamp.
FUNDING_BUFFER_SECONDS = 10
SL_ATR_MULT = 1.5
TP_ATR_MULT = 1.0

# Bybit funding: 00:00, 08:00, 16:00 UTC (стандарт для USDT perp).
FUNDING_HOURS_UTC = (0, 8, 16)


@dataclass(frozen=True, slots=True)
class FundingSignal:
    symbol: str
    direction: Direction
    funding_rate: float
    next_funding_time: str
    strength: float  # 0..1
    atr_value: float
    entry_price: float


class FundingScalpStrategy:
    """Скальпинг на funding rate перекосах."""

    def __init__(self, client: BybitClient | None = None) -> None:
        self._client = client

    def scan(
        self,
        symbols: tuple[str, ...],
        bars_map: dict[str, list[Bar]],
    ) -> list[FundingSignal]:
        if self._client is None:
            return []

        if not self._is_near_funding():
            return []

        signals: list[FundingSignal] = []

        for symbol in symbols:
            bars = bars_map.get(symbol, [])
            if len(bars) < 20:
                continue

            try:
                ticker = self._client.get_tickers(symbol)
            except Exception:
                continue

            rate_str = ticker.get("fundingRate", "0")
            rate = float(rate_str)
            next_time = ticker.get("nextFundingTime", "")

            if abs(rate) < FUNDING_RATE_THRESHOLD:
                continue

            atr_val = atr(bars)
            if atr_val <= 0:
                continue

            if rate > FUNDING_RATE_THRESHOLD:
                direction = Direction.SHORT
            else:
                direction = Direction.LONG

            strength = min(abs(rate) / FUNDING_RATE_STRONG, 1.0)

            signals.append(FundingSignal(
                symbol=symbol,
                direction=direction,
                funding_rate=rate,
                next_funding_time=next_time,
                strength=round(strength, 2),
                atr_value=atr_val,
                entry_price=bars[-1].close,
            ))

            log.info(
                "FUNDING: %s rate=%.4f%% → %s (сила %.0f%%)",
                symbol, rate * 100, direction.value.upper(), strength * 100,
            )

        signals.sort(key=lambda s: abs(s.funding_rate), reverse=True)
        return signals

    @staticmethod
    def _is_near_funding() -> bool:
        """Проверить, что до ближайшего funding осталось меньше ENTRY_MINUTES_BEFORE минут."""
        now = datetime.now(tz=UTC)
        current_hour = now.hour
        current_min = now.minute

        for fh in FUNDING_HOURS_UTC:
            diff_hours = (fh - current_hour) % 24
            if diff_hours == 0:
                diff_min = -current_min
            else:
                diff_min = diff_hours * 60 - current_min

            if 0 < diff_min <= ENTRY_MINUTES_BEFORE:
                return True

        return False

    @staticmethod
    def should_exit_after_funding() -> bool:
        """Проверить, что funding только что прошёл (в пределах 5 минут)."""
        now = datetime.now(tz=UTC)
        for fh in FUNDING_HOURS_UTC:
            if now.hour == fh and now.minute < 5:
                return True
        return False
