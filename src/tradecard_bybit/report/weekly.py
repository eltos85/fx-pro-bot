"""Недельный report card (markdown, TASKSPEC §8.2).

Файл ``data/tradecard/{scalp|flowzone}_YYYY-WW.md``: темы, 5 Why, гипотеза-
решение, small-wins/momentum, грейд-аналитика (score→перформанс), факторный
аудит (reasons), per-strategy разрез, baseline-vs-A+ (big-game-hunting).

Это **advisory**-документ оператору. Никаких автоправок конфига; продвижение
гипотезы — отдельным одобренным коммитом человека (strategy-guard.mdc).
"""
from __future__ import annotations

from tradecard_bybit.analysis.detectors import PatternFinding
from tradecard_bybit.analysis.grading import GRADE_RISK_REF, GradeCurve
from tradecard_bybit.analysis.pnl import ModePnl
from tradecard_bybit.llm.five_why import FiveWhyResult


def _money(x: float) -> str:
    return f"${x:+.2f}"


def build_weekly_report(*, bot: str, week: str,
                        pnl_paper: ModePnl, pnl_live: ModePnl,
                        findings: list[PatternFinding],
                        top_theme: PatternFinding | None,
                        five_why: FiveWhyResult | None,
                        grade_by_strategy: dict[str, GradeCurve],
                        small_win_count: int,
                        momentum_lines: list[str],
                        baseline_note: str | None = None) -> str:
    L: list[str] = []
    L.append(f"# tradecard {bot} — weekly report card {week}")
    L.append("")
    L.append("> Advisory-ревью детерминированного бота (SMB Momentum Model). "
             "tradecard НЕ меняет конфиг/правила; любые гипотезы — кандидаты на "
             "ручную проверку человеком (strategy-guard.mdc).")
    if baseline_note:
        L.append("")
        L.append(f"> {baseline_note}")
    L.append("")

    # ─── P&L ────────────────────────────────────────────────────────────
    L.append("## P&L (paper / live раздельно, источник правды = Bybit closedPnl)")
    L.append("")
    L.append("| mode | n | WR | net (БД) | verified | Bybit net | Δ |")
    L.append("|---|---|---|---|---|---|---|")
    for p in (pnl_live, pnl_paper):
        bn = _money(p.bybit_net) if p.bybit_net is not None else "—"
        dd = _money(p.discrepancy) if p.discrepancy is not None else "—"
        L.append(f"| {p.mode} | {p.n_decided} | {p.wr:.0%} | {_money(p.net_db)} "
                 f"| {p.n_verified}/{p.n_decided} | {bn} | {dd} |")
    L.append("")

    # ─── Тема №1 + 5 Why ────────────────────────────────────────────────
    L.append("## Тема №1 периода (5 Why)")
    L.append("")
    if top_theme is None:
        L.append("_Повторяющихся паттернов выше порога темы не выявлено "
                 "(НАБЛЮДЕНИЕ, sample-size)._")
    else:
        L.append(f"**{top_theme.code}** — `{top_theme.strategy or '—'}` "
                 f"{top_theme.scope}")
        L.append("")
        L.append(f"- сделок: {top_theme.n}, WR {top_theme.wr:.0%}, "
                 f"EXP "
                 + (f"{top_theme.exp_r:.2f}" if top_theme.exp_r is not None else "n/a")
                 + f", net {_money(top_theme.net)}")
        L.append(f"- {top_theme.detail}")
        L.append("")
        if five_why and five_why.chain:
            L.append("**5 Why (DeepSeek, read-only):**")
            for i, why in enumerate(five_why.chain, 1):
                L.append(f"{i}. {why}")
            L.append("")
            if five_why.hypothesis:
                L.append(f"**Гипотеза-решение (кандидат):** {five_why.hypothesis}")
                L.append("")
                L.append("> Внедрение = отдельный одобренный коммит человека "
                         "(обновить STRATEGY_*/тесты). Small win засчитывается "
                         "только OOS ПОСЛЕ внедрения, не по in-sample.")
        elif five_why and five_why.error:
            L.append(f"_5 Why недоступен: {five_why.error}_")
        else:
            L.append("_5 Why отключён (TRADECARD_BYBIT_FIVE_WHY_ENABLED=false)._")
    L.append("")

    # ─── Все паттерны периода ───────────────────────────────────────────
    L.append("## Паттерны периода (наблюдения)")
    L.append("")
    if findings:
        L.append("| код | страта | срез | n | WR | EXP | net |")
        L.append("|---|---|---|---|---|---|---|")
        for f in findings:
            exp = f"{f.exp_r:.2f}" if f.exp_r is not None else "—"
            L.append(f"| {f.code} | {f.strategy or '—'} | "
                     f"{_scope_str(f.scope)} | {f.n} | {f.wr:.0%} | {exp} | "
                     f"{_money(f.net)} |")
    else:
        L.append("_паттернов не выявлено_")
    L.append("")

    # ─── Грейд-аналитика (score → перформанс) ───────────────────────────
    L.append("## Грейд-аналитика (score → перформанс, per-strategy)")
    L.append("")
    L.append("> Риск-аллокация канона (A+ до 80% / A 30% / B 15% / C 5%) — "
             "**референс**, не применяется автоматически (риск-модель фиксирована).")
    L.append("")
    for strat, curve in grade_by_strategy.items():
        rho = f"{curve.rho:.2f}" if curve.rho is not None else "n/a"
        verdict = "✅ монотонна" if curve.monotonic else "❌ НЕ монотонна (грейд сломан)"
        L.append(f"### `{strat}` — Spearman ρ={rho} {verdict}")
        L.append("")
        L.append("| грейд | score | n | WR | EXP(avgR) | net | риск-реф |")
        L.append("|---|---|---|---|---|---|---|")
        for b in sorted(curve.buckets, key=lambda x: x.rank, reverse=True):
            exp = f"{b.exp_r:.2f}" if b.exp_r is not None else "—"
            ref = GRADE_RISK_REF.get(b.label, "—")
            L.append(f"| {b.label} | {b.score_min}-{b.score_max} | {b.n} | "
                     f"{b.wr:.0%} | {exp} | {_money(b.net)} | {ref} |")
        L.append("")

    # ─── Факторный аудит (reasons) ──────────────────────────────────────
    fnoise = [f for f in findings if f.code == "factor_noise"]
    L.append("## Факторный аудит (reasons → factor-noise кандидаты)")
    L.append("")
    if fnoise:
        for f in fnoise:
            L.append(f"- `{f.strategy}` фактор **{f.scope.get('factor')}**: "
                     f"{f.detail}")
        L.append("")
        L.append("> Удаление фактора — решение человека после OOS (как scalp "
                 "v0.9.0 убрал funding/liq). Не удаляем по in-sample.")
    else:
        L.append("_кандидатов factor-noise не выявлено_")
    L.append("")

    # ─── Small wins / momentum ──────────────────────────────────────────
    L.append("## Small wins / momentum (OOS-гейт)")
    L.append("")
    L.append(f"- накоплено OOS-подтверждённых small wins: **{small_win_count}**")
    if momentum_lines:
        for ml in momentum_lines:
            L.append(f"- {ml}")
    L.append("")
    L.append("> Small win = значимое снижение частоты темы на forward/OOS ПОСЛЕ "
             "одобренного человеком внедрения (≥100 сделок, ≥2 недели, p<0.05). "
             "До внедрения — ГИПОТЕЗА; после, но до порога — НАБЛЮДЕНИЕ.")
    L.append("")
    return "\n".join(L)


def _scope_str(scope: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(scope.items()))
