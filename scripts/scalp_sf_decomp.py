"""Глубокий разбор sweep_fade (база) с cutoff 2026-06-17: где концентрируется
минус. Read-only. Запуск:

    docker exec -i fx-pro-bot-scalp-bot-1 python3 - < scripts/scalp_sf_decomp.py

Разбивки:
 1. по символам (n / WR / net / avgR)
 2. по сторонам (long/short)
 3. по символу × стороне  (ловим side-асимметрию и toxic-символы)
 4. по close_reason (sl_hit / tp_hit / flow_exit / ...)
 5. по дням + день × сторона
 6. размер ход-в-плюс vs ход-в-минус (win/loss avg $) — payoff-асимметрия
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

DB = "/data/scalp_bot.sqlite"
STRAT = "sweep_fade"
SINCE = datetime.fromisoformat("2026-06-17").replace(tzinfo=UTC).timestamp()

_NON_TRADE = ("restart_flat", "entry_Cancelled", "entry_Rejected",
              "entry_Deactivated", "entry_timeout")


def _day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts or 0, UTC).strftime("%Y-%m-%d")


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = [r["name"] for r in con.execute("PRAGMA table_info(trades)")]
    need = {"symbol", "side", "close_reason", "ts_open", "ts_close", "status",
            "pnl_usd", "entry_price", "exit_price", "sl_price", "tp_price",
            "qty"}
    miss = need - set(cols)
    if miss:
        print("WARN: отсутствуют колонки:", miss, "— часть метрик пропадёт")

    rows = [dict(r) for r in con.execute(
        "SELECT * FROM trades WHERE strategy=? AND ts_open>=?",
        (STRAT, SINCE))]
    real = [r for r in rows
            if r["status"] == "closed"
            and str(r["close_reason"] or "") not in _NON_TRADE
            and not str(r["close_reason"] or "").startswith("entry_")
            and r["pnl_usd"] is not None]
    n = len(real)
    print(f"\n=== {STRAT} с 2026-06-17 | реальных сделок: {n} ===\n")

    def R(r):
        """Приблизительная R-единица исхода: pnl / риск($). Риск = |entry-sl|*qty."""
        try:
            risk = abs((r["entry_price"] or 0) - (r["sl_price"] or 0)) * (r["qty"] or 0)
            if risk and risk > 0:
                return r["pnl_usd"] / risk
        except Exception:
            pass
        return None

    # 1. по символам
    print("--- по символам ---")
    print(f"{'symbol':<10}{'n':>5}{'wins':>6}{'WR%':>7}{'net$':>11}{'avgR':>8}")
    by_sym: dict[str, list] = defaultdict(list)
    for r in real:
        by_sym[r["symbol"]].append(r)
    for sym in sorted(by_sym, key=lambda k: sum(x["pnl_usd"] for x in by_sym[k])):
        g = by_sym[sym]
        w = sum(1 for x in g if x["pnl_usd"] > 0)
        net = sum(x["pnl_usd"] for x in g)
        rs = [R(x) for x in g if R(x) is not None]
        avgR = sum(rs) / len(rs) if rs else 0
        print(f"{sym:<10}{len(g):>5}{w:>6}{100*w/len(g):>6.0f}%{net:>11.2f}{avgR:>8.2f}")

    # 2. по сторонам
    print("\n--- по сторонам ---")
    print(f"{'side':<8}{'n':>5}{'wins':>6}{'WR%':>7}{'net$':>11}{'avgR':>8}")
    by_side: dict[str, list] = defaultdict(list)
    for r in real:
        by_side[r["side"]].append(r)
    for sd in sorted(by_side):
        g = by_side[sd]
        w = sum(1 for x in g if x["pnl_usd"] > 0)
        net = sum(x["pnl_usd"] for x in g)
        rs = [R(x) for x in g if R(x) is not None]
        avgR = sum(rs) / len(rs) if rs else 0
        print(f"{sd:<8}{len(g):>5}{w:>6}{100*w/len(g):>6.0f}%{net:>11.2f}{avgR:>8.2f}")

    # 3. символ × сторона
    print("\n--- символ × сторона ---")
    print(f"{'symbol':<10}{'side':<8}{'n':>5}{'wins':>6}{'WR%':>7}{'net$':>11}{'avgR':>8}")
    by_ss: dict[tuple, list] = defaultdict(list)
    for r in real:
        by_ss[(r["symbol"], r["side"])].append(r)
    for k in sorted(by_ss, key=lambda k: sum(x["pnl_usd"] for x in by_ss[k])):
        g = by_ss[k]
        w = sum(1 for x in g if x["pnl_usd"] > 0)
        net = sum(x["pnl_usd"] for x in g)
        rs = [R(x) for x in g if R(x) is not None]
        avgR = sum(rs) / len(rs) if rs else 0
        print(f"{k[0]:<10}{k[1]:<8}{len(g):>5}{w:>6}{100*w/len(g):>6.0f}%{net:>11.2f}{avgR:>8.2f}")

    # 4. по close_reason
    print("\n--- по close_reason ---")
    print(f"{'reason':<18}{'n':>5}{'wins':>6}{'WR%':>7}{'net$':>11}{'avg$':>9}")
    by_cr: dict[str, list] = defaultdict(list)
    for r in real:
        by_cr[str(r["close_reason"])].append(r)
    for cr in sorted(by_cr, key=lambda k: -len(by_cr[k])):
        g = by_cr[cr]
        w = sum(1 for x in g if x["pnl_usd"] > 0)
        net = sum(x["pnl_usd"] for x in g)
        print(f"{cr:<18}{len(g):>5}{w:>6}{100*w/len(g):>6.0f}%{net:>11.2f}{net/len(g):>9.2f}")

    # 5. день × сторона
    print("\n--- день × сторона ---")
    print(f"{'day':<12}{'side':<8}{'n':>5}{'wins':>6}{'WR%':>7}{'net$':>11}")
    by_ds: dict[tuple, list] = defaultdict(list)
    for r in real:
        by_ds[(_day(r["ts_close"] or r["ts_open"]), r["side"])].append(r)
    cur_day = None
    for k in sorted(by_ds):
        if k[0] != cur_day:
            if cur_day is not None:
                print()
            cur_day = k[0]
        g = by_ds[k]
        w = sum(1 for x in g if x["pnl_usd"] > 0)
        net = sum(x["pnl_usd"] for x in g)
        print(f"{k[0]:<12}{k[1]:<8}{len(g):>5}{w:>6}{100*w/len(g):>6.0f}%{net:>11.2f}")

    # 6. payoff-асимметрия: средний win vs средний loss
    wins = [r for r in real if r["pnl_usd"] > 0]
    losses = [r for r in real if r["pnl_usd"] <= 0]
    avgW = sum(r["pnl_usd"] for r in wins) / len(wins) if wins else 0
    avgL = sum(r["pnl_usd"] for r in losses) / len(losses) if losses else 0
    print("\n--- payoff-асимметрия ---")
    print(f"средний WIN:  ${avgW:+.2f} (n={len(wins)})")
    print(f"средний LOSS: ${avgL:+.2f} (n={len(losses)})")
    wr = 100 * len(wins) / n if n else 0
    exp = (wr / 100) * avgW + (1 - wr / 100) * avgL
    be_wr = -avgL / (avgW - avgL) * 100 if (avgW - avgL) != 0 else 0
    print(f"WR={wr:.1f}% | expectancy=${exp:+.2f} | break-even WR={be_wr:.1f}%")
    print(f"R:R (avgW/|avgL|) = {avgW/abs(avgL):.2f} : 1" if avgL else "")

    # 7. payoff по close_reason для win/loss
    print("\n--- средний $ по close_reason (win vs loss) ---")
    print(f"{'reason':<18}{'n':>5}{'avg$':>10}{'win$':>10}{'loss$':>10}")
    for cr in sorted(by_cr):
        g = by_cr[cr]
        gw = [x for x in g if x["pnl_usd"] > 0]
        gl = [x for x in g if x["pnl_usd"] <= 0]
        avg = sum(x["pnl_usd"] for x in g) / len(g)
        aw = sum(x["pnl_usd"] for x in gw) / len(gw) if gw else 0
        al = sum(x["pnl_usd"] for x in gl) / len(gl) if gl else 0
        print(f"{cr:<18}{len(g):>5}{avg:>10.2f}{aw:>10.2f}{al:>10.2f}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
