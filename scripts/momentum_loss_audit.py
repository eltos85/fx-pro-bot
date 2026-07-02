"""Loss-аудит fx_momentum_bot: КАК и НА ЧЁМ стратегия теряет (read-only).

Источник истины — cTrader deal-list (ctrader-pnl.mdc / stats-collection.mdc),
контекст входа — momentum_decisions (сила сигнала, ATR). Переиспользует
инфраструктуру tradecard_momentum (fetch_momentum_trades + MomentumTrade,
проверенную на weekly-отчётах).

Срезы (все — наблюдения; n<100 → без выводов о стратегии, sample-size.mdc):
1. Периоды: pre-baseline (до 2026-06-26 08:15, старая логика: без
   session-filter/friday-flat, с VP до 06-18) vs post-baseline.
2. Убытки по symbol × side; по FX-сессии входа; по дню недели.
3. Тип выхода (эвристика, deal-list не несёт close_reason):
   - sl_hit:      R ≤ −0.85 (SL на −1R ± слиппедж)
   - beyond_sl:   R ≤ −1.15 (гэп/слиппедж ЗА SL — weekend/news)
   - early_exit:  −0.85 < R < 0 (sign-decay / friday-flat / flip)
   - friday_flat: закрытие в Пт 20:00–20:45 UTC
   - weekend_hold: позиция пережила Сб/Вс
4. Длительность удержания лоссов vs винов; partial-выходы.
5. Cost drag: swap + commission в лоссах.
6. Top-10 худших сделок с полным контекстом.
7. JSON-дамп всех сделок в /data/tradecard/loss_audit_trades.json — для
   локального MFE-анализа (yfinance 5m) без повторного коннекта к брокеру.

Запуск (read-only, в контейнере tradecard-momentum; deal-list + reconcile,
ордера не ставятся):

    docker compose run --rm --entrypoint python tradecard-momentum \
        /scripts/momentum_loss_audit.py 2026-06-05
    # (scripts смонтировать: -v /root/fx-pro-bot/scripts:/scripts)
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%m-%d %H:%M")


def _crossed_weekend(ts_open: float, ts_close: float | None) -> bool:
    if not ts_close:
        return False
    d = datetime.fromtimestamp(ts_open, tz=UTC)
    end = datetime.fromtimestamp(ts_close, tz=UTC)
    while d < end:
        if d.weekday() == 5:  # прошли через субботу
            return True
        d += timedelta(days=1)
    return False


def _exit_kind(t) -> str:
    """Эвристический тип выхода (см. докстринг модуля)."""
    if t.ts_close:
        c = datetime.fromtimestamp(t.ts_close, tz=UTC)
        if c.weekday() == 4 and (20, 0) <= (c.hour, c.minute) < (20, 45):
            return "friday_flat"
    r = t.r_multiple
    if r is None:
        return "unknown_r"
    if r <= -1.15:
        return "beyond_sl"
    if r <= -0.85:
        return "sl_hit"
    if r < 0:
        return "early_exit"
    if r < 0.2:
        return "scratch"
    return "profit_exit"


def _agg(label: str, trades: list) -> None:
    if not trades:
        return
    wins = [t for t in trades if t.net_usd > 0]
    losses = [t for t in trades if t.net_usd < 0]
    net = sum(t.net_usd for t in trades)
    gw = sum(t.net_usd for t in wins)
    gl = -sum(t.net_usd for t in losses)
    pf = gw / gl if gl > 0 else float("inf")
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    avg_r = f"{sum(rs)/len(rs):+.2f}" if rs else "  n/a"
    avg_loss = (-gl / len(losses)) if losses else 0.0
    print(f"  {label:<22} n={len(trades):>3} WR={len(wins)/len(trades)*100:>3.0f}% "
          f"net=${net:>+8.2f} avgR={avg_r}  PF={pf:>5.2f}  "
          f"avg_loss=${avg_loss:>+6.2f}")


def main() -> int:
    since_str = sys.argv[1] if len(sys.argv) > 1 else "2026-06-05"
    since_ts = datetime.strptime(since_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
    until_ts = datetime.now(tz=UTC).timestamp()
    baseline_ts = datetime(2026, 6, 26, 8, 15, tzinfo=UTC).timestamp()

    from tradecard_momentum.config.settings import TradecardMomentumSettings
    from tradecard_momentum.data.broker import fetch_momentum_trades
    from tradecard_momentum.data.momentum_db import MomentumDBReadOnly

    cfg = TradecardMomentumSettings()
    with MomentumDBReadOnly(cfg.momentum_db_path) as db:
        decisions = db.executed_decisions(since_ts=since_ts - 7 * 86400,
                                          until_ts=until_ts)
    trades = fetch_momentum_trades(cfg, since_ts=since_ts, until_ts=until_ts,
                                   decisions=decisions)
    closed = [t for t in trades if t.is_closed]
    print(f"=== momentum LOSS AUDIT {since_str} → now | closed={len(closed)} "
          f"(baseline 2026-06-26 08:15 UTC) ===\n")
    if not closed:
        print("Нет закрытых сделок (или брокер недоступен).")
        return 1

    pre = [t for t in closed if t.ts_open < baseline_ts]
    post = [t for t in closed if t.ts_open >= baseline_ts]

    print("── Периоды (правки 06-26: session-filter + friday-flat) ──")
    _agg("PRE  (старая логика)", pre)
    _agg("POST (текущая логика)", post)
    _agg("TOTAL", closed)

    for name, seg in [("PRE", pre), ("POST", post)]:
        if not seg:
            continue
        print(f"\n────────── сегмент {name} ({len(seg)} сделок) ──────────")

        print("── symbol × side ──")
        keys = sorted({(t.symbol, t.side) for t in seg})
        for sym, side in keys:
            _agg(f"{sym} {side}", [t for t in seg if t.symbol == sym and t.side == side])

        print("── FX-сессия входа ──")
        for s in ("asia", "london", "ny", "late"):
            _agg(s, [t for t in seg if t.session == s])

        print("── день недели входа ──")
        for wd, wname in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
            _agg(wname, [t for t in seg if t.open_dt.weekday() == wd])

        print("── тип выхода (эвристика по R) ──")
        kinds = ["beyond_sl", "sl_hit", "early_exit", "friday_flat",
                 "scratch", "profit_exit", "unknown_r"]
        for k in kinds:
            _agg(k, [t for t in seg if _exit_kind(t) == k])

        print("── weekend-hold / partial ──")
        _agg("weekend_hold", [t for t in seg if _crossed_weekend(t.ts_open, t.ts_close)])
        _agg("partial(>1 close)", [t for t in seg if t.n_closing_deals > 1])

        losses = [t for t in seg if t.net_usd < 0]
        wins = [t for t in seg if t.net_usd > 0]
        if losses:
            dur_l = [((t.ts_close or t.ts_open) - t.ts_open) / 3600 for t in losses]
            dur_w = [((t.ts_close or t.ts_open) - t.ts_open) / 3600 for t in wins] or [0]
            swap = sum(t.swap_usd for t in losses)
            comm = sum(t.commission_usd for t in losses)
            gross_l = sum(t.gross_usd for t in losses)
            print(f"── лоссы: механика ──\n"
                  f"  hold: лоссы avg {sum(dur_l)/len(dur_l):.1f}h "
                  f"(max {max(dur_l):.1f}h) vs вины avg {sum(dur_w)/len(dur_w):.1f}h\n"
                  f"  cost drag в лоссах: gross ${gross_l:+.2f}, "
                  f"swap ${swap:+.2f}, comm ${comm:+.2f} "
                  f"(costs = {abs(swap+comm)/abs(gross_l+swap+comm)*100 if (gross_l+swap+comm) else 0:.0f}% net-лосса)")

    print("\n── TOP-10 худших сделок (весь период) ──")
    print(f"{'pid':<11}{'sym':<8}{'side':<6}{'open':<13}{'close':<13}"
          f"{'hold_h':>6} {'R':>6} {'net$':>8}  {'sess':<7}{'exit':<12}|mom|")
    for t in sorted(closed, key=lambda x: x.net_usd)[:10]:
        hold = ((t.ts_close or t.ts_open) - t.ts_open) / 3600
        r = f"{t.r_multiple:+.2f}" if t.r_multiple is not None else "  n/a"
        m = f"{t.signal_momentum:.4f}" if t.signal_momentum is not None else "n/a"
        print(f"{t.position_id:<11}{t.symbol:<8}{t.side:<6}"
              f"{_fmt_ts(t.ts_open):<13}{_fmt_ts(t.ts_close):<13}"
              f"{hold:>6.1f} {r:>6} {t.net_usd:>+8.2f}  {t.session:<7}"
              f"{_exit_kind(t):<12}{m}")

    dump = [{
        "pid": t.position_id, "symbol": t.symbol, "side": t.side,
        "ts_open": t.ts_open, "ts_close": t.ts_close,
        "entry": t.entry, "exit": t.exit, "volume": t.volume_units,
        "gross": t.gross_usd, "swap": t.swap_usd, "comm": t.commission_usd,
        "net": round(t.net_usd, 4), "r": t.r_multiple,
        "signal_momentum": t.signal_momentum, "signal_atr": t.signal_atr,
        "risk_price": t.risk_price, "n_closings": t.n_closing_deals,
        "session": t.session, "exit_kind": _exit_kind(t),
    } for t in closed]
    out_path = "/data/tradecard/loss_audit_trades.json"
    try:
        with open(out_path, "w") as f:
            json.dump(dump, f, indent=1)
        print(f"\nJSON-дамп: {out_path} ({len(dump)} сделок)")
    except OSError as exc:
        print(f"\nJSON-дамп не записан: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
