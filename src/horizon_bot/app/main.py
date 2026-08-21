"""Цикл daytrend / swing: сигнал на закрытом баре, рынок на следующем тике.

Чужую позицию на общем демо-счёте (scalp) не трогаем: если на символе
есть лот, а в нашей БД его нет — пропускаем символ.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from horizon_bot.client import HorizonClient
from horizon_bot.db import HorizonDB
from horizon_bot.settings import load_settings
from horizon_bot.signals import STRATEGIES

log = logging.getLogger("horizon_bot")


def _link(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def working_capital(equity: float, virtual_capital: float) -> float:
    """Капитал для ставки: не больше виртуального лимита.

    На общем демо лежит десятки тысяч, но бот считает, что у него ровно
    ``virtual_capital``. Если живой счёт меньше лимита — берём живой, чтобы
    не ставить больше, чем есть. ``virtual_capital<=0`` — старое поведение
    (весь живой счёт).
    """
    if virtual_capital <= 0:
        return max(0.0, equity)
    return max(0.0, min(equity, virtual_capital))


def _cycle(cfg, client: HorizonClient, db: HorizonDB) -> None:
    fn = STRATEGIES.get(cfg.strategy)
    if fn is None:
        log.error("неизвестная стратегия %s", cfg.strategy)
        return
    equity = client.wallet_equity()
    if equity <= 0:
        log.warning("equity=0 — нет счёта или ключей")
        return
    for sym in cfg.symbol_list:
        bars = client.closed_klines(sym, cfg.interval, limit=250)
        closes = [c for _, c in bars]
        want = fn(closes)
        if want is None:
            log.info("%s мало баров (%d)", sym, len(closes))
            continue
        broker = client.get_position(sym)
        if broker is None:
            log.warning("%s get_position не ответил — skip", sym)
            continue
        ours = db.owned(sym)
        if broker.size > 0 and ours is None:
            log.info("%s чужая позиция %.6f %s — не трогаем",
                     sym, broker.size, broker.side)
            continue
        if broker.size == 0 and ours is not None:
            db.close_pos(sym, broker.entry_price or closes[-1], "broker_flat")
            ours = None

        if want == 1 and ours is None:
            px = client.last_price(sym) or closes[-1]
            capital = working_capital(equity, cfg.virtual_capital)
            notional = capital * cfg.position_frac
            if notional < cfg.min_notional_usd:
                log.info("%s нотионал $%.2f < min — skip", sym, notional)
                continue
            qty = float(client.fmt_qty(sym, notional / px))
            info = client.instrument(sym)
            if qty <= 0 or (info and qty < info.min_order_qty):
                log.info("%s qty %.6f < min — skip", sym, qty)
                continue
            client.set_leverage(sym, cfg.leverage)
            lid = _link(cfg.link_prefix)
            res = client.market(symbol=sym, side="Buy", qty=qty,
                                order_link_id=lid)
            if not res.get("ok"):
                log.warning("%s вход отклонён: %s", sym, res.get("error"))
                continue
            db.open_pos(sym, "Buy", qty, px, lid)
            log.info("%s LONG qty=%.6f px=%.4f frac=%.2f",
                     sym, qty, px, cfg.position_frac)
        elif want == 0 and ours is not None:
            lid = _link(cfg.link_prefix)
            res = client.market(symbol=sym, side="Sell", qty=ours["qty"],
                                order_link_id=lid, reduce_only=True)
            if not res.get("ok"):
                log.warning("%s выход отклонён: %s", sym, res.get("error"))
                continue
            px = client.last_price(sym) or closes[-1]
            db.close_pos(sym, px, "signal_flat")
            log.info("%s FLAT выход px=%.4f", sym, px)
        else:
            log.info("%s hold want=%s owned=%s broker=%.6f",
                     sym, want, bool(ours), broker.size)


def run() -> None:
    logging.basicConfig(
        level=os.environ.get("DAYTREND_LOG_LEVEL")
        or os.environ.get("SWING_LOG_LEVEL") or "INFO",
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = load_settings()
    log.info("старт %s strategy=%s interval=%s symbols=%s demo=%s",
             cfg.bot_name, cfg.strategy, cfg.interval,
             ",".join(cfg.symbol_list), cfg.bybit_demo)
    if cfg.trading_enabled and (not cfg.bybit_api_key or not cfg.bybit_api_secret):
        log.error("нет API ключей — выходим")
        return
    os.makedirs(cfg.data_dir, exist_ok=True)
    db = HorizonDB(cfg.db_path)
    client = HorizonClient(cfg.bybit_api_key, cfg.bybit_api_secret,
                           demo=cfg.bybit_demo, category=cfg.bybit_category)
    while True:
        try:
            _cycle(cfg, client, db)
        except Exception:
            log.exception("цикл")
        time.sleep(max(30, cfg.poll_sec))


if __name__ == "__main__":
    run()
