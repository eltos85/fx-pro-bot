"""Per-symbol funding schedule (Bybit fundingInterval).

Скальп не должен держать позицию через funding settlement: на волатильных
альтах ставка бывает ~1% за интервал (LAB наблюдалась −0.967%), что кратно
превышает наш R≈0.44% — одно неучтённое списание перекроет серию удачных
сделок. Раньше график был зашит под 8ч (00/08/16 UTC), но по факту ALLO и
LAB — 4ч (00/04/08/12/16/20 UTC), т.е. половину их списаний мы не избегали.

Источник интервала — официальная Bybit instruments-info, поле ``fundingInterval``
(в минутах: 480=8ч, 240=4ч, 60=1ч):
https://bybit-exchange.github.io/docs/v5/market/instrument
Settlements в UTC кратны интервалу от полуночи (00:00 + k·interval).
"""
from __future__ import annotations

import logging

log = logging.getLogger("scalp_bot.funding")

# Фолбэк при неизвестном символе = 8ч (старое поведение, не регрессируем).
DEFAULT_INTERVAL_MIN = 480


def sec_to_next_funding(now: float, interval_min: int = DEFAULT_INTERVAL_MIN) -> float:
    """Секунд до ближайшего funding settlement для интервала ``interval_min`` (мин).

    Settlements кратны UTC-полуночи: для 4ч это 00/04/08/12/16/20, для 1ч —
    каждый час. 86400 делится на 3600/14400/28800 нацело, поэтому метки всегда
    выравнены на полночь без «хвоста»."""
    iv = max(1, interval_min) * 60.0
    sod = now % 86400.0
    nxt = (int(sod // iv) + 1) * iv
    return nxt - sod


class FundingSchedule:
    """Кэш fundingInterval по символу + проверка окна перед списанием."""

    def __init__(self) -> None:
        self._interval: dict[str, int] = {}

    def refresh(self, client, symbols: list[str]) -> None:
        """Подтянуть fundingInterval по каждому символу (instruments-info).
        Интервал статичен per-instrument — зовём на старте и при ротации."""
        for sym in symbols:
            iv = client.get_funding_interval(sym)
            if iv:
                self._interval[sym] = iv
        log.info("funding-интервалы (мин): %s",
                 {s: self._interval.get(s, DEFAULT_INTERVAL_MIN) for s in symbols})

    def interval(self, symbol: str) -> int:
        return self._interval.get(symbol, DEFAULT_INTERVAL_MIN)

    def sec_to_next(self, symbol: str, now: float) -> float:
        return sec_to_next_funding(now, self.interval(symbol))

    def blocked(self, symbol: str, now: float, window_sec: float) -> bool:
        """True → мы в окне ``window_sec`` перед списанием, вход держим."""
        return self.sec_to_next(symbol, now) < window_sec
