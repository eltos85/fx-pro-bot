"""Оценка гипотезы 'Crypto Overbought Fader' (COF) по критерию пользователя
% прибыльных дней > 55% + PF > 1.3 + n >= 100.

Вариант A (самый широкий): DUO-SHORT × NY × RSI>=65 → n=299 в истории
Вариант B (широкий по ATR): DUO-SHORT × ATR>=0.3 → n=255
Вариант C (символ-специфичный): DUO-SHORT × {SOL,LINK,AVAX} × RSI>=65 → n=203
Вариант D (ещё строже): DUO-SHORT × NY × {SOL,LINK,AVAX,ADA} × RSI>=65
Вариант E (полный стек): DUO-SHORT × NY × RSI>=65 × ATR>=0.3
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

IN = Path("data/backtest_trades_enriched.csv")
WINDOW_MIN = 15


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
    out: list[Trade] = []
    with IN.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
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


def _compute_peers(trs: list[Trade]) -> None:
    window = timedelta(minutes=WINDOW_MIN)
    grp = defaultdict(list)
    for i, t in enumerate(trs):
        grp[(t.symbol, t.direction)].append((t.entry_ts, i, t.strategy))
    for k in grp:
        grp[k].sort()
    for items in grp.values():
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


def _eval(name: str, sel: list[Trade]) -> None:
    n = len(sel)
    if n == 0:
        print(f"\n{name}: пусто"); return
    wins = [t for t in sel if t.net_pct > 0]
    losses = [t for t in sel if t.net_pct <= 0]
    wr = len(wins) / n
    exp = sum(t.net_pct for t in sel) / n * 100
    sw = sum(t.net_pct for t in wins); sl = abs(sum(t.net_pct for t in losses))
    pf = sw / sl if sl > 0 else float("inf")
    sum_pct = sum(t.net_pct for t in sel) * 100

    # Daily breakdown
    daily_pnl = defaultdict(float)
    daily_count = defaultdict(int)
    for t in sel:
        d = t.entry_ts.date()
        daily_pnl[d] += t.net_pct
        daily_count[d] += 1
    days = sorted(daily_pnl.keys())
    n_days = len(days)
    profitable_days = sum(1 for d in days if daily_pnl[d] > 0)
    day_pct = profitable_days / n_days * 100 if n_days else 0
    avg_trades_per_day = n / n_days if n_days else 0

    # Avg PnL on profitable days
    avg_win_day = sum(daily_pnl[d] for d in days if daily_pnl[d] > 0) / max(profitable_days, 1) * 100
    avg_loss_day = sum(daily_pnl[d] for d in days if daily_pnl[d] <= 0) / max(n_days - profitable_days, 1) * 100

    # Max drawdown (by daily cumulative)
    cum = 0.0; peak = 0.0; mdd = 0.0
    for d in days:
        cum += daily_pnl[d]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)

    print(f"\n=== {name} ===")
    print(f"  N={n}  WR={wr*100:.1f}%  EXP={exp:+.3f}%  PF={pf:.2f}  Σ={sum_pct:+.1f}%")
    print(f"  Дней с сигналом: {n_days}  Прибыльных дней: {profitable_days} ({day_pct:.1f}%)")
    print(f"  Ср. сделок/день: {avg_trades_per_day:.2f}")
    print(f"  Ср. +день: {avg_win_day:+.3f}%  ср. -день: {avg_loss_day:+.3f}%")
    print(f"  Max drawdown (cum%): {mdd:+.2f}%")

    crit_ok = (day_pct >= 55) and (pf >= 1.3) and (exp > 0) and (n >= 100)
    status = "✓ ПРОХОДИТ критерий пользователя" if crit_ok else "✗ не проходит"
    print(f"  Статус: {status}")


def main() -> None:
    trs = _load()
    _compute_peers(trs)
    duo_short = [t for t in trs if t.peers >= 1 and t.direction == "short"]
    print(f"DUO-SHORT base: {len(duo_short)} сделок")

    # A — NY × RSI≥65
    sel = [t for t in duo_short if t.session == "ny" and t.rsi14 >= 65]
    _eval("A. NY × RSI>=65 (бёрла COF)", sel)

    # B — ATR≥0.3 (без других фильтров)
    sel = [t for t in duo_short if t.atr_pct >= 0.3]
    _eval("B. ATR>=0.3 (высокая волатильность)", sel)

    # C — {SOL,LINK,AVAX} × RSI≥65
    syms = {"SOLUSDT", "LINKUSDT", "AVAXUSDT"}
    sel = [t for t in duo_short if t.symbol in syms and t.rsi14 >= 65]
    _eval("C. symbols={SOL,LINK,AVAX} × RSI>=65", sel)

    # D — NY × {SOL,LINK,AVAX,ADA} × RSI≥65
    syms_ex = {"SOLUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT"}
    sel = [t for t in duo_short if t.session == "ny" and t.symbol in syms_ex and t.rsi14 >= 65]
    _eval("D. NY × {SOL,LINK,AVAX,ADA} × RSI>=65 (рекомендованный рецепт)", sel)

    # E — NY × RSI≥65 × ATR≥0.3 (полный стек)
    sel = [t for t in duo_short if t.session == "ny" and t.rsi14 >= 65 and t.atr_pct >= 0.3]
    _eval("E. NY × RSI>=65 × ATR>=0.3 (максимально строгий)", sel)

    # F — NY × {SOL,LINK,AVAX,ADA} × RSI≥65 × ATR≥0.3 (ВСЁ ВМЕСТЕ)
    sel = [t for t in duo_short if t.session == "ny" and t.symbol in syms_ex
           and t.rsi14 >= 65 and t.atr_pct >= 0.3]
    _eval("F. NY × {SOL,LINK,AVAX,ADA} × RSI>=65 × ATR>=0.3 (полная гипотеза COF)", sel)


if __name__ == "__main__":
    main()
