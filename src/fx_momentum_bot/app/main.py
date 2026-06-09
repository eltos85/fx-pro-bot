from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass

import yfinance as yf

from fx_momentum_bot.config.settings import MomentumBotSettings
from fx_momentum_bot.state.store import MomentumStore
from fx_momentum_bot.strategy.momentum import MomentumSignal, build_signal
from fx_momentum_bot.strategy.volume_profile import (
    VolumeProfileSignal,
    build_signal as build_vp_signal,
)
from fx_pro_bot.trading.auth import TokenData
from fx_pro_bot.trading.client import CTraderClient
from fx_pro_bot.trading.executor import TradeExecutor
from fx_pro_bot.trading.symbols import SymbolCache, lots_to_volume
from shared_oauth.token_client import (
    ServiceConfig,
    TokenServiceRejected,
    TokenServiceUnavailable,
    fetch_token,
    push_token,
)

log = logging.getLogger("fx_momentum_bot")

_shutdown = False


@dataclass(slots=True)
class ManagedPosition:
    position_id: int
    symbol: str
    side: str  # "long" | "short"
    volume: int
    entry_price: float
    stop_loss: float | None
    digits: int


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Received signal %d, shutting down", signum)


def _fetch_candles(symbol: str, interval: str, period: str, retries: int = 3):
    # yfinance периодически отдаёт пустой результат / "possibly delisted"
    # по ОДНОМУ тикеру при транзиентном сбое Yahoo (остальные в том же
    # цикле качаются нормально). Лёгкий retry с backoff сглаживает это,
    # чтобы цикл не пропускал валидный сигнал из-за разовой флакоты.
    data = None
    for attempt in range(retries):
        try:
            data = yf.download(
                tickers=symbol,
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
                threads=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("yfinance download %s failed (attempt %d/%d): %s",
                        symbol, attempt + 1, retries, exc)
            data = None
        if data is not None and not data.empty:
            break
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    if data is None or data.empty:
        return data
    # yfinance can return multi-index columns for single ticker.
    if hasattr(data.columns, "nlevels") and data.columns.nlevels > 1:
        data.columns = data.columns.get_level_values(0)
    return data


def _build_executor(settings: MomentumBotSettings) -> TradeExecutor | None:
    if not settings.trading_enabled:
        return None
    if (
        not settings.ctrader_client_id
        or not settings.ctrader_client_secret
        or not settings.ctrader_account_id
    ):
        log.warning("Trading enabled but MOMENTUM_BOT_CTRADER_* credentials are incomplete")
        return None

    if not settings.token_service_url or not settings.token_service_secret:
        if settings.require_token_service:
            raise RuntimeError(
                "Token service is required. Set MOMENTUM_BOT_TOKEN_SERVICE_URL and "
                "MOMENTUM_BOT_TOKEN_SERVICE_SECRET."
            )
        log.warning("Token service not configured; momentum bot trading disabled")
        return None

    service_cfg = ServiceConfig(
        url=settings.token_service_url.rstrip("/"),
        secret=settings.token_service_secret,
        client_label=settings.token_service_label,
    )
    try:
        service_token = fetch_token(service_cfg)
    except (TokenServiceRejected, TokenServiceUnavailable) as exc:
        if settings.require_token_service:
            raise RuntimeError(f"Failed to fetch token from token-service: {exc}") from exc
        log.warning("Token service unavailable, trading disabled: %s", exc)
        return None

    token = TokenData(
        access_token=service_token.access_token,
        refresh_token=service_token.refresh_token,
        expires_at=service_token.expires_at,
        token_type=service_token.token_type,
    )

    def _on_token_refreshed(new_access: str, new_refresh: str, expires_at: float) -> None:
        try:
            push_token(service_cfg, new_access, new_refresh, expires_at)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to push refreshed token to token-service: %s", exc)

    client = CTraderClient(
        client_id=settings.ctrader_client_id,
        client_secret=settings.ctrader_client_secret,
        access_token=token.access_token,
        account_id=settings.ctrader_account_id,
        host_type=settings.ctrader_host_type,
        refresh_token=token.refresh_token,
        expires_at=token.expires_at,
        on_token_refreshed=_on_token_refreshed,
    )
    client.start(timeout=30)
    symbol_cache = SymbolCache()
    executor = TradeExecutor(client, symbol_cache, lot_size=settings.lot_size)
    loaded = executor.load_symbols()
    log.info("cTrader symbols loaded: %d", loaded)
    return executor


def _count_open_positions_for_symbols(
    executor: TradeExecutor, symbols: tuple[str, ...], *, label: str
) -> int:
    try:
        open_positions = executor.get_open_positions()
    except Exception:
        return 0

    symbol_ids = set()
    for yf_symbol in symbols:
        info = executor.symbols.resolve_yfinance(yf_symbol)
        if info is not None:
            symbol_ids.add(info.symbol_id)

    count = 0
    for pos in open_positions:
        # Изоляция по label: считаем ТОЛЬКО свои позиции, не чужих ботов
        # на общем счёте (напр. XAUUSD у fx_ai_trader label="ai-fx-trader").
        if getattr(pos, "label", "") != label:
            continue
        trade_data = getattr(pos, "tradeData", None)
        sid = getattr(trade_data, "symbolId", None) if trade_data else None
        if sid in symbol_ids:
            count += 1
    return count


def _r_multiple(side: str, *, entry_price: float, current_price: float, risk_price: float) -> float:
    if risk_price <= 0:
        return 0.0
    if side == "long":
        return (current_price - entry_price) / risk_price
    return (entry_price - current_price) / risk_price


def _calc_partial_close_volume(
    *,
    current_volume: int,
    fraction: float,
    step_volume: int,
    min_volume: int,
) -> int:
    if current_volume <= 0 or fraction <= 0:
        return 0
    step = max(1, step_volume)
    # Оставляем хотя бы min_volume, иначе частичное закрытие превращается в полный выход.
    max_close = current_volume - max(min_volume, step)
    if max_close < min_volume:
        return 0
    requested = int(round(current_volume * fraction))
    close_volume = (requested // step) * step
    if close_volume <= 0:
        return 0
    close_volume = min(close_volume, max_close)
    close_volume = (close_volume // step) * step
    if close_volume < min_volume:
        return 0
    return close_volume


def _optional_float(obj: object, field: str) -> float | None:
    try:
        if hasattr(obj, "HasField") and obj.HasField(field):
            raw = getattr(obj, field)
            return float(raw) if raw else None
    except Exception:
        pass
    raw = getattr(obj, field, 0)
    return float(raw) if raw else None


def _collect_managed_positions(
    executor: TradeExecutor, symbols: tuple[str, ...], *, label: str
) -> dict[str, list[ManagedPosition]]:
    sid_to_symbol: dict[int, str] = {}
    symbol_meta: dict[str, tuple[int, int]] = {}  # symbol -> (digits, symbol_id)
    for symbol in symbols:
        info = executor.symbols.resolve_yfinance(symbol)
        if info is None:
            continue
        sid_to_symbol[info.symbol_id] = symbol
        symbol_meta[symbol] = (info.digits, info.symbol_id)

    grouped: dict[str, list[ManagedPosition]] = {s: [] for s in symbols}
    for pos in executor.get_open_positions():
        # Изоляция по label: управляем ТОЛЬКО своими позициями (BE/трейлинг/
        # partial), не трогаем чужих ботов на общем счёте (fx_ai_trader и т.п.).
        if getattr(pos, "label", "") != label:
            continue
        td = getattr(pos, "tradeData", None)
        if td is None:
            continue
        sid = getattr(td, "symbolId", None)
        if sid is None:
            continue
        symbol = sid_to_symbol.get(int(sid))
        if symbol is None:
            continue
        side_val = int(getattr(td, "tradeSide", 0) or 0)
        side = "long" if side_val == 1 else "short"
        volume = int(getattr(td, "volume", 0) or 0)
        if volume <= 0:
            continue
        entry_price = float(getattr(pos, "price", 0) or 0)
        if entry_price <= 0:
            continue
        stop_loss = _optional_float(pos, "stopLoss")
        digits = symbol_meta.get(symbol, (5, 0))[0]
        grouped[symbol].append(
            ManagedPosition(
                position_id=int(getattr(pos, "positionId", 0) or 0),
                symbol=symbol,
                side=side,
                volume=volume,
                entry_price=entry_price,
                stop_loss=stop_loss,
                digits=digits,
            )
        )
    return grouped


def _manage_positions(
    *,
    executor: TradeExecutor,
    store: MomentumStore,
    settings: MomentumBotSettings,
    signal_by_symbol: dict[str, MomentumSignal | VolumeProfileSignal],
    positions_by_symbol: dict[str, list[ManagedPosition]],
) -> None:
    # Trader-backed policy:
    # - Van Tharp: transfer risk to break-even at +1R.
    # - Linda Raschke discretionary pattern: partial take + runner.
    # - Turtle/LeBeau ATR-family trailing to hold trend legs.
    active_ids: set[int] = set()
    for symbol, positions in positions_by_symbol.items():
        signal_data = signal_by_symbol.get(symbol)
        if signal_data is None:
            continue
        current_price = signal_data.last_close
        atr = signal_data.atr
        info = executor.symbols.resolve_yfinance(symbol)
        if info is None:
            continue

        for pos in positions:
            if pos.position_id <= 0:
                continue
            active_ids.add(pos.position_id)
            state = store.get_position_state(pos.position_id)
            risk_price = 0.0
            if state and float(state.get("risk_price", 0) or 0) > 0:
                risk_price = float(state["risk_price"])
            elif pos.stop_loss is not None and pos.stop_loss > 0:
                risk_price = abs(pos.entry_price - pos.stop_loss)
            else:
                risk_price = max(atr * settings.atr_stop_mult, 0.0)
            if risk_price <= 0:
                continue

            if state is None:
                store.upsert_position_state(
                    broker_position_id=pos.position_id,
                    symbol=symbol,
                    entry_price=pos.entry_price,
                    initial_volume=pos.volume,
                    risk_price=risk_price,
                )
                state = store.get_position_state(pos.position_id)
            r_now = _r_multiple(
                pos.side,
                entry_price=pos.entry_price,
                current_price=current_price,
                risk_price=risk_price,
            )
            break_even_done = bool((state or {}).get("break_even_done", 0))
            partial_done = bool((state or {}).get("partial_done", 0))

            if not break_even_done and r_now >= settings.break_even_r:
                be_sl = round(pos.entry_price, pos.digits)
                ok = executor.amend_sl_tp(
                    pos.position_id,
                    sl_price=be_sl,
                    tp_price=None,
                    yf_symbol=symbol,
                    current_price=current_price,
                )
                if ok:
                    store.set_break_even_done(pos.position_id)
                    pos.stop_loss = be_sl
                    log.info(
                        "MANAGE %s #%d: BE set @ %.5f (R=%.2f)",
                        symbol,
                        pos.position_id,
                        be_sl,
                        r_now,
                    )

            if not partial_done and r_now >= settings.partial_take_r:
                close_volume = _calc_partial_close_volume(
                    current_volume=pos.volume,
                    fraction=settings.partial_take_fraction,
                    step_volume=info.step_volume,
                    min_volume=info.min_volume,
                )
                if close_volume > 0:
                    close_res = executor.close_position(pos.position_id, close_volume)
                    if close_res.success:
                        store.set_partial_done(pos.position_id)
                        pos.volume = max(0, pos.volume - close_volume)
                        log.info(
                            "MANAGE %s #%d: partial close %d/%d @ R=%.2f",
                            symbol,
                            pos.position_id,
                            close_volume,
                            close_volume + pos.volume,
                            r_now,
                        )

            if r_now >= settings.trailing_activate_r and atr > 0:
                if pos.side == "long":
                    candidate_sl = current_price - settings.trailing_atr_mult * atr
                    if pos.stop_loss is not None:
                        candidate_sl = max(candidate_sl, pos.stop_loss)
                    candidate_sl = max(candidate_sl, pos.entry_price)
                else:
                    candidate_sl = current_price + settings.trailing_atr_mult * atr
                    if pos.stop_loss is not None:
                        candidate_sl = min(candidate_sl, pos.stop_loss)
                    candidate_sl = min(candidate_sl, pos.entry_price)
                candidate_sl = round(candidate_sl, pos.digits)
                prev_sl = round(pos.stop_loss, pos.digits) if pos.stop_loss is not None else None
                if prev_sl is not None and candidate_sl == prev_sl:
                    continue
                trail_ok = executor.amend_sl_tp(
                    pos.position_id,
                    sl_price=candidate_sl,
                    tp_price=None,
                    yf_symbol=symbol,
                    current_price=current_price,
                )
                if trail_ok:
                    pos.stop_loss = candidate_sl
                    log.info(
                        "MANAGE %s #%d: trail SL -> %.5f (R=%.2f, ATR=%.6f)",
                        symbol,
                        pos.position_id,
                        candidate_sl,
                        r_now,
                        atr,
                    )

    cleaned = store.cleanup_position_state(active_ids)
    if cleaned > 0:
        log.info("Momentum state cleanup: removed %d stale positions", cleaned)


def _open_vp_position(
    *,
    executor: TradeExecutor,
    store: MomentumStore,
    settings: MomentumBotSettings,
    symbol: str,
    sig: VolumeProfileSignal,
) -> tuple[bool, str]:
    """Открыть VP-позицию с явными SL/TP (дистанции из цен сетапа)."""
    sl_distance = abs(sig.entry - sig.sl_price)
    tp_distance = abs(sig.tp_price - sig.entry)
    if sl_distance <= 0 or tp_distance <= 0:
        return False, "vp_bad_sl_tp"
    result = executor.open_position(
        yf_symbol=symbol,
        direction=sig.direction,
        sl_distance=sl_distance,
        tp_distance=tp_distance,
        lot_size=settings.vp_lot_size,
        comment=settings.vp_order_label,
        entry_price_hint=sig.entry,
        label=settings.position_label,
    )
    if not result.success:
        return False, f"vp_open:{result.error}"
    if result.broker_position_id > 0 and sl_distance > 0:
        store.upsert_position_state(
            broker_position_id=result.broker_position_id,
            symbol=symbol,
            entry_price=result.fill_price if result.fill_price > 0 else sig.entry,
            initial_volume=(
                result.volume if result.volume > 0 else lots_to_volume(settings.vp_lot_size)
            ),
            risk_price=sl_distance,
        )
    log.info(
        "VP OPEN %s %s %s lot=%.2f entry=%.5f sl=%.5f tp=%.5f | %s",
        symbol,
        sig.setup,
        sig.direction,
        settings.vp_lot_size,
        sig.entry,
        sig.sl_price,
        sig.tp_price,
        sig.reason,
    )
    return True, "vp_open:ok"


def _process_vp_symbol(
    *,
    symbol: str,
    sig: VolumeProfileSignal,
    executor: TradeExecutor | None,
    store: MomentumStore,
    settings: MomentumBotSettings,
    positions_by_symbol: dict[str, list[ManagedPosition]],
    open_count: int,
) -> bool:
    """Гейтинг + открытие VP-сделки. Возврат True если позиция открыта."""
    executed = False
    note = "no_setup"
    already_open = len(positions_by_symbol.get(symbol, [])) > 0
    day_count = store.count_executed_today(symbol, sig.direction) if sig.direction in {"long", "short"} else 0

    can_open = (
        sig.direction in {"long", "short"}
        and sig.setup != "none"
        and executor is not None
        and not already_open
        and open_count < settings.max_open_positions
        and day_count < settings.vp_max_trades_per_dir_per_day
    )
    if can_open and executor is not None:
        executed, note = _open_vp_position(
            executor=executor, store=store, settings=settings, symbol=symbol, sig=sig
        )
    elif sig.direction in {"long", "short"} and sig.setup != "none":
        # сетап есть, но не открываем — зафиксируем причину
        if already_open:
            note = "vp_skip:position_open"
        elif day_count >= settings.vp_max_trades_per_dir_per_day:
            note = "vp_skip:daily_limit"
        elif open_count >= settings.max_open_positions:
            note = "vp_skip:max_positions"
        elif executor is None:
            note = "paper_mode"

    store.add_decision(
        symbol=symbol,
        direction=sig.direction,
        momentum_value=0.0,
        atr=sig.atr,
        close_price=sig.last_close,
        executed=executed,
        note=note,
    )
    log.info(
        "%s VP setup=%s dir=%s close=%.5f atr=%.6f executed=%s note=%s",
        symbol, sig.setup, sig.direction, sig.last_close, sig.atr, executed, note,
    )
    return executed


def run() -> None:
    settings = MomentumBotSettings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    store = MomentumStore(settings.db_path)
    executor = _build_executor(settings)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    vp_symbols = set(settings.vp_symbols)
    log.info(
        "Momentum bot started | mode=%s | momentum=%s | vp=%s | interval=%s/%s | db=%s",
        "LIVE" if (settings.trading_enabled and executor is not None) else "PAPER",
        ",".join(s for s in settings.symbols if s not in vp_symbols) or "-",
        ",".join(settings.vp_symbols) or "-",
        settings.yfinance_interval,
        settings.yfinance_period,
        settings.db_path,
    )

    while not _shutdown:
        try:
            signal_by_symbol: dict[str, MomentumSignal | VolumeProfileSignal] = {}
            positions_by_symbol: dict[str, list[ManagedPosition]] = {}
            if executor is not None and settings.position_management_enabled:
                positions_by_symbol = _collect_managed_positions(
                    executor, settings.all_symbols, label=settings.position_label
                )
            for symbol in settings.all_symbols:
                is_vp = symbol in vp_symbols
                if is_vp:
                    candles = _fetch_candles(
                        symbol, settings.vp_yfinance_interval, settings.vp_yfinance_period
                    )
                    signal_data: MomentumSignal | VolumeProfileSignal | None = build_vp_signal(
                        candles,
                        tz=settings.vp_session_tz,
                        session_start=settings.vp_session_start,
                        session_end=settings.vp_session_end,
                        value_area_pct=settings.vp_value_area_pct,
                        num_bins=settings.vp_num_bins,
                        atr_period=settings.vp_atr_period,
                        min_rr=settings.vp_min_rr,
                        breach_lookback=settings.vp_breach_lookback,
                        consolidation_bars=settings.vp_consolidation_bars,
                    )
                else:
                    candles = _fetch_candles(
                        symbol, settings.yfinance_interval, settings.yfinance_period
                    )
                    signal_data = build_signal(
                        candles,
                        lookback_bars=settings.momentum_lookback_bars,
                        atr_period=settings.atr_period,
                        threshold=settings.signal_threshold,
                    )
                if signal_data is None:
                    store.add_decision(
                        symbol=symbol,
                        direction="flat",
                        momentum_value=0.0,
                        atr=0.0,
                        close_price=0.0,
                        executed=False,
                        note="not_enough_data",
                    )
                    continue
                signal_by_symbol[symbol] = signal_data

            if executor is not None and settings.position_management_enabled:
                _manage_positions(
                    executor=executor,
                    store=store,
                    settings=settings,
                    signal_by_symbol=signal_by_symbol,
                    positions_by_symbol=positions_by_symbol,
                )
            if executor is not None:
                open_count = _count_open_positions_for_symbols(
                    executor, settings.all_symbols, label=settings.position_label
                )
            else:
                open_count = 0

            for symbol in settings.all_symbols:
                signal_data = signal_by_symbol.get(symbol)
                if signal_data is None:
                    continue

                if symbol in vp_symbols and isinstance(signal_data, VolumeProfileSignal):
                    opened = _process_vp_symbol(
                        symbol=symbol,
                        sig=signal_data,
                        executor=executor,
                        store=store,
                        settings=settings,
                        positions_by_symbol=positions_by_symbol,
                        open_count=open_count,
                    )
                    if opened:
                        open_count += 1
                    continue

                last_direction = store.get_last_direction(symbol)
                should_open = (
                    signal_data.direction in {"long", "short"}
                    and signal_data.direction != last_direction
                    and executor is not None
                    and open_count < settings.max_open_positions
                )

                executed = False
                note = "paper_mode"

                if should_open:
                    sl_distance = signal_data.atr * settings.atr_stop_mult
                    tp_distance = signal_data.atr * settings.atr_take_mult
                    result = executor.open_position(
                        yf_symbol=symbol,
                        direction=signal_data.direction,
                        sl_distance=sl_distance,
                        tp_distance=tp_distance,
                        lot_size=settings.lot_size,
                        comment=settings.order_label,
                        entry_price_hint=signal_data.last_close,
                        label=settings.position_label,
                    )
                    executed = bool(result.success)
                    note = (
                        f"live_open:{'ok' if result.success else result.error}"
                    )
                    if result.success:
                        risk_price = max(sl_distance, 0.0)
                        if result.broker_position_id > 0 and risk_price > 0:
                            store.upsert_position_state(
                                broker_position_id=result.broker_position_id,
                                symbol=symbol,
                                entry_price=(
                                    result.fill_price if result.fill_price > 0 else signal_data.last_close
                                ),
                                initial_volume=(
                                    result.volume if result.volume > 0 else lots_to_volume(settings.lot_size)
                                ),
                                risk_price=risk_price,
                            )
                        open_count += 1
                        log.info(
                            "OPEN %s %s lot=%.2f sl=%.6f tp=%.6f",
                            symbol,
                            signal_data.direction,
                            settings.lot_size,
                            sl_distance,
                            tp_distance,
                        )

                store.add_decision(
                    symbol=symbol,
                    direction=signal_data.direction,
                    momentum_value=signal_data.momentum_value,
                    atr=signal_data.atr,
                    close_price=signal_data.last_close,
                    executed=executed,
                    note=note,
                )
                store.set_last_direction(symbol, signal_data.direction)
                log.info(
                    "%s signal=%s momentum=%.5f atr=%.6f close=%.6f executed=%s",
                    symbol,
                    signal_data.direction,
                    signal_data.momentum_value,
                    signal_data.atr,
                    signal_data.last_close,
                    executed,
                )
        except Exception:
            log.exception("Cycle failed")

        time.sleep(settings.poll_interval_sec)

    log.info("Momentum bot stopped")


if __name__ == "__main__":
    run()

