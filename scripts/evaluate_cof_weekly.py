"""Оценка COF-вариантов по WEEKLY-критерию вместо daily.

Группировка по ISO-неделям (Mon-Sun). Нужно:
  - >= 55% прибыльных недель
  - >= 10 недель в выборке (минимум для стат. значимости)
  - PF >= 1.3 и EXP > 0

Также показываем:
  - Серия подряд убыточных недель (max losing streak)
  - Cum-PnL-график по неделям
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path

IN = Path("data/backtest_trades_enriched.csv")
WINDOW_MIN = 15
SPLIT_DATE = date(2026, 3, 25)


@dataclass
class Trade:
    strategy: str
    symbol: str
    direction: str
    entry_ts: datetime
    net_pct: float
    session: str
    atr_pct: float
    rsi14: float
    peers: int = 0


def _load() -> list[Trade]:
    out = []
    with IN.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(Trade(
                strategy=row["strategy"],
                symbol=row["symbol"],
                direction=row["direction"],
                entry_ts=datetime.fromisoformat(row["entry_ts"]),
                net_pct=float(row["net_pct"]),
                session=row["session"],
                atr_pct=float(row["atr_pct"]),
                rsi14=float(row["rsi14"]),
            ))
    return out


def _compute_peers(trs):
    window = timedelta(minutes=WINDOW_MIN)
    grp = defaultdict(list)
    for i, t in enumerate(trs):
        grp[(t.symbol, t.direction)].append((t.entry_ts, i, t.strategy))
    for items in grp.values():
        items.sort()
        n = len(items)
        for a in range(n):
            ts_a, idx_a, strat_a = items[a]
            cnt = 0
            b = a - 1
            while b >= 0 and (ts_a - items[b][0]) <= window:
                if items[b][2] != strat_a:
                    cnt += 1
                b -= 1
            b = a + 1
            while b < n and (items[b][0] - ts_a) <= window:
                if items[b][2] != strat_a:
                    cnt += 1
                b += 1
            trs[idx_a].peers = cnt


def _iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _eval(sel: list[Trade]) -> dict:
    n = len(sel)
    if n == 0:
        return {"n": 0}
    wins = [t for t in sel if t.net_pct > 0]
    losses = [t for t in sel if t.net_pct <= 0]
    wr = len(wins) / n * 100
    exp = sum(t.net_pct for t in sel) / n * 100
    sw = sum(t.net_pct for t in wins); sl = abs(sum(t.net_pct for t in losses))
    pf = sw / sl if sl > 0 else 99.0
    sum_pct = sum(t.net_pct for t in sel) * 100

    daily = defaultdict(float)
    weekly = defaultdict(float)
    for t in sel:
        d = t.entry_ts.date()
        daily[d] += t.net_pct
        weekly[_iso_week_key(d)] += t.net_pct

    days = list(daily.keys())
    pdays = sum(1 for d in days if daily[d] > 0)
    pdays_pct = pdays / len(days) * 100 if days else 0

    wks = sorted(weekly.keys())
    pwks = sum(1 for w in wks if weekly[w] > 0)
    pwks_pct = pwks / len(wks) * 100 if wks else 0

    # Max loss streak (по неделям)
    max_streak = 0; cur = 0
    for w in wks:
        if weekly[w] < 0:
            cur += 1; max_streak = max(max_streak, cur)
        else:
            cur = 0

    # Cum-PnL по неделям
    cum = 0.0; peak = 0.0; mdd = 0.0
    weekly_series = []
    for w in wks:
        cum += weekly[w] * 100
        weekly_series.append((w, weekly[w] * 100, cum))
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)

    return {
        "n": n, "wr": wr, "exp": exp, "pf": pf, "sum": sum_pct,
        "days": len(days), "pdays": pdays_pct,
        "weeks": len(wks), "pweeks": pwks_pct,
        "max_loss_streak": max_streak,
        "mdd_weekly": mdd,
        "weekly_series": weekly_series,
    }


def _fmt_block(name: str, sel: list[Trade], show_weeks: bool = False) -> None:
    m = _eval(sel)
    if m["n"] == 0:
        print(f"\n{name}: пусто"); return
    ok_w = (m["weeks"] >= 10 and m["pweeks"] >= 55 and m["pf"] >= 1.3 and m["exp"] > 0)
    ok_d = (m["days"] >= 30 and m["pdays"] >= 55 and m["pf"] >= 1.3 and m["exp"] > 0)
    print(f"\n=== {name} ===")
    print(f"  N={m['n']}  WR={m['wr']:.1f}%  EXP={m['exp']:+.3f}%  PF={m['pf']:.2f}  Σ={m['sum']:+.1f}%")
    print(f"  DAILY:  {m['days']:>3} дней, +d%={m['pdays']:.1f}%  {'✓' if ok_d else '✗'}")
    print(f"  WEEKLY: {m['weeks']:>3} нед., +w%={m['pweeks']:.1f}%  max_loss_streak={m['max_loss_streak']}  "
          f"cum_mdd={m['mdd_weekly']:+.2f}%  {'✓' if ok_w else '✗'}")
    if show_weeks and m["weekly_series"]:
        print("  Недели:")
        for w, pnl, cum in m["weekly_series"]:
            mark = "+" if pnl > 0 else "-"
            print(f"    {w}  {mark} {pnl:+.2f}%  cum={cum:+.2f}%")


def _run(name, rule, trs, show_weeks=True):
    all_data = [t for t in trs if rule(t)]
    train = [t for t in all_data if t.entry_ts.date() < SPLIT_DATE]
    test = [t for t in all_data if t.entry_ts.date() >= SPLIT_DATE]
    print(f"\n========== {name} ==========")
    _fmt_block("ALL (90 дней)", all_data, show_weeks=show_weeks)
    _fmt_block("TRAIN (60 дней)", train, show_weeks=False)
    _fmt_block("TEST (30 дней)", test, show_weeks=False)


def main():
    trs = _load()
    _compute_peers(trs)
    duo_short = [t for t in trs if t.peers >= 1 and t.direction == "short"]

    print(f"Базовый DUO-SHORT: {len(duo_short)} сделок")

    # Base
    _run("BASE all DUO-SHORT", lambda t: True, duo_short, show_weeks=False)

    syms_lite = {"SOLUSDT", "LINKUSDT", "AVAXUSDT"}
    syms_ext = {"SOLUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT"}

    _run("A. NY × RSI>=65",
         lambda t: t.session == "ny" and t.rsi14 >= 65, duo_short)

    _run("C. {SOL,LINK,AVAX} × RSI>=65",
         lambda t: t.symbol in syms_lite and t.rsi14 >= 65, duo_short)

    _run("D. NY × {SOL,LINK,AVAX,ADA} × RSI>=65",
         lambda t: t.session == "ny" and t.symbol in syms_ext and t.rsi14 >= 65, duo_short)

    _run("E. NY × RSI>=65 × ATR>=0.3 (favorite)",
         lambda t: t.session == "ny" and t.rsi14 >= 65 and t.atr_pct >= 0.3, duo_short)

    _run("F. NY × {SOL,LINK,AVAX,ADA} × RSI>=65 × ATR>=0.3 (полный)",
         lambda t: (t.session == "ny" and t.symbol in syms_ext
                    and t.rsi14 >= 65 and t.atr_pct >= 0.3), duo_short)


if __name__ == "__main__":
    main()
