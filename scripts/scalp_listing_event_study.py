"""Event study: первые часы новых бессрочных на Bybit (листинг как событие).

Зачем это направление
─────────────────────
Все прочие семейства закрыты замерами издержек (BUILDLOG_SCALP.md, 2026-08-18):
край на нашем горизонте меньше транзакционной стоимости. Событие листинга —
единственный оставшийся режим, где ход измеряется десятками процентов, то есть
round-trip 0.11% перестаёт быть связывающим ограничением. Каскады ликвидаций в
этот скрипт не входят намеренно: семейство «liquidations + open interest» уже
проверено независимо (`retail-crypto-alpha`, 5 активов) с отрицательным
результатом, а листинги в том переборе не участвовали.

Механизм, который проверяется
─────────────────────────────
У свежего контракта нет истории, поэтому цена открывается в условиях предельной
информационной асимметрии и розничного FOMO. Каноничная аналогия — IPO
underpricing и последующий long-run underperformance (Ritter 1991; Ritter/Welch
2002): первичный всплеск, затем распад. Отдельный попутный ветер для фейда: при
FOMO-лонгах ставка фандинга положительна, а значит ШОРТ её ПОЛУЧАЕТ. У новых
листингов интервал фандинга часто 1ч (а не 8ч), поэтому поток может быть
значимым — он считается здесь явно, а не игнорируется.

Гипотезы зафиксированы ДО прогона (no-data-fitting.mdc):
  H1 «моментум»: лонг на +5м, выход на +1ч
  H2 «распад»:   шорт на +1ч, выход на +24ч
  H3 «распад-длинный»: шорт на +4ч, выход на +48ч
Печатается вся сетка окон, включая убыточные. Выборка делится по дате листинга
на две половины (IS/OOS), критерий приёмки: знак совпал в обеих половинах И CI
не накрывает ноль. Число испытаний печатается для дефляции
(Bailey/Lopez de Prado).

Отдельно считаются две популяции, потому что это разные механизмы:
  * крипто-токены — первичное открытие цены, FOMO;
  * токенизированные акции/ETF — есть внешняя референсная цена, всплеска
    открытия быть не должно. Служат контрольной группой.

Издержки: taker round-trip 0.110%
(https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate) ПЛЮС слippage,
который у свежего листинга и есть главный расход. Результат печатается при
нескольких уровнях слippage, чтобы видеть чувствительность вывода к допущению.

API: https://bybit-exchange.github.io/docs/v5/market/instrument (launchTime),
     https://bybit-exchange.github.io/docs/v5/market/kline,
     https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
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

# Токенизированные акции/ETF: у них есть внешняя референсная цена, поэтому это
# контрольная группа, а не тестовая. Признак — baseCoin из тикеров акций и
# характерные суффиксы; отделяем эвристикой по baseCoin, сверяя с displayName.
EQUITY_HINTS = ("KODEX", "ETF", "NASDAQ", "SP500", "XAU", "XAG", "OIL", "GOLD")


def load_listings(sess, max_age_days: float, min_age_hours: float):
    cur, items = "", []
    while True:
        r = sess.get_instruments_info(category="linear", limit=1000,
                                      cursor=cur)["result"]
        items += r["list"]
        cur = r.get("nextPageCursor") or ""
        if not cur:
            break
    now = time.time() * 1000
    out = []
    for it in items:
        if it.get("quoteCoin") != "USDT" or it.get("status") != "Trading":
            continue
        if it.get("contractType") != "LinearPerpetual":
            continue
        lt = it.get("launchTime")
        if not lt:
            continue
        lt = int(lt)
        age_h = (now - lt) / 3600000
        if age_h < min_age_hours or age_h > max_age_days * 24:
            continue
        out.append({"symbol": it["symbol"], "launch": lt,
                    "base": it.get("baseCoin", ""),
                    "name": it.get("displayName", ""),
                    "fund_int": int(it.get("fundingInterval") or 480)})
    out.sort(key=lambda x: x["launch"])
    return out


def is_equity(row: dict) -> bool:
    """Токенизированная акция/ETF — контрольная группа."""
    blob = f"{row['base']} {row['name']}".upper()
    if any(h in blob for h in EQUITY_HINTS):
        return True
    # у токенизированных акций тикер обычно длинный и буквенный без крипто-
    # признаков; надёжнее опереться на фандинг-интервал + отсутствие крипто-
    # базы нельзя, поэтому оставляем только явные подсказки выше
    return False


def fetch_bars(sess, symbol: str, start_ms: int, hours: int):
    end = start_ms + hours * 3600000
    out: dict[int, float] = {}
    cursor_end = end
    while True:
        try:
            rows = sess.get_kline(category="linear", symbol=symbol,
                                  interval=str(BAR_MIN), start=start_ms,
                                  end=cursor_end, limit=1000
                                  )["result"]["list"]
        except Exception:
            break
        if not rows:
            break
        oldest = cursor_end
        for r in rows:
            ts = int(r[0])
            out[ts] = float(r[4])
            oldest = min(oldest, ts)
        if len(rows) < 1000 or oldest <= start_ms:
            break
        cursor_end = oldest - 1
    return out


def fetch_funding(sess, symbol: str, start_ms: int, hours: int):
    end = start_ms + hours * 3600000
    out: dict[int, float] = {}
    cursor_end = end
    while True:
        try:
            rows = sess.get_funding_rate_history(
                category="linear", symbol=symbol, startTime=start_ms,
                endTime=cursor_end, limit=200)["result"]["list"]
        except Exception:
            break
        if not rows:
            break
        oldest = cursor_end
        for r in rows:
            ts = int(r["fundingRateTimestamp"])
            out[ts] = float(r["fundingRate"])
            oldest = min(oldest, ts)
        if len(rows) < 200 or oldest <= start_ms:
            break
        cursor_end = oldest - 1
    return out


def price_at(bars: dict, base: int, minutes: int):
    """Закрытие бара через ``minutes`` после ``base``.

    ``base`` — метка ПЕРВОГО фактического бара, а не `launchTime`: биржа
    публикует launchTime как анонсированное время, а торговля реально
    начинается на 8–32 минуты позже (проверено на 12 символах). Привязка к
    первому бару убирает эту неопределённость и делает якорь «первая
    торгуемая цена» вместо несуществующей.
    """
    target = base + minutes * 60000
    slot = (target // BAR_MS) * BAR_MS
    for k in (slot, slot + BAR_MS, slot - BAR_MS):
        if k in bars:
            return bars[k]
    return None


def funding_between(fund: dict, base: int, m0: int, m1: int) -> float:
    lo, hi = base + m0 * 60000, base + m1 * 60000
    return sum(v for ts, v in fund.items() if lo < ts <= hi)


def stats(vals: list[float]) -> dict:
    n = len(vals)
    if n < 5:
        return {"n": n}
    mean = statistics.mean(vals)
    sd = statistics.stdev(vals) if n > 1 else 0.0
    se = sd / math.sqrt(n)
    return {"n": n, "mean": mean, "median": statistics.median(vals),
            "wr": 100 * sum(1 for v in vals if v > 0) / n,
            "ci_lo": mean - 1.96 * se, "ci_hi": mean + 1.96 * se,
            "sd": sd}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-days", type=float, default=400)
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--hours", type=int, default=52)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    from pybit.unified_trading import HTTP
    sess = HTTP()

    listings = load_listings(sess, args.max_age_days, args.hours)
    if len(listings) > args.limit:
        listings = listings[-args.limit:]
    print(f"листингов в выборке: {len(listings)} "
          f"({time.strftime('%Y-%m-%d', time.gmtime(listings[0]['launch'] / 1000))}"
          f" .. "
          f"{time.strftime('%Y-%m-%d', time.gmtime(listings[-1]['launch'] / 1000))})")

    # окна зафиксированы заранее
    marks = [5, 15, 30, 60, 240, 720, 1440, 2880]
    data = []
    for i, row in enumerate(listings, 1):
        bars = fetch_bars(sess, row["symbol"], row["launch"], args.hours)
        if len(bars) < 100:
            if args.verbose:
                print(f"    пропуск {row['symbol']}: баров={len(bars)}")
            continue
        fund = fetch_funding(sess, row["symbol"], row["launch"], args.hours)
        base = min(bars)
        px = {m: price_at(bars, base, m) for m in marks}
        if px[5] is None or px[60] is None:
            if args.verbose:
                print(f"    пропуск {row['symbol']}: нет якорных цен "
                      f"(баров={len(bars)})")
            continue
        data.append({**row, "px": px, "fund": fund, "base": base})
        if args.verbose and i % 25 == 0:
            print(f"    [{i}/{len(listings)}] загружено {len(data)}")
    print(f"с полными данными: {len(data)}")

    crypto = [d for d in data if not is_equity(d)]
    equity = [d for d in data if is_equity(d)]
    print(f"    крипто-токены: {len(crypto)}   токенизированные акции/ETF: "
          f"{len(equity)}")

    print("\n" + "=" * 104)
    print("ФОРМА СОБЫТИЯ: медианная доходность от +5м до метки (без издержек)")
    print("=" * 104)
    for label, pop in (("крипто-токены", crypto),
                       ("акции/ETF (контроль)", equity)):
        if len(pop) < 5:
            continue
        print(f"\n{label}, n={len(pop)}")
        print(f"{'до метки':>12}{'n':>6}{'медиана%':>11}{'средн.%':>10}"
              f"{'доля>0':>9}{'95% CI средн.':>24}")
        for m in marks[1:]:
            vals = [(d["px"][m] / d["px"][5] - 1) * 100
                    for d in pop if d["px"].get(m) and d["px"][5]]
            s = stats(vals)
            if s.get("n", 0) < 5:
                continue
            ci = f"[{s['ci_lo']:+.2f}; {s['ci_hi']:+.2f}]"
            lbl = f"{m}м" if m < 60 else f"{m // 60}ч"
            print(f"{lbl:>12}{s['n']:>6}{s['median']:>11.2f}{s['mean']:>10.2f}"
                  f"{s['wr']:>8.0f}%{ci:>24}")

    print("\n" + "=" * 104)
    print("ГИПОТЕЗЫ (заданы до прогона), P&L с фандингом и издержками")
    print("=" * 104)
    hyps = [("H1 моментум: лонг +5м -> +1ч", +1, 5, 60),
            ("H2 распад: шорт +1ч -> +24ч", -1, 60, 1440),
            ("H3 распад: шорт +4ч -> +48ч", -1, 240, 2880)]
    slippages = [0.0, 0.001, 0.003]
    trials = 0
    for name, side, m0, m1 in hyps:
        print(f"\n--- {name} ---")
        print(f"{'слippage':>10}{'n':>6}{'средн.%':>10}{'медиана%':>11}"
              f"{'WR':>7}{'фанд.%':>9}{'95% CI средн.':>24}  IS/OOS")
        for slip in slippages:
            trials += 1
            cost = (2 * TAKER_FEE + 2 * slip) * 100
            rows_pl, fund_part, launches = [], [], []
            for d in crypto:
                p0, p1 = d["px"].get(m0), d["px"].get(m1)
                if not p0 or not p1:
                    continue
                gross = side * (p1 / p0 - 1) * 100
                # шорт получает положительную ставку, лонг платит её
                f = -side * funding_between(d["fund"], d["base"], m0, m1) * 100
                rows_pl.append(gross + f - cost)
                fund_part.append(f)
                launches.append(d["launch"])
            s = stats(rows_pl)
            if s.get("n", 0) < 5:
                print(f"{slip * 100:>9.1f}%{s.get('n', 0):>6}  мало данных")
                continue
            order = sorted(range(len(rows_pl)), key=lambda i: launches[i])
            half = len(order) // 2
            a = stats([rows_pl[i] for i in order[:half]])
            b = stats([rows_pl[i] for i in order[half:]])
            agree = ("ДА" if a.get("n", 0) >= 5 and b.get("n", 0) >= 5
                     and (a["mean"] > 0) == (b["mean"] > 0) else "нет")
            ci = f"[{s['ci_lo']:+.2f}; {s['ci_hi']:+.2f}]"
            print(f"{slip * 100:>9.1f}%{s['n']:>6}{s['mean']:>10.2f}"
                  f"{s['median']:>11.2f}{s['wr']:>6.0f}%"
                  f"{statistics.mean(fund_part):>9.3f}{ci:>24}  {agree}"
                  f" ({a.get('mean', 0):+.2f}/{b.get('mean', 0):+.2f})")

    print("\n" + "=" * 104)
    print(f"испытаний: {trials} — порог дефлировать на это число "
          f"(Bailey/Lopez de Prado)")
    print("приём ТОЛЬКО если CI не накрывает ноль И знак совпал в IS/OOS.")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    sys.exit(main())
