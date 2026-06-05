from __future__ import annotations

import logging
import signal
import time

import yfinance as yf

from fx_momentum_bot.config.settings import MomentumBotSettings
from fx_momentum_bot.state.store import MomentumStore
from fx_momentum_bot.strategy.momentum import build_signal
from fx_pro_bot.trading.auth import TokenData
from fx_pro_bot.trading.client import CTraderClient
from fx_pro_bot.trading.executor import TradeExecutor
from fx_pro_bot.trading.symbols import SymbolCache
from shared_oauth.token_client import (
    ServiceConfig,
    TokenServiceRejected,
    TokenServiceUnavailable,
    fetch_token,
    push_token,
)

log = logging.getLogger("fx_momentum_bot")

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Received signal %d, shutting down", signum)


def _fetch_candles(symbol: str, interval: str, period: str):
    data = yf.download(
        tickers=symbol,
        period=period,
        interval=interval,
        auto_adjust=False,
        progress=False,
        threads=False,
    )
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


def _count_open_positions_for_symbols(executor: TradeExecutor, symbols: tuple[str, ...]) -> int:
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
        trade_data = getattr(pos, "tradeData", None)
        sid = getattr(trade_data, "symbolId", None) if trade_data else None
        if sid in symbol_ids:
            count += 1
    return count


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

    log.info(
        "Momentum bot started | mode=%s | symbols=%s | interval=%s/%s | db=%s",
        "LIVE" if (settings.trading_enabled and executor is not None) else "PAPER",
        ",".join(settings.symbols),
        settings.yfinance_interval,
        settings.yfinance_period,
        settings.db_path,
    )

    while not _shutdown:
        try:
            if executor is not None:
                open_count = _count_open_positions_for_symbols(executor, settings.symbols)
            else:
                open_count = 0

            for symbol in settings.symbols:
                candles = _fetch_candles(symbol, settings.yfinance_interval, settings.yfinance_period)
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
                    )
                    executed = bool(result.success)
                    note = (
                        f"live_open:{'ok' if result.success else result.error}"
                    )
                    if result.success:
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

