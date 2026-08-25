#!/usr/bin/env python3
"""Пометить фантомные сделки как технические — но только доказанные биржей.

Фантом: до `c9effac` (24.08) наш вход против чужой стороны на общем one-way
счёте не открывал позицию, а срезал чужую (офдок: один лот на символ,
https://bybit-exchange.github.io/docs/v5/position/position-mode). Позиции не
существовало, значит не существовало и результата — но строка в БД жила,
«сопровождалась» и в итоге закрывалась по mark-price. #4325 так набрал
выдуманные −$289.18 (−28.5R, 30% всего минуса 20-дневной выборки), тогда как на
бирже в ту же секунду закрылся ЧУЖОЙ лонг 1.45 ETH @ 2280.50 с +$21.96.

Скрипт НЕ верит списку id на слово: перед записью он заново снимает отпечаток с
биржи и переписывает только те строки, где отпечаток совпал. Причина в
`no-data-fitting.mdc`: чистка истории — это изменение выборки, на которой потом
принимаются решения по стратегиям, поэтому она должна опираться на артефакт, а
не на мою уверенность.

Отпечаток (тот же, что в части B1 `scalp_phantom_and_stale_bracket_audit.py`):
в close-pnl есть запись, где сторона ЗАКРЫВАЮЩЕГО ордера равна стороне нашего
входа, объём равен нашему, цена выхода равна цене нашего входа, а время
совпадает с нашим входом. Семантика `side` = сторона закрывающего ордера видна
из примера в офдоке close-pnl (side=Sell при avgEntryPrice > avgExitPrice и
отрицательном closedPnl — то есть закрывали ЛОНГ).

Ограничение: `get_closed_pnl` отдаёт максимум 7 суток («endTime − startTime <=
7 days»), поэтому доказать фантом старше недели нечем. Такие строки скрипт
оставляет как есть и печатает отдельно.

Что делает с доказанной строкой: `close_reason='entry_netted'` (префикс
`entry_` выводит её из всех статистических выборок), `pnl_usd=0`, `fees_usd=0`.
Сама строка не удаляется — история подозрительных входов нужна для аудита.

Запуск (сначала всегда без `--apply` — покажет что сделает и ничего не тронет):

    docker exec -i fx-pro-bot-scalp-bot-1 python3 - /data/scalp_bot.sqlite 4325 \\
        < scripts/scalp_relabel_netting_phantoms.py
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import UTC, datetime

_PRICE_TOL = 5e-4
_QTY_TOL = 0.02
_MATCH_WINDOW_SEC = 90.0
_API_WINDOW_SEC = 6.5 * 86400          # запас к лимиту окна 7 суток


def _ts(v: float) -> str:
    return datetime.fromtimestamp(v, UTC).strftime("%m-%d %H:%M:%S")


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) / max(abs(a), abs(b), 1e-12) <= tol


def _closed_pnl(symbol: str, since_ms: int, until_ms: int) -> list[dict]:
    """Записи close-pnl с обязательной пагинацией (stats-collection.mdc)."""
    from pybit.unified_trading import HTTP
    sess = HTTP(api_key=os.environ["SCALP_BYBIT_API_KEY"],
                api_secret=os.environ["SCALP_BYBIT_API_SECRET"],
                demo=os.environ.get("SCALP_BYBIT_DEMO", "true").lower() != "false",
                recv_window=10000)
    recs: list[dict] = []
    cursor: str | None = None
    while True:
        kw = dict(category="linear", symbol=symbol, startTime=since_ms,
                  endTime=until_ms, limit=100)
        if cursor:
            kw["cursor"] = cursor
        res = sess.get_closed_pnl(**kw)["result"]
        recs += res.get("list", []) or []
        cursor = res.get("nextPageCursor")
        if not cursor:
            break
    return recs


def _proof(row: sqlite3.Row, recs: list[dict]) -> dict | None:
    """Запись close-pnl, доказывающая что наш вход срезал ЧУЖУЮ позицию."""
    our_close_side = "Buy" if row["side"] == "long" else "Sell"
    for x in recs:
        if x.get("side") != our_close_side:
            continue
        if abs(int(x["updatedTime"]) / 1000.0 - row["ts_open"]) > _MATCH_WINDOW_SEC:
            continue
        if not _close(float(x["closedSize"]), row["qty"], _QTY_TOL):
            continue
        if not _close(float(x["avgExitPrice"]), row["entry"], _PRICE_TOL):
            continue
        return x
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("ids", nargs="+", type=int, help="id сделок-кандидатов")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (без флага — только показать)")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    now = time.time()
    horizon = now - _API_WINDOW_SEC

    ph = ",".join("?" for _ in args.ids)
    rows = db.execute(
        f"""SELECT id, ts_open, ts_close, symbol, side, qty, entry, exit,
                   pnl_usd, fees_usd, close_reason, strategy
            FROM trades WHERE id IN ({ph}) ORDER BY id""", args.ids).fetchall()
    missing = set(args.ids) - {r["id"] for r in rows}
    if missing:
        print("нет в БД: " + ", ".join(str(i) for i in sorted(missing)))

    proven: list[tuple[sqlite3.Row, dict]] = []
    for r in rows:
        if r["close_reason"] == "entry_netted":
            print(f"#{r['id']}: уже помечен, пропускаю")
            continue
        if r["ts_open"] < horizon:
            print(f"#{r['id']} {r['symbol']} {_ts(r['ts_open'])}: старше "
                  "7 суток — биржевого доказательства уже не получить, "
                  "не трогаю")
            continue
        recs = _closed_pnl(r["symbol"], int(horizon * 1000), int(now * 1000))
        x = _proof(r, recs)
        if x is None:
            print(f"#{r['id']} {r['symbol']} {_ts(r['ts_open'])}: отпечаток "
                  "фантома НЕ подтвердился — не трогаю")
            continue
        proven.append((r, x))
        print(f"#{r['id']} {r['symbol']} {r['side']} {r['qty']} @{r['entry']}: "
              f"в БД {r['pnl_usd']:+.2f} / причина {r['close_reason']}; "
              f"на бирже закрыт ЧУЖОЙ вход @{float(x['avgEntryPrice'])} "
              f"с {float(x['closedPnl']):+.2f} — фантом подтверждён")

    if not proven:
        print("\nподтверждённых фантомов нет, БД не менялась")
        db.close()
        return

    removed = sum(r["pnl_usd"] or 0.0 for r, _ in proven)
    print(f"\nподтверждено: {len(proven)}; из статистики уйдёт "
          f"${removed:+.2f} выдуманного P&L")
    if not args.apply:
        print("это прогон без записи — повторите с --apply")
        db.close()
        return

    for r, _ in proven:
        db.execute("UPDATE trades SET close_reason='entry_netted', pnl_usd=0, "
                   "fees_usd=0 WHERE id=?", (r["id"],))
    db.commit()
    print(f"записано: {len(proven)} строк помечены как entry_netted")
    db.close()


if __name__ == "__main__":
    main()
