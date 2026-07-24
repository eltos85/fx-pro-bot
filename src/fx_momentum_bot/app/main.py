from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

from fx_momentum_bot.config.settings import MomentumBotSettings
from fx_pro_bot.config.settings import calc_lot_size
from fx_momentum_bot.state.store import MomentumStore
from fx_momentum_bot.strategy.context_metrics import (
    EntryContext,
    adx_block_reason,
    compute_entry_context,
)
from fx_momentum_bot.strategy.event_guard import (
    high_impact_event_near,
    high_impact_event_upcoming,
)
from fx_momentum_bot.strategy.friday_flat import friday_entry_blocked, friday_flat_due
from fx_momentum_bot.strategy.momentum import MomentumSignal, build_signal
from fx_momentum_bot.strategy.session_filter import (
    hour_blocklist_skip_reason,
    session_skip_reason,
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


_INTERVAL_SEC = {
    "1m": 60,
    "2m": 120,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "1h": 3600,
    "90m": 5400,
    "1d": 86400,
}


def _drop_forming_bar(df: pd.DataFrame | None, interval: str) -> pd.DataFrame | None:
    """Отбросить последний НЕЗАКРЫТЫЙ бар: сигналы только по закрытым барам.

    yfinance включает текущий формирующийся бар (timestamp = начало бара).
    Сигнал по нему «репейнтит»: momentum/reclaim может появиться в середине
    бара и исчезнуть к закрытию — отсюда дребезг входов вокруг порога
    (3×USDJPY long за один день 06-05). Канон — подтверждение на close бара
    (Al Brooks 2012 ch.5, тот же принцип что confirm bar у SessionOrb).
    """
    if df is None or df.empty:
        return df
    sec = _INTERVAL_SEC.get(interval)
    if sec is None or not isinstance(df.index, pd.DatetimeIndex):
        return df
    last_open = df.index[-1]
    if last_open.tzinfo is None:
        last_open = last_open.tz_localize("UTC")
    now = pd.Timestamp.now(tz="UTC")
    if last_open.tz_convert("UTC") + pd.Timedelta(seconds=sec) > now:
        return df.iloc[:-1]
    return df


def _broker_price(
    executor: TradeExecutor, symbol: str, max_age_sec: float = 900.0
) -> float | None:
    """Текущая цена ИНСТРУМЕНТА ИСПОЛНЕНИЯ (cTrader spot mid) для yf-символа.

    Критично для GC=F→XAUUSD: данные стратегии — фьючерс COMEX, исполнение —
    спот-металл, базис между ними десятки долларов. Все цены, сравниваемые с
    брокерскими (entry hint для slippage-guard, current_price для R-multiple/
    BE/trailing), обязаны быть в координатах брокера, не yfinance.
    Возвращает None если нет подписки/свежей цены (вызывающий код делает
    fallback на yfinance close — для FX-пар расхождение пренебрежимо).
    """
    info = executor.symbols.resolve_yfinance(symbol)
    if info is None:
        return None
    try:
        px = executor.client.get_spot_price(info.symbol_id, max_age_sec=max_age_sec)
    except Exception:  # noqa: BLE001
        return None
    if not px or not px.get("mid"):
        return None
    return float(px["mid"])


def _subscribe_spots(executor: TradeExecutor, symbols: tuple[str, ...]) -> None:
    """Подписаться на spot-стрим всех торгуемых символов (для _broker_price)."""
    sids = []
    for symbol in symbols:
        info = executor.symbols.resolve_yfinance(symbol)
        if info is not None:
            sids.append(info.symbol_id)
    if not sids:
        return
    try:
        executor.client.subscribe_spots(sids)
    except Exception as exc:  # noqa: BLE001
        log.warning("subscribe_spots failed (fallback на yfinance close): %s", exc)


def _position_lot(
    settings: MomentumBotSettings, symbol: str, sl_distance: float, fallback_lot: float
) -> float:
    """ATR-scaled лот от фикс-риска $ (Tharp ch.11) либо legacy фикс-лот.

    Переиспользует advisor'овский calc_lot_size: lot = risk / (sl_pips ×
    pip_value), клампы [0.01, max_lot_size]. Выравнивает риск на сделку
    между FX (~$2-3 при 0.01) и золотом (~$24-32 при 0.01): один стоп
    золота не должен стоить 10 FX-винов (broker-truth 06-05→06-10).
    """
    if settings.risk_per_trade_usd <= 0:
        return fallback_lot
    return calc_lot_size(
        symbol,
        sl_distance,
        settings.risk_per_trade_usd,
        max_lot=settings.max_lot_size,
    )


def _spread_too_wide(
    executor: TradeExecutor, symbol: str, sl_distance: float, max_fraction: float
) -> str | None:
    """Причина скипа, если live спред > max_fraction от SL-дистанции.

    Спред — прямой вычет из R (вход по ask, выход/SL по bid): при спреде
    10% от риска система 2:1 теряет ~0.1R на сделку до начала торговли
    (cost-to-risk, Harris 2003 ch.21). Меряем фактический bid/ask — ночь,
    роллувер 17:00 ET и пост-релизные минуты блокируются сами собой.
    Нет данных спреда (нет подписки/стейл) — НЕ блокируем: guard защита,
    не зависимость.
    """
    if max_fraction <= 0 or sl_distance <= 0:
        return None
    info = executor.symbols.resolve_yfinance(symbol)
    if info is None:
        return None
    try:
        px = executor.client.get_spot_price(info.symbol_id, max_age_sec=900.0)
    except Exception:  # noqa: BLE001
        return None
    if not px or px.get("bid") is None or px.get("ask") is None:
        return None
    spread = float(px["ask"]) - float(px["bid"])
    if spread <= 0:
        return None
    fraction = spread / sl_distance
    if fraction > max_fraction:
        return f"spread={spread:.5f}={fraction:.0%} of SL-dist (max {max_fraction:.0%})"
    return None


def _entry_spread_pips(executor: TradeExecutor | None, symbol: str) -> float | None:
    """Live-спред в пипсах на момент решения (observability, не гейт).

    Тот же источник, что у спред-гарда (_spread_too_wide): spot-кэш клиента.
    None при отсутствии данных — метрика опциональна.
    """
    if executor is None:
        return None
    info = executor.symbols.resolve_yfinance(symbol)
    if info is None:
        return None
    try:
        px = executor.client.get_spot_price(info.symbol_id, max_age_sec=900.0)
    except Exception:  # noqa: BLE001
        return None
    if not px or px.get("bid") is None or px.get("ask") is None:
        return None
    spread = float(px["ask"]) - float(px["bid"])
    if spread <= 0:
        return None
    from fx_pro_bot.config.settings import pip_size
    ps = pip_size(symbol)
    return round(spread / ps, 2) if ps > 0 else None


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
    executor: TradeExecutor,
    symbols: tuple[str, ...],
    *,
    labels: frozenset[str],
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
        trade_data = getattr(pos, "tradeData", None)
        # Изоляция по label: считаем ТОЛЬКО свои позиции, не чужих ботов
        # на общем счёте (напр. XAUUSD у fx_ai_trader label="ai-fx-trader").
        # label живёт в ProtoOATradeData, НЕ на самой ProtoOAPosition.
        if getattr(trade_data, "label", "") not in labels:
            continue
        sid = getattr(trade_data, "symbolId", None) if trade_data else None
        if sid in symbol_ids:
            count += 1
    return count


def _flip_close_targets(
    positions: list[ManagedPosition], new_direction: str
) -> list[ManagedPosition]:
    """Позиции, которые надо закрыть при флипе сигнала на new_direction.

    Канон trend-following reversal (Donchian; Faith «Way of the Turtle» 2007:
    выход по противоположному сигналу): противоположный сигнал закрывает
    текущую позицию, а не открывает встречную рядом с ней — иначе экспозиция
    нулевая, а спред/комиссия платятся дважды.
    """
    if new_direction not in {"long", "short"}:
        return []
    return [p for p in positions if p.side != new_direction]


def _has_same_side_position(
    positions: list[ManagedPosition], direction: str
) -> bool:
    """Есть ли уже открытая позиция этого символа в направлении сигнала.

    Per-symbol гард входа — инвариант бэктест-модели «один трейд на символ»
    (scripts/momentum_exit_backtest.py: вход только при pos is None). Live
    его не имел, и edge-trigger дублировал позиции двумя путями (диагностика
    BUILDLOG 2026-07-10, 22 дубля / −10.1R за 05.06–10.07):
      1) дребезг momentum вокруг threshold: long→flat→long при живой
         позиции — «новый флип» открывал вторую;
      2) retry после отказа slippage-guard, чей аварийный close не прошёл —
         позиция жила на брокере, бот открывал вторую.
    Сигнал при открытой same-side позиции считается отработанным (в бэктесте
    last_direction обновляется каждый бар независимо от позиции).
    """
    if direction not in {"long", "short"}:
        return False
    return any(p.side == direction for p in positions)


def _is_market_closed_error(err: str | None) -> bool:
    """Ошибка закрытия/открытия = рынок закрыт (выходные, maintenance break).

    cTrader возвращает ProtoOAErrorRes с текстом 'MARKET_CLOSED' когда
    символ вне торговых часов (FX закрыт Пт ~21:00 → Вс ~21:00 UTC).
    https://help.ctrader.com/open-api/ — ордера вне сессии отвергаются.
    """
    return bool(err) and "MARKET_CLOSED" in err


def _momentum_sign_direction(momentum_value: float, threshold: float = 0.0) -> str:
    """Направление, в сторону которого указывает ЗНАК momentum ('' если в мёртвой зоне).

    TSMOM sign rule (Moskowitz/Ooi/Pedersen 2012, «Time Series Momentum»,
    J. Financial Economics): позиция удерживается, пока знак momentum
    совпадает с её направлением. Пересечение нуля против позиции = тезис
    сделки умер → выход, не дожидаясь полного флипа за -threshold или SL.
    Вход остаётся на ±threshold → гистерезис (вход на импульсе, выход на
    его затухании), защита прибыли «когда дальше роста не будет».

    При ``threshold>0`` — гистерезисный выход (BUILDLOG 2026-07-24): сторона
    считается «живой» только когда |momentum| > threshold. Выход — на
    развороте за -threshold, а не на шумовом колебании вокруг нуля. На H1
    Hurst≈0.535 (тонкий trending edge), zero-cut закрывал победителей
    досрочно (avg win +0.48R, не доживая до BE@1R). threshold=0.0 = чистый
    sign-rule (старое поведение). См. ``decay_exit_threshold_mult`` в settings.
    Research: Chan — momentum требует persistence; Moskowitz 2012 — sign-rule.
    """
    if momentum_value > threshold:
        return "long"
    if momentum_value < -threshold:
        return "short"
    return ""


def _should_record_direction(*, live: bool, wants_open: bool, executed: bool) -> bool:
    """Обновлять ли last_direction после цикла (edge-trigger памяти сигнала).

    paper-режим — всегда (поведение не меняется); live — только если вход
    не требовался ЛИБО состоялся. Заблокированный/неудавшийся вход не
    фиксируем, чтобы повторить попытку, пока сигнал актуален.
    """
    return (not live) or (not wants_open) or executed


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
    executor: TradeExecutor,
    symbols: tuple[str, ...],
    *,
    labels: frozenset[str],
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
        td = getattr(pos, "tradeData", None)
        if td is None:
            continue
        # Изоляция по label: управляем ТОЛЬКО своими позициями (BE/трейлинг/
        # partial), не трогаем чужих ботов на общем счёте (fx_ai_trader и т.п.).
        # label живёт в ProtoOATradeData, НЕ на самой ProtoOAPosition.
        if getattr(td, "label", "") not in labels:
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
    signal_by_symbol: dict[str, MomentumSignal],
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
        # Цена для R-multiple/BE/trailing — В КООРДИНАТАХ БРОКЕРА.
        # yfinance last_close — только fallback: для GC=F→XAUUSD базис
        # фьючерс-спот ~$15-40 искажал R на единицы (см. BUILDLOG 2026-06-10).
        current_price = _broker_price(executor, symbol) or signal_data.last_close
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


def run() -> None:
    settings = MomentumBotSettings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    store = MomentumStore(settings.db_path)
    executor = _build_executor(settings)
    if executor is not None:
        _subscribe_spots(executor, settings.symbols)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Дедуп логов «рынок закрыт»: sign-decay хочет закрыть позицию, но рынок
    # закрыт (выходные) — попытки повторяются каждый цикл (чтобы исполнить
    # сразу на открытии), но логируем один раз на переходе, иначе 569
    # ERROR-строк за выходные (BUILDLOG 2026-06-15).
    market_closed_pids: set[int] = set()
    # Аналогичный дедуп для ОТКРЫТИЙ: symbol → уже логировали «рынок закрыт».
    market_closed_open_syms: set[str] = set()
    log.info(
        "Momentum bot started | mode=%s | momentum=%s | interval=%s/%s | db=%s",
        "LIVE" if (settings.trading_enabled and executor is not None) else "PAPER",
        ",".join(settings.symbols) or "-",
        settings.yfinance_interval,
        settings.yfinance_period,
        settings.db_path,
    )

    while not _shutdown:
        try:
            signal_by_symbol: dict[str, MomentumSignal] = {}
            ctx_by_symbol: dict[str, EntryContext | None] = {}
            positions_by_symbol: dict[str, list[ManagedPosition]] = {}
            if executor is not None:
                # Позиции нужны не только management'у: exit-on-flip у momentum
                # читает их независимо от флага position_management_enabled.
                positions_by_symbol = _collect_managed_positions(
                    executor, settings.symbols, labels=settings.managed_labels
                )
            for symbol in settings.symbols:
                candles = _drop_forming_bar(
                    _fetch_candles(
                        symbol, settings.yfinance_interval, settings.yfinance_period
                    ),
                    settings.yfinance_interval,
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
                # Метрики контекста решения (observability, BUILDLOG
                # 2026-07-03): считаются по тем же закрытым барам, что и
                # сигнал → без look-ahead. None не блокирует торговлю.
                ctx_by_symbol[symbol] = compute_entry_context(
                    candles, signal_data.direction
                )

            # Friday-flat-флаг цикла: вычисляем один раз (переиспользуется
            # для close-блока ниже и для запрета новых входов в окно flat).
            friday_flat_now = executor is not None and friday_flat_due(
                enabled=settings.friday_flat_enabled,
                flat_start=settings.friday_flat_start,
                flat_end=settings.friday_flat_end,
            )
            if friday_flat_now:
                log.info(
                    "FRIDAY-FLAT: окно закрытия перед выходными (%s–%s UTC)",
                    settings.friday_flat_start, settings.friday_flat_end,
                )
            # Блок новых входов — ШИРЕ окна закрытия: от flat_start до конца
            # пятницы UTC. Иначе вход в 20:45–21:00 (после окна flat, до FX
            # weekly close) немедленно уезжает в выходные — дыра, замеченная
            # 2026-06-26 (MARKET_CLOSED-спам 21:03–21:59, BUILDLOG 2026-07-02).
            friday_entry_block = executor is not None and friday_entry_blocked(
                enabled=settings.friday_flat_enabled,
                flat_start=settings.friday_flat_start,
            )

            if executor is not None and settings.position_management_enabled:
                _manage_positions(
                    executor=executor,
                    store=store,
                    settings=settings,
                    signal_by_symbol=signal_by_symbol,
                    positions_by_symbol=positions_by_symbol,
                )
            # Friday-flat: открытые momentum-позиции принудительно закрываются
            # перед выходными (окно в пятницу UTC, до FX close). Сопровождение
            # выше уже отработало (BE/partial/trailing); здесь закрываем
            # остаток, чтобы не везти через Сб/Вс (гэп понедельника вне 1R).
            # Retry в следующем цикле внутри окна; MARKET_CLOSED-дедуп через
            # market_closed_pids ниже по коду переиспользуется для логов.
            if friday_flat_now:
                for sym in settings.symbols:
                    remaining: list[ManagedPosition] = []
                    for pos in positions_by_symbol.get(sym, []):
                        close_res = executor.close_position(
                            pos.position_id, pos.volume
                        )
                        if close_res.success:
                            market_closed_pids.discard(pos.position_id)
                            log.info(
                                "FRIDAY FLAT %s #%d %s vol=%d closed "
                                "(не несём через выходные: гэп пн вне 1R)",
                                sym, pos.position_id, pos.side, pos.volume,
                            )
                        elif _is_market_closed_error(close_res.error):
                            # Рынок уже закрылся в этом цикле — повторим в
                            # следующем (внутри окна); логируем один раз.
                            if pos.position_id not in market_closed_pids:
                                market_closed_pids.add(pos.position_id)
                                log.info(
                                    "FRIDAY FLAT отложен %s #%d: рынок закрыт",
                                    sym, pos.position_id,
                                )
                            remaining.append(pos)
                        else:
                            remaining.append(pos)
                            log.error(
                                "FRIDAY FLAT failed %s #%d: %s (повтор в цикле)",
                                sym, pos.position_id, close_res.error,
                            )
                    if sym in positions_by_symbol:
                        positions_by_symbol[sym] = remaining
            # Gap-защита (BUILDLOG 2026-07-24): HIGH-impact релиз в следующие
            # news_close_before_min минут → закрыть открытые позиции scoped-
            # символов, чтобы не нести gap за SL через шип релиза. Сопровождение
            # (BE/partial/trailing) выше уже отработало. Симметрично friday_flat
            # по retry/MARKET_CLOSED-дедупу. Scoping: US-релизы — все символы;
            # ECB — только EUR-пары; BoJ — только JPY-пары (внутри high_impact_event_upcoming).
            if (
                executor is not None
                and settings.news_close_enabled
                and settings.news_close_before_min > 0
            ):
                for sym in settings.symbols:
                    reason = high_impact_event_upcoming(
                        symbol=sym,
                        before_min=settings.news_close_before_min,
                    )
                    if reason is None:
                        continue
                    remaining: list[ManagedPosition] = []
                    for pos in positions_by_symbol.get(sym, []):
                        close_res = executor.close_position(
                            pos.position_id, pos.volume
                        )
                        if close_res.success:
                            market_closed_pids.discard(pos.position_id)
                            log.info(
                                "NEWS CLOSE %s #%d %s vol=%d closed (%s: "
                                "gap-защита, не нести шип релиза за SL)",
                                sym, pos.position_id, pos.side, pos.volume, reason,
                            )
                        elif _is_market_closed_error(close_res.error):
                            if pos.position_id not in market_closed_pids:
                                market_closed_pids.add(pos.position_id)
                                log.info(
                                    "NEWS CLOSE отложен %s #%d: рынок закрыт",
                                    sym, pos.position_id,
                                )
                            remaining.append(pos)
                        else:
                            remaining.append(pos)
                            log.error(
                                "NEWS CLOSE failed %s #%d: %s (повтор в цикле)",
                                sym, pos.position_id, close_res.error,
                            )
                    if sym in positions_by_symbol:
                        positions_by_symbol[sym] = remaining
            if executor is not None:
                open_count = _count_open_positions_for_symbols(
                    executor, settings.symbols, labels=settings.managed_labels
                )
            else:
                open_count = 0

            # Event-guard: HIGH-impact релиз в окне ±N минут → новые входы
            # блокируются; сопровождение и выходы работают.
            # Per-symbol scoping: US-релизы (CPI/FOMC/NFP) блокируют всё,
            # ECB — только EUR-пары, BoJ — только JPY-пары.
            # Andersen et al. 2003: вход в момент релиза ловит шип/фейкаут.
            news_blocks: dict[str, str] = {}
            session_skips: dict[str, str] = {}
            if settings.news_block_enabled:
                for symbol in settings.symbols:
                    reason = high_impact_event_near(
                        symbol=symbol,
                        before_min=settings.news_block_before_min,
                        after_min=settings.news_block_after_min,
                    )
                    if reason:
                        news_blocks[symbol] = reason
                if news_blocks:
                    log.info(
                        "EVENT-GUARD: входы заблокированы — %s",
                        "; ".join(f"{s}: {r}" for s, r in news_blocks.items()),
                    )
            # Session-фильтр: час закрытого бара, по которому взят сигнал.
            # Для 1h-интервала последний закрытый бар = now минус ~1ч; для
            # сессионных границ 07/21 погрешность в час несущественна.
            if settings.session_filter_enabled:
                now_h = datetime.now(timezone.utc).hour
                # Сдвиг на один интервал назад (сигнал по закрытому бару).
                try:
                    interval_h = _INTERVAL_SEC.get(settings.yfinance_interval, 3600) // 3600
                except Exception:  # noqa: BLE001
                    interval_h = 1
                signal_hour = (now_h - max(1, interval_h)) % 24
                skip_reason = session_skip_reason(
                    hour_utc=signal_hour,
                    enabled=settings.session_filter_enabled,
                    start_hour_utc=settings.session_filter_start_hour_utc,
                    end_hour_utc=settings.session_filter_end_hour_utc,
                )
                if skip_reason:
                    for symbol in settings.symbols:
                        session_skips[symbol] = skip_reason
                    log.info("SESSION-FILTER: входы заблокированы — %s", skip_reason)
            # NY-open block: конкретные часы UTC внутри ликвидной сессии
            # (BUILDLOG 2026-07-24): NY-open 14-16h UTC — liquidity trap,
            # WR 0-20% net −$109 на 34 сделках. Тот же signal_hour, что и у
            # session-filter (час закрытого бара).
            if settings.ny_open_block_enabled and settings.ny_open_block_hours:
                now_h = datetime.now(timezone.utc).hour
                try:
                    interval_h = _INTERVAL_SEC.get(settings.yfinance_interval, 3600) // 3600
                except Exception:  # noqa: BLE001
                    interval_h = 1
                signal_hour = (now_h - max(1, interval_h)) % 24
                ny_skip = hour_blocklist_skip_reason(
                    hour_utc=signal_hour,
                    enabled=settings.ny_open_block_enabled,
                    blocked_hours=settings.ny_open_block_hours,
                )
                if ny_skip:
                    for symbol in settings.symbols:
                        # Не перетираем более раннюю причину session-filter.
                        session_skips.setdefault(symbol, ny_skip)
                    log.info("NY-OPEN-BLOCK: входы заблокированы — %s", ny_skip)

            for symbol in settings.symbols:
                signal_data = signal_by_symbol.get(symbol)
                if signal_data is None:
                    continue

                sym_news_block = news_blocks.get(symbol)
                sym_session_block = session_skips.get(symbol)
                # Контекст входа (observability + ADX-фильтр с 2026-07-24).
                # Поднимаем выше ADX-блока: он читает ctx.adx.
                ctx = ctx_by_symbol.get(symbol)

                last_direction = store.get_last_direction(symbol)
                # Per-symbol гард: уже есть открытая позиция символа в эту же
                # сторону → входа не будет (см. _has_same_side_position).
                # wants_open=False → direction фиксируется, сигнал считается
                # отработанным (эквивалент бэктест-семантики).
                same_side_open = _has_same_side_position(
                    positions_by_symbol.get(symbol, []), signal_data.direction
                )
                # Edge-trigger: входим только на СМЕНЕ направления сигнала.
                wants_open = (
                    signal_data.direction in {"long", "short"}
                    and signal_data.direction != last_direction
                    and not same_side_open
                    and executor is not None
                )

                # Sign-decay exit (TSMOM sign rule, Moskowitz 2012) —
                # обобщение exit-on-flip: позиция живёт, пока ЗНАК momentum
                # совпадает с её направлением. Momentum пересёк ноль против
                # позиции (включая полный флип за -threshold) → закрываем.
                # Выполняем каждый цикл, независимо от max_positions —
                # выход важнее входа (и освобождает слот).
                #
                # Гистерезис (BUILDLOG 2026-07-24): порог выхода = signal_threshold
                # × decay_exit_threshold_mult. mult=1.0 → выход на -threshold
                # (полный гистерезис, победители доживают до BE/partial/trailing);
                # mult=0.0 → выход на пересечении нуля (старый sign-rule).
                decay_threshold = (
                    settings.signal_threshold * settings.decay_exit_threshold_mult
                )
                sign_dir = _momentum_sign_direction(
                    signal_data.momentum_value, decay_threshold
                )
                decay_closed = 0
                if executor is not None and sign_dir:
                    targets = _flip_close_targets(
                        positions_by_symbol.get(symbol, []), sign_dir
                    )
                    for pos in targets:
                        close_res = executor.close_position(pos.position_id, pos.volume)
                        if close_res.success:
                            decay_closed += 1
                            open_count = max(0, open_count - 1)
                            market_closed_pids.discard(pos.position_id)
                            log.info(
                                "DECAY CLOSE %s #%d %s vol=%d "
                                "(momentum=%.5f against position)",
                                symbol,
                                pos.position_id,
                                pos.side,
                                pos.volume,
                                signal_data.momentum_value,
                            )
                        elif _is_market_closed_error(close_res.error):
                            # Рынок закрыт (выходные): попытку повторим в
                            # след. цикле — исполнится на открытии. Логируем
                            # один раз на переходе, без спама каждые 5 мин.
                            if pos.position_id not in market_closed_pids:
                                market_closed_pids.add(pos.position_id)
                                log.info(
                                    "DECAY CLOSE отложен %s #%d %s: рынок "
                                    "закрыт, закрою на открытии",
                                    symbol, pos.position_id, pos.side,
                                )
                        else:
                            log.error(
                                "DECAY CLOSE failed %s #%d: %s",
                                symbol,
                                pos.position_id,
                                close_res.error,
                            )
                    if decay_closed:
                        positions_by_symbol[symbol] = [
                            p for p in positions_by_symbol.get(symbol, [])
                            if p.side == sign_dir
                        ]

                # ADX-фильтр входа (BUILDLOG 2026-07-24): рейндж (ADX<adx_min)
                # → momentum не работает. ctx.adx считается compute_entry_context
                # (раньше observability-only, теперь блокирующий). ctx=None
                # (мало данных / холодный старт) → НЕ блокировать.
                sym_adx_block = adx_block_reason(
                    ctx, enabled=settings.adx_filter_enabled, adx_min=settings.adx_min
                )

                should_open = (
                    wants_open
                    and open_count < settings.max_open_positions
                    and sym_news_block is None
                    and sym_session_block is None
                    and sym_adx_block is None
                    and not friday_entry_block
                )

                executed = False
                if executor is None:
                    note = "paper_mode"
                elif signal_data.direction not in {"long", "short"}:
                    note = "flat"
                elif signal_data.direction == last_direction:
                    note = "same_direction"
                elif same_side_open:
                    # Позиция в эту сторону уже открыта — дубль запрещён
                    # (per-symbol гард). Direction фиксируется: сигнал
                    # отработан существующей позицией.
                    note = "skip:already_open"
                elif friday_entry_block:
                    # От flat_start до конца пятницы новые входы запрещены:
                    # позиция уехала бы в выходные (в окне flat её тут же
                    # закроем, после окна — некому закрыть до понедельника).
                    # Direction НЕ фиксируется → сигнал повторится в
                    # следующую неделю, если актуален.
                    note = "skip:friday_flat_window"
                elif sym_news_block is not None:
                    # Event-guard: вход отложен, direction НЕ фиксируется
                    # (_should_record_direction) → попытка повторится после
                    # окна, пока сигнал актуален. Сигнал не теряется.
                    note = f"skip:news_window({sym_news_block})"
                elif sym_session_block is not None:
                    # Session-фильтр / NY-open block: вход отложен до
                    # ликвидной сессии / вне враждебных часов, direction НЕ
                    # фиксируется → попытка повторится, пока сигнал актуален.
                    note = f"skip:off_session({sym_session_block})"
                elif sym_adx_block is not None:
                    # ADX-фильтр: вход в рейндже отложен, direction НЕ
                    # фиксируется → повторится, когда ADX поднимется над min.
                    note = f"skip:{sym_adx_block}"
                elif not should_open:
                    note = "skip:max_positions"
                else:
                    note = "live_open:pending"
                if decay_closed:
                    note = f"{note}+decay_closed:{decay_closed}"

                if should_open:
                    sl_distance = signal_data.atr * settings.atr_stop_mult
                    spread_err = _spread_too_wide(
                        executor, symbol, sl_distance,
                        settings.max_spread_risk_fraction,
                    )
                    if spread_err:
                        # direction не фиксируется (wants_open и не executed)
                        # → попытка повторится, когда спред нормализуется.
                        should_open = False
                        note = f"skip:wide_spread({spread_err})"
                if should_open:
                    lot = _position_lot(
                        settings, symbol, sl_distance, settings.lot_size
                    )
                    # БЕЗ брокерского TP: выход ведёт сопровождение — BE@1R,
                    # partial@1.5R, ATR-trailing (Raschke partial+runner,
                    # LeBeau Chandelier). Старый TP=3.5 ATR стоял на 1.4R и
                    # закрывал позицию ДО активации partial/trailing (1.5R) —
                    # весь runner-механизм был мёртвым кодом. Согласовано
                    # 2026-06-10. Slippage-guard при tp=None использует
                    # static-лимит (max_slippage_pips).
                    result = executor.open_position(
                        yf_symbol=symbol,
                        direction=signal_data.direction,
                        sl_distance=sl_distance,
                        tp_distance=None,
                        lot_size=lot,
                        comment=settings.order_label,
                        # hint в координатах брокера (для slippage-guard);
                        # fallback на yfinance close для FX безопасен.
                        entry_price_hint=(
                            _broker_price(executor, symbol) or signal_data.last_close
                        ),
                        label=settings.position_label,
                    )
                    executed = bool(result.success)
                    note = (
                        f"live_open:{'ok' if result.success else result.error}"
                    )
                    if not result.success and _is_market_closed_error(result.error):
                        # Рынок закрыт: попытка повторится в след. цикле
                        # (direction не фиксируется), но логируем один раз
                        # на переходе — симметрично decay-close дедупу
                        # (BUILDLOG 2026-06-15), без спама каждые 5 мин.
                        if symbol not in market_closed_open_syms:
                            market_closed_open_syms.add(symbol)
                            log.info(
                                "OPEN отложен %s %s: рынок закрыт, "
                                "повторю на открытии",
                                symbol, signal_data.direction,
                            )
                    if result.success:
                        market_closed_open_syms.discard(symbol)
                        risk_price = max(sl_distance, 0.0)
                        if result.broker_position_id > 0 and risk_price > 0:
                            store.upsert_position_state(
                                broker_position_id=result.broker_position_id,
                                symbol=symbol,
                                entry_price=(
                                    result.fill_price if result.fill_price > 0 else signal_data.last_close
                                ),
                                initial_volume=(
                                    result.volume if result.volume > 0 else lots_to_volume(lot)
                                ),
                                risk_price=risk_price,
                            )
                        open_count += 1
                        log.info(
                            "OPEN %s %s lot=%.2f sl=%.6f tp=runner(BE/partial/trail)",
                            symbol,
                            signal_data.direction,
                            lot,
                            sl_distance,
                        )

                # Контекст решения (observability, BUILDLOG 2026-07-03):
                # спред меряем только на реальных попытках входа — на
                # skip-строках он не несёт информации о качестве исполнения.
                ctx = ctx_by_symbol.get(symbol)
                spread_pips = (
                    _entry_spread_pips(executor, symbol)
                    if note.startswith("live_open")
                    else None
                )
                if note.startswith("live_open") and ctx is not None:
                    log.info(
                        "ENTRY CTX %s %s: ema_dist=%+.2f ATR, adx=%.1f, "
                        "with_htf=%s, spread=%s pip",
                        symbol,
                        signal_data.direction,
                        ctx.ema_dist_atr,
                        ctx.adx,
                        ctx.with_htf,
                        f"{spread_pips:.2f}" if spread_pips is not None else "n/a",
                    )

                store.add_decision(
                    symbol=symbol,
                    direction=signal_data.direction,
                    momentum_value=signal_data.momentum_value,
                    atr=signal_data.atr,
                    close_price=signal_data.last_close,
                    executed=executed,
                    note=note,
                    ctx_ema_dist_atr=ctx.ema_dist_atr if ctx else None,
                    ctx_adx=ctx.adx if ctx else None,
                    ctx_with_htf=ctx.with_htf if ctx else None,
                    ctx_spread_pips=spread_pips,
                )
                # НЕ фиксируем direction, если live-вход хотели, но он был
                # заблокирован (max_positions) или не удался: иначе edge-trigger
                # считает сигнал «отработанным» и сделка теряется навсегда.
                # Без фиксации попытка повторится в следующем цикле.
                if _should_record_direction(
                    live=executor is not None, wants_open=wants_open, executed=executed
                ):
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

