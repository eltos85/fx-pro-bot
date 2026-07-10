"""Диагностика live-vs-backtest разрыва momentum (06-05→07-10, observation only).

Вопрос (BUILDLOG 2026-07-10): live 126 сделок WR 29% / PF 0.55 / EXP −0.16R
(p=0.018), при этом исторический бэктест давал WR 47% / PF 2.04. Развилка:

  A. Реплика стратегии на ЭТОМ ЖЕ окне тоже теряет → режим рынка
     (TSMOM не работает в текущем рейндже) — искать рынок/таймфрейм на
     истории, стратегию не «подкручивать».
  B. Реплика на этом окне зарабатывает → implementation gap (исполнение,
     задержка входа, фильтры) — сверять сделку-к-сделке.

Метод: реплика из scripts/momentum_exit_backtest.py (BE@1.0R = текущий
конфиг), yfinance 5m→1h mid, окно [2026-06-05, 2026-07-10). Два режима:
  raw      — без фильтров (как исторический бэктест);
  filtered — с session-filter [7,21) UTC (деплой 06-26 08:15) и
             friday-entry-block с 20:00 (деплой 07-02) в те же календарные
             даты, что и live — максимально честное сравнение с live.

Live-сторона берётся из data/loss_audit_trades_0710.json (broker-truth).
Спред/комиссия: в реплике mid-цены; по аудиту costs = 5-6% net-лосса, в R
это ~0.01-0.03R/сделку — не двигает вывод A/B.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
from momentum_exit_backtest import (  # noqa: E402
    SYMBOLS, backtest_symbol, load, to_1h,
)

WINDOW_START = pd.Timestamp("2026-06-05", tz="UTC")
WINDOW_END = pd.Timestamp("2026-07-10 23:59", tz="UTC")
SESSION_DEPLOY = pd.Timestamp("2026-06-26 08:15", tz="UTC")
FRIDAY_BLOCK_DEPLOY = pd.Timestamp("2026-07-02 12:00", tz="UTC")


def entry_allowed_live_filters(ts: pd.Timestamp) -> bool:
    """Реплика live-фильтров в календаре их деплоя (для режима filtered)."""
    if ts >= SESSION_DEPLOY:
        # session-filter смотрит час ЗАКРЫТОГО 1h бара (час ts-1h)
        bar_hour = (ts - pd.Timedelta(hours=1)).hour
        if not (7 <= bar_hour < 21):
            return False
    if ts >= FRIDAY_BLOCK_DEPLOY:
        if ts.weekday() == 4 and (ts.hour, ts.minute) >= (20, 0):
            return False
    return True


def stats(label: str, rs: list[float]) -> None:
    if not rs:
        print(f"  {label:<26} n=0")
        return
    a = np.array(rs)
    wins, losses = a[a > 0], a[a < 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    print(f"  {label:<26} n={len(a):>3} netR={a.sum():+7.2f} "
          f"WR={len(wins)/len(a)*100:>3.0f}% avgR={a.mean():+5.2f} PF={pf:>5.2f}")


def main() -> int:
    data = {}
    for sym in SYMBOLS:
        df5 = load(sym)
        if df5 is None or len(df5) < 3000:
            print(f"WARN {sym}: мало данных", file=sys.stderr)
            continue
        data[sym] = (df5, to_1h(df5))
    if not data:
        return 1

    all_trades: dict[str, list[tuple[pd.Timestamp, float, str]]] = {}
    for sym, (df5, df1h) in data.items():
        # BE@1.0R — текущий live-конфиг
        for t, r in backtest_symbol(df5, df1h, 1.0):
            all_trades.setdefault(sym, []).append((t, r, sym))

    in_window = [
        (t, r, s) for lst in all_trades.values() for (t, r, s) in lst
        if WINDOW_START <= t <= WINDOW_END
    ]
    print(f"=== РЕПЛИКА (mid, BE@1.0R) на live-окне "
          f"{WINDOW_START.date()} → {WINDOW_END.date()} ===\n")

    print("── raw (без live-фильтров, как исторический бэктест) ──")
    stats("ALL", [r for _, r, _ in in_window])
    for sym in sorted(data):
        stats(sym, [r for t, r, s in in_window if s == sym])

    filt = [(t, r, s) for (t, r, s) in in_window if entry_allowed_live_filters(t)]
    print("\n── filtered (session-filter с 06-26, friday-block с 07-02) ──")
    stats("ALL", [r for _, r, _ in filt])
    for sym in sorted(data):
        stats(sym, [r for t, r, s in filt if s == sym])

    print("\n── по неделям (filtered) ──")
    weeks: dict[str, list[float]] = {}
    for t, r, _ in filt:
        iso = t.isocalendar()
        weeks.setdefault(f"{iso[0]}-{iso[1]:02d}", []).append(r)
    for w in sorted(weeks):
        stats(w, weeks[w])

    # live для сравнения
    try:
        live = json.load(open("data/loss_audit_trades_0710.json"))
    except OSError:
        print("\n(live-дамп не найден — сравнение пропущено)")
        return 0
    print("\n=== LIVE (broker-truth, тот же период) ===")
    live_r = [t["r"] for t in live if t.get("r") is not None]
    stats("ALL (в R)", live_r)
    weeks_l: dict[str, list[float]] = {}
    for t in live:
        if t.get("r") is None:
            continue
        iso = datetime.fromtimestamp(t["ts_open"], tz=UTC).isocalendar()
        weeks_l.setdefault(f"{iso[0]}-{iso[1]:02d}", []).append(t["r"])
    for w in sorted(weeks_l):
        stats(w, weeks_l[w])
    return 0


if __name__ == "__main__":
    sys.exit(main())
