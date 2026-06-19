"""Read-only dump момент-сделок с брокера в JSON (для локального MFE-анализа).

Печатает в stdout одну JSON-строку: список закрытых сделок momentum-вселенной
(FX-мажоры + VP-золото) с момента указанной даты. Поля на сделку:
  pid, symbol, side, entry_price, exit_price, gross, net, open_ts_ms,
  close_ts_ms, volume_lots, is_momentum

Цены — ProtoOADeal.executionPrice (реальные, не scaled). $-множитель для MFE
калибруется локально из gross/(ход цены), pip-value тут не нужен.

Запуск (в fx-momentum-bot контейнере, fx-ai-trader остановлен под лимит коннектов):
    docker exec fx-pro-bot-fx-momentum-bot-1 python /tmp/_momentum_trade_dump.py 2026-06-11
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone


def _scale_price(raw):
    if isinstance(raw, (int, float)) and abs(raw) > 1_000_000:
        return float(raw) / 100_000.0
    return float(raw)


def main() -> int:
    start_date = sys.argv[1] if len(sys.argv) > 1 else "2026-06-11"
    from_ms = int(
        datetime.strptime(start_date, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )
    now_ms = int(time.time() * 1000)

    from fx_momentum_bot.app.main import _build_executor
    from fx_momentum_bot.config.settings import MomentumBotSettings

    settings = MomentumBotSettings()
    executor = _build_executor(settings)
    if executor is None:
        print("ERR executor None", file=sys.stderr)
        return 1
    client = executor._client  # noqa: SLF001
    symbols = executor.symbols

    def sym_name(sid: int) -> str:
        info = symbols.get_by_id(sid)
        return info.name if info else f"id={sid}"

    momentum_sids = set()
    for yf_sym in settings.all_symbols:
        info = symbols.resolve_yfinance(yf_sym)
        if info is not None:
            momentum_sids.add(info.symbol_id)

    resp = client.get_deal_list(from_ts=from_ms, to_ts=now_ms, max_rows=2000)
    deals = list(resp.deal) if hasattr(resp, "deal") else []

    by_pos: dict[int, list] = {}
    for d in deals:
        by_pos.setdefault(int(d.positionId), []).append(d)

    out = []
    for pid, ds in by_pos.items():
        ds.sort(key=lambda x: int(getattr(x, "executionTimestamp", 0)))
        opening = ds[0]
        closings = [x for x in ds if x.HasField("closePositionDetail")]
        if not closings:
            continue  # ещё открыта
        sid = int(getattr(opening, "symbolId", 0))
        gross = 0.0
        for c in closings:
            cpd = c.closePositionDetail
            div = 10 ** (int(cpd.moneyDigits) if cpd.moneyDigits else 2)
            gross += cpd.grossProfit / div
        net = 0.0
        for c in closings:
            cpd = c.closePositionDetail
            div = 10 ** (int(cpd.moneyDigits) if cpd.moneyDigits else 2)
            net += (cpd.grossProfit + cpd.swap + cpd.commission) / div
        last_close = closings[-1]
        vol_units = int(getattr(opening, "filledVolume", 0))
        out.append(
            {
                "pid": pid,
                "symbol": sym_name(sid),
                "side": "BUY" if int(getattr(opening, "tradeSide", 0)) == 1 else "SELL",
                "entry_price": _scale_price(getattr(opening, "executionPrice", 0)),
                "exit_price": _scale_price(getattr(last_close, "executionPrice", 0)),
                "gross": round(gross, 2),
                "net": round(net, 2),
                "open_ts_ms": int(getattr(opening, "executionTimestamp", 0)),
                "close_ts_ms": int(getattr(last_close, "executionTimestamp", 0)),
                "volume_units": vol_units,
                "is_momentum": sid in momentum_sids,
            }
        )

    print("JSON_START")
    print(json.dumps(out))
    print("JSON_END")
    if hasattr(client, "stop"):
        client.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
