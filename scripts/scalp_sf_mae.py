"""sweep_fade MAE-анализ (канон Sweeney 1988, выбор ширины SL без подгонки).
Для КАЖДОЙ заполненной сделки идём по 1m-барам от входа (горизонт 30мин):
сделка = «eventual winner», если ход В ПЛЮС достиг target (+TGT×base) ДО конца
горизонта. Для винеров фиксируем MAE = макс. ход ПРОТИВ входа (в base_risk),
случившийся ДО достижения цели. Канон: стоп ставим за 85–90-м перцентилем MAE
винеров — тогда он не режет прибыльные сделки, но и не шире нужного.
base_risk = |entry−sl| (исторически mult=1.0). Внутри бара порядок hi/lo
неизвестен → консервативно считаем, что adverse случился ДО target (биас в
сторону чуть более широкого стопа)."""
import time, urllib.request, json
from statistics import median

HORIZON_MIN = 30
TGT = 1.5          # цель = уровень flow_exit (реальная точка фиксации)
CAT = "linear"
STOPS = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]
PCTS = [50, 70, 80, 85, 90, 95]


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


def mae_to_target(side, entry, sl, bars):
    """Возвращает MAE (в base_risk) до достижения цели, или None если цель не
    достигнута за горизонт (не winner)."""
    R = abs(entry - sl)
    if R <= 0:
        return None
    tgt = TGT * R
    worst_adv = 0.0
    for (ts, hi, lo) in bars:
        adv = (entry - lo) if side == "long" else (hi - entry)
        if adv > worst_adv:
            worst_adv = adv
        fav = (hi - entry) if side == "long" else (entry - lo)
        if fav >= tgt:               # цель достигнута → это winner
            return worst_adv / R     # MAE включает adverse текущего бара (консерв.)
    return None                      # цель не достигнута за горизонт


def pct(xs, p):
    s = sorted(xs)
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[i]


def main():
    trades = []
    for ln in open("/tmp/sf_all.txt"):
        ln = ln.strip()
        if ln.count("|") < 7:
            continue
        p = ln.split("|")
        try:
            trades.append({"ts": float(p[1]), "sym": p[2], "side": p[3],
                           "entry": float(p[4]), "sl": float(p[5])})
        except ValueError:
            continue
    print(f"загружено {len(trades)}; цель winner = +{TGT}R, горизонт {HORIZON_MIN}м")
    mae, miss = [], 0
    for i, t in enumerate(trades):
        bars = klines(t["sym"], t["ts"], t["ts"] + HORIZON_MIN * 60)
        if not bars:
            miss += 1; continue
        m = mae_to_target(t["side"], t["entry"], t["sl"], bars)
        if m is not None:
            mae.append(m)
        if (i + 1) % 150 == 0:
            print(f"  ...{i+1}/{len(trades)}")
    n = len(mae)
    print(f"\nбез клинов: {miss}")
    print(f"eventual-winners (дошли до +{TGT}R): n={n}")
    if not n:
        return
    print("\nперцентили MAE винеров (в base_risk R):")
    for p in PCTS:
        print(f"  p{p}: {pct(mae,p):.2f}R")
    print(f"  медиана={median(mae):.2f}R  среднее={sum(mae)/n:.2f}R")
    print("\nдоля винеров, которых СОХРАНИТ стоп шириной X (MAE < X):")
    for X in STOPS:
        keep = sum(1 for m in mae if m < X) / n * 100
        print(f"  стоп ×{X:.2f}: сохраняет {keep:4.0f}% винеров")
    print("\nКанон Sweeney: брать ширину ≈ 85–90-й перцентиль MAE винеров "
          "(сохраняет ~85–90%, не режет эдж, но и не шире нужного).")


if __name__ == "__main__":
    main()
