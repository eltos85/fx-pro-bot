#!/usr/bin/env python3
"""Аномальный объём на альтах — как в постах CScalp / Bitcointalk, не канон.

Источник правила (зафиксировано ДО прогона, no-data-fitting.mdc):
  Bitcointalk topic=5577812 (Dzhango, 2026-03-19):
    «99% времени — шум. Игнор BTC/ETH/SOL.
     Суточный оборот $100k–$15M (в ответе модератору — до $15–30M).
     Сигнал: за 15с влили ~$30k И цена сдвинулась ≥0.2%.
     Через 5 минут сами проверяют, отработал памп или ложный пробой.»
  Smart-lab 963593 / 965140: руки, 1–2ч, плечо, мало импульсов.
  CScalp: стакан+лента — исторически не восстановить; меряем ценовой след.

Ограничение данных
──────────────────
Bybit REST kline минимум 1 минута
(https://bybit-exchange.github.io/docs/v5/market/kline).
15-секундных баров нет. Поэтому две заранее заданные интерпретации:

  V30  — turnover 1м ≥ $30k и |close/open| ≥ 0.2%
          буквальные числа треда, на 1м слабее «удара за 15с»
  V120 — turnover 1м ≥ $120k и |close/open| ≥ 0.2%
          та же интенсивность, что $30k / 15с

Направление (оба — до прогона):
  FOLLOW — по знаку бара («забрать движение», формулировка треда)
  FADE   — против бара (их же «ложный пробой» через 5м)

Вход на OPEN следующего 1м бара. Выход на CLOSE через 5 / 15 / 60 минут.
Перекрытия на символе: следующее событие не раньше конца удержания 5м.
Издержка: taker 0.055% × 2 = 0.110%
https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate

Приёмка (до прогона): n≥100, средний net > 0, знак совпал в IS/OOS,
CI среднего не накрывает ноль. Сетка печатается целиком.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

TAKER = 0.00055
RT = 2 * TAKER
MAJORS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
# 1м-прокси двух формулировок треда — не сетка для подбора.
VOL_RULES = (("V30", 30_000.0), ("V120", 120_000.0))
HOLD_MIN = (5, 15, 60)
SIDES = ("FOLLOW", "FADE")


def universe(sess, lo: float, hi: float, cap: int) -> list[tuple[str, float]]:
    rows = sess.get_tickers(category="linear")["result"]["list"]
    out = []
    for r in rows:
        sym = r.get("symbol") or ""
        if not sym.endswith("USDT") or sym in MAJORS:
            continue
        try:
            t = float(r.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if lo <= t <= hi:
            out.append((t, sym))
    out.sort(reverse=True)
    return [(s, t) for t, s in out[:cap]]


def fetch_1m(sess, symbol: str, start_ms: int) -> list[tuple]:
    """(ts, open, close, turnover). Новые Bybit отдаёт сверху."""
    acc: dict[int, tuple] = {}
    end = int(time.time() * 1000)
    while True:
        try:
            rows = sess.get_kline(
                category="linear", symbol=symbol, interval="1",
                start=start_ms, end=end, limit=1000,
            )["result"]["list"]
        except Exception:
            time.sleep(0.4)
            break
        if not rows:
            break
        oldest = end
        for r in rows:
            ts = int(r[0])
            acc[ts] = (ts, float(r[1]), float(r[4]), float(r[6]))
            oldest = min(oldest, ts)
        if len(rows) < 1000 or oldest <= start_ms:
            break
        end = oldest - 1
        time.sleep(0.12)
    return [acc[k] for k in sorted(acc)]


def events(bars: list[tuple], min_turn: float) -> list[int]:
    """Индексы закрытых баров-ударов."""
    out = []
    last_ok = -10**9
    for i, (_, o, c, turn) in enumerate(bars[:-1]):  # последний может формироваться
        if o <= 0 or turn < min_turn:
            continue
        if abs(c / o - 1) < 0.002:
            continue
        if i - last_ok < 5:  # не чаще раза в 5м на символе
            continue
        out.append(i)
        last_ok = i
    return out


def pack(pnls: list[float]) -> dict:
    n = len(pnls)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(pnls)
    sd = statistics.stdev(pnls) if n > 1 else 0.0
    se = sd / math.sqrt(n)
    return {
        "n": n,
        "mean": mean * 100,
        "med": statistics.median(pnls) * 100,
        "wr": 100 * sum(1 for x in pnls if x > 0) / n,
        "ci_lo": (mean - 1.96 * se) * 100,
        "ci_hi": (mean + 1.96 * se) * 100,
        "tot": (math.prod(1 + x for x in pnls) - 1) * 100,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--cap", type=int, default=50)
    ap.add_argument("--lo", type=float, default=100_000)
    ap.add_argument("--hi", type=float, default=15_000_000)
    args = ap.parse_args()

    from pybit.unified_trading import HTTP
    sess = HTTP()
    uni = universe(sess, args.lo, args.hi, args.cap)
    start = int((time.time() - args.days * 86400) * 1000)
    print(f"универсум {len(uni)} альтов (не BTC/ETH/SOL), "
          f"оборот24ч ${args.lo/1e6:.1f}–{args.hi/1e6:.0f}M, {args.days}д 1м")
    for s, t in uni[:8]:
        print(f"  {s:16} ${t/1e6:.2f}M")
    if len(uni) > 8:
        print(f"  … ещё {len(uni) - 8}")

    series: dict[str, list[tuple]] = {}
    for i, (sym, _) in enumerate(uni, 1):
        bars = fetch_1m(sess, sym, start)
        if len(bars) >= 400:
            series[sym] = bars
        print(f"  [{i}/{len(uni)}] {sym} баров={len(bars)}", flush=True)

    print("\nвход = open следующего 1м после удара; net = ход − 0.110%")
    print(f"{'правило':<8}{'сторона':<8}{'hold':>6}{'n':>7}"
          f"{'gross%':>9}{'net%':>9}{'med%':>8}{'WR':>6}"
          f"{'CI net':>22}  IS/OOS")

    trials = 0
    accepted = []
    for vname, vmin in VOL_RULES:
        evs: list[tuple[str, int, int, float]] = []
        for sym, bars in series.items():
            for idx in events(bars, vmin):
                _, o, c, _ = bars[idx]
                sign = 1.0 if c > o else -1.0
                evs.append((sym, idx, bars[idx][0], sign))
        evs.sort(key=lambda x: x[2])
        for side in SIDES:
            for hold in HOLD_MIN:
                trials += 1
                gross, nets, ts = [], [], []
                for sym, idx, t0, sign in evs:
                    bars = series[sym]
                    entry_i = idx + 1
                    exit_i = entry_i + hold - 1
                    if exit_i >= len(bars):
                        continue
                    px0 = bars[entry_i][1]
                    px1 = bars[exit_i][2]
                    if px0 <= 0:
                        continue
                    raw = (px1 / px0 - 1) * (sign if side == "FOLLOW" else -sign)
                    gross.append(raw)
                    nets.append(raw - RT)
                    ts.append(t0)
                g = pack(gross)
                s = pack(nets)
                if s.get("n", 0) < 5:
                    print(f"{vname:<8}{side:<8}{hold:>5}м{s.get('n', 0):>7}  мало")
                    continue
                order = sorted(range(len(nets)), key=lambda i: ts[i])
                mid = len(order) // 2
                a = pack([nets[i] for i in order[:mid]])
                b = pack([nets[i] for i in order[mid:]])
                agree = (a.get("n", 0) >= 30 and b.get("n", 0) >= 30
                         and (a["mean"] > 0) == (b["mean"] > 0))
                ok = (s["n"] >= 100 and s["mean"] > 0
                      and s["ci_lo"] > 0 and agree)
                if ok:
                    accepted.append((vname, side, hold, s["mean"]))
                ci = f"[{s['ci_lo']:+.3f}; {s['ci_hi']:+.3f}]"
                flag = " ПРИНЯТ" if ok else ""
                print(f"{vname:<8}{side:<8}{hold:>5}м{s['n']:>7}"
                      f"{g['mean']:>9.3f}{s['mean']:>9.3f}{s['med']:>8.3f}"
                      f"{s['wr']:>5.0f}%{ci:>22}  "
                      f"{'ДА' if agree else 'нет'}"
                      f" ({a.get('mean', 0):+.3f}/{b.get('mean', 0):+.3f})"
                      f"{flag}")

    print(f"\nиспытаний: {trials}. принятых ячеек: {len(accepted)}")
    if accepted:
        print("принятые:", accepted)
    else:
        print("ни одна ячейка не прошла заранее заданный гейт.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
