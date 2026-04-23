"""OOS-валидация гипотезы COF.

Делим 90 дней истории на 2 окна:
  TRAIN: первые 60 дней (23 янв – 24 мар) — на них мы смотрели при mining
  TEST: последние 30 дней (25 мар – 23 апр) — это out-of-sample

Применяем правила Варианта E (DUO-SHORT × NY × RSI>=65 × ATR>=0.3) + ещё
нескольких альтернатив к обоим окнам. Если на TEST метрики близки к TRAIN —
гипотеза выдержала OOS. Если падают к base rate — это был overfit.

Также тестируем variant A/C/D.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path

IN = Path("data/backtest_trades_enriched.csv")
WINDOW_MIN = 15

# Границы истории видны из базового backtest: 23 янв 2026 → 23 апр 2026.
# Train = 23 янв – 24 мар (60 дней). Test = 25 мар – 23 апр (30 дней).
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


def _compute(sel: list[Trade]) -> dict:
    n = len(sel)
    if n == 0:
        return {"n": 0, "wr": 0, "exp": 0, "pf": 0, "sum": 0, "days": 0, "pdays_pct": 0}
    wins = [t for t in sel if t.net_pct > 0]
    losses = [t for t in sel if t.net_pct <= 0]
    wr = len(wins) / n
    exp = sum(t.net_pct for t in sel) / n * 100
    sw = sum(t.net_pct for t in wins); sl = abs(sum(t.net_pct for t in losses))
    pf = sw / sl if sl > 0 else float("inf")
    sum_pct = sum(t.net_pct for t in sel) * 100

    daily_pnl = defaultdict(float)
    for t in sel:
        daily_pnl[t.entry_ts.date()] += t.net_pct
    days = list(daily_pnl.keys())
    pdays = sum(1 for d in days if daily_pnl[d] > 0)
    pdays_pct = pdays / len(days) * 100 if days else 0
    return {
        "n": n,
        "wr": wr * 100,
        "exp": exp,
        "pf": pf if pf != float("inf") else 99.0,
        "sum": sum_pct,
        "days": len(days),
        "pdays_pct": pdays_pct,
    }


def _fmt(m: dict) -> str:
    return (
        f"N={m['n']:>4} WR={m['wr']:>5.1f}% EXP={m['exp']:+.3f}% "
        f"PF={m['pf']:.2f} Σ={m['sum']:+.1f}% d={m['days']:>2} +d%={m['pdays_pct']:.1f}"
    )


def _run_variant(name: str, rule, all_trs: list[Trade]) -> None:
    train = [t for t in all_trs if t.entry_ts.date() < SPLIT_DATE and rule(t)]
    test = [t for t in all_trs if t.entry_ts.date() >= SPLIT_DATE and rule(t)]
    m_tr = _compute(train)
    m_te = _compute(test)
    # сравнение
    delta_pf = m_te["pf"] - m_tr["pf"]
    delta_wr = m_te["wr"] - m_tr["wr"]
    delta_pd = m_te["pdays_pct"] - m_tr["pdays_pct"]
    oos_ok = (
        m_te["n"] >= 30 and
        m_te["wr"] >= 55 and
        m_te["pf"] >= 1.3 and
        m_te["exp"] > 0 and
        m_te["pdays_pct"] >= 55
    )
    print(f"\n=== {name} ===")
    print(f"  TRAIN (до {SPLIT_DATE}):  {_fmt(m_tr)}")
    print(f"  TEST  (с {SPLIT_DATE}):   {_fmt(m_te)}")
    print(f"  Δ test-train:            WR={delta_wr:+.1f}  PF={delta_pf:+.2f}  +d%={delta_pd:+.1f}")
    status = "✓ OOS ПОДТВЕРЖДЁН" if oos_ok else "✗ OOS не прошёл"
    print(f"  {status}")


def main() -> None:
    trs = _load()
    _compute_peers(trs)
    duo_short = [t for t in trs if t.peers >= 1 and t.direction == "short"]
    all_short = duo_short  # базовая "область" — все DUO-SHORT

    total_days = (
        max(t.entry_ts.date() for t in trs) - min(t.entry_ts.date() for t in trs)
    ).days + 1
    train_days = (SPLIT_DATE - min(t.entry_ts.date() for t in trs)).days
    test_days = total_days - train_days
    print(f"Окно истории: {min(t.entry_ts.date() for t in trs)} – {max(t.entry_ts.date() for t in trs)} ({total_days} д.)")
    print(f"SPLIT: TRAIN {train_days} дней / TEST {test_days} дней")
    print(f"Базовый DUO-SHORT: {len(all_short)}")

    # BASE RATE (для сравнения)
    print("\n--- BASE RATES (без фильтров) ---")
    _run_variant("BASE all DUO-SHORT", lambda t: True, all_short)

    # Variant A: NY × RSI>=65
    _run_variant("A. NY × RSI>=65", lambda t: t.session == "ny" and t.rsi14 >= 65, all_short)

    # Variant C: symbols={SOL,LINK,AVAX} × RSI>=65
    syms = {"SOLUSDT", "LINKUSDT", "AVAXUSDT"}
    _run_variant("C. {SOL,LINK,AVAX} × RSI>=65",
                 lambda t: t.symbol in syms and t.rsi14 >= 65, all_short)

    # Variant D: NY × {SOL,LINK,AVAX,ADA} × RSI>=65
    syms_ex = {"SOLUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT"}
    _run_variant("D. NY × {SOL,LINK,AVAX,ADA} × RSI>=65",
                 lambda t: t.session == "ny" and t.symbol in syms_ex and t.rsi14 >= 65, all_short)

    # Variant E: NY × RSI>=65 × ATR>=0.3 (наш favorite)
    _run_variant("E. NY × RSI>=65 × ATR>=0.3 (favorite)",
                 lambda t: t.session == "ny" and t.rsi14 >= 65 and t.atr_pct >= 0.3, all_short)

    # Variant F (для полноты — полный стек)
    _run_variant("F. NY × {SOL,LINK,AVAX,ADA} × RSI>=65 × ATR>=0.3",
                 lambda t: (t.session == "ny" and t.symbol in syms_ex
                            and t.rsi14 >= 65 and t.atr_pct >= 0.3), all_short)


if __name__ == "__main__":
    main()
