"""One-shot backfill: пересчитать `realized_pnl_usd` в fx_ai_trader.sqlite
из broker NET (= gross + swap + commission) через cTrader API.

Зачем: до 2026-05-20 БД хранила idealized gross PnL (`_calc_pnl_usd` на
основе entry/exit/pip_value). Broker реально списывает NET (gross +
swap + commission). Разница — комиссии и overnight swap, в среднем
−$1.5/trade в наших данных (см. BUILDLOG_AI_FX_TRADER.md 2026-05-20
«broker-truth audit»).

Что делает:
1. Берёт все closed positions с `broker_position_id IS NOT NULL` и
   `is_paper = 0`.
2. Для каждой запрашивает closing deal через
   ``ProtoOAGetDealListReq`` (через `get_closing_deal_for_position`).
3. Считает `broker_net = gross + swap + commission`.
4. Сравнивает с текущим `realized_pnl_usd`, если delta > $0.01 —
   UPDATE'ит запись.

Безопасно: только UPDATE на closed positions, не трогает open. Перед
UPDATE'ом печатает diff для каждой записи и просит подтверждение.

Запуск (внутри fx-ai-trader контейнера):

    docker exec -it fx-pro-bot-fx-ai-trader-1 \\
        python /tmp/backfill_net_pnl.py [--apply]

Без `--apply` — dry-run, только показывает разницу.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

DB_PATH = "/data/fx_ai_trader.sqlite"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true",
        help="Реально применить UPDATE'ы. Без флага — только dry-run.",
    )
    parser.add_argument(
        "--lookback-hours", type=int, default=720,
        help="Окно поиска closing deal на брокере (по умолчанию 30 дней).",
    )
    args = parser.parse_args()

    # Локальные closed positions
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, symbol, side, volume_lots, broker_position_id,
               opened_at, closed_at, realized_pnl_usd, close_reason
        FROM positions
        WHERE closed_at IS NOT NULL
          AND broker_position_id IS NOT NULL
          AND broker_position_id > 0
          AND is_paper = 0
        ORDER BY opened_at ASC
        """
    ).fetchall()
    if not rows:
        print("Нет closed live-позиций для backfill.")
        return 0

    # cTrader adapter
    from fx_ai_trader.config.settings import AiFxTraderSettings
    from fx_ai_trader.trading.client_adapter import CTraderFxAdapter

    settings = AiFxTraderSettings()
    adapter = CTraderFxAdapter(settings)
    adapter.start(timeout=30.0)
    if not adapter.is_ready:
        print("ERROR: adapter not ready", file=sys.stderr)
        return 1

    print(f"=== Backfill candidates: {len(rows)} closed live positions ===")
    print(f"=== Lookback window: {args.lookback_hours}h ===")
    print(f"=== Mode: {'APPLY (will UPDATE)' if args.apply else 'DRY-RUN'} ===\n")

    print(f"{'id':<4} {'symbol':<7} {'side':<5} {'opened':<11} "
          f"{'closed':<11} {'db_pnl':>9} {'broker_net':>11} {'delta':>9} action")
    print("─" * 100)

    updates = []
    misses = []
    for row in rows:
        deal = adapter.get_closing_deal_for_position(
            int(row["broker_position_id"]), lookback_hours=args.lookback_hours,
        )
        if deal is None:
            misses.append(row["id"])
            print(
                f"{row['id']:<4} {row['symbol']:<7} {row['side']:<5} "
                f"{row['opened_at'][:16]:<11} {row['closed_at'][:16]:<11} "
                f"{row['realized_pnl_usd']:+9.2f} {'(no deal)':>11} {'  -':>9} "
                f"SKIP (deal not found)"
            )
            continue
        broker_net = (
            deal["gross_pnl_usd"]
            + deal["swap_usd"]
            + deal["commission_usd"]
        )
        delta = broker_net - (row["realized_pnl_usd"] or 0.0)
        if abs(delta) < 0.01:
            print(
                f"{row['id']:<4} {row['symbol']:<7} {row['side']:<5} "
                f"{row['opened_at'][:16]:<11} {row['closed_at'][:16]:<11} "
                f"{row['realized_pnl_usd']:+9.2f} {broker_net:+11.2f} {delta:+9.4f} "
                f"OK (matches)"
            )
            continue
        print(
            f"{row['id']:<4} {row['symbol']:<7} {row['side']:<5} "
            f"{row['opened_at'][:16]:<11} {row['closed_at'][:16]:<11} "
            f"{row['realized_pnl_usd']:+9.2f} {broker_net:+11.2f} {delta:+9.4f} "
            f"UPDATE"
        )
        updates.append((broker_net, row["id"]))
        # Не спамим API
        time.sleep(0.3)

    print()
    print(f"=== Summary: {len(updates)} updates needed, {len(misses)} deal-not-found ===")

    if args.apply and updates:
        # daily_pnl нужно тоже скорректировать — но это сложно (зависит от
        # дня закрытия). Для простоты пересчитываем daily_pnl с нуля по
        # всем positions после UPDATE.
        for new_pnl, pid in updates:
            conn.execute(
                "UPDATE positions SET realized_pnl_usd = ? WHERE id = ?",
                (new_pnl, pid),
            )
        # Пересоберём daily_pnl с нуля из positions
        conn.execute("DELETE FROM daily_pnl")
        conn.execute(
            """
            INSERT INTO daily_pnl (day, realized_pnl_usd, n_trades, n_wins)
            SELECT
                substr(closed_at, 1, 10) AS day,
                SUM(realized_pnl_usd) AS realized_pnl_usd,
                COUNT(*) AS n_trades,
                SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS n_wins
            FROM positions
            WHERE closed_at IS NOT NULL
            GROUP BY substr(closed_at, 1, 10)
            """
        )
        conn.commit()
        print(f"✓ Applied {len(updates)} UPDATE'ов + пересобрана daily_pnl.")
    elif updates:
        print("(dry-run — use --apply to commit)")

    conn.close()
    adapter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
