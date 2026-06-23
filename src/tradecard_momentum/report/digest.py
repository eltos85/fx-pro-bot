"""Дневной digest (Telegram, TASKSPEC §8.1).

Краткая сводка за день: net P&L (broker deal-list), топ-3 паттерна,
грейд-распределение по силе сигнала, 1 actionable-наблюдение. Префикс
``[tradecard-momentum]`` ставит TelegramNotifier.
"""
from __future__ import annotations

from tradecard_momentum.analysis.detectors import PatternFinding
from tradecard_momentum.analysis.grading import GradeCurve
from tradecard_momentum.analysis.pnl import BrokerPnl


def _money(x: float) -> str:
    return f"${x:+.2f}"


def build_daily_digest(*, date_label: str, pnl: BrokerPnl,
                       findings: list[PatternFinding], grade: GradeCurve | None,
                       baseline_note: str | None = None,
                       broker_ok: bool = True) -> str:
    lines = ["<b>tradecard momentum</b> — daily " + date_label + " (UTC)"]
    if baseline_note:
        lines.append(baseline_note)
    if not broker_ok:
        lines.append("⚠️ broker deal-list недоступен — P&L не сверён "
                     "(нет ground truth)")

    if pnl.n_decided == 0:
        lines.append("• закрытых сделок за период нет")
    else:
        exp = f"{pnl.exp_r:.2f}" if pnl.exp_r is not None else "n/a"
        lines.append(
            f"• live: net {_money(pnl.net)} (n={pnl.n_decided}, WR {pnl.wr:.0%}, "
            f"EXP {exp}, R-known {pnl.n_with_r}/{pnl.n_decided})")
        lines.append(f"  gross {_money(pnl.gross)} | swap {_money(pnl.swap)} "
                     f"| comm {_money(pnl.commission)}")

    if findings:
        lines.append("Топ-3 паттерна (наблюдение, не вывод):")
        for f in findings[:3]:
            lines.append(f"  – {f.code} {_scope(f.scope)} "
                         f"n={f.n} net {_money(f.net)}")
    else:
        lines.append("Паттернов выше порога не выявлено")

    if grade and grade.buckets:
        dist = " ".join(f"{b.label}:{b.n}" for b in sorted(
            grade.buckets, key=lambda x: x.rank, reverse=True))
        mono = "монотонна" if grade.monotonic else "НЕ монотонна (грейд сломан)"
        lines.append(f"Грейд по силе сигнала: {dist} | кривая {mono}")

    lines.append(_actionable(findings, grade))
    return "\n".join(lines)


def _scope(scope: dict) -> str:
    return "[" + ", ".join(f"{k}={v}" for k, v in sorted(scope.items())) + "]"


def _actionable(findings: list[PatternFinding], grade: GradeCurve | None) -> str:
    if grade and not grade.monotonic:
        return ("👉 сила сигнала не отделяет винов — кандидат темы №1 для 5 Why. "
                "НЕ менять порог входа без OOS-проверки.")
    if findings:
        f = findings[0]
        return (f"👉 главный срез: {f.code} {_scope(f.scope)}. Материал для 5 Why; "
                f"решение — человеку (strategy-guard).")
    return "👉 наблюдаем — выборка ниже порога темы (sample-size)."
