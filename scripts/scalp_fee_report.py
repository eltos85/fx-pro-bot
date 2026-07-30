#!/usr/bin/env python3
"""Read-only отчёт по ФАКТИЧЕСКИМ ставкам комиссии (v0.18.53).

Зачем нужен. Гейты fee-guard в ``build_signal`` считают издержки от константы
``cfg.round_trip_fee_frac`` = 0.075% (maker-вход + taker-выход) и обещают в
комментарии инвариант «fee ≤ 0.25R» при ``min_risk_fee_mult=4``. Константа
верна только для maker-входа на стандартном тарифе, но живут ещё два
отклонения, и оба ломают обещание:

1. **Тип входа.** Market-входом ходят три стратегии из четырёх
   (``sweep_fade_canon``, ``density_break``, ``density_bounce``) — у них обе
   ноги taker, round-trip 0.11%, а не 0.075%.
2. **Тариф контракта.** BANKUSDT и ESPORTSUSDT берут вдвое больше стандартных
   Bybit 0.055%/0.02%. Заранее это не узнать: ``/v5/account/fee-rate`` в
   demo-списке API отсутствует, поэтому ставку мы учим из исполнений
   (``feeRate`` в каждом филле) и складываем в ``symbol_fees``.

Замеры совпали с обоими предсказаниями: 0.247R при maker-входе, 0.366R при
market-входе, 0.459R при market-входе на двойном тарифе — против обещанных
кодом 0.25R.

Скрипт НИЧЕГО не меняет и не активирует. Решение о геометрии стопа ждёт
завершения ``sl_widen`` (он измеряет ровно то расширение, к которому привела бы
буквальная починка константы), см. ``scripts/scalp_forward_checkpoint.py``.

Usage:
  python scripts/scalp_fee_report.py data/scalp_bot.sqlite \
      [--since 2026-07-22T14:08:00Z] [--assumed-pct 0.075]
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

# Стандартные ставки Bybit linear для не-VIP аккаунта.
# https://www.bybit.com/en/help-center/article/Trading-Fee-Structure
STD_MAKER_PCT = 0.02
STD_TAKER_PCT = 0.055

# Инвариант, обещанный в settings.py рядом с min_risk_fee_mult=4:
# R ≥ 4 × round_trip → комиссия ≤ 1/4 доли риска.
PROMISED_FEE_R = 0.25

# Ставка, из которой считают гейты (cfg.round_trip_fee_frac = 0.00075).
DEFAULT_ASSUMED_PCT = 0.075


def _timestamp(value: str) -> float:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def learned_rates(conn: sqlite3.Connection) -> list[dict]:
    """Ставки, выученные из исполнений. Таблица появилась в v0.18.53, поэтому
    на старой БД её может не быть — это не ошибка, просто пусто."""
    try:
        rows = conn.execute(
            "SELECT symbol, maker_rate, taker_rate, maker_samples, "
            "taker_samples, updated_at FROM symbol_fees ORDER BY symbol"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def trade_costs(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Издержки по закрытым live-сделкам с известной комиссией.

    ``fees_usd`` начал заполняться в v0.18.44; до него он был нулём, поэтому
    строки без комиссии отбрасываем — иначе нулями размыли бы средние.
    """
    rows = conn.execute(
        """SELECT strategy, symbol, qty, entry, sl, exit, fees_usd, ts_open
           FROM trades
           WHERE mode='live' AND status='closed' AND ts_open >= ?
             AND fees_usd IS NOT NULL AND fees_usd > 0
             AND exit IS NOT NULL AND qty > 0 AND entry > 0
             AND (close_reason IS NULL OR close_reason NOT LIKE 'entry_%')""",
        (since,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        risk_usd = r["qty"] * abs(r["entry"] - r["sl"])
        # Знаменатель round-turn — сумма ноционалов ОБЕИХ ног: комиссия берётся
        # с каждой отдельно, и на выходе ноционал другой (цена ушла).
        notional = r["qty"] * (r["entry"] + r["exit"])
        if risk_usd <= 0 or notional <= 0:
            continue
        out.append({
            "strategy": r["strategy"], "symbol": r["symbol"],
            # Средняя ставка за сторону: удобно сравнивать со стандартными
            # 0.055%/0.02% напрямую, без домножения.
            "per_side_pct": 100.0 * r["fees_usd"] / notional,
            "sl_pct": 100.0 * abs(r["entry"] - r["sl"]) / r["entry"],
            "fee_r": r["fees_usd"] / risk_usd,
        })
    return out


def _aggregate(rows: list[dict], key: str,
               assumed_pct: float) -> list[tuple[str, dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    out: list[tuple[str, dict]] = []
    for name, items in groups.items():
        n = len(items)
        sl_pct = sum(i["sl_pct"] for i in items) / n
        out.append((name, {
            "n": n,
            "per_side_pct": sum(i["per_side_pct"] for i in items) / n,
            "sl_pct": sl_pct,
            "fee_r": sum(i["fee_r"] for i in items) / n,
            # Что «думал» гейт про эту сделку: round_trip_fee_frac / SL%.
            "assumed_fee_r": (assumed_pct / sl_pct) if sl_pct > 0 else None,
        }))
    return sorted(out, key=lambda kv: -kv[1]["fee_r"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--since", default="1970-01-01T00:00:00Z")
    ap.add_argument("--assumed-pct", type=float, default=DEFAULT_ASSUMED_PCT,
                    help="round-trip ставка, из которой считают гейты, в %%")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    since = _timestamp(args.since)

    print(f"=== выученные ставки (из исполнений) === since={args.since}")
    learned = learned_rates(conn)
    if not learned:
        print("таблица symbol_fees пуста — ставки учатся с первого филла "
              "после выкатки v0.18.53")
    else:
        print(f"{'символ':16} {'maker%':>8} {'taker%':>8} {'×станд':>8} "
              f"{'филлов':>8}")
        for row in learned:
            maker = row["maker_rate"]
            taker = row["taker_rate"]
            # Кратность считаем по taker: он есть почти всегда (выход всегда
            # market/bracket), а maker появляется только у лимитных входов.
            ratio = (taker / (STD_TAKER_PCT / 100.0)) if taker else None
            samples = (row["maker_samples"] or 0) + (row["taker_samples"] or 0)
            flag = " ⚠ двойной тариф" if ratio and ratio > 1.5 else ""
            print(f"{row['symbol']:16} "
                  f"{_fmt(100.0 * maker, 4) if maker else 'n/a':>8} "
                  f"{_fmt(100.0 * taker, 4) if taker else 'n/a':>8} "
                  f"{_fmt(ratio, 2):>8} {samples:>8}{flag}")

    rows = trade_costs(conn, since)
    if not rows:
        print("\nнет закрытых сделок с известной комиссией "
              "(fees_usd заполняется с v0.18.44)")
        conn.close()
        return 0

    for key, title in (("strategy", "стратегиям"), ("symbol", "символам")):
        print(f"\n=== фактическая комиссия по {title} ===")
        print(f"{'группа':18} {'N':>5} {'за_сторону%':>12} {'SL%цены':>9} "
              f"{'факт_R':>8} {'гейт_R':>8} {'Δ_R':>8}")
        for name, agg in _aggregate(rows, key, args.assumed_pct):
            delta = (None if agg["assumed_fee_r"] is None
                     else agg["fee_r"] - agg["assumed_fee_r"])
            print(f"{name:18} {agg['n']:>5} {agg['per_side_pct']:>12.4f} "
                  f"{agg['sl_pct']:>9.3f} {agg['fee_r']:>8.3f} "
                  f"{_fmt(agg['assumed_fee_r']):>8} {_fmt(delta):>8}")

    overall = sum(r["fee_r"] for r in rows) / len(rows)
    print(f"\nвсего сделок {len(rows)}, средняя комиссия {overall:.3f}R "
          f"против обещанных кодом {PROMISED_FEE_R:.2f}R "
          f"(min_risk_fee_mult=4 при ставке {args.assumed_pct}%)")
    print("«гейт_R» — во что комиссию оценивал fee-guard; «факт_R» — что "
          "списала биржа. Разрыв означает, что инвариант «fee ≤ 0.25R» "
          "не выполняется, а не что стоп надо расширить: расширение измеряет "
          "sl_widen, и его предварительная оценка отрицательна.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
