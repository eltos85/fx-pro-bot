"""Реконсилиация advisor_stats.sqlite ↔ cTrader Open API.

Сверяет каждую закрытую позицию в БД с реальной сделкой (deal) у брокера
по `broker_position_id ↔ positionId`. Источник истины — API. БД может
расходиться из-за бага в `monitor.py`: `profit_pips` сохраняется на
момент выдачи команды на закрытие, а реальный fill у брокера происходит
позже на другой цене. Особенно проявляется на `scalp_trail` (видно по
DID331537907 30.04.2026: API +$12.40 = +62 pips, БД +33.1 pips).

Режимы:
    --dry-run  (по умолчанию) — только показать расхождения, ничего не пишем.
    --apply    — обновить БД (с бекапом). Поля `profit_pips` и
                 `current_price` пересчитываются из API. Остальные поля
                 (entry_price, status, exit_reason) НЕ трогаем.

Запуск (внутри контейнера на VPS, чтобы был доступ к токенам):
    docker exec fx-pro-bot-advisor-1 python3 -m scripts.reconcile_db_vs_api \\
        --since 2026-04-07 --dry-run

После согласия:
    docker exec fx-pro-bot-advisor-1 python3 -m scripts.reconcile_db_vs_api \\
        --since 2026-04-07 --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fx_pro_bot.config.settings import Settings, pip_size, pip_value_from_volume
from fx_pro_bot.trading.auth import TokenStore, ensure_valid_token
from fx_pro_bot.trading.client import CTraderClient
from fx_pro_bot.trading.executor import TradeExecutor
from fx_pro_bot.trading.symbols import SymbolCache


log = logging.getLogger("reconcile_db_vs_api")


@dataclass
class Mismatch:
    db_id: str
    broker_pos_id: int
    deal_id: int
    instrument: str
    direction: str
    strategy: str
    exit_reason: str
    entry_price: float
    db_pips: float
    db_current_price: float
    api_gross_usd: float
    api_pips: float
    api_close_price: float
    api_volume: int
    closed_at: str


def _api_pips_from_gross(symbol: str, volume: int, gross_usd: float) -> float:
    pv = pip_value_from_volume(symbol, volume)
    if pv <= 0:
        return 0.0
    return gross_usd / pv


def _api_close_price(direction: str, entry_price: float, volume: int,
                     gross_usd: float, symbol: str) -> float:
    """Восстанавливает реальную close-цену из gross$ + entry + volume.

    Для XAUUSD: gross = (close-entry)*units (long), units = vol/100.
    Для FX майоров: gross = (close-entry)*units*quote_to_usd, ниже
    приближение через pip_value (достаточно для отчёта).
    """
    units = volume / 100.0
    if units <= 0:
        return entry_price
    ps = pip_size(symbol)
    pv_per_unit = pip_value_from_volume(symbol, 100) / 1.0  # $ за 1 unit за 1 pip
    if ps <= 0 or pv_per_unit <= 0:
        return entry_price
    price_delta_per_dollar = ps / pv_per_unit / units
    if direction == "long":
        return entry_price + gross_usd * price_delta_per_dollar
    return entry_price - gross_usd * price_delta_per_dollar


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="2026-04-07")
    p.add_argument("--db", default="/data/advisor_stats.sqlite")
    p.add_argument("--apply", action="store_true",
                   help="Применить исправления (БД будет изменена)")
    p.add_argument("--tolerance-pct", type=float, default=2.0,
                   help="Допустимое расхождение pips в %% (по умолчанию 2%%)")
    p.add_argument("--tolerance-pips", type=float, default=1.0,
                   help="Минимальное расхождение в pips для считания (защита от шума)")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = Settings()
    token_store = TokenStore(settings.ctrader_token_path)
    token_data = ensure_valid_token(
        token_store, settings.ctrader_client_id, settings.ctrader_client_secret,
    )
    client = CTraderClient(
        client_id=settings.ctrader_client_id,
        client_secret=settings.ctrader_client_secret,
        access_token=token_data.access_token,
        account_id=settings.ctrader_account_id,
        host_type=settings.ctrader_host_type,
        refresh_token=token_data.refresh_token,
    )
    client.start(timeout=30)
    sc = SymbolCache()
    executor = TradeExecutor(client=client, symbols=sc, lot_size=1.0)
    executor.load_symbols()

    since_dt = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    now = datetime.now(UTC)
    log.info("Период: %s → %s", since_dt.isoformat(), now.isoformat())

    # cTrader API ограничивает диапазон одного запроса. Разбиваем на окна по 7 дней.
    deals: list[dict] = []
    win = since_dt
    step = timedelta(days=7)
    while win < now:
        win_end = min(win + step, now)
        chunk = executor.get_deal_list(
            int(win.timestamp() * 1000), int(win_end.timestamp() * 1000),
        )
        deals.extend(chunk)
        log.info(
            "  deals %s → %s: %d closing deals (всего %d)",
            win.date(), win_end.date(), len(chunk), len(deals),
        )
        win = win_end
    client.stop()

    deal_by_pos: dict[int, dict] = {}
    for d in deals:
        deal_by_pos[d["positionId"]] = d

    log.info("Уникальных positionId с closing-deal: %d", len(deal_by_pos))

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT id, broker_position_id, broker_volume, created_at, closed_at,
                  strategy, instrument, direction, profit_pips, exit_reason,
                  entry_price, current_price
           FROM positions
           WHERE status='closed' AND closed_at >= ?
             AND broker_position_id IS NOT NULL
           ORDER BY closed_at""",
        (since_dt.isoformat(),),
    ).fetchall()
    log.info("Закрытых позиций в БД с broker_position_id: %d", len(rows))

    mismatches: list[Mismatch] = []
    no_api_match = 0

    for r in rows:
        try:
            broker_id = int(r["broker_position_id"])
        except (TypeError, ValueError):
            continue
        deal = deal_by_pos.get(broker_id)
        if deal is None:
            no_api_match += 1
            continue

        symbol = r["instrument"]
        # cTrader symbol vs БД (XAUUSD vs GC=F): подбираем по volume.
        vol = int(deal["volume"])
        gross = float(deal["grossProfit"])
        api_pips = _api_pips_from_gross(symbol, vol, gross)
        api_close = _api_close_price(
            r["direction"], float(r["entry_price"]), vol, gross, symbol,
        )

        db_pips = float(r["profit_pips"] or 0.0)
        diff_pips = api_pips - db_pips
        denom = max(abs(api_pips), abs(db_pips), 0.5)
        diff_pct = abs(diff_pips) / denom * 100

        if abs(diff_pips) < args.tolerance_pips:
            continue
        if diff_pct < args.tolerance_pct:
            continue

        mismatches.append(Mismatch(
            db_id=r["id"], broker_pos_id=broker_id, deal_id=int(deal["dealId"]),
            instrument=symbol, direction=r["direction"], strategy=r["strategy"],
            exit_reason=r["exit_reason"] or "?",
            entry_price=float(r["entry_price"]),
            db_pips=db_pips,
            db_current_price=float(r["current_price"] or 0.0),
            api_gross_usd=gross,
            api_pips=api_pips,
            api_close_price=api_close,
            api_volume=vol,
            closed_at=r["closed_at"],
        ))

    print()
    print("=" * 130)
    print(" РЕКОНСИЛИАЦИЯ DB ↔ cTrader API")
    print("=" * 130)
    print(f" Период: {since_dt.date()} → {now.date()}")
    print(f" БД closed: {len(rows)}  | API deals: {len(deals)} | без пары в API: {no_api_match}")
    print(f" Расхождений (>{args.tolerance_pips}p и >{args.tolerance_pct}%): {len(mismatches)}")
    print()
    if mismatches:
        print(f" {'closed_at':<19}  {'strat':<10} {'exit':<14} {'dir':<5} "
              f"{'DB pips':>9}  {'API pips':>9}  {'Δ':>7}  {'API gross$':>10}  vol  pos_id")
        print(" " + "─" * 120)
        by_exit: dict[str, list[Mismatch]] = {}
        for m in mismatches:
            by_exit.setdefault(m.exit_reason, []).append(m)
            print(
                f" {m.closed_at[:19]:<19}  {m.strategy:<10} {m.exit_reason:<14} "
                f"{m.direction:<5} {m.db_pips:>+9.1f}  {m.api_pips:>+9.1f}  "
                f"{m.api_pips - m.db_pips:>+7.1f}  ${m.api_gross_usd:>+9.2f}  "
                f"{m.api_volume:>5}  {m.broker_pos_id}"
            )
        print()
        print(" Сводка по exit_reason:")
        for reason, lst in sorted(by_exit.items(), key=lambda x: -len(x[1])):
            db_sum = sum(m.db_pips for m in lst)
            api_sum = sum(m.api_pips for m in lst)
            print(
                f"   {reason:<14}  n={len(lst):>3}  "
                f"DB ΣP&L={db_sum:>+8.1f}p  API ΣP&L={api_sum:>+8.1f}p  "
                f"Δ={api_sum - db_sum:>+8.1f}p"
            )

    if args.apply and mismatches:
        # Backup БД.
        backup_path = f"{args.db}.backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        shutil.copy2(args.db, backup_path)
        log.info("Backup БД: %s", backup_path)

        cur = db.cursor()
        for m in mismatches:
            cur.execute(
                "UPDATE positions SET profit_pips=?, current_price=? WHERE id=?",
                (round(m.api_pips, 2), round(m.api_close_price, 5), m.db_id),
            )
        db.commit()
        log.info("Обновлено строк: %d", len(mismatches))
        print()
        print(f" *** APPLIED: {len(mismatches)} строк обновлено в {args.db}")
        print(f" *** Backup сохранён: {backup_path}")
    elif mismatches and not args.apply:
        print()
        print(" *** DRY-RUN: ничего не изменено. Запустите с --apply чтобы применить.")
    else:
        print(" Расхождений нет, БД и API сходятся.")

    print("=" * 130)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
