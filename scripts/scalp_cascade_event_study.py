"""Event study: разворот после каскада вынужденных продаж/покупок.

Зачем
─────
Вторая половина событийного направления (первая — новые листинги, см.
`scripts/scalp_listing_event_study.py`, закрыта отрицательно на 346 листингах).
Идея: принудительное закрытие плечевых позиций толкает цену за пределы
информационно обоснованного уровня, после чего следует возврат. Это единственный
механизм, который в принципе может дать ход, кратно превышающий издержку 0.110%.

Честная оговорка о приоре: семейство «liquidations + open interest» уже
проверено независимо (`retail-crypto-alpha`, Mykola-Quant 2026, 5 активов) с
отрицательным результатом при round-trip ~0.13%. Здесь проверяется на нашем
универсуме альтов, где каскады резче, чем на мажорах.

Прокси каскада
──────────────
Исторических данных по ликвидациям Bybit через REST не отдаёт (только
websocket-поток в реальном времени), поэтому каскад определяется по следу в
барах: 5-минутный бар с |доходностью| выше k медианных абсолютных доходностей за
предыдущие сутки. Это стандартный подход к детекции ценовых дислокаций, и он не
подгоняется: сетка k задана заранее и печатается целиком.

Гипотеза (зафиксирована до прогона): вход ПРОТИВ направления каскадного бара на
его закрытии, удержание 15м / 60м / 240м. Критерий приёмки: CI не накрывает ноль
И знак совпал в первой и второй половине выборки по времени.

API: https://bybit-exchange.github.io/docs/v5/market/kline
Ставки: https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

TAKER_FEE = 0.00055
BAR_MIN = 5
BAR_MS = BAR_MIN * 60 * 1000
DAY_BARS = 24 * 60 // BAR_MIN


def liquid_symbols(sess, min_turnover: float, limit: int) -> list[str]:
    rows = sess.get_tickers(category="linear")["result"]["list"]
    out = []
    for r in rows:
        sym = r.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            t = float(r.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if t >= min_turnover:
            out.append((t, sym))
    out.sort(reverse=True)
    return [s for _, s in out[:limit]]


def fetch_bars(sess, symbol: str, start_ms: int) -> list[tuple[int, float]]:
    out: dict[int, float] = {}
    end = int(time.time() * 1000)
    while True:
        try:
            rows = sess.get_kline(category="linear", symbol=symbol,
                                  interval=str(BAR_MIN), start=start_ms,
                                  end=end, limit=1000)["result"]["list"]
        except Exception:
            break
        if not rows:
            break
        oldest = end
        for r in rows:
            ts = int(r[0])
            out[ts] = float(r[4])
            oldest = min(oldest, ts)
        if len(rows) < 1000 or oldest <= start_ms:
            break
        end = oldest - 1
    return sorted(out.items())


def find_events(series: list[tuple[int, float]], k: float
                ) -> list[tuple[int, int, int]]:
    """(индекс, знак каскада, метка) для баров с аномальным ходом."""
    closes = [c for _, c in series]
    rets = [0.0] + [(closes[i] / closes[i - 1] - 1)
                    for i in range(1, len(closes))]
    absr = [abs(x) for x in rets]
    ev = []
    for i in range(DAY_BARS, len(series)):
        window = absr[i - DAY_BARS:i]
        med = statistics.median(window)
        if med <= 0:
            continue
        if absr[i] >= k * med:
            ev.append((i, 1 if rets[i] > 0 else -1, series[i][0]))
    return ev


def stats(vals: list[float]) -> dict:
    n = len(vals)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(vals)
    sd = statistics.stdev(vals) if n > 1 else 0.0
    se = sd / math.sqrt(n)
    return {"n": n, "mean": mean, "median": statistics.median(vals),
            "wr": 100 * sum(1 for v in vals if v > 0) / n,
            "ci_lo": mean - 1.96 * se, "ci_hi": mean + 1.96 * se}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--min-turnover", type=float, default=20_000_000)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    from pybit.unified_trading import HTTP
    sess = HTTP()
    syms = liquid_symbols(sess, args.min_turnover, args.symbols)
    start = int((time.time() - args.days * 86400) * 1000)
    print(f"универсум {len(syms)} символов, {args.days} дней, бар {BAR_MIN}м")

    series: dict[str, list[tuple[int, float]]] = {}
    for i, s in enumerate(syms, 1):
        b = fetch_bars(sess, s, start)
        if len(b) > DAY_BARS + 100:
            series[s] = b
        if args.verbose:
            print(f"    [{i}/{len(syms)}] {s}: баров={len(b)}")
    print(f"загружено символов: {len(series)}")

    horizons = [3, 12, 48]        # 15м / 60м / 240м
    ks = [5.0, 8.0, 12.0]
    slippages = [0.0, 0.0005, 0.0015]
    trials = 0

    print("\n" + "=" * 104)
    print("РАЗВОРОТ ПОСЛЕ КАСКАДА: вход ПРОТИВ каскадного бара на его закрытии")
    print("=" * 104)
    for k in ks:
        events = []
        for sym, ser in series.items():
            for idx, sign, ts in find_events(ser, k):
                events.append((sym, idx, sign, ts))
        if not events:
            print(f"\nk={k}: событий нет")
            continue
        moves = {}
        for sym, idx, sign, ts in events:
            ser = series[sym]
            for h in horizons:
                if idx + h >= len(ser):
                    continue
                p0, p1 = ser[idx][1], ser[idx + h][1]
                if not p0:
                    continue
                # вход против каскада: sign=+1 (рывок вверх) -> шорт
                moves.setdefault(h, []).append(
                    (-sign * (p1 / p0 - 1) * 100, ts))
        print(f"\n--- k={k} (порог аномальности), событий {len(events)} ---")
        print(f"{'горизонт':>10}{'слippage':>10}{'n':>7}{'средн.%':>10}"
              f"{'медиана%':>11}{'WR':>7}{'95% CI средн.':>24}  IS/OOS")
        for h in horizons:
            rows = moves.get(h) or []
            if len(rows) < 5:
                continue
            for slip in slippages:
                trials += 1
                cost = (2 * TAKER_FEE + 2 * slip) * 100
                vals = [v - cost for v, _ in rows]
                s = stats(vals)
                if s.get("n", 0) < 5:
                    continue
                order = sorted(range(len(rows)), key=lambda i: rows[i][1])
                half = len(order) // 2
                a = stats([vals[i] for i in order[:half]])
                b = stats([vals[i] for i in order[half:]])
                agree = ("ДА" if a.get("n", 0) >= 5 and b.get("n", 0) >= 5
                         and (a["mean"] > 0) == (b["mean"] > 0) else "нет")
                ci = f"[{s['ci_lo']:+.3f}; {s['ci_hi']:+.3f}]"
                lbl = f"{h * BAR_MIN}м"
                print(f"{lbl:>10}{slip * 100:>9.2f}%{s['n']:>7}"
                      f"{s['mean']:>10.3f}{s['median']:>11.3f}"
                      f"{s['wr']:>6.0f}%{ci:>24}  {agree}"
                      f" ({a.get('mean', 0):+.3f}/{b.get('mean', 0):+.3f})")

    print("\n" + "=" * 104)
    print(f"испытаний: {trials} — порог дефлировать на это число")
    print("приём ТОЛЬКО если CI не накрывает ноль И знак совпал в IS/OOS.")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    sys.exit(main())
