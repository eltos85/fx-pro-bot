"""Positioning-фичи (Open Interest delta, funding rate dynamics).

Эти фичи относятся к классу «institutional positioning / flow signals»
и в 2024-2026 считаются primary индикаторами в крипто-квант-десках,
заменяя классические RSI/MACD как «единственный сигнал».

─── Research basis ───
- Decentralised.news «Quant Signals for Crypto Derivatives 2026»:
    «OI alone is meaningless; combine with price, funding, and liquidation
    density to map market reflexivity». Конкретные паттерны:
        * High OI + rising price + rising funding = crowded longs с риском
            cascade liquidation.
        * Falling OI + falling price + negative funding = forced
            deleveraging, mean-reversion edge.
- Borri, Cagnazzo «Bitcoin perpetual futures: predictive content of
    open interest and funding rates» (J. Empirical Finance 2024).
- Lambda Finance «Crypto Funding-Band Framework 2026»:
    |rate| <0.05% per 8h → нейтральная зона
    0.05–0.20% → лёгкий перекос (mild bias)
    >0.20% → сильный перекос (strong bias / euphoria или panic)
- MetaMask «Monitoring perps funding rate trends» (2026):
    «cumulative funding» лучше передаёт capital deployment чем snapshot.

Bybit V5 функционал, на котором стоит модуль:
- `/v5/market/open-interest` — `intervalTime=1h, limit=24` даёт
    24 точки OI за сутки.
- `/v5/market/funding/history` — 8h funding × 10 = ≈3.3 дня.

Формулы:
- OI delta % = (OI_now − OI_then) / OI_then × 100
- Funding cumulative 24h = sum(last 3 settlements) — компонент
    «реальный capital deployment cost» (MetaMask 2026).
"""
from __future__ import annotations

from dataclasses import dataclass

from ai_trader.trading.client import FundingPoint, OpenInterestPoint


@dataclass
class PositioningSnapshot:
    """Сводка positioning-фич по одному символу."""

    # Open Interest
    oi_now: float | None
    oi_4h_ago: float | None
    oi_24h_ago: float | None
    oi_delta_4h_pct: float | None
    oi_delta_24h_pct: float | None

    # Funding rate
    funding_now: float | None              # snapshot (already in ticker, дублируем для symmetry)
    funding_24h_cumulative: float | None   # sum последних 3 settlements (24h при 8h funding)
    funding_24h_mean: float | None
    funding_7d_mean: float | None
    funding_prev_period: float | None       # rate prev settlement (для tracking turning point)


def _delta_pct(now: float, then: float) -> float | None:
    if then <= 0:
        return None
    return (now - then) / then * 100


def build_positioning_snapshot(
    oi_history: list[OpenInterestPoint] | None,
    funding_history: list[FundingPoint] | None,
    funding_now: float | None = None,
) -> PositioningSnapshot:
    """Построить snapshot из сырых истории-массивов.

    Tolerant к None / коротким массивам — для каждой производной
    проверяет наличие минимально нужных точек.

    Конвенция: oi_history и funding_history отсортированы по ts возр.
    (как возвращает client.get_*).
    """
    oi_now = oi_4h = oi_24h = oi_d4 = oi_d24 = None
    if oi_history:
        oi_now = oi_history[-1].value
        # Шаг 1h на интервале 1h: -4 = 4 часа назад, -24 = 24 часа назад.
        # При недостатке точек — оставляем None.
        if len(oi_history) >= 5:
            oi_4h = oi_history[-5].value
            oi_d4 = _delta_pct(oi_now, oi_4h)
        if len(oi_history) >= 25:
            oi_24h = oi_history[-25].value
            oi_d24 = _delta_pct(oi_now, oi_24h)

    f_cum_24h = f_mean_24h = f_mean_7d = f_prev = None
    if funding_history:
        # Последние 3 события ≈ 24h при 8h-funding settle. Если в массиве
        # их меньше 3 — берём что есть (агрегат всё равно осмыслен, просто
        # охватывает <24h; это лучше чем None).
        last3 = funding_history[-3:]
        f_cum_24h = sum(p.rate for p in last3)
        f_mean_24h = f_cum_24h / len(last3)
        # 7d ≈ 21 settlement (8h × 21 = 168h)
        last21 = funding_history[-21:]
        f_mean_7d = sum(p.rate for p in last21) / len(last21)
        # «prev period» — событие до текущего settlement
        if len(funding_history) >= 2:
            f_prev = funding_history[-2].rate

    return PositioningSnapshot(
        oi_now=oi_now,
        oi_4h_ago=oi_4h,
        oi_24h_ago=oi_24h,
        oi_delta_4h_pct=oi_d4,
        oi_delta_24h_pct=oi_d24,
        funding_now=funding_now,
        funding_24h_cumulative=f_cum_24h,
        funding_24h_mean=f_mean_24h,
        funding_7d_mean=f_mean_7d,
        funding_prev_period=f_prev,
    )


# ─── Метки / форматирование ──────────────────────────────────────────


def _oi_delta_label(delta_pct: float | None) -> str:
    if delta_pct is None:
        return ""
    a = abs(delta_pct)
    if a >= 15:
        sign = "EXTREME buildup" if delta_pct > 0 else "EXTREME unwind"
        return f" [{sign}]"
    if a >= 10:
        sign = "strong buildup" if delta_pct > 0 else "strong unwind"
        return f" [{sign}]"
    if a >= 5:
        sign = "buildup" if delta_pct > 0 else "unwind"
        return f" [{sign}]"
    if a >= 2:
        return " [moderate]"
    return ""


def _funding_label(rate_per_period: float | None) -> str:
    """Lambda Finance 2026 funding bands. rate_per_period — единичный
    8h funding (доля, 0.0005 = 0.05%)."""
    if rate_per_period is None:
        return ""
    a = abs(rate_per_period)
    if a >= 0.0020:  # 0.20%
        sign = "STRONG long bias" if rate_per_period > 0 else "STRONG short bias"
        return f" [{sign}]"
    if a >= 0.0005:  # 0.05%
        sign = "mild long bias" if rate_per_period > 0 else "mild short bias"
        return f" [{sign}]"
    return " [neutral leverage]"


def _fmt(v: float | None, spec: str) -> str:
    if v is None:
        return "n/a"
    try:
        return spec.format(v)
    except (ValueError, TypeError):
        return "n/a"


def format_positioning(s: PositioningSnapshot) -> str:
    """Двустрочный текстовый формат для system-promptа.

    Пример вывода:

        OI: now=1.235M, Δ4h=+3.1% [moderate], Δ24h=+11.8% [strong buildup]
        Funding: now=+0.012%, 24h cum=+0.036% [mild long bias],
        24h mean=+0.012%, 7d mean=+0.008%
    """
    line1 = (
        f"  OI: now={_fmt(s.oi_now, '{:.4g}')}, "
        f"Δ4h={_fmt(s.oi_delta_4h_pct, '{:+.2f}')}%{_oi_delta_label(s.oi_delta_4h_pct)}, "
        f"Δ24h={_fmt(s.oi_delta_24h_pct, '{:+.2f}')}%{_oi_delta_label(s.oi_delta_24h_pct)}"
    )
    # funding_now / funding_24h_mean / funding_7d_mean — все в долях,
    # выводим как % с 4 знаками.
    fnow_pct = s.funding_now * 100 if s.funding_now is not None else None
    fcum_pct = s.funding_24h_cumulative * 100 if s.funding_24h_cumulative is not None else None
    fmean_pct = s.funding_24h_mean * 100 if s.funding_24h_mean is not None else None
    f7d_pct = s.funding_7d_mean * 100 if s.funding_7d_mean is not None else None

    line2 = (
        f"  Funding: now={_fmt(fnow_pct, '{:+.4f}')}%{_funding_label(s.funding_now)}, "
        f"24h cum={_fmt(fcum_pct, '{:+.4f}')}%{_funding_label(s.funding_24h_mean)}, "
        f"24h mean={_fmt(fmean_pct, '{:+.4f}')}%, "
        f"7d mean={_fmt(f7d_pct, '{:+.4f}')}%"
    )
    return f"{line1}\n{line2}"
