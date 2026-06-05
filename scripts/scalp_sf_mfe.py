"""sweep_fade MFE-анализ (канон B: цель из Maximum Favorable Excursion).
Для каждой заполненной сделки: как далеко цена ушла В ПЛЮС (в R = |entry-sl|)
ПРЕЖДЕ чем её убил бы базовый стоп (-1R), горизонт HORIZON мин. Это «доступный
ход» на сигнал — основа для постановки TP по данным, а не по конвенции 3.5R.
Консервативно: если в баре задело и SL, и новый максимум — берём ход ДО этого
бара (не считаем фаворит в баре смерти), чтобы не завысить цель."""
import time, urllib.request, json
from statistics import median

HORIZON_MIN = 30
CAT = "linear"
LEVELS = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
PCTS = [25, 50, 70, 75, 80, 90]


def klines(sym, s, e):
    url = (f"https://api.bybit.com/v5/market/kline?category={CAT}&symbol={sym}"
           f"&interval=1&start={int(s*1000)}&end={int(e*1000)}&limit=1000")
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                d = json.load(r)
            out = [(int(x[0])/1000.0, float(x[2]), float(x[3]))
                   for x in d.get("result", {}).get("list", []) or []]
            out.sort()
            return out
        except Exception:
            time.sleep(0.5)
    return []


def mfe_r(side, entry, sl, bars):
    R = abs(entry - sl)
    if R <= 0:
        return None
    best = 0.0
    for (ts, hi, lo) in bars:
        dead = (lo <= sl) if side == "long" else (hi >= sl)
        if dead:
            break  # не считаем фаворит в баре смерти (консервативно)
        fav = (hi - entry) if side == "long" else (entry - lo)
        if fav > best:
            best = fav
    return best / R


def pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[i]


def report(name, vals):
    n = len(vals)
    if not n:
        print(f"\n[{name}] нет данных"); return
    print(f"\n=== {name} (n={n}) ===")
    print("  перцентили MFE (R): " + "  ".join(
        f"p{p}={pct(vals,p):.2f}" for p in PCTS))
    print(f"  медиана={median(vals):.2f}R  среднее={sum(vals)/n:.2f}R")
    print("  доля, достигших уровня:")
    for L in LEVELS:
        share = sum(1 for v in vals if v >= L) / n * 100
        print(f"    ≥{L:.1f}R: {share:4.0f}%")
    med = median(vals)
    print(f"  → канон T1 (60-70% медианы): {0.6*med:.2f}–{0.7*med:.2f}R")


def main():
    trades = []
    for ln in open("/tmp/sf_all.txt"):
        ln = ln.strip()
        if ln.count("|") < 7:
            continue
        p = ln.split("|")
        try:
            trades.append({"ts": float(p[1]), "sym": p[2], "side": p[3],
                           "entry": float(p[4]), "sl": float(p[5]),
                           "reason": p[6], "pnl": float(p[7])})
        except ValueError:
            continue
    print(f"загружено {len(trades)} сделок, тяну клины (горизонт {HORIZON_MIN}м)...")
    allv, longv, shortv, winv = [], [], [], []
    miss = 0
    for i, t in enumerate(trades):
        bars = klines(t["sym"], t["ts"], t["ts"] + HORIZON_MIN * 60)
        if not bars:
            miss += 1; continue
        m = mfe_r(t["side"], t["entry"], t["sl"], bars)
        if m is None:
            continue
        allv.append(m)
        (longv if t["side"] == "long" else shortv).append(m)
        if t["pnl"] > 0:
            winv.append(m)
        if (i + 1) % 150 == 0:
            print(f"  ...{i+1}/{len(trades)}")
    print(f"\nбез клинов: {miss}")
    report("ВСЕ заполненные", allv)
    report("LONG", longv)
    report("SHORT", shortv)
    report("только winners (pnl>0)", winv)
    print("\nТекущая цель: 3.5R (канон A, фикс R:R). Сравни с долей ≥3.5R выше.")


if __name__ == "__main__":
    main()
