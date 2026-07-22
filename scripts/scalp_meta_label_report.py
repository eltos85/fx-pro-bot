#!/usr/bin/env python3
"""Post-cutoff отчёт preregistered shadow meta-label scalp_bot.

Использует только наблюдения ПОСЛЕ META_LABEL_OBSERVATIONAL_CUTOFF. Старые
strategy-behaviour cutoffs намеренно не импортируются и не меняются.

Пример:
  python scripts/scalp_meta_label_report.py --db /data/scalp_bot.sqlite
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

from scipy import stats


META_LABEL_OBSERVATIONAL_CUTOFF = "2026-07-22T13:30:00Z"
TECHNICAL_CLOSE_PREFIX = "entry_"


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        UTC).timestamp()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def _f(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def bh_fdr(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values с сохранением исходного порядка."""
    n = len(pvalues)
    if not n:
        return []
    order = sorted(range(n), key=pvalues.__getitem__)
    adjusted = [1.0] * n
    running = 1.0
    for rank0 in range(n - 1, -1, -1):
        idx = order[rank0]
        rank = rank0 + 1
        running = min(running, pvalues[idx] * n / rank)
        adjusted[idx] = min(1.0, running)
    return adjusted


def _compare(rows: list[dict]) -> dict | None:
    keep = [r["outcome"] for r in rows
            if r["would_keep"] == 1 and r["outcome"] is not None]
    drop = [r["outcome"] for r in rows
            if r["would_keep"] == 0 and r["outcome"] is not None]
    if not keep or not drop:
        return None
    u, p_mw = stats.mannwhitneyu(keep, drop, alternative="two-sided")
    kw = sum(x > 0 for x in keep)
    dw = sum(x > 0 for x in drop)
    _, p_fisher = stats.fisher_exact(
        [[kw, len(keep) - kw], [dw, len(drop) - dw]],
        alternative="two-sided",
    )
    return {
        "n_keep": len(keep),
        "n_drop": len(drop),
        "mean_keep": sum(keep) / len(keep),
        "mean_drop": sum(drop) / len(drop),
        "wr_keep": kw / len(keep),
        "wr_drop": dw / len(drop),
        "rank_biserial": 2.0 * float(u) / (len(keep) * len(drop)) - 1.0,
        "wr_diff": kw / len(keep) - dw / len(drop),
        "p_mw": float(p_mw),
        "p_fisher": float(p_fisher),
    }


def _actual_rows(con: sqlite3.Connection, cutoff: float) -> list[dict]:
    rows = con.execute(
        """SELECT t.id, t.ts_open AS ts, t.strategy, t.pnl_usd AS outcome,
                  m.would_keep, m.meta_score, r.session
           FROM trades t
           JOIN meta_label_features m ON m.trade_id=t.id
           LEFT JOIN regime_features r ON r.trade_id=t.id
           WHERE t.ts_open>=? AND t.status='closed' AND t.pnl_usd IS NOT NULL
             AND (t.close_reason IS NULL OR t.close_reason NOT LIKE ?)
           ORDER BY t.ts_open""",
        (cutoff, f"{TECHNICAL_CLOSE_PREFIX}%"),
    ).fetchall()
    return [{**dict(r), "cohort": "actual_fill", "day": _day(r["ts"])}
            for r in rows]


def _nonfill_rows(con: sqlite3.Connection, cutoff: float) -> list[dict]:
    """Maker non-fill с первым terminal 1.5R outcome, где он уже наблюдён."""
    rows = con.execute(
        """SELECT n.id, n.ts_signal AS ts, n.strategy, n.outcome_1_5r,
                  n.target_r, m.would_keep, m.meta_score, r.session
           FROM maker_nonfill_shadows n
           JOIN meta_label_features m ON m.trade_id=n.trade_id
           LEFT JOIN regime_features r ON r.trade_id=n.trade_id
           WHERE n.ts_signal>=? AND n.outcome_1_5r IN ('target','sl')
           ORDER BY n.ts_signal""",
        (cutoff,),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["outcome"] = float(d["target_r"]) if d["outcome_1_5r"] == "target" else -1.0
        d["cohort"] = "maker_nonfill"
        d["day"] = _day(d["ts"])
        out.append(d)
    return out


def _groups(rows: list[dict]):
    specs = (
        ("strategy", lambda r: r["strategy"]),
        ("day", lambda r: r["day"]),
        ("session", lambda r: r["session"] or "unknown"),
        ("strategy/day/session",
         lambda r: f"{r['strategy']}/{r['day']}/{r['session'] or 'unknown'}"),
    )
    for dimension, keyfn in specs:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[keyfn(row)].append(row)
        for key, group in sorted(grouped.items()):
            yield dimension, key, group


def _walk_forward(rows: list[dict]) -> None:
    """Expanding chronological report; каждый test-day строго post-cutoff."""
    days = sorted({r["day"] for r in rows})
    print("\nWALK_FORWARD (fixed preregistered thresholds, test day only)")
    for test_day in days:
        test = [r for r in rows if r["day"] == test_day]
        prior_n = sum(r["day"] < test_day for r in rows)
        result = _compare(test)
        if result is None:
            print(f"{test_day}: prior_post_cutoff_n={prior_n} test_n={len(test)} insufficient")
        else:
            print(
                f"{test_day}: prior_post_cutoff_n={prior_n} test_n={len(test)} "
                f"effect={result['rank_biserial']:+.3f} "
                f"wr_diff={result['wr_diff']:+.3f} "
                f"p_mw={result['p_mw']:.4g} p_fisher={result['p_fisher']:.4g}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/scalp_bot.sqlite")
    parser.add_argument(
        "--cutoff", default=META_LABEL_OBSERVATIONAL_CUTOFF,
        help="observational ISO cutoff UTC; strategy behavior cutoffs не меняет",
    )
    args = parser.parse_args()
    cutoff = _ts(args.cutoff)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    actual = _actual_rows(con, cutoff)
    nonfills = _nonfill_rows(con, cutoff)
    rows = actual + nonfills
    print(
        f"cutoff={args.cutoff} actual_fills={len(actual)} "
        f"maker_nonfill_terminal={len(nonfills)} total={len(rows)}"
    )

    reports: list[tuple[str, str, str, dict]] = []
    for cohort, cohort_rows in (
        ("actual_fill", actual), ("maker_nonfill", nonfills), ("combined", rows)
    ):
        for dimension, key, group in _groups(cohort_rows):
            result = _compare(group)
            if result is not None:
                reports.append((cohort, dimension, key, result))
    pvals = [r[3][test] for r in reports for test in ("p_mw", "p_fisher")]
    qvals = bh_fdr(pvals)
    qi = 0
    print("\nGROUP_EFFECTS")
    for cohort, dimension, key, result in reports:
        q_mw, q_fisher = qvals[qi], qvals[qi + 1]
        qi += 2
        print(
            f"{cohort} {dimension}={key} "
            f"n={result['n_keep']}+{result['n_drop']} "
            f"mean={result['mean_keep']:+.3f}/{result['mean_drop']:+.3f} "
            f"WR={result['wr_keep']:.1%}/{result['wr_drop']:.1%} "
            f"effect_rbc={result['rank_biserial']:+.3f} "
            f"effect_WR={result['wr_diff']:+.3f} "
            f"MW_p/q={result['p_mw']:.4g}/{q_mw:.4g} "
            f"Fisher_p/q={result['p_fisher']:.4g}/{q_fisher:.4g}"
        )
    _walk_forward(rows)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
