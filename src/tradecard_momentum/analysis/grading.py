"""Грейдинг сделок по силе сигнала входа (канон §7, TASKSPEC §5).

У momentum-бота нет дискретного ``score``; естественный аналог «силы сетапа» —
**магнитуда сигнала входа** ``|momentum_value|`` (TSMOM: чем дальше momentum от
порога, тем сильнее тренд-импульс на входе; Moskowitz/Ooi/Pedersen 2012). Маппинг
в A+/A/B/C — **квантильный** (по распределению), НЕ подогнан под P&L
(no-data-fitting.mdc). Задача — аналитика: построить кривую «сила сигнала →
WR/EXP» и проверить **монотонность** (сильнее сигнал → лучше). Если сила сигнала
не отделяет винов — это детектор ``signal_not_predictive`` (§4).

Риск-аллокация канона (80/30/15/5%) НЕ применяется автоматически — это лишь
референс в отчёте (риск-модель бота фиксирована, меняется только с одобрения).
"""
from __future__ import annotations

from dataclasses import dataclass

from tradecard_momentum.analysis.stats import spearman_rho
from tradecard_momentum.analysis.trade import (MomentumTrade, decided,
                                               expectancy_r, net_pnl, win_rate)

_GRADE_LABELS_DESC = ["A+", "A", "B", "C", "D", "E"]
GRADE_RISK_REF = {"A+": "до 80%", "A": "30%", "B": "15%", "C": "5%"}


@dataclass
class GradeBucket:
    label: str
    score_min: float
    score_max: float
    n: int
    wins: int
    losses: int
    wr: float
    exp_r: float | None
    net: float

    @property
    def rank(self) -> int:
        return len(_GRADE_LABELS_DESC) - 1 - _GRADE_LABELS_DESC.index(self.label)


@dataclass
class GradeCurve:
    buckets: list[GradeBucket]
    rho: float | None
    monotonic: bool

    @property
    def predictive(self) -> bool:
        return self.monotonic


def _graded(trades: list[MomentumTrade]) -> list[MomentumTrade]:
    """decided-сделки с известной силой сигнала И валидным R (для грейда)."""
    return [t for t in decided(trades)
            if t.signal_momentum is not None and t.r_multiple is not None]


def _quantile_thresholds(scores: list[float], k: int) -> list[float]:
    uniq = sorted(set(scores))
    if len(uniq) <= k:
        return uniq[:-1]
    srt = sorted(scores)
    thr: list[float] = []
    for i in range(1, k):
        idx = int(round(i / k * (len(srt) - 1)))
        thr.append(srt[idx])
    out: list[float] = []
    for t in thr:
        if not out or t > out[-1]:
            out.append(t)
    return out


def _bucket_index(score: float, thresholds: list[float]) -> int:
    idx = 0
    for t in thresholds:
        if score > t:
            idx += 1
        else:
            break
    return idx


def grade_curve(trades: list[MomentumTrade], *, buckets: int = 4,
                min_rho: float = 0.5) -> GradeCurve | None:
    """Кривая «сила сигнала → перформанс» + проверка монотонности (детектор §5)."""
    dd = _graded(trades)
    if len(dd) < buckets:
        return None
    scores = [float(t.signal_momentum) for t in dd]  # type: ignore[arg-type]
    thresholds = _quantile_thresholds(scores, buckets)
    n_buckets = len(thresholds) + 1
    if n_buckets < 2:
        return None
    groups: dict[int, list[MomentumTrade]] = {i: [] for i in range(n_buckets)}
    for t in dd:
        groups[_bucket_index(float(t.signal_momentum), thresholds)].append(t)

    labels_for_index: dict[int, str] = {}
    for i in range(n_buckets):
        from_top = n_buckets - 1 - i
        labels_for_index[i] = (_GRADE_LABELS_DESC[from_top]
                               if from_top < len(_GRADE_LABELS_DESC)
                               else f"G{from_top}")

    out: list[GradeBucket] = []
    for i in range(n_buckets):
        grp = groups[i]
        if not grp:
            continue
        sc = [float(t.signal_momentum) for t in grp]
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
    return GradeCurve(buckets=out, rho=rho, monotonic=monotonic)
