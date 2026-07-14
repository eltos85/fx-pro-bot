"""scalp_bot main loop — orderflow-скальпер Bybit (детерминированный).

Каждые ``eval_interval_sec`` (default 1с):
1. Killswitch check (дневной/совокупный убыток).
2. Сопровождение открытых сделок (тайм-стоп / TP / SL).
3. Для каждого символа: snapshot микроструктуры из WS-кэша → стратегии
   (SweepReclaimDetector и др.) → если сигнал и прошли гейты (cooldown,
   лимит позиций, rate) → on_signal.
4. Heartbeat-лог раз в 60с.

Решения принимаются БЕЗ LLM. Запуск: ``python -m scalp_bot.app.main``.
PAPER по умолчанию (trading_enabled=false) — ордера только логируются.
"""
from __future__ import annotations

import logging
import signal
import time

from scalp_bot.analysis.regime import compute_regime_features, is_dead_market
from scalp_bot.analysis.signals import diagnose
from scalp_bot.analysis.strategies import build_strategies, resolve
from scalp_bot.config.settings import load_settings
from scalp_bot.data.aggregates import SymbolState
from scalp_bot.data.exec_stream import BybitExecStream
from scalp_bot.data.funding import FundingSchedule
from scalp_bot.data.htf import HtfTrend
from scalp_bot.data.levels import KeyLevels
from scalp_bot.data.market_stream import BybitMarketStream
from scalp_bot.data.momentum_universe import select_momentum_universe
from scalp_bot.data.universe import (apply_pins, filter_tickers,
                                     hourly_range_rvol, pad_universe,
                                     rank_rows)
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

    db = ScalpDB(cfg.data_dir)

    client = None
    if cfg.trading_enabled:
        if not cfg.bybit_api_key or not cfg.bybit_api_secret:
            log.error("trading_enabled=true, но нет SCALP_BYBIT_API_KEY/SECRET — выходим")
            return
        client = ScalpBybitClient(cfg.bybit_api_key, cfg.bybit_api_secret,
                                  demo=cfg.bybit_demo, category=cfg.bybit_category)
        log.info("Bybit REST: demo=%s category=%s", cfg.bybit_demo, cfg.bybit_category)
        # авто-селектор вселенной: бот сам выбирает монеты под стратегию
        if cfg.auto_universe_enabled:
            picked = _select_universe(client, cfg)
            if picked:
                symbols = picked
                log.info("авто-вселенная (метод=%s, топ-%d): %s",
                         cfg.universe_method, cfg.universe_top_n,
                         ",".join(symbols))
            else:
                log.warning("авто-вселенная пуста — fallback на SCALP_SYMBOLS=%s",
                            ",".join(symbols))
        if cfg.flatten_on_start:
            # закрыть позиции по выбранным символам И по символам открытых сделок
            flat_syms = set(symbols) | {tr.symbol for tr in db.open_trades()}
            _flatten_on_start(client, db, sorted(flat_syms))
        else:
            # v0.18.0: НЕ флэтим — даём открытым позициям дожить до TP/SL
            _adopt_on_start(client, db)

    # v0.18.20: вселенная канон-страты (мейджоры) ВСЕГДА в WS-подписках, но
    # торгуется ТОЛЬКО sweep_fade_canon (symbol_scope); остальные стратегии
    # canon-only символы не трогают (canon_only-гейт ниже) — их vol-вселенная
    # не изменена, A/B чистый.
    # v0.18.27: sweep_fade_run наследует канон-вход → её вселенная тоже
    # требует WS-подписок + key_levels-прогрева. Объединяем символы всех
    # «canon-like» стратегий (атрибут symbol_scope), а не хардкодим имя.
    canon_syms: list[str] = []
    if "sweep_fade_canon" in cfg.strategy_list:
        canon_syms += cfg.sweep_fade_canon_symbol_list
    if "sweep_fade_run" in cfg.strategy_list:
        canon_syms += cfg.sweep_fade_run_symbol_list
    canon_syms = list(dict.fromkeys(canon_syms))  # dedup, preserve order
    universe_syms = set(symbols)
    if canon_syms:
        symbols = list(dict.fromkeys(symbols + canon_syms))

    log.info("scalp_bot старт | mode=%s | symbols=%s | lot=$%.0f (min $%.0f) | "
             "kill day/total=$%.0f/$%.0f | strats=%s", mode, ",".join(symbols),
             cfg.position_usd, cfg.min_position_usd, cfg.max_daily_loss_usd,
             cfg.max_total_loss_usd, ",".join(cfg.strategy_list))

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
    # Применимость фильтров per-strategy (v0.18.1): MR-стратегии (sweep_fade,
    # density_bounce) — под HTF-направлением и ADX-режим-гейтом; momentum
    # (density_break) — НЕТ (направленный EMA режет контртренд-пробои, ADX-гейт
    # backwards для пробоя). Атрибут на классе стратегии (getattr default True).
    htf_strats = {s.name for s in strategies if getattr(s, "htf_filtered", True)}
    adx_strats = {s.name for s in strategies if getattr(s, "regime_gated", True)}
    # Асимметричный DMI long-gate (v0.18.4 sweep_fade → v0.18.18 density_break, C-08):
    # лонг разрешён только если DMI вверх; шорты свободно. MR-страты наследуют от
    # htf_filtered; momentum density_break включает явно (di_long_gated=True) — у
    # него СИММЕТРИЧНОГО EMA-фильтра НЕТ (htf_filtered=False), но контртренд-ЛОНГ-
    # пробои = bull traps (live long WR 5.9% / net −158, p<0.02).
    di_long_strats = {s.name for s in strategies
                      if getattr(s, "di_long_gated", getattr(s, "htf_filtered", True))}
    executor = Executor(db, cfg, client, notifier=notifier, strategies=strategies)

    # v0.18.20: ключевые уровни (PDH/PDL + дневные экстремумы) для канон-страты.
    # Инжектим в стратегию (ей нужен REST-клиент только опосредованно — через
    # этот кэш). Fail-closed: пока уровни не прогреты, канон-детектор не взводится.
    key_levels = None
    levels_strats = [s for s in strategies if hasattr(s, "key_levels")]
    last_levels = 0.0
    if levels_strats:
        # v0.18.27→v0.18.33: KeyLevels считает rolling-regime_ratio для
        # regime_features-телеметрии (страта sweep_fade_trend удалена).
        lookback = getattr(cfg, "regime_ratio_lookback_bars", 8)
        key_levels = KeyLevels(cfg.htf_interval, regime_lookback=int(lookback))
        for s in levels_strats:
            s.key_levels = key_levels
        if client is not None:
            try:
                key_levels.refresh(client, canon_syms)
                last_levels = time.time()
            except Exception:
                log.exception("key levels initial refresh failed")
    # canon-only символы: в WS-подписках ради канон-страты, но НЕ в авто-вселенной
    # остальных стратегий — те их пропускают (чистота A/B).
    canon_only = set(canon_syms) - universe_syms

    # HTF-bias: трендовый фильтр старшего ТФ (EMA200 1H). Первичный прогрев на
    # старте, далее refresh раз в htf_refresh_sec (метрика медленная).
    htf = HtfTrend(cfg.htf_ema_len, cfg.htf_interval, cfg.htf_adx_len)
    last_htf = 0.0
    if client is not None and cfg.require_htf_trend:
        try:
            htf.refresh(client, symbols)
            last_htf = time.time()
        except Exception:
            log.exception("htf initial refresh failed")

    # Funding-график по РЕАЛЬНОМУ интервалу символа (8/4/1ч), а не зашитые 8ч.
    # Интервал статичен per-instrument → refresh на старте и при ротации.
    funding = FundingSchedule()
    if client is not None:
        try:
            funding.refresh(client, symbols)
        except Exception:
            log.exception("funding initial refresh failed")

    # анти-даблклик после входа: ключ (strategy, symbol) — пер-стратегийный
    # (v0.18.21); раньше ключ был только symbol и глушил все страты разом.
    cooldown: dict[tuple[str, str], float] = {}
    last_heartbeat = 0.0
    last_universe = time.time()  # уже выбрали на старте — ждём refresh до ротации
    kill_notified = False
    funnel = _new_funnel()

    try:
        while not _shutdown:
            loop_start = time.monotonic()
            now = time.time()

            # 0a) часовая ротация вселенной (бот сам выбирает монеты)
            if (client is not None and cfg.auto_universe_enabled
                    and now - last_universe >= cfg.universe_refresh_sec):
                last_universe = now
                try:
                    prev_syms = set(symbols)
                    stream, states, symbols, picked = _rotate_universe(
                        client, cfg, db, stream, states, strategies, symbols,
                        notifier, extra_syms=canon_syms)
                    if picked:
                        universe_syms = set(picked)
                        canon_only = set(canon_syms) - universe_syms
                    funding.refresh(client, symbols)  # новые символы → их график
                    # v0.18.2: прогрев HTF новых символов СРАЗУ (до того как они
                    # смогут торговаться) — закрываем fail-open окно ≤htf_refresh_sec.
                    # Канон QuantConnect: warm up indicator перед торговлей нового
                    # символа динамической вселенной.
                    if cfg.require_htf_trend:
                        new_syms = [s for s in symbols if s not in prev_syms]
                        if new_syms:
                            htf.refresh(client, new_syms)
                except Exception:
                    log.exception("rotate_universe failed")

            # 0a2) HTF-bias refresh (EMA200 1H, метрика медленная — раз в ~5мин)
            if (client is not None and cfg.require_htf_trend
                    and now - last_htf >= cfg.htf_refresh_sec):
                last_htf = now
                try:
                    htf.refresh(client, symbols)
                except Exception:
                    log.exception("htf refresh failed")

            # 0a3) ключевые уровни канон-страты (PDH/PDL + дневные экстремумы;
            # 15m-клины, та же каденция, что HTF)
            if (client is not None and key_levels is not None
                    and now - last_levels >= cfg.htf_refresh_sec):
                last_levels = now
                try:
                    key_levels.refresh(client, canon_syms)
                except Exception:
                    log.exception("key levels refresh failed")

            # 0b) забрать исполнения из приватного WS → атрибуция к сделкам
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
                candidates = []
                for st in strategies:
                    # v0.18.20: пер-стратегийный скоуп символов. Канон-страта
                    # торгует ТОЛЬКО свой whitelist (symbol_scope); остальные
                    # НЕ трогают canon-only мейджоры (их вселенная не менялась
                    # — A/B чистый, поведение базовых страт не задето).
                    scope = getattr(st, "symbol_scope", None)
                    if scope is not None and sym not in scope:
                        continue
                    if scope is None and sym in canon_only:
                        continue
                    # v0.18.21: signal_cooldown ПЕР-СТРАТЕГИЙНЫЙ (запрос
                    # пользователя 2026-06-11). Раньше вход (или неналитая
                    # maker-вставка) ОДНОЙ страты глушил по символу ВСЕ на 60с
                    # — density_break/bounce теряли сигналы из-за чужого входа,
                    # стата страт перемешивалась. Анти-даблклик остаётся, но
                    # только для страты, которая сама только что входила.
                    if now - cooldown.get((st.name, sym), 0.0) < cfg.signal_cooldown_sec:
                        continue
                    try:
                        s = st.update(snap, now)
                    except Exception:
                        log.exception("strategy %s %s failed", st.name, sym)
                        continue
                    if st.armed(sym):
                        funnel["armed"] += 1
                    # v0.18.32: lifecycle-телеметрия треков density_bounce →
                    # БД (density_tracks). Стратегия эмитит в очередь, main
                    # loop дренирует и пишет. Не влияет на торговлю.
                    if (getattr(cfg, "density_track_log_enabled", True)
                            and hasattr(st, "drain_lifecycle")):
                        try:
                            for row in st.drain_lifecycle():
                                db.insert_density_track(row)
                        except Exception:
                            log.exception("density track lifecycle log failed")
                    if s is not None:
                        candidates.append(s)
                sig = resolve(candidates)
                if sig is None:
                    continue
                # снапшот BTC для regime-фичи btc_ret_bps (импульс мейджора);
                # для самого BTC — его же snap (ретёрн символа = импульс BTC)
                if sym == "BTCUSDT":
                    btc_snap = snap
                else:
                    _bst = states.get("BTCUSDT")
                    btc_snap = _bst.snapshot() if _bst is not None else None
                # Per-symbol LONG-блок (v0.18.17, C-07): на символах из no_long_list
                # запрещаем ЛОНГ ВСЕМ стратегиям (включая density_break, у которого
                # нет HTF/DMI-гейтов), шорты разрешены. Exposure-management по запросу
                # пользователя — ZEC-лонги тянут весь минус, шорты ок; согласуется с
                # research DMI long-gate. Reversible (env SCALP_NO_LONG_SYMBOLS),
                # пересмотр при n≥100 по символу (sample-size.mdc).
                if sig.side == "long" and sig.symbol in cfg.no_long_list:
                    play.info("🚫 [%s] long заблокирован (no_long_symbols) — "
                              "лонги по символу отключены, шорты разрешены",
                              sig.symbol)
                    _log_shadow(db, cfg, sig, "no_long_symbol", snap, htf,
                                key_levels, now, btc_snap)
                    continue
                # v0.18.2: fail-CLOSED для непрогретого символа. Канон QuantConnect:
                # «refuse to trade until indicator ready» — не фейдим символ, у
                # которого HTF-фильтр ещё ни разу не посчитан (свежая ротация). Без
                # этого fail-open пропускал контртренд-фейды в окно прогрева. Только
                # для MR (htf_strats); momentum density_break не зависит от HTF.
                if (cfg.require_htf_trend and sig.strategy in htf_strats
                        and not htf.has_data(sig.symbol)):
                    play.info("⏳ [%s] %s — HTF-фильтр не прогрет (свежий символ) — "
                              "фейд пропускаю (канон: не торговать до готовности "
                              "индикатора)", sig.symbol, sig.side)
                    _log_shadow(db, cfg, sig, "htf_warmup", snap, htf,
                                key_levels, now, btc_snap)
                    continue
                # HTF-bias: фейд только по старшему тренду (EMA200 15m). Контртренд
                # (ловля ножа) пропускаем. ТОЛЬКО для MR-стратегий (sig.strategy in
                # htf_strats) — momentum density_break торгует пробои в обе стороны.
                if (cfg.require_htf_trend and sig.strategy in htf_strats
                        and not htf.aligned(sig.symbol, sig.side, snap.last_price)):
                    d = htf.direction(sig.symbol, snap.last_price)
                    play.info("🧭 [%s] %s против старшего тренда (HTF=%s) — "
                              "пропускаю (фейдим только по тренду)", sig.symbol,
                              sig.side, d or "?")
                    _log_shadow(db, cfg, sig, "htf_align", snap, htf,
                                key_levels, now, btc_snap)
                    continue
                # DMI-гейт направления только для ЛОНГОВ (v0.18.4, асимметрия).
                # Диагноз: live sweep_fade-лонги 20% WR (контртренд в дип),
                # шорты 54% (EMA на шортах хорош). EMA200-кросс плохо ловит
                # направление на даунтрендовых альтах → пропускает контртренд-
                # лонги. Wilder DMI (+DI/−DI, 1978) быстрее ловит доминирующую
                # сторону. Лонг разрешён только если +DI>−DI вверх; шорты не трогаем.
                # MR-страты (sweep_fade/density_bounce) + momentum density_break
                # (v0.18.18, C-08: long-пробои против тренда = bull traps, WR 5.9%/
                # net −158, p<0.02; симметричный EMA-фильтр НЕ ставим — сохраняем
                # profitable контртренд-ШОРТЫ). A/B 3 окна (data/scalp_di_long_gate
                # .txt): лонги avgR −0.092/−0.100/−0.098 → +0.004/+0.023/−0.006.
                if (cfg.htf_di_long_gate and sig.strategy in di_long_strats
                        and sig.side == "long" and htf.di_blocks_long(sig.symbol)):
                    play.info("🧭 [%s] %s long но DMI вниз (−DI≥+DI) — пропускаю "
                              "(контртренд-лонг-пробой в дип = bull trap)",
                              sig.symbol, sig.strategy)
                    _log_shadow(db, cfg, sig, "dmi_long", snap, htf,
                                key_levels, now, btc_snap)
                    continue
                # ADX режим-гейт (v0.17.0): EMA дала направление, но если тренд
                # СЛИШКОМ сильный (ADX≥adx_max, «трендовый день») — фейд запрещён
                # ВНЕ зависимости от направления. Канон MR: «never fade a one-
                # timeframe trending market» (Connors/Raschke; Dalton). Additive
                # поверх EMA. Fail-open: нет ADX → не блокируем. ТОЛЬКО для MR
                # (sig.strategy in adx_strats): для momentum density_break гейт
                # backwards — пробой ХОЧЕТ сильного тренда (v0.18.1). Порог ≥30
                # (v0.18.9, под SL ×2.0): корзина 25–30 фейдится в плюс OOS, режем
                # только strong ≥30 (Connors/Raschke). Валидирован на sweep_fade.
                if (cfg.htf_adx_gate and sig.strategy in adx_strats
                        and htf.is_strong_trend(sig.symbol, cfg.htf_adx_max)):
                    play.info("🚂 [%s] %s — сильный тренд (ADX=%.0f≥%.0f, трендовый "
                              "день) — не фейдю (канон: не фейдить one-TF тренд)",
                              sig.symbol, sig.side,
                              htf.trend_strength(sig.symbol) or 0.0, cfg.htf_adx_max)
                    _log_shadow(db, cfg, sig, "adx_strong", snap, htf,
                                key_levels, now, btc_snap)
                    continue
                # Гейт «мёртвого рынка» (v0.18.34, 2026-07-10, одобрено
                # пользователем): fade-профит = амплитуда отскока, в тихом
                # рынке без топлива (NATR<0.5 И liq=0 И rv_burst<1.1) отскоку
                # некуда идти. ТОЛЬКО sweep_fade-семейство (data-driven scope:
                # threshold-sweep n=86 07-03..10, cut WR 16% −$208 vs keep 38%
                # +$16, p=0.049; гипотеза префиксирована BUILDLOG 07-07; см.
                # docstring is_dead_market). density-страты не тронуты (нет
                # данных). Fail-open при None-фичах. Откат:
                # SCALP_DEAD_MARKET_GATE_ENABLED=false (без деплоя).
                if (cfg.dead_market_gate_enabled
                        and sig.strategy.startswith("sweep_fade")):
                    try:
                        _feats = compute_regime_features(
                            snap, htf, key_levels, now, btc_snap=btc_snap)
                    except Exception:
                        log.exception("dead-market feats %s failed", sig.symbol)
                        _feats = None
                    if is_dead_market(_feats,
                                      natr_max=cfg.dead_market_natr_max_pct,
                                      rv_max=cfg.dead_market_rv_max):
                        play.info("💀 [%s] %s — мёртвый рынок (NATR %.2f%%<%.2f, "
                                  "liq=0, rv %.2f<%.2f): отскоку нет топлива — "
                                  "фейд пропускаю", sig.symbol, sig.side,
                                  _feats.get("htf_natr_pct") or 0.0,
                                  cfg.dead_market_natr_max_pct,
                                  _feats.get("rv_burst") or 0.0,
                                  cfg.dead_market_rv_max)
                        _log_shadow(db, cfg, sig, "dead_market", snap, htf,
                                    key_levels, now, btc_snap, feats=_feats)
                        continue
                # SL-cooldown: не перефейдиваем провалившийся уровень сразу.
                # Повторный вход той же стороной сразу после SL — в среднем
                # убыточен (backtest 15д; live-кейс XLMUSDT #816 SL→#817 SL за
                # 3мин). Противоположную сторону не трогаем (реальный разворот
                # ловим). Окно пер-стратегийное (v0.18.14): sweep_fade=60м
                # (канон MR + sweep n=829), прочие — базовые 300с.
                # v0.18.21: окно И сам факт SL — пер-стратегийные (запрос
                # пользователя 2026-06-11): SL фейда ничего не говорит о
                # пробое — раньше чужой стоп глушил страту на её же окно
                # (sweep_fade — 60 мин), и density_break/bounce теряли сигналы.
                cd_sec = cfg.sl_cooldown_for(sig.strategy)
                if cd_sec > 0:
                    last_sl = db.last_sl_close_ts(sig.symbol, sig.side,
                                                  strategy=sig.strategy)
                    if last_sl is not None and now - last_sl < cd_sec:
                        play.info("🧊 [%s] %s — недавний SL %.0fс назад (<%.0fс "
                                  "cooldown), не перефейдиваю уровень сразу",
                                  sig.symbol, sig.side, now - last_sl, cd_sec)
                        _log_shadow(db, cfg, sig, "sl_cooldown", snap, htf,
                                    key_levels, now, btc_snap)
                        continue
                # funding-окно (per-symbol по реальному интервалу): не открываемся
                # перед списанием — funding кратно превышает R на волатильных альтах.
                if funding.blocked(sig.symbol, now, cfg.avoid_funding_window_sec):
                    play.info("💸 [%s] funding через %.0fс (интервал %dм) — "
                              "пропускаю вход", sig.symbol,
                              funding.sec_to_next(sig.symbol, now),
                              funding.interval(sig.symbol))
                    _log_shadow(db, cfg, sig, "funding_window", snap, htf,
                                key_levels, now, btc_snap)
                    continue
                funnel["fired"] += 1
                gate = killswitch.can_open(db, cfg, now)
                if not gate.allowed:
                    log.info("gate block: %s", gate.reason)
                    break
                # regime-фичи на момент входа (meta-labeling, Lopez de Prado
                # AFML Ch3) — ТОЛЬКО логирование, на торговлю не влияет. Любая
                # ошибка вычисления → regime=None (анализ просто пропустит).
                try:
                    sig.regime = compute_regime_features(snap, htf, key_levels,
                                                         now, btc_snap=btc_snap)
                except Exception:
                    log.exception("regime features %s failed", sym)
                    sig.regime = None
                if executor.on_signal(sig) is not None:
                    cooldown[(sig.strategy, sym)] = now
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


# Аудит v0.9.0: liq/funding убраны из воронки — больше не факторы входа.
def _log_shadow(db, cfg, sig, blocked_by: str, snap, htf, key_levels,
                now: float, btc_snap=None, feats=None) -> None:
    """Shadow-лог сигнала, отвергнутого режим-гейтом (v0.18.31).

    Пишет regime-фичи + причину блокировки + уровни несостоявшейся сделки в
    shadow_signals. Лечит range restriction в оценке гейтов: без этого в
    regime_features видны только ПРОШЕДШИЕ гейты сигналы, и невозможно
    измерить «а что было бы без гейта» (спасает гейт от лузов или режет
    профит). ТОЛЬКО телеметрия — любая ошибка глушится, торговый поток не
    рвётся (no-data-fitting.mdc)."""
    if not getattr(cfg, "shadow_log_enabled", True):
        return
    if feats is None:  # dead_market-гейт передаёт уже посчитанные (не дублируем)
        try:
            feats = compute_regime_features(snap, htf, key_levels, now,
                                            btc_snap=btc_snap)
        except Exception:
            log.exception("shadow regime features %s failed", sig.symbol)
            feats = None
    try:
        db.insert_shadow(symbol=sig.symbol, side=sig.side,
                         strategy=sig.strategy, blocked_by=blocked_by,
                         features=feats, ts=now, entry_ref=sig.entry_ref,
                         sl_level=sig.sl_level, tp_level=sig.tp_level,
                         score=sig.score)
    except Exception:
        log.exception("shadow log %s failed", sig.symbol)


_FUNNEL_RULES = ("sweep", "div", "reclaim", "momentum", "ob")


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


def _adopt_on_start(client, db) -> None:
    """Старт БЕЗ флэта (v0.18.0): открытые позиции НЕ закрываем — биржевые SL/TP
    (вешаются на позицию в place_entry) защищают их, manage() со следующего цикла
    читает их из БД и продолжает сопровождать (flow_exit/time-stop/bracket), даёт
    дойти до TP/SL. Кейс #926: рестарт-флэт срезал прибыльный шорт и записал
    pnl=0 — теперь позиция доживает сама.

    Для open-сделок БЕЗ живой позиции:
    • резящий НЕзаполненный maker-вход (статус New/PartiallyFilled) — снимаем
      ТОЧЕЧНО по сохранённому link (cancel_all не зовём — аккаунт может быть
      общим) и помечаем entry_timeout;
    • реально закрылось пока бот лежал — реконсил реальным closed_pnl (tp/sl по
      знаку), а не restart_flat=0."""
    now = time.time()
    for tr in db.open_trades():
        try:
            pos = client.get_position(tr.symbol)
        except Exception:
            log.exception("adopt: get_position %s failed", tr.symbol)
            continue
        if pos and pos.size > 0:
            log.info("adopt: #%d %s %s size=%.6f — продолжаю сопровождать "
                     "(биржевые SL/TP активны)", tr.id, tr.symbol, tr.side, pos.size)
            continue
        status = None
        if tr.entry_order_id:
            try:
                status = client.order_status(tr.symbol, tr.entry_order_id)
            except Exception:
                log.exception("adopt: order_status %s failed", tr.symbol)
        if status in ("New", "PartiallyFilled", "Untriggered"):
            if tr.entry_order_id:
                client.cancel_order(tr.symbol, tr.entry_order_id)
            db.mark_closed(tr.id, exit_price=tr.entry, pnl_usd=0.0, fees_usd=0.0,
                           close_reason="entry_timeout", ts_close=now)
            log.info("adopt: #%d %s резящий вход снят при рестарте", tr.id, tr.symbol)
            continue
        pnl = None
        try:
            pnl = client.closed_pnl(tr.symbol, qty=tr.qty,
                                    since_ms=int(tr.ts_open * 1000))
        except Exception:
            log.exception("adopt: closed_pnl %s failed", tr.symbol)
        if pnl is None:
            reason = "restart_flat"
        else:
            reason = "tp_hit" if pnl >= 0 else "sl_hit"
        db.mark_closed(tr.id, exit_price=tr.entry, pnl_usd=pnl or 0.0,
                       fees_usd=0.0, close_reason=reason, ts_close=now)
        log.info("adopt: #%d %s закрылось пока лежали → %s pnl=%.4f", tr.id,
                 tr.symbol, reason, pnl or 0.0)


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


def _fresh_rvol(client, cfg, rows: list[dict]) -> dict[str, float]:
    """Свежий RVOL по амплитуде для прошедших 24h-фильтр символов (v0.14.0).
    Тянем 5м-свечи (~сутки) и считаем rolling-1ч amplitude / медиану часовых.
    fail-open: символ без klines не попадает в словарь → в гейте/ранге он на
    нейтральном fallback (24h range / keep)."""
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
    - "momentum" — ТОП по 24h росту/падению + порог оборота (как в ролике
      SerCrypto, data/momentum_universe.py). Без анти-памп кэпа.
    - "rvol" (default) — 24h hard-фильтр (ликвидность/спред/анти-памп) → свежий
      intraday RVOL-гейт+ранжирование (что «в игре сейчас», v0.14.0) → floor
      «минимум N монет» (P-4, v0.18.19) → пины. См. data/universe.py."""
    tickers = client.get_tickers()
    # Отсев stock-перпов (перпы на акции/ETF): на demo требуют Trading Terms
    # (ErrCode 110126), который нельзя принять через API, плюс торгуются по
    # сессиям реальных бирж, а не 24/7 крипто-флоу. fail-open: пустое множество
    # при ошибке API → не блокируем вселенную.
    stock = client.stock_type_symbols()
    if stock:
        before = len(tickers)
        tickers = [t for t in tickers if (t.get("symbol") or "") not in stock]
        if before != len(tickers):
            log.info("отсев stock-перпов из вселенной: %d → %d тикеров",
                     before, len(tickers))
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
            # гейт по свежести: затихшие в последний час режем (RVOL < порога).
            # fail-open: символ без klines (нет в rvol) — keep (REST-хиккап не пустошит).
            kept = [m for m in rows
                    if rvol.get(m["symbol"], cfg.universe_min_rvol) >= cfg.universe_min_rvol]
            rows = kept or rows  # если гейт всё срезал — не оставлять пусто
        ranked = rank_rows(rows, top_n=cfg.universe_top_n, vol_metric=rvol)
    if cfg.universe_min_symbols > 0 and len(ranked) < cfg.universe_min_symbols:
        # P-4 (audit A-4): вселенная выродилась (range/RVOL-гейты на остывшем
        # рынке) — добор из liquidity-pool. v0.18.29 (запрос пользователя
        # 2026-06-28): pool ослабляет ТОЛЬКО RVOL-свежесть, range-floor НЕ
        # трогаем (min_range_pct = canon floor) — иначе добор тащил майоры
        # BTC/ETH/SOL (range 2-5%), для которых base sweep_fade непригоден
        # (fee-guard режет сигналы). Лучше вселенная < floor (или пустая),
        # чем торговля непригодными монетами. Стражи ликвидности/анти-памп
        # (turnover, spread cap, range-cap) — остаются.
        pool = filter_tickers(
            tickers,
            min_turnover=cfg.universe_min_turnover_usd,
            min_range_pct=cfg.universe_min_range_pct,
            max_range_pct=cfg.universe_max_range_pct,
            max_spread_bps=cfg.universe_max_spread_bps)
        padded = pad_universe(ranked, pool, cfg.universe_min_symbols)
        if len(padded) > len(ranked):
            log.info("вселенная ниже floor (%d < %d) — добор по range24h из "
                     "liquidity-pool: +%s", len(ranked),
                     cfg.universe_min_symbols,
                     ",".join(padded[len(ranked):]))
        ranked = padded
    return apply_pins(ranked, cfg.universe_pin_list, cfg.universe_top_n)


def _rotate_universe(client, cfg, db, stream, states, strategies, symbols,
                     notifier, extra_syms: list[str] | None = None):
    """Часовой пересмотр вселенной. Возвращает (stream, states, symbols, picked)
    — picked = свежая авто-вселенная (None при сбое; нужна вызывающему для
    учёта canon-only символов).

    ``extra_syms`` (v0.18.20) — символы канон-страты: всегда остаются в
    WS-подписках независимо от авто-фильтра (торгуются только своей стратой).

    Безопасно для открытых: символ с открытой позицией НЕ выкидываем, пока она
    не закроется (даже если выпал из топа). Существующие SymbolState
    переиспользуем (CVD/агрегаты переживают рестарт WS — теряется лишь ~1с
    реконнекта, не всё окно). Стратегии не пересоздаём — лениво добавляем новые
    символы (ensure_symbols), чтобы executor продолжал ссылаться на те же
    объекты для дискреционного выхода."""
    picked = _select_universe(client, cfg)
    if not picked:
        log.warning("ротация: авто-вселенная пуста — оставляю текущие символы")
        return stream, states, symbols, None
    open_syms = {tr.symbol for tr in db.open_trades()}
    # топ-N плюс канон-символы плюс символы с открытыми позициями
    target = list(dict.fromkeys(
        list(picked) + list(extra_syms or []) + [s for s in open_syms]))
    if set(target) == set(symbols):
        return stream, states, symbols, picked
    log.info("ротация вселенной: %s → %s", ",".join(symbols), ",".join(target))
    new_states = {
        s: states.get(s) or SymbolState(
            s, cvd_window_sec=cfg.cvd_window_sec,
            liq_window_sec=cfg.liq_window_sec, ob_levels=cfg.ob_levels)
        for s in target
    }
    stream.stop()
    new_stream = BybitMarketStream(target, new_states, category=cfg.bybit_category,
                                   testnet=cfg.bybit_testnet)
    new_stream.start()
    for st in strategies:
        st.ensure_symbols(target)
    # TG-уведомление о ротации убрано (спам — с RVOL-отбором состав меняется
    # часто). Смена состава видна в логах (log.info «ротация вселенной» выше).
    return new_stream, new_states, target, picked


def in_active_session(now: float, cfg) -> bool:
    """Текущий UTC-час входит в активные торговые часы (cfg.active_hours)."""
    hour = int((now % 86400.0) // 3600.0)
    return hour in cfg.active_hours


def now_utc_day() -> float:
    now = time.time()
    return now - (now % 86400.0)


if __name__ == "__main__":
    run()
