"""Посуточный broker-truth P&L по fx_momentum_bot (read-only).

Переиспользует auth momentum-бота, тянет get_deal_list, атрибуция по ТОРГОВОЙ
ВСЕЛЕННОЙ momentum (settings.symbols → cTrader symbol_ids) — сделки fx_ai_trader
(BRENT/NG/gold) исключаются. Группировка по ДАТЕ ЗАКРЫТИЯ позиции (UTC).

Запуск (внутри fx-momentum-bot контейнера, fx_ai_trader предварительно остановлен):

    docker cp scripts/_momentum_perday_pnl.py fx-pro-bot-fx-momentum-bot-1:/tmp/
    docker exec fx-pro-bot-fx-momentum-bot-1 python /tmp/_momentum_perday_pnl.py 2026-06-05
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from datetime import datetime, timezone


def main() -> int:
    start_date = sys.argv[1] if len(sys.argv) > 1 else "2026-06-05"
    from_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    from_ms = int(from_dt.timestamp() * 1000)
    now_ms = int(time.time() * 1000)

    from fx_momentum_bot.app.main import _build_executor
    from fx_momentum_bot.config.settings import MomentumBotSettings

    settings = MomentumBotSettings()
    executor = _build_executor(settings)
    if executor is None:
        print("ERROR: executor is None (trading disabled / no token)", file=sys.stderr)
        return 1
    client = executor._client  # noqa: SLF001
    symbols = executor.symbols

    def sym_name(sid: int) -> str:
        info = symbols.get_by_id(sid)
        return info.name if info else f"id={sid}"

    momentum_sids: set[int] = set()
    for yf_sym in settings.symbols:
        info = symbols.resolve_yfinance(yf_sym)
        if info is not None:
            momentum_sids.add(info.symbol_id)
    print(f"Momentum universe: {sorted({symbols.get_by_id(s).name for s in momentum_sids if symbols.get_by_id(s)})}")
    print(f"Window: {from_dt.date()} → now\n")

    resp = client.get_deal_list(from_ts=from_ms, to_ts=now_ms, max_rows=2000)
    deals = list(resp.deal) if hasattr(resp, "deal") else []
    by_pos: dict[int, list] = defaultdict(list)
    for d in deals:
        by_pos[int(d.positionId)].append(d)

    # per-day net (по дате закрытия), per-symbol breakdown
    per_day: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for pid in sorted(by_pos, key=lambda p: int(getattr(by_pos[p][0], "executionTimestamp", 0))):
        ds = sorted(by_pos[pid], key=lambda x: int(getattr(x, "executionTimestamp", 0)))
        opening = ds[0]
        sid = int(getattr(opening, "symbolId", 0))
        if sid not in momentum_sids:
            continue  # чужой бот
        closings = [x for x in ds if x.HasField("closePositionDetail")]
        if not closings:
            continue  # ещё открыта
        net = 0.0
        close_ts = 0
        for c in closings:
            cpd = c.closePositionDetail
            div = 10 ** (int(cpd.moneyDigits) if cpd.moneyDigits else 2)
            net += cpd.grossProfit / div + cpd.swap / div + cpd.commission / div
            close_ts = int(getattr(c, "executionTimestamp", 0))
        day = datetime.fromtimestamp(close_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        per_day[day].append((net, sym_name(sid)))

    print(f"{'date':<12} {'n':>3} {'W':>3} {'L':>3} {'WR':>5} {'net$':>9} {'best':>7} {'worst':>7}  symbols")
    print("-" * 80)
    grand_n = grand_w = grand_l = 0
    grand_net = 0.0
    best_day = None
    best_day_net = -1e9
    for day in sorted(per_day):
        trades = per_day[day]
        nets = [t[0] for t in trades]
        w = sum(1 for v in nets if v > 0)
        l = sum(1 for v in nets if v < 0)
        net = sum(nets)
        syms = sorted({t[1] for t in trades})
        print(f"{day:<12} {len(nets):>3} {w:>3} {l:>3} {100*w//len(nets):>4}% {net:>+9.2f} "
              f"{max(nets):>+7.2f} {min(nets):>+7.2f}  {','.join(syms)}")
        grand_n += len(nets); grand_w += w; grand_l += l; grand_net += net
        if net > best_day_net:
            best_day_net = net; best_day = day
    print("-" * 80)
    print(f"{'TOTAL':<12} {grand_n:>3} {grand_w:>3} {grand_l:>3} "
          f"{(100*grand_w//grand_n) if grand_n else 0:>4}% {grand_net:>+9.2f}")
    print(f"\nBEST DAY: {best_day}  net=${best_day_net:+.2f}")
    if best_day:
        print("  trades:")
        for net, sym in per_day[best_day]:
            print(f"    {sym:<9} {net:>+7.2f}")

    client.stop() if hasattr(client, "stop") else None
    return 0


if __name__ == "__main__":
    sys.exit(main())
