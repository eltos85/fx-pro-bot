#!/usr/bin/env python3
"""Аудит density_break: подбор монет / fill-rate / по символам (C-06, 2026-06-08).

Артефакт для STRATEGY_CONTRADICTIONS_SCALP.md §C-06. Отвечает на вопрос
пользователя «исследовать подбор монет для density_break» — синхронны ли
авто-вселенная (volatility-based), сайзинг и природа пробойной стратегии.

ВЫВОД (см. C-06): vol-вселенная для пробоя КАНОНИЧНА (momentum хочет тонких/
волатильных книг — Tradeify DOM; deep=fade, thin/vol=breakout). Узкие места —
НЕ подбор монет, а fill-rate (maker-лимит vs убегающий пробой) и отсутствие
confirmation ложного пробоя. n=66 filled < 100 → решений по правилу sample-size
НЕ принимаем (no-data-fitting.mdc / sample-size.mdc).

Запуск на VPS (БД в volume scalp_bot_data):
    docker cp scripts/scalp_dbr_stats.py fx-pro-bot-scalp-bot-1:/tmp/ && \\
    docker exec fx-pro-bot-scalp-bot-1 python3 /tmp/scalp_dbr_stats.py

Локально:  python3 scripts/scalp_dbr_stats.py /path/to/scalp_bot.sqlite
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "/data/scalp_bot.sqlite"

# low-vol мейджоры (ES-подобные глубокие книги) vs vol-альты (NQ-подобные тонкие)
MAJORS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "LTCUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "DOGEUSDT",
)
# close_reason'ы, означающие НЕзаполненный сигнал (не реальная сделка)
NOFILL = ("entry_Cancelled", "entry_timeout", "restart_flat")


def _filled_pred() -> str:
    return ("status='closed' AND close_reason NOT LIKE 'entry_%' "
            "AND close_reason!='restart_flat'")


def main() -> None:
    c = sqlite3.connect(DB)
    print(f"USING: {DB}\n")

    print("=== density_break: всего по статусу ===")
    for row in c.execute("SELECT status, COUNT(*) FROM trades "
                         "WHERE strategy='density_break' GROUP BY status"):
        print(row)

    print("\n=== close_reason (closed) ===")
    for row in c.execute(
            "SELECT close_reason, COUNT(*), ROUND(SUM(pnl_usd),2) FROM trades "
            "WHERE strategy='density_break' AND status='closed' "
            "GROUP BY close_reason ORDER BY COUNT(*) DESC"):
        print(row)

    tot = c.execute("SELECT COUNT(*) FROM trades "
                    "WHERE strategy='density_break'").fetchone()[0]
    filled = c.execute(f"SELECT COUNT(*) FROM trades "
                       f"WHERE strategy='density_break' AND {_filled_pred()}"
                       ).fetchone()[0]
    print(f"\n=== fill-rate: filled={filled} / всего={tot} = "
          f"{filled / tot * 100:.1f}% (остальное — maker не налился) ===")

    print("\n=== filled-only WR / net ===")
    print(c.execute(
        f"SELECT COUNT(*), SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END), "
        f"ROUND(SUM(pnl_usd),2) FROM trades "
        f"WHERE strategy='density_break' AND {_filled_pred()}").fetchone())

    print("\n=== по символам (filled): symbol, n, wins, net ===")
    for row in c.execute(
            f"SELECT symbol, COUNT(*), SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END), "
            f"ROUND(SUM(pnl_usd),2) FROM trades "
            f"WHERE strategy='density_break' AND {_filled_pred()} "
            f"GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 25"):
        print(row)

    ph = ",".join("?" * len(MAJORS))
    print("\n=== сегменты (filled): n, wins, net ===")
    print("low-vol мейджоры (deep=fade-родной):", c.execute(
        f"SELECT COUNT(*), SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END), "
        f"ROUND(SUM(pnl_usd),2) FROM trades WHERE strategy='density_break' "
        f"AND {_filled_pred()} AND symbol IN ({ph})", MAJORS).fetchone())
    print("vol-альты (thin/vol=breakout-родной):", c.execute(
        f"SELECT COUNT(*), SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END), "
        f"ROUND(SUM(pnl_usd),2) FROM trades WHERE strategy='density_break' "
        f"AND {_filled_pred()} AND symbol NOT IN ({ph})", MAJORS).fetchone())

    mn, mx = c.execute("SELECT MIN(ts_open), MAX(ts_open) FROM trades "
                       "WHERE strategy='density_break'").fetchone()
    print("\nпериод:", dt.datetime.fromtimestamp(mn, dt.UTC),
          "->", dt.datetime.fromtimestamp(mx, dt.UTC))
    print("\n[sample-size] n=%d filled < 100 → решений НЕ принимаем "
          "(sample-size.mdc). Узкие места — fill-rate + confirmation, НЕ монеты."
          % filled)


if __name__ == "__main__":
    main()
