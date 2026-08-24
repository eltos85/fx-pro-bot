"""Аудит живого `hybrid_bot`: считает гейты §8 канона по данным биржи.

Read-only. Отличие от `hybrid_contour_pnl.py`: тот мерил *непреднамеренный*
контур на общем счёте двух ботов (нужна была атрибуция ног по orderLinkId).
Здесь счёт изолированный (STRATEGY_HYBRID.md §18.4) — все ноги наши, поэтому
книга сходится тривиально, а вопрос другой: **обгоняет ли регулярная фиксация
простое удержание** и **сходится ли учёт бота с биржей**.

Источник правды — биржа (`stats-collection.mdc`): `closedPnl` уже net,
комиссии внутри. Локальная БД используется только для traceability: из неё
берётся причина закрытия (`fix_threshold` / `trend_flat` / `broker_flat`),
которой в API нет. Деньги из БД в выводы не идут, они сверяются с биржей.

Что считается:
  * события фиксации: якорь, лок, фактическое расстояние против заявленного
    порога (разница = проскальзывание рыночного выхода — этого в §17.6 не
    было, там заявка исполнялась по максимуму свечи);
  * бенчмарк «просто держать первый лот» с той же комиссией и funding;
  * расхождение учёта бота с биржей (гейт §8.4);
  * прогресс по выборке против порогов `sample-size.mdc` (гейт §8.1);
  * форвард §16.2: текущая вола и ставка следующего входа (не размер
    уже открытого лота — тот не пересчитывается).

Запуск (контейнер бота — там ключи HYBRID_BYBIT_* и pybit):
    ssh root@204.168.149.140 \
      "docker exec -i fx-pro-bot-hybrid-bot-1 python3 - --days 7 --snapshot" \
      < scripts/hybrid_live_audit.py

Docs:
  https://bybit-exchange.github.io/docs/v5/position/close-pnl
  https://bybit-exchange.github.io/docs/v5/order/execution
  Оба эндпоинта: окно endTime-startTime <= 7 дней, limit [1,100], cursor.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from pybit.unified_trading import HTTP

SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000

# Bybit linear без VIP. Нужна только для бенчмарка холда: у него одна нога,
# фактической комиссии за неё в истории нет.
# https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate
TAKER_FEE = 0.00055

# Консервативный троттлинг, ниже лимитов API.
# https://bybit-exchange.github.io/docs/v5/rate-limit
THROTTLE_SEC = 0.2

# Funding приходит в ту же историю исполнений, но это не сделка.
# https://bybit-exchange.github.io/docs/v5/enum#exectype
TRADE_EXEC_TYPES = {"Trade", "AdlTrade", "BustTrade", "Delivery"}
FUNDING_EXEC_TYPES = {"Funding"}

# Пороги выборки для гейта §8.1 (sample-size.mdc).
GATE_MIN_EVENTS = 100
GATE_MIN_DAYS = 14

# Допуск при сопоставлении сделки из БД с реализацией на бирже. Бот пишет строку
# сразу после ордера, биржа проставляет своё время закрытия — расхождение
# секундное, но берём с запасом на цикл бота (3 мин).
MATCH_WINDOW_SEC = 300

# Наши ордера помечены этим префиксом в orderLinkId. Счёт достался от
# предшественника вместе с его историей торгов (он торговал там до 2026-08-20),
# поэтому считать «всё, что было на счёте» нельзя — только свои ноги.
OUR_PREFIX = "hybrid_"

SNAPSHOT_FIELDS = [
    "ts_utc", "window_days", "symbol",
    "n_events", "n_fix", "n_trend_exit", "obs_days",
    "realized_net", "fees_legs", "funding_paid", "n_legs",
    "pos_size", "pos_avg", "mark", "unrealized", "strategy_total",
    "hold_qty", "hold_entry", "hold_total", "delta_vs_hold",
    "fix_gross", "trend_exit_gross",
    "declared_threshold", "median_fix_dist", "median_slip",
    "db_net", "db_delta", "foreign_legs", "skipped_closes",
    "vol_annual", "next_stake",
]


def _ms_to_utc(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _fnum(raw: object, default: float = 0.0) -> float:
    try:
        if raw in (None, ""):
            return default
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    srt = sorted(xs)
    mid = len(srt) // 2
    if len(srt) % 2:
        return srt[mid]
    return (srt[mid - 1] + srt[mid]) / 2.0


def _paginate(method, *, start_ms: int, end_ms: int, **params) -> list[dict]:
    """Полный обход с cursor по окнам <= 7 дней (требование обоих эндпоинтов)."""
    rows: list[dict] = []
    win_start = start_ms
    while win_start < end_ms:
        win_end = min(win_start + SEVEN_DAYS_MS, end_ms)
        cursor = ""
        while True:
            resp = method(startTime=win_start, endTime=win_end,
                          limit=100, cursor=cursor, **params)
            if resp.get("retCode") != 0:
                raise RuntimeError(f"Bybit: {resp.get('retMsg')}")
            result = resp.get("result") or {}
            rows.extend(result.get("list") or [])
            cursor = result.get("nextPageCursor") or ""
            time.sleep(THROTTLE_SEC)
            if not cursor:
                break
        win_start = win_end
    return rows


def db_reasons(path: str, symbol: str, since_sec: float) -> list[dict]:
    """Причины закрытий из БД бота. Только traceability, деньги не берём."""
    if not path or not os.path.exists(path):
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT ts_close, qty, entry, exit, pnl_usd, close_reason "
            "FROM trades WHERE status='closed' AND symbol=? AND ts_close>=? "
            "ORDER BY ts_close", (symbol, since_sec)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    keys = ("ts_close", "qty", "entry", "exit", "pnl_usd", "reason")
    return [dict(zip(keys, r)) for r in rows]


def match_reason(close_ms: int, qty: float, rows: list[dict]) -> str:
    """Причина закрытия для реализации биржи: ближайшая по времени запись БД.

    Сопоставление по времени, а не по объёму: объём у всех фиксаций одинаковый,
    поэтому различать записи он не помогает. Объём служит только проверкой.
    """
    best: tuple[float, dict] | None = None
    for r in rows:
        gap = abs(r["ts_close"] - close_ms / 1000.0)
        if gap > MATCH_WINDOW_SEC:
            continue
        if qty > 0 and abs(r["qty"] - qty) > max(1e-8, qty * 0.01):
            continue
        if best is None or gap < best[0]:
            best = (gap, r)
    return best[1]["reason"] if best else "?"


def split_ours(fills: list[dict], closes: list[dict], *,
               prefix: str = OUR_PREFIX) -> tuple[list[dict], list[dict],
                                                  list[dict], int]:
    """Делит ноги и реализации на свои и чужие.

    В `closed-pnl` нет `orderLinkId`, поэтому принадлежность реализации
    определяется через `orderId` её ноги. Реализация без известной ноги (нога
    осталась за границей окна) считается чужой: лучше недосчитать своё, чем
    приписать себе чужой результат.
    """
    ours, foreign = [], []
    for f in fills:
        (ours if (f.get("orderLinkId") or "").startswith(prefix)
         else foreign).append(f)
    our_orders = {f.get("orderId") for f in ours if f.get("orderId")}
    our_closes = [c for c in closes if c.get("orderId") in our_orders]
    skipped = len(closes) - len(our_closes)
    return ours, foreign, our_closes, skipped


def hold_benchmark(fills: list[dict], fundings: list[dict],
                   mark: float) -> dict | None:
    """«Просто держать первый купленный лот» — та же комиссия и funding.

    Бенчмарк именно такой, потому что вопрос стратегии — стоит ли регулярно
    фиксировать вместо удержания (§17.6 п.5).
    """
    buys = [f for f in fills if f.get("side") == "Buy"]
    if not buys or mark <= 0:
        return None
    first = min(buys, key=lambda r: int(r["execTime"]))
    ts = int(first["execTime"])
    qty = _fnum(first.get("execQty"))
    px = _fnum(first.get("execPrice"))
    if qty <= 0 or px <= 0:
        return None
    gross = (mark - px) * qty
    fee = _fnum(first.get("execFee")) or px * qty * TAKER_FEE
    funding = 0.0
    for f in fundings:
        if int(f["execTime"]) < ts:
            continue
        value = _fnum(f.get("execValue"))
        if value <= 0:
            continue
        rate = _fnum(f.get("execFee")) / value
        px_at = _fnum(f.get("markPrice")) or _fnum(f.get("execPrice")) or mark
        funding += rate * qty * px_at
    return {"ts": ts, "qty": qty, "entry": px, "gross": gross, "fee": fee,
            "funding": funding, "total": gross - fee - funding}


def strategy_total(*, realized_net: float, unrealized: float,
                   funding_paid: float, fees_legs: float,
                   fees_in_closes: float) -> float:
    """Итог стратегии, сопоставимый с холдом.

    `closedPnl` уже содержит комиссии закрытых кругов, но комиссия входа по
    ещё открытой позиции не лежит ни там, ни в unrealized. Холд свою входную
    комиссию вычитает, поэтому её надо вычесть и здесь — иначе сравнение
    завышало бы стратегию ровно на эту величину, и при нуле фиксаций (когда
    стратегия и холд — одна и та же позиция) Δ был бы не нулевым.
    """
    fees_open = max(0.0, fees_legs - fees_in_closes)
    return realized_net + unrealized - funding_paid - fees_open


def vol_forward(closes: list[float], base_usd: float,
                interval: str = "240") -> tuple[float | None, float | None]:
    """Вола и следующая ставка §16.2. None если мало баров."""
    from hybrid_bot.signals import realized_vol_annual, vol_notional
    return (realized_vol_annual(closes, interval=interval),
            vol_notional(base_usd, closes, interval=interval))


def gate_status(n_fix: int, obs_days: float) -> str:
    """Строка о прогрессе выборки. Порог — sample-size.mdc, не наш выбор."""
    parts = [f"фиксаций {n_fix}/{GATE_MIN_EVENTS}",
             f"дней {obs_days:.1f}/{GATE_MIN_DAYS}"]
    ready = n_fix >= GATE_MIN_EVENTS and obs_days >= GATE_MIN_DAYS
    return ("выборка набрана" if ready else "выборка НЕ набрана") \
        + " (" + ", ".join(parts) + ")"


def _report_symbol(sess: HTTP, symbol: str, category: str, start_ms: int,
                   end_ms: int, *, threshold: float, db_path: str) -> dict:
    raw = _paginate(sess.get_executions, start_ms=start_ms, end_ms=end_ms,
                    category=category, symbol=symbol)
    all_fills = [f for f in raw if f.get("execType") in TRADE_EXEC_TYPES]
    all_fundings = [f for f in raw if f.get("execType") in FUNDING_EXEC_TYPES]
    all_closes = _paginate(sess.get_closed_pnl, start_ms=start_ms,
                           end_ms=end_ms, category=category, symbol=symbol)
    fills, foreign, closes, skipped_closes = split_ours(all_fills, all_closes)
    # Funding платит владелец позиции; пока на счёте была чужая позиция,
    # начисления были не наши. Своими считаем те, что после первой нашей ноги.
    our_since = min((int(f["execTime"]) for f in fills), default=end_ms)
    fundings = [f for f in all_fundings if int(f["execTime"]) >= our_since]
    pos_resp = sess.get_positions(category=category, symbol=symbol)
    time.sleep(THROTTLE_SEC)
    tick_resp = sess.get_tickers(category=category, symbol=symbol)
    time.sleep(THROTTLE_SEC)

    pos = ((pos_resp.get("result") or {}).get("list") or [{}])[0]
    tick = ((tick_resp.get("result") or {}).get("list") or [{}])[0]
    pos_size = _fnum(pos.get("size"))
    pos_avg = _fnum(pos.get("avgPrice"))
    unreal = _fnum(pos.get("unrealisedPnl"))
    mark = _fnum(pos.get("markPrice")) or _fnum(tick.get("markPrice")) \
        or _fnum(tick.get("lastPrice"))

    print(f"\n{'=' * 78}\n  {symbol}   окно "
          f"{_ms_to_utc(start_ms):%Y-%m-%d %H:%M} → "
          f"{_ms_to_utc(end_ms):%Y-%m-%d %H:%M} UTC\n{'=' * 78}")

    # ── Чужие ноги: в расчёт не идут, но знать про них надо (§18.4) ───────
    if foreign:
        who = sorted({(f.get("orderLinkId") or "")[:12] or "(без id)"
                      for f in foreign})
        f_ts = [int(f["execTime"]) for f in foreign]
        print(f"\n  На счёте есть чужие ноги: {len(foreign)} шт "
              f"({', '.join(who)}), последняя "
              f"{_ms_to_utc(max(f_ts)):%m-%d %H:%M} UTC. В расчёт НЕ идут: "
              f"считаются только ноги с префиксом {OUR_PREFIX!r}.")
        if max(f_ts) > our_since:
            print("  ⚠ чужие ноги ПОСЛЕ нашего первого входа — изоляция "
                  "§18.4 нарушена сейчас, а не только в истории")
    if skipped_closes:
        print(f"  Реализаций без нашей ноги в окне: {skipped_closes} — "
              f"отброшены (чужие или нога за границей окна)")

    # ── События: что именно закрывалось и на каком расстоянии ────────────
    reasons = db_reasons(db_path, symbol, start_ms / 1000.0)
    events: list[dict] = []
    for c in sorted(closes, key=lambda r: int(r["createdTime"])):
        anchor = _fnum(c.get("avgEntryPrice"))
        lock = _fnum(c.get("avgExitPrice"))
        qty = _fnum(c.get("closedSize"))
        events.append({
            "ts": int(c["createdTime"]),
            "qty": qty,
            "anchor": anchor,
            "lock": lock,
            "dist": (lock / anchor - 1.0) * 100.0 if anchor else 0.0,
            "net": _fnum(c.get("closedPnl")),
            "reason": match_reason(int(c["createdTime"]), qty, reasons),
        })

    realized_net = sum(e["net"] for e in events)
    fix_events = [e for e in events if e["reason"] == "fix_threshold"]
    exit_events = [e for e in events if e["reason"] == "trend_flat"]
    if events:
        print(f"\n  События ({len(events)}); причина — из БД бота, "
              f"деньги — с биржи")
        print(f"  {'время UTC':<17} {'причина':<15} {'объём':>9} "
              f"{'якорь':>10} {'лок':>10} {'лок−якорь':>10} {'net$':>9}")
        for e in events:
            print(f"  {_ms_to_utc(e['ts']):%Y-%m-%d %H:%M} {e['reason']:<15} "
                  f"{e['qty']:>9.4f} {e['anchor']:>10.2f} {e['lock']:>10.2f} "
                  f"{e['dist']:>9.3f}% {e['net']:>9.2f}")
        print(f"  {'ИТОГО реализовано (net, комиссии внутри)':<64} "
              f"{realized_net:>12.2f}")
    else:
        print("\n  Реализаций в окне нет — бот ещё держит первую позицию "
              "или тренд не давал входа.")

    # ── Проскальзывание фиксации против заявленного порога ───────────────
    med_dist = _median([e["dist"] for e in fix_events])
    med_slip = med_dist - threshold if fix_events else 0.0
    if fix_events:
        print(f"\n  Фиксации по порогу: {len(fix_events)}, "
              f"медианное расстояние {med_dist:+.3f}% против заявленных "
              f"{threshold:+.2f}%")
        print(f"    проскальзывание медианное {med_slip:+.3f} п.п. "
              f"(<0 = вышли хуже порога; в §17.6 его не было)")

    # ── Ноги и комиссии ──────────────────────────────────────────────────
    fees_legs = sum(_fnum(f.get("execFee")) for f in fills)
    makers = sum(1 for f in fills if f.get("isMaker"))
    if fills:
        print(f"\n  Ноги: {len(fills)} (maker {makers}), "
              f"комиссия ${fees_legs:.4f}")
    funding_paid = sum(_fnum(f.get("execFee")) for f in fundings)
    if fundings:
        print(f"  Funding: {len(fundings)} начислений, "
              f"${funding_paid:.4f} (>0 = списано с нас)")

    # ── Итог стратегии и бенчмарк ────────────────────────────────────────
    # Комиссии входа по ЕЩЁ ОТКРЫТОЙ позиции не лежат ни в closedPnl, ни в
    # unrealized, а бенчмарк холда свою входную комиссию вычитает. Без этой
    # поправки сравнение было бы в пользу стратегии ровно на её величину.
    fees_in_closes = sum(_fnum(c.get("openFee")) + _fnum(c.get("closeFee"))
                         for c in closes)
    fees_open = max(0.0, fees_legs - fees_in_closes)
    total = strategy_total(realized_net=realized_net, unrealized=unreal,
                           funding_paid=funding_paid, fees_legs=fees_legs,
                           fees_in_closes=fees_in_closes)
    print(f"\n  Открытый остаток: {pos_size:.4f} @ {pos_avg:.2f}, "
          f"mark {mark:.2f}, unrealized ${unreal:.2f}")
    interval = os.environ.get("HYBRID_INTERVAL", "240")
    base_usd = _fnum(os.environ.get("HYBRID_POSITION_USD"), 200.0)
    vol = None
    stake = None
    try:
        kl = sess.get_kline(category=category, symbol=symbol,
                            interval=interval, limit=400)
        time.sleep(THROTTLE_SEC)
        raw_kl = (kl.get("result") or {}).get("list") or []
        series = sorted((int(r[0]), float(r[4])) for r in raw_kl)
        if len(series) >= 3:
            series = series[:-1]
        vol, stake = vol_forward([c for _, c in series], base_usd, interval)
        if vol is not None and stake is not None:
            print(f"  Форвард §16.2: вола {vol * 100:.1f}% год., "
                  f"следующая ставка ${stake:.0f} (база ${base_usd:.0f})")
    except Exception as exc:
        print(f"  Форвард §16.2: не посчитался ({exc})")
    print(f"  СТРАТЕГИЯ ИТОГО (realized net + unrealized − funding "
          f"− комиссия открытого входа ${fees_open:.4f}): ${total:.2f}")

    hold = hold_benchmark(fills, fundings, mark)
    delta = None
    if hold:
        print(f"\n  Бенчмарк «просто держать»: {hold['qty']:.4f} @ "
              f"{hold['entry']:.2f} от {_ms_to_utc(hold['ts']):%m-%d %H:%M}")
        print(f"    gross ${hold['gross']:.2f} − комиссия ${hold['fee']:.4f} "
              f"− funding ${hold['funding']:.4f} = ${hold['total']:.2f}")
        delta = total - hold["total"]
        # Пока фиксаций не было, стратегия и холд — буквально одна позиция, и
        # Δ обязан быть нулём. Это self-check измерителя, а не результат.
        if abs(delta) < 0.01:
            verdict = "то же самое (фиксаций ещё не было)"
        else:
            verdict = "фиксация ВПЕРЕДИ" if delta > 0 else "фиксация ОТСТАЁТ"
        print(f"    Δ стратегия − холд = ${delta:.2f}  → {verdict}")
        print("    (у обоих остаток открыт, выходная комиссия не учтена)")

    # ── Гейт §8.4: сходится ли учёт бота с биржей ────────────────────────
    db_net = sum(r["pnl_usd"] or 0.0 for r in reasons)
    db_delta = db_net - realized_net
    if reasons:
        print(f"\n  Сверка учёта (гейт §8.4): БД бота ${db_net:.2f} vs "
              f"биржа ${realized_net:.2f} (расх. ${db_delta:+.2f})")
        if abs(db_delta) > max(0.05, abs(realized_net) * 0.01):
            print("    ⚠ расхождение больше 1% — учёт бота врёт, "
                  "смотри цену исполнения и комиссии")

    # ── Гейт §8.1: сколько данных набрано ────────────────────────────────
    obs_days = 0.0
    if fills:
        first_ts = min(int(f["execTime"]) for f in fills)
        obs_days = (end_ms - first_ts) / 86400000.0
    print(f"\n  Гейт §8.1: {gate_status(len(fix_events), obs_days)}")
    if delta is not None:
        print(f"    текущий знак Δ: ${delta:+.2f} — до набора выборки это "
              f"наблюдение, а не вывод")

    return {
        "symbol": symbol,
        "n_events": len(events),
        "n_fix": len(fix_events),
        "n_trend_exit": len(exit_events),
        "obs_days": round(obs_days, 4),
        "realized_net": round(realized_net, 4),
        "fees_legs": round(fees_legs, 6),
        "funding_paid": round(funding_paid, 6),
        "n_legs": len(fills),
        "pos_size": pos_size,
        "pos_avg": pos_avg,
        "mark": mark,
        "unrealized": unreal,
        "strategy_total": round(total, 4),
        "hold_qty": hold["qty"] if hold else "",
        "hold_entry": hold["entry"] if hold else "",
        "hold_total": round(hold["total"], 4) if hold else "",
        "delta_vs_hold": round(delta, 4) if delta is not None else "",
        "fix_gross": round(sum(e["net"] for e in fix_events), 4),
        "trend_exit_gross": round(sum(e["net"] for e in exit_events), 4),
        "declared_threshold": threshold,
        "median_fix_dist": round(med_dist, 4) if fix_events else "",
        "median_slip": round(med_slip, 4) if fix_events else "",
        "db_net": round(db_net, 4) if reasons else "",
        "db_delta": round(db_delta, 4) if reasons else "",
        "foreign_legs": len(foreign),
        "skipped_closes": skipped_closes,
        "vol_annual": round(vol, 6) if vol is not None else "",
        "next_stake": round(stake, 2) if stake is not None else "",
    }


def append_snapshot(path: str, rows: list[dict], *, ts_utc: str,
                    window_days: float) -> str | None:
    """Дописывает снапшот, не ломая накопленный ряд.

    При смене схемы старый файл откладывается в `*.bak`: сдвинутые колонки
    хуже разрыва в истории (тот же дефект уже ловили в §11 п.3).
    """
    if not rows:
        return None
    rotated: str | None = None
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, newline="") as fh:
            header = (fh.readline() or "").strip().split(",")
        if header != SNAPSHOT_FIELDS:
            stamp = ts_utc.replace("-", "").replace(":", "")
            rotated = f"{path}.{stamp}.bak"
            os.replace(path, rotated)
    fresh = not (os.path.exists(path) and os.path.getsize(path) > 0)
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SNAPSHOT_FIELDS, restval="",
                           extrasaction="raise")
        if fresh:
            w.writeheader()
        for r in rows:
            w.writerow({"ts_utc": ts_utc, "window_days": window_days, **r})
    return rotated


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=os.environ.get("HYBRID_SYMBOLS",
                                                        "ETHUSDT"))
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--category", default=os.environ.get(
        "HYBRID_BYBIT_CATEGORY", "linear"))
    ap.add_argument("--threshold", type=float, default=_fnum(
        os.environ.get("HYBRID_FIX_THRESHOLD_PCT"), 6.0))
    ap.add_argument("--db", default=os.path.join(
        os.environ.get("HYBRID_DATA_DIR", "/data"), "hybrid_bot.sqlite"))
    ap.add_argument("--snapshot", nargs="?",
                    const="/data/hybrid_live_audit.csv", default=None)
    args = ap.parse_args()

    key = os.environ.get("HYBRID_BYBIT_API_KEY", "")
    secret = os.environ.get("HYBRID_BYBIT_API_SECRET", "")
    if not key or not secret:
        raise SystemExit("нужны HYBRID_BYBIT_API_KEY / HYBRID_BYBIT_API_SECRET")
    demo = os.environ.get("HYBRID_BYBIT_DEMO", "true").lower() in (
        "1", "true", "yes")

    sess = HTTP(demo=demo, api_key=key, api_secret=secret, recv_window=20000)
    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)

    print(f"hybrid_bot live audit | demo={demo} | порог +{args.threshold}% | "
          f"источник: Bybit API, причины закрытий: {args.db}")

    rows = [_report_symbol(sess, s.strip().upper(), args.category, start_ms,
                           end_ms, threshold=args.threshold, db_path=args.db)
            for s in args.symbols.split(",") if s.strip()]

    total = sum(r["strategy_total"] for r in rows)
    print(f"\n{'=' * 78}\n  Сумма по символам: ${total:.2f}\n{'=' * 78}")

    if args.snapshot:
        rotated = append_snapshot(args.snapshot, rows,
                                  ts_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  window_days=args.days)
        if rotated:
            print(f"схема снапшота изменилась, старый файл отложен: {rotated}")
        print(f"снапшот дописан: {args.snapshot}")


if __name__ == "__main__":
    main()
