"""Advisor exit efficiency analysis — насколько рано Advisor закрывал сделки.

Источник правды: advisor_stats.sqlite, таблица `positions` (2381 закрытая
сделка). У каждой записи есть `peak_price` и `trough_price` — экстремумы
цены ЗА ВРЕМЯ ЖИЗНИ сделки. Это позволяет точно посчитать "сколько
профита Advisor оставлял на столе".

Для каждой closed-сделки считаем:

  direction = long:
    max_potential_pips  = (peak_price - entry_price) / pip_size    if peak  > entry else 0
    max_adverse_pips    = (entry_price - trough_price) / pip_size  if trough< entry else 0
    realized_pips       = (current_price - entry_price) / pip_size

  direction = short:
    max_potential_pips  = (entry_price - trough_price) / pip_size  if trough< entry else 0
    max_adverse_pips    = (peak_price - entry_price) / pip_size    if peak  > entry else 0
    realized_pips       = (entry_price - current_price) / pip_size

Метрики:
1) **Capture efficiency** = realized / max_potential (для winners) — %
   профита от максимума.
2) **Early-exit losers** — сделки где realized<0, но max_potential>0
   (видели плюс, потом ушли в минус и Advisor закрыл).
3) **Drawdown survivors** (то что наблюдал пользователь!) —
   max_adverse > X pips, потом realized > Y pips.
4) **MAE / MFE** (canonical Tharp metrics): max-adverse-excursion vs
   max-favorable-excursion.

Без правок кода. Чистая read-only диагностика.
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict


# Pip sizes by instrument (consistent с executor.py pip_size table).
PIP_SIZE = {
    "GC=F": 0.01,    # XAUUSD у broker'а (Gold pip = $0.01)
    "BZ=F": 0.01,    # Brent oil pip = $0.01
    "NG=F": 0.001,
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "GBPJPY": 0.01,
    "EURJPY": 0.01,
    "AUDUSD": 0.0001,
    "NZDUSD": 0.0001,
    "USDCAD": 0.0001,
    "USDCHF": 0.0001,
}


def pip_size(instrument: str) -> float:
    return PIP_SIZE.get(instrument, 0.0001)


def main(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, strategy, source, instrument, direction, "
        "entry_price, current_price, peak_price, trough_price, "
        "stop_loss_price, profit_pips, profit_pct, exit_reason, "
        "closed_at, broker_volume "
        "FROM positions WHERE status = 'closed'"
    ).fetchall()
    conn.close()

    if not rows:
        print("(no closed positions)")
        return 1

    print(f"=== Advisor exit efficiency analysis: {len(rows)} closed trades ===\n")

    # Group by strategy + winners/losers + interesting buckets.
    per_strategy: dict[str, list[dict]] = defaultdict(list)
    all_trades: list[dict] = []
    for r in rows:
        pip = pip_size(r["instrument"])
        entry = r["entry_price"]
        cur = r["current_price"]
        peak = r["peak_price"]
        trough = r["trough_price"]
        direction = r["direction"]
        if pip <= 0 or entry <= 0:
            continue

        if direction == "long":
            max_potential = max(0.0, (peak - entry) / pip)
            max_adverse = max(0.0, (entry - trough) / pip)
            realized = (cur - entry) / pip
        else:  # short
            max_potential = max(0.0, (entry - trough) / pip)
            max_adverse = max(0.0, (peak - entry) / pip)
            realized = (entry - cur) / pip

        rec = {
            "id": r["id"],
            "strategy": r["strategy"],
            "instrument": r["instrument"],
            "direction": direction,
            "max_potential": max_potential,
            "max_adverse": max_adverse,
            "realized": realized,
            "exit_reason": r["exit_reason"],
            "is_winner": realized > 0,
            "is_loser": realized < 0,
            "left_on_table": max(0.0, max_potential - realized) if realized > 0 else max_potential,
        }
        per_strategy[r["strategy"]].append(rec)
        all_trades.append(rec)

    # ─── overall capture efficiency ─────────────────────────────────────
    print(f"=== ALL strategies combined (n={len(all_trades)}) ===")
    winners = [t for t in all_trades if t["is_winner"]]
    losers = [t for t in all_trades if t["is_loser"]]
    flat = [t for t in all_trades if not t["is_winner"] and not t["is_loser"]]
    wr = 100 * len(winners) / len(all_trades) if all_trades else 0
    print(f"Win rate: {wr:.1f}% ({len(winners)}W / {len(losers)}L / {len(flat)}F)")

    if winners:
        # Capture efficiency только для winners с положительным потенциалом
        winners_with_pot = [t for t in winners if t["max_potential"] > 0]
        capture_ratios = [t["realized"] / t["max_potential"] for t in winners_with_pot]
        if capture_ratios:
            print(f"\n--- WINNERS (n={len(winners)}) capture efficiency ---")
            print(f"  median: {100*statistics.median(capture_ratios):.1f}%")
            print(f"  mean:   {100*statistics.mean(capture_ratios):.1f}%")
            print(f"  p25:    {100*statistics.quantiles(capture_ratios, n=4)[0]:.1f}%")
            print(f"  p75:    {100*statistics.quantiles(capture_ratios, n=4)[2]:.1f}%")
            # Сколько winners закрылось с <50% / <30% / <10% от max
            low_capture_50 = [t for t in winners_with_pot if t["realized"] / t["max_potential"] < 0.5]
            low_capture_30 = [t for t in winners_with_pot if t["realized"] / t["max_potential"] < 0.3]
            low_capture_10 = [t for t in winners_with_pot if t["realized"] / t["max_potential"] < 0.1]
            print(f"  Closed < 50% of max-favorable: {len(low_capture_50)} ({100*len(low_capture_50)/len(winners_with_pot):.0f}%)")
            print(f"  Closed < 30% of max-favorable: {len(low_capture_30)} ({100*len(low_capture_30)/len(winners_with_pot):.0f}%)")
            print(f"  Closed < 10% of max-favorable: {len(low_capture_10)} ({100*len(low_capture_10)/len(winners_with_pot):.0f}%)")

    if losers:
        # Loosers, у которых был ПЛЮС перед закрытием в минус
        # (классический "was-winner-became-loser")
        early_winners_now_losers = [t for t in losers if t["max_potential"] > 0]
        print(f"\n--- LOSERS (n={len(losers)}) early-winner-now-loser pattern ---")
        print(f"  Losers с peak в плюсе: {len(early_winners_now_losers)} "
              f"({100*len(early_winners_now_losers)/len(losers):.0f}%)")
        if early_winners_now_losers:
            potentials_pips = [t["max_potential"] for t in early_winners_now_losers]
            losses_pips = [-t["realized"] for t in early_winners_now_losers]
            print(f"  median peak BEFORE turning loser: {statistics.median(potentials_pips):.1f} pips")
            print(f"  median final loss:                {statistics.median(losses_pips):.1f} pips")

        # Pattern user наблюдал: drawdown survivor — drop in minus deep,
        # then recover. Это не для Advisor (он бы закрыл), но мы видим
        # его в orphan-позиции.

    # ─── per-strategy breakdown (топ-10 по объёму) ──────────────────────
    print(f"\n=== PER-STRATEGY (top 10 by volume) ===")
    strategies_by_n = sorted(per_strategy.items(), key=lambda kv: -len(kv[1]))[:10]
    for strat, trades in strategies_by_n:
        n = len(trades)
        wins = [t for t in trades if t["is_winner"]]
        losses = [t for t in trades if t["is_loser"]]
        wr = 100 * len(wins) / n if n else 0
        wins_with_pot = [t for t in wins if t["max_potential"] > 0]
        if wins_with_pot:
            cap = statistics.median(t["realized"] / t["max_potential"] for t in wins_with_pot)
        else:
            cap = 0
        ewnl = [t for t in losses if t["max_potential"] > 0]
        median_realized = statistics.median(t["realized"] for t in trades) if trades else 0
        print(
            f"  {strat:<20} n={n:>4}  WR={wr:>5.1f}%  "
            f"med_capture(W)={100*cap:>5.1f}%  "
            f"early-W-loss={len(ewnl):>3}/{len(losses):>3}  "
            f"med_realized={median_realized:>+7.2f}pips"
        )

    # ─── специальный кейс: orphan position pattern ──────────────────────
    print(f"\n=== Drawdown-survivor pattern (что наблюдал пользователь) ===")
    print(f"(сделки где max_adverse > 30 pips и затем realized > 30 pips — выжили из глубокой просадки)")
    survivors = [
        t for t in all_trades
        if t["max_adverse"] > 30 and t["realized"] > 30
    ]
    print(f"  n={len(survivors)} ({100*len(survivors)/len(all_trades):.1f}% от всех)")
    if survivors:
        print(f"  median max_adverse:  {statistics.median(t['max_adverse'] for t in survivors):.1f} pips")
        print(f"  median realized:     {statistics.median(t['realized'] for t in survivors):.1f} pips")

    print(f"\n=== Stop-loss hit too quick? ===")
    print(f"(LOSERS с max_potential > max_adverse — мы могли бы выйти в плюс, но SL сработал)")
    could_be_winners = [t for t in losers if t["max_potential"] > t["max_adverse"] and t["max_potential"] > 0]
    print(f"  n={len(could_be_winners)} ({100*len(could_be_winners)/len(losers):.1f}% от losers)")
    if could_be_winners:
        print(f"  median missed-profit: {statistics.median(t['max_potential'] for t in could_be_winners):.1f} pips")

    return 0


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "/root/fx-pro-bot/data/advisor_stats.sqlite"
    sys.exit(main(db))
