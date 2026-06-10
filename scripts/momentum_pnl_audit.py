"""Broker-truth P&L аудит для fx_momentum_bot (cTrader deal history).

Источник истины — cTrader API ``get_deal_list`` (правило ctrader-pnl.mdc),
НЕ локальная БД (momentum хранит только факт открытия, без PnL/закрытия).

Что делает (read-only):
1. Переиспользует ``_build_executor`` momentum'а (тот же auth/token-service,
   тот же client_id) → подключённый ``CTraderClient``.
2. ``get_deal_list(from_ts, to_ts)`` за окно (по умолчанию с 2026-06-05).
3. Группирует deals по ``positionId`` → net PnL = Σ(gross+swap+commission)
   по closing-deals (как в cTrader History; правило: PnL уже NET).
4. Резолвит symbolId → имя через ``SymbolCache``.
5. Сводит winrate / profit factor / expectancy ($/сделку) по инструментам
   и итог. Также печатает текущие open-позиции (reconcile) с label.

ВАЖНО про атрибуцию: ProtoOADeal НЕ содержит label (label только у
ProtoOAPosition.tradeData у открытых). Closed-сделки атрибутируем по факту:
fx_ai_trader за период сделал 0 сделок, advisor profile=disabled → ВСЕ deal'ы
счёта принадлежат fx_momentum_bot. Для open-позиций label печатаем явно.

Запуск (внутри fx-momentum-bot контейнера, ai-trader предварительно
остановлен чтобы не превысить лимит 2 коннекта/app):

    docker cp scripts/momentum_pnl_audit.py fx-pro-bot-fx-momentum-bot-1:/tmp/
    docker exec fx-pro-bot-fx-momentum-bot-1 python /tmp/momentum_pnl_audit.py 2026-06-05
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone


def _ts_to_iso(ts_ms: int) -> str:
    if not ts_ms:
        return "-"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


def main() -> int:
    start_date = sys.argv[1] if len(sys.argv) > 1 else "2026-06-05"
    from_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    from_ms = int(from_dt.timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    hours = (now_ms - from_ms) / 3_600_000
    print(f"=== fx_momentum_bot broker P&L audit: {from_dt.date()} → now ({hours:.1f}h) ===\n")

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

    # 1. Deal list
    resp = client.get_deal_list(from_ts=from_ms, to_ts=now_ms, max_rows=2000)
    deals = list(resp.deal) if hasattr(resp, "deal") else []
    print(f"Broker deals returned: {len(deals)}\n")

    # 2. Group by positionId
    by_pos: dict[int, list] = {}
    for d in deals:
        by_pos.setdefault(int(d.positionId), []).append(d)

    # 3. Currently open (reconcile) for label + open marker
    rec = client.reconcile()
    open_meta: dict[int, dict] = {}
    for p in list(rec.position):
        pid = int(p.positionId)
        td = p.tradeData if p.HasField("tradeData") else None
        open_meta[pid] = {
            "symbol": sym_name(int(td.symbolId)) if td and td.HasField("symbolId") else "?",
            "side": ("BUY" if td and td.HasField("tradeSide") and int(td.tradeSide) == 1 else "SELL"),
            "label": str(td.label) if td and td.HasField("label") else "",
        }

    # 4. Per-position net PnL
    print(f"{'pid':<11} {'symbol':<9} {'side':<5} {'opened':<11} {'closed':<11} "
          f"{'gross':>8} {'swap':>6} {'comm':>6} {'NET$':>8} status")
    print("-" * 96)
    rows: list[dict] = []
    for pid in sorted(by_pos, key=lambda p: int(getattr(by_pos[p][0], "executionTimestamp", 0))):
        ds = sorted(by_pos[pid], key=lambda x: int(getattr(x, "executionTimestamp", 0)))
        opening = ds[0]
        closings = [x for x in ds if x.HasField("closePositionDetail")]
        sname = sym_name(int(getattr(opening, "symbolId", 0)))
        side = "BUY" if int(getattr(opening, "tradeSide", 0)) == 1 else "SELL"
        opened_iso = _ts_to_iso(int(getattr(opening, "executionTimestamp", 0)))
        gross = swap = comm = 0.0
        closed_iso = "-"
        for c in closings:
            cpd = c.closePositionDetail
            div = 10 ** (int(cpd.moneyDigits) if cpd.moneyDigits else 2)
            gross += cpd.grossProfit / div
            swap += cpd.swap / div
            comm += cpd.commission / div
            closed_iso = _ts_to_iso(int(getattr(c, "executionTimestamp", 0)))
        is_open = pid in open_meta
        net = (gross + swap + comm) if closings else 0.0
        status = "OPEN" if is_open else "closed"
        if closings:
            print(f"{pid:<11} {sname:<9} {side:<5} {opened_iso:<11} {closed_iso:<11} "
                  f"{gross:>+8.2f} {swap:>+6.2f} {comm:>+6.2f} {net:>+8.2f} {status}")
            rows.append({"sym": sname, "net": net})
        else:
            print(f"{pid:<11} {sname:<9} {side:<5} {opened_iso:<11} {'-':<11} "
                  f"{'(open)':>8} {'-':>6} {'-':>6} {'-':>8} {status}")

    # 5. Aggregates by instrument + total
    def agg(label: str, vals: list[float]) -> None:
        if not vals:
            return
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v < 0]
        gross_w = sum(wins)
        gross_l = -sum(losses)
        pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
        wr = len(wins) / len(vals) * 100
        net = sum(vals)
        print(f"  {label:<10} n={len(vals):>2}  W={len(wins):>2} L={len(losses):>2}  "
              f"WR={wr:>3.0f}%  net=${net:>+8.2f}  avg=${net/len(vals):>+6.2f}  "
              f"PF={pf:>5.2f}  best=${max(vals):>+6.2f} worst=${min(vals):>+6.2f}")

    print("\n=== CLOSED trades summary (broker NET, ground truth) ===")
    by_sym: dict[str, list[float]] = {}
    for r in rows:
        by_sym.setdefault(r["sym"], []).append(r["net"])
    for s in sorted(by_sym):
        agg(s, by_sym[s])
    agg("TOTAL", [r["net"] for r in rows])

    print("\n=== Currently OPEN on broker ===")
    for pid in sorted(open_meta):
        m = open_meta[pid]
        print(f"  pid={pid}  {m['symbol']:<9} {m['side']:<5} label='{m['label']}'")

    client.stop() if hasattr(client, "stop") else None
    return 0


if __name__ == "__main__":
    sys.exit(main())
