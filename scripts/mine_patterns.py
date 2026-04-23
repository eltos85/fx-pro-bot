"""Pattern-mining на data/backtest_trades_enriched.csv.

Искомое: срезы где WR >= 55%, PF >= 1.3, n >= 100.

Срезы:
  1. Одномерные: session, symbol, direction, DoW, hour_utc, strategy
  2. Бакетные (квантили): adx14, rsi14, atr_pct, bb_width_pct, volume_ratio,
     ema20_slope_pct, ema50_slope_pct, range_24h_pct
  3. Двумерные: session × direction, session × symbol, adx_bucket × direction
  4. Trend-congruence: direction vs знак ema50_slope_pct
     (long при + slope = попутный; short при - slope = попутный)

Вход: data/backtest_trades_enriched.csv
Выход: stdout — таблицы топ-срезов по Expectancy с фильтром n >= 100.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median

IN = Path("data/backtest_trades_enriched.csv")
MIN_N = 100
MIN_WR = 0.55
MIN_PF = 1.3


@dataclass
class Trade:
    strategy: str
    symbol: str
    direction: str  # long / short
    net_pct: float
    session: str
    hour_utc: int
    dow: int
    atr_pct: float
    rsi14: float
    adx14: float
    ema20_slope_pct: float
    ema50_slope_pct: float
    bb_width_pct: float
    volume_ratio: float
    range_24h_pct: float


def _load() -> list[Trade]:
    out: list[Trade] = []
    with IN.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            out.append(Trade(
                strategy=row["strategy"],
                symbol=row["symbol"],
                direction=row["direction"],
                net_pct=float(row["net_pct"]),
                session=row["session"],
                hour_utc=int(row["hour_utc"]),
                dow=int(row["day_of_week"]),
                atr_pct=float(row["atr_pct"]),
                rsi14=float(row["rsi14"]),
                adx14=float(row["adx14"]),
                ema20_slope_pct=float(row["ema20_slope_pct"]),
                ema50_slope_pct=float(row["ema50_slope_pct"]),
                bb_width_pct=float(row["bb_width_pct"]),
                volume_ratio=float(row["volume_ratio"]),
                range_24h_pct=float(row["range_24h_pct"]),
            ))
    return out


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


def _quantile_bucket(values: list[float], q: int = 3) -> list[float]:
    """Возвращает точки разбиения для q квантилей (q=3 → терциали)."""
    if not values:
        return []
    s = sorted(values)
    cuts = []
    for i in range(1, q):
        pos = i * len(s) // q
        cuts.append(s[pos])
    return cuts


def _bucket_name(v: float, cuts: list[float], labels: list[str]) -> str:
    for i, c in enumerate(cuts):
        if v <= c:
            return labels[i]
    return labels[-1]


def _print_slices(title: str, buckets: dict[str, list[Trade]], top_n: int = 30) -> None:
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
    print(f"  {'КЛЮЧ':<55} {'N':>5} {'WR%':>6} {'EXP%':>7} {'PF':>5} {'Σ%':>8}")
    for key, m in rows[:top_n]:
        mark = " ✓" if (m["wr"] >= MIN_WR and m["pf"] >= MIN_PF and m["exp_pct"] > 0) else ""
        pf_s = f"{m['pf']:.2f}" if m["pf"] != float("inf") else " inf"
        print(f"  {key:<55} {m['n']:>5} {m['wr']*100:>5.1f} {m['exp_pct']:>+7.3f} {pf_s:>5} {m['sum_pct']:>+8.1f}{mark}")


def _bucket_by(
    trs: list[Trade],
    getter,
    label: str,
) -> dict[str, list[Trade]]:
    out = defaultdict(list)
    for t in trs:
        out[f"{label}={getter(t)}"].append(t)
    return out


def _bucket_by_quantile(
    trs: list[Trade],
    getter,
    label: str,
    q: int = 3,
    suffix_labels: list[str] | None = None,
) -> dict[str, list[Trade]]:
    vals = [getter(t) for t in trs]
    cuts = _quantile_bucket(vals, q=q)
    if suffix_labels is None:
        suffix_labels = [f"q{i+1}" for i in range(q)]
    out: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        bname = _bucket_name(getter(t), cuts, suffix_labels)
        key = f"{label}[{bname}, cuts={[round(c, 3) for c in cuts]}]"
        out[key].append(t)
    return out


def main() -> None:
    trs = _load()
    print(f"Загружено {len(trs)} сделок\n")

    # BASE RATE
    print("=" * 72)
    print("BASE RATE (все сделки)")
    m = _metrics(trs)
    print(
        f"  N={m['n']}  WR={m['wr']*100:.1f}%  EXP={m['exp_pct']:+.3f}%  "
        f"PF={m['pf']:.2f}  Σ={m['sum_pct']:+.1f}%"
    )
    print("=" * 72)
    print(f"\nКритерии прохождения: n >= {MIN_N}, WR >= {MIN_WR*100:.0f}%, PF >= {MIN_PF}, EXP > 0")
    print("(помечены ✓)\n")

    # 1. ОДНОМЕРНЫЕ СРЕЗЫ
    print("### 1. ПО СТРАТЕГИЯМ")
    _print_slices("Стратегии:", _bucket_by(trs, lambda t: t.strategy, "strategy"))

    print("\n### 2. ПО СЕССИЯМ (UTC)")
    _print_slices("Сессии:", _bucket_by(trs, lambda t: t.session, "session"))

    print("\n### 3. ПО ЧАСУ UTC")
    _print_slices("Час UTC:", _bucket_by(trs, lambda t: f"{t.hour_utc:02d}", "hour"))

    print("\n### 4. ПО СИМВОЛАМ")
    _print_slices("Символы:", _bucket_by(trs, lambda t: t.symbol, "symbol"))

    print("\n### 5. ПО НАПРАВЛЕНИЯМ")
    _print_slices("Направление:", _bucket_by(trs, lambda t: t.direction, "dir"))

    print("\n### 6. ПО ДНЮ НЕДЕЛИ")
    _print_slices("DoW:", _bucket_by(trs, lambda t: t.dow, "dow(0=Mon)"))

    # 2. КВАНТИЛЬНЫЕ БАКЕТЫ
    qlabels = ["low", "mid", "high"]
    print("\n### 7. ADX14 (терцили)")
    _print_slices("ADX14:", _bucket_by_quantile(trs, lambda t: t.adx14, "adx14", 3, qlabels))

    print("\n### 8. RSI14 (терцили)")
    _print_slices("RSI14:", _bucket_by_quantile(trs, lambda t: t.rsi14, "rsi14", 3, qlabels))

    print("\n### 9. ATR% (терцили)")
    _print_slices("ATR%:", _bucket_by_quantile(trs, lambda t: t.atr_pct, "atr_pct", 3, qlabels))

    print("\n### 10. BB-width % (терцили)")
    _print_slices("BB-width:", _bucket_by_quantile(trs, lambda t: t.bb_width_pct, "bb_w", 3, qlabels))

    print("\n### 11. Volume-ratio (терцили)")
    _print_slices("Volume-ratio:", _bucket_by_quantile(trs, lambda t: t.volume_ratio, "vol_r", 3, qlabels))

    print("\n### 12. EMA20-slope % (терцили)")
    _print_slices("EMA20-slope:", _bucket_by_quantile(trs, lambda t: t.ema20_slope_pct, "ema20_s", 3, qlabels))

    print("\n### 13. EMA50-slope % (терцили)")
    _print_slices("EMA50-slope:", _bucket_by_quantile(trs, lambda t: t.ema50_slope_pct, "ema50_s", 3, qlabels))

    print("\n### 14. 24h-Range % (терцили)")
    _print_slices("Range24h%:", _bucket_by_quantile(trs, lambda t: t.range_24h_pct, "range24h", 3, qlabels))

    # 3. TREND-CONGRUENCE
    print("\n### 15. TREND-CONGRUENCE (направление vs EMA50-slope)")
    def _congr(t: Trade) -> str:
        if t.direction == "long":
            return "long×up" if t.ema50_slope_pct > 0 else ("long×down" if t.ema50_slope_pct < 0 else "long×flat")
        return "short×up" if t.ema50_slope_pct > 0 else ("short×down" if t.ema50_slope_pct < 0 else "short×flat")
    _print_slices("Congruence:", _bucket_by(trs, _congr, ""))

    # 4. ДВУМЕРНЫЕ СРЕЗЫ
    print("\n### 16. СЕССИЯ × НАПРАВЛЕНИЕ")
    _print_slices("Session×Dir:", _bucket_by(trs, lambda t: f"{t.session}/{t.direction}", ""))

    print("\n### 17. СЕССИЯ × СИМВОЛ (топ-30)")
    _print_slices("Session×Sym:", _bucket_by(trs, lambda t: f"{t.session}/{t.symbol}", ""), top_n=30)

    print("\n### 18. СТРАТА × СЕССИЯ")
    _print_slices("Strat×Session:", _bucket_by(trs, lambda t: f"{t.strategy}/{t.session}", ""))

    print("\n### 19. СТРАТА × СИМВОЛ (топ-30)")
    _print_slices("Strat×Sym:", _bucket_by(trs, lambda t: f"{t.strategy}/{t.symbol}", ""), top_n=30)

    print("\n### 20. ADX-бакет × НАПРАВЛЕНИЕ")
    # ручные пороги ADX: < 20, 20-30, > 30
    def _adx_dir(t: Trade) -> str:
        a = "adx<20" if t.adx14 < 20 else ("adx20-30" if t.adx14 < 30 else "adx>=30")
        return f"{a}/{t.direction}"
    _print_slices("ADX×Dir:", _bucket_by(trs, _adx_dir, ""))

    print("\n### 21. RSI-бакет × НАПРАВЛЕНИЕ")
    def _rsi_dir(t: Trade) -> str:
        r = "rsi<35" if t.rsi14 < 35 else ("rsi35-65" if t.rsi14 < 65 else "rsi>=65")
        return f"{r}/{t.direction}"
    _print_slices("RSI×Dir:", _bucket_by(trs, _rsi_dir, ""))


if __name__ == "__main__":
    main()
