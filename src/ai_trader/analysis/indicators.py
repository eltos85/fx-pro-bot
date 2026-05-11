"""Технические индикаторы для AI-Trader.

Все формулы — каноничные, см. блок Research basis ниже.
Реализации без внешних зависимостей (только Python stdlib).

─── Research basis ───
- RSI: J. Welles Wilder Jr. «New Concepts in Technical Trading Systems» (1978).
  Период по умолчанию 14, использует Wilder's smoothing (RMA, не EMA).
- MACD: Gerald Appel «Technical Analysis: Power Tools for Active Investors»
  (2005). Стандартные параметры: fast=12, slow=26, signal=9 (EMA-based).
- ATR: тот же Wilder (1978). RMA(True Range, 14).
- EMA: канонический экспоненциально-взвешенный mean. α = 2/(N+1).
- Bollinger Bands: John Bollinger «Bollinger on Bollinger Bands» (2001).
  Middle = SMA(20), Upper/Lower = middle ± 2·std(20).
- VWAP (Volume-Weighted Average Price): Berkowitz, Logue, Noser «The Total
  Cost of Transactions on the NYSE» (Journal of Finance 1988); institutional
  standard для оценки fair-value execution. Typical price (H+L+C)/3 × volume,
  cumulative по окну. Используется как 2026-крипто proxy на institutional bid.
- Realized Volatility (RV): Andersen/Bollerslev/Diebold/Labys «Modeling and
  Forecasting Realized Volatility» (Econometrica 2003); Andersen et al.
  «The Distribution of Realized Stock Return Volatility» (J. Financial
  Econ. 2001). Sum of squared log-returns по окну, аннуализируется.
  В 2024-2026 квант-десках RV предпочитают ATR — он modeling-friendly
  (lognormal returns) и используется как input для GARCH/HAR-RV
  forecasting. См. Decentralised.news «Quant Signals for Crypto
  Derivatives 2026»: «Realized Volatility vs Implied Volatility spread —
  signal что market makers price директионального риска».
"""
from __future__ import annotations

from dataclasses import dataclass
from math import log, sqrt


@dataclass
class IndicatorSnapshot:
    """Полный набор индикаторов для одного символа.

    v0.5 (2026-05-07): добавлены VWAP и Realized Volatility — современные
    quant-фичи 2026 года (positioning/flow-aware). VWAP — institutional
    fair-value benchmark; RV — modeling-friendly альтернатива ATR.
    Классические индикаторы (RSI/MACD/BB/EMA/ATR) остаются как
    secondary context для LLM.
    """

    last_close: float
    rsi14: float | None
    macd_line: float | None
    macd_signal: float | None
    macd_hist: float | None
    atr14: float | None
    atr14_pct: float | None  # ATR / last_close * 100, для нормализации
    ema20: float | None
    ema50: float | None
    bb_upper: float | None
    bb_middle: float | None
    bb_lower: float | None
    bb_position: float | None  # (close-lower)/(upper-lower) [0..1]; <0 / >1 = за пределами
    # v0.5: новые quant-фичи 2026.
    vwap: float | None = None              # rolling VWAP по окну (default = весь массив свечей)
    vwap_dev_pct: float | None = None      # (close-vwap)/vwap*100 — отклонение от institutional fair-value
    rv_pct: float | None = None            # rolling annualised realized volatility, в %
    rv_window_bars: int | None = None      # сколько баров пошло в RV (для интерпретации)
    # v0.12 (2026-05-11): regime classifier по ADX (Wilder 1978). ADX —
    # сила тренда (любого направления), 0-100. >25 = trending, <20 = ranging.
    # +DI / -DI показывают направление: +DI>-DI = uptrend, иначе downtrend.
    # Используется как regime filter: mean-reversion разрешён только в ranging
    # (botversusbot 2026, Connors/Raschke 1995).
    adx14: float | None = None             # сила тренда
    plus_di14: float | None = None         # бычья сила (Directional Indicator +)
    minus_di14: float | None = None        # медвежья сила (Directional Indicator -)


# ─── Базовые helpers ─────────────────────────────────────────────────────


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    alpha = 2 / (period + 1)
    seed = sum(values[:period]) / period
    e = seed
    for v in values[period:]:
        e = alpha * v + (1 - alpha) * e
    return e


def _ema_series(values: list[float], period: int) -> list[float]:
    """Возвращает полный массив EMA (None для первых period-1 значений)."""
    if period <= 0 or len(values) < period:
        return [float("nan")] * len(values)
    alpha = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out = [float("nan")] * (period - 1) + [seed]
    e = seed
    for v in values[period:]:
        e = alpha * v + (1 - alpha) * e
        out.append(e)
    return out


def _rma(values: list[float], period: int) -> float | None:
    """Wilder's RMA (Running Moving Average), как в RSI/ATR.

    Первое значение = SMA первых period значений.
    Дальше: RMA[i] = (RMA[i-1] * (period - 1) + value[i]) / period.
    """
    if len(values) < period or period <= 0:
        return None
    seed = sum(values[:period]) / period
    rma = seed
    for v in values[period:]:
        rma = (rma * (period - 1) + v) / period
    return rma


# ─── Конкретные индикаторы ───────────────────────────────────────────────


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = _rma(gains, period)
    avg_loss = _rma(losses, period)
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    """Возвращает (macd_line, signal_line, histogram).

    macd_line = EMA(fast) - EMA(slow)
    signal = EMA(macd_line, signal)
    histogram = macd_line - signal
    """
    if len(closes) < slow + signal:
        return (None, None, None)
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line_series = [
        f - s if not (f != f or s != s) else float("nan")
        for f, s in zip(ema_fast, ema_slow)
    ]
    valid_macd = [v for v in macd_line_series if v == v]  # отбрасываем NaN
    if len(valid_macd) < signal:
        return (None, None, None)
    sig = ema(valid_macd, signal)
    macd_now = valid_macd[-1]
    if sig is None:
        return (macd_now, None, None)
    return (macd_now, sig, macd_now - sig)


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """TR = max(high-low, |high-prev_close|, |low-prev_close|)."""
    if len(highs) != len(lows) or len(highs) != len(closes) or len(highs) < 2:
        return []
    out: list[float] = []
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        out.append(max(hl, hc, lc))
    return out


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float | None:
    trs = true_ranges(highs, lows, closes)
    if len(trs) < period:
        return None
    return _rma(trs, period)


def adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> tuple[float | None, float | None, float | None]:
    """ADX/+DI/-DI по Wilder 1978 ("New Concepts in Technical Trading Systems").

    ADX — мера СИЛЫ тренда (не направления), 0-100. Стандартные пороги:
    - ADX > 25 — выраженный тренд (любой);
    - ADX 20-25 — слабый / формирующийся тренд;
    - ADX < 20 — рынок в боковике (ranging).

    +DI > -DI означает uptrend, -DI > +DI — downtrend.

    Алгоритм точно по Wilder (с RMA-сглаживанием TR/DM, не SMA):
    1. TR  = max(high-low, |high-prevClose|, |low-prevClose|)
    2. +DM = up_move если up_move > down_move и >0 иначе 0
    3. -DM = down_move если down_move > up_move и >0 иначе 0
    4. RMA(TR, period), RMA(+DM, period), RMA(-DM, period)
    5. +DI = +DM_smooth / TR_smooth * 100
       -DI = -DM_smooth / TR_smooth * 100
    6. DX  = |+DI - -DI| / (+DI + -DI) * 100
    7. ADX = RMA(DX, period)

    Возвращает (adx, +DI, -DI). None если данных <2*period+1.

    Research basis: J. Welles Wilder Jr., "New Concepts in Technical Trading
    Systems" (1978). Применение как regime filter: botversusbot 2026, AOTrading
    "3-5-7 Rule 2026", canonical Connors/Raschke "Street Smarts" (1995).
    """
    n = len(closes)
    if n != len(highs) or n != len(lows):
        return (None, None, None)
    # Нужно как минимум 2*period+1 баров (period для сглаживания TR/DM +
    # ещё period для сглаживания DX).
    if n < 2 * period + 1:
        return (None, None, None)

    tr_list: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        hi = highs[i]
        lo = lows[i]
        prev_close = closes[i - 1]
        prev_hi = highs[i - 1]
        prev_lo = lows[i - 1]
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        tr_list.append(tr)
        up = hi - prev_hi
        down = prev_lo - lo
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    # Wilder использует RMA (= EMA с alpha=1/period), реализован как _rma.
    # Считаем сглаженные ряды TR/+DM/-DM пошагово, чтобы получить ряд DX.
    def rma_series(values: list[float], p: int) -> list[float]:
        if len(values) < p:
            return []
        out: list[float] = []
        seed = sum(values[:p]) / p
        out.append(seed)
        for v in values[p:]:
            out.append((out[-1] * (p - 1) + v) / p)
        return out

    tr_s = rma_series(tr_list, period)
    pdm_s = rma_series(plus_dm, period)
    mdm_s = rma_series(minus_dm, period)
    if not tr_s or len(tr_s) != len(pdm_s):
        return (None, None, None)

    dx_list: list[float] = []
    for i in range(len(tr_s)):
        if tr_s[i] <= 0:
            continue
        pdi = pdm_s[i] / tr_s[i] * 100
        mdi = mdm_s[i] / tr_s[i] * 100
        denom = pdi + mdi
        if denom <= 0:
            dx_list.append(0.0)
        else:
            dx_list.append(abs(pdi - mdi) / denom * 100)

    if len(dx_list) < period:
        return (None, None, None)
    adx_smoothed = rma_series(dx_list, period)
    if not adx_smoothed:
        return (None, None, None)
    adx_val = adx_smoothed[-1]
    plus_di_last = pdm_s[-1] / tr_s[-1] * 100 if tr_s[-1] > 0 else None
    minus_di_last = mdm_s[-1] / tr_s[-1] * 100 if tr_s[-1] > 0 else None
    return (adx_val, plus_di_last, minus_di_last)


def bollinger(closes: list[float], period: int = 20, sigma: float = 2.0) -> tuple[float | None, float | None, float | None]:
    """Возвращает (upper, middle, lower). middle = SMA(period)."""
    if len(closes) < period or period <= 0:
        return (None, None, None)
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = sqrt(var)
    return (mid + sigma * sd, mid, mid - sigma * sd)


def vwap(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    period: int | None = None,
) -> float | None:
    """Volume-Weighted Average Price по окну.

    Typical price = (H+L+C)/3, взвешенный по volume каждого бара,
    суммируется по последним `period` барам (или по всему массиву если
    period=None). Используется институционалами как proxy на
    fair-value execution price (Berkowitz/Logue/Noser 1988).

    Возвращает None если данных недостаточно или volume суммарный = 0.
    """
    n = len(closes)
    if n == 0 or n != len(highs) or n != len(lows) or n != len(volumes):
        return None
    if period is not None and period <= 0:
        return None
    window = n if period is None else min(period, n)
    if window <= 0:
        return None
    h = highs[-window:]
    l = lows[-window:]
    c = closes[-window:]
    v = volumes[-window:]
    pv_sum = 0.0
    v_sum = 0.0
    for i in range(window):
        if v[i] <= 0:
            continue
        typical = (h[i] + l[i] + c[i]) / 3
        pv_sum += typical * v[i]
        v_sum += v[i]
    if v_sum <= 0:
        return None
    return pv_sum / v_sum


def realized_volatility(
    closes: list[float],
    period: int | None = None,
    bars_per_year: float = 24 * 365,
) -> float | None:
    """Аннуализированная Realized Volatility, в долях (0.5 = 50%).

    RV = √(Σ(log_return²) / N × bars_per_year) —
    стандарт Andersen/Bollerslev (Econometrica 2003).

    `period` — окно последних N return'ов (None = все доступные).
    `bars_per_year` — для аннуализации; default 24×365 предполагает
    часовые свечи. Для 4h свечей: 6×365=2190.

    Возвращает None если данных < 2.
    """
    n = len(closes)
    if n < 2:
        return None
    if period is not None and period <= 0:
        return None
    returns: list[float] = []
    start = 1 if period is None else max(1, n - period)
    for i in range(start, n):
        prev = closes[i - 1]
        cur = closes[i]
        if prev <= 0 or cur <= 0:
            continue
        returns.append(log(cur / prev))
    if not returns:
        return None
    sq_sum = sum(r * r for r in returns)
    return sqrt(sq_sum / len(returns) * bars_per_year)


# ─── Сводка по всем индикаторам ──────────────────────────────────────────


def compute_snapshot(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float] | None = None,
    *,
    vwap_window: int | None = None,
    rv_window: int | None = 24,
    bars_per_year: float = 24 * 365,
) -> IndicatorSnapshot:
    """Полный snapshot. Тихо возвращает None для тех индикаторов, для которых
    данных не хватило (например, при первом запуске когда только 5 свечей).

    v0.5: добавлены VWAP и RV. `volumes` опционален (если None — VWAP=None).
    `vwap_window` default = len(closes) (rolling VWAP по всему окну
    переданных свечей; для 1H × 100 это VWAP за последние ~4 дня).
    `rv_window` default = 24 (последние 24 часовых return'а ≈ 1 сутки).
    `bars_per_year` для аннуализации RV: 8760 для 1H, 2190 для 4H.
    """
    last_close = closes[-1] if closes else 0.0
    rsi_v = rsi(closes, 14)
    macd_line, macd_sig, macd_h = macd(closes)
    atr_v = atr(highs, lows, closes, 14)
    atr_pct = (atr_v / last_close * 100) if (atr_v is not None and last_close > 0) else None
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    bb_u, bb_m, bb_l = bollinger(closes, 20, 2.0)
    bb_pos: float | None = None
    if bb_u is not None and bb_l is not None and bb_u != bb_l:
        bb_pos = (last_close - bb_l) / (bb_u - bb_l)

    vwap_v: float | None = None
    vwap_dev: float | None = None
    if volumes is not None:
        vwap_v = vwap(highs, lows, closes, volumes, period=vwap_window)
        if vwap_v is not None and vwap_v > 0:
            vwap_dev = (last_close - vwap_v) / vwap_v * 100

    rv = realized_volatility(closes, period=rv_window, bars_per_year=bars_per_year)
    rv_pct = rv * 100 if rv is not None else None
    rv_n = min(rv_window or len(closes), max(0, len(closes) - 1))

    adx_v, plus_di_v, minus_di_v = adx(highs, lows, closes, 14)

    return IndicatorSnapshot(
        last_close=last_close,
        rsi14=rsi_v,
        macd_line=macd_line,
        macd_signal=macd_sig,
        macd_hist=macd_h,
        atr14=atr_v,
        atr14_pct=atr_pct,
        ema20=ema20,
        ema50=ema50,
        bb_upper=bb_u,
        bb_middle=bb_m,
        bb_lower=bb_l,
        bb_position=bb_pos,
        vwap=vwap_v,
        vwap_dev_pct=vwap_dev,
        rv_pct=rv_pct,
        rv_window_bars=rv_n if rv is not None else None,
        adx14=adx_v,
        plus_di14=plus_di_v,
        minus_di14=minus_di_v,
    )


def format_snapshot(s: IndicatorSnapshot) -> str:
    """Компактная человекочитаемая строка для вкладывания в LLM-context."""

    def fmt(x: float | None, pattern: str) -> str:
        return pattern.format(x) if x is not None else "n/a"

    rsi_label = ""
    if s.rsi14 is not None:
        if s.rsi14 >= 70:
            rsi_label = " [OVERBOUGHT]"
        elif s.rsi14 <= 30:
            rsi_label = " [OVERSOLD]"

    macd_label = ""
    if s.macd_hist is not None:
        macd_label = " [bullish]" if s.macd_hist > 0 else " [bearish]"

    trend_label = ""
    if s.ema20 is not None and s.ema50 is not None:
        if s.ema20 > s.ema50 and s.last_close > s.ema20:
            trend_label = " [uptrend]"
        elif s.ema20 < s.ema50 and s.last_close < s.ema20:
            trend_label = " [downtrend]"
        else:
            trend_label = " [mixed]"

    bb_label = ""
    if s.bb_position is not None:
        if s.bb_position >= 1.0:
            bb_label = " [above upper BB]"
        elif s.bb_position <= 0.0:
            bb_label = " [below lower BB]"
        elif s.bb_position >= 0.8:
            bb_label = " [near upper BB]"
        elif s.bb_position <= 0.2:
            bb_label = " [near lower BB]"

    # VWAP-метка: насколько price отклонился от institutional fair-value.
    # Пороги ±0.5% / ±2% — разумные эвристики для крипто (на 1h-окне).
    vwap_label = ""
    if s.vwap_dev_pct is not None:
        if s.vwap_dev_pct >= 2.0:
            vwap_label = " [STRETCHED above VWAP]"
        elif s.vwap_dev_pct >= 0.5:
            vwap_label = " [above VWAP]"
        elif s.vwap_dev_pct <= -2.0:
            vwap_label = " [STRETCHED below VWAP]"
        elif s.vwap_dev_pct <= -0.5:
            vwap_label = " [below VWAP]"
        else:
            vwap_label = " [near VWAP]"

    # Realized Volatility-метка: режим волатильности.
    # Эмпирические пороги для крипто-перпов (annualised RV): low <50%,
    # normal 50-100%, elevated 100-200%, extreme >200%.
    rv_label = ""
    if s.rv_pct is not None:
        if s.rv_pct >= 200:
            rv_label = " [EXTREME vol regime]"
        elif s.rv_pct >= 100:
            rv_label = " [elevated vol]"
        elif s.rv_pct >= 50:
            rv_label = " [normal vol]"
        else:
            rv_label = " [low vol / squeeze candidate]"

    # v0.12: ADX-based regime classifier. Используется LLM как блокер
    # counter-trend mean-reversion (см. REGIME FILTER в SYSTEM_PROMPT).
    adx_label = ""
    if s.adx14 is not None and s.plus_di14 is not None and s.minus_di14 is not None:
        regime: str
        if s.adx14 >= 25:
            direction = "uptrend" if s.plus_di14 > s.minus_di14 else "downtrend"
            regime = f"TRENDING {direction}"
        elif s.adx14 < 20:
            regime = "RANGING (mean-reversion zone)"
        else:
            regime = "TRANSITION"
        adx_label = f" [{regime}]"

    return (
        f"  RSI14={fmt(s.rsi14, '{:.1f}')}{rsi_label} "
        f"MACD={fmt(s.macd_line, '{:.4g}')}/sig={fmt(s.macd_signal, '{:.4g}')}/"
        f"hist={fmt(s.macd_hist, '{:+.4g}')}{macd_label}\n"
        f"  ATR14={fmt(s.atr14, '{:.4g}')} ({fmt(s.atr14_pct, '{:.2f}')}% of price)  "
        f"EMA20={fmt(s.ema20, '{:.4g}')} EMA50={fmt(s.ema50, '{:.4g}')}{trend_label}\n"
        f"  BB(20,2): upper={fmt(s.bb_upper, '{:.4g}')} mid={fmt(s.bb_middle, '{:.4g}')} "
        f"lower={fmt(s.bb_lower, '{:.4g}')} pos={fmt(s.bb_position, '{:.2f}')}{bb_label}\n"
        f"  VWAP={fmt(s.vwap, '{:.6g}')} dev={fmt(s.vwap_dev_pct, '{:+.2f}')}%{vwap_label}  "
        f"RV(annualised, n={s.rv_window_bars or 0})={fmt(s.rv_pct, '{:.1f}')}%{rv_label}\n"
        f"  ADX14={fmt(s.adx14, '{:.1f}')} +DI={fmt(s.plus_di14, '{:.1f}')} "
        f"-DI={fmt(s.minus_di14, '{:.1f}')}{adx_label}"
    )
