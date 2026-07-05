"""yorsh_bot main loop — изолированный сканер «ёрш»-паттернов (data-only).

Фаза 1: коллектор MEXC/Bitget spot → локальная книга → recorder сырой ленты
→ density-tracker → ёрш-сканер → SQLite. **Без торговли** (модуля trading/
нет).

M0: heartbeat-заглушка.
M1: MEXC-коллектор (WS protobuf + REST snapshot = init).
M2: Bitget-коллектор (JSON books/trade, seq/pseq gap → resubscribe).
M3: universe-менеджер + SubscriptionSupervisor. Режим определяется
   ``YORSH_SYMBOLS_STATIC``: если задан — статические подписки (для тестов/
   отладки); если пуст — динамическая вселенная из REST (продакшн), ротация
   раз в ``YORSH_UNIVERSE_REFRESH_HOURS``, protected = active-кандидаты БД.
M4+: density-tracker, ёрш-сканер.

Запуск: ``python -m yorsh_bot`` или ``yorsh-bot`` (CLI из pyproject).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from yorsh_bot.config.settings import load_settings
from yorsh_bot.data.orderbook import LocalOrderBook
from yorsh_bot.data.recorder import RawRecorder
from yorsh_bot.data.supervisor import SubscriptionSupervisor
from yorsh_bot.data.universe import UniverseManager
from yorsh_bot.exchanges.base import BookSnapshot, DepthDiff, Trade
from yorsh_bot.exchanges.bitget import BitgetSpotClient
from yorsh_bot.exchanges.mexc import MexcSpotClient
from yorsh_bot.state.db import YorshDB

log = logging.getLogger("yorsh_bot")

_shutdown = False
_HEARTBEAT_SEC = 30.0


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Получен сигнал %d, завершаю...", signum)


def _build_client(settings, db: YorshDB, exchange: str, syms: list[str],
                  recorder: RawRecorder):
    """Создать коллектор с привязанными колбэками (книга/recorder/health).

    Per-exchange: version_mode (MEXC range / Bitget seq), snapshot-apply rule
    (MEXC REST=init; Bitget WS-snapshot=init, REST=record-only), Bitget
    gap → request_reinit.
    """
    version_mode = "range" if exchange == "mexc" else "seq"
    books = {s: LocalOrderBook(exchange, s, version_mode=version_mode) for s in syms}

    async def on_trade(t: Trade) -> None:
        recorder.write_trade(t)

    async def on_diff(d: DepthDiff) -> None:
        book = books.get(d.symbol)
        if book is None:
            return
        if not book._snapshot_loaded:  # noqa: SLF001
            book.buffer_pre_snapshot(d)
            recorder.write_diff(d)
            return
        if book.needs_reinit(d):
            db.log_health(exchange=exchange, event="gap", symbol=d.symbol,
                          detail=f"from={d.prev_seq} to={d.seq} "
                                f"last={book.last_version} → reinit")
            return
        book.apply_diff(d)
        recorder.write_diff(d)

    async def on_snapshot(s: BookSnapshot) -> None:
        book = books.get(s.symbol)
        apply_to_book = (s.source == "ws_books") or (exchange == "mexc")
        if book is not None and apply_to_book:
            book.apply_snapshot(s)
        recorder.write_snapshot(s)

    async def on_health(ev: str, det: str | None) -> None:
        db.log_health(exchange=exchange, event=ev, detail=det)

    client_cls = MexcSpotClient if exchange == "mexc" else BitgetSpotClient
    client = client_cls(
        syms, on_trade=on_trade, on_diff=on_diff,
        on_snapshot=on_snapshot, on_health=on_health)

    if exchange == "bitget":
        async def on_diff_bitget(d: DepthDiff) -> None:
            book = books.get(d.symbol)
            if book is not None and book._snapshot_loaded and book.needs_reinit(d):  # noqa: SLF001
                db.log_health(exchange=exchange, event="gap", symbol=d.symbol,
                              detail=f"pseq={d.prev_seq} seq={d.seq} "
                                    f"last={book.last_version} → resubscribe")
                client.request_reinit(d.symbol)
                return
            await on_diff(d)
        client.on_diff = on_diff_bitget  # type: ignore[assignment]

    return client


def _recorder(settings, db: YorshDB, exchange: str) -> RawRecorder:
    return RawRecorder(
        settings.data_dir, exchange=exchange,
        retention_days=settings.raw_retention_days, max_gb=settings.raw_max_gb,
        health_log=lambda ev, det: db.log_health(exchange=exchange,
                                                  event=ev, detail=det))


# ─── static mode (M1/M2) ─────────────────────────────────────────────────

async def _run_static(settings, db: YorshDB) -> list[asyncio.Task]:
    tasks: list[asyncio.Task] = []
    syms = settings.static_symbol_list
    if not syms:
        return tasks
    for exch in settings.exchange_list:
        rec = _recorder(settings, db, exch)
        client = _build_client(settings, db, exch, syms, rec)
        tasks.append(asyncio.create_task(client.run(), name=f"{exch}-static"))
    return tasks


# ─── dynamic mode (M3) ───────────────────────────────────────────────────

def _active_candidates(db: YorshDB, exchange: str) -> set[str]:
    """Protected-символы = active-кандидаты по бирже (не отписываемся)."""
    rows = db.conn.execute(
        "SELECT symbol FROM candidates WHERE exchange=? AND status='active'",
        (exchange,)).fetchall()
    return {r["symbol"] for r in rows}


async def _run_dynamic(settings, db: YorshDB) -> tuple[SubscriptionSupervisor, asyncio.Task]:
    mgr = UniverseManager(
        settings,
        log_event=lambda exch, ev, sym: db.log_universe(exchange=exch, event=ev,
                                                       symbol=sym),
        get_protected=lambda exch: _active_candidates(db, exch))
    # recorder — по одному на биржу (общий для батчей, single event loop).
    recorders: dict[str, RawRecorder] = {
        e: _recorder(settings, db, e) for e in settings.exchange_list}

    def factory(exch: str, syms: list[str]):
        return _build_client(settings, db, exch, syms, recorders[exch])

    sup = SubscriptionSupervisor(mgr, factory)
    # начальный reconcile + периодический loop
    for exch in settings.exchange_list:
        await sup.reconcile(exch)
    loop_task = asyncio.create_task(sup.run_loop(stop=lambda: _shutdown),
                                    name="supervisor-loop")
    return sup, loop_task


async def _amain(settings, db: YorshDB) -> None:
    tasks: list[asyncio.Task] = []
    sup: SubscriptionSupervisor | None = None
    if settings.static_symbol_list:
        tasks = await _run_static(settings, db)
        log.info("yorsh_bot started (static): collectors=%d", len(tasks))
    else:
        sup, loop_t = await _run_dynamic(settings, db)
        tasks = [loop_t]
        log.info("yorsh_bot started (dynamic universe): batches=%d",
                 len(sup.tasks))

    last_beat = 0.0
    try:
        while not _shutdown:
            now = time.time()
            if now - last_beat >= _HEARTBEAT_SEC:
                for exch in settings.exchange_list:
                    db.log_health(exchange=exch, event="heartbeat")
                log.info("heartbeat: collectors=%d", len(tasks))
                last_beat = now
            await asyncio.sleep(1.0)
    finally:
        if sup is not None:
            await sup.stop_all()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("yorsh_bot stopped.")


def run() -> None:
    global _shutdown
    cfg = load_settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    db = YorshDB(cfg.data_dir)
    log.info("yorsh_bot startup: data_dir=%s exchanges=%s db=%s mode=%s",
             cfg.data_dir, ",".join(cfg.exchange_list) or "(none)", db.path,
             "static" if cfg.static_symbol_list else "dynamic")
    try:
        asyncio.run(_amain(cfg, db))
    finally:
        db.close()


if __name__ == "__main__":
    run()
