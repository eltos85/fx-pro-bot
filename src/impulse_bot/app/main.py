"""Цикл impulse-bot: скринер удара → лента+кластер → рынок с биржевым SL/TP.

Чужую позицию (scalp / swing на том же демо) не трогает.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone

from impulse_bot.client import ImpulseClient, Ticker
from impulse_bot.db import ImpulseDB
from impulse_bot.settings import load_settings
from impulse_bot.signals import detect_burst, in_session, in_universe, should_enter

log = logging.getLogger("impulse_bot")


def _link() -> str:
    # Bybit orderLinkId ≤36. Префикс отделяет ордера от scalp_/daytrend_/swing_.
    return f"impulse_{uuid.uuid4().hex[:16]}"


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _manage(cfg, client: ImpulseClient, db: ImpulseDB) -> None:
    now = time.time()
    for pos in db.all_owned():
        broker = client.get_position(pos["symbol"])
        if broker is None:
            continue
        if broker.size == 0:
            px = client.last_price(pos["symbol"]) or pos["entry"]
            db.close_pos(pos["symbol"], px, "broker_flat")
            continue
        if now - pos["ts_open"] >= cfg.scratch_sec:
            lid = _link()
            close_side = "Sell" if pos["side"] == "Buy" else "Buy"
            res = client.market(symbol=pos["symbol"], side=close_side,
                                qty=pos["qty"], order_link_id=lid,
                                reduce_only=True)
            if res.get("ok"):
                px = client.last_price(pos["symbol"]) or pos["entry"]
                db.close_pos(pos["symbol"], px, "time_scratch")
                log.info("%s scratch %ds", pos["symbol"], cfg.scratch_sec)


def _enter(cfg, client: ImpulseClient, db: ImpulseDB, symbol: str,
           side: str, px: float, equity: float) -> None:
    sl_frac = cfg.sl_pct / 100.0
    tp_frac = cfg.tp_pct / 100.0
    if side == "Buy":
        sl, tp = px * (1 - sl_frac), px * (1 + tp_frac)
    else:
        sl, tp = px * (1 + sl_frac), px * (1 - tp_frac)
    risk = equity * cfg.risk_frac
    dist = abs(px - sl)
    if dist <= 0:
        return
    qty = float(client.fmt_qty(symbol, risk / dist))
    info = client.instrument(symbol)
    notional = qty * px
    if qty <= 0 or notional < cfg.min_notional_usd:
        log.info("%s qty/notional мало — skip", symbol)
        return
    if info and qty < info.min_order_qty:
        log.info("%s qty < min", symbol)
        return
    client.set_leverage(symbol, cfg.leverage)
    lid = _link()
    res = client.market(symbol=symbol, side=side, qty=qty, order_link_id=lid,
                        sl=sl, tp=tp)
    if not res.get("ok"):
        log.warning("%s вход отклонён: %s", symbol, res.get("error"))
        return
    db.open_pos(symbol, side, qty, px, sl, tp, lid)
    db.bump_session(_day())
    log.info("%s %s qty=%.6f px=%.6f sl=%.6f tp=%.6f",
             symbol, side, qty, px, sl, tp)


def _cycle(cfg, client: ImpulseClient, db: ImpulseDB,
           cache: dict[str, tuple[float, float]]) -> None:
    hour = datetime.now(timezone.utc).hour
    _manage(cfg, client, db)
    if db.open_count() >= cfg.max_open:
        return
    if not in_session(hour, cfg.session_start_utc, cfg.session_end_utc):
        return
    if db.session_trades(_day()) >= cfg.max_trades_session:
        return
    equity = client.wallet_equity()
    if equity <= 0:
        log.warning("equity=0")
        return
    ticks = client.tickers()
    scored: list[Ticker] = []
    for t in ticks:
        if not in_universe(t.symbol, t.turnover24h, skip=cfg.skip_set,
                           lo=cfg.turnover_lo, hi=cfg.turnover_hi):
            continue
        scored.append(t)
    scored.sort(key=lambda x: -x.turnover24h)
    scored = scored[:cfg.universe_cap]
    for t in scored:
        prev = cache.get(t.symbol)
        cache[t.symbol] = (t.last, t.turnover24h)
        if prev is None:
            continue
        burst = detect_burst(
            t.symbol, prev[0], prev[1], t.last, t.turnover24h,
            burst_usd=cfg.burst_usd, move_pct=cfg.burst_move_pct)
        if burst is None:
            continue
        if db.owned(t.symbol) is not None:
            continue
        broker = client.get_position(t.symbol)
        if broker is None:
            continue
        if broker.size > 0:
            log.info("%s чужая позиция — не трогаем", t.symbol)
            continue
        tape, cluster = client.tape_and_cluster(
            t.symbol, burst.side, window_sec=cfg.tape_sec)
        if not should_enter(burst, tape, cluster, tape_ratio=cfg.tape_ratio):
            log.info("%s удар есть, лента/кластер нет buy=%.0f sell=%.0f cl=%.2f",
                     t.symbol, tape.buy_usd, tape.sell_usd, cluster.dir_frac)
            continue
        _enter(cfg, client, db, t.symbol, burst.side, t.last, equity)
        return


def run() -> None:
    logging.basicConfig(
        level=os.environ.get("IMPULSE_LOG_LEVEL") or "INFO",
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = load_settings()
    log.info("старт impulse demo=%s session=%02d-%02d UTC burst=$%.0f/%.2f%%",
             cfg.bybit_demo, cfg.session_start_utc, cfg.session_end_utc,
             cfg.burst_usd, cfg.burst_move_pct)
    if cfg.trading_enabled and (not cfg.bybit_api_key or not cfg.bybit_api_secret):
        log.error("нет API ключей — выходим")
        return
    os.makedirs(cfg.data_dir, exist_ok=True)
    db = ImpulseDB(cfg.db_path)
    client = ImpulseClient(cfg.bybit_api_key, cfg.bybit_api_secret,
                           demo=cfg.bybit_demo, category=cfg.bybit_category)
    cache: dict[str, tuple[float, float]] = {}
    while True:
        try:
            _cycle(cfg, client, db, cache)
        except Exception:
            log.exception("цикл")
        time.sleep(max(5, cfg.poll_sec))


if __name__ == "__main__":
    run()
