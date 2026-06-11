"""Ключевые уровни ликвидности (PDH/PDL + дневные экстремумы) — sweep_fade_canon.

─── Research basis ───
Канон liquidity-sweep фейда (CAP/chartwhisperer order-flow 2026) фейдит свип
ЗНАЧИМОГО уровня — места, где физически скапливаются стопы: previous day
high/low (PDH/PDL), session high/low, equal highs/lows. Osler 2003 (NY Fed,
«Currency Orders and Exchange Rate Dynamics»): стоп- и TP-ордера сильно
кластеризуются на видимых уровнях — пробой такого уровня триггерит каскад
стопов, который и даёт absorption-разворот. Базовый sweep_fade фейдит
экстремум 3-минутного окна — ликвидности там почти нет (главное упрощение
канона; live-разрыв WR 35% vs канонные 60%+).

Реализация: из 15m-клинов (тот же REST get_kline, что HTF-фильтр) держим по
символу:
- pdh / pdl   — high/low ПРЕДЫДУЩЕГО UTC-дня (статичны весь день);
- day_high / day_low — экстремумы ТЕКУЩЕГО UTC-дня по ЗАКРЫТЫМ барам
  (формирующийся бар исключён — уровень должен существовать ДО свипа,
  иначе детектор «свипал бы» уровень, который сам же и создал).

``swept_key_level(symbol, side, swept)`` — гейт взвода детектора: True если
свип-экстремум took out (пробил) хотя бы один уровень: long → swept ≤ уровню
поддержки (pdl/day_low), short → swept ≥ уровню сопротивления (pdh/day_high).
Fail-closed: нет данных по символу → False (канон-страта без уровней не
торгует — QuantConnect «refuse to trade until indicator ready», как HTF-гейт
v0.18.2).
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("scalp_bot.levels")

# 15m-баров в сутках = 96; +96 на предыдущий день + запас на стык. Bybit
# get_kline limit ≤ 1000 (https://bybit-exchange.github.io/docs/v5/market/kline).
_KLINE_LIMIT = 200
_DAY_SEC = 86_400


def day_levels(kline: list[list], now: float) -> dict | None:
    """(pdh, pdl, day_high, day_low) из Bybit get_kline 15m (DESC, новые сверху).

    Элемент свечи: [startTime(ms), open, high, low, close, volume, turnover].
    Текущий день — только ЗАКРЫТЫЕ бары (start + 15м ≤ now). Если текущий день
    ещё без закрытых баров (первые минуты суток) — day_high/day_low = None.
    Нет полного покрытия предыдущего дня → None (уровни ненадёжны).
    """
    day_start = now - (now % _DAY_SEC)
    prev_start = day_start - _DAY_SEC
    prev_hi: float | None = None
    prev_lo: float | None = None
    cur_hi: float | None = None
    cur_lo: float | None = None
    oldest_ts: float | None = None
    for row in kline or []:
        try:
            ts = float(row[0]) / 1000.0
            hi = float(row[2])
            lo = float(row[3])
        except (IndexError, TypeError, ValueError):
            continue
        oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)
        if prev_start <= ts < day_start:
            prev_hi = hi if prev_hi is None else max(prev_hi, hi)
            prev_lo = lo if prev_lo is None else min(prev_lo, lo)
        elif ts >= day_start and ts + 900.0 <= now:  # закрытый 15m-бар сегодня
            cur_hi = hi if cur_hi is None else max(cur_hi, hi)
            cur_lo = lo if cur_lo is None else min(cur_lo, lo)
    # требуем, чтобы история доставала до начала предыдущего дня — иначе
    # PDH/PDL посчитаны по обрезку и врут (fail-closed)
    if prev_hi is None or prev_lo is None or oldest_ts is None \
            or oldest_ts > prev_start + 900.0:
        return None
    return {"pdh": prev_hi, "pdl": prev_lo,
            "day_high": cur_hi, "day_low": cur_lo}


class KeyLevels:
    """Кэш ключевых уровней по символу (refresh из get_kline 15m)."""

    def __init__(self, interval: str = "15") -> None:
        self.interval = interval
        self._levels: dict[str, dict] = {}

    def refresh(self, client, symbols: list[str], now: float | None = None) -> None:
        """Обновить уровни. При сбое REST по символу — keep-last (транзиентный
        хиккап не снимает уровни), как HtfTrend.refresh."""
        ts = time.time() if now is None else now
        for sym in symbols:
            try:
                kline = client.get_kline(sym, self.interval, limit=_KLINE_LIMIT)
            except Exception:
                log.exception("levels get_kline %s failed", sym)
                continue
            lv = day_levels(kline, ts)
            if lv is not None:
                self._levels[sym] = lv

    def has_data(self, symbol: str) -> bool:
        return symbol in self._levels

    def levels(self, symbol: str) -> dict | None:
        return self._levels.get(symbol)

    def swept_key_level(self, symbol: str, side: str, swept: float) -> str | None:
        """Имя ключевого уровня, который took out свип-экстремум, или None.

        long  (фейд свипа НИЗОВ):  swept ≤ pdl / day_low;
        short (фейд свипа ВЕРХОВ): swept ≥ pdh / day_high.
        Нет данных → None (fail-closed: канон-страта не торгует без уровней).
        """
        lv = self._levels.get(symbol)
        if lv is None:
            return None
        if side == "long":
            checks = (("day_low", lv.get("day_low")), ("pdl", lv.get("pdl")))
            for name, level in checks:
                if level is not None and swept <= level:
                    return name
            return None
        checks = (("day_high", lv.get("day_high")), ("pdh", lv.get("pdh")))
        for name, level in checks:
            if level is not None and swept >= level:
                return name
        return None
