"""Дневной digest (Telegram, TASKSPEC §8.1).

Краткая сводка за день: net P&L (paper/live раздельно), топ-3 паттерна,
грейд-распределение по score, 1 actionable-наблюдение. Префикс ботов
(``[tradecard-scalp]`` / ``[tradecard-flowzone]``) ставит TelegramNotifier.
"""
from __future__ import annotations

from tradecard_bybit.analysis.detectors import PatternFinding
from tradecard_bybit.analysis.grading import GradeCurve
from tradecard_bybit.analysis.pnl import ModePnl


def _fmt_money(x: float) -> str:
    return f"${x:+.2f}"


def build_daily_digest(*, bot: str, date_label: str,
                       pnl_paper: ModePnl, pnl_live: ModePnl,
                       findings: list[PatternFinding],
                       grade: GradeCurve | None) -> str:
    lines = [f"<b>tradecard {bot}</b> — daily {date_label} (UTC)"]

    for p in (pnl_live, pnl_paper):
        if p.n_decided == 0:
            continue
        gt = ""
        if p.bybit_net is not None:
            gt = f" | Bybit closedPnl net {_fmt_money(p.bybit_net)}"
        lines.append(
            f"• {p.mode}: net {_fmt_money(p.net_db)} (n={p.n_decided}, "
            f"WR {p.wr:.0%}, verified {p.n_verified}/{p.n_decided}){gt}")
    if pnl_live.n_decided == 0 and pnl_paper.n_decided == 0:
        lines.append("• сделок за период нет")

    if findings:
        lines.append("Топ-3 паттерна (наблюдение, не вывод):")
        for f in findings[:3]:
            lines.append(f"  – {f.code} [{f.strategy or '—'}] "
                         f"n={f.n} net {_fmt_money(f.net)}")
    else:
        lines.append("Паттернов выше порога не выявлено")

    if grade and grade.buckets:
        dist = " ".join(f"{b.label}:{b.n}" for b in sorted(
            grade.buckets, key=lambda x: x.rank, reverse=True))
        mono = "монотонна" if grade.monotonic else "НЕ монотонна (грейд сломан)"
        lines.append(f"Грейд по score: {dist} | кривая {mono}")

    lines.append(_actionable(findings, grade))
    return "\n".join(lines)


def _actionable(findings: list[PatternFinding], grade: GradeCurve | None) -> str:
    """Одно actionable-наблюдение (advisory, не команда). Канон: не прыгать к
    тюнингу — это материал для 5 Why и ручной проверки."""
    if grade and not grade.monotonic:
        return ("👉 score не отделяет винов — кандидат темы №1 для 5 Why "
                "(грейдинг/веса факторов). НЕ менять конфиг без OOS-проверки.")
    if findings:
        f = findings[0]
        return (f"👉 главный срез: {f.code} [{f.strategy or '—'}] {f.scope}. "
                f"Материал для 5 Why; решение — человеку (strategy-guard).")
    return "👉 наблюдаем — выборка ниже порога темы (sample-size)."
