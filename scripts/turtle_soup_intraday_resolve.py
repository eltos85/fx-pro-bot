#!/usr/bin/env python3
"""Turtle Soup на дневках, порядок хода внутри дня разрешён 15-минутками.

Дневной бэктест (`turtle_soup_daily_backtest.py`) вынужденно делал два
допущения, и ОБА в пользу стратегии:
  1. если уровень задет внутри дня, считалось, что возврат случился ПОСЛЕ
     прокола (а мог и до — тогда стоп-ордер не сработал бы);
  2. стоп проверялся только со СЛЕДУЮЩЕГО дня, то есть вылет в день входа
     не учитывался вовсе.

Здесь оба сняты: для каждого сетапа берутся 15-минутные бары этого дня и
сделка проигрывается по порядку.

Канон (Connors/Raschke «Street Smarts» 1996) воспроизводится буквально:
  - цена пробивает 20-дневный экстремум → ставим стоп-ордер НА уровне;
  - ордер срабатывает при первом возврате цены к уровню;
  - защитный стоп — за экстремумом, достигнутым К МОМЕНТУ входа;
  - ордер действителен только сегодня.

Bybit V5 kline принимает start/end в мс:
https://bybit-exchange.github.io/docs/v5/market/kline
Только чтение, публичные market-эндпойнты.
"""

from __future__ import annotations

import argparse
import time
from math import sqrt

LOOKBACK = 20
MIN_GAP_SESSIONS = 4
FEE_ROUND_TRIP = 0.0011
MAX_HOLD_DAYS = 10
TARGETS = (1.0, 2.0, 3.0)
DAY_MS = 86_400_000


def mean_ci(v: list[float]) -> tuple[float, float, float]:
    n = len(v)
    if not n:
        return float("nan"), float("nan"), float("nan")
    mu = sum(v) / n
    if n < 2:
        return mu, float("nan"), float("nan")
    se = sqrt(sum((x - mu) ** 2 for x in v) / (n - 1) / n)
    return mu, mu - 1.96 * se, mu + 1.96 * se


def daily_setups(bars: list[dict]) -> list[dict]:
    out = []
    for t in range(LOOKBACK, len(bars) - 1):
        window = bars[t - LOOKBACK:t]
        today = bars[t]
        for side in ("long", "short"):
            if side == "long":
                level = min(b["l"] for b in window)
                idx = max(i for i, b in enumerate(window) if b["l"] == level)
                broke = today["l"] < level
            else:
                level = max(b["h"] for b in window)
                idx = max(i for i, b in enumerate(window) if b["h"] == level)
                broke = today["h"] > level
            if not broke or LOOKBACK - idx < MIN_GAP_SESSIONS:
                continue
            out.append({"i": t, "side": side, "level": level,
                        "ts": today["ts"]})
    return out


def fetch(session, symbol: str, interval: str, **kw) -> list[dict]:
    resp = session.get_kline(category="linear", symbol=symbol,
                             interval=interval, **kw)
    rows = resp.get("result", {}).get("list", []) or []
    bars = []
    for r in rows:
        try:
            bars.append({"ts": float(r[0]) / 1000.0, "o": float(r[1]),
                         "h": float(r[2]), "l": float(r[3]), "c": float(r[4])})
        except (IndexError, TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b["ts"])
    return bars


def resolve_entry(m15: list[dict], side: str, level: float) -> dict | None:
    """Первый возврат к уровню ПОСЛЕ прокола. None — вход не состоялся.

    Цена исполнения. Триггерный бар целиком лежит по нужную сторону уровня
    (его low ≥ level для лонга), то есть на уровне внутри этого бара сделок
    не было — цена прошла его между барами. Стоп-ордер исполнился бы по
    ПЕРВОЙ доступной цене, а это открытие триггерного бара. Брать сам level
    значило бы подарить себе проскальзывание в свою пользу, а при стопе
    порядка 0.7% цены даже десятая доля процента — это седьмая часть риска.
    """
    broke = False
    extreme = None
    for k, b in enumerate(m15):
        if side == "long":
            if b["l"] < level:
                broke = True
                extreme = b["l"] if extreme is None else min(extreme, b["l"])
            # возврат считаем со СЛЕДУЮЩЕГО бара после прокола: внутри одного
            # 15-минутного бара порядок снова неизвестен
            elif broke and b["h"] >= level:
                return {"k": k, "entry": max(level, b["o"]),
                        "stop": extreme, "level": level}
        else:
            if b["h"] > level:
                broke = True
                extreme = b["h"] if extreme is None else max(extreme, b["h"])
            elif broke and b["l"] <= level:
                return {"k": k, "entry": min(level, b["o"]),
                        "stop": extreme, "level": level}
    return None


def play(m15: list[dict], daily: list[dict], s: dict, e: dict) -> dict | None:
    sign = 1.0 if s["side"] == "long" else -1.0
    entry, stop = e["entry"], e["stop"]
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    mfe = 0.0
    hit = {t: None for t in TARGETS}
    stopped = False
    step = 0
    slip_r = abs(entry - e["level"]) / risk  # во что обошёлся проскок уровня

    def scan(b) -> bool:
        nonlocal mfe, stopped
        fav = (b["h"] - entry) if sign > 0 else (entry - b["l"])
        adv = (entry - b["l"]) if sign > 0 else (b["h"] - entry)
        mfe = max(mfe, fav / risk)
        if adv >= risk:
            stopped = True
            return True
        for t in TARGETS:
            if hit[t] is None and fav / risk >= t:
                hit[t] = step
        return False

    for b in m15[e["k"] + 1:]:  # остаток дня входа
        if scan(b):
            break
    if not stopped:
        for k in range(1, MAX_HOLD_DAYS + 1):
            j = s["i"] + k
            if j >= len(daily):
                break
            step = k
            if scan(daily[j]):
                break
    j = min(s["i"] + MAX_HOLD_DAYS, len(daily) - 1)
    res = {}
    for t in TARGETS:
        if hit[t] is not None:
            res[t] = t
        elif stopped:
            res[t] = -1.0
        else:
            res[t] = sign * (daily[j]["c"] - entry) / risk
    # «Стоп сработал» = стоп задет и НИ ОДНА цель до него не взята. Иначе
    # сделка уже была бы закрыта в плюс, а стоп ловил бы воздух.
    real_stop = stopped and all(hit[t] is None for t in TARGETS)
    return {"mfe": mfe, "stopped": real_stop, "slip_r": slip_r,
            "risk_pct": risk / entry * 100,
            **{f"t{t}": res[t] for t in TARGETS}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--days", type=int, default=1000)
    args = ap.parse_args()

    from scalp_bot.config.settings import load_settings
    from scalp_bot.trading.client import ScalpBybitClient
    cfg = load_settings()
    session = ScalpBybitClient(cfg.bybit_api_key, cfg.bybit_api_secret)._session

    results, no_entry, calls = [], 0, 0
    for sym in [x.strip() for x in args.symbols.split(",") if x.strip()]:
        daily = fetch(session, sym, "D", limit=args.days)
        if len(daily) < LOOKBACK + 5:
            continue
        setups = daily_setups(daily)
        before = len(results)
        for s in setups:
            start = int(s["ts"] * 1000)
            try:
                m15 = fetch(session, sym, "15", start=start,
                            end=start + DAY_MS, limit=200)
                calls += 1
            except Exception:
                continue
            if len(m15) < 10:
                continue
            e = resolve_entry(m15, s["side"], s["level"])
            if e is None:
                no_entry += 1
                continue
            r = play(m15, daily, s, e)
            if r is not None:
                results.append(r)
            if calls % 200 == 0:
                time.sleep(0.5)
        print(f"  {sym}: сетапов {len(setups)}, входов "
              f"{len(results) - before}", flush=True)

    print(f"\nзапросов 15m: {calls}")
    print(f"сетапов без входа (возврата к уровню в тот же день не было): {no_entry}")
    if not results:
        print("входов нет")
        return
    n = len(results)
    risks = sorted(r["risk_pct"] for r in results)
    med = risks[len(risks) // 2]
    fee_r = FEE_ROUND_TRIP * 100 / med
    stopped_same = sum(1 for r in results if r["stopped"])
    slips = sorted(r["slip_r"] for r in results)
    mfes = sorted(r["mfe"] for r in results)
    print(f"\n=== Канон с разрешённым внутридневным порядком ===")
    print(f"сделок: {n}")
    print(f"медиана риска: {med:.2f}% цены → комиссия {fee_r:.3f}R")
    print(f"закрыто стопом без взятой цели: {stopped_same / n * 100:.0f}%")
    print(f"проскок уровня при входе: медиана {slips[len(slips)//2]:.3f}R, "
          f"75-й перцентиль {slips[int(len(slips)*0.75)]:.3f}R")
    print(f"ход в плюс: медиана {mfes[len(mfes) // 2]:.2f}R, "
          f"доля дошедших до 1R {sum(1 for m in mfes if m >= 1) / n * 100:.0f}%")
    hdr = f"{'цель':<8}{'n':>6}{'валR':>9}{'ком.R':>8}{'чистR':>9}{'95% CI чист':>22}"
    print(hdr)
    print("-" * len(hdr))
    for t in TARGETS:
        vals = [r[f"t{t}"] for r in results]
        mu, lo, hi = mean_ci(vals)
        print(f"{t:<8.1f}{n:>6}{mu:>9.3f}{fee_r:>8.3f}{mu - fee_r:>9.3f}"
              f"{f'[{lo - fee_r:+.3f}; {hi - fee_r:+.3f}]':>22}")


if __name__ == "__main__":
    main()
