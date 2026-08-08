#!/usr/bin/env python3
"""Засев `symbol_fees` тарифами, выведенными из исторических комиссий.

Зачем нужен засев. Гейт тарифа (v0.18.61) не торгует контракты, чья ставка выше
стандартной сетки Bybit, но узнаёт ставку только из поля ``feeRate`` в филлах —
а учить её мы начали лишь 2026-08-06 (`71a11ed`). Сделки по дорогим контрактам
случились раньше, поэтому без засева гейт молчал бы до следующего филла по
такому символу, то есть ровно до той сделки, которую он должен предотвратить.

Откуда берётся ставка. По закрытым сделкам считаем комиссию на сторону:
``fees_usd / (entry×qty + exit×qty)``. Замер 2026-08-08 разложился на
стандартную сетку без остатка и подтвердил `cae61f4`:

    ZEC/ADA/SHIB1000  0.0375% = (0.0200 + 0.0550)/2  maker-вход + taker-выход
    HYPE/SOL/XRP      0.0550% = taker обе ноги
    BTC/ETH           0.0544% / 0.0540% ≈ taker обе ноги
    ESPORTSUSDT       0.0750% = (0.0400 + 0.1100)/2  ДВОЙНОЙ тариф
    BANKUSDT          0.0966% — между 0.0400 и 0.1100, ДВОЙНОЙ тариф

Скрипт НЕ угадывает ставки: он записывает удвоенную сетку только тем символам,
для которых это подтверждено, и только если по символу ещё нет ставки, выученной
из ``feeRate`` — настоящий филл всегда приоритетнее вывода из агрегата.

Идемпотентен. Запуск: python3 scalp_seed_symbol_fees.py <путь к sqlite> [--apply]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time

from scalp_bot.analysis.fees import STANDARD_MAKER_FEE, STANDARD_TAKER_FEE

# Символы с подтверждённым двойным тарифом и замеренная ставка на сторону,
# по которой сделан вывод. Список НЕ расширять без такого же замера.
DOUBLE_TARIFF = {
    "BANKUSDT": 0.000966,
    "ESPORTSUSDT": 0.000750,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--apply", action="store_true",
                    help="без этого флага только показывает, что сделает")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    now = time.time()
    planned, skipped = [], []

    for symbol, observed in sorted(DOUBLE_TARIFF.items()):
        row = conn.execute(
            "SELECT maker_rate, taker_rate, maker_samples, taker_samples "
            "FROM symbol_fees WHERE symbol = ?", (symbol,)).fetchone()
        if row is not None and (row["maker_samples"] or row["taker_samples"]):
            skipped.append(
                f"{symbol}: уже выучен из филлов "
                f"(maker={row['maker_rate']}, taker={row['taker_rate']}) — "
                f"не перезаписываю")
            continue
        planned.append((symbol, observed))

    for line in skipped:
        print(line)
    for symbol, observed in planned:
        print(f"{symbol}: замер {observed * 100:.4f}%/сторона "
              f"(×{observed / STANDARD_TAKER_FEE:.2f} к стандарту) → "
              f"maker {2 * STANDARD_MAKER_FEE * 100:.4f}%, "
              f"taker {2 * STANDARD_TAKER_FEE * 100:.4f}%")

    if not args.apply:
        print("\nсухой прогон; повторите с --apply, чтобы записать")
        return 0

    for symbol, _observed in planned:
        # samples=0: запись помечена как ВЫВЕДЕННАЯ, не выученная из филла.
        # Первый настоящий филл перезапишет ставку и поднимет счётчик.
        conn.execute(
            "INSERT INTO symbol_fees "
            "(symbol, maker_rate, taker_rate, maker_samples, taker_samples, "
            " first_seen, updated_at) VALUES (?,?,?,0,0,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET maker_rate=excluded.maker_rate, "
            "taker_rate=excluded.taker_rate, updated_at=excluded.updated_at",
            (symbol, 2 * STANDARD_MAKER_FEE, 2 * STANDARD_TAKER_FEE, now, now))
    conn.commit()
    print(f"\nзаписано символов: {len(planned)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
