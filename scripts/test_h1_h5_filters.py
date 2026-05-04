#!/usr/bin/env python3
"""Strict statistical testing для H1/H2/H5 фильтров на enriched-CSV.

Compliance (BUILDLOG 2026-05-04):
- Каждая гипотеза тестируется НЕЗАВИСИМО (не в комбинации, до прохождения
  individual теста).
- Bonferroni correction: p < 0.05 / 5 = **p < 0.01** (учитываем все 5
  гипотез из изначального плана, даже H3/H4 disqualified — это
  консервативно).
- IS partition (255 дней) — для оценки edge без data-snooping.
- OOS partition (110 дней, hold-out) — для подтверждения устойчивости.
- Pass criteria:
    1. На ALL: improvement в PF ≥ 0.10 vs baseline.
    2. На OOS: improvement в PF ≥ 0.05 vs OOS baseline (slightly looser
       т.к. меньше точек).
    3. p-value < 0.01 хотя бы на одном из (Fisher exact 2x2 для WR,
       Mann-Whitney U для PnL distribution) на ALL.
    4. OOS direction совпадает с IS direction (edge не флипает знак).
- Если фильтр НЕ проходит — записываем negative finding в BUILDLOG,
  НЕ подкручиваем параметры (no-data-fitting.mdc).

Использование:
    python3 -m scripts.test_h1_h5_filters \\
        --csv data/gold_orb_h1_h5_enriched_wick.csv \\
        --report data/gold_orb_h1_h5_test_report.txt
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import scipy.stats as ss

log = logging.getLogger("test_h1_h5_filters")

# Bonferroni: 5 гипотез из изначального плана (H1-H5).
# H3/H4 disqualified до тестирования (structural mismatch / negative
# finding) — но для конservativenoss оставляем поправку на 5.
N_HYPOTHESES = 5
P_THRESHOLD = 0.05 / N_HYPOTHESES   # 0.01


@dataclass
class Stats:
    n: int
    wr_pct: float
    net_pips: float
    avg_pips: float
    avg_win: float
    avg_loss: float
    pf: float
    median: float

    @classmethod
    def from_pnls(cls, pnls: list[float]) -> "Stats":
        n = len(pnls)
        if n == 0:
            return cls(0, 0, 0, 0, 0, 0, 0, 0)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        avg_win = statistics.mean(wins) if wins else 0.0
        avg_loss = statistics.mean(losses) if losses else 0.0
        sum_wins = sum(wins)
        sum_losses_abs = abs(sum(losses)) if losses else 0.0
        pf = (sum_wins / sum_losses_abs) if sum_losses_abs > 0 else float("inf")
        return cls(
            n=n,
            wr_pct=100 * len(wins) / n,
            net_pips=sum(pnls),
            avg_pips=sum(pnls) / n,
            avg_win=avg_win,
            avg_loss=avg_loss,
            pf=pf,
            median=statistics.median(pnls),
        )

    def fmt(self) -> str:
        pf_str = f"{self.pf:.2f}" if self.pf != float("inf") else "inf"
        return (
            f"n={self.n:4d}  WR={self.wr_pct:5.1f}%  "
            f"net={self.net_pips:+7.0f}p  avg={self.avg_pips:+7.1f}p  "
            f"avg_w={self.avg_win:+6.1f} avg_l={self.avg_loss:+6.1f}  PF={pf_str}"
        )


def fisher_exact_wr(kept_pnls: list[float], blocked_pnls: list[float]) -> float:
    """Fisher exact 2x2: kept_wins/kept_losses vs blocked_wins/blocked_losses."""
    if not kept_pnls or not blocked_pnls:
        return 1.0
    kw = sum(1 for p in kept_pnls if p > 0)
    kl = len(kept_pnls) - kw
    bw = sum(1 for p in blocked_pnls if p > 0)
    bl = len(blocked_pnls) - bw
    table = [[kw, kl], [bw, bl]]
    try:
        _, p = ss.fisher_exact(table, alternative="two-sided")
    except Exception:
        p = 1.0
    return float(p)


def mwu_pnl(kept_pnls: list[float], blocked_pnls: list[float]) -> float:
    """Mann-Whitney U test: PnL distribution kept vs blocked."""
    if len(kept_pnls) < 5 or len(blocked_pnls) < 5:
        return 1.0
    try:
        _, p = ss.mannwhitneyu(kept_pnls, blocked_pnls, alternative="two-sided")
    except Exception:
        p = 1.0
    return float(p)


@dataclass
class HypothesisDef:
    code: str               # "H1", "H2", "H5"
    title: str
    rationale: str
    keep_predicate: Callable[[dict], bool]   # True = пропустить trade
    research_source: str


HYPOTHESES: list[HypothesisDef] = [
    HypothesisDef(
        code="H1",
        title="ORB Internal Direction (only aligned)",
        rationale="Trade direction совпадает с close-direction "
                  "ORB-свечи (aggregated) — research-claim 77-80% align "
                  "+ 6.5p continuation.",
        keep_predicate=lambda r: r["h1_aligned"] == "True",
        research_source="tradingstats.net/orb-strategy-research, Filter #4",
    ),
    HypothesisDef(
        code="H2",
        title="ATR Regime Filter (only expansion)",
        rationale="Торгуем только когда ATR-14d > P70 на 30-day rolling "
                  "window (volatile expansion regime). По research'у "
                  "trend-following выживает только в expansion.",
        keep_predicate=lambda r: r["h2_regime"] == "expansion",
        research_source="mql5/769030 «Regime Mismatch» + XAU SENTINEL v2.2",
    ),
    HypothesisDef(
        code="H5",
        title="Liquidity Sweep Pre-Break (only swept)",
        rationale="Перед ORB-пробоем должен быть выкос «equal-low/high» "
                  "за last 50 баров (institutional liquidity grab). "
                  "Без sweep-а пробой = trap.",
        keep_predicate=lambda r: r["h5_swept_pre"] == "True",
        research_source="ICT/SMC 2026 paradigm",
    ),
]


def evaluate_hypothesis(rows: list[dict], h: HypothesisDef) -> dict:
    """Применить гипотезу как hard-rule filter, посчитать metrics + p-values."""
    is_rows = [r for r in rows if r["is_oos"] == "False"]
    oos_rows = [r for r in rows if r["is_oos"] == "True"]

    def split(rows_part: list[dict]) -> tuple[list[float], list[float]]:
        kept = [float(r["net_pips"]) for r in rows_part if h.keep_predicate(r)]
        blocked = [float(r["net_pips"]) for r in rows_part if not h.keep_predicate(r)]
        return kept, blocked

    all_kept, all_blocked = split(rows)
    is_kept, is_blocked = split(is_rows)
    oos_kept, oos_blocked = split(oos_rows)

    baseline_all = Stats.from_pnls([float(r["net_pips"]) for r in rows])
    baseline_is = Stats.from_pnls([float(r["net_pips"]) for r in is_rows])
    baseline_oos = Stats.from_pnls([float(r["net_pips"]) for r in oos_rows])

    return {
        "h": h,
        "baseline_all": baseline_all,
        "baseline_is": baseline_is,
        "baseline_oos": baseline_oos,
        "kept_all": Stats.from_pnls(all_kept),
        "kept_is": Stats.from_pnls(is_kept),
        "kept_oos": Stats.from_pnls(oos_kept),
        "blocked_all": Stats.from_pnls(all_blocked),
        "p_wr_all": fisher_exact_wr(all_kept, all_blocked),
        "p_wr_is": fisher_exact_wr(is_kept, is_blocked),
        "p_wr_oos": fisher_exact_wr(oos_kept, oos_blocked),
        "p_pnl_all": mwu_pnl(all_kept, all_blocked),
        "p_pnl_is": mwu_pnl(is_kept, is_blocked),
        "p_pnl_oos": mwu_pnl(oos_kept, oos_blocked),
    }


def verdict(res: dict) -> tuple[str, list[str]]:
    """APPROVE / REJECT с обоснованиями."""
    reasons: list[str] = []
    h = res["h"]

    # 1) PF improvement on ALL
    pf_all_gain = res["kept_all"].pf - res["baseline_all"].pf
    if pf_all_gain < 0.10:
        reasons.append(f"PF gain on ALL = {pf_all_gain:+.2f} < 0.10 threshold")

    # 2) PF improvement on OOS
    pf_oos_gain = res["kept_oos"].pf - res["baseline_oos"].pf
    if pf_oos_gain < 0.05:
        reasons.append(f"PF gain on OOS = {pf_oos_gain:+.2f} < 0.05 threshold")

    # 3) IS/OOS direction consistency (edge не флипает)
    pf_is_gain = res["kept_is"].pf - res["baseline_is"].pf
    if (pf_is_gain > 0) != (pf_oos_gain > 0):
        reasons.append(
            f"IS/OOS PF-edge sign-flip: IS={pf_is_gain:+.2f}, OOS={pf_oos_gain:+.2f}"
        )

    # 4) p-value < Bonferroni threshold (0.01) хотя бы на одном тесте на ALL
    p_min_all = min(res["p_wr_all"], res["p_pnl_all"])
    if p_min_all >= P_THRESHOLD:
        reasons.append(
            f"min p-value on ALL = {p_min_all:.4f} >= {P_THRESHOLD:.4f} (Bonferroni)"
        )

    # 5) Минимум сделок в OOS (sample-size guard)
    if res["kept_oos"].n < 30:
        reasons.append(f"OOS kept n={res['kept_oos'].n} < 30 (sample-size)")

    return ("APPROVE", reasons) if not reasons else ("REJECT", reasons)


def report(rows: list[dict], path: Path) -> None:
    lines: list[str] = []
    def w(s: str = "") -> None:
        lines.append(s)
        print(s)

    w("=" * 78)
    w("STATISTICAL TEST H1/H2/H5 — gold_orb 365d M5 GC=F")
    w("=" * 78)
    w(f"Bonferroni p-threshold: {P_THRESHOLD:.4f} (0.05 / {N_HYPOTHESES} hypotheses)")
    w(f"Total trades: {len(rows)}")
    n_oos = sum(1 for r in rows if r["is_oos"] == "True")
    w(f"  IS:  {len(rows) - n_oos}  |  OOS: {n_oos}")
    baseline_all = Stats.from_pnls([float(r["net_pips"]) for r in rows])
    baseline_is = Stats.from_pnls([float(r["net_pips"]) for r in rows if r["is_oos"] == "False"])
    baseline_oos = Stats.from_pnls([float(r["net_pips"]) for r in rows if r["is_oos"] == "True"])
    w("\nBASELINE (no filter):")
    w(f"  ALL: {baseline_all.fmt()}")
    w(f"  IS:  {baseline_is.fmt()}")
    w(f"  OOS: {baseline_oos.fmt()}")

    summary: list[tuple[str, str, list[str]]] = []
    for h in HYPOTHESES:
        w("\n" + "─" * 78)
        w(f"{h.code}: {h.title}")
        w(f"  Rationale: {h.rationale}")
        w(f"  Source:    {h.research_source}")

        res = evaluate_hypothesis(rows, h)
        w(f"\n  ALL:")
        w(f"    baseline:   {res['baseline_all'].fmt()}")
        w(f"    kept   :    {res['kept_all'].fmt()}  Δpf={res['kept_all'].pf - res['baseline_all'].pf:+.2f}")
        w(f"    blocked:    {res['blocked_all'].fmt()}")
        w(f"    p(WR)  Fisher: {res['p_wr_all']:.4f}  {'***' if res['p_wr_all'] < P_THRESHOLD else ''}")
        w(f"    p(PnL) MWU:    {res['p_pnl_all']:.4f}  {'***' if res['p_pnl_all'] < P_THRESHOLD else ''}")
        w(f"\n  IS:")
        w(f"    baseline:   {res['baseline_is'].fmt()}")
        w(f"    kept   :    {res['kept_is'].fmt()}  Δpf={res['kept_is'].pf - res['baseline_is'].pf:+.2f}")
        w(f"    p(WR)/p(PnL): {res['p_wr_is']:.4f} / {res['p_pnl_is']:.4f}")
        w(f"\n  OOS:")
        w(f"    baseline:   {res['baseline_oos'].fmt()}")
        w(f"    kept   :    {res['kept_oos'].fmt()}  Δpf={res['kept_oos'].pf - res['baseline_oos'].pf:+.2f}")
        w(f"    p(WR)/p(PnL): {res['p_wr_oos']:.4f} / {res['p_pnl_oos']:.4f}")

        v, reasons = verdict(res)
        w(f"\n  VERDICT: **{v}**")
        if reasons:
            for r in reasons:
                w(f"    - {r}")
        summary.append((h.code, v, reasons))

    # ─── Combined hypothesis: H2-only as best single, then H1+H2 if both individually approved ───
    w("\n" + "═" * 78)
    w("COMBINED FILTERS (применяются ТОЛЬКО если individual прошли)")
    w("═" * 78)
    approved_codes = [code for code, v, _ in summary if v == "APPROVE"]
    if len(approved_codes) < 2:
        w(f"\n  Approved individually: {approved_codes}")
        w(f"  (нужно >=2 для combined-теста; пропускаем)")
    else:
        # AND-комбинация всех approved
        and_predicate: Callable[[dict], bool] = lambda r: all(
            h.keep_predicate(r) for h in HYPOTHESES if h.code in approved_codes
        )
        is_rows = [r for r in rows if r["is_oos"] == "False"]
        oos_rows = [r for r in rows if r["is_oos"] == "True"]
        kept_all = [float(r["net_pips"]) for r in rows if and_predicate(r)]
        kept_is = [float(r["net_pips"]) for r in is_rows if and_predicate(r)]
        kept_oos = [float(r["net_pips"]) for r in oos_rows if and_predicate(r)]
        w(f"\n  AND-combo {' + '.join(approved_codes)}:")
        w(f"    ALL: {Stats.from_pnls(kept_all).fmt()}")
        w(f"    IS:  {Stats.from_pnls(kept_is).fmt()}")
        w(f"    OOS: {Stats.from_pnls(kept_oos).fmt()}")
        w("    [для combined-теста на activation нужен отдельный rigor-тест, "
          "это только preview]")

    w("\n" + "═" * 78)
    w("FINAL SUMMARY")
    w("═" * 78)
    for code, v, _ in summary:
        marker = "✓" if v == "APPROVE" else "✗"
        w(f"  {marker} {code}: {v}")

    if path:
        path.write_text("\n".join(lines), encoding="utf-8")
        log.info("→ %s", path)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default="data/gold_orb_h1_h5_enriched_wick.csv")
    p.add_argument("--report", default="data/gold_orb_h1_h5_test_report.txt")
    args = p.parse_args()

    path = Path(args.csv)
    if not path.exists():
        log.error("FAIL: %s не найден", path)
        return 1
    rows = list(csv.DictReader(path.open()))
    report(rows, Path(args.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
