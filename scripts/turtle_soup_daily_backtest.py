#!/usr/bin/env python3
"""Канонный Turtle Soup на ДНЕВНЫХ барах — проверка на истории (read-only).

Зачем. `sweep_fade_canon` — это Turtle Soup, пересаженный на 3-минутки, и
аудит `f6332f9` показал, что при таком масштабе канон ломается экономически:
прокол однодневного уровня 11.5 bps при round-trip 11 bps даёт комиссию 0.96R.
Здесь проверяется исходный сетап на его РОДНОМ таймфрейме, где прокол
двадцатидневного экстремума на порядок глубже, а комиссия та же.

Почему бэктест, а не теневой сбор. Канонный сетап читает только OHLC дневных
баров — ни стакана, ни CVD, поэтому история полностью восстановима из
публичных клинов. Форвардный сбор при частоте «несколько сетапов на символ в
месяц» добирал бы сотню наблюдений больше года.

Правила (Connors/Raschke «Street Smarts» 1996, сверено по трём изложениям):
  1. сегодня новый 20-дневный экстремум;
  2. предыдущий 20-дневный экстремум — не ближе 4 сессий назад;
  3. вход стоп-ордером на уровне предыдущего экстремума, действителен только
     сегодня;
  4. стоп — за сегодняшним экстремумом;
  5. сопровождение трейлингом, удержание часы–дни.

Про честность замера. Внутри дневного бара порядок хода цены неизвестен,
поэтому вход считается двумя способами:

- «канонный»: вход по стоп-ордеру НА УРОВНЕ, если уровень задет внутри дня.
  Ровно правило книги, но с двусмысленностью: неизвестно, был ли возврат
  к уровню ПОСЛЕ прокола или до него. Оценка сверху.
- «по закрытию»: условие — день ЗАКРЫЛСЯ за уровнем, и вход тогда берётся
  ПО ЦЕНЕ ЗАКРЫТИЯ. Двусмысленности нет.

Ставить условие по закрытию, а вход считать по внутридневному уровню — это
заглядывание вперёд: на момент входа закрытие ещё не известно. Такой замер
завышает результат и здесь не используется.

Если стоп и цель задеты в один день, считаем стоп первым.

Выходы не подбираются: печатается распределение хода в плюс (MFE) и заранее
названные фиксированные цели. Выбирать из них лучшую post-hoc нельзя —
это подгонка (`no-data-fitting.mdc`).
"""

from __future__ import annotations

import argparse
from math import sqrt

LOOKBACK = 20          # канон: 20-дневный экстремум
MIN_GAP_SESSIONS = 4   # канон: предыдущий экстремум ≥4 сессий назад
FEE_ROUND_TRIP = 0.0011  # taker вход + taker выход, Bybit
MAX_HOLD_DAYS = 10     # канон «часы–дни»; 10 сессий с запасом
TARGETS = (1.0, 2.0, 3.0)  # названы заранее, не подбираются


def mean_ci(v: list[float]) -> tuple[float, float, float]:
    n = len(v)
    if not n:
        return float("nan"), float("nan"), float("nan")
    mu = sum(v) / n
    if n < 2:
        return mu, float("nan"), float("nan")
    se = sqrt(sum((x - mu) ** 2 for x in v) / (n - 1) / n)
    return mu, mu - 1.96 * se, mu + 1.96 * se


def find_setups(bars: list[dict], mode: str) -> list[dict]:
    """bars: старые→новые, каждый {ts, o, h, l, c}.

    mode='canon' — вход по уровню, условие «уровень задет внутри дня».
    mode='close' — вход по закрытию, условие «день закрылся за уровнем».
    Вход и условие всегда берутся из ОДНОЙ информации: иначе look-ahead.
    """
    out = []
    for t in range(LOOKBACK, len(bars) - 1):
        window = bars[t - LOOKBACK:t]
        today = bars[t]
        for side in ("long", "short"):
            if side == "long":
                level = min(b["l"] for b in window)
                # самый СВЕЖИЙ бар, поставивший экстремум (консервативно)
                idx = max(i for i, b in enumerate(window) if b["l"] == level)
                broke = today["l"] < level
                triggered = (today["c"] > level if mode == "close"
                             else today["h"] >= level)
            else:
                level = max(b["h"] for b in window)
                idx = max(i for i, b in enumerate(window) if b["h"] == level)
                broke = today["h"] > level
                triggered = (today["c"] < level if mode == "close"
                             else today["l"] <= level)
            if not broke or not triggered:
                continue
            gap = LOOKBACK - idx  # сессий между постановкой уровня и сегодня
            if gap < MIN_GAP_SESSIONS:
                continue
            entry = today["c"] if mode == "close" else level
            stop = today["l"] if side == "long" else today["h"]
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            overshoot = (abs(level - today["l"]) if side == "long"
                         else abs(today["h"] - level))
            out.append({"i": t, "side": side, "entry": entry, "stop": stop,
                        "risk": risk, "gap": gap,
                        "overshoot_pct": overshoot / level * 100,
                        "risk_pct": risk / entry * 100})
    return out


def simulate(bars: list[dict], s: dict) -> dict:
    """Проход вперёд от следующего бара. Стоп приоритетнее цели в один день."""
    sign = 1.0 if s["side"] == "long" else -1.0
    mfe = mae = 0.0
    hit = {t: None for t in TARGETS}
    stopped_day = None
    for k in range(1, MAX_HOLD_DAYS + 1):
        j = s["i"] + k
        if j >= len(bars):
            break
        b = bars[j]
        fav = (b["h"] - s["entry"]) if sign > 0 else (s["entry"] - b["l"])
        adv = (s["entry"] - b["l"]) if sign > 0 else (b["h"] - s["entry"])
        mfe = max(mfe, fav / s["risk"])
        mae = max(mae, adv / s["risk"])
        if adv >= s["risk"]:          # стоп задет
            stopped_day = k
            break
        for t in TARGETS:
            if hit[t] is None and fav / s["risk"] >= t:
                hit[t] = k
    else:
        j = min(s["i"] + MAX_HOLD_DAYS, len(bars) - 1)
    # результат по каждой заранее названной цели
    res = {}
    for t in TARGETS:
        if hit[t] is not None and (stopped_day is None or hit[t] <= stopped_day):
            res[t] = t
        elif stopped_day is not None:
            res[t] = -1.0
        else:  # горизонт досмотрен, ни один барьер не задет — переоценка
            j = min(s["i"] + MAX_HOLD_DAYS, len(bars) - 1)
            res[t] = sign * (bars[j]["c"] - s["entry"]) / s["risk"]
    return {"mfe": mfe, "mae": mae, "stopped": stopped_day is not None, **{f"t{t}": res[t] for t in TARGETS}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT")
    ap.add_argument("--days", type=int, default=1000)
    args = ap.parse_args()

    from scalp_bot.config.settings import load_settings
    from scalp_bot.trading.client import ScalpBybitClient
    cfg = load_settings()
    client = ScalpBybitClient(cfg.bybit_api_key, cfg.bybit_api_secret)

    all_setups: list[tuple[str, dict, dict]] = []
    coverage = []
    for sym in args.symbols.split(","):
        sym = sym.strip()
        if not sym:
            continue
        try:
            raw = client.get_kline(sym, "D", limit=args.days)
        except Exception as exc:
            print(f"{sym}: не удалось получить историю — {exc}")
            continue
        bars = []
        for row in raw or []:
            try:
                bars.append({"ts": float(row[0]) / 1000.0, "o": float(row[1]),
                             "h": float(row[2]), "l": float(row[3]),
                             "c": float(row[4])})
            except (IndexError, TypeError, ValueError):
                continue
        bars.sort(key=lambda b: b["ts"])
        if len(bars) < LOOKBACK + 5:
            print(f"{sym}: истории мало ({len(bars)} баров)")
            continue
        coverage.append((sym, len(bars)))
        for mode in ("canon", "close"):
            for s in find_setups(bars, mode):
                s["mode"] = mode
                all_setups.append((sym, s, simulate(bars, s)))

    if not all_setups:
        print("сетапов не найдено")
        return

    print("=== Покрытие истории ===")
    for sym, n in coverage:
        print(f"  {sym:<10}{n} дневных баров ≈ {n / 365:.1f} года")

    for mode in ("canon", "close"):
        rows = [(sym, s, r) for sym, s, r in all_setups if s["mode"] == mode]
        title = ("КАНОННЫЙ вход по уровню (порядок хода внутри дня неизвестен)"
                 if mode == "canon"
                 else "ВХОД ПО ЗАКРЫТИЮ дня, закрывшегося за уровнем")
        print(f"\n=== {title} ===")
        if not rows:
            print("сетапов нет")
            continue
        risks = sorted(s["risk_pct"] for _, s, _ in rows)
        over = sorted(s["overshoot_pct"] for _, s, _ in rows)
        med_risk = risks[len(risks) // 2]
        fee_r = FEE_ROUND_TRIP * 100 / med_risk
        print(f"сетапов: {len(rows)}   "
              f"частота: {len(rows) / max(1, sum(n for _, n in coverage)) * 365:.1f} в год на символ")
        print(f"медиана прокола за уровень: {over[len(over) // 2]:.2f}% цены")
        print(f"медиана риска (стоп):       {med_risk:.2f}% цены")
        print(f"комиссия в R при этом стопе: {fee_r:.3f}R "
              f"(у 3-минутной версии было 0.36R, канонная геометрия там давала 0.96R)")
        mfes = sorted(r["mfe"] for _, _, r in rows)
        print(f"ход в плюс (MFE): медиана {mfes[len(mfes) // 2]:.2f}R, "
              f"75-й перцентиль {mfes[int(len(mfes) * 0.75)]:.2f}R, "
              f"доля дошедших до 1R {sum(1 for m in mfes if m >= 1) / len(mfes) * 100:.0f}%")
        hdr = f"{'цель':<8}{'n':>6}{'валR':>9}{'ком.R':>8}{'чистR':>9}{'95% CI чист':>22}"
        print(hdr)
        print("-" * len(hdr))
        for t in TARGETS:
            vals = [r[f"t{t}"] for _, _, r in rows]
            mu, lo, hi = mean_ci(vals)
            print(f"{t:<8.1f}{len(vals):>6}{mu:>9.3f}{fee_r:>8.3f}"
                  f"{mu - fee_r:>9.3f}"
                  f"{f'[{lo - fee_r:+.3f}; {hi - fee_r:+.3f}]':>22}")

    print("\nЦели названы заранее и печатаются ВСЕ: выбирать лучшую post-hoc")
    print("нельзя, это подгонка. Вход и условие входа в каждом варианте взяты")
    print("из одной информации — заглядывания вперёд нет.")


if __name__ == "__main__":
    main()
