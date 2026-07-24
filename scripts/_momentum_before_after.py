"""Сравнение momentum-бота до/после правок после 29 июня (read-only).

Берёт cTrader deal-list (ground truth P&L) + momentum_decisions (skip-reasons
входа), режет по timestamp'ам коммитов-правок и сравнивает периоды:
- частота входов, WR, avg/net P&L;
- что блокировали гарды (skip:friday_flat_window / skip:already_open /
  skip:off_session / skip:event_guard / same_direction).

Запуск (внутри fx-momentum-bot контейнера, fx_ai_trader остановлен):

    docker cp scripts/_momentum_before_after.py fx-pro-bot-fx-momentum-bot-1:/tmp/
    docker exec fx-pro-bot-fx-momentum-bot-1 python /tmp/_momentum_before_after.py
"""
from __future__ import annotations

import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

# Коммиты-правки (UTC), режущие периоды. MSK(+0300) → UTC: -3h.
PERIODS = [
    ("P0 до правок            ", None, "2026-07-02 11:28"),   # до 9804e8e
    ("P1 friday-block         ", "2026-07-02 11:28", "2026-07-10 08:41"),  # 9804e8e → 1af1022
    ("P2 per-symbol guard v1  ", "2026-07-10 08:41", "2026-07-13 07:08"),  # 1af1022 → 83f8a2a
    ("P3 guard re-applied     ", "2026-07-13 07:08", "2026-07-15 07:05"),  # 83f8a2a → c0d530f
    ("P4 profit-protect       ", "2026-07-15 07:05", "2026-07-22 09:25"),  # c0d530f → revert
    ("P5 после отмены protect ", "2026-07-22 09:25", "2026-07-24 08:27"),  # revert → 60f386c/a523e6f
    ("P6 exit-hyst+NY+ADX+gap ", "2026-07-24 08:27", None),               # 60f386c/a523e6f deploy
]


def _parse(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def main() -> int:
    from fx_momentum_bot.app.main import _build_executor
    from fx_momentum_bot.config.settings import MomentumBotSettings

    settings = MomentumBotSettings()
    executor = _build_executor(settings)
    if executor is None:
        print("ERROR: executor is None", file=sys.stderr); return 1
    client = executor._client  # noqa: SLF001
    symbols = executor.symbols
    momentum_sids = set()
    for yf in settings.symbols:
        info = symbols.resolve_yfinance(yf)
        if info: momentum_sids.add(info.symbol_id)

    # deal-list (P&L)
    from_ms = int(datetime(2026, 6, 5, tzinfo=timezone.utc).timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    resp = client.get_deal_list(from_ts=from_ms, to_ts=now_ms, max_rows=2000)
    deals = list(resp.deal) if hasattr(resp, "deal") else []
    by_pos: dict[int, list] = defaultdict(list)
    for d in deals:
        by_pos[int(d.positionId)].append(d)

    # per-trade: open_ts, net
    trades: list[tuple[float, str, str, float]] = []  # (open_ts, sym, side, net)
    for pid, ds in by_pos.items():
        ds.sort(key=lambda x: int(getattr(x, "executionTimestamp", 0)))
        opening = ds[0]
        sid = int(getattr(opening, "symbolId", 0))
        if sid not in momentum_sids:
            continue
        closings = [x for x in ds if x.HasField("closePositionDetail")]
        if not closings:
            continue
        net = 0.0
        for c in closings:
            cpd = c.closePositionDetail
            div = 10 ** (int(cpd.moneyDigits) if cpd.moneyDigits else 2)
            net += cpd.grossProfit / div + cpd.swap / div + cpd.commission / div
        open_ts = int(getattr(opening, "executionTimestamp", 0)) / 1000
        sname = symbols.get_by_id(sid).name if symbols.get_by_id(sid) else f"id={sid}"
        side = "BUY" if int(getattr(opening, "tradeSide", 0)) == 1 else "SELL"
        trades.append((open_ts, sname, side, net))

    # momentum_decisions (skip-reasons)
    db = sqlite3.connect("/data/momentum_bot.sqlite")
    dec_rows = db.execute(
        "select created_at, symbol, direction, executed, note from momentum_decisions"
    ).fetchall()

    def period_of(ts: float) -> int:
        for i, (_, start, end) in enumerate(PERIODS):
            if start and ts < _parse(start):
                continue
            if end and ts >= _parse(end):
                continue
            return i
        return 0

    # Aggregate trades by period
    by_p: dict[int, list[tuple[str, str, float]]] = defaultdict(list)
    for open_ts, sym, side, net in trades:
        by_p[period_of(open_ts)].append((sym, side, net))

    print("=== PER-TRADE P&L по периодам (по дате ОТКРЫТИЯ сделки) ===")
    print(f"{'период':<26} {'n':>3} {'W':>3} {'L':>3} {'WR':>5} {'net$':>9} {'avg$':>7} {'best':>7} {'worst':>8}")
    print("-" * 90)
    for i, (label, _, _) in enumerate(PERIODS):
        ts = by_p.get(i, [])
        nets = [t[2] for t in ts]
        if not nets:
            print(f"{label:<26} {0:>3} {0:>3} {0:>3} {'-':>5} {'$+0.00':>9}"); continue
        w = sum(1 for v in nets if v > 0); l = sum(1 for v in nets if v < 0)
        net = sum(nets)
        print(f"{label:<26} {len(nets):>3} {w:>3} {l:>3} {100*w//len(nets):>4}% "
              f"{net:>+9.2f} {net/len(nets):>+7.2f} {max(nets):>+7.2f} {min(nets):>+8.2f}")

    # Skip-reasons by period (по created_at решения)
    print("\n=== SKIP-REASONS входа по периодам (momentum_decisions) ===")
    print(f"{'период':<26} {'dec':>6} {'exec':>4} {'friday':>7} {'already':>8} {'offses':>7} {'event':>6} {'same':>5} {'flat':>5} {'ok':>4}")
    print("-" * 95)
    for i, (label, _, _) in enumerate(PERIODS):
        cnt = Counter()
        executed = 0
        total = 0
        for created_at, symbol, direction, ex, note in dec_rows:
            try:
                ts = datetime.strptime(created_at, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if period_of(ts) != i:
                continue
            total += 1
            if ex:
                executed += 1
            n = note or ""
            if "friday_flat" in n: cnt["friday"] += 1
            elif "already_open" in n: cnt["already"] += 1
            elif "off_session" in n: cnt["offses"] += 1
            elif "event" in n: cnt["event"] += 1
            elif n == "same_direction": cnt["same"] += 1
            elif n == "flat": cnt["flat"] += 1
            elif "live_open:ok" in n: cnt["ok"] += 1
        print(f"{label:<26} {total:>6} {executed:>4} {cnt['friday']:>7} {cnt['already']:>8} "
              f"{cnt['offses']:>7} {cnt['event']:>6} {cnt['same']:>5} {cnt['flat']:>5} {cnt['ok']:>4}")

    client.stop() if hasattr(client, "stop") else None
    return 0


if __name__ == "__main__":
    sys.exit(main())
