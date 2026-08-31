"""Цена исправления `round_trip_fee_frac`: сколько сигналов отсечёт честная ставка.

Замер 31.08 показал, что бот считает издержки от константы 0.075% за круг, а
платит 0.1006% (`scripts/collect_scalp_stats.py`). Прежде чем трогать
константу, надо знать цену правки. Она бьёт по двум местам сразу:

1. **fee-guard** в `analysis/signals.build_signal`: сигнал отбрасывается, если
   ход до цели меньше `min_target_fee_mult` (3.0) × round-trip. Поднятие
   ставки поднимает и планку: 0.225% хода → 0.302%.
2. **Пол минимального риска** там же: `R ≥ min_risk_fee_mult` (4.0) ×
   round-trip = 0.30% цены → 0.402%. Стоп раздвигается в 1.34 раза, а вместе
   с ним и цель (TP = target_r × R).

Скрипт считает оба эффекта на накопленной телеметрии, ничего не меняя.

Часть A — отсев сигналов. По геометрии реальных сигналов (живые сделки и
`shadow_signals`) проверяем, сколько из них не прошли бы новую планку
fee-guard. Здесь же проверяется, связывает ли этот гейт хоть когда-нибудь:
он сравнивает `target_r × R` с `3 × rt`, при этом сам R не может быть меньше
`4 × rt` из-за пола, так что порог достижим только при `target_r < 0.75`.

Часть B — исход при раздвинутом стопе. По `counterfactual_setups` (state
`final`, поля `mfe_r`/`mae_r` — крайние отклонения в долях риска) пересчитываем
те же сетапы на риск в 1.34 раза шире. Когда достигнуты и стоп, и цель,
засчитывается стоп: порядок событий телеметрия не хранит, поэтому берём
худший вариант — это нижняя граница оценки, а не точный результат.

Комиссия в обоих сценариях — фактическая (0.1006% за круг), а не модельная.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict

DB = "/data/scalp_bot.sqlite"
# Замер scripts/collect_scalp_stats.py --days 30 (31.08): 969 филлов, оборот
# по closed-pnl, две ручки API разошлись на 0.8%.
FEE_RT_ACTUAL = 0.001006
FEE_RT_CONST = 0.00075
MIN_RISK_MULT = 4.0
MIN_TARGET_MULT = 3.0
NON_TRADE = ("restart_flat", "entry_Cancelled", "entry_Rejected",
             "entry_Deactivated", "entry_timeout", "entry_netted")


def pct(a: int, b: int) -> str:
    return f"{a / b * 100:.1f}%" if b else "—"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    q = conn.execute
    import time
    since = time.time() - args.days * 86400

    widen = FEE_RT_ACTUAL / FEE_RT_CONST
    out: list[str] = []
    out.append("=" * 72)
    out.append(f"ЦЕНА ИСПРАВЛЕНИЯ КОНСТАНТЫ ИЗДЕРЖЕК · окно {args.days} дн")
    out.append("=" * 72)
    out.append(f"  Ставка в коде : {FEE_RT_CONST * 100:.4f}% за круг")
    out.append(f"  Ставка по факту: {FEE_RT_ACTUAL * 100:.4f}% за круг "
               f"(шире в {widen:.2f}×)")
    out.append(f"  Пол риска      : {MIN_RISK_MULT * FEE_RT_CONST * 100:.3f}% "
               f"→ {MIN_RISK_MULT * FEE_RT_ACTUAL * 100:.3f}% цены")
    out.append(f"  Планка fee-guard: {MIN_TARGET_MULT * FEE_RT_CONST * 100:.3f}% "
               f"→ {MIN_TARGET_MULT * FEE_RT_ACTUAL * 100:.3f}% хода")

    # ─── A. Отсев сигналов новой планкой ─────────────────────────────
    out.append("")
    out.append("A. СКОЛЬКО СИГНАЛОВ ОТСЕЧЁТ НОВАЯ ПЛАНКА")
    ph = ",".join("?" for _ in NON_TRADE)
    live = q(f"SELECT strategy, entry, sl, tp FROM trades "
             f"WHERE status='closed' AND mode='live' AND ts_close>=? "
             f"AND entry>0 AND sl>0 AND tp>0 "
             f"AND (close_reason IS NULL OR close_reason NOT IN ({ph}))",
             (since, *NON_TRADE)).fetchall()
    shadow = q("SELECT strategy, entry_ref AS entry, sl_level AS sl, "
               "tp_level AS tp FROM shadow_signals "
               "WHERE ts>=? AND entry_ref>0 AND sl_level>0 AND tp_level>0",
               (since,)).fetchall()

    thr_old = MIN_TARGET_MULT * FEE_RT_CONST
    thr_new = MIN_TARGET_MULT * FEE_RT_ACTUAL
    floor_old = MIN_RISK_MULT * FEE_RT_CONST
    for label, rows in (("живые сделки", live), ("теневые сигналы", shadow)):
        if not rows:
            continue
        moves: list[float] = []
        risks: list[float] = []
        on_floor = 0
        cut_old = cut_new = 0
        for r in rows:
            e = float(r["entry"])
            rk = abs(e - float(r["sl"])) / e
            mv = abs(float(r["tp"]) - e) / e
            risks.append(rk)
            moves.append(mv)
            if abs(rk - floor_old) / floor_old < 0.02:
                on_floor += 1
            if mv < thr_old:
                cut_old += 1
            if mv < thr_new:
                cut_new += 1
        n = len(rows)
        out.append(f"  {label} (n={n}):")
        out.append(f"    Ход до цели: медиана {statistics.median(moves) * 100:.3f}%, "
                   f"минимум {min(moves) * 100:.3f}%")
        out.append(f"    Стоп: медиана {statistics.median(risks) * 100:.3f}% цены, "
                   f"ровно на полу 0.300% — {on_floor} ({pct(on_floor, n)})")
        out.append(f"    Не прошли бы старую планку {thr_old * 100:.3f}%: {cut_old}")
        out.append(f"    Не прошли бы новую планку {thr_new * 100:.3f}%: "
                   f"{cut_new} ({pct(cut_new, n)})")

    out.append("")
    out.append("  Почему так: fee-guard сравнивает target_r × R с 3 × ставка,")
    out.append("  а сам R не бывает меньше 4 × ставка из-за пола. Гейт может")
    out.append(f"  сработать только при target_r < {MIN_TARGET_MULT / MIN_RISK_MULT:.2f}; "
               f"в бою target_r = 2.5–3.5.")

    # ─── B. Исход при раздвинутом стопе ──────────────────────────────
    out.append("")
    out.append("B. ЧТО БУДЕТ С ИСХОДАМИ, ЕСЛИ СТОП РАЗДВИНУТЬ В 1.34 РАЗА")
    cf = q("SELECT strategy, target_r, mfe_r, mae_r FROM counterfactual_setups "
           "WHERE state='final' AND ts_entry>=? AND risk>0",
           (since,)).fetchall()
    out.append(f"  Завершённых контрфактуалов за окно: {len(cf)}")
    if cf:
        by_strat: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in cf:
            by_strat[r["strategy"]].append(r)
        out.append(f"  {'стратегия':<20} {'n':>5} {'сценарий':<10} {'тейк':>6} "
                   f"{'стоп':>6} {'ни то':>6} {'валR':>8} {'чистR':>8}")
        for strat, rs in sorted(by_strat.items(), key=lambda kv: -len(kv[1])):
            for label, k in (("сейчас", 1.0), ("шире 1.34", widen)):
                tp_hit = sl_hit = neither = 0
                res: list[float] = []
                for r in rs:
                    tgt = float(r["target_r"] or 3.5)
                    mfe = float(r["mfe_r"] or 0.0) / k
                    mae = float(r["mae_r"] or 0.0) / k
                    # mae_r хранится по модулю хода против позиции.
                    hit_sl = abs(mae) >= 1.0
                    hit_tp = mfe >= tgt
                    if hit_sl:
                        sl_hit += 1
                        res.append(-1.0)
                    elif hit_tp:
                        tp_hit += 1
                        res.append(tgt)
                    else:
                        neither += 1
                        res.append(mfe - abs(mae))
                # Комиссия в долях НОВОГО риска: ставка / (пол риска).
                fee_r = FEE_RT_ACTUAL / (MIN_RISK_MULT * FEE_RT_CONST * k)
                gross = statistics.fmean(res)
                out.append(f"  {strat:<20} {len(rs):5d} {label:<10} "
                           f"{tp_hit:6d} {sl_hit:6d} {neither:6d} "
                           f"{gross:+8.3f} {gross - fee_r:+8.3f}")
        out.append("")
        out.append("  Когда достигнуты и стоп, и цель, засчитан стоп: порядок")
        out.append("  событий телеметрия не хранит. Это нижняя граница оценки —")
        out.append("  строки сравнимы между собой, абсолютные значения занижены.")

    conn.close()
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
