"""Broker-side audit: история всех cTrader deals за указанное окно.

Используется когда нужно убедиться, что **БД ботов не пропустила**
ни одной реальной сделки (broker-truth audit, не БД-truth).

Что делает:
1. Через ``CTraderFxAdapter`` (== тот же путь что у fx-ai-trader в
   продакшене) запрашивает ``ProtoOAGetDealListReq`` с
   ``from_ts/to_ts`` за указанное окно.
2. Группирует deals по ``positionId`` → строит полную историю каждой
   позиции (opening + closing deals, net PnL = gross - swap - commission).
3. Также делает ``ProtoOAReconcileReq`` — текущие open positions.
4. Сшивает каждый ``positionId`` с двумя локальными БД:
   - ``/data/fx_ai_trader.sqlite``
   - ``/data/advisor_stats.sqlite``
   Если в БД есть запись с этим ``broker_position_id`` — печатает
   label, иначе ``⊘ NOT IN DB`` (= orphan / другой бот / ручная).

Запуск (внутри fx-ai-trader контейнера — там есть ctrader-client):

    docker exec fx-pro-bot-fx-ai-trader-1 \
        python /tmp/fx_ai_broker_history_audit.py 2026-05-18

Read-only: не пишет ни в БД, ни на брокера.

Источники:
- cTrader Open API ProtoOADealListReq / ProtoOADeal /
  ProtoOAClosePositionDetail — структура из advisor `compare_stats.py`
  и `scripts/fx_ai_verify_pnl_from_history.py`.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

DB_PATHS = {
    "fx_ai_trader": "/data/fx_ai_trader.sqlite",
    "advisor": "/data/advisor_stats.sqlite",
}


def _scale_price(raw: int | float) -> float:
    if isinstance(raw, (int, float)) and abs(raw) > 1_000_000:
        return float(raw) / 100_000.0
    return float(raw)


def _load_db_index() -> dict[int, dict[str, str]]:
    """Returns ``{broker_position_id: {"bot": ..., "label": ..., "symbol": ..., "side": ...}}``."""
    index: dict[int, dict[str, str]] = {}

    # fx_ai_trader — единственный LLM-бот на cTrader; fx_ai_trend
    # удалён 2026-05-20 (3 paper-трейда в минус и тишина → возврат
    # на Advisor rule-based ensemble).
    for bot in ("fx_ai_trader",):
        path = DB_PATHS[bot]
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            cur = conn.execute(
                """
                SELECT broker_position_id, broker_order_label, symbol, side,
                       opened_at, closed_at, realized_pnl_usd, close_reason,
                       is_paper
                FROM positions
                WHERE broker_position_id IS NOT NULL AND broker_position_id > 0
                """
            )
            for row in cur:
                bpid = int(row[0])
                index[bpid] = {
                    "bot": bot,
                    "label": str(row[1] or ""),
                    "symbol": str(row[2]),
                    "side": str(row[3]),
                    "opened_at": str(row[4]),
                    "closed_at": str(row[5] or ""),
                    "db_pnl": row[6] if row[6] is not None else "",
                    "close_reason": str(row[7] or ""),
                    "is_paper": int(row[8]) if row[8] is not None else 0,
                }
            conn.close()
        except Exception as exc:
            print(f"WARN: can't read {path}: {exc}", file=sys.stderr)

    # advisor — другая схема
    try:
        conn = sqlite3.connect(f"file:{DB_PATHS['advisor']}?mode=ro", uri=True)
        cur = conn.execute(
            """
            SELECT broker_position_id, strategy, instrument, direction,
                   created_at, closed_at, status, exit_reason
            FROM positions
            WHERE broker_position_id > 0
            """
        )
        for row in cur:
            bpid = int(row[0])
            if bpid in index:
                continue  # fx_ai_* приоритетнее (Advisor мог записать тот же id?)
            index[bpid] = {
                "bot": "advisor",
                "label": str(row[1] or ""),
                "symbol": str(row[2]),
                "side": str(row[3]).upper(),
                "opened_at": str(row[4]),
                "closed_at": str(row[5] or ""),
                "db_pnl": "",
                "close_reason": str(row[7] or ""),
                "is_paper": 0,
            }
        conn.close()
    except Exception as exc:
        print(f"WARN: can't read advisor db: {exc}", file=sys.stderr)

    return index


def _ts_to_iso(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} YYYY-MM-DD  (start of audit window, UTC)")
        return 1
    start_date = sys.argv[1]
    try:
        from_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"Bad date: {start_date}, expected YYYY-MM-DD")
        return 1
    from_ms = int(from_dt.timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    hours = (now_ms - from_ms) / 3_600_000
    print(f"=== Audit window: {from_dt.isoformat()} → now ({hours:.1f}h) ===\n")

    # 1. Подключение
    from fx_ai_trader.config.settings import AiFxTraderSettings
    from fx_ai_trader.trading.client_adapter import CTraderFxAdapter

    settings = AiFxTraderSettings()
    adapter = CTraderFxAdapter(settings)
    adapter.start(timeout=30.0)
    client = adapter._client  # noqa: SLF001
    if client is None:
        print("ERROR: adapter._client is None", file=sys.stderr)
        return 1

    # 2. Symbol resolve (для красивого вывода). cTrader catalog может
    # иметь алиасы (например XAUUSD и GC=F → один и тот же symbolId
    # если FxPro листит только один). Поэтому печатаем ВСЕ имена которые
    # мапятся в один и тот же id, чтобы не скрывать identity путаницу.
    name_by_id: dict[int, list[str]] = {}
    for name in ("XAUUSD", "BZ=F", "NG=F", "GC=F"):
        info = adapter.get_symbol_info(name)
        if info is not None:
            name_by_id.setdefault(info.symbol_id, []).append(name)
    symbol_id_to_name = {sid: "/".join(names) for sid, names in name_by_id.items()}
    print("=== Symbol resolve (internal_name → cTrader symbolId) ===")
    for sid, names in sorted(name_by_id.items()):
        print(f"  symbolId={sid}  ←  {names}")
    print()

    # 3. Deal list
    resp = client.get_deal_list(from_ts=from_ms, to_ts=now_ms, max_rows=2000)
    deals = list(resp.deal) if hasattr(resp, "deal") else []
    print(f"=== Broker deals returned: {len(deals)} ===\n")

    # 4. Group by positionId
    by_pos: dict[int, list] = {}
    for d in deals:
        by_pos.setdefault(int(d.positionId), []).append(d)

    # 5. Reconcile — open positions сейчас
    rec = client.reconcile()
    open_pids = set()
    open_meta: dict[int, dict] = {}
    for p in list(rec.position):
        pid = int(p.positionId)
        open_pids.add(pid)
        td = p.tradeData if p.HasField("tradeData") else None
        sym_id = int(td.symbolId) if td and td.HasField("symbolId") else 0
        side_raw = int(td.tradeSide) if td and td.HasField("tradeSide") else 0
        side = "BUY" if side_raw == 1 else "SELL"
        label = str(td.label) if td and td.HasField("label") else ""
        open_meta[pid] = {
            "symbol": symbol_id_to_name.get(sym_id, f"id={sym_id}"),
            "side": side,
            "label": label,
        }

    # 6. БД-индекс
    db_idx = _load_db_index()

    # 7. Сшивка
    print(f"{'pid':<11} {'symbol':<7} {'side':<5} {'opened':<11} {'closed':<11} "
          f"{'gross':>8} {'swap':>6} {'comm':>6} {'NET':>8} {'bot':<14} status")
    print("─" * 130)

    pids_sorted = sorted(by_pos.keys(), key=lambda pid: int(by_pos[pid][0].executionTimestamp or 0))
    summary_by_bot: dict[str, list[float]] = {}

    for pid in pids_sorted:
        ds = by_pos[pid]
        # opening = первый deal (по executionTimestamp); closing = те у кого есть closePositionDetail
        ds.sort(key=lambda x: int(getattr(x, "executionTimestamp", 0)))
        opening = ds[0]
        closings = [x for x in ds if x.HasField("closePositionDetail")]
        sym_name = symbol_id_to_name.get(int(opening.symbolId), f"id={opening.symbolId}")
        open_side_raw = int(getattr(opening, "tradeSide", 0))
        open_side = "BUY" if open_side_raw == 1 else "SELL"
        opened_iso = _ts_to_iso(int(getattr(opening, "executionTimestamp", 0)))

        # PnL net по всем closing deals
        gross_total = 0.0
        swap_total = 0.0
        comm_total = 0.0
        closed_iso = ""
        broker_label_open = ""
        for c in closings:
            cpd = c.closePositionDetail
            md = int(cpd.moneyDigits) if cpd.moneyDigits else 2
            div = 10 ** md
            gross_total += cpd.grossProfit / div
            swap_total += cpd.swap / div
            comm_total += cpd.commission / div
            closed_iso = _ts_to_iso(int(getattr(c, "executionTimestamp", 0)))

        # cTrader ProtoOADeal не содержит label (label есть только у
        # ProtoOAPosition.tradeData). Для open positions берём label
        # из reconcile; для closed — единственный источник наша БД
        # (через broker_position_id match).
        broker_label = ""
        if pid in open_meta:
            broker_label = open_meta[pid]["label"]

        # Match c DB
        db_info = db_idx.get(pid)
        if db_info is not None:
            db_bot = db_info["bot"]
            db_label = db_info["label"]
        else:
            db_bot = "⊘ NOT IN DB"
            db_label = "?"

        if pid in open_pids:
            status = "OPEN"
        else:
            status = "closed"

        # NET = то что брокер реально списывает (как в cTrader app History).
        # swap/commission приходят с знаком от cTrader; берём как есть.
        net = (gross_total + swap_total + comm_total) if closings else 0.0
        if closings:
            gross_str = f"{gross_total:+8.2f}"
            swap_str = f"{swap_total:+6.2f}"
            comm_str = f"{comm_total:+6.2f}"
            net_str = f"{net:+8.2f}"
        else:
            gross_str = "  (open)"
            swap_str = comm_str = net_str = "      -"
        print(
            f"{pid:<11} {sym_name:<7} {open_side:<5} {opened_iso:<11} "
            f"{closed_iso or '-':<11} {gross_str:>8} {swap_str:>6} {comm_str:>6} "
            f"{net_str:>8} {db_bot:<14} {status}"
        )
        # Сумма для отчёта (по NET, как у брокера)
        if closings:
            summary_by_bot.setdefault(db_bot, []).append(net)

    print()
    print("=== SUMMARY by bot (broker NET PnL, как в cTrader app History) ===")
    for bot, vals in sorted(summary_by_bot.items()):
        wins = sum(1 for v in vals if v > 0)
        loss = sum(1 for v in vals if v < 0)
        total = sum(vals)
        print(
            f"  {bot:<14}: n={len(vals):>2}  W={wins:>2}  L={loss:>2}  "
            f"net=${total:+8.2f}  avg=${(total / len(vals)) if vals else 0:+6.2f}"
        )

    # Orphans = OPEN на брокере но не в БД
    print()
    print("=== Currently OPEN on broker ===")
    for pid in sorted(open_pids):
        meta = open_meta.get(pid, {})
        db_info = db_idx.get(pid)
        marker = db_info["bot"] if db_info else "⊘ ORPHAN"
        print(
            f"  pid={pid}  {meta.get('symbol','?'):<7} {meta.get('side','?'):<5} "
            f"broker_label='{meta.get('label','')}'   →   {marker}"
        )

    adapter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
