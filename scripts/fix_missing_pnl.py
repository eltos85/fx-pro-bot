"""Разовый фикс: дозаполнить реальный PnL для закрытых позиций с pnl_usd=0.

Бот записывал 0 при закрытии, если Bybit closed-pnl API ещё не успевал
зафиксировать запись (1-5 сек лага). Теперь подтягиваем реальный closedPnl
через API и обновляем БД.

Запуск внутри контейнера:
  docker exec fx-pro-bot-bybit-bot-1 python scripts/fix_missing_pnl.py
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

from bybit_bot.trading.client import BybitClient

DB_PATH = "/data/bybit_stats.sqlite"
CUTOFF_ISO = "2026-04-16T00:00:00"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT id, symbol, side, qty, opened_at, closed_at, pnl_usd, close_reason "
        "FROM positions "
        "WHERE closed_at IS NOT NULL AND opened_at >= ? "
        "  AND (pnl_usd = 0 OR close_reason = 'sync_pending')",
        (CUTOFF_ISO,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    print(f"Найдено кандидатов на дозаполнение: {len(rows)}")
    if not rows:
        return

    client = BybitClient(
        api_key=os.environ["BYBIT_BOT_API_KEY"],
        api_secret=os.environ["BYBIT_BOT_API_SECRET"],
        demo=os.environ.get("BYBIT_BOT_DEMO", "true").lower() == "true",
    )

    updated = 0
    for row in rows:
        try:
            opened_dt = datetime.fromisoformat(row["opened_at"])
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=UTC)
            since_ms = int((opened_dt - timedelta(minutes=1)).timestamp() * 1000)
        except (ValueError, TypeError):
            continue

        records = client.get_closed_pnl(symbol=row["symbol"], limit=20, start_time=since_ms)
        if not records:
            print(f"  {row['symbol']} id={row['id']}: API пусто, пропуск")
            continue

        # Ищем запись ближайшую по времени к closed_at
        try:
            closed_dt = datetime.fromisoformat(row["closed_at"])
            if closed_dt.tzinfo is None:
                closed_dt = closed_dt.replace(tzinfo=UTC)
            closed_ms = int(closed_dt.timestamp() * 1000)
        except (ValueError, TypeError):
            closed_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

        best = min(
            records,
            key=lambda r: abs(int(r.get("updatedTime", 0)) - closed_ms),
        )
        real_pnl = float(best["closedPnl"])
        exit_price = float(best.get("avgExitPrice", 0))

        cur.execute(
            "UPDATE positions SET pnl_usd=?, exit_price=?, close_reason=? WHERE id=?",
            (real_pnl, exit_price, "sync_closed", row["id"]),
        )
        conn.commit()
        updated += 1
        print(
            f"  {row['symbol']} id={row['id']}: "
            f"{row['pnl_usd']:+.4f} -> {real_pnl:+.4f} (exit={exit_price:.4f})"
        )

    print(f"\nОбновлено записей: {updated}/{len(rows)}")
    conn.close()


if __name__ == "__main__":
    main()
