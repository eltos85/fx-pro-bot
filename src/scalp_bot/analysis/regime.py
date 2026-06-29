"""Regime-фичи на момент входа — логирование для meta-labeling анализа.

Research basis:
    Marcos Lopez de Prado, «Advances in Financial Machine Learning» (2018,
    Wiley), Chapter 3 «Meta-Labeling». Primary model = торговый сигнал
    страты (direction решает стратегия); meta-модель = бинарный классификатор
    «брать ли сделку с учётом regime-фичей». Фичи описывают качество
    сигнального инстанса + рыночный режим (Ch3: «features from the first
    model concatenated … additional regime features not in the primary»).

    Сами индикаторы каноничны и уже в проекте:
    - ADX (Wilder 1978) — `data/htf.py` (тренд vs range).
    - regime_ratio = |close−open|/avgATR за N 15m-баров (Kaufman Efficiency
      Ratio-аналог, Connors/Raschke «MR в диапазоне, momentum в тренде») —
      `data/levels.py`.
    - CVD-slope / absorption — Kalena/chartwhisperer CAP-канон (`signals.py`).
    - session buckets — канон сессионности скальпа.
    - spread/imbalance/funding/liq — микроструктура (`data/aggregates.py`).

НАЗНАЧЕНИЕ: ТОЛЬКО логирование. Фичи пишутся в отдельную таблицу
``regime_features`` на каждый вход и НЕ влияют на торговую логику
(no-data-fitting.mdc: «Добавление метрик/логирования (не влияют на торговлю)»
— допустимо без выборки). Анализ (условный разрез E[trade|regime] на n≥100 +
OOS по Lopez de Prado Ch7/Ch11) делается офлайн в ``scripts/``, по результатам
гейт предлагается отдельно с research-ссылкой (strategy-guard.mdc).
"""
from __future__ import annotations

from dataclasses import dataclass


def _session_bucket(utc_hour: float) -> str:
    """Сессия по UTC-часу (крипто 24/7, но ликвидность сессионная)."""
    h = int(utc_hour) % 24
    if 0 <= h < 8:
        return "asia"
    if 8 <= h < 13:
        return "europe"
    if 13 <= h < 21:
        return "us"
    return "asia_pm"


def _cvd_slope(samples) -> float | None:
    """Наклон CVD по времени (least-squares). CvdSample = (ts, price, cvd).
    None если <2 точек или нулевой разброс времени. Сырые единицы контрактов/
    сек — кросс-символьно не сравнивать напрямую, нормировать в анализе."""
    if not samples or len(samples) < 2:
        return None
    xs = [float(getattr(s, "ts", None)) for s in samples]
    ys = [float(getattr(s, "cvd", None)) for s in samples]
    if any(v is None for v in xs) or any(v is None for v in ys):
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


@dataclass
class _Sources:
    """Тонкая обёртка, чтобы не тащить в хелпер тяжёлые импорты типов."""
    snap: object
    htf: object | None
    key_levels: object | None
    now: float


def compute_regime_features(snap, htf=None, key_levels=None, now: float = 0.0
                            ) -> dict:
    """Считает regime-фичи из snap + htf + key_levels. Pure, без side-effect.

    Любая缺失ная фича → None (fail-soft: анализ просто игнорирует null).
    Возвращает dict с фиксированным набором ключей (для схемы БД).
    """
    sym = getattr(snap, "symbol", None)
    last = getattr(snap, "last_price", None)
    bb = getattr(snap, "best_bid", None)
    ba = getattr(snap, "best_ask", None)
    imb = getattr(snap, "ob_imbalance", None)
    fund = getattr(snap, "funding_rate", None)
    cvd = getattr(snap, "cvd_samples", None) or []
    liqs = getattr(snap, "liq_events", None) or []

    # spread (bps) — реальный кост скальпа
    spread_bps = None
    if bb is not None and ba is not None and last and last > 0:
        mid = (bb + ba) / 2.0
        if mid > 0:
            spread_bps = (ba - bb) / mid * 1e4

    # funding (bps 8h) — raw fraction × 1e4
    funding_bps = None
    if fund is not None:
        funding_bps = fund * 1e4

    # regime из kline-кэшей
    adx = None
    if htf is not None:
        adx = htf.trend_strength(sym) if hasattr(htf, "trend_strength") else None
    regime_ratio = None
    day_range_pct = None
    dist_high_pct = None
    dist_low_pct = None
    if key_levels is not None:
        regime_ratio = key_levels.regime_ratio(sym) \
            if hasattr(key_levels, "regime_ratio") else None
        lv = key_levels.levels(sym) if hasattr(key_levels, "levels") else None
        if lv is not None and last and last > 0:
            dh = lv.get("day_high")
            dl = lv.get("day_low")
            if dh is not None and dl is not None and dh > dl:
                day_range_pct = (dh - dl) / last * 100.0
                dist_high_pct = (dh - last) / last * 100.0
                dist_low_pct = (last - dl) / last * 100.0

    # session по UTC
    ts = getattr(snap, "ts", None)
    if ts is None:
        ts = now
    session = _session_bucket((ts / 3600.0) % 24) if ts is not None else None

    return {
        "adx": adx,
        "regime_ratio": regime_ratio,
        "day_range_pct": day_range_pct,
        "dist_high_pct": dist_high_pct,
        "dist_low_pct": dist_low_pct,
        "spread_bps": spread_bps,
        "ob_imbalance": imb,
        "funding_bps": funding_bps,
        "cvd_slope": _cvd_slope(cvd),
        "liq_count": len(liqs),
        "session": session,
    }


# Порядок колонок = порядок значений в dict (для INSERT в regime_features).
REGIME_COLUMNS = (
    "adx", "regime_ratio", "day_range_pct", "dist_high_pct", "dist_low_pct",
    "spread_bps", "ob_imbalance", "funding_bps", "cvd_slope", "liq_count",
    "session",
)
