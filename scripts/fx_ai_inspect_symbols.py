"""Read-only диагностика — сырой ProtoOASymbol из cTrader Open API.

Запускать ОДНОРАЗОВО внутри fx-ai-trader контейнера:

    docker exec fx-pro-bot-fx-ai-trader-1 python scripts/fx_ai_inspect_symbols.py

Печатает все поля ProtoOASymbol для XAUUSD и BRENT, чтобы сверить с
официальными contract specs FxPro перед правкой pip_value_per_lot().

Никаких побочных эффектов — не открывает позиций, не пишет в БД.
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.trading.client_adapter import CTraderFxAdapter


def _dump_proto_message(obj) -> dict:
    """Все скалярные поля ProtoOA*-сообщения как dict."""
    out = {}
    if hasattr(obj, "ListFields"):
        for descriptor, value in obj.ListFields():
            out[descriptor.name] = value
    return out


def main() -> int:
    settings = AiFxTraderSettings()
    adapter = CTraderFxAdapter(settings)
    print(f"=== Connecting cTrader (account {settings.ctrader_account_id}) ===")
    adapter.start(timeout=30.0)
    if not adapter.is_ready:
        print("Adapter not ready, abort.")
        return 1

    client = adapter._client  # noqa: SLF001 — diag-only
    if client is None:
        print("client is None, abort.")
        return 1

    light = client.get_symbols()
    target_by_name = {"XAUUSD": None, "BRENT": None}
    for s in light.symbol:
        name = getattr(s, "symbolName", "")
        if name in target_by_name:
            target_by_name[name] = s.symbolId

    print("\n=== Symbol IDs ===")
    for name, sid in target_by_name.items():
        print(f"  {name}: id={sid}")

    ids = [sid for sid in target_by_name.values() if sid is not None]
    if not ids:
        print("None of requested symbols found.")
        return 1

    det_resp = client.get_symbol_details(ids)
    for sym in det_resp.symbol:
        fields = _dump_proto_message(sym)
        name_for_id = next(
            (n for n, sid in target_by_name.items() if sid == sym.symbolId),
            f"id={sym.symbolId}",
        )
        print(f"\n=== ProtoOASymbol: {name_for_id} (id={sym.symbolId}) ===")
        for key, val in fields.items():
            print(f"  {key:30s} = {val!r}")

        pip_position = getattr(sym, "pipPosition", None)
        lot_size = getattr(sym, "lotSize", None)
        digits = getattr(sym, "digits", None)
        if pip_position is not None and lot_size is not None:
            pip_size_abs = 10 ** (-pip_position)
            pip_value_per_lot_quote_currency = pip_size_abs * lot_size
            print(
                f"\n  Derived: pip_size = 10^-{pip_position} = {pip_size_abs}"
                f"\n           lotSize  = {lot_size}"
                f"\n           pip_value/lot in quote-currency units (no FX-conversion) = "
                f"{pip_value_per_lot_quote_currency}"
            )
            print(
                "  NOTE: cTrader Open API возвращает lotSize в RAW units. "
                "Финальный USD pip-value зависит от quote currency, account "
                "currency, и (для не-USD-quoted) — от текущего курса.\n"
            )

    adapter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
