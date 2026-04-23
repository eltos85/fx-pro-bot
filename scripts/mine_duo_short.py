"""Углублённый анализ DUO-SHORT ансамбля (TURTLE+VWAP согласны в шорт).

Найденный edge: duo/short N=789, WR 59.7%, EXP +0.033%, PF 1.15.
Задача: найти доп. фильтры которые поднимут PF до 1.3+ и EXP до 0.1+%
БЕЗ сильного снижения выборки (n >= 100).

Срезы внутри duo-short:
  - × session (asia/london/ny/off)
  - × symbol
  - × ADX-бакет
  - × RSI-бакет (гипотеза: вход при RSI>=65 — overbought = классический short-сетап)
  - × ATR%-бакет (low/mid/high волатильность)
  - × EMA50-slope (shorts лучше против тренда?)
  - × комбинации (session + rsi, adx + rsi)
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

IN = Path("data/backtest_trades_enriched.csv")
WINDOW_MIN = 15
MIN_N = 50   # тут опустим до 50 — срезы внутри duo будут меньше
MIN_WR = 0.55
MIN_PF = 1.3


@dataclass
class Trade:
    strategy: str
    symbol: str
    direction: str
    entry_ts: datetime
    net_pct: float
    session: str
    hour_utc: int
    atr_pct: float
    rsi14: float
    adx14: float
    ema50_slope_pct: float
    bb_width_pct: float
    volume_ratio: float
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
                hour_utc=int(row["hour_utc"]),
                atr_pct=float(row["atr_pct"]),
                rsi14=float(row["rsi14"]),
                adx14=float(row["adx14"]),
                ema50_slope_pct=float(row["ema50_slope_pct"]),
                bb_width_pct=float(row["bb_width_pct"]),
                volume_ratio=float(row["volume_ratio"]),
            ))
    return out


def _compute_peers(trs: list[Trade]) -> None:
    window = timedelta(minutes=WINDOW_MIN)
    grp: dict[tuple[str, str], list[tuple[datetime, int, str]]] = defaultdict(list)
    for i, t in enumerate(trs):
        grp[(t.symbol, t.direction)].append((t.entry_ts, i, t.strategy))
    for key in grp:
        grp[key].sort()
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


def _metrics(trs: list[Trade]) -> dict:
    n = len(trs)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trs if t.net_pct > 0]
    losses = [t for t in trs if t.net_pct <= 0]
    wr = len(wins) / n
    exp_pct = sum(t.net_pct for t in trs) / n * 100
    sum_wins = sum(t.net_pct for t in wins)
    sum_losses = abs(sum(t.net_pct for t in losses))
    pf = (sum_wins / sum_losses) if sum_losses > 0 else float("inf")
    return {
        "n": n,
        "wr": wr,
        "exp_pct": exp_pct,
        "pf": pf,
        "sum_pct": sum(t.net_pct for t in trs) * 100,
    }


def _print_slices(title: str, buckets: dict[str, list[Trade]], top_n: int = 20) -> None:
    rows = []
    for key, trs in buckets.items():
        m = _metrics(trs)
        if m["n"] < MIN_N:
            continue
        rows.append((key, m))
    rows.sort(key=lambda x: x[1]["exp_pct"], reverse=True)
    if not rows:
        print(f"\n{title}\n  (нет срезов с n >= {MIN_N})")
        return
    print(f"\n{title}")
    print(f"  {'КЛЮЧ':<50} {'N':>5} {'WR%':>6} {'EXP%':>7} {'PF':>5} {'Σ%':>8}")
    for key, m in rows[:top_n]:
        mark = " ✓" if (m["wr"] >= MIN_WR and m["pf"] >= MIN_PF and m["exp_pct"] > 0) else ""
        pf_s = f"{m['pf']:.2f}" if m["pf"] != float("inf") else " inf"
        print(f"  {key:<50} {m['n']:>5} {m['wr']*100:>5.1f} {m['exp_pct']:>+7.3f} {pf_s:>5} {m['sum_pct']:>+8.1f}{mark}")


def _group(trs, key_fn):
    out = defaultdict(list)
    for t in trs:
        out[key_fn(t)].append(t)
    return out


def main() -> None:
    trs = _load()
    _compute_peers(trs)

    # DUO SHORT
    duo_short = [t for t in trs if t.peers >= 1 and t.direction == "short"]
    print(f"\nБазовый DUO-SHORT: {len(duo_short)} сделок")
    m = _metrics(duo_short)
    print(
        f"  WR={m['wr']*100:.1f}%  EXP={m['exp_pct']:+.3f}%  "
        f"PF={m['pf']:.2f}  Σ={m['sum_pct']:+.1f}%"
    )
    print(f"\nЦель: найти доп. фильтр дающий PF >= {MIN_PF} при n >= {MIN_N}\n")

    print("### 1. DUO-SHORT × SESSION")
    _print_slices("", _group(duo_short, lambda t: f"session={t.session}"))

    print("\n### 2. DUO-SHORT × SYMBOL")
    _print_slices("", _group(duo_short, lambda t: f"sym={t.symbol}"))

    print("\n### 3. DUO-SHORT × HOUR")
    _print_slices("", _group(duo_short, lambda t: f"hour={t.hour_utc:02d}"))

    # RSI 3 bucket: <35 / 35-65 / >=65
    print("\n### 4. DUO-SHORT × RSI")
    def _rsi_b(t):
        r = "rsi<35" if t.rsi14 < 35 else ("rsi35-65" if t.rsi14 < 65 else "rsi>=65")
        return r
    _print_slices("", _group(duo_short, _rsi_b))

    # ADX
    print("\n### 5. DUO-SHORT × ADX")
    def _adx_b(t):
        return "adx<20" if t.adx14 < 20 else ("adx20-30" if t.adx14 < 30 else "adx>=30")
    _print_slices("", _group(duo_short, _adx_b))

    # ATR%
    print("\n### 6. DUO-SHORT × ATR%")
    def _atr_b(t):
        if t.atr_pct < 0.2:
            return "atr<0.2"
        if t.atr_pct < 0.3:
            return "atr0.2-0.3"
        return "atr>=0.3"
    _print_slices("", _group(duo_short, _atr_b))

    # EMA50-slope
    print("\n### 7. DUO-SHORT × EMA50-slope")
    def _slope_b(t):
        s = t.ema50_slope_pct
        if s < -0.05:
            return "slope<-0.05 (downtrend)"
        if s > 0.05:
            return "slope>+0.05 (uptrend)"
        return "slope-flat"
    _print_slices("", _group(duo_short, _slope_b))

    # BB-width (measure of recent volatility)
    print("\n### 8. DUO-SHORT × BB-width")
    def _bb_b(t):
        if t.bb_width_pct < 0.5:
            return "bb<0.5 (squeeze)"
        if t.bb_width_pct < 1.0:
            return "bb0.5-1.0"
        if t.bb_width_pct < 2.0:
            return "bb1.0-2.0"
        return "bb>=2.0"
    _print_slices("", _group(duo_short, _bb_b))

    # КОМБО: session + RSI
    print("\n### 9. DUO-SHORT × (SESSION, RSI)")
    def _sess_rsi(t):
        return f"{t.session}/{_rsi_b(t)}"
    _print_slices("", _group(duo_short, _sess_rsi), top_n=25)

    # КОМБО: symbol + RSI
    print("\n### 10. DUO-SHORT × (SYMBOL, RSI)")
    def _sym_rsi(t):
        return f"{t.symbol}/{_rsi_b(t)}"
    _print_slices("", _group(duo_short, _sym_rsi), top_n=30)

    # КОМБО: session + symbol + RSI (самый узкий, но возможно самый чистый)
    print("\n### 11. DUO-SHORT × (SESSION, SYMBOL) — топ-30")
    _print_slices("", _group(duo_short, lambda t: f"{t.session}/{t.symbol}"), top_n=30)

    # Золотая комбинация: high RSI (>=65) + no uptrend (slope <= 0)
    print("\n### 12. ГИПОТЕЗА: DUO-SHORT + rsi>=65 + slope<=0 (по символам)")
    filt = [t for t in duo_short if t.rsi14 >= 65 and t.ema50_slope_pct <= 0]
    print(f"  Всего после фильтра: {len(filt)}")
    if filt:
        mall = _metrics(filt)
        print(
            f"  WR={mall['wr']*100:.1f}%  EXP={mall['exp_pct']:+.3f}%  "
            f"PF={mall['pf']:.2f}  Σ={mall['sum_pct']:+.1f}%"
        )
        _print_slices("  По символам:", _group(filt, lambda t: t.symbol))


if __name__ == "__main__":
    main()
