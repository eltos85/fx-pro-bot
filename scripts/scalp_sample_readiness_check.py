"""Набрана ли выборка по боевым стратам scalp_bot и берут ли они порог отключения.

Read-only: скрипт только читает SQLite и печатает. Ничего не активирует и не
отключает — решение об изменении стратегии принимает пользователь.

Проверяются все четыре условия `sample-size.mdc` разом:

* ``n ≥ 100`` сделок по стратегии;
* ``≥ 2 недели`` данных (печатается календарный размах);
* ``p < 0.05`` для среднего результата в R;
* размер эффекта: ``|cleanR| ≥ 0.3`` **или** дефицит win rate до безубытка
  ``≥ 10`` п.п.

Дополнительно печатается число независимых кластеров символ×день. Наблюдения
внутри одного символо-дня скоррелированы (тот же уровень, тот же режим, те же
участники), поэтому ошибка среднего считается кластер-робастной, а не iid.
Порог 40 кластеров — общепринятое эмпирическое правило для такого инференса:
Cameron & Miller (2015) «A Practitioner's Guide to Cluster-Robust Inference»,
Journal of Human Resources 50(2):317–372, §VI «few clusters».

Из выборки вычитаются два класса строк, несопоставимых с остальными:

1. **Двойной тариф.** С v0.18.61 (`c34b444`, 2026-08-08) символы, чей taker
   выше стандартной сетки Bybit, не торгуются. Их прошлые сделки — другая
   экономика издержек (комиссия ~0.46R против ~0.25R).
2. **Общий счёт one-way.** С запуска swing-бота на BTCUSDT/ETHUSDT
   (2026-08-18T11:19Z) `pnl_usd` скальпа содержит чужой P&L: `set_trading_stop`
   в режиме `Full` вешает стоп на весь лот символа. Карантин введён в
   BUILDLOG_SCALP 2026-08-20, механизм разобран там же 2026-08-19 (H-SL-CONTAM).

    docker exec -i fx-pro-bot-scalp-bot-1 python3 - < scripts/scalp_sample_readiness_check.py
"""
from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime

DB = "/data/scalp_bot.sqlite"

STRATEGIES = ("sweep_fade", "sweep_fade_canon", "density_break")

# Стандартная сетка Bybit: taker 0.055%, допуск 5% — как в analysis/fees.py.
# https://bybit-exchange.github.io/docs/v5/enum#tradingfee
TAKER_STANDARD_MAX = 0.00055 * 1.05

QUARANTINE_SYMBOLS = ("BTCUSDT", "ETHUSDT")
QUARANTINE_SINCE = "2026-08-18T11:19:00"

_NON_TRADE = ("restart_flat", "entry_Cancelled", "entry_Rejected",
              "entry_Deactivated", "entry_timeout")

MIN_N = 100
MIN_CLUSTERS = 40
MIN_SPAN_DAYS = 14
MIN_EFFECT_R = 0.3
MIN_WR_DEFICIT_PP = 10.0


def _ts(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d")


def _two_sided_p(z: float) -> float:
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))


def clean_trades(con: sqlite3.Connection, strategy: str,
                 double_fee: set[str]) -> tuple[list[dict], int]:
    """Реальные сделки страты без двойного тарифа и без карантина."""
    rows = [dict(r) for r in con.execute(
        "SELECT symbol, qty, entry, sl, pnl_usd, close_reason, ts_open "
        "FROM trades WHERE strategy=? AND status='closed' "
        "AND pnl_usd IS NOT NULL", (strategy,))]
    real = [r for r in rows
            if str(r["close_reason"] or "") not in _NON_TRADE
            and not str(r["close_reason"] or "").startswith("entry_")
            and r["sl"] and r["entry"] and r["qty"]
            and abs(r["entry"] - r["sl"]) * r["qty"] > 0]
    quarantine = _ts(QUARANTINE_SINCE)
    clean = []
    for r in real:
        if r["symbol"] in double_fee:
            continue
        if r["symbol"] in QUARANTINE_SYMBOLS and r["ts_open"] >= quarantine:
            continue
        # R-единица = запланированный риск сделки в долларах.
        r["R"] = r["pnl_usd"] / (abs(r["entry"] - r["sl"]) * r["qty"])
        clean.append(r)
    return clean, len(real) - len(clean)


def report(strategy: str, rows: list[dict], dropped: int) -> None:
    n = len(rows)
    print(f"\n{'='*70}\n{strategy}\n{'='*70}")
    if n < 2:
        print("  недостаточно сделок")
        return
    values = [r["R"] for r in rows]
    mean = sum(values) / n
    clusters: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        clusters.setdefault((r["symbol"], _day(r["ts_open"])), []).append(
            r["R"] - mean)
    g = len(clusters)
    se = math.sqrt(
        sum(sum(v) ** 2 for v in clusters.values()) * g / (g - 1)) / n
    p = _two_sided_p(mean / se)
    span = (max(r["ts_open"] for r in rows)
            - min(r["ts_open"] for r in rows)) / 86_400.0

    wins = [r["pnl_usd"] for r in rows if r["pnl_usd"] > 0]
    losses = [-r["pnl_usd"] for r in rows if r["pnl_usd"] <= 0]
    wr = 100.0 * len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    # Безубыточный WR при текущем соотношении среднего выигрыша к проигрышу.
    breakeven = (100.0 * avg_loss / (avg_win + avg_loss)
                 if (avg_win + avg_loss) else 0.0)
    deficit = breakeven - wr

    checks = {
        f"n≥{MIN_N}": n >= MIN_N,
        f"кластеров≥{MIN_CLUSTERS}": g >= MIN_CLUSTERS,
        f"размах≥{MIN_SPAN_DAYS}д": span >= MIN_SPAN_DAYS,
        "p<0.05": p < 0.05,
        f"эффект (|R|≥{MIN_EFFECT_R} или дефицит WR≥{MIN_WR_DEFICIT_PP}пп)":
            abs(mean) >= MIN_EFFECT_R or deficit >= MIN_WR_DEFICIT_PP,
    }
    print(f"  n={n}  кластеров={g}  размах={span:.0f}д  "
          f"с {_day(min(r['ts_open'] for r in rows))}  отброшено {dropped}")
    print(f"  cleanR={mean:+.3f}  CI[{mean-1.96*se:+.3f}; {mean+1.96*se:+.3f}]"
          f"  p={p:.2g}")
    print(f"  WR={wr:.1f}%  R:R={avg_win/avg_loss if avg_loss else 0:.2f}  "
          f"безубыточный WR={breakeven:.1f}%  дефицит={deficit:+.1f} п.п.")
    for name, ok in checks.items():
        print(f"    [{'OK' if ok else '  '}] {name}")
    print(f"  ВЕРДИКТ: {'все условия выполнены' if all(checks.values()) else 'порог отключения НЕ взят'}")


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    double_fee = {r[0] for r in con.execute(
        "SELECT symbol FROM symbol_fees WHERE taker_rate > ?",
        (TAKER_STANDARD_MAX,))}
    print("условия sample-size.mdc + кластеры (Cameron & Miller 2015)")
    print(f"исключены двойной тариф: {', '.join(sorted(double_fee))}")
    print(f"исключены {'/'.join(QUARANTINE_SYMBOLS)} с {QUARANTINE_SINCE}Z "
          "(карантин общего счёта, BUILDLOG_SCALP 2026-08-20)")
    for strategy in STRATEGIES:
        rows, dropped = clean_trades(con, strategy, double_fee)
        report(strategy, rows, dropped)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
