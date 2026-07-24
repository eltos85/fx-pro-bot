"""Метрики контекста входа — ТОЛЬКО observability, на торговлю не влияют.

Контекст (BUILDLOG 2026-07-03, loss-аудит 06-05→07-02, 103 сделки broker-truth):
лонги теряют −$154.58 (avgR −0.23) против шортов −$34.50 (avgR −0.04) при
одинаковом WR 31% — «направленная» слепота есть, но из deal-list её причину
не видно: в БД решений нет ни старшего тренда, ни режима, ни спреда на входе.
Каждый аудит приходится реконструировать контекст постфактум из yfinance
(scripts/momentum_loss_audit.py) с риском look-ahead/несовпадения баров.

Этот модуль считает контекст В МОМЕНТ решения и персистит его в
``momentum_decisions`` (ctx_* колонки). Дальше срезы «where хромает»
(with-trend vs counter, режим ADX, растянутость, спред) достаются одним
SQL-запросом без реконструкции.

─── Research basis (метрики каноничные, пороги НЕ применяются к торговле) ───
- EMA200 как прокси старшего тренда: Murphy «Technical Analysis of the
  Financial Markets» (1999), ch.9 — так же используется блокирующим фильтром
  в advisor-стратегиях (strategy-guard.mdc), здесь — только лог.
- ADX(14) Уайлдера как мера трендовости режима: Wilder «New Concepts in
  Technical Trading Systems» (1978). ADX<20 ≈ рейндж, >25–30 ≈ тренд.
- Спред на входе как прямой вычет из R (cost-to-risk): Harris «Trading and
  Exchanges» (2003), ch.21 — уже меряется спред-гардом, теперь и логируется.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

EMA_SPAN = 200
ADX_PERIOD = 14


@dataclass(frozen=True, slots=True)
class EntryContext:
    """Снимок контекста на закрытом баре, по которому принято решение."""

    ema_dist_atr: float      # (close − EMA200) / ATR14 — знак = сторона тренда
    adx: float               # ADX(14) Уайлдера
    with_htf: bool | None    # направление сигнала совпадает со стороной EMA200
                             # (None для flat — сравнивать нечего)


def adx_block_reason(
    ctx: EntryContext | None, *, enabled: bool, adx_min: float
) -> str | None:
    """Причина скипа входа в рейндже (ADX < adx_min), либо None (вход разрешён).

    None == вход разрешён (в т.ч. при ctx=None — холодный старт / мало данных:
    не блокируем, чтобы не ломать старт и не подгонять). Строка == вход
    блокируется (текст для лога).

    ─── Research basis (BUILDLOG 2026-07-24) ───
    - Wilder «New Concepts…» (1978): ADX(14) < 20 ≈ рейндж (нет трендовости).
    - Chan / AQR (Hurst, Ooi, Pedersen 2017, «A Century of Evidence…»):
      time-series momentum требует трендового режима; в chop/рейндже edge
      отсутствует.
    - Эмпирика (loss-audit 13.07-24.07, 34 сделки): ADX<20 — 19/34 сделок,
      PF 0.24, net −$119; ADX 20-30 — ~ноль. МАЛАЯ ВЫБОРКА — переоценить на
      ≥100 сделках (no-data-fitting.mdc). Обратимо: enabled=False.
    """
    if not enabled or ctx is None:
        return None
    if ctx.adx < adx_min:
        return f"low_adx(adx={ctx.adx:.1f}<{adx_min:.0f})"
    return None


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Сглаживание Уайлдера = EWM с alpha=1/period (Wilder 1978)."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def compute_entry_context(
    candles: pd.DataFrame, direction: str
) -> EntryContext | None:
    """Контекст входа по df свечей (тот же, что видит build_signal).

    None, если данных мало (< EMA_SPAN баров) или в хвосте NaN — контекст
    опционален. EMA-dist/with_htf остаются observability-only (НЕ блокируют);
    ADX с 2026-07-24 стал блокирующим фильтром (settings.adx_filter_enabled,
    BUILDLOG 2026-07-24) — но ctx=None по-прежнему НЕ блокирует (холодный
    старт, мало данных).
    """
    if candles is None or candles.empty or len(candles) < EMA_SPAN:
        return None
    try:
        close = candles["Close"]
        high = candles["High"]
        low = candles["Low"]

        ema = close.ewm(span=EMA_SPAN, adjust=False).mean()

        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = _wilder_smooth(tr, ADX_PERIOD)

        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        plus_di = 100.0 * _wilder_smooth(plus_dm, ADX_PERIOD) / atr
        minus_di = 100.0 * _wilder_smooth(minus_dm, ADX_PERIOD) / atr
        di_sum = plus_di + minus_di
        dx = 100.0 * (plus_di - minus_di).abs() / di_sum.where(di_sum > 0)
        adx = _wilder_smooth(dx, ADX_PERIOD)

        last_close = float(close.iloc[-1])
        last_ema = float(ema.iloc[-1])
        last_atr = float(atr.iloc[-1])
        last_adx = float(adx.iloc[-1])
        if not (last_atr > 0) or pd.isna(last_adx):
            return None

        dist = (last_close - last_ema) / last_atr
        with_htf: bool | None = None
        if direction in {"long", "short"}:
            with_htf = (direction == "long") == (dist > 0)
        return EntryContext(
            ema_dist_atr=round(dist, 4),
            adx=round(last_adx, 2),
            with_htf=with_htf,
        )
    except Exception:  # noqa: BLE001 — метрики не должны ронять торговый цикл
        return None
