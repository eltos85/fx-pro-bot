"""Small wins / momentum tracking с жёстким OOS-гейтом (канон §6, TASKSPEC §7).

Анти-overfit инвариант: tradecard **не вправе** объявить small win по in-sample
улучшению. Победа засчитывается ТОЛЬКО когда:
  (а) гипотеза была одобрена человеком и **внедрена** (проставлена
      ``implemented_week`` — это делает оператор, не tradecard);
  (б) на **forward/OOS** выборке ПОСЛЕ внедрения частота темы значимо снизилась
      (sample-size.mdc: ≥100 сделок, ≥2 недели, p<0.05).
До внедрения — ГИПОТЕЗА; после, но до порога — НАБЛЮДЕНИЕ; только пройдя порог —
SMALL WIN. momentum = накопленное число OOS-подтверждённых побед.
"""
from __future__ import annotations

from dataclasses import dataclass

from tradecard_momentum.analysis.stats import two_proportion_test
from tradecard_momentum.state.db import TradecardDB


@dataclass
class SmallWinCheck:
    hypothesis_id: int
    theme_id: int
    status: str             # "no_baseline" | "observation" | "small_win" | "no_change"
    baseline_freq: float
    oos_freq: float
    p_value: float | None
    n_oos: int
    weeks_oos: int
    detail: str


def evaluate_small_win(db: TradecardDB, *, hypothesis_id: int, theme_id: int,
                       mode: str, implemented_week: str,
                       min_trades: int, min_weeks: int,
                       significance_p: float) -> SmallWinCheck:
    """OOS-проверка одной внедрённой гипотезы по истории частот темы.

    Baseline = недели ДО внедрения; OOS = недели С/ПОСЛЕ внедрения. Снижение
    частоты паттерна должно быть статистически значимым (two-proportion test).
    """
    rows = db.freq_history(theme_id, mode)
    pre = [r for r in rows if r["week"] < implemented_week]
    post = [r for r in rows if r["week"] >= implemented_week]

    pre_pat = sum(int(r["n_pattern"]) for r in pre)
    pre_n = sum(int(r["n_trades"]) for r in pre)
    post_pat = sum(int(r["n_pattern"]) for r in post)
    post_n = sum(int(r["n_trades"]) for r in post)
    base_freq = (pre_pat / pre_n * 100.0) if pre_n else 0.0
    oos_freq = (post_pat / post_n * 100.0) if post_n else 0.0
    weeks_oos = len(post)

    if pre_n == 0:
        return SmallWinCheck(hypothesis_id, theme_id, "no_baseline", base_freq,
                             oos_freq, None, post_n, weeks_oos,
                             "нет baseline-частоты до внедрения")

    if post_n < min_trades or weeks_oos < min_weeks:
        return SmallWinCheck(hypothesis_id, theme_id, "observation", base_freq,
                             oos_freq, None, post_n, weeks_oos,
                             f"OOS пока мала (n={post_n}<{min_trades} или "
                             f"недель {weeks_oos}<{min_weeks}) — НАБЛЮДЕНИЕ")

    test = two_proportion_test(pre_pat, pre_n, post_pat, post_n)
    pval = test.p_value if test else None
    if test and test.diff < 0 and test.p_value < significance_p:
        return SmallWinCheck(hypothesis_id, theme_id, "small_win", base_freq,
                             oos_freq, pval, post_n, weeks_oos,
                             f"частота {base_freq:.1f}→{oos_freq:.1f}/100 "
                             f"(p={pval:.3f}<{significance_p}) — SMALL WIN")
    pstr = f"{pval:.3f}" if pval is not None else "n/a"
    return SmallWinCheck(hypothesis_id, theme_id, "no_change", base_freq,
                         oos_freq, pval, post_n, weeks_oos,
                         f"частота {base_freq:.1f}→{oos_freq:.1f}/100 "
                         f"(p={pstr}) — значимого снижения нет")
