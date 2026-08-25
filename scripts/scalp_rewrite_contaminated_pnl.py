#!/usr/bin/env python3
"""Переписать биржевой P&L на собственную геометрию там, где лот был общим.

Зачем. На Bybit linear one-way счёте один лот на символ
(https://bybit-exchange.github.io/docs/v5/position/position-mode), а счёт делят
несколько ботов. И `execPnl`, и `closedPnl` считаются от `avgEntryPrice` ВСЕЙ
позиции символа — то есть от средней нашей и чужой ноги. Когда чужая нога вошла
по другой цене, биржевая цифра описывает не нашу сделку. Геометрия
`(exit − entry) × qty × sign − fees` считается только по нашим собственным
записям и потому остаётся нашей.

Ровно это правило с 25.08 применяет и сам бот (`d8f9105`, `_mixed_lot` →
`_realized_from_fills`). Скрипт распространяет его на строки, закрытые ДО
деплоя, чтобы прошлая статистика и живая считались одинаково.

Порог отбора. Только строки с записанной комиссией (`fees_usd > 0`): без неё
геометрия получается валовой, и сравнение с чистым биржевым P&L несопоставимо.
Комиссия сохраняется с 08.08 — до этого 78–100% строк без неё, поэтому по
умолчанию берём август.

Порог $1 честнее считать техническим, а не доказательным. Замер 25.08 по августу
дал 18 строк, и они не делятся надвое чисто: 12 строк с разрывом $14.88…$636.60 и
6 строк с разрывом $1.04…$5.84. Большие однозначны — четыре из них это `sl_hit`,
записанный как крупный плюс (#4266 +$618.83, #4288 +$393.95, #4379 +$260.97,
#4300 +$86.94), что при живом брекете арифметически невозможно. Малые
неотличимы от дрейфа комиссии и усреднения цены частичных филлов, где биржевая
цифра как раз точнее нашей. Их переписывание сдвигает итог периода на ~$9 из
$1690 — на выводы не влияет ни в одну сторону, поэтому отдельный порог для них
не вводится: это была бы подгонка ради красоты (`no-data-fitting.mdc`).

Чего скрипт НЕ делает. Не трогает `exit`, `qty`, `entry`, комиссию и причину
закрытия — сделка была настоящей, неверна только цифра результата. Не удаляет
строки. Фантомы (позиции не существовало) — отдельный случай, для них
`scalp_relabel_netting_phantoms.py`.

Ограничение честности: доказать контаминацию по бирже можно только за 7 суток
(`get_closed_pnl`: «endTime − startTime <= 7 days»). Дальше опираемся на
арифметику своих же записей, а не на биржевое подтверждение. Поэтому скрипт
пишет только там, где расхождение материально, и печатает каждую строку до
записи.

Запуск (сначала без `--apply` — покажет и ничего не тронет):

    docker exec -i fx-pro-bot-scalp-bot-1 python3 - /data/scalp_bot.sqlite \\
        < scripts/scalp_rewrite_contaminated_pnl.py
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

_NON_TRADE = ("restart_flat", "entry_Cancelled", "entry_Rejected",
              "entry_Deactivated", "entry_timeout", "entry_netted")
_MIN_DELTA_USD = 1.0


def _ts(v: float) -> str:
    return datetime.fromtimestamp(v, UTC).strftime("%m-%d %H:%M:%S")


def _geometry(row: sqlite3.Row) -> float:
    """Чистый результат по нашим собственным записям."""
    sign = 1.0 if row["side"] == "long" else -1.0
    return (row["exit"] - row["entry"]) * row["qty"] * sign - row["fees_usd"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", default="2026-08-01",
                    help="дата начала (YYYY-MM-DD), по умолчанию август — "
                         "с него комиссия записана у всех строк")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (без флага — только показать)")
    args = ap.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d").replace(
        tzinfo=UTC).timestamp()
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    ph = ",".join("?" for _ in _NON_TRADE)
    rows = db.execute(
        f"""SELECT id, ts_open, symbol, side, qty, entry, sl, exit, pnl_usd,
                   fees_usd, close_reason, strategy
            FROM trades
            WHERE status='closed' AND mode='live' AND exit IS NOT NULL
              AND pnl_usd IS NOT NULL AND COALESCE(fees_usd, 0) > 0
              AND ts_open >= ?
              AND (close_reason IS NULL OR close_reason NOT IN ({ph}))
            ORDER BY id""", (since, *_NON_TRADE)).fetchall()

    hits = [(r, _geometry(r)) for r in rows]
    hits = [(r, g) for r, g in hits if abs(r["pnl_usd"] - g) > _MIN_DELTA_USD]

    print(f"строк в выборке с {args.since}: {len(rows)}; "
          f"расходятся с геометрией >${_MIN_DELTA_USD:.0f}: {len(hits)}")
    if not hits:
        print("переписывать нечего, БД не менялась")
        db.close()
        return

    head = (f"{'id':>6} {'открыт':<15}{'символ':<13}{'страт':<18}"
            f"{'причина':<12}{'было':>10}{'станет':>10}{'дельта':>10}")
    print(head)
    print("-" * len(head))
    by_sym: dict[str, float] = defaultdict(float)
    for r, g in hits:
        by_sym[r["symbol"]] += g - r["pnl_usd"]
        print(f"{r['id']:>6} {_ts(r['ts_open']):<15}{r['symbol']:<13}"
              f"{(r['strategy'] or '?'):<18}{(r['close_reason'] or '?'):<12}"
              f"{r['pnl_usd']:>10.2f}{g:>10.2f}{g - r['pnl_usd']:>10.2f}")

    shift = sum(g - r["pnl_usd"] for r, g in hits)
    was = sum(r["pnl_usd"] for r in rows)
    print(f"\nпо символам: " + ", ".join(
        f"{s} ${v:+.2f}" for s, v in sorted(by_sym.items(),
                                            key=lambda kv: -abs(kv[1]))))
    print(f"период целиком: было ${was:+.2f} → станет ${was + shift:+.2f} "
          f"(сдвиг ${shift:+.2f})")

    if not args.apply:
        print("это прогон без записи — повторите с --apply")
        db.close()
        return

    # verified=1 / provisional=0: геометрия и есть источник правды для такой
    # строки, биржевой closedPnl не имеет права её перезаписать.
    for r, g in hits:
        db.execute("UPDATE trades SET pnl_usd=?, pnl_verified=1, "
                   "pnl_provisional=0 WHERE id=?", (g, r["id"]))
    db.commit()
    print(f"записано: {len(hits)} строк переведены на собственную геометрию")
    db.close()


if __name__ == "__main__":
    main()
