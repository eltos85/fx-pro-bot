"""HTF-bias: трендовый фильтр старшего таймфрейма для sweep_fade.

Канон: «без структурного контекста CVD-дивергенция — шум» (chartwhisperer CAP
gates 1–3). Фейд берём ТОЛЬКО в согласии со старшим трендом — «покупай дно в
аптренде, продавай вершину в даунтренде» (Murphy 1999 — EMA200 primary trend;
Asness et al. 2013 «Value & Momentum Everywhere» — mean-reversion работает в
согласии с трендом, а не против). Без фильтра sweep_fade фейдил в вакууме —
вероятная причина низкого WR (аудит v0.9.0).

Реализация: периодически тянем HTF-свечи (1H) и держим EMA(ema_len) на закрытиях
по символу. ``aligned`` — fail-open: нет данных → НЕ блокируем (сбой свечей не
должен глушить торговлю).
"""
from __future__ import annotations

import logging

log = logging.getLogger("scalp_bot.htf")


def compute_ema(closes: list[float], length: int) -> float | None:
    """EMA на закрытиях (closes по ВОЗРАСТАНИЮ времени). None если данных мало.

    Требуем ≥ length свечей: EMA200 на коротком ряду ненадёжна → лучше None
    (fail-open «разрешаем», чем ложный bias на тонкой истории нового листинга)."""
    if length <= 0 or len(closes) < length:
        return None
    k = 2.0 / (length + 1.0)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1.0 - k)
    return ema


def _closes_ascending(kline: list[list]) -> list[float]:
    """Закрытия из Bybit get_kline (DESC, новые сверху) по ВОЗРАСТАНИЮ времени.
    Элемент свечи: [startTime, open, high, low, close, volume, turnover]."""
    out: list[float] = []
    for row in reversed(kline):
        try:
            out.append(float(row[4]))
        except (IndexError, ValueError, TypeError):
            continue
    return out


class HtfTrend:
    """Кэш EMA старшего ТФ по символу + проверка согласия направления сделки."""

    def __init__(self, ema_len: int = 200, interval: str = "60") -> None:
        self.ema_len = ema_len
        self.interval = interval
        self._ema: dict[str, float] = {}

    def refresh(self, client, symbols: list[str]) -> None:
        """Обновить EMA по символам. При сбое одного — сохраняем прошлое значение
        (fail-open), не удаляем (иначе мигнувший REST-сбой снимет фильтр)."""
        for sym in symbols:
            kline = client.get_kline(sym, self.interval, limit=self.ema_len)
            ema = compute_ema(_closes_ascending(kline), self.ema_len)
            if ema is not None and ema > 0:
                self._ema[sym] = ema

    def direction(self, symbol: str, price: float | None) -> str | None:
        """'long' (price>EMA, аптренд) | 'short' (даунтренд) | None (нет данных)."""
        ema = self._ema.get(symbol)
        if ema is None or price is None or price <= 0:
            return None
        return "long" if price > ema else "short"

    def aligned(self, symbol: str, side: str, price: float | None) -> bool:
        """Согласован ли фейд со старшим трендом. Нет данных → True (fail-open)."""
        d = self.direction(symbol, price)
        return d is None or d == side
