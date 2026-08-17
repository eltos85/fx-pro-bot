"""Исполнитель сделок flowzone_bot.

Два режима (settings.trading_enabled):
- OBSERVE/PAPER (False): ордера НЕ ставятся, сделка симулируется на live-цене,
  TP/SL считаются локально (для наблюдения сигналов без риска).
- LIVE (True, Bybit DEMO): LIMIT вход в зоне (канон §5.1 «put a limit order
  here») с биржевыми SL/TP (стоп ЗА зоной, §5.2, масштаб 1-2-3/4/5), reduce-only
  MARKET выход.

Канон §5.3: полный выход на swing point (TP=ближайший swing). Никакой частичной
фиксации — канон её не описывает (правило no-data-fitting.mdc). Re-entry на
следующей зоне — отдельной новой сделкой (см. reload_cooldown в main/scan).

Размер позиции — риск-базированный: qty = risk_per_trade_usd / |entry−SL|
(Van K. Tharp 2007 ch.11 — размер как следствие стопа). net сделки берём из
приватного WS execution (Σ execPnl − Σ execFee = Bybit closedPnl), REST —
фолбэк для restart-сирот.
"""
from __future__ import annotations

import logging
import math
import time

from flowzone_bot.analysis.orderflow import (big_trade_threshold,
                                             detect_big_trades,
                                             detect_exhaustion)
from flowzone_bot.analysis.strategy import Signal
from flowzone_bot.trading.client import is_expired_api_key

log = logging.getLogger("flowzone_bot.exec")
play = logging.getLogger("flowzone_bot.play")

MAKER_FEE = 0.0002
TAKER_FEE = 0.00055


def _qty_decimals(step: float) -> int:
    if step <= 0:
        return 8
    d = f"{step:.10f}".rstrip("0")
    return len(d.split(".")[1]) if "." in d else 0


def position_size_by_risk(risk_usd: float, entry: float, sl: float, *,
                          min_notional: float = 0.0, qty_step: float = 0.0,
                          min_qty: float = 0.0) -> float:
    """qty из фиксированного $-риска: qty = risk_usd / |entry−sl| (Tharp 2007)."""
    if entry <= 0 or risk_usd <= 0:
        return 0.0
    dist = abs(entry - sl)
    if dist <= 0:
        return 0.0
    qty = risk_usd / dist
    if qty * entry < min_notional:
        qty = min_notional / entry
    if qty_step > 0:
        qty = round(math.floor(qty / qty_step) * qty_step, _qty_decimals(qty_step))
    if min_qty > 0 and qty < min_qty:
        qty = round(min_qty, _qty_decimals(qty_step)) if qty_step > 0 else min_qty
    return qty


def paper_pnl(side: str, entry: float, exit_price: float,
              qty: float) -> tuple[float, float]:
    gross = (exit_price - entry) * qty if side == "long" else (entry - exit_price) * qty
    fees = qty * (entry * MAKER_FEE + exit_price * TAKER_FEE)
    return (gross - fees, fees)


def taker_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    gross = (exit_price - entry) * qty if side == "long" else (entry - exit_price) * qty
    fees = qty * (entry + exit_price) * TAKER_FEE
    return gross - fees


def bracket_exit_reason(side: str, entry: float, exit_price: float | None,
                        sl: float | None = None,
                        tp: float | None = None) -> str:
    """Расщепить биржевой bracket-выход на tp_hit / sl_hit.

    Канон-корректно после BE-lock/trail (видео 39:00), где SL стоит В СТОРОНЕ
    ПРИБЫЛИ (long: SL > entry, short: SL < entry): классифицируем по пересечению
    ``tp`` / ``sl`` уровней, НЕ по знаку (exit−entry) — иначе закрытие по BE-SL
    в малый плюс метится как tp_hit (#489: exit=SL, pnl +0.25, reason=tp_hit).

    При наличии sl/tp:
      long  — exit ≥ tp → tp_hit; exit ≤ sl → sl_hit; иначе ближайший по дистанции.
      short — exit ≤ tp → tp_hit; exit ≥ sl → sl_hit; иначе ближайший по дистанции.
    Без sl/tp — фолбэк на знак (старый behaviour, для совместимости)."""
    if exit_price is None or entry <= 0:
        return "tp_sl"
    if sl is not None and tp is not None and sl > 0 and tp > 0:
        if side == "long":
            if exit_price >= tp:
                return "tp_hit"
            if exit_price <= sl:
                return "sl_hit"
            return ("tp_hit" if (tp - exit_price) <= (exit_price - sl)
                    else "sl_hit")
        else:  # short
            if exit_price <= tp:
                return "tp_hit"
            if exit_price >= sl:
                return "sl_hit"
            return ("tp_hit" if (exit_price - tp) <= (sl - exit_price)
                    else "sl_hit")
    favorable = (exit_price - entry) if side == "long" else (entry - exit_price)
    return "tp_hit" if favorable >= 0 else "sl_hit"


_BRACKET_REASONS = frozenset({"tp_hit", "sl_hit", "tp_sl"})


def _last_swing_price(swings: list, kind: str) -> float | None:
    """Цена последнего подтверждённого swing-экстремума заданного типа
    ('high'|'low') = «предыдущий уровень» канона (аукцион/Break-this-level).
    Последний = max по idx (или ts если idx нет). None если такого нет."""
    cands = [s for s in swings if s.kind == kind]
    if not cands:
        return None

    def _key(s) -> float:
        idx = getattr(s, "idx", None)
        if idx is not None:
            return float(idx)
        ts = getattr(s, "ts", None)
        return float(ts) if ts is not None else 0.0

    return max(cands, key=_key).price


def reconciled_bracket_reason(tr, exit_price: float | None,
                              net: float) -> str | None:
    """При REST-сверке переопределить close_reason по КАНОН-логике
    ``bracket_exit_reason`` (по пересечению sl/tp), НЕ по знаку net.

    Почему не по знаку: после BE-lock/trail (видео 39:00) SL стоит В СТОРОНЕ
    ПРИБЫЛИ (long: SL>entry, short: SL<entry). Закрытие по такому BE/trail-SL
    даёт малый ПОЛОЖИТЕЛЬНЫЙ net → sign-based классификатор метит `tp_hit`,
    хотя по факту это `sl_hit` (E3-баг в пути reconciliation: кейсы #489, #496 —
    exit=SL в малый плюс, close_reason ошибочно tp_hit). Канон-корректно — по
    уровню: long exit≥tp→tp_hit, exit≤sl→sl_hit; short зеркально; иначе ближайший.

    Если sl/tp/exit неизвестны — НЕ переопределяем (держим old_reason из
    WS-закрытия, которое уже классифицировало через ``bracket_exit_reason``)."""
    old = getattr(tr, "close_reason", None)
    if old not in _BRACKET_REASONS:
        return None
    side = getattr(tr, "side", None)
    entry = getattr(tr, "entry", 0.0) or 0.0
    sl = getattr(tr, "sl", None)
    tp = getattr(tr, "tp", None)
    if (exit_price and entry > 0 and sl and tp and sl > 0 and tp > 0
            and side in ("long", "short")):
        return bracket_exit_reason(side, entry, exit_price, sl, tp)
    return None  # не переопределяем по знаку (E3-баг) — держим WS-классификацию


class Executor:
    def __init__(self, db, settings, client=None, *, notifier=None,
                 now=time.time) -> None:
        self._db = db
        self._cfg = settings
        self._client = client
        self._notifier = notifier
        self._now = now
        self._pending: dict[int, dict] = {}
        self._hold_log: dict[int, float] = {}
        self._link2trade: dict[str, int] = {}
        self._fills: dict[int, dict] = {}
        self._close_pending: dict[int, dict] = {}
        self._rest_recon_attempts: dict[int, float] = {}
        self._verify_fail: dict[int, int] = {}  # tid → счётчик неудачных REST-сверок
        self._last_win: dict[str, float] = {}  # символ → ts последнего профита (re-entry)
        self._auth_expired: bool = False

    def auth_expired(self) -> bool:
        """True после Bybit 33004 — новые входы остановлены до рестарта."""
        if self._auth_expired:
            return True
        cl = self._client
        return bool(getattr(cl, "auth_expired", False))

    def _halt_if_expired(self, err: object) -> None:
        if is_expired_api_key(err) or getattr(self._client, "auth_expired", False):
            if not self._auth_expired:
                log.error("Bybit API key expired (33004) — новые входы "
                          "остановлены до смены ключа и рестарта. "
                          "https://bybit-exchange.github.io/docs/v5/error")
            self._auth_expired = True

    def last_win_ts(self, symbol: str) -> float | None:
        """ts последнего ВЫИГРЫШНОГО закрытия по символу (для re-entry §5.3)."""
        return self._last_win.get(symbol)

    def _note_close(self, symbol: str, pnl: float | None) -> None:
        if (pnl or 0.0) > 0:
            self._last_win[symbol] = self._now()

    def _notify(self, text: str) -> None:
        if self._notifier is not None:
            self._notifier.send(text)

    # ─── открытие ────────────────────────────────────────────────────────

    def on_signal(self, sig: Signal) -> int | None:
        cfg = self._cfg
        if self.auth_expired():
            log.info("skip %s %s: api key expired (33004)", sig.symbol, sig.side)
            return None
        qty_step = min_qty = 0.0
        if self._client is not None:
            info = self._client.instrument(sig.symbol)
            if info:
                qty_step, min_qty = info.qty_step, info.min_order_qty
        qty = position_size_by_risk(
            cfg.risk_per_trade_usd, sig.entry_ref, sig.sl_level,
            min_notional=cfg.min_position_usd, qty_step=qty_step, min_qty=min_qty)
        if qty <= 0:
            log.info("skip %s %s: qty=0", sig.symbol, sig.side)
            return None
        reasons = "+".join(sig.reasons)
        risk_usd = qty * abs(sig.entry_ref - sig.sl_level)

        if not cfg.trading_enabled:
            tid = self._db.insert_open(
                symbol=sig.symbol, side=sig.side, qty=qty, entry=sig.entry_ref,
                sl=sig.sl_level, tp=sig.tp_level, score=sig.score,
                reasons=reasons, mode="paper", strategy=sig.strategy,
                ts_open=self._now(), zone_low=sig.zone_low, zone_high=sig.zone_high)
            log.info("PAPER open #%d %s %s qty=%.6f risk=$%.2f entry=%.4f "
                     "sl=%.4f tp=%.4f [%s] score=%d", tid, sig.symbol, sig.side,
                     qty, risk_usd, sig.entry_ref, sig.sl_level, sig.tp_level,
                     reasons, sig.score)
            self._notify(f"📝 PAPER open #{tid} {sig.symbol} {sig.side.upper()} "
                         f"@{sig.entry_ref:.4f} SL {sig.sl_level:.4f} "
                         f"TP {sig.tp_level:.4f} [{reasons}]")
            return tid

        cl = self._client
        side = "Buy" if sig.side == "long" else "Sell"
        cl.set_leverage(sig.symbol, cfg.max_leverage)
        if self.auth_expired():
            self._halt_if_expired("33004")
            log.warning("LIVE entry skipped %s %s: api key expired",
                        sig.symbol, side)
            return None
        link = f"flowzone_{sig.symbol}_{int(self._now() * 1000)}"
        limit_price = cl.round_price(sig.symbol, sig.entry_ref)
        # канон §5.3: полный выход на swing point → биржевой TP = sig.tp_level
        # (единственная цель). SL+TP всегда на бирже — позиция защищена при
        # падении бота. Никакой частичной фиксации.
        exch_tp = sig.tp_level
        # write-ahead: строка БД ДО постановки ордера (детектируемый осиротевший
        # вход вместо «призрака»-позиции без строки).
        tid = self._db.insert_open(
            symbol=sig.symbol, side=sig.side, qty=qty, entry=limit_price,
            sl=sig.sl_level, tp=exch_tp, score=sig.score, reasons=reasons,
            mode="live", strategy=sig.strategy, entry_order_id=link,
            ts_open=self._now(), zone_low=sig.zone_low, zone_high=sig.zone_high)
        self._link2trade[link] = tid
        self._fills[tid] = {"fee": 0.0, "pnl": 0.0, "close_val": 0.0,
                            "close_qty": 0.0, "open_val": 0.0, "open_qty": 0.0}
        otype = sig.entry_order_type or "limit"
        res = cl.place_entry(
            symbol=sig.symbol, side=side, qty=qty, order_link_id=link,
            order_type=otype, limit_price=limit_price,
            sl_price=cl.round_price(sig.symbol, sig.sl_level),
            tp_price=cl.round_price(sig.symbol, exch_tp))
        if not res.get("ok"):
            self._halt_if_expired(res.get("error"))
            self._db.mark_closed(tid, exit_price=limit_price, pnl_usd=0.0,
                                 fees_usd=0.0, close_reason="entry_Rejected",
                                 ts_close=self._now())
            self._forget_trade(tid)
            log.warning("LIVE entry rejected %s %s: %s", sig.symbol, side,
                        res.get("error"))
            return None
        open_text = (f"🟢 open #{tid} {sig.symbol} {sig.side.upper()} "
                     f"@{limit_price:.4f} SL {sig.sl_level:.4f} "
                     f"TP {sig.tp_level:.4f} [{reasons}]")
        self._pending[tid] = {"link": link, "filled": False,
                              "ts": self._now(), "open_text": open_text}
        log.info("LIVE PLACED #%d %s %s qty=%.6f risk=$%.2f @%.4f sl=%.4f "
                 "tp=%.4f [%s]", tid, sig.symbol, side, qty, risk_usd,
                 limit_price, sig.sl_level, sig.tp_level, reasons)
        play.info("📤 [%s] лимитка %s в зоне @%.4f — стоп %.4f за зоной, цель "
                  "%.4f (swing); жду филл", sig.symbol, sig.side.upper(),
                  limit_price, sig.sl_level, sig.tp_level)
        self._notify(f"⏳ #{tid} {sig.symbol} {sig.side.upper()} лимитка @"
                     f"{limit_price:.4f} выставлена в зоне")
        return tid

    # ─── сопровождение ───────────────────────────────────────────────────

    def manage(self, states: dict, swings_by_symbol: dict | None = None) -> None:
        swings_by_symbol = swings_by_symbol or {}
        for tr in self._db.open_trades():
            st = states.get(tr.symbol)
            snap = st.snapshot() if st else None
            swings = swings_by_symbol.get(tr.symbol, [])
            if tr.mode == "paper":
                self._manage_paper(tr, snap, swings)
            else:
                self._manage_live(tr, snap, swings)
        try:
            self.reconcile()
        except Exception:
            log.exception("reconcile failed")

    def ingest_executions(self, rows: list[dict]) -> None:
        entry_dirty: set[int] = set()
        for r in rows or []:
            tid = self._link2trade.get(r.get("orderLinkId", ""))
            if tid is None:
                tid = self._open_trade_for_symbol(r.get("symbol", ""))
            if tid is None:
                continue
            acc = self._fills.setdefault(
                tid, {"fee": 0.0, "pnl": 0.0, "close_val": 0.0, "close_qty": 0.0,
                      "open_val": 0.0, "open_qty": 0.0})
            acc["fee"] += r.get("execFee", 0.0)
            acc["pnl"] += r.get("execPnl", 0.0)
            if r.get("closedSize", 0.0) > 0 or r.get("execPnl", 0.0) != 0.0:
                acc["close_val"] += r.get("execPrice", 0.0) * r.get("execQty", 0.0)
                acc["close_qty"] += r.get("execQty", 0.0)
            else:
                acc["open_val"] += r.get("execPrice", 0.0) * r.get("execQty", 0.0)
                acc["open_qty"] += r.get("execQty", 0.0)
                entry_dirty.add(tid)
        if self._db is None:
            return
        for tid in entry_dirty:
            acc = self._fills.get(tid)
            if not acc or acc.get("open_qty", 0.0) <= 0:
                continue
            avg_entry = acc["open_val"] / acc["open_qty"]
            try:
                self._rebracket(tid, avg_entry)
            except Exception:
                log.exception("rebracket #%d failed", tid)
            try:
                self._db.update_entry(tid, avg_entry)
            except Exception:
                log.exception("update_entry #%d failed", tid)

    _REBRACKET_MIN_REL = 1e-4

    def _rebracket(self, tid: int, avg_entry: float) -> None:
        if self._client is None or avg_entry <= 0:
            return
        tr = next((t for t in self._db.open_trades() if t.id == tid), None)
        if tr is None or tr.mode != "live" or tr.entry <= 0:
            return
        delta = avg_entry - tr.entry
        if abs(delta) / tr.entry < self._REBRACKET_MIN_REL:
            return
        new_sl = self._client.round_price(tr.symbol, tr.sl + delta)
        new_tp = self._client.round_price(tr.symbol, tr.tp + delta)
        res = self._client.set_trading_stop(tr.symbol, sl_price=new_sl,
                                            tp_price=new_tp)
        if not res.get("ok"):
            log.warning("rebracket #%d %s отклонён (%s)", tid, tr.symbol,
                        res.get("error"))
            return
        self._db.update_levels(tid, sl=new_sl, tp=new_tp)

    def _open_trade_for_symbol(self, symbol: str) -> int | None:
        if not symbol:
            return None
        for tr in self._db.open_trades():
            if tr.symbol == symbol and tr.mode == "live":
                return tr.id
        return None

    def _realized_from_fills(self, tr) -> tuple[float, float | None, bool]:
        acc = self._fills.get(tr.id)
        if not acc or acc["close_qty"] <= 0:
            return (0.0, None, False)
        net = acc["pnl"] - acc["fee"]
        exit_px = acc["close_val"] / acc["close_qty"]
        complete = acc["close_qty"] >= tr.qty * 0.98
        return (net, exit_px, complete)

    def _realized_or_estimate(self, tr, exit_price: float
                              ) -> tuple[float, float, bool]:
        net, exit_px, complete = self._realized_from_fills(tr)
        if complete:
            return (net, exit_px if exit_px is not None else exit_price, True)
        # incomplete: оценка provisional. Учитываем уже зафиксированную долю
        # (частичная фиксация на цели 1) из реальных филлов, а taker-оценку
        # считаем ТОЛЬКО на остаток позиции — иначе на партиалах полный объём
        # по финальной (более выгодной) цене завышает net.
        acc = self._fills.get(tr.id) or {}
        realized = acc.get("pnl", 0.0) - acc.get("fee", 0.0)
        remaining = max(tr.qty - acc.get("close_qty", 0.0), 0.0)
        pnl = realized + taker_pnl(tr.side, tr.entry, exit_price, remaining)
        return (pnl, exit_price, False)

    def _forget_trade(self, tid: int) -> None:
        self._fills.pop(tid, None)
        self._close_pending.pop(tid, None)
        for link in [k for k, v in self._link2trade.items() if v == tid]:
            self._link2trade.pop(link, None)

    def _on_close(self, tr, pnl: float, reason: str, is_real: bool) -> None:
        res = "профит" if (pnl or 0.0) >= 0 else "убыток"
        self._note_close(tr.symbol, pnl)
        play.info("🏁 [%s] закрыл #%d %s: %s, pnl=$%.2f", tr.symbol, tr.id,
                  tr.side.upper(), res, pnl or 0.0)
        if is_real:
            self._send_close_msg(tr.id, tr.symbol, pnl or 0.0, reason)
        else:
            self._close_pending[tr.id] = {"ts": self._now(), "label": reason,
                                          "symbol": tr.symbol}

    def _send_close_msg(self, tid: int, symbol: str, pnl: float, label: str,
                        approx: bool = False) -> None:
        emoji = "✅" if pnl >= 0 else "🔴"
        mark = "≈" if approx else ""
        self._notify(f"{emoji} close #{tid} {symbol} pnl={mark}${pnl:.2f} ({label})")

    # макс. попыток REST-сверки до сдачи (неоднозначные сделки того же символа+
    # entry не разделимы по closedSize → не зацикливаем бюджет; оставляем WS-net).
    _VERIFY_MAX_FAILS = 3

    def reconcile(self) -> None:
        now = self._now()
        ws_horizon = now - 600.0
        fallback = getattr(self._cfg, "close_notify_fallback_sec", 30.0)
        rest_horizon = 7 * 24 * 3600 - 3600
        rest_grace = 60.0
        rest_retry = 300.0
        rest_budget = 3
        # 1) provisional: быстрый WS-финал (полные филлы) или REST-фолбэк
        for tr in self._db.provisional_closed_since(now - rest_horizon):
            ts_close = getattr(tr, "ts_close", None) or now
            if ts_close >= ws_horizon:
                net, exit_px, complete = self._realized_from_fills(tr)
                if complete:
                    reason = reconciled_bracket_reason(tr, exit_px, net)
                    # WS-net снимает provisional, но НЕ verified — REST-true-up
                    # (цикл 2) досверит против closedPnl (ловит дрейф комиссий).
                    self._db.finalize_pnl(tr.id, pnl_usd=net, exit_price=exit_px,
                                          close_reason=reason)
                    pend = self._close_pending.pop(tr.id, None)
                    if pend is not None:
                        self._send_close_msg(tr.id, pend["symbol"], net,
                                             pend["label"])
                    self._rest_recon_attempts.pop(tr.id, None)
                    self._forget_trade(tr.id)
                    continue
            age = now - ts_close
            if (self._client is not None and rest_budget > 0
                    and age >= rest_grace
                    and now - self._rest_recon_attempts.get(tr.id, 0.0) >= rest_retry):
                self._rest_recon_attempts[tr.id] = now
                rest_budget -= 1
                if self._rest_finalize(tr, ts_close):
                    continue
            pend = self._close_pending.get(tr.id)
            if pend is not None and now - pend["ts"] > fallback:
                self._send_close_msg(tr.id, pend["symbol"], tr.pnl_usd or 0.0,
                                     pend["label"], approx=True)
                self._close_pending.pop(tr.id, None)
        # 2) универсальный true-up: ВСЕ закрытые live, ещё не сверённые с биржей
        # (verified=0), включая WS-финализированные. Канон: REST closedPnl —
        # единственный источник правды (офдок close-pnl: closedPnl уже net).
        for tr in self._db.unverified_closed_live_since(now - rest_horizon):
            if tr.pnl_provisional:
                continue  # provisional обрабатывает цикл выше
            if rest_budget <= 0:
                break
            ts_close = getattr(tr, "ts_close", None) or now
            age = now - ts_close
            if (self._client is None or age < rest_grace
                    or now - self._rest_recon_attempts.get(tr.id, 0.0) < rest_retry):
                continue
            self._rest_recon_attempts[tr.id] = now
            rest_budget -= 1
            self._rest_verify(tr, ts_close)

    def _fetch_closed_pnl(self, tr, ts_close: float) -> dict | None:
        """Биржевой closedPnl сделки: точечный матч (одиночное закрытие) →
        фолбэк на сумму по позиции (партиалы, §5.3). None если не сматчилось."""
        if self._client is None:
            return None
        window = 180.0
        ts_open = getattr(tr, "ts_open", None) or ts_close
        near = int(ts_close * 1000)
        since_ms = int((ts_open - 60.0) * 1000)
        until_ms = int((ts_close + window) * 1000)
        entry = getattr(tr, "entry", None)
        try:
            d = self._client.closed_pnl_detail(
                tr.symbol, qty=tr.qty, entry_price=entry,
                near_ms=near, since_ms=since_ms, until_ms=until_ms)
            if (not d or d.get("pnl") is None) and tr.qty > 0:
                d = self._client.closed_pnl_position(
                    tr.symbol, qty=tr.qty, entry_price=entry,
                    since_ms=since_ms, until_ms=until_ms)
        except Exception:
            log.exception("reconcile REST #%d %s failed", tr.id, tr.symbol)
            return None
        return d

    def _rest_finalize(self, tr, ts_close: float) -> bool:
        """REST-сведение provisional-сделки: closedPnl авторитетен → verify."""
        d = self._fetch_closed_pnl(tr, ts_close)
        if not d or d.get("pnl") is None:
            return False
        reason = reconciled_bracket_reason(tr, d.get("exit"), d["pnl"])
        self._db.verify_pnl(tr.id, pnl_usd=d["pnl"], exit_price=d.get("exit"),
                            close_reason=reason)
        pend = self._close_pending.pop(tr.id, None)
        if pend is not None:
            self._send_close_msg(tr.id, pend["symbol"], d["pnl"], pend["label"])
        self._rest_recon_attempts.pop(tr.id, None)
        self._verify_fail.pop(tr.id, None)
        self._forget_trade(tr.id)
        return True

    def _rest_verify(self, tr, ts_close: float) -> bool:
        """True-up уже финализированной (WS) сделки против биржевого closedPnl:
        чинит дрейф недосчитанных комиссий и помечает verified. Неоднозначные
        (несколько сделок того же символа+entry) после _VERIFY_MAX_FAILS попыток
        принимаем как есть (WS-net), чтобы не жечь бюджет вечными ретраями."""
        d = self._fetch_closed_pnl(tr, ts_close)
        if not d or d.get("pnl") is None:
            fails = self._verify_fail.get(tr.id, 0) + 1
            self._verify_fail[tr.id] = fails
            if fails >= self._VERIFY_MAX_FAILS:
                self._db.verify_pnl(tr.id, pnl_usd=tr.pnl_usd or 0.0)
                self._verify_fail.pop(tr.id, None)
                self._rest_recon_attempts.pop(tr.id, None)
                log.info("true-up #%d %s: REST не сматчился ×%d — оставляю WS-net "
                         "$%.4f (verified)", tr.id, tr.symbol, fails,
                         tr.pnl_usd or 0.0)
            return False
        net = d["pnl"]
        old = tr.pnl_usd or 0.0
        reason = reconciled_bracket_reason(tr, d.get("exit"), net)
        self._db.verify_pnl(tr.id, pnl_usd=net, exit_price=d.get("exit"),
                            close_reason=reason)
        self._rest_recon_attempts.pop(tr.id, None)
        self._verify_fail.pop(tr.id, None)
        if abs(net - old) >= 0.01:
            log.info("true-up #%d %s: WS-net $%.4f → closedPnl $%.4f (Δ$%.4f)",
                     tr.id, tr.symbol, old, net, net - old)
        return True

    # ─── Trade Management: BE-lock + trail (канон Fabervaale, видео 39:00) ──
    # Канон (полный транскрипт 39:00): «when you BREAK THIS LEVEL, put your stop
    # loss to break even... after breaking out of complete absorption and you
    # have an amazing explosion where you can TRAIL your position following the
    # aggression of the market. This one print a new one, you bring your stop
    # loss here and you continue.» + tradezella: «If CVD shows strong pressure,
    # move stop to BE early» + forex.in.rs: «Trail to the LAST absorption, never
    # re-widen a stop.»
    #
    # Стадия 1 (BE-lock): SL → entry±buf при пробое предыдущего swing-уровня в
    # сторону сделки + CVD-pressure. Стадия 2 (trail): SL едет за последним
    # absorption-принтом контр-стороны в стороне сделки. Idempotency: persisted
    # tr.sl — ключ (executor rebuilds tr from DB each cycle).

    def _be_sl(self, tr) -> float | None:
        """BE-уровень = entry ± anti-flicker буфер (sl_buffer_bps). Покрывает
        round-trip fees, чтобы BE не стал микро-убытком."""
        if tr.entry <= 0:
            return None
        buf = self._cfg.sl_buffer_bps / 10000.0 * tr.entry
        return tr.entry + buf if tr.side == "long" else tr.entry - buf

    def _maybe_be_lock(self, tr, price: float | None,
                       swings: list, trades: list) -> None:
        """Стадия 1: вынос SL в BE (канон «when you break this level»).

        Триггер — ПРОБОЙ swing-уровня, подтверждённого ПОСЛЕ входа, в стороне
        сделки между entry и TP (long: price > post-entry swing high; short:
        price < post-entry swing low) + CVD-pressure в окне доминирует в сторону
        сделки (tradezella «If CVD shows strong pressure»). Пред-entry swing не
        используется: ближайший из них по направлению сделки — это сама TP-цель
        (тот же набор M5-фракталов, что у nearest_swing_target) → триггер
        совпадал бы с TP. НЕ [НАШЕ] «favourable ≥ N×zone_width» — то срабатывало
        слишком рано и обрезало wins на откате к entry (кейсы #488/#489/#492)."""
        if not self._cfg.be_lock_enabled or price is None:
            return
        if not getattr(self._cfg, "be_lock_break_structure", True) or not swings:
            return  # канон: BE по структурному пробою; нет swing-данных — не BE
        # канон «break this level» / «this one print a new one»: уровень для
        # пробоя — swing, ПОДТВЕРЖДЁННЫЙ ПОСЛЕ входа (s.ts > ts_open), в стороне
        # сделки МЕЖДУ entry и TP. Пред-entry фракталы не годятся: ближайший
        # из них в сторону сделки — это сама swing-цель (nearest_swing_target),
        # т.е. пробой = момент исполнения TP → BE вырождался в no-op. Рынок
        # после входа печатает новый уровень — его пробой и есть канон-триггер.
        ts_open = getattr(tr, "ts_open", 0.0) or 0.0
        fresh = [s for s in swings
                 if getattr(s, "ts", 0.0) and s.ts > ts_open]
        if tr.side == "long":
            fresh = [s for s in fresh
                     if s.kind == "high" and tr.entry < s.price < tr.tp]
            hi = _last_swing_price(fresh, "high")
            if hi is None or price <= hi:
                return
        else:
            fresh = [s for s in fresh
                     if s.kind == "low" and tr.tp < s.price < tr.entry]
            lo = _last_swing_price(fresh, "low")
            if lo is None or price >= lo:
                return
        # CVD-pressure gate (tradezella): доминирует сторона сделки в окне
        if getattr(self._cfg, "be_lock_cvd_gate", True) and trades:
            buy_vol = sum(t.size for t in trades if t.side.upper() == "BUY")
            sell_vol = sum(t.size for t in trades if t.side.upper() == "SELL")
            if tr.side == "long" and buy_vol <= sell_vol:
                return
            if tr.side == "short" and sell_vol <= buy_vol:
                return
        be_sl = self._be_sl(tr)
        if be_sl is None:
            return
        # BE только улучшает защиту: long → выше текущего SL, short → ниже.
        if tr.side == "long" and be_sl <= tr.sl:
            return
        if tr.side == "short" and be_sl >= tr.sl:
            return
        if tr.mode == "live" and self._client is not None:
            new_sl = self._client.round_price(tr.symbol, be_sl)
            if tr.side == "long" and new_sl <= tr.sl:
                return  # округление съело улучшение → no-op
            if tr.side == "short" and new_sl >= tr.sl:
                return
            res = self._client.set_trading_stop(
                tr.symbol, sl_price=new_sl, tp_price=tr.tp)
            if not res.get("ok") and not res.get("no_op"):
                log.warning("be-lock #%d %s отклонён (%s)", tr.id, tr.symbol,
                            res.get("error"))
                return
            self._db.update_levels(tr.id, sl=new_sl, tp=tr.tp)
            tr.sl = new_sl  # in-memory консистентность в рамках цикла
            play.info("🔒 [%s] be-lock #%d %s: SL→BE %.4f (пробой swing-уровня, "
                      "risk-free; биржевой TP %.4f сохранён)", tr.symbol, tr.id,
                      tr.side.upper(), new_sl, tr.tp)
        else:
            self._db.update_levels(tr.id, sl=be_sl, tp=tr.tp)
            tr.sl = be_sl  # paper: close-детекция в этом же цикле видит BE
            play.info("🔒 [%s] PAPER be-lock #%d %s: SL→BE %.4f", tr.symbol,
                      tr.id, tr.side.upper(), be_sl)

    def _sl_in_be_or_beyond(self, tr) -> bool:
        """SL уже подтянут к BE/дальше в сторону сделки (стадия 1 отработала).
        long: initial SL < entry; после BE SL ≥ entry. short: зеркально."""
        if tr.entry <= 0:
            return False
        return (tr.sl >= tr.entry) if tr.side == "long" else (tr.sl <= tr.entry)

    def _maybe_trail(self, tr, snap) -> None:
        """Стадия 2: trail SL за последним absorption-принтом контр-стороны в
        стороне сделки (канон «this print a new one, you bring your stop loss
        here and you continue»). Только после BE (стадия 1). SL двигается
        ТОЛЬКО в сторону сделки (never re-widen, forex.in.rs)."""
        if not getattr(self._cfg, "trail_enabled", True) or snap is None:
            return
        if not self._sl_in_be_or_beyond(tr):
            return  # стадия 1 (BE) ещё не отработала
        price = snap.last_price
        if price is None:
            return
        cut = snap.ts - getattr(self._cfg, "trail_window_sec",
                                self._cfg.absorption_window_sec)
        window = [t for t in snap.trades if t.ts >= cut]
        if not window:
            return
        big_thr = big_trade_threshold(
            snap.trades, pct=self._cfg.big_trade_pct,
            min_samples=self._cfg.big_trade_min_samples)
        if big_thr is None:
            return
        # контр-сторона для trail = пытается развернуть цену против сделки:
        # long — deep SELL (продавцы агрессировали вниз, поглощены = поддержка);
        # short — deep BUY (покупатели вверх, поглощены = сопротивление).
        counter = "Sell" if tr.side == "long" else "Buy"
        big = detect_big_trades(window, big_thr, side=counter)
        if not big:
            return
        # SL ставится ЗА absorption-уровнем (long: чуть НИЖЕ поддержки, short:
        # чуть ВЫШЕ сопротивления) — та же конвенция, что стоп «за зоной» при
        # входе (§5.2 «protecting yourself above/below the area»). Буфер внутрь
        # уровня выбивал бы позицию на обычном ретесте ещё не сломанного уровня.
        buf = self._cfg.sl_buffer_bps / 10000.0 * tr.entry
        if tr.side == "long":
            cands = [t for t in big if t.price < price]
            if not cands:
                return
            anchor = max(cands, key=lambda t: t.price)  # верхний deep-sell под ценой
            new_sl = anchor.price - buf
            if new_sl <= tr.sl:
                return  # never re-widen + only towards deal
        else:
            cands = [t for t in big if t.price > price]
            if not cands:
                return
            anchor = min(cands, key=lambda t: t.price)  # нижний deep-buy над ценой
            new_sl = anchor.price + buf
            if new_sl >= tr.sl:
                return
        if tr.mode == "live" and self._client is not None:
            new_sl = self._client.round_price(tr.symbol, new_sl)
            if tr.side == "long" and new_sl <= tr.sl:
                return
            if tr.side == "short" and new_sl >= tr.sl:
                return
            res = self._client.set_trading_stop(
                tr.symbol, sl_price=new_sl, tp_price=tr.tp)
            if not res.get("ok") and not res.get("no_op"):
                log.warning("trail #%d %s отклонён (%s)", tr.id, tr.symbol,
                            res.get("error"))
                return
            self._db.update_levels(tr.id, sl=new_sl, tp=tr.tp)
            tr.sl = new_sl
            play.info("🔁 [%s] trail #%d %s: SL→%.4f (за absorption-принтом; "
                      "TP %.4f сохранён)", tr.symbol, tr.id, tr.side.upper(),
                      new_sl, tr.tp)
        else:
            self._db.update_levels(tr.id, sl=new_sl, tp=tr.tp)
            tr.sl = new_sl
            play.info("🔁 [%s] PAPER trail #%d %s: SL→%.4f", tr.symbol, tr.id,
                      tr.side.upper(), new_sl)

    # ─── Стадия 3: фиксация по exhaustion (канон «My Signature Orderflow
    # Model» 06:04) ─────────────────────────────────────────────────────────
    # *«when you see an opposite area on the buy side, you can understand that
    # now this selling pressure is almost exhausted… it's not worth to risk all
    # this profit that I make just to make an additional small movement by
    # risking all this profit. So I take out my position»*. Повторный вход, если
    # движение продолжится, канон делает отдельной сделкой — у нас это
    # reload_cooldown_sec в main._scan_signals.

    def _exhaustion_exit_ready(self, tr, snap) -> bool:
        """Выдохлось ли движение В НАШУ сторону при позиции в плюсе.

        Канон фиксирует прибыль именно на затухании собственного движения, а не
        на любом откате: exhaustion = падающий объём + встречная агрессия на
        экстремуме. Позиция в минусе не фиксируется — там работает стоп.
        """
        if not getattr(self._cfg, "initiative_exhaustion_enabled", False):
            return False
        if snap is None or snap.last_price is None or tr.entry <= 0:
            return False
        price = snap.last_price
        in_profit = (price > tr.entry) if tr.side == "long" else (price < tr.entry)
        if not in_profit:
            return False
        cut = snap.ts - getattr(self._cfg, "exhaustion_window_sec",
                                self._cfg.absorption_window_sec)
        window = [t for t in snap.trades if t.ts >= cut]
        move_dir = "up" if tr.side == "long" else "down"
        return detect_exhaustion(
            window, move_dir,
            min_decay=self._cfg.exhaustion_min_decay,
            min_contrarian_frac=self._cfg.exhaustion_min_contrarian_frac,
        ).confirmed

    def _maybe_exhaustion_exit(self, tr, snap) -> bool:
        """Закрыть прибыльную позицию на exhaustion. True — позиция закрыта."""
        if not self._exhaustion_exit_ready(tr, snap):
            return False
        price = snap.last_price
        if tr.mode == "paper":
            pnl, fees = paper_pnl(tr.side, tr.entry, price, tr.qty)
            self._db.mark_closed(tr.id, exit_price=price, pnl_usd=pnl,
                                 fees_usd=fees, close_reason="exhaustion_exit",
                                 ts_close=self._now())
            self._note_close(tr.symbol, pnl)
            play.info("🟡 [%s] PAPER exhaustion-exit #%d %s @%.4f pnl=%.2f",
                      tr.symbol, tr.id, tr.side.upper(), price, pnl)
            self._notify(f"🟡 PAPER close #{tr.id} {tr.symbol} pnl=${pnl:.2f} "
                         f"(exhaustion_exit)")
            return True
        side = "Buy" if tr.side == "long" else "Sell"
        link = f"fz{tr.id}x{int(self._now())}"
        res = self._client.close_market(tr.symbol, side, tr.qty, link)
        if not res.get("ok"):
            log.warning("exhaustion-exit #%d %s отклонён (%s)", tr.id,
                        tr.symbol, res.get("error"))
            return False
        self._link2trade[link] = tr.id
        play.info("🟡 [%s] exhaustion-exit #%d %s @%.4f — движение выдохлось, "
                  "фиксирую прибыль (канон 06:04)", tr.symbol, tr.id,
                  tr.side.upper(), price)
        return True

    def _manage_paper(self, tr, snap, swings: list) -> None:
        price = snap.last_price if snap else None
        if price is None:
            return
        trades = snap.trades if snap else []
        self._maybe_be_lock(tr, price, swings, trades)  # стадия 1: BE
        self._maybe_trail(tr, snap)                      # стадия 2: trail
        if self._maybe_exhaustion_exit(tr, snap):        # стадия 3: exhaustion
            return
        hit_tp = price >= tr.tp if tr.side == "long" else price <= tr.tp
        hit_sl = price <= tr.sl if tr.side == "long" else price >= tr.sl
        if not (hit_tp or hit_sl):
            return
        reason, exit_px = ("sl", tr.sl) if hit_sl else ("tp", tr.tp)
        pnl, fees = paper_pnl(tr.side, tr.entry, exit_px, tr.qty)
        self._db.mark_closed(tr.id, exit_price=exit_px, pnl_usd=pnl, fees_usd=fees,
                             close_reason=reason, ts_close=self._now())
        self._note_close(tr.symbol, pnl)
        log.info("PAPER close #%d %s @%.4f pnl=%.4f (%s)", tr.id, tr.symbol,
                 exit_px, pnl, reason)
        emoji = "✅" if pnl >= 0 else "🔴"
        self._notify(f"{emoji} PAPER close #{tr.id} {tr.symbol} pnl=${pnl:.2f} "
                     f"({reason})")

    def _manage_live(self, tr, snap, swings: list) -> None:
        cl = self._client
        price = snap.last_price if snap else None
        pend = self._pending.get(tr.id)
        if pend and not pend["filled"]:
            status = cl.order_status(tr.symbol, pend["link"])
            if status in ("Filled", "PartiallyFilled"):
                pend["filled"] = True
                pend["ts"] = self._now()
                play.info("✅ [%s] филл #%d %s @%.4f — слежу за TP %.4f / SL %.4f",
                          tr.symbol, tr.id, tr.side.upper(), tr.entry, tr.tp, tr.sl)
                self._notify(pend.get("open_text", f"🟢 open #{tr.id} {tr.symbol}"))
                return
            if status in ("Cancelled", "Rejected", "Deactivated"):
                self._db.mark_closed(tr.id, exit_price=tr.entry, pnl_usd=0.0,
                                     fees_usd=0.0, close_reason=f"entry_{status}",
                                     ts_close=self._now())
                self._pending.pop(tr.id, None)
                self._forget_trade(tr.id)
                return
            if self._now() - pend["ts"] > self._cfg.entry_fill_timeout_sec:
                cl.cancel_order(tr.symbol, pend["link"])
                self._db.mark_closed(tr.id, exit_price=tr.entry, pnl_usd=0.0,
                                     fees_usd=0.0, close_reason="entry_timeout",
                                     ts_close=self._now())
                self._pending.pop(tr.id, None)
                self._forget_trade(tr.id)
                play.info("⌛ [%s] лимитка #%d не исполнилась за %.0fс — снимаю",
                          tr.symbol, tr.id, self._cfg.entry_fill_timeout_sec)
            return
        pos = cl.get_position(tr.symbol)
        if pos is None:
            return
        if pos.size <= 0:
            pnl, exitp, is_real = self._realized_or_estimate(
                tr, pos.mark_price or tr.entry)
            reason = bracket_exit_reason(tr.side, tr.entry, exitp,
                                         sl=tr.sl, tp=tr.tp)
            self._db.mark_closed(tr.id, exit_price=exitp, pnl_usd=pnl,
                                 fees_usd=0.0, close_reason=reason,
                                 ts_close=self._now(), provisional=not is_real)
            if is_real:
                self._forget_trade(tr.id)
            self._pending.pop(tr.id, None)
            self._hold_log.pop(tr.id, None)
            log.info("LIVE close #%d %s pnl=%.4f (биржа %s)", tr.id, tr.symbol,
                     pnl or 0.0, reason)
            self._on_close(tr, pnl, reason, is_real)
            return
        # держим позицию — стадии 1-2 Trade Management (канон 39:00), затем
        # троттлим лог дистанций до TP/SL
        self._maybe_be_lock(tr, price, swings, snap.trades if snap else [])
        self._maybe_trail(tr, snap)
        if self._maybe_exhaustion_exit(tr, snap):        # стадия 3: exhaustion
            return
        iv = 15.0
        if price is not None and self._now() - self._hold_log.get(tr.id, 0.0) >= iv:
            self._hold_log[tr.id] = self._now()
            age = self._now() - tr.ts_open
            to_tp = (tr.tp - price) if tr.side == "long" else (price - tr.tp)
            to_sl = (price - tr.sl) if tr.side == "long" else (tr.sl - price)
            play.info("⏱ [%s] держу #%d %s %.0fс | px %.4f | до TP %+.4f, "
                      "до SL %+.4f", tr.symbol, tr.id, tr.side.upper(), age,
                      price, to_tp, to_sl)
