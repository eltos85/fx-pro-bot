"""SL-cooldown sweep на РЕАЛЬНОЙ истории сделок (расширение калибровки v0.15.0).

Методика та же, что дала текущие 300с: по логу заполненных sweep_fade-сделок для
каждого окна W считаем повторные входы «та же монета + та же сторона в пределах W
после SL» и их суммарный net. Если net заблокированных < 0 — кулдаун W спасает
деньги. Расширяем окна до 30/60/90 мин (v0.15.0 тестировал только ≤600с).

Допущение (как и в исходной калибровке): блокировка входа = просто удаляем его
pnl; не моделируем, что слот/капитал ушёл бы в другую сделку. Read-only.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime

WINDOWS = [0, 300, 600, 1800, 3600, 5400, 7200]  # 0/5/10/30/60/90/120 мин


def load(db: str, frm: float, to: float, strategy: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT symbol,side,pnl_usd,close_reason,ts_open FROM trades "
        "WHERE ts_open>=? AND ts_open<? AND strategy=? AND status='closed' "
        "AND close_reason NOT LIKE 'entry_%' AND close_reason!='restart_flat' "
        "AND pnl_usd IS NOT NULL ORDER BY ts_open", (frm, to, strategy))]
    con.close()
    return rows


def sweep(rows: list[dict], window_s: float) -> dict:
    """Возвращает метрики, если применить cooldown=window_s (по symbol+side)."""
    last_sl: dict[tuple[str, str], float] = {}
    blocked, kept = [], []
    for x in rows:
        key = (x["symbol"], x["side"])
        prev = last_sl.get(key)
        is_block = window_s > 0 and prev is not None and (x["ts_open"] - prev) < window_s
        (blocked if is_block else kept).append(x)
        if x["close_reason"] == "sl_hit":
            last_sl[key] = x["ts_open"]
    net_kept = sum(x["pnl_usd"] for x in kept)
    net_block = sum(x["pnl_usd"] for x in blocked)
    n = len(kept)
    w = sum(1 for x in kept if x["pnl_usd"] > 0)
    return {"n_kept": n, "wr": (100 * w / n if n else 0), "net_kept": net_kept,
            "n_block": len(blocked), "net_block": net_block}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", default="2026-05-01")
    p.add_argument("--to", default="2026-06-09")
    p.add_argument("--strategy", default="sweep_fade")
    p.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = p.parse_args()
    frm = datetime.fromisoformat(args.frm).replace(tzinfo=UTC).timestamp()
    to = datetime.fromisoformat(args.to).replace(tzinfo=UTC).timestamp()
    rows = load(args.db, frm, to, args.strategy)

    base = sweep(rows, 0)
    print(f"=== SL-cooldown sweep | {args.strategy} | {args.frm}→{args.to} ===")
    print(f"всего сделок: {len(rows)}  (база net {base['net_kept']:+.2f})")
    print(f"\n{'окно':>8}{'оставлено':>11}{'WR':>6}{'net остав.':>12}"
          f"{'заблок.':>9}{'net заблок.':>13}{'Δ net':>9}")
    for w in WINDOWS:
        s = sweep(rows, w)
        delta = s["net_kept"] - base["net_kept"]
        lbl = "выкл" if w == 0 else f"{int(w/60)}м"
        print(f"{lbl:>8}{s['n_kept']:>11}{s['wr']:>5.0f}%{s['net_kept']:>12.2f}"
              f"{s['n_block']:>9}{s['net_block']:>13.2f}{delta:>+9.2f}")
    print("\nΔ net = насколько вырос бы net при этом окне vs cooldown выкл.")
    print("Если net заблок. отрицателен — окно режет именно убыточные перефейды.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
