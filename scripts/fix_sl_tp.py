"""Одноразовый скрипт: восстановить SL + TP на позициях, где они отсутствуют.

Для позиций без SL: рассчитывает SL как entry ± DEFAULT_SL_PIPS * pip_size.
Для позиций без TP: рассчитывает TP как entry ± DEFAULT_TP_PIPS * pip_size.
Передаёт ОБА поля в amend, чтобы не затереть одно другим.
"""

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
DEFAULT_SL_PIPS = 10.0

JPY_NAMES = {"EURJPY", "GBPJPY", "USDJPY"}


def pip_size_for(name: str) -> float:
    upper = name.upper()
    if any(j in upper for j in JPY_NAMES):
        return 0.01
    if "BITCOIN" in upper:
        return 1.0
    if "ETHEREUM" in upper:
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


def digits_for(ps: float) -> int:
    if ps >= 1.0:
        return 1
    if ps >= 0.1:
        return 2
    if ps >= 0.01:
        return 3
    if ps >= 0.001:
        return 4
    return 5


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
    sym_map = {s.symbolId: getattr(s, "symbolName", str(s.symbolId)) for s in sym_resp.symbol}

    log.info("Получаем открытые позиции...")
    reconcile = client.reconcile()
    positions = list(reconcile.position) if hasattr(reconcile, "position") else []
    log.info("Открытых позиций: %d", len(positions))

    fixed = 0
    ok = 0
    errors = 0

    for pos in positions:
        pos_id = pos.positionId
        td = pos.tradeData if hasattr(pos, "tradeData") else None
        symbol_id = td.symbolId if td else 0
        trade_side = td.tradeSide if td else 0
        side = "BUY" if trade_side == 1 else "SELL"
        symbol_name = sym_map.get(symbol_id, f"id#{symbol_id}")
        entry = pos.price if hasattr(pos, "price") else 0

        has_tp = pos.HasField("takeProfit")
        has_sl = pos.HasField("stopLoss")
        cur_sl = pos.stopLoss if has_sl else None
        cur_tp = pos.takeProfit if has_tp else None

        if has_sl and has_tp:
            log.info(
                "  [OK] #%d %s %s entry=%.5f SL=%.5f TP=%.5f",
                pos_id, symbol_name, side, entry, cur_sl, cur_tp,
            )
            ok += 1
            continue

        ps = pip_size_for(symbol_name)
        dg = digits_for(ps)

        new_sl = cur_sl
        new_tp = cur_tp

        if not has_sl and entry:
            sl_dist = DEFAULT_SL_PIPS * ps
            if side == "BUY":
                new_sl = round(entry - sl_dist, dg)
            else:
                new_sl = round(entry + sl_dist, dg)

        if not has_tp and entry:
            tp_dist = DEFAULT_TP_PIPS * ps
            if side == "BUY":
                new_tp = round(entry + tp_dist, dg)
            else:
                new_tp = round(entry - tp_dist, dg)

        flags = []
        if not has_sl:
            flags.append(f"SL: — → {new_sl:.5f}")
        if not has_tp:
            flags.append(f"TP: — → {new_tp:.5f}")

        log.info(
            "  [FIX] #%d %s %s entry=%.5f  %s",
            pos_id, symbol_name, side, entry, ", ".join(flags),
        )

        try:
            client.amend_position_sl_tp(
                position_id=pos_id,
                stop_loss=new_sl,
                take_profit=new_tp,
            )
            log.info("    → OK")
            fixed += 1
            time.sleep(0.3)
        except Exception as e:
            log.error("    → FAILED: %s", e)
            errors += 1

    log.info("")
    log.info("=== ИТОГО ===")
    log.info("  Исправлено: %d", fixed)
    log.info("  Всё ОК:    %d", ok)
    log.info("  Ошибки:    %d", errors)

    client.stop()


if __name__ == "__main__":
    main()
