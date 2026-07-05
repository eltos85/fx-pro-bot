"""«ёрш»-сканер: повторяющиеся прострелы от genuine/iceberg density (M5).

Для серии прострелов одного символа — три проверки (аудит п.1, признаки 1–3):
  (а) триггер-принты из одного кластера размеров («одинаковый принт»);
  (б) repeat-frequency test: интервалы между прострелами против Пуассон-нуля,
      p-value < 0.05 (регулярность выше случайной);
  (в) прострел стартует от уровня genuine/iceberg density (join с densities).
Прошедшие все три → ``candidates``. ВСЕ прострелы → ``spurt_events``
(passed_filters 0/1) — для калибровки M6.

─── Research basis ───
- Repeat-frequency test: дисперсионный (dispersion) тест для Пуассона.
  Для Пуассон-процесса интервалы экспоненциальны → дисперсия интервалов ≈
  среднее (index of dispersion D ≈ 1). Регулярные интервалы → D << 1
  (under-dispersion). Статистика ``Q = (n-1)·s²/x̄ ~ chi²(n-1)`` under H0
  (Cox & Lewis 1966 «The Statistical Analysis of Series of Events», ch.6;
  интервальный дисперсионный тест). p-value = ``P(Q ≤ q)`` (lower tail) —
  малое p = регулярность выше случайной. Реализация chi2-CDF — stdlib через
  regularized lower incomplete gamma (Numerical Recipes gammp), без scipy.
- Привязка к density: прострел стартует по цене рядом с genuine/iceberg
  плотностью, активной на момент старта (аудит п.1, признак 3 + п.2 bridge).

Пороги ``price_tol_pct``, минимальное число прострелов для теста — стартовые
точки (``no-data-fitting.mdc``), калибруются M6.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass

from yorsh_bot.analysis.prints import Spurt
from yorsh_bot.state.db import YorshDB

log = logging.getLogger("yorsh_bot.scanner")

# Минимальное число прострелов для repeat-frequency test (нужно ≥3 интервалов).
MIN_SPURTS_FOR_TEST = 4
# Допуск по цене для привязки к density (% от цены старта) — стартовая точка.
PRICE_TOL_PCT = 0.5
# variance «одинакового принта» для проверки (а) — доля кластера в триггерах.
SAME_PRINT_CLUSTER_FRAC = 0.5


# ─── chi2 CDF via regularized lower incomplete gamma (Numerical Recipes) ──

def _gammp(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x) = γ(a,x)/Γ(a)."""
    if x < 0 or a <= 0:
        return 0.0
    if x == 0:
        return 0.0
    if x < a + 1.0:
        # series
        ap = a
        s = 1.0 / a
        d = s
        for _ in range(200):
            ap += 1
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-14:
                break
        return s * math.exp(-x + a * math.log(x) - math.lgamma(a))
    # continued fraction (Lentz)
    b = x + 1.0 - a
    c = 1e300
    d = 1.0 / b
    h = d
    for i in range(1, 200):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < 1e-300:
            d = 1e-300
        c = b + an / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    q = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
    return 1.0 - q   # P(a,x) = 1 - Q(a,x)


def chi2_cdf(x: float, k: int) -> float:
    """CDF хи-квадрат с k df: P(X ≤ x) = gammp(k/2, x/2)."""
    return _gammp(k / 2.0, x / 2.0)


def repeat_frequency_pvalue(spurt_ts: list[float]) -> float | None:
    """p-value дисперсионного теста интервалов против Пуассон-нуля.

    Малое p (<0.05) = интервалы TOO REGULAR (under-dispersion) = прострелы
    регулярнее случайного = «ёрш». Большое p = согласуется с Пуассоном.
    ``None`` если недостаточно интервалов (<3).
    """
    if len(spurt_ts) < MIN_SPURTS_FOR_TEST:
        return None
    ts = sorted(spurt_ts)
    intervals = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    n = len(intervals)
    if n < 3:
        return None
    mean = statistics.fmean(intervals)
    var = statistics.variance(intervals)   # sample variance (n-1)
    if mean <= 0:
        return None
    Q = (n - 1) * var / mean     # ~ chi2(n-1) under H0
    # lower tail: регулярность = малое Q → p = P(Q ≤ q)
    return chi2_cdf(Q, n - 1)


# ─── scanner ─────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    """Результат проверки серии прострелов одного символа."""
    exchange: str
    symbol: str
    passed: bool
    regularity_pvalue: float | None
    print_cluster_size: float | None
    density_ids: list[int]
    spurts_per_day: float
    n_spurts: int


class YorshScanner:
    """Сканер «ёрш»-паттернов по серии прострелов символа.

    ``evaluate(exchange, symbol, spurts, day_span_sec)`` — проверяет (а)(б)(в),
    пишет ВСЕ прострелы в ``spurt_events`` (passed_filters=1/0), при passing —
    upsert в ``candidates``. Возвращает ``ScanResult``.
    """

    def __init__(self, db: YorshDB, *,
                 price_tol_pct: float = PRICE_TOL_PCT,
                 same_print_frac: float = SAME_PRINT_CLUSTER_FRAC) -> None:
        self.db = db
        self.price_tol_pct = price_tol_pct
        self.same_print_frac = same_print_frac

    def evaluate(self, exchange: str, symbol: str,
                 spurts: list[Spurt], day_span_sec: float) -> ScanResult:
        # (а) «одинаковый принт»: ≥1 кластер размеров покрывает ≥frac триггеров
        #   (по всем прострелам суммарно)
        all_triggers = [p for sp in spurts for p in sp.trigger_prints]
        cluster_size: float | None = None
        same_print_ok = False
        if all_triggers:
            from yorsh_bot.analysis.prints import cluster_prints_by_size
            clusters = cluster_prints_by_size(all_triggers)
            biggest = max(clusters, key=len)
            cluster_size = statistics.median(p.size for p in biggest)
            same_print_ok = len(biggest) >= self.same_print_frac * len(all_triggers)

        # (б) repeat-frequency test
        p = repeat_frequency_pvalue([sp.ts for sp in spurts])
        regular_ok = p is not None and p < 0.05

        # (в) привязка к genuine/iceberg density
        density_ids: list[int] = []
        density_ok = False
        for sp in spurts:
            tol = sp.start_price * self.price_tol_pct / 100.0
            rows = self.db.densities_near(
                exchange, symbol, sp.start_price,
                ts_before=sp.ts, price_tol=tol,
                verdict_in=("genuine", "iceberg"))
            if rows:
                density_ids.append(rows[0]["id"])
                density_ok = True

        passed = bool(same_print_ok and regular_ok and density_ok
                      and len(spurts) >= MIN_SPURTS_FOR_TEST)

        # пишем ВСЕ прострелы
        for sp in spurts:
            did = density_ids[0] if density_ids else None
            self.db.insert_spurt(
                exchange=exchange, symbol=symbol, ts=sp.ts,
                direction=sp.direction, amplitude_pct=sp.amplitude_pct,
                duration_ms=sp.duration_ms,
                trigger_print_size=sp.trigger_cluster_size,
                density_id=did, passed_filters=1 if passed else 0)

        spurts_per_day = len(spurts) / (day_span_sec / 86400.0) if day_span_sec > 0 else 0.0
        if passed:
            self.db.upsert_candidate(
                exchange=exchange, symbol=symbol,
                first_detected=spurts[0].ts, last_detected=spurts[-1].ts,
                spurts_per_day=spurts_per_day,
                regularity_pvalue=p, print_cluster_size=cluster_size)
            log.info("ёрш-candidate: %s %s p=%.4f spurts/day=%.2f",
                     exchange, symbol, p or -1, spurts_per_day)

        return ScanResult(exchange, symbol, passed, p, cluster_size,
                          density_ids, spurts_per_day, len(spurts))
