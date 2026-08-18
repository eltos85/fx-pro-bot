"""Арифметика маркет-мейкинга: при каком тарифе сходится и что для этого нужно.

Зачем
─────
Перебор направленных стратегий закрыт (BUILDLOG_SCALP.md, 2026-08-18): по ~11k
контрфактов MFE/MAE=0.98–1.21, то есть цена на нашем горизонте — бездрейфовое
блуждание, а у него любая схема стоп/тейк даёт нулевое валовое ожидание.
Прибыль из такого процесса извлекает только тот, кому ПЛАТЯТ за услугу. Канал
«страховка» (фандинг) закрыт замером: 0.0008% за 8ч = 0.9% APR gross против
0.110% round-trip. Остаётся канал «ликвидность» — спред.

Условие жизнеспособности пассивной двусторонней котировки:
    спред  >  2 * maker_fee  +  издержка неблагоприятного отбора
Первое слагаемое известно из тарифа, второе — главный реальный расход маркет-
мейкера: когда цена уходит, исполняется только одна нога, и мы остаёмся с
позицией против движения. Прокси для него — реализованная волатильность за
время экспозиции котировки (Glosten/Milgrom 1985; Roll 1984: наблюдаемый спред
как раз и есть компенсация за этот риск).

Скрипт печатает по каждому символу: медианный спред (много срезов), спред в
тиках, минутную волатильность, и требуемый maker-тариф для безубытка ДО учёта
отбора. Затем сверяет с официальной лестницей Bybit и считает, какой оборот
нужен для достижения нужного уровня, против нашего фактического оборота из БД.

Источники ставок (api-docs.mdc):
- сетка VIP/Pro: https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate
- группы и рибейты по API: https://bybit-exchange.github.io/docs/v5/market/fee-group-info
- Market Maker Incentive Program (заявка через institutional RM):
  https://www.bybit.com/en/help-center/article/Introduction-to-the-Market-Maker-Incentive-Program/
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

# Официальная лестница для Perpetual & Futures, maker/taker в долях.
# Требования — «30-дневный оборот ИЛИ баланс активов», что выше.
FEE_LADDER = [
    # (уровень, maker, taker, требование)
    ("VIP 0 (наш)",   0.000200, 0.000550, "—"),
    ("VIP 1",         0.000180, 0.000400, "оборот/активы по сетке Bybit"),
    ("VIP 2",         0.000160, 0.000375, "оборот/активы по сетке Bybit"),
    ("VIP 3",         0.000140, 0.000350, "$50M деривативы 30д или $500k активов"),
    ("VIP 4",         0.000120, 0.000320, "оборот/активы по сетке Bybit"),
    ("VIP 5",         0.000100, 0.000320, "оборот/активы по сетке Bybit"),
    ("Supreme VIP",   0.000000, 0.000300, "$500M деривативы 30д"),
    ("Pro 3+",        0.000000, 0.000320, "институциональный оборот"),
    ("MM1 Group 1-2", -0.000010, 0.000320, "weighted maker share >= 0.03%"),
    ("MM1 Group 5",   -0.000075, 0.000320, "weighted maker share >= 0.03%"),
    ("MM2 Group 5",   -0.000100, 0.000320, "weighted maker share >= 0.50%"),
    ("MM3 Group 5",   -0.000125, 0.000320, "weighted maker share >= 1.00%"),
]


def measure(sess, min_turnover: float, samples: int, pause: float,
            verbose: bool) -> list[dict]:
    """Медианный спред, тик и минутная волатильность по ликвидным символам."""
    acc: dict[str, dict] = {}
    for k in range(samples):
        rows = sess.get_tickers(category="linear")["result"]["list"]
        for r in rows:
            sym = r.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                bid = float(r.get("bid1Price") or 0)
                ask = float(r.get("ask1Price") or 0)
                turn = float(r.get("turnover24h") or 0)
            except (TypeError, ValueError):
                continue
            if bid <= 0 or ask <= 0 or turn < min_turnover:
                continue
            mid = (ask + bid) / 2
            d = acc.setdefault(sym, {"sp": [], "turnover": turn, "mid": mid})
            d["sp"].append((ask - bid) / mid * 100.0)
        if k < samples - 1:
            time.sleep(pause)

    # тик-сайз из инструментов
    ticks: dict[str, float] = {}
    cursor = ""
    while True:
        resp = sess.get_instruments_info(category="linear", limit=1000,
                                         cursor=cursor)
        res = resp.get("result", {})
        for it in res.get("list") or []:
            try:
                ticks[it["symbol"]] = float(it["priceFilter"]["tickSize"])
            except (KeyError, TypeError, ValueError):
                continue
        cursor = res.get("nextPageCursor") or ""
        if not cursor:
            break

    out = []
    for sym, d in acc.items():
        if len(d["sp"]) < max(3, samples // 2):
            continue
        # минутная волатильность за сутки — прокси неблагоприятного отбора
        try:
            kl = sess.get_kline(category="linear", symbol=sym,
                                interval="1", limit=200)["result"]["list"]
            closes = [float(r[4]) for r in reversed(kl)]
            rets = [abs(closes[i] / closes[i - 1] - 1) * 100
                    for i in range(1, len(closes)) if closes[i - 1]]
            vol1m = statistics.median(rets) if rets else float("nan")
        except Exception:
            vol1m = float("nan")
        tick = ticks.get(sym, 0.0)
        sp = statistics.median(d["sp"])
        out.append({
            "symbol": sym, "spread_pct": sp, "turnover": d["turnover"],
            "tick_pct": (tick / d["mid"] * 100.0) if tick and d["mid"] else 0.0,
            "vol1m_pct": vol1m,
        })
        if verbose:
            print(f"    {sym:<14} спред={sp:.4f}% срезов={len(d['sp'])}")
    out.sort(key=lambda x: -x["spread_pct"])
    return out


def our_volume(db_path: str) -> tuple[float, int, int]:
    """Наш фактический оборот деривативов за 30 дней из БД сделок."""
    import sqlite3
    db = sqlite3.connect(db_path)
    row = db.execute("""
        select coalesce(sum(abs(entry_price * qty)) * 2, 0), count(*),
               count(distinct date(ts_open,'unixepoch'))
        from trades
        where status='closed'
          and coalesce(close_reason,'') not like 'entry_%'
          and ts_open >= strftime('%s','now','-30 days')
    """).fetchone()
    return float(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-turnover", type=float, default=20_000_000)
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--pause", type=float, default=4.0)
    ap.add_argument("--db", default="")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    from pybit.unified_trading import HTTP
    sess = HTTP()

    print(f"замер спреда: {args.samples} срезов с шагом {args.pause}с, "
          f"оборот24ч >= ${args.min_turnover:,.0f}")
    rows = measure(sess, args.min_turnover, args.samples, args.pause,
                   args.verbose)
    if not rows:
        print("нет данных")
        return 1
    spreads = [r["spread_pct"] for r in rows]
    med = statistics.median(spreads)

    print(f"\nсимволов измерено: {len(rows)}; медиана спреда {med:.4f}%")
    print("\n" + "=" * 108)
    print("СИМВОЛЫ: спред, тик, минутная волатильность, требуемый maker для "
          "безубытка")
    print("=" * 108)
    print(f"{'символ':<14}{'спред%':>9}{'тик%':>8}{'спред/тик':>11}"
          f"{'вола1м%':>10}{'спред/вола':>12}{'нужен maker<':>14}"
          f"{'оборот,$млн':>13}")
    for r in rows[:28]:
        need = r["spread_pct"] / 2.0
        sp_tick = (r["spread_pct"] / r["tick_pct"]) if r["tick_pct"] else 0
        sp_vol = (r["spread_pct"] / r["vol1m_pct"]) if r["vol1m_pct"] else 0
        print(f"{r['symbol']:<14}{r['spread_pct']:>9.4f}{r['tick_pct']:>8.4f}"
              f"{sp_tick:>11.1f}{r['vol1m_pct']:>10.4f}{sp_vol:>12.2f}"
              f"{need:>13.4f}%{r['turnover'] / 1e6:>13.0f}")

    print("\n" + "=" * 108)
    print("ЛЕСТНИЦА ТАРИФОВ: сколько символов проходят условие спред > 2*maker")
    print("(это порог ДО издержки неблагоприятного отбора — необходимое, но не "
          "достаточное условие)")
    print("=" * 108)
    print(f"{'уровень':<16}{'maker':>10}{'2*maker':>10}{'проходят':>11}"
          f"{'доля':>8}   требование")
    for name, maker, _taker, req in FEE_LADDER:
        rt = 2 * maker
        ok = sum(1 for r in rows if r["spread_pct"] / 100.0 > rt)
        print(f"{name:<16}{maker * 100:>9.4f}%{rt * 100:>9.4f}%{ok:>11}"
              f"{100 * ok / len(rows):>7.0f}%   {req}")

    print("\n" + "=" * 108)
    print("ВЫВОД ПО ТАРИФУ")
    print("=" * 108)
    print(f"медианный спред {med:.4f}% ⇒ для безубытка нужен maker "
          f"< {med / 2:.5f}%")
    reachable = [n for n, m, _t, _r in FEE_LADDER if 2 * m < med / 100.0]
    print(f"уровни, где условие выполняется по медиане: "
          f"{', '.join(reachable) if reachable else 'НЕТ НИ ОДНОГО'}")
    wide = [r for r in rows if r["spread_pct"] > 0.040]
    print(f"на нашем VIP 0 (maker 0.0200%, round-trip 0.0400%) условие "
          f"выполняют {len(wide)} символов:")
    for r in wide[:10]:
        print(f"    {r['symbol']:<14}спред={r['spread_pct']:.4f}% "
              f"спред/вола1м={(r['spread_pct'] / r['vol1m_pct']) if r['vol1m_pct'] else 0:.2f} "
              f"оборот=${r['turnover'] / 1e6:.0f}млн")

    if args.db:
        vol, n, days = our_volume(args.db)
        print(f"\nнаш фактический оборот за 30 дней: ${vol:,.0f} "
              f"({n} сделок, {days} дней с активностью)")
        for name, maker, _t, req in FEE_LADDER:
            if "$" in req:
                print(f"    до {name}: требуется {req}")
        if vol > 0:
            print(f"    разрыв до Supreme VIP ($500M/30д): "
                  f"{500e6 / vol:.0f}x нашего оборота")
    print("=" * 108)
    return 0


if __name__ == "__main__":
    sys.exit(main())
