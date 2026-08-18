"""OOS-проверка единственного выжившего кандидата: TS-момент с удержанием 3 суток.

Контекст
────────
`scripts/scalp_carry_research.py` перебрал 44 испытания по четырём семействам
(perp-only funding carry, delta-neutral carry с ротацией и без, TS-момент) на
180 днях. Все закрылись отрицательно, КРОМЕ одной ячейки TS-момента:
lookback 1 сутки / удержание 3 суток дала ср. +0.85% за период, итог +57.3%,
Sharpe 2.26 — но CI периода [−0.211; +1.917] накрывает ноль, n=59, а ячейка
найдена сканом 9 комбинаций. По `sample-size.mdc` и Bailey/Lopez de Prado
принимать такое нельзя: это кандидат, а не вывод.

Что делает этот скрипт
──────────────────────
Проверяет ровно то же правило на истории, которой оно не видело: 3 года,
разбитые на три года-периода, плюс исходное окно как контроль. Правило
зафиксировано ЗАРАНЕЕ и не тюнится (no-data-fitting.mdc). Полная сетка
печатается целиком, чтобы было видно, единичная ли это ячейка или плато.

Правило (time-series momentum, Moskowitz/Ooi/Pedersen 2012; для крипты
SSRN 4675565 — «evidence of time-series momentum is strong, эффект
сосредоточен в winners, losers часто отскакивают»):
  на метке t знак доходности за lookback → вес +1 (вверх) или −1 (вниз),
  равные веса по всем символам, удержание hold, ребаланс по истечении.
Издержка: taker round-trip 0.110% на единицу оборота
(https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate).

Критерий приёмки, заданный до прогона: правило принимается, только если знак
средней доходности положителен во ВСЕХ трёх годовых подпериодах И хотя бы в
двух из них CI не накрывает ноль. Иначе — шум, кандидат закрывается.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

TAKER_FEE = 0.00055
STEP_H = 8
STEP_MS = STEP_H * 3600 * 1000

# Универсум зафиксирован заранее: мажоры, торгуемые все 3 года (без
# survivorship-подгонки под свежие листинги, ср. «On survivor cryptocurrency
# momentum» — там показано, что доходность момента часто артефакт временно
# доступных монет).
UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
            "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "ATOMUSDT"]


def fetch_4h(sess, symbol: str, start_ms: int) -> dict[int, float]:
    out: dict[int, float] = {}
    end_ms = int(time.time() * 1000)
    while True:
        try:
            resp = sess.get_kline(category="linear", symbol=symbol,
                                  interval="240", start=start_ms,
                                  end=end_ms, limit=1000)
        except Exception:
            break
        rows = resp.get("result", {}).get("list") or []
        if not rows:
            break
        oldest = end_ms
        for r in rows:
            ts = int(r[0])
            out[ts] = float(r[4])
            oldest = min(oldest, ts)
        if len(rows) < 1000 or oldest <= start_ms:
            break
        end_ms = oldest - 1
    return out


def run(price: dict, grid: list[int], lookback: int, hold: int,
        fee: float) -> dict:
    per: list[float] = []
    prev: dict[str, float] = {}
    i = lookback
    while i + hold < len(grid):
        ts = grid[i]
        sig = []
        for s in price:
            p0 = price[s].get(grid[i - lookback])
            p1 = price[s].get(ts)
            if p0 and p1:
                sig.append((1.0 if p1 > p0 else -1.0, s))
        if not sig:
            i += hold
            continue
        w = 1.0 / len(sig)
        pos = {s: sgn * w for sgn, s in sig}
        turn = sum(abs(pos.get(s, 0.0) - prev.get(s, 0.0))
                   for s in set(pos) | set(prev))
        ret = -turn * fee
        ok = True
        for s, wt in pos.items():
            p0, p1 = price[s].get(ts), price[s].get(grid[i + hold])
            if not (p0 and p1):
                ok = False
                break
            ret += wt * (p1 - p0) / p0
        if ok:
            per.append(ret)
            prev = pos
        i += hold
    n = len(per)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(per)
    sd = statistics.stdev(per) if n > 1 else 0.0
    ppy = (365 * 24 / STEP_H) / hold
    se = sd / math.sqrt(n)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in per:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return {"n": n, "mean_pct": mean * 100, "total_pct": (eq - 1) * 100,
            "sharpe": (mean / sd * math.sqrt(ppy)) if sd else 0.0,
            "mdd_pct": mdd * 100,
            "ci_lo": (mean - 1.96 * se) * 100,
            "ci_hi": (mean + 1.96 * se) * 100}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    args = ap.parse_args()
    from pybit.unified_trading import HTTP
    sess = HTTP()

    days = args.years * 365
    start_ms = int((time.time() - days * 86400) * 1000)
    grid_start = (start_ms // STEP_MS + 1) * STEP_MS
    grid_all = list(range(grid_start, int(time.time() * 1000), STEP_MS))

    print(f"универсум зафиксирован заранее: {len(UNIVERSE)} мажоров")
    print(f"история: {args.years} года, сетка {STEP_H}ч из 4ч-баров")
    price: dict[str, dict[int, float]] = {}
    for sym in UNIVERSE:
        bars = fetch_4h(sess, sym, start_ms)
        got = {ts: bars[ts] for ts in grid_all if ts in bars}
        if len(got) >= 500:
            price[sym] = got
        print(f"    {sym:<10} баров_на_сетке={len(got)}")
    grid = [ts for ts in grid_all
            if sum(1 for s in price if ts in price[s]) >= len(price) - 2]
    print(f"панель: символов={len(price)} меток={len(grid)} "
          f"({time.strftime('%Y-%m-%d', time.gmtime(grid[0] / 1000))} .. "
          f"{time.strftime('%Y-%m-%d', time.gmtime(grid[-1] / 1000))})")

    # годовые подпериоды: честный OOS для правила, найденного на 2026-H1
    per_year = (365 * 24 // STEP_H)
    chunks = []
    for k in range(args.years):
        seg = grid[k * per_year:(k + 1) * per_year]
        if len(seg) > 100:
            lo = time.strftime('%Y-%m-%d', time.gmtime(seg[0] / 1000))
            hi = time.strftime('%Y-%m-%d', time.gmtime(seg[-1] / 1000))
            chunks.append((f"{lo}..{hi}", seg))

    print("\n" + "=" * 104)
    print("КАНДИДАТ: lookback=3 (1 сутки), hold=9 (3 суток) — ячейка, "
          "выжившая на 180 днях")
    print("=" * 104)
    print(f"{'подпериод':<26}{'n':>5}{'ср.%':>9}{'итог%':>9}{'Sharpe':>8}"
          f"{'проc.%':>9}{'95% CI периода':>26}")
    signs = []
    excl = 0
    for name, seg in chunks + [("ВСЯ история", grid)]:
        r = run(price, seg, 3, 9, 2 * TAKER_FEE)
        if r.get("n", 0) < 5:
            print(f"{name:<26}{r.get('n', 0):>5}  мало данных")
            continue
        ci = f"[{r['ci_lo']:+.4f}; {r['ci_hi']:+.4f}]"
        print(f"{name:<26}{r['n']:>5}{r['mean_pct']:>9.4f}"
              f"{r['total_pct']:>9.2f}{r['sharpe']:>8.2f}{r['mdd_pct']:>9.1f}"
              f"{ci:>26}")
        if name != "ВСЯ история":
            signs.append(r["mean_pct"] > 0)
            if r["ci_lo"] > 0 or r["ci_hi"] < 0:
                excl += 1

    print("\n" + "=" * 104)
    print("ПОЛНАЯ СЕТКА на всей истории — плато или единичная ячейка?")
    print("=" * 104)
    print(f"{'lb':>4}{'hold':>6}{'n':>6}{'ср.%':>9}{'итог%':>10}{'Sharpe':>8}"
          f"{'проc.%':>9}{'95% CI периода':>26}")
    for lb in [1, 3, 9, 21]:
        for hold in [1, 3, 9, 21]:
            r = run(price, grid, lb, hold, 2 * TAKER_FEE)
            if r.get("n", 0) < 5:
                continue
            ci = f"[{r['ci_lo']:+.4f}; {r['ci_hi']:+.4f}]"
            mark = " <-- кандидат" if (lb, hold) == (3, 9) else ""
            print(f"{lb:>4}{hold:>6}{r['n']:>6}{r['mean_pct']:>9.4f}"
                  f"{r['total_pct']:>10.2f}{r['sharpe']:>8.2f}"
                  f"{r['mdd_pct']:>9.1f}{ci:>26}{mark}")

    print("\n" + "=" * 104)
    ok = len(signs) == len(chunks) and all(signs) and excl >= 2
    print(f"критерий (задан до прогона): знак + во всех {len(chunks)} годовых "
          f"подпериодах И CI не накрывает ноль минимум в 2")
    print(f"факт: положительных подпериодов {sum(signs)}/{len(signs)}, "
          f"со значимым CI {excl}")
    print(f"ВЕРДИКТ: {'кандидат ПРОШЁЛ' if ok else 'кандидат ЗАКРЫТ — шум'}")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    sys.exit(main())
