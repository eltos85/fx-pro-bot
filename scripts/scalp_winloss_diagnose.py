#!/usr/bin/env python3
"""Диагностика вин vs луз: баг / геометрия / комиссия / перекос стороны.

Только чтение. Новых фильтров и порогов не предлагает.
Источник: локальная SQLite (структура сделки). PnL — расчётный net из БД,
не биржевая выписка (stats-collection.mdc).

Usage:
  python3 scripts/scalp_winloss_diagnose.py /data/scalp_bot.sqlite
  python3 scripts/scalp_winloss_diagnose.py /data/hybrid_bot.sqlite --name hybrid
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import time
from collections import defaultdict


ENTRY_SKIP = ("entry_Cancelled", "entry_timeout", "entry_Rejected")


def _mean(xs: list[float]) -> float | None:
    return statistics.mean(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _fmt(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def _welch_p(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or len(b) < 3:
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    va = statistics.variance(a) if len(a) > 1 else 0.0
    vb = statistics.variance(b) if len(b) > 1 else 0.0
    na, nb = len(a), len(b)
    se2 = va / na + vb / nb
    if se2 <= 0:
        return None
    t = (ma - mb) / math.sqrt(se2)
    # df Welch–Satterthwaite
    num = se2 ** 2
    den = 0.0
    if na > 1:
        den += (va / na) ** 2 / (na - 1)
    if nb > 1:
        den += (vb / nb) ** 2 / (nb - 1)
    if den <= 0:
        return None
    # двусторонняя нормальная аппроксимация (n большое)
    p = math.erfc(abs(t) / math.sqrt(2))
    return p


def _binom_p(wins: int, n: int, p0: float = 0.5) -> float | None:
    if n <= 0:
        return None
    # нормальная аппроксимация
    mean = n * p0
    var = n * p0 * (1 - p0)
    if var <= 0:
        return None
    z = (wins - mean) / math.sqrt(var)
    return math.erfc(abs(z) / math.sqrt(2))


def load_filled(conn: sqlite3.Connection, since: float | None) -> list[dict]:
    q = (
        "SELECT id, ts_open, ts_close, symbol, side, qty, entry, sl, tp, "
        "exit, score, strategy, pnl_usd, fees_usd, close_reason, mode "
        "FROM trades WHERE status='closed' AND exit IS NOT NULL "
        "AND qty > 0 AND entry > 0"
    )
    args: list = []
    if since is not None:
        q += " AND ts_open >= ?"
        args.append(since)
    rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        reason = r["close_reason"] or ""
        if any(reason.startswith(p) or reason == p for p in ENTRY_SKIP):
            continue
        if reason == "restart_flat":
            continue
        sl = float(r["sl"] or 0)
        entry = float(r["entry"])
        tp = float(r["tp"] or 0)
        exit_px = float(r["exit"])
        qty = float(r["qty"])
        side = (r["side"] or "").lower()
        risk = qty * abs(entry - sl) if sl else 0.0
        hold = None
        if r["ts_close"] and r["ts_open"]:
            hold = (float(r["ts_close"]) - float(r["ts_open"])) / 60.0
        planned_rr = abs(tp - entry) / abs(entry - sl) if sl and entry != sl else None
        sign = 1.0 if side in ("buy", "long") else -1.0
        gross = sign * (exit_px - entry) * qty
        fees = float(r["fees_usd"] or 0)
        pnl = float(r["pnl_usd"] or 0)
        # баг геометрии: стоп не с той стороны
        if side in ("buy", "long"):
            sl_ok = sl < entry if sl else None
            tp_ok = tp > entry if tp else None
        else:
            sl_ok = sl > entry if sl else None
            tp_ok = tp < entry if tp else None
        out.append({
            "id": r["id"], "symbol": r["symbol"], "side": side,
            "strategy": r["strategy"] or "?", "reason": reason,
            "score": r["score"], "pnl": pnl, "gross": gross, "fees": fees,
            "risk": risk, "r": (pnl / risk) if risk > 0 else None,
            "gross_r": (gross / risk) if risk > 0 else None,
            "fee_r": (fees / risk) if risk > 0 and fees > 0 else None,
            "planned_rr": planned_rr, "hold_min": hold,
            "sl_ok": sl_ok, "tp_ok": tp_ok,
            "sl_pct": 100 * abs(entry - sl) / entry if sl else None,
        })
    return out


def _pack(rows: list[dict]) -> dict:
    pnls = [x["pnl"] for x in rows]
    wins = [x for x in rows if x["pnl"] > 0]
    loss = [x for x in rows if x["pnl"] < 0]
    flat = [x for x in rows if x["pnl"] == 0]
    rs = [x["r"] for x in rows if x["r"] is not None]
    gr = [x["gross_r"] for x in rows if x["gross_r"] is not None]
    fr = [x["fee_r"] for x in rows if x["fee_r"] is not None]
    holds_w = [x["hold_min"] for x in wins if x["hold_min"] is not None]
    holds_l = [x["hold_min"] for x in loss if x["hold_min"] is not None]
    scores_w = [float(x["score"]) for x in wins if x["score"] is not None]
    scores_l = [float(x["score"]) for x in loss if x["score"] is not None]
    n = len(rows)
    w = len(wins)
    return {
        "n": n, "w": w, "l": len(loss), "z": len(flat),
        "wr": 100 * w / n if n else None,
        "net": sum(pnls), "avg": _mean(pnls),
        "avg_w": _mean([x["pnl"] for x in wins]),
        "avg_l": _mean([x["pnl"] for x in loss]),
        "med_w": _median([x["pnl"] for x in wins]),
        "med_l": _median([x["pnl"] for x in loss]),
        "avg_r": _mean(rs), "avg_gr": _mean(gr), "avg_fee_r": _mean(fr),
        "fees": sum(x["fees"] for x in rows),
        "gross": sum(x["gross"] for x in rows),
        "hold_w": _median(holds_w), "hold_l": _median(holds_l),
        "score_w": _mean(scores_w), "score_l": _mean(scores_l),
        "score_p": _welch_p(scores_w, scores_l),
        "rr": _mean([x["planned_rr"] for x in rows if x["planned_rr"]]),
        "sl_bad": sum(1 for x in rows if x["sl_ok"] is False),
        "tp_bad": sum(1 for x in rows if x["tp_ok"] is False),
    }


def _print_pack(title: str, p: dict) -> None:
    print(f"\n## {title}")
    print(
        f"n={p['n']}  W={p['w']} L={p['l']} Z={p['z']}  "
        f"WR={_fmt(p['wr'])}%  net=${_fmt(p['net'])}  avg=${_fmt(p['avg'])}"
    )
    print(
        f"avgW=${_fmt(p['avg_w'])}  avgL=${_fmt(p['avg_l'])}  "
        f"medW=${_fmt(p['med_w'])}  medL=${_fmt(p['med_l'])}"
    )
    be = None
    if p["avg_w"] and p["avg_l"] and p["avg_w"] > 0 and p["avg_l"] < 0:
        be = 100 * abs(p["avg_l"]) / (p["avg_w"] + abs(p["avg_l"]))
    print(
        f"planned R:R={_fmt(p['rr'])}  break-even WR≈{_fmt(be)}%  "
        f"avgR={_fmt(p['avg_r'], 3)}  grossR={_fmt(p['avg_gr'], 3)}  "
        f"feeR={_fmt(p['avg_fee_r'], 3)}"
    )
    print(
        f"gross$={_fmt(p['gross'])}  fees$={_fmt(p['fees'])}  "
        f"holdW={_fmt(p['hold_w'])}м  holdL={_fmt(p['hold_l'])}м  "
        f"scoreW={_fmt(p['score_w'], 2)}  scoreL={_fmt(p['score_l'], 2)}  "
        f"p(score)={_fmt(p['score_p'], 3)}"
    )
    print(f"баг SL не с той стороны: {p['sl_bad']}  баг TP: {p['tp_bad']}")


def _group(rows: list[dict], key: str) -> None:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[str(r[key])].append(r)
    print(f"\n### разбивка по {key}")
    print(f"{'ключ':<22} {'n':>5} {'WR%':>6} {'net$':>10} {'avg$':>8} "
          f"{'avgR':>7} {'avgW$':>8} {'avgL$':>8}")
    for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        p = _pack(items)
        print(
            f"{name:<22} {p['n']:5d} {_fmt(p['wr']):>6} {_fmt(p['net']):>10} "
            f"{_fmt(p['avg']):>8} {_fmt(p['avg_r'], 3):>7} "
            f"{_fmt(p['avg_w']):>8} {_fmt(p['avg_l']):>8}"
        )


def diagnose(path: str, name: str, days: int | None) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (time.time() - days * 86400) if days else None
    rows = load_filled(conn, since)
    skipped = conn.execute(
        "SELECT close_reason, COUNT(*) FROM trades WHERE status='closed' "
        "GROUP BY close_reason"
    ).fetchall()
    print(f"# {name}  источник=SQLite {path}")
    print(f"залитые (без entry_*/restart): {len(rows)}"
          + (f"  since last {days}d" if days else "  all history"))
    print("закрытия по reason (включая нефиллы):")
    for r, n in skipped:
        print(f"  {r or '?':<22} {n}")
    if not rows:
        print("нет залитых сделок")
        return
    _print_pack("все залитые", _pack(rows))
    longs = [x for x in rows if x["side"] in ("buy", "long")]
    shorts = [x for x in rows if x["side"] in ("sell", "short")]
    _print_pack("LONG", _pack(longs))
    _print_pack("SHORT", _pack(shorts))
    if longs and shorts:
        p_pnl = _welch_p([x["pnl"] for x in longs], [x["pnl"] for x in shorts])
        p_wr = _binom_p(sum(1 for x in longs if x["pnl"] > 0), len(longs))
        print(
            f"\nперекос стороны: p(avg LONG vs SHORT)={_fmt(p_pnl, 3)}  "
            f"p(WR long vs 50%)={_fmt(p_wr, 3)}"
        )
    _group(rows, "strategy")
    _group(rows, "reason")
    # топ символов по |net|
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    print("\n### символы |net| топ-12")
    ranked = sorted(by_sym.items(), key=lambda kv: -abs(sum(x["pnl"] for x in kv[1])))
    print(f"{'sym':<14} {'n':>5} {'WR%':>6} {'net$':>10} {'long n/net':>16} {'short n/net':>16}")
    for sym, items in ranked[:12]:
        p = _pack(items)
        lg = [x for x in items if x["side"] in ("buy", "long")]
        sh = [x for x in items if x["side"] in ("sell", "short")]
        lp, sp = _pack(lg), _pack(sh)
        print(
            f"{sym:<14} {p['n']:5d} {_fmt(p['wr']):>6} {_fmt(p['net']):>10} "
            f"{lp['n']}/{_fmt(lp['net']):>8}  {sp['n']}/{_fmt(sp['net']):>8}"
        )
    wins = [x for x in rows if x["pnl"] > 0]
    loss = [x for x in rows if x["pnl"] < 0]
    print("\n## вин vs луз — что общее (без новых фильтров)")
    print(
        f"score  W={_fmt(_mean([float(x['score']) for x in wins if x['score'] is not None]), 2)}  "
        f"L={_fmt(_mean([float(x['score']) for x in loss if x['score'] is not None]), 2)}  "
        f"p={_fmt(_welch_p([float(x['score']) for x in wins if x['score'] is not None], [float(x['score']) for x in loss if x['score'] is not None]), 3)}"
    )
    print(
        f"planned R:R  W={_fmt(_mean([x['planned_rr'] for x in wins if x['planned_rr']]), 2)}  "
        f"L={_fmt(_mean([x['planned_rr'] for x in loss if x['planned_rr']]), 2)}"
    )
    print(
        f"SL%  W={_fmt(_mean([x['sl_pct'] for x in wins if x['sl_pct']]), 3)}  "
        f"L={_fmt(_mean([x['sl_pct'] for x in loss if x['sl_pct']]), 3)}"
    )
    sl_hits = [x for x in rows if x["reason"] == "sl_hit"]
    if sl_hits:
        sl_r = [x["r"] for x in sl_hits if x["r"] is not None]
        sl_gr = [x["gross_r"] for x in sl_hits if x["gross_r"] is not None]
        print(
            f"sl_hit n={len(sl_hits)}  avgR={_fmt(_mean(sl_r), 3)}  "
            f"grossR={_fmt(_mean(sl_gr), 3)}  "
            f"(канон полного стопа = −1.0R; разница ≈ издержка+проскальзывание)"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--name", default="scalp")
    ap.add_argument("--days", type=int, default=0,
                    help="0 = вся история; иначе только последние N дней")
    args = ap.parse_args()
    diagnose(args.db, args.name, args.days or None)
    if not args.days:
        print("\n" + "=" * 60)
        print("те же залитые, последние 14 дней")
        diagnose(args.db, args.name + " 14d", 14)


if __name__ == "__main__":
    main()
