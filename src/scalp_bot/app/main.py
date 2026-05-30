"""scalp_bot main loop — orderflow-скальпер Bybit (детерминированный).

Каждые ``eval_interval_sec`` (default 1с):
1. Killswitch check (дневной/совокупный убыток).
2. Сопровождение открытых сделок (тайм-стоп / TP / SL).
3. Для каждого символа: snapshot микроструктуры из WS-кэша → evaluate()
   → если сигнал и прошли гейты (cooldown, лимит позиций, rate) → on_signal.
4. Heartbeat-лог раз в 60с.

Решения принимаются БЕЗ LLM. Запуск: ``python -m scalp_bot.app.main``.
PAPER по умолчанию (trading_enabled=false) — ордера только логируются.
"""
from __future__ import annotations

import logging
import signal
import time

from scalp_bot.analysis.signals import evaluate
from scalp_bot.config.settings import load_settings
from scalp_bot.data.aggregates import SymbolState
from scalp_bot.data.market_stream import BybitMarketStream
from scalp_bot.safety import killswitch
from scalp_bot.state.db import ScalpDB
from scalp_bot.trading.client import ScalpBybitClient
from scalp_bot.trading.executor import Executor

log = logging.getLogger("scalp_bot")

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Получен сигнал %d, завершаю...", signum)


def run() -> None:
    cfg = load_settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    symbols = cfg.symbol_list
    mode = "LIVE(demo)" if cfg.trading_enabled else "PAPER"
    log.info("scalp_bot старт | mode=%s | symbols=%s | lot=$%.0f (min $%.0f) | "
             "kill day/total=$%.0f/$%.0f | min_conf=%d", mode, ",".join(symbols),
             cfg.position_usd, cfg.min_position_usd, cfg.max_daily_loss_usd,
             cfg.max_total_loss_usd, cfg.min_confluence)

    db = ScalpDB(cfg.data_dir)

    client = None
    if cfg.trading_enabled:
        if not cfg.bybit_api_key or not cfg.bybit_api_secret:
            log.error("trading_enabled=true, но нет SCALP_BYBIT_API_KEY/SECRET — выходим")
            return
        client = ScalpBybitClient(cfg.bybit_api_key, cfg.bybit_api_secret,
                                  demo=cfg.bybit_demo, category=cfg.bybit_category)
        log.info("Bybit REST: demo=%s category=%s", cfg.bybit_demo, cfg.bybit_category)

    states: dict[str, SymbolState] = {
        s: SymbolState(s, cvd_window_sec=cfg.cvd_window_sec,
                       liq_window_sec=cfg.liq_window_sec, ob_levels=cfg.ob_levels)
        for s in symbols
    }
    stream = BybitMarketStream(symbols, states, category=cfg.bybit_category,
                               testnet=cfg.bybit_testnet)
    stream.start()

    executor = Executor(db, cfg, client)
    cooldown: dict[str, float] = {}
    last_heartbeat = 0.0

    try:
        while not _shutdown:
            loop_start = time.monotonic()
            now = time.time()

            # 1) сопровождение открытых
            try:
                executor.manage(states)
            except Exception:
                log.exception("manage failed")

            # 2) killswitch
            killed = killswitch.is_killed(db, cfg, now)
            if not killed.allowed:
                if now - last_heartbeat >= 60:
                    log.warning("KILLSWITCH: %s — новые входы заблокированы", killed.reason)
                    last_heartbeat = now
                time.sleep(cfg.eval_interval_sec)
                continue

            open_symbols = {tr.symbol for tr in db.open_trades()}

            # 2b) funding-окно: не открываемся перед списанием (00/08/16 UTC)
            to_funding = sec_to_next_funding(now)
            if to_funding < cfg.avoid_funding_window_sec:
                if now - last_heartbeat >= 60:
                    log.info("funding через %.0fс — входы на паузе (окно %.0fс)",
                             to_funding, cfg.avoid_funding_window_sec)
                time.sleep(cfg.eval_interval_sec)
                continue

            # 3) сигналы
            for sym in symbols:
                if sym in open_symbols:
                    continue
                if now - cooldown.get(sym, 0.0) < cfg.signal_cooldown_sec:
                    continue
                snap = states[sym].snapshot()
                try:
                    sig = evaluate(snap, cfg)
                except Exception:
                    log.exception("evaluate %s failed", sym)
                    continue
                if sig is None:
                    continue
                gate = killswitch.can_open(db, cfg, now)
                if not gate.allowed:
                    log.info("gate block: %s", gate.reason)
                    break
                if executor.on_signal(sig) is not None:
                    cooldown[sym] = now
                    open_symbols.add(sym)

            # 4) heartbeat
            if now - last_heartbeat >= 60:
                _heartbeat(states, db, stream)
                last_heartbeat = now

            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, cfg.eval_interval_sec - elapsed))
    finally:
        stream.stop()
        db.close()
        log.info("scalp_bot остановлен")


def _heartbeat(states: dict[str, SymbolState], db: ScalpDB,
               stream: BybitMarketStream) -> None:
    parts = []
    for sym, st in states.items():
        s = st.snapshot()
        fund = f"{s.funding_rate * 100:.3f}%" if s.funding_rate is not None else "?"
        imb = f"{s.ob_imbalance:.2f}" if s.ob_imbalance is not None else "?"
        flag = "STALE" if s.stale else "ok"
        parts.append(f"{sym}:{flag} px={s.last_price} cvdN={len(s.cvd_samples)} "
                     f"imb={imb} fund={fund} liq={len(s.liq_events)}")
    day_pnl = db.realized_pnl_since(now_utc_day())
    log.info("HB ws=%s open=%d dayPnL=%.2f | %s",
             stream.is_connected(), db.open_count(), day_pnl, " | ".join(parts))


def now_utc_day() -> float:
    now = time.time()
    return now - (now % 86400.0)


def sec_to_next_funding(now: float) -> float:
    """Секунд до ближайшего funding settlement Bybit (00:00/08:00/16:00 UTC)."""
    sec_of_day = now % 86400.0
    for boundary in (0.0, 28800.0, 57600.0, 86400.0):
        if boundary > sec_of_day:
            return boundary - sec_of_day
    return 86400.0 - sec_of_day


if __name__ == "__main__":
    run()
