"""Детекторы «ошибок системы» (таксономия TASKSPEC §4, канон §4).

«Ошибка» детерминированного бота — НЕ психология, а **повторяющийся убыточный
паттерн правил** (страта × режим × сессия × символ × score-бакет). Каждый
детектор — наблюдатель над ``trades`` (+ опц. post-exit MFE), помечает срезы
кодом паттерна для агрегации. Пороги **нейтральные/относительные/структурные**,
НЕ под желаемый P&L (no-data-fitting.mdc). Запланированный SL ≠ ошибка; ошибка =
паттерн на достаточной выборке (sample-size.mdc — гейт «темы» в движке отчёта).

Детекторы возвращают наблюдения; решение об отключении/удалении фактора — всегда
человеку (strategy-guard.mdc). Список расширяемый.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from tradecard_bybit.analysis.grading import grade_curve
from tradecard_bybit.analysis.trade import (Trade, decided, expectancy_r,
                                            net_pnl, win_rate)


@dataclass
class PatternFinding:
    code: str                       # код паттерна (§4)
    bot: str
    mode: str                       # paper | live | mixed (paper_live)
    strategy: str | None
    scope: dict                     # срез (symbol/session/side/factor/...)
    n: int                          # сделок в срезе
    wr: float
    exp_r: float | None
    net: float
    detail: str                     # человекочитаемое объяснение
    trade_ids: list[int] = field(default_factory=list)

    def scope_key(self) -> str:
        import json
        return json.dumps(self.scope, sort_keys=True, ensure_ascii=False)


def _by_strategy(trades: list[Trade]) -> dict[str, list[Trade]]:
    out: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        out[t.strategy].append(t)
    return out


# ─── 1. grade_not_predictive ─────────────────────────────────────────────

def detect_grade_not_predictive(trades: list[Trade], *, bot: str, mode: str,
                                buckets: int, min_rho: float,
                                min_trades: int) -> list[PatternFinding]:
    """Score-бакеты НЕ монотонны по EXP (высокий score не отделяет винов, §5)."""
    out: list[PatternFinding] = []
    for strat, grp in _by_strategy(trades).items():
        dd = decided(grp)
        if len(dd) < min_trades:
            continue
        curve = grade_curve(dd, buckets=buckets, min_rho=min_rho, strategy=strat)
        if curve is None or curve.predictive:
            continue
        bucket_str = "; ".join(
            f"{b.label}[{b.score_min}-{b.score_max}] EXP={b.exp_r:.2f} n={b.n}"
            for b in curve.buckets if b.exp_r is not None)
        out.append(PatternFinding(
            code="grade_not_predictive", bot=bot, mode=mode, strategy=strat,
            scope={"strategy": strat}, n=len(dd), wr=win_rate(dd),
            exp_r=expectancy_r(dd), net=net_pnl(dd),
            detail=(f"score не предиктивен (Spearman ρ="
                    f"{curve.rho if curve.rho is not None else float('nan'):.2f}"
                    f" < {min_rho}): {bucket_str}"),
            trade_ids=[t.id for t in dd]))
    return out


# ─── 2. strategy_regime_leak ─────────────────────────────────────────────

def detect_strategy_regime_leak(trades: list[Trade], *, bot: str, mode: str,
                                min_trades: int) -> list[PatternFinding]:
    """Страта системно убыточна в срезе (symbol / session) при общем плюсе."""
    out: list[PatternFinding] = []
    for strat, grp in _by_strategy(trades).items():
        dd = decided(grp)
        overall_exp = expectancy_r(dd)
        if len(dd) < min_trades or overall_exp is None or overall_exp <= 0:
            continue  # «утечка режима» осмысленна только при общем плюсе страты
        for dim in ("symbol", "session"):
            slices: dict[str, list[Trade]] = defaultdict(list)
            for t in dd:
                slices[getattr(t, dim) if dim == "session" else t.symbol].append(t)
            for key, sl_trades in slices.items():
                if len(sl_trades) < min_trades:
                    continue
                exp = expectancy_r(sl_trades)
                if exp is not None and exp < 0:
                    out.append(PatternFinding(
                        code="strategy_regime_leak", bot=bot, mode=mode,
                        strategy=strat, scope={"strategy": strat, dim: key},
                        n=len(sl_trades), wr=win_rate(sl_trades), exp_r=exp,
                        net=net_pnl(sl_trades),
                        detail=(f"{strat} в срезе {dim}={key} EXP={exp:.2f} "
                                f"(<0) при общем EXP страты {overall_exp:.2f}"),
                        trade_ids=[t.id for t in sl_trades]))
    return out


# ─── 3. sl_cluster ───────────────────────────────────────────────────────

def detect_sl_cluster(trades: list[Trade], *, bot: str, mode: str,
                      factor: float, min_trades: int) -> list[PatternFinding]:
    """Повтор sl_hit на связке (symbol×side×strategy) выше базовой частоты."""
    out: list[PatternFinding] = []
    for strat, grp in _by_strategy(trades).items():
        dd = decided(grp)
        if not dd:
            continue
        base_rate = sum(1 for t in dd if t.close_reason == "sl_hit") / len(dd)
        if base_rate <= 0:
            continue
        bind: dict[tuple, list[Trade]] = defaultdict(list)
        for t in dd:
            bind[(t.symbol, t.side)].append(t)
        for (sym, side), bt in bind.items():
            if len(bt) < min_trades:
                continue
            rate = sum(1 for t in bt if t.close_reason == "sl_hit") / len(bt)
            if rate >= factor * base_rate:
                out.append(PatternFinding(
                    code="sl_cluster", bot=bot, mode=mode, strategy=strat,
                    scope={"strategy": strat, "symbol": sym, "side": side},
                    n=len(bt), wr=win_rate(bt), exp_r=expectancy_r(bt),
                    net=net_pnl(bt),
                    detail=(f"{strat} {sym} {side}: SL-доля {rate:.0%} ≥ "
                            f"{factor}× базовой {base_rate:.0%}"),
                    trade_ids=[t.id for t in bt]))
    return out


# ─── 4. exit_left_money ──────────────────────────────────────────────────

def detect_exit_left_money(trades: list[Trade], *, bot: str, mode: str,
                           factor: float, min_trades: int,
                           mfe_fn: Callable[[Trade], float | None] | None,
                           ) -> list[PatternFinding]:
    """Выход систематически до значимого продолжения (MFE_after ≫ реализ., §4).

    Свойство **правила выхода**, не психологии. ``mfe_fn`` отдаёт благоприятный
    ход (в цене) ПОСЛЕ выхода (Sweeney 1988 MFE); если None — детектор молчит
    (post-exit klines отключены). Считаем по выходам tp_hit/flow_exit.
    """
    if mfe_fn is None:
        return []
    out: list[PatternFinding] = []
    exit_reasons = {"tp_hit", "flow_exit"}
    for strat, grp in _by_strategy(trades).items():
        cand = [t for t in decided(grp)
                if (t.close_reason or "") in exit_reasons and t.exit is not None]
        ratios: list[float] = []
        ids: list[int] = []
        for t in cand:
            mfe = mfe_fn(t)
            if mfe is None or mfe <= 0:
                continue
            realized = abs((t.exit or t.entry) - t.entry)
            if realized <= 0:
                continue
            ratios.append(mfe / realized)
            ids.append(t.id)
        if len(ratios) < min_trades:
            continue
        med = statistics.median(ratios)
        if med >= factor:
            out.append(PatternFinding(
                code="exit_left_money", bot=bot, mode=mode, strategy=strat,
                scope={"strategy": strat}, n=len(ratios),
                wr=win_rate(cand), exp_r=expectancy_r(cand), net=net_pnl(cand),
                detail=(f"{strat}: медиана post-exit MFE/реализ.хода {med:.1f}× "
                        f"≥ {factor}× (выход рано на {len(ratios)} сделках)"),
                trade_ids=ids))
    return out


# ─── 5. factor_noise ─────────────────────────────────────────────────────

def detect_factor_noise(trades: list[Trade], *, bot: str, mode: str,
                        max_exp_frac: float, min_trades: int,
                        ) -> list[PatternFinding]:
    """Токен reasons не улучшает EXP (присутствие ≈ отсутствие) → кандидат на
    удаление (родная практика проекта: scalp v0.9.0 убрал funding/liq). Вывод об
    удалении — человеку (strategy-guard)."""
    out: list[PatternFinding] = []
    for strat, grp in _by_strategy(trades).items():
        dd = decided(grp)
        if len(dd) < 2 * min_trades:
            continue
        strat_exp = expectancy_r(dd)
        if strat_exp is None or strat_exp == 0:
            continue
        all_factors: set[str] = set()
        for t in dd:
            all_factors.update(t.factors)
        for factor in sorted(all_factors):
            withf = [t for t in dd if factor in t.factors]
            without = [t for t in dd if factor not in t.factors]
            if len(withf) < min_trades or len(without) < min_trades:
                continue
            ew = expectancy_r(withf)
            eo = expectancy_r(without)
            if ew is None or eo is None:
                continue
            if abs(ew - eo) <= max_exp_frac * abs(strat_exp):
                out.append(PatternFinding(
                    code="factor_noise", bot=bot, mode=mode, strategy=strat,
                    scope={"strategy": strat, "factor": factor},
                    n=len(withf), wr=win_rate(withf), exp_r=ew, net=net_pnl(withf),
                    detail=(f"{strat}: фактор '{factor}' EXP_с={ew:.2f} ≈ "
                            f"EXP_без={eo:.2f} (Δ≤{max_exp_frac:.0%} от "
                            f"|EXP страты|) — кандидат factor-noise"),
                    trade_ids=[t.id for t in withf]))
    return out


# ─── 6. overtrading ──────────────────────────────────────────────────────

def detect_overtrading(trades: list[Trade], *, bot: str, mode: str,
                       spike_factor: float, min_trades: int,
                       strategy: str | None = None,
                       ) -> list[PatternFinding]:
    """Всплеск числа сделок при падении EXP («горячие» часы хуже спокойных).

    Группируем по календарному часу (ts_open//3600). «Горячий» час = число
    сделок ≥ spike_factor × медианы по активным часам (self-нормировка). Если
    EXP горячих < EXP спокойных и горячих ≥ min — наблюдение. ``strategy`` —
    метка среза (движок зовёт per-strategy; trades уже отфильтрованы)."""
    dd = decided(trades)
    if len(dd) < min_trades:
        return []
    by_hour: dict[int, list[Trade]] = defaultdict(list)
    for t in dd:
        by_hour[int(t.ts_open // 3600)].append(t)
    counts = [len(v) for v in by_hour.values()]
    if len(counts) < 2:
        return []
    med = statistics.median(counts)
    if med <= 0:
        return []
    hot: list[Trade] = []
    calm: list[Trade] = []
    for v in by_hour.values():
        (hot if len(v) >= spike_factor * med else calm).extend(v)
    if len(hot) < min_trades or not calm:
        return []
    hot_exp = expectancy_r(hot)
    calm_exp = expectancy_r(calm)
    if hot_exp is None or calm_exp is None or hot_exp >= calm_exp:
        return []
    scope = {"spike_factor": spike_factor}
    if strategy is not None:
        scope["strategy"] = strategy
    return [PatternFinding(
        code="overtrading", bot=bot, mode=mode, strategy=strategy,
        scope=scope,
        n=len(hot), wr=win_rate(hot), exp_r=hot_exp, net=net_pnl(hot),
        detail=(f"перегретые часы (≥{spike_factor}× медианы {med:.0f} сделок/ч): "
                f"EXP={hot_exp:.2f} хуже спокойных EXP={calm_exp:.2f}"),
        trade_ids=[t.id for t in hot])]


# ─── 7. big_game_hunting ─────────────────────────────────────────────────

def detect_big_game_hunting(trades: list[Trade], *, bot: str, mode: str,
                            max_top_share: float, min_trades: int,
                            buckets: int, strategy: str | None = None,
                            ) -> list[PatternFinding]:
    """Дрейф к редкому high-score (A+) при том, что baseline даёт momentum (§8).

    Наблюдение: top-грейд РЕДОК (доля < max_top_share) И не превосходит baseline
    по EXP — значит гнаться за A+ в ущерб baseline-страте = big-game-hunting
    (канон §8: «вернись к baseline»). Структурный, без подгонки P&L. ``strategy``
    — метка среза (движок зовёт per-strategy; trades уже отфильтрованы).
    """
    dd = decided(trades)
    if len(dd) < min_trades:
        return []
    curve = grade_curve(dd, buckets=buckets, min_rho=0.0)
    if curve is None or len(curve.buckets) < 2:
        return []
    top = max(curve.buckets, key=lambda b: b.rank)
    base = [b for b in curve.buckets if b.rank < top.rank]
    base_exp = expectancy_r([t for t in dd
                             if any(b.score_min <= t.score <= b.score_max
                                    for b in base)])
    top_share = top.n / len(dd)
    if (top_share < max_top_share and top.exp_r is not None
            and base_exp is not None and base_exp > 0
            and top.exp_r <= base_exp):
        scope = {"top_grade": top.label}
        if strategy is not None:
            scope["strategy"] = strategy
        return [PatternFinding(
            code="big_game_hunting", bot=bot, mode=mode, strategy=strategy,
            scope=scope,
            n=top.n, wr=top.wr, exp_r=top.exp_r, net=top.net,
            detail=(f"top-грейд {top.label} редок (доля {top_share:.0%}) и не "
                    f"бьёт baseline по EXP ({top.exp_r:.2f} ≤ {base_exp:.2f}) — "
                    f"baseline остаётся momentum-движком"),
            trade_ids=[])]
    return []


# ─── 8. paper_live_divergence ────────────────────────────────────────────

def detect_paper_live_divergence(trades_all_modes: list[Trade], *, bot: str,
                                 min_trades: int) -> list[PatternFinding]:
    """Связка валидна на paper (EXP>0), но системно проигрывает на live (EXP<0).

    Работает на ОБОИХ режимах сразу (mode='mixed'). Связка = (strategy, symbol).
    """
    out: list[PatternFinding] = []
    bind: dict[tuple, dict[str, list[Trade]]] = defaultdict(
        lambda: {"paper": [], "live": []})
    for t in decided(trades_all_modes):
        if t.mode in ("paper", "live"):
            bind[(t.strategy, t.symbol)][t.mode].append(t)
    for (strat, sym), modes in bind.items():
        paper, live = modes["paper"], modes["live"]
        if len(paper) < min_trades or len(live) < min_trades:
            continue
        pe = expectancy_r(paper)
        le = expectancy_r(live)
        if pe is not None and le is not None and pe > 0 and le < 0:
            out.append(PatternFinding(
                code="paper_live_divergence", bot=bot, mode="mixed",
                strategy=strat, scope={"strategy": strat, "symbol": sym},
                n=len(live), wr=win_rate(live), exp_r=le, net=net_pnl(live),
                detail=(f"{strat} {sym}: paper EXP={pe:.2f} (>0) vs live "
                        f"EXP={le:.2f} (<0) — расхождение paper/live"),
                trade_ids=[t.id for t in live]))
    return out
