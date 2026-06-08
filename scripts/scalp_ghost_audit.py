"""Аудит «призраков» scalp_bot: что scalp открыл на бирже vs что записал в БД.

ВАЖНО: сабаккаунт Bybit ОБЩИЙ (один ключ у scalp_bot и bybit_bot — проверено
9amkig…). Поэтому get_closed_pnl возвращает сделки ОБОИХ ботов; сравнивать его
целиком с scalp-БД нельзя (правило stats-collection.mdc: общий сабаккаунт
сплитим по префиксу orderLinkId).

Идея пользователя (БД vs биржа, сортировка, diff) реализована ПРАВИЛЬНО через
историю ОРДЕРОВ, а не closed-PnL:

  scalp метит каждый вход orderLinkId = "scalp_{symbol}_{ms}". Берём с биржи ВСЕ
  ордера за период, оставляем только filled ВХОДЫ с префиксом "scalp_"
  (reduceOnly=False, cumExecQty>0) — это полный список позиций, которые scalp
  реально открыл на бирже. Сверяем с DB.trades.entry_order_id:

  • GHOST   — filled scalp-вход есть на бирже, но НЕТ строки в БД (бот не
              записал позицию → потерянный PnL/учёт). Это и есть дыра.
  • DB-ONLY — в БД есть scalp-вход, которого нет в filled-истории биржи (вход
              не исполнился / отменён — норм если close_reason=entry_*).

Bybit order history: endTime−startTime ≤ 7д → бьём период на окна по 7д.
https://bybit-exchange.github.io/docs/v5/order/order-list
Ничего не пишем — только показываем.

Запуск на VPS внутри scalp-контейнера (ключи в env, БД в /data):
    docker exec fx-pro-bot-scalp-bot-1 python3 -m scripts.scalp_ghost_audit \\
        --since 2026-05-06
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import UTC, datetime

from scalp_bot.config.settings import ScalpSettings
from scalp_bot.trading.client import ScalpBybitClient, _as_float

_PREFIX = "scalp_"
_WIN_MS = (7 * 24 * 3600 - 3600) * 1000


def _fetch_db_entries(db_path: str) -> dict[str, dict]:
    """Все scalp-сделки из БД по entry_order_id (orderLinkId входа)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT id, symbol, side, qty, entry, status, close_reason,
                  pnl_usd, ts_open, mode, entry_order_id
           FROM trades
           WHERE entry_order_id LIKE 'scalp_%'""",
    ).fetchall()
    con.close()
    return {r["entry_order_id"]: dict(r) for r in rows}


def _fetch_bybit_orders(client: ScalpBybitClient, since_ms: int,
                        until_ms: int) -> list[dict]:
    """Вся история ордеров с биржи за период (пагинация + окна по 7д)."""
    out: list[dict] = []
    start = since_ms
    while start < until_ms:
        end = min(start + _WIN_MS, until_ms)
        cursor = None
        for _ in range(100):
            params = {"category": "linear", "limit": 50,
                      "startTime": int(start), "endTime": int(end)}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = client._session.get_order_history(**params)
            except Exception as e:  # noqa: BLE001
                print(f"  ! get_order_history {start}-{end} failed: {e}")
                break
            lst = resp.get("result", {}).get("list", []) or []
            out += lst
            cursor = resp.get("result", {}).get("nextPageCursor")
            if not cursor or not lst:
                break
        start = end
    return out


def _is_filled_scalp_entry(o: dict) -> bool:
    link = o.get("orderLinkId", "") or ""
    if not link.startswith(_PREFIX):
        return False
    # reduceOnly=True → это выход/закрытие, не открытие позиции
    if o.get("reduceOnly") in (True, "true", "True"):
        return False
    return (_as_float(o.get("cumExecQty")) or 0.0) > 0.0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="2026-05-06",
                   help="дата начала периода (YYYY-MM-DD, UTC)")
    p.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = p.parse_args()

    since_ms = int(datetime.fromisoformat(args.since)
                   .replace(tzinfo=UTC).timestamp() * 1000)
    until_ms = int(time.time() * 1000)

    cfg = ScalpSettings()
    if not cfg.bybit_api_key or not cfg.bybit_api_secret:
        print("! Нет SCALP_BYBIT_API_KEY/SECRET — запускай внутри scalp-контейнера")
        return 2
    client = ScalpBybitClient(cfg.bybit_api_key, cfg.bybit_api_secret,
                              demo=cfg.bybit_demo, category=cfg.bybit_category)

    db = _fetch_db_entries(args.db)
    orders = _fetch_bybit_orders(client, since_ms, until_ms)

    # filled scalp-входы на бирже, дедуп по orderLinkId (берём последнее состояние)
    ex_entries: dict[str, dict] = {}
    scalp_total = 0
    for o in orders:
        if (o.get("orderLinkId", "") or "").startswith(_PREFIX):
            scalp_total += 1
        if _is_filled_scalp_entry(o):
            ex_entries[o["orderLinkId"]] = o

    ghosts = {lk: o for lk, o in ex_entries.items() if lk not in db}
    db_only = {lk: r for lk, r in db.items()
               if lk not in ex_entries and (r["ts_open"] or 0) * 1000 >= since_ms}

    w = 116
    print("=" * w)
    print(" SCALP GHOST AUDIT — биржа (scalp_-входы) vs БД")
    print("=" * w)
    print(f" Период: {args.since} → now (UTC)   demo={cfg.bybit_demo}")
    print(f" Ордеров с биржи всего: {len(orders)}  | с префиксом scalp_: {scalp_total}")
    print(f" Filled scalp-входов на бирже: {len(ex_entries)}")
    print(f" scalp-сделок в БД (entry_order_id LIKE scalp_%): {len(db)}")
    print(f" GHOST (на бирже, НЕТ в БД): {len(ghosts)}"
          f"   |  DB-ONLY (в БД, не filled на бирже): {len(db_only)}")

    if ghosts:
        print("\n" + "-" * w)
        print(" GHOST — scalp открыл позицию на бирже, но НЕ записал в БД (ДЫРА):")
        print(f" {'created(UTC)':<20} {'symbol':<12} {'side':<5} {'avgPrice':>12} "
              f"{'qty':>12}  orderLinkId")
        for lk, o in sorted(ghosts.items(),
                            key=lambda kv: int(kv[1].get("createdTime", 0) or 0)):
            ts = int(o.get("createdTime", 0) or 0) / 1000
            t = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
            print(f" {t:<20} {o.get('symbol',''):<12} {o.get('side',''):<5} "
                  f"{_as_float(o.get('avgPrice')) or 0:>12.6f} "
                  f"{_as_float(o.get('cumExecQty')) or 0:>12.4f}  {lk}")

    if db_only:
        print("\n" + "-" * w)
        print(" DB-ONLY — вход в БД, но НЕ filled на бирже (норм если reason=entry_*):")
        print(f" {'open(UTC)':<20} {'#id':<7} {'symbol':<12} {'side':<5} "
              f"{'status':<8} reason")
        bad = 0
        for lk, r in sorted(db_only.items(), key=lambda kv: kv[1]["ts_open"] or 0):
            ts = r["ts_open"] or 0
            t = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
            reason = r["close_reason"] or "-"
            if not str(reason).startswith("entry_") and reason != "restart_flat":
                bad += 1
            print(f" {t:<20} {r['id']:<7} {r['symbol']:<12} {r['side']:<5} "
                  f"{r['status']:<8} {reason}")
        if bad:
            print(f"   ⚠ из них {bad} с НЕ-entry причиной — подозрительно "
                  f"(записаны как реальные, но входа на бирже нет)")

    if not ghosts and not db_only:
        print("\n Дыр нет: каждый scalp_-вход с биржи есть в БД и наоборот.")
    print("=" * w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
