"""cTrader deal-list адаптер — ground truth по P&L momentum-бота (TASKSPEC §3.2).

Источник истины по деньгам — cTrader история сделок (``get_deal_list``):
broker-净 grossProfit/swap/commission (stats-collection.mdc, ctrader-pnl.mdc).
Логика реконструкции повторяет проверенный на реальных данных
``scripts/momentum_pnl_audit.py`` (атрибуция по торговой вселенной, т.к.
ProtoOADeal НЕ несёт label — делим deal'ы по symbolId).

Соединение строится через тот же token-service + CTraderClient, что у бота
(переиспользуем ``fx_momentum_bot.app.main._build_executor`` как инфраструктуру,
не торговую логику). Лимит cTrader — 2 connections per application
(https://help.ctrader.com/open-api/, api-docs.mdc): если momentum + fx_ai_trader
уже держат 2 коннекта на одном app, для tradecard нужен ОТДЕЛЬНЫЙ client_id
(TRADECARD_MOMENTUM_CTRADER_*) либо запуск в окно, когда слот свободен.

Read-only: только ``get_deal_list`` / ``reconcile``; ордера не ставятся.
"""
from __future__ import annotations

import logging

from tradecard_momentum.analysis.trade import MomentumTrade
from tradecard_momentum.config.settings import TradecardMomentumSettings
from tradecard_momentum.data.momentum_db import EntryDecision

log = logging.getLogger("tradecard_momentum.broker")


def _scale_price(raw: object) -> float:
    """ProtoOADeal.executionPrice → реальная цена.

    cTrader иногда отдаёт цену в scaled-формате (×100000). Эвристика как в
    scripts/_momentum_trade_dump.py: делим, только если число неправдоподобно
    велико для цены инструмента.
    """
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if abs(v) > 1_000_000:
        return v / 100_000.0
    return v


def _build_readonly_executor(cfg: TradecardMomentumSettings):
    """Подключённый cTrader-executor (read-only использование).

    Переиспользуем ``_build_executor`` momentum-бота, подменяя creds на
    TRADECARD_MOMENTUM_* (если заданы) и форсируя trading_enabled=True (нам
    нужен только подключённый клиент; ордера мы не ставим). None при отсутствии
    конфигурации/недоступности token-service.
    """
    from fx_momentum_bot.app.main import _build_executor
    from fx_momentum_bot.config.settings import MomentumBotSettings

    base = MomentumBotSettings()
    overrides: dict = {"trading_enabled": True}
    if cfg.ctrader_client_id:
        overrides["ctrader_client_id"] = cfg.ctrader_client_id
    if cfg.ctrader_client_secret:
        overrides["ctrader_client_secret"] = cfg.ctrader_client_secret
    if cfg.ctrader_account_id:
        overrides["ctrader_account_id"] = cfg.ctrader_account_id
    if cfg.ctrader_host_type:
        overrides["ctrader_host_type"] = cfg.ctrader_host_type
    if cfg.token_service_url:
        overrides["token_service_url"] = cfg.token_service_url
    if cfg.token_service_secret:
        overrides["token_service_secret"] = cfg.token_service_secret
    if cfg.token_service_label:
        overrides["token_service_label"] = cfg.token_service_label
    settings = base.model_copy(update=overrides)
    return _build_executor(settings)


def _match_decision(decisions: list[EntryDecision], *, symbol_yf: str | None,
                    side: str, ts_open: float, window_sec: float,
                    ) -> EntryDecision | None:
    """Ближайшее executed-решение к открытию сделки (symbol + side + время)."""
    if symbol_yf is None:
        return None
    best: EntryDecision | None = None
    best_dt = window_sec + 1.0
    for d in decisions:
        if d.symbol_yf != symbol_yf or d.direction != side:
            continue
        dt = abs(d.ts - ts_open)
        if dt <= window_sec and dt < best_dt:
            best, best_dt = d, dt
    return best


def fetch_momentum_trades(cfg: TradecardMomentumSettings, *, since_ts: float,
                          until_ts: float, decisions: list[EntryDecision],
                          ) -> list[MomentumTrade]:
    """Реконструировать закрытые сделки momentum-вселенной из cTrader deal-list.

    Возвращает список ``MomentumTrade`` (ground truth net), обогащённый сигналом
    входа (из ``decisions``). Сделки вне вселенной momentum (XAUUSD/BRENT/NG —
    fx_ai_trader) ИСКЛЮЧАЮТСЯ (атрибуция по symbolId). Пустой список при
    недоступном брокере (НЕ выдумываем P&L — no-data-fitting.mdc).
    """
    from fx_pro_bot.trading.symbols import CTRADER_TO_YFINANCE

    executor = _build_readonly_executor(cfg)
    if executor is None:
        log.warning("cTrader executor недоступен (creds/token-service) — "
                    "broker P&L пропущен, отчёт без ground truth")
        return []

    client = executor.client
    symbols = executor.symbols
    try:
        # Вселенная momentum → cTrader symbol_ids (атрибуция деалов).
        momentum_sids: set[int] = set()
        for yf_sym in cfg.momentum_symbols:
            info = symbols.resolve_yfinance(yf_sym)
            if info is not None:
                momentum_sids.add(info.symbol_id)

        from_ms = int(since_ts * 1000)
        to_ms = int(until_ts * 1000)
        resp = client.get_deal_list(from_ts=from_ms, to_ts=to_ms, max_rows=2000)
        deals = list(resp.deal) if hasattr(resp, "deal") else []

        by_pos: dict[int, list] = {}
        for d in deals:
            by_pos.setdefault(int(d.positionId), []).append(d)

        out: list[MomentumTrade] = []
        for pid, ds in by_pos.items():
            ds.sort(key=lambda x: int(getattr(x, "executionTimestamp", 0)))
            opening = ds[0]
            closings = [x for x in ds if x.HasField("closePositionDetail")]
            if not closings:
                continue  # ещё открыта
            sid = int(getattr(opening, "symbolId", 0))
            if sid not in momentum_sids:
                continue  # другой бот (вне вселенной momentum)
            info = symbols.get_by_id(sid)
            sname = info.name if info else f"id={sid}"
            side = "long" if int(getattr(opening, "tradeSide", 0)) == 1 else "short"

            gross = swap = comm = 0.0
            for c in closings:
                cpd = c.closePositionDetail
                div = 10 ** (int(cpd.moneyDigits) if cpd.moneyDigits else 2)
                gross += cpd.grossProfit / div
                swap += cpd.swap / div
                comm += cpd.commission / div
            last_close = closings[-1]
            ts_open = int(getattr(opening, "executionTimestamp", 0)) / 1000.0
            ts_close = int(getattr(last_close, "executionTimestamp", 0)) / 1000.0
            entry = _scale_price(getattr(opening, "executionPrice", 0))
            exit_px = _scale_price(getattr(last_close, "executionPrice", 0))

            yf_sym = CTRADER_TO_YFINANCE.get(sname)
            dec = _match_decision(
                decisions, symbol_yf=yf_sym, side=side, ts_open=ts_open,
                window_sec=cfg.decision_match_window_sec)
            signal_mom = abs(dec.momentum_value) if dec else None
            signal_atr = dec.atr if (dec and dec.atr > 0) else None
            risk_price = (signal_atr * cfg.atr_stop_mult
                          if signal_atr else None)

            out.append(MomentumTrade(
                position_id=pid, symbol=sname, side=side,
                ts_open=ts_open, ts_close=ts_close, entry=entry, exit=exit_px,
                volume_units=int(getattr(opening, "filledVolume", 0) or 0),
                gross_usd=round(gross, 4), swap_usd=round(swap, 4),
                commission_usd=round(comm, 4),
                signal_momentum=signal_mom, signal_atr=signal_atr,
                ctx_ema_dist_atr=(dec.ctx_ema_dist_atr if dec else None),
                ctx_adx=(dec.ctx_adx if dec else None),
                ctx_with_htf=(dec.ctx_with_htf if dec else None),
                ctx_spread_pips=(dec.ctx_spread_pips if dec else None),
                risk_price=risk_price, n_closing_deals=len(closings)))
        out.sort(key=lambda t: t.ts_close or 0.0)
        return out
    finally:
        try:
            if hasattr(client, "stop"):
                client.stop()
        except Exception:  # noqa: BLE001
            log.debug("client.stop() failed (ignored)")
