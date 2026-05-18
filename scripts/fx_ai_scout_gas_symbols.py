"""Read-only разведка газовых символов в cTrader FxPro.

Запускать ОДНОРАЗОВО внутри fx-ai-trader контейнера:

    docker exec fx-pro-bot-fx-ai-trader-1 python scripts/fx_ai_scout_gas_symbols.py

Ищет все символы с упоминанием газа (NAT, GAS, NG, TTF, HENRY) и печатает
полный ProtoOASymbol для каждого — нужно для расчёта pip_value до правки
executor.py.

Никаких побочных эффектов: не открывает позиций, не пишет в БД.
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.trading.client_adapter import CTraderFxAdapter

GAS_KEYWORDS = ("NAT", "GAS", "NG", "TTF", "HENRY", "LNG")


def _dump_proto_message(obj) -> dict:
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
    candidates: list[tuple[str, int]] = []
    for s in light.symbol:
        name = getattr(s, "symbolName", "")
        upper = name.upper()
        if any(kw in upper for kw in GAS_KEYWORDS):
            candidates.append((name, s.symbolId))

    print(f"\n=== Found {len(candidates)} candidate gas symbols ===")
    for name, sid in candidates:
        print(f"  {name:30s} id={sid}")

    if not candidates:
        print("No gas symbols found by keyword match.")
        return 0

    ids = [sid for _, sid in candidates]
    det_resp = client.get_symbol_details(ids)
    for sym in det_resp.symbol:
        fields = _dump_proto_message(sym)
        name_for_id = next(
            (n for n, sid in candidates if sid == sym.symbolId),
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
            pip_value_per_lot_quote = pip_size_abs * lot_size
            print(
                f"\n  Derived: pip_size = 10^-{pip_position} = {pip_size_abs}"
                f"\n           lotSize  = {lot_size}"
                f"\n           digits   = {digits}"
                f"\n           pip_value/lot in quote-ccy units = {pip_value_per_lot_quote}"
            )

    adapter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
