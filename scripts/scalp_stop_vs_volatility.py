#!/usr/bin/env python3
"""Проверка: стоп поставлен по волатильности символа или по константе?

Код в analysis/signals.py ставит пол ширины стопа как
``min_risk_fee_mult × round_trip_fee_frac × entry`` — это КОНСТАНТНАЯ доля цены,
одинаковая для BTC и для свежего альта. При этом обоснование пола в settings.py
ссылается на канон, который привязан к ВОЛАТИЛЬНОСТИ: «стоп = структура +
ATR-буфер, 0.8–1.5× ATR за свингом» (Wilder 1978 «2 ATR»; VT Markets;
cryptotrading-guide). Константная доля цены и доля ATR — разные вещи: при
NATR 0.2% стоп 0.3% это 1.5 ATR, при NATR 1.0% — 0.3 ATR, то есть внутри
обычного шума.

Скрипт меряет по фактическим сделкам, во сколько ATR встал стоп, и растёт ли
доля стоп-аутов по мере того, как стоп становится теснее относительно шума.
Гипотеза механическая, не подогнанная: стоп внутри шумовой полосы выбивается
шумом, а не опровержением тезиса.

Только чтение.
"""

from __future__ import annotations

import argparse
import sqlite3
from statistics import median

# Границы бакетов «стоп в единицах ATR». 1.0 — канонический минимум Wilder-
# школы для свинг-стопа, 0.5 — заведомо внутри шума, 2.0 — «2 ATR» Wilder.
BUCKETS = ((0.0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 1e9))


def bucket(x: float) -> str:
    for lo, hi in BUCKETS:
        if lo <= x < hi:
            return f"{lo:g}–{hi:g}" if hi < 1e9 else f"{lo:g}+"
    return "?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", type=float, required=True)
    ap.add_argument("--strategy", default=None)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    # regime_features пишется на момент входа и содержит htf_natr_pct —
    # нормированный ATR старшего таймфрейма в процентах цены. Это и есть
    # масштаб шума, с которым нужно сравнивать ширину стопа.
    where = "AND t.strategy = :s" if args.strategy else ""
    rows = db.execute(
        f"""SELECT t.strategy, t.symbol, t.close_reason,
                   abs(t.entry - t.sl) / t.entry * 100.0 AS sl_pct,
                   r.htf_natr_pct AS natr,
                   t.pnl_usd, t.fees_usd, abs(t.entry - t.sl) * t.qty AS risk_usd
            FROM trades t JOIN regime_features r ON r.trade_id = t.id
            WHERE t.status = 'closed' AND t.ts_open >= :since
              AND t.entry > 0 AND t.sl > 0 AND t.qty > 0
              AND r.htf_natr_pct IS NOT NULL AND r.htf_natr_pct > 0 {where}""",
        {"since": args.since, "s": args.strategy},
    ).fetchall()

    if not rows:
        print("нет сделок с разметкой волатильности")
        return

    groups: dict[str, list[sqlite3.Row]] = {}
    ratios = []
    for r in rows:
        ratio = r["sl_pct"] / r["natr"]
        ratios.append(ratio)
        groups.setdefault(bucket(ratio), []).append(r)

    title = args.strategy or "все стратегии"
    print(f"{title}: {len(rows)} сделок с разметкой волатильности")
    print(f"стоп в единицах ATR (htf NATR): медиана {median(ratios):.2f}, "
          f"мин {min(ratios):.2f}, макс {max(ratios):.2f}")
    print("\nканон Wilder: свинг-стоп 0.8–2.0 ATR. Ниже 0.5 ATR — внутри шума.\n")

    head = f"{'стоп/ATR':<12}{'n':>5}{'доля SL':>10}{'валR':>9}{'чистR':>9}"
    print(head)
    print("-" * len(head))
    order = [f"{lo:g}–{hi:g}" if hi < 1e9 else f"{lo:g}+" for lo, hi in BUCKETS]
    for key in order:
        g = groups.get(key)
        if not g:
            continue
        n = len(g)
        sl_hits = sum(1 for r in g if (r["close_reason"] or "").lower().find("sl") >= 0
                      or (r["close_reason"] or "").lower().find("stop") >= 0)
        gross, net = [], []
        for r in g:
            if not r["risk_usd"]:
                continue
            fee = r["fees_usd"] or 0.0
            pnl = r["pnl_usd"] or 0.0
            net.append(pnl / r["risk_usd"])
            gross.append((pnl + fee) / r["risk_usd"])
        print(f"{key:<12}{n:>5}{sl_hits / n * 100:>9.0f}%"
              f"{sum(gross) / len(gross):>9.3f}{sum(net) / len(net):>9.3f}")


if __name__ == "__main__":
    main()
