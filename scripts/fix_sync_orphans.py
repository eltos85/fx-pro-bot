"""Разовый фикс: восстановить реальный PnL для позиций с close_reason='sync_orphan'.

Проблема:
- Bybit иногда возвращает неполный список get_positions() (race condition).
- Бот помечал живые позиции sync_pending → sync_orphan с pnl_usd=0.
- На бирже позиция продолжала жить до SL/TP, но бот терял над ней контроль
  (time-stop 24ч не работал).

Фикс в main.py: API race-guard с повторным запросом (уже в коде).
Этот скрипт — РАЗОВАЯ прошивка уже пострадавших записей в /data/bybit_stats.sqlite.

Источник истины — /ab-data/ab_snapshots.sqlite (closed_trades, собранные из
Bybit closedPnl API в scripts/ab_test_snapshot.py). Матчим по:
- symbol
- инвертированный side (Buy в positions → Sell в closed_trades)
- qty ±5%
- entry_price ±0.5%
- closedPnl.updatedTime > positions.opened_at

Если найден match → обновляем:
- pnl_usd = closedPnl (реальный NET)
- exit_price = avg_exit_price
- closed_at = updatedTime (реальное время закрытия на бирже)
- close_reason = 'sync_orphan_recovered'

Запуск на VPS:
  docker exec fx-pro-bot-bybit-bot-1 python3 -m scripts.fix_sync_orphans

Идемпотентно: уже восстановленные записи (close_reason='sync_orphan_recovered')
пропускаются.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

BOT_DB = Path("/data/bybit_stats.sqlite")
AB_DB = Path("/ab-data/ab_snapshots.sqlite")

QTY_REL_TOL = 0.05  # 5%
ENTRY_PRICE_REL_TOL = 0.005  # 0.5%

log = logging.getLogger(__name__)


def _iso_to_ms(iso_str: str) -> int:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def find_match(ab_conn: sqlite3.Connection, orphan: sqlite3.Row) -> sqlite3.Row | None:
    """Fuzzy-match orphan-позиции против closed_trades в ab_snapshots.sqlite."""
    opened_ms = _iso_to_ms(orphan["opened_at"])
    inverted_side = "Sell" if orphan["side"] == "Buy" else "Buy"
    entry = float(orphan["entry_price"])
    qty = float(orphan["qty"])
    if entry <= 0 or qty <= 0:
        return None

    row = ab_conn.execute(
        """
        SELECT order_id, symbol, side, qty, avg_entry_price, avg_exit_price,
               closed_pnl, updated_time_ms
        FROM closed_trades
        WHERE symbol = ?
          AND side = ?
          AND ABS(CAST(qty AS REAL) - ?) <= ? * ?
          AND ABS(CAST(avg_entry_price AS REAL) - ?) <= ? * ?
          AND updated_time_ms >= ?
        ORDER BY ABS(updated_time_ms - ?) ASC
        LIMIT 1
        """,
        (
            orphan["symbol"],
            inverted_side,
            qty, QTY_REL_TOL, qty,
            entry, ENTRY_PRICE_REL_TOL, entry,
            opened_ms,
            opened_ms,
        ),
    ).fetchone()
    return row


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if not BOT_DB.exists():
        log.error("Bot DB не найден: %s", BOT_DB)
        return
    if not AB_DB.exists():
        log.error("AB DB не найден: %s — сначала запустите ab_test_snapshot", AB_DB)
        return

    bot_conn = sqlite3.connect(str(BOT_DB))
    bot_conn.row_factory = sqlite3.Row
    ab_conn = sqlite3.connect(str(AB_DB))
    ab_conn.row_factory = sqlite3.Row

    orphans = bot_conn.execute(
        "SELECT id, symbol, side, qty, entry_price, strategy, opened_at, closed_at, "
        "       pnl_usd, close_reason "
        "FROM positions "
        "WHERE close_reason = 'sync_orphan' "
        "  AND (pnl_usd IS NULL OR pnl_usd = 0.0)"
    ).fetchall()

    log.info("Найдено orphan-позиций с pnl=0: %d", len(orphans))
    if not orphans:
        log.info("Нечего чинить.")
        return

    updated = 0
    unmatched = 0
    for o in orphans:
        match = find_match(ab_conn, o)
        if match is None:
            unmatched += 1
            log.warning(
                "  NO MATCH: id=%s %s %s qty=%s entry=%.6f opened=%s",
                o["id"], o["symbol"], o["side"], o["qty"], o["entry_price"], o["opened_at"],
            )
            continue

        real_pnl = float(match["closed_pnl"])
        exit_price = float(match["avg_exit_price"] or 0)
        real_closed_at = _ms_to_iso(match["updated_time_ms"])

        bot_conn.execute(
            "UPDATE positions "
            "SET pnl_usd = ?, exit_price = ?, closed_at = ?, close_reason = ? "
            "WHERE id = ?",
            (real_pnl, exit_price, real_closed_at, "sync_orphan_recovered", o["id"]),
        )
        updated += 1
        log.info(
            "  id=%s %s %s qty=%s: pnl %.4f -> %.4f, closed_at %s -> %s",
            o["id"], o["symbol"], o["side"], o["qty"],
            o["pnl_usd"] or 0.0, real_pnl,
            o["closed_at"], real_closed_at,
        )

    bot_conn.commit()
    log.info("Готово: восстановлено %d из %d (не найдено %d)", updated, len(orphans), unmatched)

    bot_conn.close()
    ab_conn.close()


if __name__ == "__main__":
    main()
