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

from scalp_bot.analysis.signals import diagnose
from scalp_bot.analysis.strategies import build_strategies, resolve
from scalp_bot.config.settings import load_settings
from scalp_bot.data.aggregates import SymbolState
from scalp_bot.data.exec_stream import BybitExecStream
from scalp_bot.data.market_stream import BybitMarketStream
from scalp_bot.safety import killswitch
from scalp_bot.state.db import ScalpDB
from scalp_bot.telegram.notifier import TelegramNotifier
from scalp_bot.trading.client import ScalpBybitClient
from scalp_bot.trading.executor import Executor

log = logging.getLogger("scalp_bot")
play = logging.getLogger("scalp_bot.play")  # пошаговый нарратив торговли

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
        if cfg.flatten_on_start:
            _flatten_on_start(client, db, cfg.symbol_list)

    states: dict[str, SymbolState] = {
        s: SymbolState(s, cvd_window_sec=cfg.cvd_window_sec,
                       liq_window_sec=cfg.liq_window_sec, ob_levels=cfg.ob_levels)
        for s in symbols
    }
    stream = BybitMarketStream(symbols, states, category=cfg.bybit_category,
                               testnet=cfg.bybit_testnet)
    stream.start()

    # приватный поток исполнений — источник истины по net P&L/комиссиям (без REST)
    exec_stream = None
    if client is not None:
        exec_stream = BybitExecStream(cfg.bybit_api_key, cfg.bybit_api_secret,
                                      demo=cfg.bybit_demo, testnet=cfg.bybit_testnet)
        exec_stream.start()

    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                enabled=cfg.telegram_enabled)
    if notifier.active:
        notifier.send(f"🚀 scalp_bot старт | {mode} | {','.join(symbols)} | "
                      f"лот ${cfg.position_usd:.0f} | kill ${cfg.max_daily_loss_usd:.0f}/день")

    strategies = build_strategies(cfg, symbols)
    log.info("стратегии: %s", ",".join(s.name for s in strategies))
    executor = Executor(db, cfg, client, notifier=notifier, strategies=strategies)
    cooldown: dict[str, float] = {}
    last_heartbeat = 0.0
    kill_notified = False
    funnel = _new_funnel()

    try:
        while not _shutdown:
            loop_start = time.monotonic()
            now = time.time()

            # 0) забрать исполнения из приватного WS → атрибуция к сделкам
            if exec_stream is not None:
                try:
                    executor.ingest_executions(exec_stream.drain())
                except Exception:
                    log.exception("ingest_executions failed")

            # 1) сопровождение открытых
            try:
                executor.manage(states)
            except Exception:
                log.exception("manage failed")

            # 2) killswitch
            killed = killswitch.is_killed(db, cfg, now)
            if not killed.allowed:
                if not kill_notified:
                    notifier.send(f"⛔ KILLSWITCH: {killed.reason} — торговля остановлена")
                    kill_notified = True
                if now - last_heartbeat >= 60:
                    log.warning("KILLSWITCH: %s — новые входы заблокированы", killed.reason)
                    last_heartbeat = now
                time.sleep(cfg.eval_interval_sec)
                continue
            kill_notified = False

            open_symbols = {tr.symbol for tr in db.open_trades()}

            # 2b) funding-окно: не открываемся перед списанием (00/08/16 UTC)
            to_funding = sec_to_next_funding(now)
            if to_funding < cfg.avoid_funding_window_sec:
                if now - last_heartbeat >= 60:
                    log.info("funding через %.0fс — входы на паузе (окно %.0fс)",
                             to_funding, cfg.avoid_funding_window_sec)
                time.sleep(cfg.eval_interval_sec)
                continue

            # 2c) сессионный фильтр (опц.): только активные часы (London/NY)
            if cfg.session_filter_enabled and not in_active_session(now, cfg):
                if now - last_heartbeat >= 60:
                    log.info("вне активной сессии (UTC h=%d) — входы на паузе",
                             int((now % 86400) // 3600))
                    last_heartbeat = now
                time.sleep(cfg.eval_interval_sec)
                continue

            # 3) сигналы: прогон ВСЕХ стратегий по символу → разрешение конфликта
            for sym in symbols:
                snap = states[sym].snapshot()
                # funnel-диагностика по ВСЕМ символам (наблюдаемость воронки)
                try:
                    _accum_funnel(funnel, diagnose(snap, cfg))
                except Exception:
                    log.exception("diagnose %s failed", sym)
                if sym in open_symbols:
                    for st in strategies:  # не взводимся пока есть позиция
                        st.reset(sym)
                    continue
                if now - cooldown.get(sym, 0.0) < cfg.signal_cooldown_sec:
                    continue
                candidates = []
                for st in strategies:
                    try:
                        s = st.update(snap, now)
                    except Exception:
                        log.exception("strategy %s %s failed", st.name, sym)
                        continue
                    if st.armed(sym):
                        funnel["armed"] += 1
                    if s is not None:
                        candidates.append(s)
                sig = resolve(candidates)
                if sig is None:
                    continue
                funnel["fired"] += 1
                gate = killswitch.can_open(db, cfg, now)
                if not gate.allowed:
                    log.info("gate block: %s", gate.reason)
                    break
                if executor.on_signal(sig) is not None:
                    cooldown[sym] = now
                    open_symbols.add(sym)
                    for st in strategies:
                        st.reset(sym)

            # 4) heartbeat
            if now - last_heartbeat >= 60:
                _heartbeat(states, db, stream, exec_stream)
                _log_funnel(funnel)
                funnel = _new_funnel()
                last_heartbeat = now

            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, cfg.eval_interval_sec - elapsed))
    finally:
        stream.stop()
        if exec_stream is not None:
            exec_stream.stop()
        db.close()
        log.info("scalp_bot остановлен")


def _heartbeat(states: dict[str, SymbolState], db: ScalpDB,
               stream: BybitMarketStream, exec_stream=None) -> None:
    parts = []
    for sym, st in states.items():
        s = st.snapshot()
        fund = f"{s.funding_rate * 100:.3f}%" if s.funding_rate is not None else "?"
        imb = f"{s.ob_imbalance:.2f}" if s.ob_imbalance is not None else "?"
        flag = "STALE" if s.stale else "ok"
        parts.append(f"{sym}:{flag} px={s.last_price} cvdN={len(s.cvd_samples)} "
                     f"imb={imb} fund={fund} liq={len(s.liq_events)}")
    day_pnl = db.realized_pnl_since(now_utc_day())
    exec_ws = exec_stream.is_connected() if exec_stream is not None else "—"
    log.info("HB ws=%s execWs=%s open=%d dayPnL=%.2f | %s",
             stream.is_connected(), exec_ws, db.open_count(), day_pnl,
             " | ".join(parts))
    _log_strategy_stats(db)


def _log_strategy_stats(db: ScalpDB) -> None:
    """Постратегийная сводка за сегодня (UTC): сделки/WR/net PnL.

    WR/PnL информативны для мониторинга, но решения об отключении стратегии —
    только при выборке ≥100 сделок (sample-size.mdc). Здесь — наблюдаемость."""
    stats = db.stats_by_strategy(now_utc_day())
    if not stats:
        return
    for st in stats:
        play.info("📈 [%s] сегодня: сделок=%d, WR=%.0f%% (%d/%d), net=$%.2f",
                  st.strategy, st.trades, st.win_rate * 100, st.wins,
                  st.wins + st.losses, st.pnl_usd)


_FUNNEL_RULES = ("sweep", "div", "reclaim", "momentum", "ob", "liq", "funding")


def _new_funnel() -> dict:
    d = {k: 0 for k in _FUNNEL_RULES}
    d["evals"] = 0
    d["armed"] = 0   # циклов во взводе (после свипа+дивергенции)
    d["fired"] = 0   # фактических входов от детектора
    return d


def _accum_funnel(f: dict, diag: dict | None) -> None:
    if diag is None:
        return
    f["evals"] += 1
    for k in _FUNNEL_RULES:
        if diag.get(k):
            f[k] += 1


def _log_funnel(f: dict) -> None:
    """Воронка за минуту: частота срабатывания каждого правила + взвод/выстрел
    двухфазного детектора. armed=0 → свип+дивергенция не совпадают (нет взвода);
    armed>0 но fired=0 → reclaim/momentum/fee-guard не доходят."""
    n = f.get("evals", 0)
    if n == 0:
        log.info("FUNNEL: нет валидных оценок (данные тонкие/STALE)")
        return
    parts = " ".join(f"{k}={f[k]}" for k in _FUNNEL_RULES)
    log.info("FUNNEL evals=%d | %s | armed=%d FIRED=%d",
             n, parts, f["armed"], f["fired"])
    # плейбук-вердикт простым языком: где сейчас «затык» воронки
    if f["fired"] > 0:
        play.info("📊 за минуту: %d вход(ов) — стратегия дошла до сделки", f["fired"])
    elif f["armed"] > 0:
        play.info("📊 за минуту: взводились, но до выстрела не дошло — "
                  "reclaim/разворот CVD не совпали (нормально на спокойном рынке)")
    elif f["sweep"] == 0:
        play.info("📊 за минуту: свипов нет — рынок без проколов уровней, "
                  "спокойно жду экстремумы")
    elif f["div"] == 0:
        play.info("📊 за минуту: свипы есть, но без дивергенции CVD — это импульс, "
                  "а не поглощение, во взвод не беру (так и задумано)")
    else:
        play.info("📊 за минуту: есть свипы и дивергенции, но взвод не удержался — "
                  "проверь div_min_late_trades/окно, если так каждую минуту")


def _flatten_on_start(client, db, symbols: list[str]) -> None:
    """Старт «с чистого листа»: закрыть открытые позиции по символам и
    реконсилить зависшие open-сделки в БД под новую логику входа/выхода."""
    now = time.time()
    for sym in symbols:
        try:
            pos = client.get_position(sym)
        except Exception:
            log.exception("flatten: get_position %s failed", sym)
            continue
        if pos and pos.size > 0:
            client.close_market(sym, pos.side, pos.size, f"scalp_flat_{int(now)}")
            log.info("flatten: закрыта позиция %s %s size=%.6f", sym, pos.side, pos.size)
    for tr in db.open_trades():
        pnl = None
        try:
            pnl = client.closed_pnl(tr.symbol, qty=tr.qty,
                                    since_ms=int(tr.ts_open * 1000))
        except Exception:
            log.exception("flatten: closed_pnl %s failed", tr.symbol)
        db.mark_closed(tr.id, exit_price=tr.entry, pnl_usd=pnl or 0.0,
                       fees_usd=0.0, close_reason="restart_flat", ts_close=now)
        log.info("flatten: реконсил open-сделки #%d %s pnl=%.4f", tr.id,
                 tr.symbol, pnl or 0.0)


def in_active_session(now: float, cfg) -> bool:
    """Текущий UTC-час входит в активные торговые часы (cfg.active_hours)."""
    hour = int((now % 86400.0) // 3600.0)
    return hour in cfg.active_hours


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
