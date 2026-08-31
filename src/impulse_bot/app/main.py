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
from impulse_bot.db import ImpulseDB, SignalSnapshot
from impulse_bot.settings import load_settings
from impulse_bot.signals import (
    Burst,
    Cluster,
    Tape,
    clamp_mkt_qty,
    detect_burst,
    in_session,
    in_universe,
    should_enter,
)
from impulse_bot.telegram import TelegramNotifier, fmt_enter, fmt_exit

log = logging.getLogger("impulse_bot")


def _link() -> str:
    # Bybit orderLinkId ≤36. Префикс отделяет ордера от scalp_/daytrend_/swing_.
    return f"impulse_{uuid.uuid4().hex[:16]}"


def working_capital(equity: float, virtual_capital: float) -> float:
    """Капитал для риска: не больше виртуального лимита.

    На общем демо лежит десятки тысяч, но бот считает, что у него ровно
    ``virtual_capital``. Если живой счёт меньше лимита — берём живой.
    ``virtual_capital<=0`` — старое поведение (весь живой счёт).
    """
    if virtual_capital <= 0:
        return max(0.0, equity)
    return max(0.0, min(equity, virtual_capital))


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _snapshot(burst: Burst, tape: Tape, cluster: Cluster,
              turnover24h: float) -> SignalSnapshot:
    """Что бот видел в момент входа — пишем в БД для последующего разбора.

    На входные решения не влияет: снимок собирается уже после того, как
    `should_enter` вернул True.
    """
    return SignalSnapshot(
        burst_usd=burst.burst_usd,
        move_pct=burst.move_pct,
        tape_buy=tape.buy_usd,
        tape_sell=tape.sell_usd,
        cluster_frac=cluster.dir_frac,
        turnover24h=turnover24h,
    )


def _pnl(pos: dict, exit_px: float) -> float:
    sign = 1 if pos["side"] == "Buy" else -1
    return sign * (exit_px - pos["entry"]) * pos["qty"]


def _notify_exit(tg: TelegramNotifier, pos: dict, px: float, reason: str) -> None:
    tg.send(fmt_exit(symbol=pos["symbol"], side=pos["side"], qty=pos["qty"],
                     entry=pos["entry"], exit_px=px, pnl_usd=_pnl(pos, px),
                     reason=reason))


def _record_exit(client: ImpulseClient, db: ImpulseDB, tg: TelegramNotifier,
                 pos: dict, reason: str) -> None:
    """Закрывает позицию в БД, подставляя фактические цены с биржи.

    `last_price` — это цена на момент, когда цикл заметил закрытие, то есть
    с задержкой до одного поллинга. Для учёта берём avgExitPrice и net PnL
    из closed_pnl, а тикер оставляем как запасной вариант.
    """
    closed = client.last_closed_trade(pos["symbol"], not_before_ts=pos["ts_open"])
    px = client.last_price(pos["symbol"]) or pos["entry"]
    if closed is None:
        log.info("%s нет closed_pnl — учёт по тикеру", pos["symbol"])
        db.close_pos(pos["symbol"], px, reason)
        _notify_exit(tg, pos, px, reason)
        return
    db.close_pos(pos["symbol"], px, reason,
                 exit_real=closed.exit_price, pnl_net=closed.pnl)
    tg.send(fmt_exit(symbol=pos["symbol"], side=pos["side"], qty=pos["qty"],
                     entry=closed.entry_price, exit_px=closed.exit_price,
                     pnl_usd=closed.pnl, reason=reason))


def _manage(cfg, client: ImpulseClient, db: ImpulseDB,
            tg: TelegramNotifier) -> None:
    now = time.time()
    for pos in db.all_owned():
        broker = client.get_position(pos["symbol"])
        if broker is None:
            continue
        if broker.size == 0:
            _record_exit(client, db, tg, pos, "broker_flat")
            continue
        if now - pos["ts_open"] >= cfg.scratch_sec:
            lid = _link()
            close_side = "Sell" if pos["side"] == "Buy" else "Buy"
            res = client.market(symbol=pos["symbol"], side=close_side,
                                qty=pos["qty"], order_link_id=lid,
                                reduce_only=True)
            if res.get("ok"):
                log.info("%s scratch %ds", pos["symbol"], cfg.scratch_sec)
                _record_exit(client, db, tg, pos, "time_scratch")


def _enter(cfg, client: ImpulseClient, db: ImpulseDB, symbol: str,
           side: str, px: float, equity: float, tg: TelegramNotifier,
           signal: SignalSnapshot | None = None) -> None:
    sl_frac = cfg.sl_pct / 100.0
    tp_frac = cfg.tp_pct / 100.0
    if side == "Buy":
        sl, tp = px * (1 - sl_frac), px * (1 + tp_frac)
    else:
        sl, tp = px * (1 + sl_frac), px * (1 - tp_frac)
    risk = working_capital(equity, cfg.virtual_capital) * cfg.risk_frac
    dist = abs(px - sl)
    if dist <= 0:
        return
    raw = risk / dist
    info = client.instrument(symbol)
    max_mkt = info.max_mkt_order_qty if info else 0.0
    min_qty = info.min_order_qty if info else 0.0
    step = info.qty_step if info else 0.0
    clamped = clamp_mkt_qty(raw, max_mkt=max_mkt, min_qty=min_qty, step=step)
    if clamped is None:
        log.info("%s qty после maxMktOrderQty меньше min — skip", symbol)
        return
    qty, capped = clamped
    qty = float(client.fmt_qty(symbol, qty))
    if capped:
        log.info("%s лот обрезан до maxMktOrderQty %.6f (хотели %.6f)",
                 symbol, qty, raw)
    min_notional = max(cfg.min_notional_usd,
                       info.min_notional if info else 0.0)
    notional = qty * px
    if qty <= 0 or notional < min_notional:
        log.info("%s qty/notional мало — skip", symbol)
        return
    client.set_leverage(symbol, cfg.leverage)
    lid = _link()
    res = client.market(symbol=symbol, side=side, qty=qty, order_link_id=lid,
                        sl=sl, tp=tp)
    if not res.get("ok"):
        log.warning("%s вход отклонён: %s", symbol, res.get("error"))
        return
    db.open_pos(symbol, side, qty, px, sl, tp, lid, signal=signal)
    db.bump_session(_day())
    # Цена филла Market отличается от цены тикера, по которой принято решение.
    broker = client.get_position(symbol)
    if broker is not None and broker.entry_price > 0:
        db.set_entry_real(symbol, broker.entry_price)
    log.info("%s %s qty=%.6f px=%.6f sl=%.6f tp=%.6f", symbol, side, qty, px, sl, tp)
    if signal is not None:
        log.info("%s сигнал: удар $%.0f ход %.2f%% лента %.0f/%.0f кластер %.2f "
                 "оборот $%.0f", symbol, signal.burst_usd, signal.move_pct,
                 signal.tape_buy, signal.tape_sell, signal.cluster_frac,
                 signal.turnover24h)
    tg.send(fmt_enter(symbol=symbol, side=side, qty=qty, px=px, sl=sl, tp=tp))


def _cycle(cfg, client: ImpulseClient, db: ImpulseDB,
           cache: dict[str, tuple[float, float]], tg: TelegramNotifier) -> None:
    hour = datetime.now(timezone.utc).hour
    _manage(cfg, client, db, tg)
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
    log.info("цикл open=%d sess=%d uni=%d eq=%.0f",
             db.open_count(), db.session_trades(_day()), len(scored), equity)
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
        _enter(cfg, client, db, t.symbol, burst.side, t.last, equity, tg,
               signal=_snapshot(burst, tape, cluster, t.turnover24h))
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
    tg = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id,
                          enabled=cfg.telegram_enabled)
    log.info("telegram %s (только вход/выход)", "on" if tg.active else "off")
    cache: dict[str, tuple[float, float]] = {}
    while True:
        try:
            _cycle(cfg, client, db, cache, tg)
        except Exception:
            log.exception("цикл")
        time.sleep(max(5, cfg.poll_sec))


if __name__ == "__main__":
    run()
