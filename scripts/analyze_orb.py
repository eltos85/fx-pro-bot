"""Глубокий анализ scalp_orb из backtest_all_trades.csv.

Срезы:
  1. По сессиям (Asia / London / NY / Off) через UTC-час entry.
  2. По символам.
  3. По направлениям (long/short).
  4. По дням недели.
  5. По часу UTC.
  6. Анализ прибыльных vs убыточных дней (что их отличает).
  7. Разбивка exit-reason.

Вход: data/backtest_all_trades.csv (из backtest_all.py).
Выход: stdout — таблицы. Файл не пишет (чтобы не плодить артефактов).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median

INPUT = "data/backtest_all_trades.csv"
ROUND_TRIP_FEE_PCT = 0.11  # для референса


@dataclass(slots=True)
class Trade:
    strategy: str
    symbol: str
    direction: str
    entry_ts: datetime
    exit_ts: datetime
    gross_pct: float
    net_pct: float
    exit_reason: str
    hold_bars: int


def _parse_trades(path: str, strat_filter: str) -> list[Trade]:
    out: list[Trade] = []
    with open(path, encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if row["strategy"] != strat_filter:
                continue
            out.append(Trade(
                strategy=row["strategy"],
                symbol=row["symbol"],
                direction=row["direction"],
                entry_ts=datetime.fromisoformat(row["entry_ts"]),
                exit_ts=datetime.fromisoformat(row["exit_ts"]),
                gross_pct=float(row["gross_pct"]),
                net_pct=float(row["net_pct"]),
                exit_reason=row["exit_reason"],
                hold_bars=int(row["hold_bars"]),
            ))
    return out


def _session_for_hour(h: int) -> str:
    """UTC hour → session (совпадает с session_orb.py)."""
    if 0 <= h < 8:
        return "Asia"
    if 8 <= h < 13:
        return "London"
    if 13 <= h < 21:
        return "NY"
    return "Off"


def _day_of_week(dt: datetime) -> str:
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.weekday()]


def _stats(trs: list[Trade]) -> dict:
    if not trs:
        return {"n": 0, "wr": 0.0, "exp_pct": 0.0, "sum_pct": 0.0, "pf": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0}
    wins = [t.net_pct for t in trs if t.net_pct > 0]
    losses = [t.net_pct for t in trs if t.net_pct <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n": len(trs),
        "wr": len(wins) / len(trs) * 100.0,
        "exp_pct": mean(t.net_pct for t in trs) * 100.0,
        "sum_pct": sum(t.net_pct for t in trs) * 100.0,
        "pf": (gross_win / gross_loss) if gross_loss > 0 else 0.0,
        "avg_win": (mean(wins) * 100.0) if wins else 0.0,
        "avg_loss": (mean(losses) * 100.0) if losses else 0.0,
    }


def _print_bucket(title: str, buckets: dict[str, list[Trade]]) -> None:
    print(f"\n=== {title} ===")
    print(f"{'key':<16}{'n':>6}{'WR%':>8}{'avgW%':>9}{'avgL%':>9}{'PF':>6}{'exp%':>8}{'sum%':>10}")
    rows = []
    for k, trs in buckets.items():
        s = _stats(trs)
        rows.append((k, s))
    rows.sort(key=lambda x: x[1]["sum_pct"], reverse=True)
    for k, s in rows:
        print(f"{k:<16}{s['n']:>6}{s['wr']:>8.2f}{s['avg_win']:>9.3f}{s['avg_loss']:>9.3f}"
              f"{s['pf']:>6.2f}{s['exp_pct']:>8.3f}{s['sum_pct']:>10.2f}")


def _daily_pnl(trs: list[Trade]) -> dict[str, float]:
    day: dict[str, float] = defaultdict(float)
    for t in trs:
        day[t.entry_ts.date().isoformat()] += t.net_pct * 100.0
    return dict(day)


def _profitable_day_analysis(trs: list[Trade]) -> None:
    daily = _daily_pnl(trs)
    days_total = len(daily)
    days_plus = sum(1 for v in daily.values() if v > 0)
    days_neg = sum(1 for v in daily.values() if v < 0)
    days_zero = days_total - days_plus - days_neg

    pos_pnl = [v for v in daily.values() if v > 0]
    neg_pnl = [v for v in daily.values() if v < 0]

    print("\n=== ДНЕВНАЯ СТАТИСТИКА ===")
    print(f"Всего активных дней: {days_total}")
    print(f"  прибыльных: {days_plus} ({days_plus/days_total*100:.2f}%)")
    print(f"  убыточных:  {days_neg} ({days_neg/days_total*100:.2f}%)")
    print(f"  zero:       {days_zero}")
    print(f"Средний плюсовый день: +{mean(pos_pnl):.3f}%  (median +{median(pos_pnl):.3f}%)" if pos_pnl else "—")
    print(f"Средний минусовый день: {mean(neg_pnl):.3f}%  (median {median(neg_pnl):.3f}%)" if neg_pnl else "—")

    # что отличает плюсовой день?
    plus_dates = {d for d, v in daily.items() if v > 0}
    neg_dates = {d for d, v in daily.items() if v < 0}

    # Группируем trades по дате → делаем агрегаты
    by_date: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        by_date[t.entry_ts.date().isoformat()].append(t)

    def _day_metric(dates: set[str], metric: str) -> float:
        vals: list[float] = []
        for d in dates:
            day_trs = by_date.get(d, [])
            if not day_trs:
                continue
            if metric == "n_trades":
                vals.append(len(day_trs))
            elif metric == "wr":
                wins = sum(1 for t in day_trs if t.net_pct > 0)
                vals.append(wins / len(day_trs) * 100.0)
            elif metric == "avg_hold":
                vals.append(mean(t.hold_bars for t in day_trs))
            elif metric == "london_share":
                london = sum(1 for t in day_trs if _session_for_hour(t.entry_ts.hour) == "London")
                vals.append(london / len(day_trs) * 100.0)
            elif metric == "ny_share":
                ny = sum(1 for t in day_trs if _session_for_hour(t.entry_ts.hour) == "NY")
                vals.append(ny / len(day_trs) * 100.0)
            elif metric == "asia_share":
                asia = sum(1 for t in day_trs if _session_for_hour(t.entry_ts.hour) == "Asia")
                vals.append(asia / len(day_trs) * 100.0)
        return mean(vals) if vals else 0.0

    print("\n=== ЧТО ОТЛИЧАЕТ ПРИБЫЛЬНЫЙ ДЕНЬ ===")
    print(f"{'metric':<20}{'+days':>12}{'-days':>12}")
    for m in ("n_trades", "wr", "avg_hold", "london_share", "ny_share", "asia_share"):
        print(f"{m:<20}{_day_metric(plus_dates, m):>12.2f}{_day_metric(neg_dates, m):>12.2f}")


def main() -> None:
    trs = _parse_trades(INPUT, "scalp_orb")
    if not trs:
        print("❌ Нет сделок scalp_orb в CSV.")
        return

    print(f"{'='*88}")
    print(f"ГЛУБОКИЙ АНАЛИЗ scalp_orb (n={len(trs)})")
    print(f"{'='*88}")

    overall = _stats(trs)
    print("\n=== OVERALL ===")
    for k, v in overall.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.3f}")
        else:
            print(f"  {k}: {v}")

    # По сессиям
    by_session: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        by_session[_session_for_hour(t.entry_ts.hour)].append(t)
    _print_bucket("ПО СЕССИЯМ", dict(by_session))

    # По символам
    by_sym: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        by_sym[t.symbol].append(t)
    _print_bucket("ПО СИМВОЛАМ", dict(by_sym))

    # По направлениям
    by_dir: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        by_dir[t.direction].append(t)
    _print_bucket("ПО НАПРАВЛЕНИЯМ", dict(by_dir))

    # По дням недели
    by_dow: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        by_dow[_day_of_week(t.entry_ts)].append(t)
    # упорядочить в правильном порядке
    dow_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_dow_ordered = {d: by_dow[d] for d in dow_order if d in by_dow}
    _print_bucket("ПО ДНЯМ НЕДЕЛИ", by_dow_ordered)

    # По часам UTC
    by_hour: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        by_hour[f"{t.entry_ts.hour:02d}:00"].append(t)
    hour_ordered = {h: by_hour[h] for h in sorted(by_hour.keys())}
    _print_bucket("ПО ЧАСАМ UTC", hour_ordered)

    # exit reasons
    by_exit: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        by_exit[t.exit_reason].append(t)
    _print_bucket("ПО EXIT REASON", dict(by_exit))

    # Лучший срез: session × direction
    by_sd: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        sess = _session_for_hour(t.entry_ts.hour)
        by_sd[f"{sess}/{t.direction}"].append(t)
    _print_bucket("SESSION × DIRECTION", dict(by_sd))

    # Дневная статистика
    _profitable_day_analysis(trs)


if __name__ == "__main__":
    main()
