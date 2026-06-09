"""Тест кандидата E: коррелирует ли WR фейда с реализованной волатильностью монеты.

Для каждого символа со sweep_fade-сделками за окно: WR (filled) из БД + реализованная
волатильность из публичных 5m-клинов Bybit (медианный bar-range% и ATR%-прокси за
последние N часов). Если высокая vol → низкий WR монотонно — это КАНОН-сигнал на
vol-ceiling для фейда (Tradeify «thin/vol → dangerous to fade»; Connors premature-stop),
едино для ВСЕХ монет (НЕ скип ZEC по имени). Read-only, без подгонки порогов.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
import time
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime


def klines5m(sym: str, hours: int) -> list[tuple[float, float, float]]:
    end = time.time()
    start = end - hours * 3600
    url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}"
           f"&interval=5&start={int(start*1000)}&end={int(end*1000)}&limit=1000")
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                d = json.load(r)
            out = [(float(x[2]), float(x[3]), float(x[4]))
                   for x in d.get("result", {}).get("list", []) or []]
            return out
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return []


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", default="2026-06-05T11:00")
    p.add_argument("--hours", type=int, default=96)
    p.add_argument("--db", default="/data/scalp_bot.sqlite")
    args = p.parse_args()
    frm = datetime.fromisoformat(args.frm).replace(tzinfo=UTC).timestamp()
    con = sqlite3.connect(args.db)
    rows = con.execute(
        "SELECT symbol,pnl_usd FROM trades WHERE ts_open>=? AND strategy='sweep_fade' "
        "AND status='closed' AND close_reason IN ('flow_exit','sl_hit','tp_hit')",
        (frm,)).fetchall()
    con.close()
    by = defaultdict(list)
    for s, p_ in rows:
        by[s].append(p_ > 0)

    print(f"{'symbol':12s} n   WR     barRange%  ATR%proxy")
    data = []
    for s, w in sorted(by.items(), key=lambda kv: -len(kv[1])):
        n = len(w)
        if n < 4:
            continue
        wr = 100 * sum(w) / n
        ks = klines5m(s, args.hours)
        if not ks:
            print(f"{s:12s} {n:3d} {wr:5.1f}%   (нет клинов)")
            continue
        ranges = [(h - lo) / c * 100 for (h, lo, c) in ks if c]
        med_range = st.median(ranges) if ranges else 0.0
        atr = st.mean(ranges) if ranges else 0.0
        data.append((s, n, wr, med_range))
        print(f"{s:12s} {n:3d} {wr:5.1f}%   {med_range:6.3f}%    {atr:6.3f}%")

    if len(data) >= 4:
        vols = [d[3] for d in data]
        wrs = [d[2] for d in data]
        mv = st.mean(vols)
        mw = st.mean(wrs)
        cov = sum((v - mv) * (w - mw) for v, w in zip(vols, wrs))
        sv = (sum((v - mv) ** 2 for v in vols)) ** 0.5
        sw = (sum((w - mw) ** 2 for w in wrs)) ** 0.5
        r = cov / (sv * sw) if sv and sw else 0.0
        print(f"\nкорреляция vol↔WR (n={len(data)} монет): r={r:+.2f}")
        print("(сильно отрицательная r = высокая vol душит WR → канон vol-ceiling)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
