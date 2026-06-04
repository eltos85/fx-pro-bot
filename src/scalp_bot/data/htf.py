"""HTF-bias: трендовый фильтр старшего таймфрейма для sweep_fade.

Канон: «без структурного контекста CVD-дивергенция — шум» (chartwhisperer CAP
gates 1–3). Фейд берём ТОЛЬКО в согласии со старшим трендом — «покупай дно в
аптренде, продавай вершину в даунтренде» (Murphy 1999 — EMA200 primary trend;
Asness et al. 2013 «Value & Momentum Everywhere» — mean-reversion работает в
согласии с трендом, а не против). Без фильтра sweep_fade фейдил в вакууме —
вероятная причина низкого WR (аудит v0.9.0).

Реализация: периодически тянем HTF-свечи и держим EMA(ema_len) на закрытиях
по символу. ``aligned`` — fail-open: нет данных → НЕ блокируем (сбой свечей не
должен глушить торговлю).

Контекст-ТФ (v0.16.0): 15m, не 1H. Канон скальпинга ставит трендовый bias на
15m (DYOR Academy «scalping: context 1h/15m», VWAP-pullback guide «EMA200 на
15m для bias», ChartScout 2026 «scalping: 15m context / 5m setup / 1m entry»).
Правило соотношения ТФ 1:4–1:6: вход ~1м → контекст 5–15м (1H в ~60× старше —
слишком медленный, отстаёт от свежих разворотов). A/B на истории (15д, n=6220):
EMA200-15m даёт gross +0.122R/сделку vs +0.087R у 1H (~+40%, 4/6 монет лучше).

Режим-гейт ADX (v0.17.0): EMA даёт НАПРАВЛЕНИЕ тренда, но не его СИЛУ. Канон MR
запрещает фейд в сильный тренд ВНЕ зависимости от направления: «never fade a
one-timeframe trending market — single fastest path to ruin for a MR trader»
(Connors/Raschke «Street Smarts» 1995; Dalton Market Profile). Сила тренда —
ADX(14) по Wilder (1978): ADX<20 диапазон (фейд ОК), ADX≥25 established trend
(фейд запрещён, «трендовый день»). Гейт ADDITIVE поверх EMA (не вместо!): соло-
ADX в прошлом A/B проигрывал (фейдил в обе стороны), а связка EMA-направление +
ADX-режим — рецепт профи. A/B на истории (15д, n=6220→3104): ema+adx@25 даёт
gross +0.140R/сделку vs +0.122R у одного EMA (+15%), net −0.088 vs −0.100;
пороги 30/35 выгоды не дают. Артефакт: data/scalp_adx_gate.txt.
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


def _ohlc_ascending(kline: list[list]) -> tuple[list[float], list[float], list[float]]:
    """(highs, lows, closes) по ВОЗРАСТАНИЮ времени из Bybit get_kline (DESC).
    Элемент свечи: [startTime, open, high, low, close, volume, turnover]."""
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for row in reversed(kline):
        try:
            highs.append(float(row[2]))
            lows.append(float(row[3]))
            closes.append(float(row[4]))
        except (IndexError, ValueError, TypeError):
            continue
    return highs, lows, closes


def compute_adx(highs: list[float], lows: list[float], closes: list[float],
                length: int = 14) -> float | None:
    """Wilder ADX(length) — последнее значение или None если данных мало.

    Сила тренда (не направление): ADX<20 диапазон, ≥25 established trend, >40
    очень сильный. J. Welles Wilder «New Concepts in Technical Trading» (1978).
    Требуем ≥ 2*length+1 свечей: ADX = Wilder-сглаживание DX, прогрев ~2*length.
    Меньше → None (fail-open: не блокируем на тонкой истории)."""
    n = length
    if n <= 0 or len(closes) < 2 * n + 1:
        return None

    def _wilder(x: list[float]) -> list[float]:
        out = [0.0] * len(x)
        if len(x) <= n:
            return out
        s = sum(x[1:n + 1])
        out[n] = s
        for i in range(n + 1, len(x)):
            s = s - s / n + x[i]
            out[i] = s
        return out

    tr = [0.0]
    pdm = [0.0]
    ndm = [0.0]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
    atr = _wilder(tr)
    pdm_s = _wilder(pdm)
    ndm_s = _wilder(ndm)
    pdi = [100 * (pdm_s[i] / atr[i]) if atr[i] else 0.0 for i in range(len(closes))]
    ndi = [100 * (ndm_s[i] / atr[i]) if atr[i] else 0.0 for i in range(len(closes))]
    dx = [100 * abs(pdi[i] - ndi[i]) / (pdi[i] + ndi[i]) if (pdi[i] + ndi[i]) else 0.0
          for i in range(len(closes))]
    if len(dx) <= 2 * n:
        return None
    adx = sum(dx[n + 1:2 * n + 1]) / n
    for i in range(2 * n + 1, len(closes)):
        adx = (adx * (n - 1) + dx[i]) / n
    return adx


def compute_di_dir(highs: list[float], lows: list[float], closes: list[float],
                   length: int = 14) -> str | None:
    """Направление доминирующей стороны по Wilder DMI: 'long' (+DI>−DI) | 'short'
    (−DI≥+DI) | None (данных мало).

    J. Welles Wilder «New Concepts in Technical Trading» (1978): +DI измеряет
    бычье давление (up-moves), −DI медвежье (down-moves). Кто больше — тот и
    доминирует. Быстрее EMA200-кросса ловит смену стороны (не лаг по цене), что
    критично для отсечения контртренд-лонгов в дип на даунтрендовых альтах
    (v0.18.4, диагноз live: лонги 20% WR vs шорты 54%). Тот же Wilder-расчёт,
    что и ADX (compute_adx), но возвращаем направление, а не силу. Требуем
    ≥ length+1 свечей (прогрев Wilder-сглаживания). Меньше → None (fail-open)."""
    n = length
    if n <= 0 or len(closes) < n + 1:
        return None

    def _wilder(x: list[float]) -> list[float]:
        out = [0.0] * len(x)
        if len(x) <= n:
            return out
        s = sum(x[1:n + 1])
        out[n] = s
        for i in range(n + 1, len(x)):
            s = s - s / n + x[i]
            out[i] = s
        return out

    tr = [0.0]
    pdm = [0.0]
    ndm = [0.0]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
    atr = _wilder(tr)
    pdm_s = _wilder(pdm)
    ndm_s = _wilder(ndm)
    last = len(closes) - 1
    if atr[last] <= 0:
        return None
    pdi = 100 * (pdm_s[last] / atr[last])
    ndi = 100 * (ndm_s[last] / atr[last])
    return "long" if pdi > ndi else "short"


class HtfTrend:
    """Кэш EMA + ADX + DMI старшего ТФ по символу: направление (EMA), сила (ADX)
    и доминирующая сторона (DMI +DI/−DI) тренда."""

    def __init__(self, ema_len: int = 200, interval: str = "60",
                 adx_len: int = 14) -> None:
        self.ema_len = ema_len
        self.interval = interval
        self.adx_len = adx_len
        self._ema: dict[str, float] = {}
        self._adx: dict[str, float] = {}
        self._di_dir: dict[str, str] = {}

    def refresh(self, client, symbols: list[str]) -> None:
        """Обновить EMA+ADX по символам из ОДНОГО запроса клинов. При сбое одного —
        сохраняем прошлое значение (fail-open), не удаляем (иначе мигнувший
        REST-сбой снимет фильтр). limit покрывает прогрев и EMA, и ADX."""
        for sym in symbols:
            kline = client.get_kline(sym, self.interval, limit=self.ema_len)
            ema = compute_ema(_closes_ascending(kline), self.ema_len)
            if ema is not None and ema > 0:
                self._ema[sym] = ema
            highs, lows, closes = _ohlc_ascending(kline)
            adx = compute_adx(highs, lows, closes, self.adx_len)
            if adx is not None:
                self._adx[sym] = adx
            di = compute_di_dir(highs, lows, closes, self.adx_len)
            if di is not None:
                self._di_dir[sym] = di

    def has_data(self, symbol: str) -> bool:
        """EMA по символу хоть раз успешно посчитана (символ прогрет). False для
        НИКОГДА не считавшегося символа (свежая ротация / новый листинг). Нужно
        для fail-closed MR-гейта: канон QuantConnect — «refuse to trade until
        indicator ready» (не путать с транзиентным REST-сбоем: там keep-last,
        символ остаётся в _ema). v0.18.2."""
        return symbol in self._ema

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

    def trend_strength(self, symbol: str) -> float | None:
        """ADX старшего ТФ (сила тренда) или None (нет данных)."""
        return self._adx.get(symbol)

    def is_strong_trend(self, symbol: str, adx_max: float) -> bool:
        """Трендовый день: ADX ≥ adx_max → фейд запрещён (канон MR). Нет данных →
        False (fail-open: не блокируем, если ADX не посчитан)."""
        adx = self._adx.get(symbol)
        return adx is not None and adx >= adx_max

    def di_direction(self, symbol: str) -> str | None:
        """Доминирующая сторона по DMI: 'long' (+DI>−DI) | 'short' | None (нет
        данных). v0.18.4."""
        return self._di_dir.get(symbol)

    def di_blocks_long(self, symbol: str) -> bool:
        """Асимметричный гейт (v0.18.4): DMI смотрит вниз (−DI≥+DI) → лонг-фейд
        запрещён (контртренд-лонг в дип). Нет данных → False (fail-open: DMI не
        посчитан, не блокируем; прогрев гарантируется has_data-гейтом по EMA)."""
        return self._di_dir.get(symbol) == "short"
