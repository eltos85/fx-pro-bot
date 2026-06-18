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

ВАЖНО про атрибуцию (исправлено 2026-06-15): ProtoOADeal НЕ содержит label
(label только у ProtoOAPosition.tradeData у открытых). Раньше скрипт считал,
что ВСЕ deal'ы счёта = fx_momentum_bot (fx_ai_trader якобы 0 сделок). Это
СЛОМАЛОСЬ: после аудит-правок fx_ai_trader активно торгует BRENT/NAT.GAS/gold
на том же cTrader-счёте → его сделки попадали в momentum-стату (06-15:
BRENT/NAT.GAS −$5.48 ошибочно атрибутированы momentum).

Теперь атрибутируем по ТОРГОВОЙ ВСЕЛЕННОЙ momentum из его же конфига:
FX-мажоры (settings.symbols), резолвленные в cTrader-имена. Deal'ы вне
этой вселенной (BRENT/NAT.GAS/oil/indices/gold) идут в отдельную секцию
«другие боты» и в momentum-итог НЕ входят.

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

    # Торговая вселенная momentum (его конфиг) → cTrader symbol_ids.
    # Только эти deal'ы принадлежат momentum; остальное — другие боты.
    momentum_sids: set[int] = set()
    momentum_names: set[str] = set()
    for yf_sym in settings.symbols:
        info = symbols.resolve_yfinance(yf_sym)
        if info is not None:
            momentum_sids.add(info.symbol_id)
            momentum_names.add(info.name)
    print(f"Momentum trading universe: {sorted(momentum_names)}\n")

    def is_momentum(sid: int) -> bool:
        return sid in momentum_sids

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
    other_rows: list[dict] = []  # сделки других ботов (вне вселенной momentum)
    for pid in sorted(by_pos, key=lambda p: int(getattr(by_pos[p][0], "executionTimestamp", 0))):
        ds = sorted(by_pos[pid], key=lambda x: int(getattr(x, "executionTimestamp", 0)))
        opening = ds[0]
        closings = [x for x in ds if x.HasField("closePositionDetail")]
        sid = int(getattr(opening, "symbolId", 0))
        sname = sym_name(sid)
        mine = is_momentum(sid)
        tag = "" if mine else " [OTHER-BOT]"
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
                  f"{gross:>+8.2f} {swap:>+6.2f} {comm:>+6.2f} {net:>+8.2f} {status}{tag}")
            (rows if mine else other_rows).append({"sym": sname, "net": net})
        else:
            print(f"{pid:<11} {sname:<9} {side:<5} {opened_iso:<11} {'-':<11} "
                  f"{'(open)':>8} {'-':>6} {'-':>6} {'-':>8} {status}{tag}")

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

    print("\n=== MOMENTUM closed trades (broker NET, ground truth) ===")
    by_sym: dict[str, list[float]] = {}
    for r in rows:
        by_sym.setdefault(r["sym"], []).append(r["net"])
    for s in sorted(by_sym):
        agg(s, by_sym[s])
    agg("TOTAL", [r["net"] for r in rows])

    if other_rows:
        print("\n=== EXCLUDED — другие боты (НЕ momentum, вне его вселенной) ===")
        other_by_sym: dict[str, list[float]] = {}
        for r in other_rows:
            other_by_sym.setdefault(r["sym"], []).append(r["net"])
        for s in sorted(other_by_sym):
            agg(s, other_by_sym[s])
        agg("OTHER TOTAL", [r["net"] for r in other_rows])

    print("\n=== Currently OPEN on broker ===")
    for pid in sorted(open_meta):
        m = open_meta[pid]
        print(f"  pid={pid}  {m['symbol']:<9} {m['side']:<5} label='{m['label']}'")

    client.stop() if hasattr(client, "stop") else None
    return 0


if __name__ == "__main__":
    sys.exit(main())
