"""Проверка прогнозов из PREDICTIONS.md через Bybit public API.

Не требует API-ключей — использует публичные endpoint'ы. Работает для
фиксации снимка цен (baseline), ежедневного мониторинга и финального отчёта.

Использование:
    python3 scripts/check_predictions.py                     # текущий снимок
    python3 scripts/check_predictions.py --since 2026-04-18  # max/min с даты
    python3 scripts/check_predictions.py --save              # дописать в CSV
    python3 scripts/check_predictions.py --verdict           # финальный вердикт
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BYBIT_BASE = "https://api.bybit.com"

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "data" / "predictions_track.csv"


@dataclass
class Prediction:
    symbol: str
    entry: float
    target: float
    stop: float
    confidence: str

    @property
    def upside_pct(self) -> float:
        return (self.target / self.entry - 1.0) * 100.0

    @property
    def downside_pct(self) -> float:
        return (self.stop / self.entry - 1.0) * 100.0


PREDICTIONS: list[Prediction] = [
    Prediction("ENAUSDT", 0.12367, 0.17, 0.098, "high"),
    Prediction("PENDLEUSDT", 1.4461, 1.95, 1.25, "high"),
    Prediction("ZECUSDT", 334.48, 420.0, 285.0, "high"),
    Prediction("HYPEUSDT", 44.804, 58.0, 38.0, "medium"),
    Prediction("TAOUSDT", 252.75, 320.0, 218.0, "medium"),
    Prediction("SUIUSDT", 0.9876, 1.25, 0.85, "medium"),
    Prediction("RENDERUSDT", 1.8415, 2.50, 1.60, "medium"),
    Prediction("POPCATUSDT", 0.06469, 0.095, 0.055, "speculative"),
]

NEGATIVE_BETS: list[Prediction] = [
    Prediction("WLDUSDT", 0.2833, 0.33, 0.24, "negative"),
    Prediction("ARBUSDT", 0.13059, 0.15, 0.11, "negative"),
    Prediction("JUPUSDT", 0.18547, 0.21, 0.16, "negative"),
]

BASELINE_DATE = "2026-04-18"
TARGET_DATE = "2026-05-02"


def http_get_json(path: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"{BYBIT_BASE}{path}?{qs}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode())


def fetch_ticker(symbol: str) -> dict | None:
    data = http_get_json(
        "/v5/market/tickers",
        {"category": "linear", "symbol": symbol},
    )
    lst = data.get("result", {}).get("list", [])
    return lst[0] if lst else None


def fetch_klines(symbol: str, start_ms: int, end_ms: int | None = None) -> list[list]:
    """Daily klines between start_ms and end_ms."""
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": "D",
        "start": start_ms,
        "limit": 60,
    }
    if end_ms:
        params["end"] = end_ms
    data = http_get_json("/v5/market/kline", params)
    return data.get("result", {}).get("list", [])


def date_to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fmt_pct(pct: float) -> str:
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def evaluate(p: Prediction, current: float, max_since: float | None, min_since: float | None) -> dict:
    pnl_pct = (current / p.entry - 1.0) * 100.0
    hit_target = max_since is not None and max_since >= p.target
    hit_stop = min_since is not None and min_since <= p.stop
    if hit_target and hit_stop:
        status = "BOTH_HIT"
    elif hit_target:
        status = "WIN"
    elif hit_stop:
        status = "LOSS"
    else:
        status = "OPEN"
    return {
        "pnl_pct": pnl_pct,
        "hit_target": hit_target,
        "hit_stop": hit_stop,
        "status": status,
    }


def run_snapshot(save: bool) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n== Snapshot @ {ts} ==\n")
    rows = []
    for p in PREDICTIONS + NEGATIVE_BETS:
        t = fetch_ticker(p.symbol)
        if not t:
            print(f"  {p.symbol:12s}  NOT FOUND")
            continue
        price = float(t["lastPrice"])
        pnl = (price / p.entry - 1.0) * 100.0
        bar = "█" * min(int(abs(pnl) / 2), 20)
        direction = "↑" if pnl >= 0 else "↓"
        tag = f"[{p.confidence}]"
        print(
            f"  {p.symbol:12s} {tag:14s}  entry={p.entry:<10.5g}  now={price:<10.5g}  "
            f"{direction} {fmt_pct(pnl):>8s}  target={p.target:<8.5g}  stop={p.stop:<8.5g}  {bar}"
        )
        rows.append({
            "timestamp": ts,
            "symbol": p.symbol,
            "entry": p.entry,
            "price": price,
            "pnl_pct": round(pnl, 2),
            "target": p.target,
            "stop": p.stop,
            "confidence": p.confidence,
        })
        time.sleep(0.1)

    if save and rows:
        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        write_header = not CSV_PATH.exists()
        with CSV_PATH.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            if write_header:
                w.writeheader()
            w.writerows(rows)
        print(f"\n  saved → {CSV_PATH}")


def run_verdict(since_date: str, until_date: str | None = None) -> None:
    start_ms = date_to_ms(since_date)
    if until_date:
        end_ms = date_to_ms(until_date) + 24 * 3600 * 1000
    else:
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    print(f"\n== Verdict {since_date} → {until_date or 'now'} ==\n")
    print(f"{'symbol':<12} {'entry':>9} {'max':>9} {'min':>9} {'now':>9} {'pnl':>8} {'status':>10}")
    print("-" * 80)
    wins = 0
    losses = 0
    open_count = 0
    total_pnl = 0.0
    for p in PREDICTIONS:
        t = fetch_ticker(p.symbol)
        if not t:
            print(f"  {p.symbol:12s}  NOT FOUND")
            continue
        price = float(t["lastPrice"])
        klines = fetch_klines(p.symbol, start_ms, end_ms)
        if klines:
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            max_p = max(highs)
            min_p = min(lows)
        else:
            max_p = min_p = price
        ev = evaluate(p, price, max_p, min_p)
        print(
            f"{p.symbol:<12} {p.entry:>9.4g} {max_p:>9.4g} {min_p:>9.4g} "
            f"{price:>9.4g} {fmt_pct(ev['pnl_pct']):>8s} {ev['status']:>10}"
        )
        total_pnl += ev["pnl_pct"]
        if ev["status"] == "WIN":
            wins += 1
        elif ev["status"] == "LOSS":
            losses += 1
        else:
            open_count += 1
        time.sleep(0.1)

    n = len(PREDICTIONS)
    print("-" * 80)
    print(f"  WIN: {wins}/{n}  LOSS: {losses}/{n}  OPEN: {open_count}/{n}")
    print(f"  Avg mid-PnL (mark-to-market): {fmt_pct(total_pnl / n)}")

    print("\n== Negative bets (should NOT rise >15%) ==\n")
    neg_correct = 0
    for p in NEGATIVE_BETS:
        t = fetch_ticker(p.symbol)
        if not t:
            continue
        price = float(t["lastPrice"])
        pnl = (price / p.entry - 1.0) * 100.0
        correct = pnl < 15.0
        neg_correct += int(correct)
        mark = "✓" if correct else "✗"
        print(f"  {p.symbol:12s} entry={p.entry:<8.5g} now={price:<8.5g} {fmt_pct(pnl):>8s}  {mark}")
    print(f"\n  Negative-bet accuracy: {neg_correct}/{len(NEGATIVE_BETS)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="save snapshot to CSV")
    ap.add_argument("--verdict", action="store_true", help="final verdict with high/low since baseline")
    ap.add_argument("--since", default=BASELINE_DATE, help="verdict start date YYYY-MM-DD")
    ap.add_argument("--until", default=None, help="verdict end date YYYY-MM-DD")
    args = ap.parse_args()
    if args.verdict:
        run_verdict(args.since, args.until)
    else:
        run_snapshot(args.save)


if __name__ == "__main__":
    main()
