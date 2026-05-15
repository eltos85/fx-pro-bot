"""Backfill net realized PnL и avgExitPrice для закрытых позиций ai_arena.

Раньше `_apply_close` и `_reconcile_closed_positions` считали realized
PnL локально как `(exit-entry)*qty` (gross, без fees). Это расходилось
с Bybit `closedPnl` на ~`2 × taker_fee × notional` за сделку и сильно
ломало Telegram /status (наш «+$74» vs реальный Bybit «-$30»).

Скрипт исправляет уже сохранённые в БД 37+ закрытых записей:
1. Читает все закрытые позиции из `ai_arena.sqlite`
2. Группирует по symbol + временному окну (Bybit `get_closed_pnl`
   принимает окно ≤ 7 дней — берём с шагом по 7 дней)
3. Для каждой записи находит match в Bybit `get_closed_pnl` по
   symbol + closing_side + qty + close timestamp
4. Перезаписывает `realized_pnl_usd` и `exit_price` на net из биржи
5. Пересчитывает daily_pnl агрегаты на разницу

Запускать read-only-friendly (с `--dry-run`) внутри ai-arena контейнера:

    docker exec fx-pro-bot-ai-arena-1 python scripts/ai_arena_backfill_pnl.py --dry-run
    docker exec fx-pro-bot-ai-arena-1 python scripts/ai_arena_backfill_pnl.py

Bybit V5 docs: https://bybit-exchange.github.io/docs/v5/position/close-pnl
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

from ai_arena.config.settings import AiArenaSettings
from ai_arena.state.db import AiArenaStore
from ai_arena.trading.client import AiArenaBybitClient, ClosedPnlRecord

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("backfill")

WINDOW_DAYS = 7  # Bybit get_closed_pnl ограничение
QTY_MATCH_TOL = 1e-3  # 0.1% — допуск на rounding qty


def _iso_to_ms(iso: str) -> int:
    s = iso.replace("Z", "+00:00")
    return int(datetime.fromisoformat(s).timestamp() * 1000)


def _fetch_all_closed_pnl(
    client: AiArenaBybitClient, *, symbol: str, since_ms: int, until_ms: int
) -> list[ClosedPnlRecord]:
    """Дёргает get_closed_pnl с окном ≤7 дней, возвращает все записи.

    Не использует pagination cursor (для нашего объёма за окно <7 дней
    < 100 записей — за один call достаточно).
    """
    out: list[ClosedPnlRecord] = []
    cursor_start = since_ms
    while cursor_start < until_ms:
        cursor_end = min(cursor_start + WINDOW_DAYS * 24 * 60 * 60 * 1000, until_ms)
        recs = client.get_closed_pnl(
            symbol=symbol,
            start_time_ms=cursor_start,
            end_time_ms=cursor_end,
            limit=100,
        )
        if recs is None:
            log.warning(
                "get_closed_pnl=None for %s window [%d..%d] — пропускаем",
                symbol, cursor_start, cursor_end,
            )
        else:
            log.info(
                "  fetched %d closed_pnl records for %s window %s..%s",
                len(recs),
                symbol,
                datetime.fromtimestamp(cursor_start / 1000, tz=UTC).isoformat(),
                datetime.fromtimestamp(cursor_end / 1000, tz=UTC).isoformat(),
            )
            out.extend(recs)
        cursor_start = cursor_end
    return out


def _match_record(
    db_pos, records: list[ClosedPnlRecord]
) -> ClosedPnlRecord | None:
    """Находит подходящий closed_pnl для конкретной DB-позиции.

    Критерии: symbol, closing_side (opposite от opened_side), qty
    (±0.1%), updated_time близок к closed_at (±2ч на случай сильного
    шума timestamp-ов).
    """
    closing_side = "Sell" if db_pos.side == "Buy" else "Buy"
    closed_at_ms = _iso_to_ms(db_pos.closed_at) if db_pos.closed_at else 0
    candidates: list[tuple[int, ClosedPnlRecord]] = []
    for r in records:
        if r.symbol != db_pos.symbol:
            continue
        if r.side != closing_side:
            continue
        if abs(r.qty - db_pos.qty) > max(db_pos.qty * QTY_MATCH_TOL, 1e-8):
            continue
        delta = abs(r.updated_time_ms - closed_at_ms)
        if delta > 2 * 60 * 60 * 1000:  # > 2 часа — вряд ли тот же close
            continue
        candidates.append((delta, r))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Не записывать в БД, только показать что будет изменено",
    )
    args = parser.parse_args()

    settings = AiArenaSettings()
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        log.error("AI_ARENA_BYBIT_API_KEY/SECRET не заданы")
        return 1

    store = AiArenaStore(settings.db_path)
    client = AiArenaBybitClient(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        demo=settings.bybit_demo,
        category=settings.bybit_category,
    )

    # Все закрытые позиции из БД
    with store._conn() as c:  # noqa: SLF001
        rows = c.execute(
            "SELECT * FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at"
        ).fetchall()
    log.info("Найдено %d закрытых позиций в ai_arena.sqlite", len(rows))
    if not rows:
        return 0

    # Окно для get_closed_pnl: от первого opened_at до now
    earliest_opened = min(_iso_to_ms(r["opened_at"]) for r in rows)
    latest_closed = max(_iso_to_ms(r["closed_at"]) for r in rows if r["closed_at"])
    until_ms = latest_closed + 60 * 60 * 1000  # +1ч buffer
    since_ms = earliest_opened - 60 * 1000  # -1мин buffer

    # Группируем по symbol для оптимизации API-вызовов
    symbols = sorted({r["symbol"] for r in rows})
    log.info("Символы: %s", ", ".join(symbols))
    pnl_index: dict[str, list[ClosedPnlRecord]] = {}
    for sym in symbols:
        log.info("Fetching closed_pnl for %s...", sym)
        pnl_index[sym] = _fetch_all_closed_pnl(
            client, symbol=sym, since_ms=since_ms, until_ms=until_ms
        )

    matched = 0
    unmatched: list[dict] = []
    total_delta = 0.0
    from ai_arena.state.db import ArenaPosition

    for r in rows:
        db_pos = ArenaPosition(**dict(r))
        rec = _match_record(db_pos, pnl_index.get(db_pos.symbol, []))
        if not rec:
            unmatched.append(dict(r))
            continue
        old_pnl = float(db_pos.realized_pnl_usd or 0.0)
        delta = rec.closed_pnl - old_pnl
        log.info(
            "  id=%d %s %s qty=%s: old=%+.4f → new=%+.4f (Δ %+.4f)  exit %s → %s",
            db_pos.id, db_pos.side, db_pos.symbol, db_pos.qty,
            old_pnl, rec.closed_pnl, delta,
            f"{db_pos.exit_price:.6g}" if db_pos.exit_price else "n/a",
            f"{rec.avg_exit_price:.6g}",
        )
        total_delta += delta
        matched += 1
        if not args.dry_run:
            store.update_position_realized(
                db_pos.id,
                exit_price=rec.avg_exit_price,
                realized_pnl_usd=rec.closed_pnl,
            )

    log.info("=" * 60)
    log.info("Matched: %d/%d", matched, len(rows))
    log.info("Unmatched: %d", len(unmatched))
    log.info("Total PnL delta: %+.4f USD (new total - old total)", total_delta)
    if unmatched:
        log.warning("Unmatched позиции (требуют ручной проверки):")
        for r in unmatched:
            log.warning(
                "  id=%d %s %s qty=%s opened=%s closed=%s",
                r["id"], r["side"], r["symbol"], r["qty"],
                r["opened_at"], r["closed_at"],
            )
    if args.dry_run:
        log.info("DRY-RUN: БД не изменена. Запустите без --dry-run для применения.")
    else:
        log.info("БД обновлена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
