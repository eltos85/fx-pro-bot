"""Скан вселенной для ЯДРА контура H-HYBRID: годится ли монета кроме ETH/BTC.

Шаг 4 плана STRATEGY_HYBRID.md, вынесенный вперёд по запросу: не зацикливаться
на эфире. Read-only, только публичные эндпоинты Bybit, ключи не нужны.

Контур может возникнуть только там, где есть ЯДРО. Скальп выбирает вселенную
сам (RVOL-селектор, до 15 монет), а `swing-bot`/`daytrend-bot` жёстко прибиты
к `BTCUSDT,ETHUSDT` (docker-compose SWING_SYMBOLS / DAYTREND_SYMBOLS). Значит
расширять надо вселенную ядра, и вопрос сводится к одному: на каких ещё
монетах трендовое правило ядра само по себе не убыточно.

Протокол приёмки НЕ придумывается заново. Импортируются функции из
`scalp_swing_research.py` — те же `sma_cross` (сигнал на close, вход на next
open), издержка VIP 0 taker RT 0.110%, тот же гейт `ok()`: n≥30, средний>0,
знак совпал IS/OOS, медиана удержания 1–10 суток. Так исключается подгонка
критерия под желаемый список монет (`no-data-fitting.mdc`).

Справочные метрики (в гейт НЕ входят, нужны для оценки пригодности к контуру):
  ER      — Kaufman efficiency ratio |P_end−P_0| / Σ|ΔP|: трендовость против
            пилы. Аналог `regime_ratio` из scalp_bot/analysis/regime.py
  NATR    — медианный ATR(14)/close на 4h, %: есть ли ход над издержками
  long%   — доля времени, когда правило ядра в рынке
  серия   — средняя длина непрерывного long-режима, суток
  fixcost — RT-издержка / медианный |4h ход|: во сколько обходится одна
            фиксация контура относительно типичного хода бара
  funding — annualized ставка: ядро держит long долго и платит funding

Запуск (публичный API, ключи не нужны):
    python3 scripts/hybrid_universe_scan.py --days 730 --top 30
    python3 scripts/hybrid_universe_scan.py --symbols SOLUSDT,BNBUSDT --days 365
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pybit.unified_trading import HTTP  # noqa: E402

from scalp_swing_research import RT, fetch, ok, sma_cross  # noqa: E402

# Порог ликвидности — тот же, что у авто-селектора скальпа
# (SCALP_UNIVERSE_MIN_TURNOVER_USD, docker-compose): $100M оборота за 24ч.
MIN_TURNOVER_USD = 100_000_000.0

STABLES = {"USDCUSDT", "USDEUSDT", "FDUSDUSDT", "TUSDUSDT", "DAIUSDT",
           "USDRUSDT", "BUSDUSDT", "EURUSDT", "USTCUSDT"}

THROTTLE_SEC = 0.15


def _liquid_symbols(sess: HTTP, top: int) -> list[tuple[str, float]]:
    resp = sess.get_tickers(category="linear")
    rows = (resp.get("result") or {}).get("list") or []
    out = []
    for r in rows:
        sym = r.get("symbol") or ""
        if not sym.endswith("USDT") or sym in STABLES:
            continue
        try:
            turn = float(r.get("turnover24h") or 0)
        except ValueError:
            continue
        if turn < MIN_TURNOVER_USD:
            continue
        out.append((sym, turn))
    out.sort(key=lambda x: -x[1])
    return out[:top]


def _efficiency_ratio(closes: list[float]) -> float:
    """Kaufman ER: направленный ход / сумма всех колебаний. 1 = чистый тренд."""
    if len(closes) < 2:
        return 0.0
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return abs(closes[-1] - closes[0]) / path if path else 0.0


def _natr_median(bars: list[tuple], length: int = 14) -> float:
    """Медиана ATR(length)/close × 100 по барам (простое среднее TR)."""
    trs = []
    for i in range(1, len(bars)):
        _, _, hi, lo, cl = bars[i]
        prev_cl = bars[i - 1][4]
        trs.append(max(hi - lo, abs(hi - prev_cl), abs(lo - prev_cl)))
    vals = []
    for i in range(length, len(trs)):
        atr = sum(trs[i - length:i]) / length
        cl = bars[i + 1][4]
        if cl > 0:
            vals.append(atr / cl * 100.0)
    return statistics.median(vals) if vals else 0.0


def _regime_shape(bars: list[tuple], fast: int = 20,
                  slow: int = 50) -> tuple[float, float]:
    """Доля времени в long-режиме и средняя длина серии (сутки), 4h бары."""
    cl = [b[4] for b in bars]
    states = []
    for i in range(slow, len(bars)):
        sf = statistics.mean(cl[i - fast:i])
        ss = statistics.mean(cl[i - slow:i])
        states.append(1 if sf > ss else 0)
    if not states:
        return 0.0, 0.0
    runs, cur = [], 0
    for s in states:
        if s:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    share = 100.0 * sum(states) / len(states)
    avg_run_days = (statistics.mean(runs) * 4 / 24) if runs else 0.0
    return share, avg_run_days


def _fix_cost_ratio(bars: list[tuple]) -> float:
    """RT-издержка относительно медианного |хода| 4h бара."""
    rets = []
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1][4], bars[i][4]
        if prev > 0:
            rets.append(abs(cur / prev - 1.0))
    med = statistics.median(rets) if rets else 0.0
    return (RT / med) if med else float("inf")


def _funding_annual_pct(sess: HTTP, symbol: str) -> float | None:
    """Средняя ставка funding × 3 раза в сутки × 365, %. >0 = платим за long.
    https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
    """
    try:
        resp = sess.get_funding_rate_history(category="linear", symbol=symbol,
                                            limit=200)
    except Exception:
        return None
    rows = (resp.get("result") or {}).get("list") or []
    rates = []
    for r in rows:
        try:
            rates.append(float(r.get("fundingRate")))
        except (TypeError, ValueError):
            continue
    if not rates:
        return None
    return statistics.mean(rates) * 3 * 365 * 100.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--top", type=int, default=30,
                    help="сколько самых ликвидных перпов взять")
    ap.add_argument("--symbols", default="",
                    help="явный список вместо отбора по обороту")
    args = ap.parse_args()

    sess = HTTP()
    if args.symbols:
        pairs = [(s.strip().upper(), 0.0)
                 for s in args.symbols.split(",") if s.strip()]
    else:
        pairs = _liquid_symbols(sess, args.top)

    start = int((time.time() - args.days * 86400) * 1000)
    print(f"Ядро контура: 4h SMA 20/50 long/flat, издержка RT {RT * 100:.3f}%, "
          f"{args.days}д, {len(pairs)} символов")
    print("Гейт (из scalp_swing_research.py, не переопределяется): n>=30, "
          "средний>0, IS/OOS один знак, медиана удержания 1-10 суток")
    print("ER/NATR/long%/серия/fixcost/funding — справочные, в гейт не входят\n")

    header = (f"{'символ':<12} {'$M/24h':>8} {'n':>4} {'ср%':>7} {'мед%':>7} "
              f"{'итог%':>8} {'WR':>4} {'держ.д':>6} {'IS/OOS':>6} "
              f"{'значим':>7} {'ER':>5} {'NATR%':>6} {'long%':>6} "
              f"{'fixcost':>7} {'fund%':>6} вердикт")
    print(header)
    print("-" * len(header))

    accepted, rows_out = [], []
    for sym, turn in pairs:
        bars = fetch(sess, sym, "240", start)
        time.sleep(THROTTLE_SEC)
        if len(bars) < 200:
            print(f"{sym:<12} {turn / 1e6:>8.0f} — мало истории "
                  f"({len(bars)} бар)")
            continue
        r = sma_cross(bars, 20, 50, RT)
        closes = [b[4] for b in bars]
        er = _efficiency_ratio(closes)
        natr = _natr_median(bars)
        long_share, run_days = _regime_shape(bars)
        fixcost = _fix_cost_ratio(bars)
        fund = _funding_annual_pct(sess, sym)
        time.sleep(THROTTLE_SEC)
        good, why = ok(r)
        if r.get("n", 0) < 5:
            print(f"{sym:<12} {turn / 1e6:>8.0f} n={r.get('n', 0)} мало сделок")
            continue
        agr = "ДА" if r["agree"] else "нет"
        fund_s = f"{fund:+.1f}" if fund is not None else "n/a"
        # 95% CI среднего: если он накрывает нуль, «положительный средний» —
        # шум, сколько бы ни был велик итог. Harvey & Liu «Backtesting».
        significant = r["ci_lo"] > 0
        sig_s = "знач." if significant else "CI∋0"
        verdict = "ПРИНЯТ" if good else why
        if good and not significant:
            verdict = "ПРИНЯТ*"
        print(f"{sym:<12} {turn / 1e6:>8.0f} {r['n']:>4} {r['mean']:>+7.3f} "
              f"{r['med']:>+7.3f} {r['tot']:>+8.1f} {r['wr']:>3.0f}% "
              f"{r['hmed'] / 24:>6.1f} {agr:>6} {sig_s:>7} {er:>5.3f} "
              f"{natr:>6.2f} {long_share:>5.0f}% {fixcost:>7.2f} "
              f"{fund_s:>6} {verdict}")
        rows_out.append((sym, r, er, natr, long_share, run_days, fixcost, fund))
        if good:
            accepted.append(sym if significant else f"{sym}*")

    print("\n" + "=" * 78)
    if accepted:
        print(f"Прошли гейт ядра ({len(accepted)}): {', '.join(accepted)}")
        print("* = средний положителен, но 95% CI накрывает нуль — "
              "статистически это ноль.")
        print("Это НЕ разрешение включать их в SWING_SYMBOLS: гейт проверяет "
              "только само трендовое правило.")
        print(f"Проверено символов: {len(rows_out)}. При таком числе "
              "параллельных тестов 1-2 «прохода» ожидаемы случайно "
              "(Harvey & Liu): нужен OOS и проверка на других правилах.")
        print("Для контура дополнительно нужен forward по протоколу "
              "STRATEGY_HYBRID.md §8.")
    else:
        print("Гейт ядра не прошёл никто. Расширять вселенную ядра нельзя: "
              "на всех монетах правило SMA 20/50 4h нестабильно по IS/OOS.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
