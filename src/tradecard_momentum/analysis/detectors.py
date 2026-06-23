"""Детекторы «ошибок системы» momentum-бота (таксономия §4, канон §4).

«Ошибка» детерминированного бота — НЕ психология, а **повторяющийся убыточный
паттерн правил** (символ × сторона × сессия × сила-сигнала-бакет). Каждый
детектор — наблюдатель над сделками (broker net + сигнал входа), помечает срез
кодом паттерна для агрегации. Пороги **нейтральные/относительные/структурные**,
НЕ под желаемый P&L (no-data-fitting.mdc). Запланированный SL ≠ ошибка; ошибка =
паттерн на достаточной выборке (sample-size.mdc — гейт «темы» в движке отчёта).

Адаптация под механику momentum (отличие от tradecard_bybit, где много страт):
бот один (strategy="momentum"), нет поля score/close_reason → грейд по силе
сигнала, кластеры по win/loss (не по sl_hit), + специфичный ``swap_drag``
(overnight financing на удерживаемых TSMOM-позициях). Решения об изменении
логики — всегда человеку (strategy-guard.mdc). Список расширяемый.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from tradecard_momentum.analysis.grading import grade_curve
from tradecard_momentum.analysis.trade import (MomentumTrade, decided,
                                               expectancy_r, net_pnl, win_rate)

_STRAT = "momentum"


@dataclass
class PatternFinding:
    code: str
    bot: str
    mode: str
    strategy: str | None
    scope: dict
    n: int
    wr: float
    exp_r: float | None
    net: float
    detail: str
    trade_ids: list[int] = field(default_factory=list)

    def scope_key(self) -> str:
        import json
        return json.dumps(self.scope, sort_keys=True, ensure_ascii=False)


# ─── 1. signal_not_predictive ────────────────────────────────────────────

def detect_signal_not_predictive(trades: list[MomentumTrade], *, bot: str,
                                 mode: str, buckets: int, min_rho: float,
                                 min_trades: int) -> list[PatternFinding]:
    """Сила сигнала (|momentum|) НЕ монотонна по EXP — грейд по входу не
    отделяет винов (§5). Высокий momentum не значит лучший исход в текущем режиме."""
    dd = decided(trades)
    if len(dd) < min_trades:
        return []
    curve = grade_curve(dd, buckets=buckets, min_rho=min_rho)
    if curve is None or curve.predictive:
        return []
    graded_n = sum(b.n for b in curve.buckets)
    bucket_str = "; ".join(
        f"{b.label}[{b.score_min:.4f}-{b.score_max:.4f}] EXP="
        f"{b.exp_r:.2f} n={b.n}" for b in curve.buckets if b.exp_r is not None)
    rho = curve.rho if curve.rho is not None else float("nan")
    return [PatternFinding(
        code="signal_not_predictive", bot=bot, mode=mode, strategy=_STRAT,
        scope={"strategy": _STRAT}, n=graded_n, wr=win_rate(dd),
        exp_r=expectancy_r(dd), net=net_pnl(dd),
        detail=(f"сила сигнала не предиктивна (Spearman ρ={rho:.2f} < {min_rho}): "
                f"{bucket_str}"),
        trade_ids=[t.position_id for t in dd])]


# ─── 2. symbol_session_leak ──────────────────────────────────────────────

def detect_symbol_session_leak(trades: list[MomentumTrade], *, bot: str,
                               mode: str, min_trades: int) -> list[PatternFinding]:
    """Системная утечка в срезе (symbol / session / side) при общем плюсе бота."""
    out: list[PatternFinding] = []
    dd = decided(trades)
    overall_exp = expectancy_r(dd)
    if len(dd) < min_trades or overall_exp is None or overall_exp <= 0:
        return out  # утечка осмысленна только при общем плюсе
    for dim in ("symbol", "session", "side"):
        slices: dict[str, list[MomentumTrade]] = defaultdict(list)
        for t in dd:
            slices[getattr(t, dim)].append(t)
        for key, sl_trades in slices.items():
            if len(sl_trades) < min_trades:
                continue
            exp = expectancy_r(sl_trades)
            if exp is not None and exp < 0:
                out.append(PatternFinding(
                    code="symbol_session_leak", bot=bot, mode=mode,
                    strategy=_STRAT, scope={dim: key}, n=len(sl_trades),
                    wr=win_rate(sl_trades), exp_r=exp, net=net_pnl(sl_trades),
                    detail=(f"срез {dim}={key} EXP={exp:.2f} (<0) при общем EXP "
                            f"бота {overall_exp:.2f}"),
                    trade_ids=[t.position_id for t in sl_trades]))
    return out


# ─── 3. loss_cluster ─────────────────────────────────────────────────────

def detect_loss_cluster(trades: list[MomentumTrade], *, bot: str, mode: str,
                        factor: float, min_trades: int) -> list[PatternFinding]:
    """Повтор убытков на связке (symbol×side) выше базовой доли убытков.

    У momentum нет ``close_reason`` (deal-list его не несёт) — кластеризуем по
    факту убытка (net<0), а не по sl_hit. Относительный порог (× базы)."""
    out: list[PatternFinding] = []
    dd = decided(trades)
    if not dd:
        return out
    base_rate = sum(1 for t in dd if t.is_loss) / len(dd)
    if base_rate <= 0:
        return out
    bind: dict[tuple, list[MomentumTrade]] = defaultdict(list)
    for t in dd:
        bind[(t.symbol, t.side)].append(t)
    for (sym, side), bt in bind.items():
        if len(bt) < min_trades:
            continue
        rate = sum(1 for t in bt if t.is_loss) / len(bt)
        if rate >= factor * base_rate:
            out.append(PatternFinding(
                code="loss_cluster", bot=bot, mode=mode, strategy=_STRAT,
                scope={"symbol": sym, "side": side}, n=len(bt),
                wr=win_rate(bt), exp_r=expectancy_r(bt), net=net_pnl(bt),
                detail=(f"{sym} {side}: доля убытков {rate:.0%} ≥ {factor}× "
                        f"базовой {base_rate:.0%}"),
                trade_ids=[t.position_id for t in bt]))
    return out


# ─── 4. overtrading ──────────────────────────────────────────────────────

def detect_overtrading(trades: list[MomentumTrade], *, bot: str, mode: str,
                       spike_factor: float, min_trades: int) -> list[PatternFinding]:
    """Всплеск числа сделок при падении EXP («горячие» часы хуже спокойных).

    Для momentum это релевантно: edge-trigger вокруг порога может дребезжать
    (исторически 3× USDJPY long за день, BUILDLOG 06-05). «Горячий» час = число
    сделок ≥ spike_factor × медианы по активным часам (self-нормировка)."""
    dd = decided(trades)
    if len(dd) < min_trades:
        return []
    by_hour: dict[int, list[MomentumTrade]] = defaultdict(list)
    for t in dd:
        by_hour[int(t.ts_open // 3600)].append(t)
    counts = [len(v) for v in by_hour.values()]
    if len(counts) < 2:
        return []
    med = statistics.median(counts)
    if med <= 0:
        return []
    hot: list[MomentumTrade] = []
    calm: list[MomentumTrade] = []
    for v in by_hour.values():
        (hot if len(v) >= spike_factor * med else calm).extend(v)
    if len(hot) < min_trades or not calm:
        return []
    hot_exp = expectancy_r(hot)
    calm_exp = expectancy_r(calm)
    if hot_exp is None or calm_exp is None or hot_exp >= calm_exp:
        return []
    return [PatternFinding(
        code="overtrading", bot=bot, mode=mode, strategy=_STRAT,
        scope={"spike_factor": spike_factor}, n=len(hot), wr=win_rate(hot),
        exp_r=hot_exp, net=net_pnl(hot),
        detail=(f"перегретые часы (≥{spike_factor}× медианы {med:.0f} сделок/ч): "
                f"EXP={hot_exp:.2f} хуже спокойных EXP={calm_exp:.2f}"),
        trade_ids=[t.position_id for t in hot])]


# ─── 5. swap_drag ────────────────────────────────────────────────────────

def detect_swap_drag(trades: list[MomentumTrade], *, bot: str, mode: str,
                     min_frac: float, min_trades: int) -> list[PatternFinding]:
    """Overnight financing (swap) системно съедает результат на срезе.

    Специфично для TSMOM-механики momentum: позиция держится, пока знак momentum
    совпадает (Moskowitz 2012) → трендовые ноги переносятся через ролловер и
    платят swap. Наблюдение: |Σ swap| ≥ min_frac × |Σ gross| на связке (symbol),
    т.е. финансирование «отъедает» заметную долю валовой прибыли. Структурный
    относительный порог, без подгонки P&L. Решение — человеку (size/hold-time)."""
    out: list[PatternFinding] = []
    dd = decided(trades)
    by_sym: dict[str, list[MomentumTrade]] = defaultdict(list)
    for t in dd:
        by_sym[t.symbol].append(t)
    for sym, grp in by_sym.items():
        if len(grp) < min_trades:
            continue
        swap_sum = sum(t.swap_usd for t in grp)
        gross_sum = sum(t.gross_usd for t in grp)
        if gross_sum <= 0 or swap_sum >= 0:
            continue  # нет валовой прибыли или swap не в минус — нечего «съедать»
        frac = abs(swap_sum) / abs(gross_sum)
        if frac >= min_frac:
            out.append(PatternFinding(
                code="swap_drag", bot=bot, mode=mode, strategy=_STRAT,
                scope={"symbol": sym}, n=len(grp), wr=win_rate(grp),
                exp_r=expectancy_r(grp), net=net_pnl(grp),
                detail=(f"{sym}: swap ${swap_sum:+.2f} съедает {frac:.0%} от "
                        f"валовой прибыли ${gross_sum:.2f} (overnight financing "
                        f"на удерживаемых TSMOM-позициях)"),
                trade_ids=[t.position_id for t in grp]))
    return out
