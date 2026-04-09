"""Одноразовый скрипт: проставить TP всем открытым позициям без TP на cTrader."""

import json
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fx_pro_bot.trading.client import CTraderClient

DEFAULT_TP_PIPS = 10.0

JPY_NAMES = {"EURJPY", "GBPJPY", "USDJPY"}
CRYPTO_NAMES = {"BITCOIN", "ETHEREUM"}
METAL_NAMES = {"XAUUSD", "XPTUSD", "COPPER"}


def pip_size_for(name: str) -> float:
    upper = name.upper()
    if any(j in upper for j in JPY_NAMES):
        return 0.01
    if any(c in upper for c in CRYPTO_NAMES):
        return 1.0
    if "XAUUSD" in upper:
        return 0.1
    if "XPTUSD" in upper:
        return 0.1
    if "COPPER" in upper:
        return 0.001
    if "OIL" in upper or "NGAS" in upper:
        return 0.01
    if "US500" in upper or "USTEC" in upper:
        return 1.0
    return 0.0001


def main():
    token_path = os.environ.get("CTRADER_TOKEN_PATH", "/data/ctrader_tokens.json")
    if not os.path.exists(token_path):
        token_path = os.path.join(os.path.dirname(__file__), "..", "data", "ctrader_tokens.json")
    tokens = json.loads(open(token_path).read())

    client = CTraderClient(
        client_id=os.environ["CTRADER_CLIENT_ID"],
        client_secret=os.environ["CTRADER_CLIENT_SECRET"],
        access_token=tokens["access_token"],
        account_id=int(os.environ["CTRADER_ACCOUNT_ID"]),
        host_type=os.environ.get("CTRADER_HOST_TYPE", "demo"),
    )

    log.info("Подключаемся к cTrader...")
    client.start(timeout=30, retries=3)
    if not client.is_ready:
        log.error("Не удалось подключиться")
        return

    log.info("Получаем список символов...")
    sym_resp = client.get_symbols()
    sym_map: dict[int, str] = {}
    for s in sym_resp.symbol:
        sym_map[s.symbolId] = getattr(s, "symbolName", str(s.symbolId))
    log.info("Символов: %d", len(sym_map))

    log.info("Получаем открытые позиции...")
    reconcile = client.reconcile()
    positions = list(reconcile.position) if hasattr(reconcile, "position") else []
    log.info("Открытых позиций: %d", len(positions))

    amended = 0
    skipped = 0
    errors = 0

    for pos in positions:
        pos_id = pos.positionId
        td = pos.tradeData if hasattr(pos, "tradeData") else None
        symbol_id = td.symbolId if td else 0
        trade_side = td.tradeSide if td else 0
        side = "BUY" if trade_side == 1 else "SELL"
        symbol_name = sym_map.get(symbol_id, f"id#{symbol_id}")
        entry = pos.price if hasattr(pos, "price") else 0

        has_tp = hasattr(pos, "takeProfit") and pos.HasField("takeProfit")
        has_sl = hasattr(pos, "stopLoss") and pos.HasField("stopLoss")
        sl_val = pos.stopLoss if has_sl else None
        tp_val = pos.takeProfit if has_tp else None

        if has_tp:
            log.info(
                "  [OK] #%d %s %s entry=%.5f SL=%s TP=%.5f",
                pos_id, symbol_name, side, entry,
                f"{sl_val:.5f}" if sl_val else "—",
                tp_val,
            )
            skipped += 1
            continue

        ps = pip_size_for(symbol_name)
        tp_distance = DEFAULT_TP_PIPS * ps

        if side == "BUY":
            tp_price = entry + tp_distance
        else:
            tp_price = entry - tp_distance

        digits = 5
        if ps >= 1.0:
            digits = 1
        elif ps >= 0.1:
            digits = 2
        elif ps >= 0.01:
            digits = 3
        elif ps >= 0.001:
            digits = 4

        tp_rounded = round(tp_price, digits)

        log.info(
            "  [SET TP] #%d %s %s entry=%.5f → TP=%.5f (+%.1f pips), SL=%s",
            pos_id, symbol_name, side, entry,
            tp_rounded, DEFAULT_TP_PIPS,
            f"{sl_val:.5f}" if sl_val else "—",
        )

        try:
            client.amend_position_sl_tp(
                position_id=pos_id,
                stop_loss=sl_val,
                take_profit=tp_rounded,
            )
            log.info("    → OK")
            amended += 1
            time.sleep(0.3)
        except Exception as e:
            log.error("    → FAILED: %s", e)
            errors += 1

    log.info("")
    log.info("=== ИТОГО ===")
    log.info("  TP проставлен: %d", amended)
    log.info("  Уже с TP:     %d", skipped)
    log.info("  Ошибки:       %d", errors)

    client.stop()


if __name__ == "__main__":
    main()
