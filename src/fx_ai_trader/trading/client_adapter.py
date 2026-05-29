"""CTraderFxAdapter — обёртка над ``fx_pro_bot.CTraderClient`` для AI-агента.

Reuse инфраструктуры Advisor'а (правило ``strategy-guard.mdc``: импорт
``fx_pro_bot.trading.*`` разрешён только для infrastructure, не для
торговой логики).

Все методы:
- Используют internal-нотацию символов ("XAUUSD", "BZ=F") как public API.
- Маппят internal → cTrader через ``YFINANCE_TO_CTRADER`` и ``SymbolCache``.
- Маркируют все наши ордера ``label="ai-fx-trader"`` для broker-side
  изоляции от Advisor (``label="fx-pro-bot"``).
- При первом use лениво загружают symbols catalog (heavy-кэш).

Token-store шарится с Advisor через race-safe файл-lock в ``token_lock.py``
(research: Coder PR #22904, Nango OAuth-refresh, codex #10332).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.trading.token_lock import (
    ensure_valid_token_race_safe,
    save_refreshed_token,
)
from fx_pro_bot.trading.client import CTraderClient
from fx_pro_bot.trading.symbols import (
    SymbolCache,
    SymbolInfo,
    YFINANCE_TO_CTRADER,
    lots_to_volume,
    price_to_relative,
    volume_to_lots,
)

log = logging.getLogger(__name__)


# Trendbars приходят как low + delta*, в precision 10⁻⁵ независимо от
# digits символа (см. `src/fx_pro_bot/market_data/ctrader_feed.py`,
# документация и live-проверка EURUSD/USDJPY).
_TRENDBAR_SCALE = 100_000


@dataclass
class Bar:
    """Internal bar для AI-агента — упрощённый OHLCV."""
    ts: int  # unix seconds (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class BrokerPosition:
    """Открытая позиция на cTrader (только наши, отфильтрованные по label)."""
    position_id: int
    symbol_name: str  # cTrader name ("XAUUSD", "BRENT")
    internal_symbol: str | None  # yfinance-нотация если знаем mapping
    side: str  # "BUY" / "SELL"
    volume: int  # cTrader volume (lots × contract_size)
    volume_lots: float
    entry_price: float
    sl_price: float | None
    tp_price: float | None
    label: str


@dataclass
class OrderResult:
    success: bool
    broker_position_id: int = 0
    fill_price: float = 0.0
    volume: int = 0
    volume_lots: float = 0.0
    error: str = ""


def _internal_to_ctrader(internal: str) -> str:
    """yfinance-нотация → cTrader symbol name. XAUUSD остаётся XAUUSD."""
    if internal in YFINANCE_TO_CTRADER:
        return YFINANCE_TO_CTRADER[internal]
    return internal


def _ctrader_to_internal(ctrader_name: str) -> str | None:
    """Reverse mapping. None если не найдено (новый символ из cTrader)."""
    for yf, ct in YFINANCE_TO_CTRADER.items():
        if ct.upper() == ctrader_name.upper():
            return yf
    if ctrader_name.upper() == "XAUUSD":
        return "XAUUSD"
    return None


class CTraderFxAdapter:
    """High-level cTrader-клиент для AI-агента (gold + oil)."""

    def __init__(self, settings: AiFxTraderSettings) -> None:
        self._settings = settings
        self._symbols = SymbolCache()
        self._client: CTraderClient | None = None
        self._symbols_loaded = False

    # ─── lifecycle ───────────────────────────────────────────────────────

    def start(self, timeout: float = 30.0) -> None:
        """Авторизоваться + подключиться + загрузить symbols catalog."""
        token = ensure_valid_token_race_safe(
            self._settings.ctrader_token_path,
            self._settings.ctrader_client_id,
            self._settings.ctrader_client_secret,
        )
        self._client = CTraderClient(
            client_id=self._settings.ctrader_client_id,
            client_secret=self._settings.ctrader_client_secret,
            access_token=token.access_token,
            account_id=self._settings.ctrader_account_id,
            host_type=self._settings.ctrader_host_type,
            refresh_token=token.refresh_token,
            expires_at=token.expires_at,
            on_token_refreshed=lambda a, r, exp: save_refreshed_token(
                self._settings.ctrader_token_path, a, r, exp,
            ),
        )
        self._client.start(timeout=timeout)
        self._load_symbols_catalog()

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                log.exception("CTraderFxAdapter.stop: client.stop failed")
            self._client = None

    @property
    def is_ready(self) -> bool:
        return (
            self._client is not None
            and self._client.is_ready
            and self._symbols_loaded
        )

    def _load_symbols_catalog(self) -> None:
        """Загрузить cTrader symbol catalog и заполнить SymbolCache."""
        if self._client is None:
            raise RuntimeError("client не инициализирован")
        try:
            resp = self._client.get_symbols()
        except Exception:
            log.exception("get_symbols failed; адаптер не сможет резолвить символы")
            return

        light: dict[int, str] = {}
        for s in resp.symbol:
            name = s.symbolName if hasattr(s, "symbolName") else str(s.symbolId)
            light[s.symbolId] = name

        # batched details
        details: dict[int, object] = {}
        id_list = list(light.keys())
        for i in range(0, len(id_list), 50):
            chunk = id_list[i : i + 50]
            try:
                det_resp = self._client.get_symbol_details(chunk)
                for sym_det in det_resp.symbol:
                    details[sym_det.symbolId] = sym_det
            except Exception:
                log.exception("get_symbol_details batch %d failed (продолжаю)", i)

        infos: list[SymbolInfo] = []
        for sid, name in light.items():
            det = details.get(sid)
            infos.append(
                SymbolInfo(
                    symbol_id=sid,
                    name=name,
                    min_volume=getattr(det, "minVolume", 1000) if det else 1000,
                    max_volume=getattr(det, "maxVolume", 10_000_000) if det else 10_000_000,
                    step_volume=getattr(det, "stepVolume", 1000) if det else 1000,
                    digits=getattr(det, "digits", 5) if det else 5,
                    contract_size=getattr(det, "lotSize", 100_000) if det else 100_000,
                )
            )
        self._symbols.populate(infos)
        self._symbols_loaded = True

        # late import to avoid cycle при import executor → adapter
        from fx_ai_trader.trading.executor import (
            _pip_size_for,
            _pip_value_per_std_lot,
        )
        for internal in self._settings.symbols:
            info = self.get_symbol_info(internal)
            if info:
                pip_size = _pip_size_for(internal)
                pip_value = _pip_value_per_std_lot(internal)
                log.info(
                    "FX-AI symbol resolved: %s → %s (id=%d, digits=%d, "
                    "min_vol=%d, step_vol=%d, lot=%d) | "
                    "pip_size=%.4f, pip_value=$%.2f/pip/lot",
                    internal, info.name, info.symbol_id, info.digits,
                    info.min_volume, info.step_volume, info.contract_size,
                    pip_size, pip_value,
                )
            else:
                log.warning("FX-AI symbol %s НЕ найден в cTrader catalog", internal)

    # ─── symbol resolution ───────────────────────────────────────────────

    def get_symbol_info(self, internal_symbol: str) -> SymbolInfo | None:
        ct_name = _internal_to_ctrader(internal_symbol)
        return self._symbols.get_by_name(ct_name)

    # ─── market data ─────────────────────────────────────────────────────

    def get_bars(
        self,
        internal_symbol: str,
        period_minutes: int,
        count: int,
    ) -> list[Bar]:
        """Загрузить N последних свечей через ProtoOAGetTrendbarsReq.

        Returns пустой список при недоступности (caller graceful-skip).
        """
        if self._client is None:
            log.warning("get_bars(%s): client не запущен", internal_symbol)
            return []
        info = self.get_symbol_info(internal_symbol)
        if info is None:
            log.warning("get_bars(%s): symbol не найден в cTrader", internal_symbol)
            return []
        now_ms = int(time.time() * 1000)
        # Берём запас по времени: на 5×count минут раньше + 1 час буфер,
        # чтобы покрыть выходные / низкую волатильность и иметь нужное N.
        from_ms = now_ms - (count * period_minutes * 60 * 1000 * 2 + 3600 * 1000)
        try:
            raw = self._client.get_trendbars(
                symbol_id=info.symbol_id,
                period_minutes=period_minutes,
                from_ts_ms=from_ms,
                to_ts_ms=now_ms,
            )
        except Exception:
            log.exception("get_trendbars(%s, %dm) failed", internal_symbol, period_minutes)
            return []
        bars: list[Bar] = []
        for tb in raw:
            low_abs = tb.low
            bars.append(
                Bar(
                    ts=int(tb.utcTimestampInMinutes * 60),
                    open=(low_abs + tb.deltaOpen) / _TRENDBAR_SCALE,
                    high=(low_abs + tb.deltaHigh) / _TRENDBAR_SCALE,
                    low=low_abs / _TRENDBAR_SCALE,
                    close=(low_abs + tb.deltaClose) / _TRENDBAR_SCALE,
                    volume=float(tb.volume),
                )
            )
        bars.sort(key=lambda b: b.ts)
        return bars[-count:] if count else bars

    def subscribe_live_prices(self) -> None:
        """Подписаться на spot-стрим по всем торгуемым символам (Phase 1).

        Вызывается один раз после ``start()``. Живая цена далее доступна
        через ``get_current_price`` (предпочитает spot mid над H1-close).
        Подписки переживают reconnect (клиент переоформляет их сам в
        ``_do_auth``). Graceful: при ошибке логируем и продолжаем — фолбэк
        на M1-close в ``get_current_price`` сохраняет работоспособность.
        """
        if self._client is None:
            log.warning("subscribe_live_prices: client не запущен")
            return
        if not self._settings.live_price_enabled:
            log.info("subscribe_live_prices: live_price disabled, skip")
            return
        symbol_ids: list[int] = []
        for internal in self._settings.symbols:
            info = self.get_symbol_info(internal)
            if info is not None:
                symbol_ids.append(info.symbol_id)
            else:
                log.warning("subscribe_live_prices: %s не найден", internal)
        if not symbol_ids:
            log.warning("subscribe_live_prices: нет символов для подписки")
            return
        try:
            self._client.subscribe_spots(symbol_ids)
        except Exception:
            log.exception(
                "subscribe_live_prices failed (фолбэк на M1-close сохранён)"
            )

    def get_live_spot_mid(self, internal_symbol: str) -> float | None:
        """ТОЛЬКО живой spot mid из in-memory кэша; БЕЗ фолбэка на API.

        Для event-датчика (Phase 2): он опрашивается часто (каждые ~15с),
        поэтому НЕ должен дёргать ProtoOAGetTrendbarsReq (rate-limit). Если
        живой цены нет (стрим не пришёл / устарела / live disabled) —
        возвращает None, датчик просто пропускает позицию.
        """
        if self._client is None or not self._settings.live_price_enabled:
            return None
        info = self.get_symbol_info(internal_symbol)
        if info is None:
            return None
        spot = self._client.get_spot_price(
            info.symbol_id,
            max_age_sec=float(self._settings.live_price_max_age_sec),
        )
        if spot is not None and spot.get("mid"):
            return float(spot["mid"])
        return None

    def get_current_price(self, internal_symbol: str) -> float | None:
        """Текущая рыночная цена.

        Phase 1 (2026-05-29): предпочитает живой spot mid (bid+ask)/2 из
        ProtoOASubscribeSpots-стрима; фолбэк на последний M1-close, если
        стрима ещё нет / цена устарела / live_price отключён.
        """
        if self._client is not None and self._settings.live_price_enabled:
            info = self.get_symbol_info(internal_symbol)
            if info is not None:
                spot = self._client.get_spot_price(
                    info.symbol_id,
                    max_age_sec=float(self._settings.live_price_max_age_sec),
                )
                if spot is not None and spot.get("mid"):
                    return float(spot["mid"])
        bars = self.get_bars(internal_symbol, period_minutes=1, count=3)
        if not bars:
            return None
        return bars[-1].close

    # ─── positions ───────────────────────────────────────────────────────

    def get_open_positions(self) -> list[BrokerPosition] | None:
        """Возвращает наши открытые позиции (label = "ai-fx-trader").

        Returns ``None`` если запрос упал (API недоступно) — caller отличает
        ``None`` от ``[]`` (см. инцидент Bybit-агента 2026-05-07: ``None``
        = «не отвечает», не «нет позиций»).
        """
        if self._client is None:
            return None
        try:
            resp = self._client.reconcile()
        except Exception:
            log.exception("reconcile failed")
            return None
        out: list[BrokerPosition] = []
        for p in resp.position:
            label = getattr(p, "label", "") or ""
            if label != self._settings.order_label:
                continue
            ct_name = self._symbols.get_by_id(p.symbolId)
            ct_name_str = ct_name.name if ct_name else f"id={p.symbolId}"
            internal = _ctrader_to_internal(ct_name_str)
            side_str = "BUY" if p.tradeSide == 1 else "SELL"  # ProtoOATradeSide
            volume = int(getattr(p, "volume", 0) or 0)
            contract_size = ct_name.contract_size if ct_name else 100_000
            entry_price = float(getattr(p, "price", 0) or 0)
            sl = getattr(p, "stopLoss", 0)
            tp = getattr(p, "takeProfit", 0)
            out.append(
                BrokerPosition(
                    position_id=int(p.positionId),
                    symbol_name=ct_name_str,
                    internal_symbol=internal,
                    side=side_str,
                    volume=volume,
                    volume_lots=volume_to_lots(volume, contract_size),
                    entry_price=entry_price,
                    sl_price=float(sl) if sl else None,
                    tp_price=float(tp) if tp else None,
                    label=label,
                )
            )
        return out

    # ─── orders ──────────────────────────────────────────────────────────

    def place_market_order(
        self,
        *,
        internal_symbol: str,
        side: str,
        volume_lots: float,
        sl_price: float | None = None,
        tp_price: float | None = None,
        comment: str = "",
    ) -> OrderResult:
        """Market-ордер с label="ai-fx-trader".

        SL/TP передаются как АБСОЛЮТНЫЕ цены, мы сами конвертируем в
        cTrader-relative (price_diff × 100_000) относительно текущей цены.
        """
        if self._client is None:
            return OrderResult(success=False, error="client not started")
        info = self.get_symbol_info(internal_symbol)
        if info is None:
            return OrderResult(
                success=False,
                error=f"symbol {internal_symbol} not in cTrader catalog",
            )
        side_up = side.upper()
        if side_up not in ("BUY", "SELL"):
            return OrderResult(success=False, error=f"invalid side: {side!r}")

        # volume rounding под cTrader filters
        raw_volume = lots_to_volume(volume_lots, info.contract_size)
        volume = self._clamp_volume(raw_volume, info)
        if volume <= 0:
            return OrderResult(
                success=False,
                error=f"volume <= 0 after clamping (raw={raw_volume}, min={info.min_volume})",
            )
        if volume > raw_volume * 3:
            return OrderResult(
                success=False,
                error=(
                    f"min_volume слишком большой для {internal_symbol}: запрос "
                    f"{raw_volume}, min {info.min_volume} — отказ"
                ),
            )

        # Конвертим SL/TP в relative ИЗ ТЕКУЩЕЙ ЦЕНЫ (cTrader сам
        # пересчитает от fill-цены при SET — relative SL/TP принимается).
        current_price = self.get_current_price(internal_symbol)
        if current_price is None:
            return OrderResult(success=False, error="current price unavailable")

        rel_sl = None
        rel_tp = None
        if sl_price is not None and sl_price > 0:
            rel_sl = price_to_relative(abs(current_price - sl_price))
        if tp_price is not None and tp_price > 0:
            rel_tp = price_to_relative(abs(current_price - tp_price))

        try:
            result = self._client.send_new_order(
                symbol_id=info.symbol_id,
                trade_side=side_up,
                volume=volume,
                relative_stop_loss=rel_sl,
                relative_take_profit=rel_tp,
                comment=comment[:512] if comment else "",
                label=self._settings.order_label,
            )
        except Exception as e:
            log.exception("place_market_order failed: %s %s", internal_symbol, side_up)
            return OrderResult(success=False, error=f"send_new_order: {e}")

        pos = result.position if hasattr(result, "position") else None
        pos_id = int(pos.positionId) if pos else 0
        deal = result.deal if hasattr(result, "deal") else None
        fill_price = 0.0
        if deal and hasattr(deal, "executionPrice") and deal.executionPrice:
            fill_price = float(deal.executionPrice)
        elif pos and hasattr(pos, "price") and pos.price:
            fill_price = float(pos.price)

        return OrderResult(
            success=True,
            broker_position_id=pos_id,
            fill_price=fill_price or current_price,
            volume=volume,
            volume_lots=volume_to_lots(volume, info.contract_size),
        )

    def close_position(self, broker_position_id: int, volume: int) -> OrderResult:
        if self._client is None:
            return OrderResult(success=False, error="client not started")
        try:
            self._client.close_position(broker_position_id, volume)
        except Exception as e:
            log.exception("close_position(%d) failed", broker_position_id)
            return OrderResult(
                success=False,
                broker_position_id=broker_position_id,
                error=f"close_position: {e}",
            )
        return OrderResult(
            success=True,
            broker_position_id=broker_position_id,
            volume=volume,
        )

    # ─── broker-side reconcile (sync DB ↔ cTrader) ───────────────────────

    def get_active_broker_position_ids(self) -> set[int] | None:
        """Set of cTrader positionIds, открытых под нашим label.

        Используется для broker-side reconcile: позиции в нашей БД, не
        присутствующие в этом set'е, закрыты broker'ом сам (SL/TP).

        Returns ``None`` при ошибке (caller отличает от пустого set'а — не
        делает destructive close: правило ``None != []`` из Bybit-агента
        2026-05-07, документация ``client_adapter.get_open_positions``).
        """
        if self._client is None:
            return None
        try:
            resp = self._client.reconcile()
        except Exception:
            log.exception("get_active_broker_position_ids: reconcile failed")
            return None
        out: set[int] = set()
        for p in resp.position:
            label = getattr(p, "label", "") or ""
            if label != self._settings.order_label:
                continue
            out.add(int(p.positionId))
        return out

    def get_closing_deal_for_position(
        self,
        broker_position_id: int,
        lookback_hours: int = 24,
    ) -> dict | None:
        """Закрывающий deal для позиции, через ProtoOADealListReq.

        Returns dict с broker-side точными числами:
        ``{exit_price, gross_pnl_usd, swap_usd, commission_usd, ts_ms,
        deal_id}`` или None если deal не найден.

        Источник: cTrader Open API ``ProtoOAClosePositionDetail`` с
        ``moneyDigits`` scaling. Документация поля:
        https://help.ctrader.com/open-api/model-messages/#protooaclosepositiondetail
        """
        if self._client is None:
            return None
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - max(1, lookback_hours) * 3600 * 1000
        try:
            resp = self._client.get_deal_list(
                from_ts=from_ms, to_ts=now_ms, max_rows=1000,
            )
        except Exception:
            log.exception(
                "get_closing_deal_for_position(%d): get_deal_list failed",
                broker_position_id,
            )
            return None
        for d in resp.deal:
            if int(getattr(d, "positionId", 0)) != broker_position_id:
                continue
            if not d.HasField("closePositionDetail"):
                continue
            cpd = d.closePositionDetail
            md = int(cpd.moneyDigits) if cpd.moneyDigits else 2
            divisor = 10 ** md
            return {
                "deal_id": int(d.dealId),
                "ts_ms": int(getattr(d, "executionTimestamp", 0)),
                "exit_price": float(getattr(d, "executionPrice", 0) or 0),
                "gross_pnl_usd": float(cpd.grossProfit) / divisor,
                "swap_usd": float(cpd.swap) / divisor,
                "commission_usd": float(cpd.commission) / divisor,
            }
        return None

    # ─── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _clamp_volume(volume: int, info: SymbolInfo) -> int:
        """Округлить volume под cTrader step_volume и зажать к [min, max]."""
        step = max(1, info.step_volume)
        rounded = (volume // step) * step
        if rounded < info.min_volume:
            rounded = info.min_volume
        if rounded > info.max_volume:
            rounded = (info.max_volume // step) * step
        return rounded
