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

from flowzone_bot.analysis.strategy import Signal

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


def bracket_exit_reason(side: str, entry: float, exit_price: float | None) -> str:
    """Расщепить биржевой bracket-выход на tp_hit / sl_hit по знаку хода цены."""
    if exit_price is None or entry <= 0:
        return "tp_sl"
    favorable = (exit_price - entry) if side == "long" else (entry - exit_price)
    return "tp_hit" if favorable >= 0 else "sl_hit"


_BRACKET_REASONS = frozenset({"tp_hit", "sl_hit", "tp_sl"})


def reconciled_bracket_reason(old_reason: str | None, net: float) -> str | None:
    if old_reason not in _BRACKET_REASONS:
        return None
    corrected = "tp_hit" if net >= 0 else "sl_hit"
    return corrected if corrected != old_reason else None


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
                ts_open=self._now())
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
            ts_open=self._now())
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

    def manage(self, states: dict) -> None:
        for tr in self._db.open_trades():
            st = states.get(tr.symbol)
            snap = st.snapshot() if st else None
            price = snap.last_price if snap else None
            if tr.mode == "paper":
                self._manage_paper(tr, price)
            else:
                self._manage_live(tr, price)
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
        fallback = getattr(self._cfg, "close_notify_fallback_sec", 10.0)
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
                    reason = reconciled_bracket_reason(
                        getattr(tr, "close_reason", None), net)
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
        reason = reconciled_bracket_reason(getattr(tr, "close_reason", None),
                                           d["pnl"])
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
        reason = reconciled_bracket_reason(getattr(tr, "close_reason", None), net)
        self._db.verify_pnl(tr.id, pnl_usd=net, exit_price=d.get("exit"),
                            close_reason=reason)
        self._rest_recon_attempts.pop(tr.id, None)
        self._verify_fail.pop(tr.id, None)
        if abs(net - old) >= 0.01:
            log.info("true-up #%d %s: WS-net $%.4f → closedPnl $%.4f (Δ$%.4f)",
                     tr.id, tr.symbol, old, net, net - old)
        return True

    def _manage_paper(self, tr, price: float | None) -> None:
        if price is None:
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

    def _manage_live(self, tr, price: float | None) -> None:
        cl = self._client
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
            reason = bracket_exit_reason(tr.side, tr.entry, exitp)
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
        # держим позицию — троттлим лог дистанций до TP/SL
        iv = 15.0
        if price is not None and self._now() - self._hold_log.get(tr.id, 0.0) >= iv:
            self._hold_log[tr.id] = self._now()
            age = self._now() - tr.ts_open
            to_tp = (tr.tp - price) if tr.side == "long" else (price - tr.tp)
            to_sl = (price - tr.sl) if tr.side == "long" else (tr.sl - price)
            play.info("⏱ [%s] держу #%d %s %.0fс | px %.4f | до TP %+.4f, "
                      "до SL %+.4f", tr.symbol, tr.id, tr.side.upper(), age,
                      price, to_tp, to_sl)
