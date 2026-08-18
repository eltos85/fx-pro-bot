"""Проверка единственного класса, который литература оставляет живым на VIP 0.

Контекст
────────
VIP 0 на Bybit perpetual — это не «стратегия», а прайс-лист комиссии:
maker 0.0200% / taker 0.0550%
(https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate).
Круг рыночных сделок стоит 0.110%. На минутном горизонте этот порог уже
съел все семь семейств, которые мы проверили 18.08. Вопрос этого скрипта
другой: есть ли стратегия, у которой типичный ход >> 0.110%, чтобы тот же
тариф перестал быть связывающим.

Литература (не блоги ботов) сходится в одном месте:
- Moskowitz/Ooi/Pedersen 2012 — time-series momentum, горизонт месяцы;
- Hurst/Ooi/Pedersen 2017 — тренд положителен десятилетиями;
- Liu/Tsyvinski 2021 — в крипте TS-момент силён;
- Zarattini/Pagani/Barbon 2025 (Concretum) — ансамбль Donchian long-only
  на дневных барах, издержки 10–50 bps не убивают длинные окна;
- Olanipekun 2026 (crypto-trend-research) — однострочное правило на 4h
  бьёт buy-and-hold OOS; любой часовой/минутный ML проигрывает комиссии.

Параметры ЗАФИКСИРОВАНЫ каноном, сетка не оптимизируется
(no-data-fitting.mdc, strategy-guard.mdc):
  Turtle System 1: вход 20д high, выход 10д low (Dennis/Eckhardt);
  Turtle System 2: вход 55д high, выход 20д low;
  SMA 200 long/flat (Murphy 1999, классический фильтр режима);
  TSMOM 12×1: знак доходности за 12 месяцев, удержание 1 месяц
  (Moskowitz et al. 2012).

Правила исполнения
──────────────────
Сигнал на закрытии дня t, сделка на открытии дня t+1 (без look-ahead).
Только long или кэш — как у Concretum: шорт крипты на розничном счёте
ломается фандингом и асимметрией. Издержка — VIP 0 taker round-trip
0.110% на каждое открытие и каждое закрытие. Без плеча.

Критерий приёмки, заданный ДО прогона
─────────────────────────────────────
Правило принимается, только если на BTC:
  1) итоговая доходность неотрицательна;
  2) максимальная просадка строго меньше, чем у buy-and-hold;
  3) знак избыточной доходности против B&H положителен минимум в двух
     из трёх годовых окон.
Иначе кандидат закрывается. Печатается вся сетка, включая провалы.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

TAKER = 0.00055
RT = 2 * TAKER

UNIVERSE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def fetch_daily(sess, symbol: str, start_ms: int) -> list[tuple[int, float, float, float]]:
    """Дневные бары (open, high, low, close) с пагинацией."""
    out: dict[int, tuple[float, float, float]] = {}
    end = int(time.time() * 1000)
    while True:
        try:
            rows = sess.get_kline(
                category="linear", symbol=symbol, interval="D",
                start=start_ms, end=end, limit=1000,
            )["result"]["list"]
        except Exception:
            break
        if not rows:
            break
        oldest = end
        for r in rows:
            ts = int(r[0])
            out[ts] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            oldest = min(oldest, ts)
        if len(rows) < 1000 or oldest <= start_ms:
            break
        end = oldest - 1
    return [(ts, *out[ts]) for ts in sorted(out)]


def donchian_longflat(bars, entry_n: int, exit_n: int, fee: float) -> dict:
    """Turtle long/flat. Сигнал на close t, вход/выход на open t+1."""
    highs = [h for _, _, h, _, _ in bars]
    lows = [lo for _, _, _, lo, _ in bars]
    opens = [o for _, o, _, _, _ in bars]
    closes = [c for _, _, _, _, c in bars]
    n = len(bars)
    pos = 0
    eq, peak, mdd = 1.0, 1.0, 0.0
    daily = []
    trades = 0
    i = max(entry_n, exit_n) + 1
    while i + 1 < n:
        # каналы считаются по ЗАВЕРШЁННЫМ дням, без текущего
        eh = max(highs[i - entry_n:i])
        xl = min(lows[i - exit_n:i])
        want = pos
        if pos == 0 and closes[i] > eh:
            want = 1
        elif pos == 1 and closes[i] < xl:
            want = 0
        ret = 0.0
        if pos == 1:
            ret = opens[i + 1] / opens[i] - 1
        if want != pos:
            ret -= fee
            trades += 1
            pos = want
        eq *= 1 + ret
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        daily.append(ret)
        i += 1
    return _pack(daily, eq, mdd, trades, bars[max(entry_n, exit_n) + 1][0], bars[-2][0])


def sma_longflat(bars, window: int, fee: float) -> dict:
    closes = [c for _, _, _, _, c in bars]
    opens = [o for _, o, _, _, _ in bars]
    pos = 0
    eq, peak, mdd = 1.0, 1.0, 0.0
    daily = []
    trades = 0
    i = window
    while i + 1 < len(bars):
        sma = statistics.mean(closes[i - window:i])
        want = 1 if closes[i] > sma else 0
        ret = opens[i + 1] / opens[i] - 1 if pos == 1 else 0.0
        if want != pos:
            ret -= fee
            trades += 1
            pos = want
        eq *= 1 + ret
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        daily.append(ret)
        i += 1
    return _pack(daily, eq, mdd, trades, bars[window][0], bars[-2][0])


def tsmom_longflat(bars, lookback: int, hold: int, fee: float) -> dict:
    """Знак доходности за lookback дней → long/flat, ребаланс каждые hold дней."""
    closes = [c for _, _, _, _, c in bars]
    opens = [o for _, o, _, _, _ in bars]
    pos = 0
    eq, peak, mdd = 1.0, 1.0, 0.0
    daily = []
    trades = 0
    i = lookback
    next_rebal = i
    while i + 1 < len(bars):
        if i >= next_rebal:
            want = 1 if closes[i] > closes[i - lookback] else 0
            next_rebal = i + hold
        else:
            want = pos
        ret = opens[i + 1] / opens[i] - 1 if pos == 1 else 0.0
        if want != pos:
            ret -= fee
            trades += 1
            pos = want
        eq *= 1 + ret
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        daily.append(ret)
        i += 1
    return _pack(daily, eq, mdd, trades, bars[lookback][0], bars[-2][0])


def buy_hold(bars, start_ts: int) -> dict:
    opens = [(ts, o) for ts, o, _, _, _ in bars if ts >= start_ts]
    if len(opens) < 10:
        return {"n": 0}
    daily = []
    eq, peak, mdd = 1.0, 1.0, 0.0
    for i in range(len(opens) - 1):
        ret = opens[i + 1][1] / opens[i][1] - 1
        eq *= 1 + ret
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        daily.append(ret)
    return _pack(daily, eq, mdd, 1, opens[0][0], opens[-2][0])


def _pack(daily, eq, mdd, trades, t0, t1) -> dict:
    n = len(daily)
    if n < 20:
        return {"n": n}
    mean = statistics.mean(daily)
    sd = statistics.stdev(daily) if n > 1 else 0.0
    sharpe = mean / sd * math.sqrt(365) if sd else 0.0
    return {
        "n": n, "total_pct": (eq - 1) * 100, "mdd_pct": mdd * 100,
        "sharpe": sharpe, "trades": trades,
        "t0": t0, "t1": t1, "daily": daily,
    }


def yearly_excess(rule_daily, bh_daily, t0: int) -> list[tuple[str, float]]:
    """Избыточная доходность по календарным годам (компаунд)."""
    out = []
    buckets: dict[int, list[tuple[float, float]]] = {}
    for i, (a, b) in enumerate(zip(rule_daily, bh_daily)):
        year = time.gmtime(t0 / 1000 + i * 86400).tm_year
        buckets.setdefault(year, []).append((a, b))
    for year in sorted(buckets):
        ra = rb = 1.0
        for a, b in buckets[year]:
            ra *= 1 + a
            rb *= 1 + b
        out.append((str(year), (ra - rb) * 100))
    return out


def fmt(r: dict) -> str:
    if r.get("n", 0) < 20:
        return f"{r.get('n', 0):>5}  мало данных"
    return (f"{r['n']:>5}{r['total_pct']:>10.1f}{r['mdd_pct']:>9.1f}"
            f"{r['sharpe']:>8.2f}{r['trades']:>8}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5)
    args = ap.parse_args()
    from pybit.unified_trading import HTTP
    sess = HTTP()
    start_ms = int((time.time() - args.years * 365 * 86400) * 1000)

    print(f"тариф VIP 0 taker round-trip {RT * 100:.3f}% на каждое открытие/закрытие")
    print(f"история {args.years} лет, дневные бары Bybit linear, long/flat, без плеча")
    print("параметры канонические, сетка не оптимизируется\n")

    print(f"{'символ':<10}{'правило':<22}{'n':>5}{'итог%':>10}{'проc.%':>9}"
          f"{'Sharpe':>8}{'сделок':>8}  года +vs B&H")
    print("-" * 110)

    accept_btc = None
    for sym in UNIVERSE:
        bars = fetch_daily(sess, sym, start_ms)
        print(f"# {sym}: дневных баров={len(bars)}")
        if len(bars) < 400:
            print(f"{sym:<10}мало истории")
            continue
        rules = [
            ("Turtle 20/10", donchian_longflat(bars, 20, 10, RT)),
            ("Turtle 55/20", donchian_longflat(bars, 55, 20, RT)),
            ("SMA200 long/flat", sma_longflat(bars, 200, RT)),
            ("TSMOM 12м/1м", tsmom_longflat(bars, 365, 30, RT)),
        ]
        for name, r in rules:
            if r.get("n", 0) < 20:
                print(f"{sym:<10}{name:<22}{fmt(r)}")
                continue
            bh = buy_hold(bars, r["t0"])
            print(f"{sym:<10}{'B&H того же окна':<22}{fmt(bh)}")
            k = min(len(r["daily"]), len(bh.get("daily", [])))
            years = yearly_excess(r["daily"][-k:], bh["daily"][-k:],
                                  r["t0"]) if bh.get("n", 0) >= 20 else []
            pos_years = sum(1 for _, x in years if x > 0)
            tag = " ".join(f"{y}:{x:+.0f}" for y, x in years)
            print(f"{sym:<10}{name:<22}{fmt(r)}  +в {pos_years}/{len(years)}  {tag}")
            if sym == "BTCUSDT":
                mdd_ok = r["mdd_pct"] > bh.get("mdd_pct", -999)
                ret_ok = r["total_pct"] >= 0
                years_ok = pos_years >= 2
                if accept_btc is None:
                    accept_btc = []
                accept_btc.append((name, ret_ok and mdd_ok and years_ok,
                                   ret_ok, mdd_ok, years_ok, pos_years, len(years)))
        print()

    print("=" * 110)
    print("критерий (задан до прогона, только BTC): итог ≥ 0 И просадка лучше B&H "
          "И избыток плюсовой минимум в 2 из 3 годовых окон")
    if accept_btc:
        any_ok = False
        for name, ok, r_ok, d_ok, y_ok, py, ny in accept_btc:
            print(f"  {name:<22} итог={'да' if r_ok else 'нет'}  "
                  f"просадка={'да' if d_ok else 'нет'}  "
                  f"года={py}/{ny}  → {'ПРИНЯТ' if ok else 'закрыт'}")
            any_ok = any_ok or ok
        print(f"ВЕРДИКТ: {'есть кандидат на VIP 0' if any_ok else 'кандидата на VIP 0 нет'}")
    print("=" * 110)
    return 0


if __name__ == "__main__":
    sys.exit(main())
