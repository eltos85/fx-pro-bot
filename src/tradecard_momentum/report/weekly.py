"""Недельный report card (markdown, TASKSPEC §8.2).

Файл ``data/tradecard/momentum_YYYY-WW.md``: P&L (broker net), тема №1 + 5 Why,
гипотеза-решение, паттерны, грейд-аналитика (сила сигнала → перформанс),
per-symbol разрез, small-wins/momentum.

Это **advisory**-документ оператору. Никаких автоправок конфига; продвижение
гипотезы — отдельным одобренным коммитом человека (strategy-guard.mdc).
"""
from __future__ import annotations

from tradecard_momentum.analysis.detectors import PatternFinding
from tradecard_momentum.analysis.grading import GRADE_RISK_REF, GradeCurve
from tradecard_momentum.analysis.pnl import BrokerPnl, SymbolPnl
from tradecard_momentum.llm.five_why import FiveWhyResult


def _money(x: float) -> str:
    return f"${x:+.2f}"


def _scope_str(scope: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(scope.items()))


def build_weekly_report(*, week: str, pnl: BrokerPnl,
                        findings: list[PatternFinding],
                        top_theme: PatternFinding | None,
                        five_why: FiveWhyResult | None,
                        grade: GradeCurve | None,
                        small_win_count: int, momentum_lines: list[str],
                        symbol_pnl: list[SymbolPnl] | None = None,
                        baseline_note: str | None = None,
                        broker_ok: bool = True) -> str:
    L: list[str] = []
    L.append(f"# tradecard momentum — weekly report card {week}")
    L.append("")
    L.append("> Advisory-ревью детерминированного TSMOM-бота (SMB Momentum Model). "
             "tradecard НЕ меняет конфиг/правила; любые гипотезы — кандидаты на "
             "ручную проверку человеком (strategy-guard.mdc).")
    if baseline_note:
        L.append("")
        L.append(f"> {baseline_note}")
    if not broker_ok:
        L.append("")
        L.append("> ⚠️ broker deal-list недоступен в этом прогоне — P&L без "
                 "ground truth (cTrader не подключён). Сделки не реконструированы.")
    L.append("")

    # ─── P&L ────────────────────────────────────────────────────────────
    L.append("## P&L (live, источник правды = cTrader deal-list net)")
    L.append("")
    L.append("| n | WR | net | gross | swap | comm | EXP(avgR) | R-known |")
    L.append("|---|---|---|---|---|---|---|---|")
    exp = f"{pnl.exp_r:.2f}" if pnl.exp_r is not None else "—"
    L.append(f"| {pnl.n_decided} | {pnl.wr:.0%} | {_money(pnl.net)} | "
             f"{_money(pnl.gross)} | {_money(pnl.swap)} | {_money(pnl.commission)} "
             f"| {exp} | {pnl.n_with_r}/{pnl.n_decided} |")
    L.append("")

    # ─── P&L по символам ────────────────────────────────────────────────
    L.append("### P&L по символам")
    L.append("")
    if symbol_pnl:
        L.append("| символ | n | WR | net | EXP(avgR) |")
        L.append("|---|---|---|---|---|")
        for sp in symbol_pnl:
            e = f"{sp.exp_r:.2f}" if sp.exp_r is not None else "—"
            L.append(f"| {sp.symbol} | {sp.n_decided} | {sp.wr:.0%} | "
                     f"{_money(sp.net)} | {e} |")
    else:
        L.append("_нет закрытых сделок для разреза по символам_")
    L.append("")

    # ─── Тема №1 + 5 Why ────────────────────────────────────────────────
    L.append("## Тема №1 периода (5 Why)")
    L.append("")
    if top_theme is None:
        L.append("_Повторяющихся паттернов выше порога темы не выявлено "
                 "(НАБЛЮДЕНИЕ, sample-size)._")
    else:
        L.append(f"**{top_theme.code}** — {{{_scope_str(top_theme.scope)}}}")
        L.append("")
        e = f"{top_theme.exp_r:.2f}" if top_theme.exp_r is not None else "n/a"
        L.append(f"- сделок: {top_theme.n}, WR {top_theme.wr:.0%}, EXP {e}, "
                 f"net {_money(top_theme.net)}")
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
                         "(обновить конфиг/тесты momentum-бота). Small win "
                         "засчитывается только OOS ПОСЛЕ внедрения, не in-sample.")
        elif five_why and five_why.error:
            L.append(f"_5 Why недоступен: {five_why.error}_")
        else:
            L.append("_5 Why отключён или выборка ниже порога (sample-size)._")
    L.append("")

    # ─── Все паттерны ───────────────────────────────────────────────────
    L.append("## Паттерны периода (наблюдения)")
    L.append("")
    if findings:
        L.append("| код | срез | n | WR | EXP | net |")
        L.append("|---|---|---|---|---|---|")
        for f in findings:
            e = f"{f.exp_r:.2f}" if f.exp_r is not None else "—"
            L.append(f"| {f.code} | {_scope_str(f.scope)} | {f.n} | {f.wr:.0%} "
                     f"| {e} | {_money(f.net)} |")
    else:
        L.append("_паттернов не выявлено_")
    L.append("")

    # ─── Грейд-аналитика ────────────────────────────────────────────────
    L.append("## Грейд-аналитика (сила сигнала |momentum| → перформанс)")
    L.append("")
    L.append("> Риск-аллокация канона (A+ до 80% / A 30% / B 15% / C 5%) — "
             "**референс**, не применяется автоматически (риск-модель фиксирована).")
    L.append("")
    if grade and grade.buckets:
        rho = f"{grade.rho:.2f}" if grade.rho is not None else "n/a"
        verdict = ("✅ монотонна" if grade.monotonic
                   else "❌ НЕ монотонна (грейд сломан)")
        L.append(f"Spearman ρ={rho} {verdict}")
        L.append("")
        L.append("| грейд | |momentum| | n | WR | EXP(avgR) | net | риск-реф |")
        L.append("|---|---|---|---|---|---|---|")
        for b in sorted(grade.buckets, key=lambda x: x.rank, reverse=True):
            e = f"{b.exp_r:.2f}" if b.exp_r is not None else "—"
            ref = GRADE_RISK_REF.get(b.label, "—")
            L.append(f"| {b.label} | {b.score_min:.4f}-{b.score_max:.4f} | {b.n} "
                     f"| {b.wr:.0%} | {e} | {_money(b.net)} | {ref} |")
    else:
        L.append("_недостаточно сделок с известной силой сигнала для грейда_")
    L.append("")

    # ─── Small wins / momentum ──────────────────────────────────────────
    L.append("## Small wins / momentum (OOS-гейт)")
    L.append("")
    L.append(f"- накоплено OOS-подтверждённых small wins: **{small_win_count}**")
    for ml in momentum_lines:
        L.append(f"- {ml}")
    L.append("")
    L.append("> Small win = значимое снижение частоты темы на forward/OOS ПОСЛЕ "
             "одобренного человеком внедрения (≥100 сделок, ≥2 недели, p<0.05). "
             "До внедрения — ГИПОТЕЗА; после, но до порога — НАБЛЮДЕНИЕ.")
    L.append("")
    return "\n".join(L)
