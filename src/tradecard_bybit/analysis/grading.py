"""Грейдинг сделок по полю ``score`` (канон §7, TASKSPEC §5).

Грейд берётся НАПРЯМУЮ из ``score`` (его уже посчитал бот на входе). Маппинг
score-бакетов → A+/A/B/C — **квантильный** (по распределению), а НЕ подогнанный
под P&L (no-data-fitting.mdc). Задача — аналитика: построить кривую
«грейд → WR/EXP/avgR» и проверить **монотонность** (выше грейд → лучше). Если
score не отделяет винов — это детектор ``grade_not_predictive`` (тема №1, §4).

Риск-аллокация канона (80/30/15/5%) НЕ применяется автоматически — это лишь
референс в отчёте (риск-модель ботов фиксирована, меняется только с одобрения).
"""
from __future__ import annotations

from dataclasses import dataclass

from tradecard_bybit.analysis.stats import spearman_rho
from tradecard_bybit.analysis.trade import (Trade, decided, expectancy_r,
                                            net_pnl, win_rate)

# Метки грейдов сверху вниз (канон §7). Для k бакетов берём первые k снизу.
_GRADE_LABELS_DESC = ["A+", "A", "B", "C", "D", "E"]
# Референс-аллокация канона дневного стопа (НЕ применяется — только показываем).
GRADE_RISK_REF = {"A+": "до 80%", "A": "30%", "B": "15%", "C": "5%"}


@dataclass
class GradeBucket:
    label: str            # A+/A/B/C
    score_min: int
    score_max: int
    n: int
    wins: int
    losses: int
    wr: float
    exp_r: float | None   # средний R (EXP)
    net: float

    @property
    def rank(self) -> int:
        """Ранг качества грейда: выше = лучше (A+ максимум, C/D минимум).

        ``_GRADE_LABELS_DESC`` идёт от лучшего к худшему (индекс 0 = A+), поэтому
        качество = инверсия индекса. Используется для Spearman(rank, EXP):
        предиктивная кривая ⇒ положительный ρ (выше грейд → выше EXP).
        """
        return len(_GRADE_LABELS_DESC) - 1 - _GRADE_LABELS_DESC.index(self.label)


@dataclass
class GradeCurve:
    buckets: list[GradeBucket]
    rho: float | None       # Spearman(rank, EXP): монотонность грейда
    monotonic: bool         # rho ≥ порога настройки
    strategy: str | None

    @property
    def predictive(self) -> bool:
        return self.monotonic


def _quantile_thresholds(scores: list[int], k: int) -> list[int]:
    """Границы k квантильных бакетов по распределению score (верхние границы
    бакетов снизу вверх, без последней). Если уникальных score ≤ k — границы по
    уникальным значениям (бакет = значение)."""
    uniq = sorted(set(scores))
    if len(uniq) <= k:
        # каждый уникальный score — свой бакет; границы между соседями
        return uniq[:-1]
    srt = sorted(scores)
    thr: list[int] = []
    for i in range(1, k):
        idx = int(round(i / k * (len(srt) - 1)))
        thr.append(srt[idx])
    # убрать дубликаты границ (сжатые распределения)
    out: list[int] = []
    for t in thr:
        if not out or t > out[-1]:
            out.append(t)
    return out


def _bucket_index(score: int, thresholds: list[int]) -> int:
    """Индекс бакета снизу (0) вверх по границам (≤ thr попадает ниже)."""
    idx = 0
    for t in thresholds:
        if score > t:
            idx += 1
        else:
            break
    return idx


def grade_curve(trades: list[Trade], *, buckets: int = 4,
                min_rho: float = 0.5, strategy: str | None = None) -> GradeCurve | None:
    """Кривая грейд→перформанс + проверка монотонности (детектор §5).

    Берём только decided-сделки с валидным R. Бакетим score по квантилям,
    считаем WR/EXP/net на бакет, оцениваем Spearman(rank, EXP).
    """
    dd = [t for t in decided(trades) if t.r_multiple is not None]
    if len(dd) < buckets:
        return None
    scores = [t.score for t in dd]
    thresholds = _quantile_thresholds(scores, buckets)
    n_buckets = len(thresholds) + 1
    if n_buckets < 2:
        return None  # вырожденное распределение score — грейдить нечего
    groups: dict[int, list[Trade]] = {i: [] for i in range(n_buckets)}
    for t in dd:
        groups[_bucket_index(t.score, thresholds)].append(t)

    # метки: верхний индекс = A+, ниже — по списку DESC
    labels_for_index: dict[int, str] = {}
    for i in range(n_buckets):
        # i=0 низший → последняя из первых n_buckets меток; i=top → A+
        from_top = n_buckets - 1 - i
        labels_for_index[i] = _GRADE_LABELS_DESC[from_top] if from_top < len(_GRADE_LABELS_DESC) else f"G{from_top}"

    out: list[GradeBucket] = []
    for i in range(n_buckets):
        grp = groups[i]
        if not grp:
            continue
        sc = [t.score for t in grp]
        out.append(GradeBucket(
            label=labels_for_index[i], score_min=min(sc), score_max=max(sc),
            n=len(grp), wins=sum(1 for t in grp if t.is_win),
            losses=sum(1 for t in grp if t.is_loss), wr=win_rate(grp),
            exp_r=expectancy_r(grp), net=net_pnl(grp)))

    ranks = [b.rank for b in out if b.exp_r is not None]
    exps = [b.exp_r for b in out if b.exp_r is not None]
    rho = spearman_rho([float(r) for r in ranks], [float(e) for e in exps]) \
        if len(ranks) >= 2 else None
    monotonic = rho is not None and rho >= min_rho
    return GradeCurve(buckets=out, rho=rho, monotonic=monotonic,
                      strategy=strategy)
