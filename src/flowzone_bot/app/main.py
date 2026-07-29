"""flowzone_bot main loop — order-flow бот Bybit (детерминированный, без LLM).

ФАЗА 1 (каркас, observe): бот подключается к Bybit, выбирает вселенную
(авто-селектор scalp), подписывается на публичный поток сделок + стакан,
складывает микроструктуру (footprint-принты) и логирует heartbeat. НИЧЕГО НЕ
ТОРГУЕТ (paper/observe) — trading_enabled по умолчанию False (TASKSPEC §1
«Демо сначала»). Volume Profile / контекст / зоны / триггер / исполнение —
последующие фазы.

Каждые ``eval_interval_sec`` (default 1с):
0. Ротация вселенной (раз в universe_refresh_sec).
1. Снимок микроструктуры по символам (наблюдаемость).
2. Heartbeat-лог раз в 60с.

Запуск: ``python -m flowzone_bot.app.main``.
"""
from __future__ import annotations

import logging
import signal
import time

from flowzone_bot.analysis.auction import AuctionTracker
from flowzone_bot.analysis.context import classify
from flowzone_bot.analysis.session import (in_session, parse_windows,
                                           session_start_ts)
from flowzone_bot.analysis.strategy import evaluate
from flowzone_bot.analysis.swings import Swing, find_swings
from flowzone_bot.analysis.telemetry import DirectionTelemetry
from flowzone_bot.analysis.volume_profile import (VolumeProfile, build_profile,
                                                  merge_profiles,
                                                  value_areas_overlap)
from flowzone_bot.config.settings import load_settings
from flowzone_bot.data.aggregates import SymbolState, TradePrint
from flowzone_bot.data.exec_stream import BybitExecStream
from flowzone_bot.data.market_stream import BybitMarketStream
from flowzone_bot.data.momentum_universe import select_momentum_universe
from flowzone_bot.data.print_store import PrintStore
from flowzone_bot.data.universe import (apply_pins, filter_tickers,
                                        hourly_range_rvol, pad_universe,
                                        rank_rows)
from flowzone_bot.safety import killswitch
from flowzone_bot.state.db import FlowzoneDB
from flowzone_bot.telegram.notifier import TelegramNotifier
from flowzone_bot.trading.client import FlowzoneBybitClient
from flowzone_bot.trading.executor import Executor

log = logging.getLogger("flowzone_bot")
play = logging.getLogger("flowzone_bot.play")  # пошаговый нарратив

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
    mode = "LIVE(demo)" if cfg.trading_enabled else "OBSERVE"

    db = FlowzoneDB(cfg.data_dir)

    # REST-клиент создаём при наличии кред — он нужен и для авто-вселенной
    # (get_tickers/get_kline), и для торговли. trading_enabled гейтит ТОЛЬКО
    # постановку ордеров (фаза 4+), не подключение к данным.
    client = None
    if cfg.bybit_api_key and cfg.bybit_api_secret:
        client = FlowzoneBybitClient(cfg.bybit_api_key, cfg.bybit_api_secret,
                                     demo=cfg.bybit_demo, category=cfg.bybit_category)
        log.info("Bybit REST: demo=%s category=%s", cfg.bybit_demo, cfg.bybit_category)
        if cfg.auto_universe_enabled:
            picked = _select_universe(client, cfg)
            if picked:
                symbols = picked
                log.info("авто-вселенная (метод=%s, топ-%d): %s",
                         cfg.universe_method, cfg.universe_top_n,
                         ",".join(symbols))
            else:
                log.warning("авто-вселенная пуста — fallback на FLOWZONE_SYMBOLS=%s",
                            ",".join(symbols))
    elif cfg.trading_enabled:
        log.error("trading_enabled=true, но нет FLOWZONE_BYBIT_API_KEY/SECRET — выходим")
        return
    else:
        log.warning("нет Bybit-кред — observe по fallback-символам без авто-вселенной")

    log.info("flowzone_bot старт | mode=%s | symbols=%s | риск=$%.0f/сделку | "
             "kill day/total=$%.0f/$%.0f", mode, ",".join(symbols),
             cfg.risk_per_trade_usd, cfg.max_daily_loss_usd, cfg.max_total_loss_usd)

    states: dict[str, SymbolState] = {
        s: SymbolState(s, trade_window_sec=cfg.trade_window_sec,
                       ob_levels=cfg.ob_levels)
        for s in symbols
    }
    # session windows нужны SymbolState для per-session якоря VP (A2, канон §2)
    session_windows = (parse_windows(cfg.session_windows_utc)
                       if cfg.session_gate_enabled else [])
    for st in states.values():
        st.set_session_windows(session_windows)
    # PrintStore: persist тиков в БД для per-swing профиля (A2, канон §3).
    # Запускаем daemon-поток batched-flush; ingest вызывается из WS-callback.
    print_store = PrintStore(db, flush_interval_sec=cfg.print_flush_interval_sec,
                             prune_older_than_sec=cfg.print_prune_older_sec)
    print_store.start()
    for st in states.values():
        st._print_store = print_store  # инъекция после construction
    # Размер корзины footprint-профиля = tick_size × vp_bucket_ticks (нужен
    # REST-инструмент). Без клиента VP не строится (канон требует REST/ликвидности).
    if client is not None:
        _apply_vp_buckets(client, cfg, states)
        # рестарт mid-session: восстановить per-session профиль из persisted-
        # принтов (иначе контекст часами warming при живых данных в БД).
        _seed_session_vp(db, states, session_windows)
    stream = BybitMarketStream(symbols, states, category=cfg.bybit_category,
                               testnet=cfg.bybit_testnet)
    stream.start()

    # приватный поток исполнений (источник истины по net P&L) — только LIVE
    exec_stream = None
    if client is not None and cfg.trading_enabled:
        exec_stream = BybitExecStream(cfg.bybit_api_key, cfg.bybit_api_secret,
                                      demo=cfg.bybit_demo, testnet=cfg.bybit_testnet)
        exec_stream.start()

    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id,
                                enabled=cfg.telegram_enabled,
                                prefix=cfg.telegram_prefix)
    if notifier.active:
        notifier.send(f"🚀 flowzone_bot старт | {mode} | {','.join(symbols)}")

    executor = Executor(db, cfg, client, notifier=notifier)

    cooldown: dict[str, float] = {}
    swing_cache: dict[str, tuple[float, list[Swing]]] = {}
    # Sticky-направление аукциона (канон §2: пробой+acceptance, держим до
    # встречного структурного пробоя — не переворачиваемся на откате).
    auction = AuctionTracker()
    # Телеметрия устойчивости направления (наблюдаемость, НЕ гейтит вход):
    # initiative-alignment / dwell / дистанция до day-экстремумов / volume-shock.
    # Пишется в reasons сделки + лог переворота латча для последующего mining
    # на ≥100 сделках (sample-size.mdc). См. analysis/telemetry.py.
    telemetry = DirectionTelemetry(big_trade_pct=cfg.big_trade_pct)
    if cfg.session_gate_enabled:
        log.info("session gate: окна UTC %s", cfg.session_windows_utc)
    last_heartbeat = 0.0
    last_universe = time.time()

    try:
        while not _shutdown:
            loop_start = time.monotonic()
            now = time.time()

            # 0) ротация вселенной (бот сам выбирает монеты)
            if (client is not None and cfg.auto_universe_enabled
                    and now - last_universe >= cfg.universe_refresh_sec):
                last_universe = now
                try:
                    stream, states, symbols = _rotate_universe(
                        client, cfg, db, stream, states, symbols,
                        session_windows, print_store)
                    _apply_vp_buckets(client, cfg, states)
                    _seed_session_vp(db, states, session_windows)
                except Exception:
                    log.exception("rotate_universe failed")

            # забрать исполнения из приватного WS → атрибуция к сделкам
            if exec_stream is not None:
                try:
                    executor.ingest_executions(exec_stream.drain())
                except Exception:
                    log.exception("ingest_executions failed")

            # сопровождение открытых (фаза 4: биржевые TP/SL + сверка PnL).
            # swings нужны для BE-lock стадии 1 (канон 39:00 «break this level» —
            # пробой предыдущего swing-уровня); собираем по open-символам.
            try:
                open_syms = {tr.symbol for tr in db.open_trades()}
                swings_by_sym = {
                    sym: _swings_for(client, cfg, sym, swing_cache, now)
                    for sym in open_syms
                }
                executor.manage(states, swings_by_sym)
            except Exception:
                log.exception("manage failed")

            # session gate (§6.1): входы только в активные сессии London/NY
            in_active_session = in_session(now, session_windows)

            # DirectionTelemetry обновляется для ВСЕХ символов до open/cooldown
            # гейтов. Это наблюдаемость (не торговый фильтр): shock/dwell должны
            # видеть непрерывную ленту, иначе интервалы пропадают именно во время
            # открытой позиции или cooldown.
            _update_direction_telemetry(
                states, client, cfg, swing_cache, telemetry, now)

            # killswitch: блокирует новые входы
            killed = killswitch.is_killed(db, cfg, now)
            if killed.allowed and in_active_session:
                _scan_signals(states, db, cfg, executor, cooldown, now,
                              client, swing_cache, auction, telemetry)
            elif not killed.allowed and now - last_heartbeat >= 60:
                log.warning("KILLSWITCH: %s — входы заблокированы", killed.reason)

            # heartbeat раз в 60с (+ контекст аукциона по символам)
            if now - last_heartbeat >= 60:
                _heartbeat(states, db, stream, cfg, in_active_session, auction)
                _persist_session_profiles(states, db, cfg, now)
                last_heartbeat = now

            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, cfg.eval_interval_sec - elapsed))
    finally:
        stream.stop()
        if exec_stream is not None:
            exec_stream.stop()
        print_store.stop()
        db.close()
        log.info("flowzone_bot остановлен")


def _scan_signals(states: dict[str, SymbolState], db: FlowzoneDB, cfg,
                  executor: Executor, cooldown: dict[str, float],
                  now: float, client,
                  swing_cache: dict[str, tuple[float, list[Swing]]],
                  auction: AuctionTracker,
                  telemetry: DirectionTelemetry | None = None) -> None:
    """Прогон чеклиста входа по символам: контекст → зона → absorption → Signal
    (цель = ближайший swing, §5.3). Один открытый сетап на символ; rate/позиции —
    killswitch.can_open. ``telemetry`` — наблюдаемость устойчивости направления
    (пишется в reasons и в лог переворота латча, вход НЕ гейтит)."""
    open_symbols = {tr.symbol for tr in db.open_trades()}
    for sym, st in states.items():
        if sym in open_symbols:
            continue
        # reload (§5.3): после недавнего ВЫИГРЫША — короткий cooldown, чтобы быстро
        # перезарядиться на следующей зоне по тренду; иначе обычный анти-даблклик.
        win_ts = executor.last_win_ts(sym)
        cd = (cfg.reload_cooldown_sec
              if win_ts is not None and now - win_ts < cfg.signal_cooldown_sec
              else cfg.signal_cooldown_sec)
        if now - cooldown.get(sym, 0.0) < cd:
            continue
        snap = st.snapshot()
        # swings нужны и для латча направления (структурный пробой), и для цели,
        # и для per-swing якоря профиля зоны (A2).
        swings = _swings_for(client, cfg, sym, swing_cache, now)
        prev_dir = auction.peek(sym)
        _ctx_profile, ctx = _context_for(snap, cfg, auction=auction,
                                         swings=swings, now=now, db=db)
        # лог переворота/установки латча + телеметрия (mining флапов, D-audit
        # 06.07: кейсы #531/#532 — ложный переворот после volume-shock).
        cur_dir = auction.peek(sym)
        if telemetry is not None and cur_dir != prev_dir and cur_dir is not None:
            _refresh_preceding_initiative(
                db, cfg, telemetry, sym, now)
            log.info("auction flip %s: %s → %s | %s", sym,
                     prev_dir or "none", cur_dir,
                     telemetry.fmt(sym, now,
                                   "long" if cur_dir == "trend_up" else "short",
                                   snap.last_price))
        if ctx is None or not ctx.is_trend:
            continue
        # per-swing профиль зоны (канон §3: профиль ПРЕДЫДУЩЕЙ swing-точки из
        # исполненного потока, окно [ts prev swing, now]). None → нет зоны.
        # None → зоны нет; сетап hook (C5) её не требует, поэтому не выходим.
        swing_profile = _swing_profile_for(db, cfg, sym, swings,
                                           ctx.trade_side, snap.vp_bucket_size,
                                           now)
        hook_prints = _hook_prints_for(db, cfg, sym, snap, now)
        try:
            sig = evaluate(snap, ctx, swing_profile, cfg=cfg, swings=swings,
                           hook_prints=hook_prints)
        except Exception:
            log.exception("evaluate %s failed", sym)
            continue
        if sig is None:
            continue
        # телеметрия устойчивости направления → в reasons (persist в БД для
        # mining на ≥100 сделках; вход НЕ гейтит).
        if telemetry is not None:
            try:
                # Initiative берём из ЗАВЕРШЁННОЙ предыдущей M5-ноги
                # [now-2×window, now-window), не из текущего absorption-окна.
                _refresh_preceding_initiative(
                    db, cfg, telemetry, sym, now)
                sig.reasons.append(telemetry.fmt(sym, now, sig.side,
                                                 snap.last_price))
            except Exception:
                log.exception("telemetry fmt %s failed", sym)
        gate = killswitch.can_open(db, cfg, now)
        if not gate.allowed:
            log.info("gate block: %s", gate.reason)
            break
        if executor.on_signal(sig) is not None:
            cooldown[sym] = now
            open_symbols.add(sym)


def _update_direction_telemetry(
        states: dict[str, SymbolState], client, cfg,
        swing_cache: dict[str, tuple[float, list[Swing]]],
        telemetry: DirectionTelemetry, now: float) -> None:
    """Обновить непрерывные telemetry-фичи для всех символов.

    Не гейтит вход. ``_swings_for`` использует существующий TTL-cache, поэтому
    REST вызывается не чаще ``swing_cache_sec`` на символ.
    """
    for sym, st in states.items():
        try:
            snap = st.snapshot()
            swings = _swings_for(client, cfg, sym, swing_cache, now)
            telemetry.update(sym, now, snap, swings)
        except Exception:
            log.exception("telemetry update %s failed", sym)


def _refresh_preceding_initiative(
        db: FlowzoneDB, cfg, telemetry: DirectionTelemetry,
        symbol: str, now: float) -> None:
    """Initiative предшествующей ноги: persisted prints предыдущего M5-окна.

    Текущее окно ``[now-window, now]`` используется absorption entry-trigger и
    намеренно исключено. Функция вызывается только на signal/auction-flip, а не
    каждый eval-loop, поэтому SQLite не получает лишнюю нагрузку.
    """
    window = float(cfg.absorption_window_sec)
    if window <= 0:
        telemetry.refresh_preceding_initiative(symbol, [])
        return
    try:
        rows = db.prints_since(symbol, now - 2.0 * window, now - window)
        trades = [TradePrint(float(ts), float(price), float(size), str(side))
                  for ts, price, size, side in rows]
        telemetry.refresh_preceding_initiative(symbol, trades)
    except Exception:
        log.exception("telemetry preceding initiative %s failed", symbol)
        telemetry.refresh_preceding_initiative(symbol, [])


def _hook_prints_for(db: FlowzoneDB, cfg, symbol: str, snap,
                     now: float) -> list[TradePrint] | None:
    """Persisted-поток для сетапа hook (C5): вылазка за границу VA и возврат.

    5-минутного окна снапшота не хватает — failed auction разворачивается
    дольше. Читаем принты с начала сессии, но не глубже ``hook_lookback_sec``
    (операционный лимит объёма чтения и свежести сетапа).
    """
    if not getattr(cfg, "hook_enabled", False):
        return None
    anchor = snap.vp_session_start
    if anchor is None:
        return None
    since = max(float(anchor), now - float(cfg.hook_lookback_sec))
    try:
        rows = db.prints_since(symbol, since, now)
    except Exception:
        log.exception("hook prints %s failed", symbol)
        return None
    return [TradePrint(float(ts), float(price), float(size), str(side))
            for ts, price, size, side in rows]


def _swings_for(client, cfg, symbol: str,
                cache: dict[str, tuple[float, list[Swing]]],
                now: float) -> list[Swing]:
    """Swing-точки M5 по символу с TTL-кэшем (M5-бар обновляется раз в 5 мин).
    Возвращает Swing с ts (время swing-бара) — нужно для per-swing профиля (A2,
    канон §3: окно = [ts предыдущего swing, now]). Без клиента — пусто (цель/
    per-swing профиль недоступны → сделки нет)."""
    if client is None:
        return []
    cached = cache.get(symbol)
    if cached is not None and now - cached[0] < cfg.swing_cache_sec:
        return cached[1]
    try:
        kl = client.get_kline(symbol, cfg.swing_kline_interval,
                              limit=cfg.swing_kline_limit)
    except Exception:
        log.exception("swing get_kline %s failed", symbol)
        return cache.get(symbol, (0.0, []))[1]
    # Bybit v5 kline row: [startTime(ms), open, high, low, close, volume, ...]
    # ВАЖНО: get_kline отдаёт DESC (новые сверху, см. docstring клиента) →
    # разворачиваем в хронологический порядок. Иначе Swing.idx инвертируется и
    # «последний swing» (max idx в _last_swing_price/_recent_extreme) — самый
    # СТАРЫЙ бар окна (~16ч назад): BE-lock у шортов срабатывал сразу после
    # филла, у лонгов — никогда; латч AuctionTracker сверялся с древним уровнем.
    kl = list(reversed(kl))
    ts = [float(c[0]) / 1000.0 for c in kl]
    highs = [float(c[2]) for c in kl]
    lows = [float(c[3]) for c in kl]
    swings = find_swings(highs, lows, left=cfg.swing_left,
                         right=cfg.swing_right, ts=ts)
    cache[symbol] = (now, swings)
    return swings


def _swing_profile_for(db, cfg, symbol: str, swings: list[Swing],
                       side: str, bucket_size: float, now: float):
    """Per-swing профиль зоны (канон §3): профиль ПРЕДЫДУЩЕЙ swing-точки —
    исполненный поток (footprint) в окне [ts предыдущего swing, now], собранный
    из SQLite ``prints``. Окно = от ts последнего подтверждённого swing-экстремума
    по направлению continuation до now (канон «previous swing point»).

    Возвращает (VolumeProfile | None). None если нет swing-якоря / bucket / БД."""
    from flowzone_bot.analysis.volume_profile import build_profile_from_prints
    if db is None or bucket_size <= 0 or not swings:
        return None
    # предыдущий swing по направлению continuation: для шорта берём последний
    # swing high (резистанс reload сверху), для лонга — последний swing low.
    # Это «previous swing point» канона — куда цена откатится для reload.
    kind = "high" if side == "short" else "low"
    cands = [s for s in swings if s.kind == kind and s.ts > 0]
    if not cands:
        return None
    anchor = max(cands, key=lambda s: s.ts).ts
    prints = db.prints_since(symbol, anchor, now)
    return build_profile_from_prints(prints, bucket_size,
                                     value_area_pct=cfg.value_area_pct)


def _context_for(snap, cfg, *, auction: AuctionTracker | None = None,
                 swings: list[Swing] | None = None, now: float | None = None,
                 db: FlowzoneDB | None = None):
    """Контекст аукциона по символу: режим по ФОРМЕ per-SESSION footprint-профиля
    (направленный acceptance вне value area, STRATEGY §2). Якорь профиля — старт
    текущей канон-сессии (snap.vp_session_start, C4: одна сессия с основным
    объёмом). None если вне сессии или профиль ещё не накоплен (BALANCE → не
    торгуем).

    C1: если передан ``db`` и включён ``profile_merge_enabled`` — контекст
    считается по composite-профилю (текущая сессия + перекрывающиеся по value
    area предыдущие, канон 31:14). Возвращается ИМЕННО composite: канон
    подчёркивает, что merge даёт «really precise value area low point», то есть
    уточнённые границы и есть рабочие уровни.

    Если передан ``auction`` — мгновенный режим латчится (канон §2: держим
    направление, переворот только по встречному структурному пробою ``swings``).
    Без трекера возвращается мгновенный classify (для heartbeat-дисплея)."""
    if snap.vp_session_start is None:
        # вне активной сессии — per-session профиль не строим → не торгуем
        return None, None
    profile = build_profile(snap.vp_buckets, snap.vp_bucket_size,
                            value_area_pct=cfg.value_area_pct)
    if profile is None:
        return None, None
    if db is not None and cfg.profile_merge_enabled:
        profile = _merged_session_profile(db, cfg, snap, profile)
    inst = classify(profile, snap.last_price, accept_frac=cfg.context_accept_frac,
                    value_area_pct=cfg.value_area_pct,
                    shape_gate=cfg.profile_shape_enabled)
    if auction is None:
        return profile, inst
    ctx = auction.update(snap.symbol, inst, snap.last_price, swings or [], now=now)
    return profile, ctx


def _merged_session_profile(db: FlowzoneDB, cfg, snap,
                            current: VolumeProfile) -> VolumeProfile:
    """Composite текущей сессии с перекрывающимися предыдущими (C1, канон 31:14).

    Сливаем только те прошлые сессии, чья value area пересекается с текущей —
    канон merge-ит профили, «overlapping on the same level», а не подряд идущие.
    Любая ошибка/несовместимость → возвращаем исходный профиль (merge не должен
    ломать торговый путь).
    """
    try:
        rows = db.recent_session_profiles(snap.symbol, snap.vp_session_start,
                                          cfg.profile_merge_lookback)
    except Exception:
        log.exception("session profiles read %s failed", snap.symbol)
        return current
    group = [current]
    for _start, bucket_size, buckets in rows:
        if bucket_size != current.bucket_size:
            continue
        prev = build_profile(buckets, bucket_size,
                             value_area_pct=cfg.value_area_pct)
        if prev is None or not value_areas_overlap(current, prev):
            continue
        group.append(prev)
    if len(group) == 1:
        return current
    return merge_profiles(group, value_area_pct=cfg.value_area_pct) or current


def _persist_session_profiles(states: dict[str, SymbolState], db: FlowzoneDB,
                              cfg, now: float) -> None:
    """Сохранить профили текущих сессий (C1) + retention.

    Профили прошлых сессий нельзя пересобрать из `prints` (retention 6ч),
    поэтому текущий сессионный профиль периодически перезаписывается в БД и
    после закрытия сессии остаётся итоговым снимком.
    """
    if not cfg.profile_merge_enabled:
        return
    for sym, st in states.items():
        try:
            snap = st.snapshot()
            if snap.vp_session_start is None or not snap.vp_buckets:
                continue
            db.save_session_profile(sym, snap.vp_session_start,
                                    snap.vp_bucket_size, snap.vp_buckets, ts=now)
        except Exception:
            log.exception("save session profile %s failed", sym)
    keep_days = max(cfg.profile_merge_lookback, 1) + 4
    try:
        db.prune_session_profiles_before(now - keep_days * 86400.0)
    except Exception:
        log.exception("prune session profiles failed")


def _heartbeat(states: dict[str, SymbolState], db: FlowzoneDB,
               stream: BybitMarketStream, cfg, session_active: bool = True,
               auction: AuctionTracker | None = None) -> None:
    parts = []
    for sym, st in states.items():
        s = st.snapshot()
        imb = f"{s.ob_imbalance:.2f}" if s.ob_imbalance is not None else "?"
        flag = "STALE" if s.stale else "ok"
        _profile, ctx = _context_for(s, cfg)  # мгновенный (дисплей)
        if ctx is not None and ctx.vah is not None:
            # показываем залатченное направление аукциона + мгновенный режим
            latched = auction.peek(sym) if auction is not None else None
            shown = latched or ctx.state
            inst_tag = f"(inst={ctx.state})" if latched and latched != ctx.state else ""
            ctxs = (f" ctx={shown}{inst_tag} VA=[{ctx.val:.4g},{ctx.vah:.4g}] "
                    f"acc↑{ctx.accept_above:.0%}↓{ctx.accept_below:.0%}")
        else:
            ctxs = " ctx=warming"
        parts.append(f"{sym}:{flag} px={s.last_price} ticks={len(s.trades)} "
                     f"imb={imb}{ctxs}")
    day_pnl = db.realized_pnl_since(_now_utc_day())
    log.info("HB ws=%s session=%s open=%d dayPnL=%.2f | %s",
             stream.is_connected(), "active" if session_active else "closed",
             db.open_count(), day_pnl, " | ".join(parts))


def _seed_session_vp(db, states: dict[str, SymbolState],
                     session_windows: list[tuple[float, float]]) -> None:
    """Бэкфилл per-session профиля из persisted-принтов (таблица ``prints``)
    для символов с пустым профилем — рестарт/ротация mid-session. Требует уже
    установленного vp_bucket_size (звать ПОСЛЕ _apply_vp_buckets). Вне активной
    сессии — no-op (профиль не строим)."""
    if db is None or not session_windows:
        return
    now = time.time()
    anchor = session_start_ts(now, session_windows)
    if anchor is None:
        return
    for sym, st in states.items():
        try:
            prints = db.prints_since(sym, anchor, now)
        except Exception:
            log.exception("seed_vp prints_since %s failed", sym)
            continue
        if prints:
            st.seed_vp(prints, anchor)
            log.info("seed_vp %s: восстановлен session-профиль из %d принтов "
                     "(якорь %.0f)", sym, len(prints), anchor)


def _apply_vp_buckets(client, cfg, states: dict[str, SymbolState]) -> None:
    """Задать ширину корзины footprint-профиля = tick_size × vp_bucket_ticks для
    символов, где она ещё не установлена (REST get_instruments_info)."""
    for sym, st in states.items():
        try:
            info = client.instrument(sym)
        except Exception:
            log.exception("instrument %s failed", sym)
            continue
        if info and info.tick_size > 0:
            st.set_vp_bucket_size(info.tick_size * cfg.vp_bucket_ticks)


def _fresh_rvol(client, cfg, rows: list[dict]) -> dict[str, float]:
    """Свежий RVOL по амплитуде для прошедших 24h-фильтр символов. fail-open:
    символ без klines не попадает в словарь → нейтральный fallback."""
    rvol: dict[str, float] = {}
    for m in rows[:40]:  # safety-кап на число get_kline за рефреш (rate-limit)
        try:
            kl = client.get_kline(m["symbol"], "5", limit=289)
            v = hourly_range_rvol(kl)
            if v is not None:
                rvol[m["symbol"]] = v
        except Exception:
            log.exception("rvol get_kline %s failed", m["symbol"])
    return rvol


def _select_universe(client, cfg) -> list[str]:
    """Монеты под стратегию. Метод задаётся cfg.universe_method:
    - "momentum" — ТОП по 24h росту/падению + порог оборота (как в ролике,
      data/momentum_universe.py), без анти-памп кэпа.
    - "rvol" (default) — 24h hard-фильтр → свежий intraday RVOL-гейт+ранж →
      floor «минимум N монет» → пины. См. data/universe.py."""
    tickers = client.get_tickers()
    if cfg.universe_method.strip().lower() == "momentum":
        picked = select_momentum_universe(
            tickers,
            top_n=cfg.universe_top_n,
            min_turnover=cfg.momentum_min_turnover_usd,
            min_abs_change_pct=cfg.momentum_min_change_pct,
            max_spread_bps=cfg.momentum_max_spread_bps,
            direction=cfg.momentum_direction.strip().lower())
        return apply_pins(picked, cfg.universe_pin_list, cfg.universe_top_n)
    rows = filter_tickers(
        tickers,
        min_turnover=cfg.universe_min_turnover_usd,
        min_range_pct=cfg.universe_min_range_pct,
        max_range_pct=cfg.universe_max_range_pct,
        max_spread_bps=cfg.universe_max_spread_bps)
    ranked: list[str] = []
    if rows:
        rvol = _fresh_rvol(client, cfg, rows) if cfg.universe_min_rvol > 0 else {}
        if cfg.universe_min_rvol > 0:
            kept = [m for m in rows
                    if rvol.get(m["symbol"], cfg.universe_min_rvol) >= cfg.universe_min_rvol]
            rows = kept or rows
        ranked = rank_rows(rows, top_n=cfg.universe_top_n, vol_metric=rvol)
    if cfg.universe_min_symbols > 0 and len(ranked) < cfg.universe_min_symbols:
        pool = filter_tickers(
            tickers,
            min_turnover=cfg.universe_min_turnover_usd,
            min_range_pct=0.0,
            max_range_pct=cfg.universe_max_range_pct,
            max_spread_bps=cfg.universe_max_spread_bps)
        padded = pad_universe(ranked, pool, cfg.universe_min_symbols)
        if len(padded) > len(ranked):
            log.info("вселенная ниже floor (%d < %d) — добор из liquidity-pool: +%s",
                     len(ranked), cfg.universe_min_symbols,
                     ",".join(padded[len(ranked):]))
        ranked = padded
    return apply_pins(ranked, cfg.universe_pin_list, cfg.universe_top_n)


def _rotate_universe(client, cfg, db, stream, states, symbols,
                     session_windows, print_store):
    """Пересмотр вселенной. Символ с открытой позицией НЕ выкидываем. Существующие
    SymbolState переиспользуем (footprint-окно переживает рестарт WS)."""
    picked = _select_universe(client, cfg)
    if not picked:
        log.warning("ротация: авто-вселенная пуста — оставляю текущие символы")
        return stream, states, symbols
    open_syms = {tr.symbol for tr in db.open_trades()}
    target = list(dict.fromkeys(list(picked) + [s for s in open_syms]))
    if set(target) == set(symbols):
        return stream, states, symbols
    log.info("ротация вселенной: %s → %s", ",".join(symbols), ",".join(target))
    new_states: dict[str, SymbolState] = {}
    for s in target:
        st = states.get(s)
        if st is None:
            st = SymbolState(s, trade_window_sec=cfg.trade_window_sec,
                             ob_levels=cfg.ob_levels)
            st.set_session_windows(session_windows)
            st._print_store = print_store
        new_states[s] = st
    stream.stop()
    new_stream = BybitMarketStream(target, new_states, category=cfg.bybit_category,
                                   testnet=cfg.bybit_testnet)
    new_stream.start()
    return new_stream, new_states, target


def _now_utc_day() -> float:
    now = time.time()
    return now - (now % 86400.0)


if __name__ == "__main__":
    run()
