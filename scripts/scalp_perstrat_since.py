"""Стата scalp_bot по стратегиям с ПЕР-СТРАТЕГИЙНЫМ cutoff + разбивка по дням.

У каждой страты свой `since` (дата правки логики, см. скрин/коммиты). Реальные
сделки = status='closed', close_reason НЕ entry_*/restart_flat, pnl_usd not null.
pnl_usd — net (как записал бот / true-up). Read-only.

    docker exec -i fx-pro-bot-scalp-bot-1 python3 - < scripts/scalp_perstrat_since.py
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

DB = "/data/scalp_bot.sqlite"

# страта -> cutoff (UTC, момент последней правки логики/конфига страты).
# Точные timestamp-ы деплоев (MSK→UTC) из git log:
#   density_break       25fa872 2026-06-28T15:55Z (no-trade blacklist BTC/ZEC/TAO)
#   sweep_fade          c633366 2026-06-28T15:35Z (universe fix — меняет пул монет)
#   density_bounce      c633366 2026-06-28T15:35Z (universe fix — меняет пул монет)
#   sweep_fade_canon    d410fd0 2026-06-28T15:27Z (temporarily disabled для A/B)
#   sweep_fade_run      c4fe43a 2026-06-27T06:06Z (round-robin — конец starving)
#   sweep_fade_trend    c4fe43a 2026-06-27T06:06Z (round-robin — конец starving)
CUTOFF = {
    "sweep_fade": "2026-06-28T15:35:00",
    "sweep_fade_canon": "2026-06-28T15:27:00",
    "sweep_fade_run": "2026-06-27T06:06:00",
    "sweep_fade_trend": "2026-06-27T06:06:00",
    "density_break": "2026-06-28T15:55:00",
    "density_bounce": "2026-06-28T15:35:00",
}

_NON_TRADE = ("restart_flat", "entry_Cancelled", "entry_Rejected",
              "entry_Deactivated", "entry_timeout")


def _ts(d: str) -> float:
    return datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp()


def _agg(items: list[dict]) -> tuple[int, int, float]:
    n = len(items)
    w = sum(1 for x in items if x["pnl_usd"] > 0)
    net = sum(x["pnl_usd"] for x in items)
    return n, w, net


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    grand = 0.0
    grand_n = 0
    for strat in CUTOFF:
        since = _ts(CUTOFF[strat])
        rows = [dict(r) for r in con.execute(
            "SELECT pnl_usd, close_reason, ts_open, ts_close, status, "
            "pnl_verified FROM trades WHERE strategy=? AND ts_open>=?",
            (strat, since))]
        real = [r for r in rows
                if r["status"] == "closed"
                and str(r["close_reason"] or "") not in _NON_TRADE
                and not str(r["close_reason"] or "").startswith("entry_")
                and r["pnl_usd"] is not None]

        n, w, net = _agg(real)
        ver = sum(1 for r in real if r["pnl_verified"] == 1)
        wr = (100 * w / n) if n else 0.0
        print(f"\n{'='*54}")
        print(f"  {strat}  (с {CUTOFF[strat]} UTC)")
        print(f"  сделок: {n} | вины: {w} | WR: {wr:.0f}% | "
              f"net: ${net:+.2f} | verified: {ver}/{n}")
        print(f"{'='*54}")

        days: dict[str, list[dict]] = {}
        for r in real:
            d = datetime.fromtimestamp(
                r["ts_close"] or r["ts_open"], UTC).strftime("%Y-%m-%d")
            days.setdefault(d, []).append(r)

        if not real:
            print("  (нет реальных сделок)")
        else:
            print(f"  {'день':<12}{'сделок':>7}{'вины':>6}{'WR%':>7}{'net$':>10}")
            for d in sorted(days):
                dn, dw, dnet = _agg(days[d])
                dwr = (100 * dw / dn) if dn else 0.0
                print(f"  {d:<12}{dn:>7}{dw:>6}{dwr:>6.0f}%{dnet:>10.2f}")
        grand += net
        grand_n += n

    con.close()
    print(f"\n{'='*54}")
    print(f"  ИТОГО по 4 стратам: {grand_n} сделок, net ${grand:+.2f}")
    print(f"{'='*54}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
