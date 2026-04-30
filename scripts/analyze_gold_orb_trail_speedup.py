#!/usr/bin/env python3
"""Аналитический артефакт: верхняя граница потенциального улучшения от
ускорения trail-полла с 5 мин до 1 мин (idealized intrabar trail).

## Цель

Текущий `gold_orb` live использует 5-мин цикл (`POLL_INTERVAL_SEC=300`):
peak_price для bot-side `scalp_trail` обновляется только на close M5
(см. `monitor.py`). Вопрос: сколько бы прибыли мы выиграли, если бы
peak обновлялся в реальном времени (1 раз в минуту или чаще)?

Ответ требует M1-данных, которых нет в проекте. Этот скрипт даёт
**верхнюю границу** улучшения через идеализацию intrabar — peak
обновляется по bar high/low (= ровно по моменту экстремума внутри
M5-свечи), exit срабатывает в той же свече если retreat достаточный.

## Три режима

- **CANON**: ATR-SL=1.5×ATR, ATR-TP=3.0×ATR, time-stop по `MAX_HOLD_BARS`.
  Без trail — baseline +6146 net pip из STRATEGIES.md §3b-bis.
- **LIVE**: те же SL/TP + bot-side `scalp_trail` на close M5
  (peak обновляется раз в 5 мин, exit на close следующего бара
  после trigger+retreat).
- **INTRABAR (idealized)**: те же SL/TP + bot-side `scalp_trail`,
  но peak обновляется по bar high (long) / low (short), exit
  срабатывает в той же M5-свече при касании trail-level.
  Допущение: внутри M5 peak случается **раньше** retreat, значит
  trail успевает зафиксировать peak. Это даёт верхнюю границу
  потенциала ускорения, реальный M1 будет где-то между LIVE и INTRABAR.

## Как читать

Если `INTRABAR ≈ LIVE` (разница <5% NET) — идти в M1 backtest нет
смысла, ускорение trail ничего не даст.

Если `INTRABAR >> LIVE` (разница ≥10% NET) — есть потенциал,
имеет смысл скачать M1 cTrader-данные и сделать честный замер.

Решение о применении принимается **только** при выполнении
`sample-size.mdc` (≥100 сделок, OOS-проверка) и явном согласовании.

Запуск:
    PYTHONPATH=src python3 -m scripts.analyze_gold_orb_trail_speedup \\
        --data-dir data/fxpro_klines --out-dir data
"""

from __future__ import annotations

import argparse
import bisect
import csv
import logging
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fx_pro_bot.config.settings import pip_size, spread_cost_pips
from fx_pro_bot.market_data.models import Bar, InstrumentId

from scripts.analyze_gold_orb_trail_compare import (
    SCALPING_HARD_STOP_BARS,
    SCALPING_TRAIL_DISTANCE_ATR_MULT,
    SCALPING_TRAIL_DISTANCE_PIPS,
    SCALPING_TRAIL_TRIGGER_ATR_MULT,
    SCALPING_TRAIL_TRIGGER_PIPS,
    TradeResult,
    _build_result,
    _calc_pips,
    _simulate_canon,
    _simulate_live,
    find_gold_orb_signals,
)
from scripts.backtest_fxpro_all import _hit_sl_tp, load_bars


def load_m1_bars(data_dir: Path, yf_symbol: str) -> list[Bar]:
    """Загрузка M1-баров. Файл `<sym>_M1.csv` в том же формате что M5."""
    fname = (
        yf_symbol.replace("=X", "").replace("=F", "_F").replace("-", "_")
        + "_M1.csv"
    )
    path = data_dir / fname
    if not path.exists():
        return []
    instr = InstrumentId(symbol=yf_symbol)
    bars: list[Bar] = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_ms = int(row["timestamp"])
            bars.append(Bar(
                instrument=instr,
                ts=datetime.fromtimestamp(ts_ms / 1000, UTC),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    return bars


log = logging.getLogger("analyze_gold_orb_trail_speedup")


def _simulate_intrabar_trail(
    bars: list[Bar], entry_idx: int, direction: str,
    entry_price: float, sl: float, tp: float,
    atr_v: float, spread_pips: float, ps: float,
) -> TradeResult | None:
    """INTRABAR (idealized 1-min trail upper bound).

    Отличие от `_simulate_live`:
    - peak обновляется по bar **high** (long) / **low** (short) — то есть
      ровно по интра-бар экстремуму, как если бы trail-полл шёл с
      достаточной частотой чтобы поймать момент high/low.
    - Если peak_pips >= trigger И retreat внутри той же свечи
      достигает trail_d — exit срабатывает в этой же свече по
      trail_level (а не по close следующего бара, как в LIVE).
    - Допущение порядка: для long считаем high был ПЕРВЫМ, low — после;
      для short — low первый, high после. Это даёт верхнюю границу.

    SL/TP проверяются intrabar (broker-side) до trail-логики, как в LIVE.
    """
    is_long = direction == "long"
    atr_pips = atr_v / ps if ps > 0 else 0.0
    trigger_pips = max(
        SCALPING_TRAIL_TRIGGER_ATR_MULT * atr_pips,
        SCALPING_TRAIL_TRIGGER_PIPS,
    )
    trail_d_pips = max(
        SCALPING_TRAIL_DISTANCE_ATR_MULT * atr_pips,
        SCALPING_TRAIL_DISTANCE_PIPS,
    )
    peak = entry_price
    max_hold = SCALPING_HARD_STOP_BARS

    for j in range(entry_idx + 1, min(entry_idx + 1 + max_hold, len(bars))):
        b = bars[j]
        hit_sl, hit_tp = _hit_sl_tp(b, is_long, sl, tp)
        if hit_sl and hit_tp:
            return _build_result(
                "INTRABAR", direction, bars[entry_idx], b, entry_price, sl,
                sl, tp, atr_v, "sl", j - entry_idx, peak, ps, spread_pips,
            )
        if hit_sl:
            return _build_result(
                "INTRABAR", direction, bars[entry_idx], b, entry_price, sl,
                sl, tp, atr_v, "sl", j - entry_idx, peak, ps, spread_pips,
            )
        if hit_tp:
            return _build_result(
                "INTRABAR", direction, bars[entry_idx], b, entry_price, tp,
                sl, tp, atr_v, "tp", j - entry_idx, peak, ps, spread_pips,
            )

        new_peak = max(peak, b.high) if is_long else min(peak, b.low)
        peak_pips = _calc_pips(direction, entry_price, new_peak, ps)
        if peak_pips >= trigger_pips:
            trail_level = (
                new_peak - trail_d_pips * ps
                if is_long
                else new_peak + trail_d_pips * ps
            )
            retreat_in_bar = (
                b.low <= trail_level if is_long else b.high >= trail_level
            )
            if retreat_in_bar:
                return _build_result(
                    "INTRABAR", direction, bars[entry_idx], b,
                    entry_price, trail_level,
                    sl, tp, atr_v, "scalp_trail",
                    j - entry_idx, new_peak, ps, spread_pips,
                )
        peak = new_peak

    end = min(entry_idx + max_hold, len(bars) - 1)
    b = bars[end]
    return _build_result(
        "INTRABAR", direction, bars[entry_idx], b, entry_price, b.close,
        sl, tp, atr_v, "scalp_time_4h", end - entry_idx, peak, ps, spread_pips,
    )


def _simulate_m1_trail(
    m5_bars: list[Bar], m1_bars: list[Bar], m1_ts_index: list[datetime],
    entry_idx: int, direction: str,
    entry_price: float, sl: float, tp: float,
    atr_v: float, spread_pips: float, ps: float,
) -> TradeResult | None:
    """M1 (realistic 1-min trail).

    Использует реальные M1-бары для пути внутри M5-холда:
    - SL/TP проверяются на каждом M1-баре (broker-side, intra-bar high/low).
    - peak обновляется по close M1 (ровно так, как делал бы live trail-цикл
      раз в минуту: poll → last close M1 → update peak).
    - Если peak_pips >= trigger И (peak_pips - cur_pips) >= trail_d на M1
      close → exit на этот же M1 close.
    - Time-stop = SCALPING_HARD_STOP_BARS M5 = 48 × 5 мин = 240 M1.

    Это **реалистичная** оценка (без slippage/rejected-amend overhead),
    верхняя граница того что даст реализация fast-trail цикла.
    """
    is_long = direction == "long"
    atr_pips = atr_v / ps if ps > 0 else 0.0
    trigger_pips = max(
        SCALPING_TRAIL_TRIGGER_ATR_MULT * atr_pips,
        SCALPING_TRAIL_TRIGGER_PIPS,
    )
    trail_d_pips = max(
        SCALPING_TRAIL_DISTANCE_ATR_MULT * atr_pips,
        SCALPING_TRAIL_DISTANCE_PIPS,
    )

    entry_bar = m5_bars[entry_idx]
    start_ts = entry_bar.ts + timedelta(minutes=5)
    max_hold_minutes = SCALPING_HARD_STOP_BARS * 5
    end_ts = start_ts + timedelta(minutes=max_hold_minutes)

    start_i = bisect.bisect_left(m1_ts_index, start_ts)
    if start_i >= len(m1_bars):
        return None
    end_i = bisect.bisect_left(m1_ts_index, end_ts)

    peak = entry_price
    last_b: Bar | None = None
    for j in range(start_i, end_i):
        b = m1_bars[j]
        last_b = b
        hit_sl, hit_tp = _hit_sl_tp(b, is_long, sl, tp)
        if hit_sl and hit_tp:
            return _build_result(
                "M1", direction, entry_bar, b, entry_price, sl,
                sl, tp, atr_v, "sl", (j - start_i + 1) // 5 + 1,
                peak, ps, spread_pips,
            )
        if hit_sl:
            return _build_result(
                "M1", direction, entry_bar, b, entry_price, sl,
                sl, tp, atr_v, "sl", (j - start_i + 1) // 5 + 1,
                peak, ps, spread_pips,
            )
        if hit_tp:
            return _build_result(
                "M1", direction, entry_bar, b, entry_price, tp,
                sl, tp, atr_v, "tp", (j - start_i + 1) // 5 + 1,
                peak, ps, spread_pips,
            )
        peak = max(peak, b.close) if is_long else min(peak, b.close)
        cur_pips = _calc_pips(direction, entry_price, b.close, ps)
        peak_pips = _calc_pips(direction, entry_price, peak, ps)
        if (peak_pips >= trigger_pips
                and (peak_pips - cur_pips) >= trail_d_pips):
            return _build_result(
                "M1", direction, entry_bar, b, entry_price, b.close,
                sl, tp, atr_v, "scalp_trail", (j - start_i + 1) // 5 + 1,
                peak, ps, spread_pips,
            )

    if last_b is None:
        return None
    return _build_result(
        "M1", direction, entry_bar, last_b, entry_price, last_b.close,
        sl, tp, atr_v, "scalp_time_4h",
        (end_i - start_i) // 5 + 1, peak, ps, spread_pips,
    )


def _summarize(label: str, trades: list[TradeResult]) -> dict:
    if not trades:
        return {"label": label, "n": 0}
    n = len(trades)
    wins = [t for t in trades if t.net_pips > 0]
    losses = [t for t in trades if t.net_pips <= 0]
    nets = [t.net_pips for t in trades]
    gross_p = sum(t.net_pips for t in wins)
    gross_l = -sum(t.net_pips for t in losses) or 1e-9
    pf = gross_p / gross_l
    avg_win = statistics.mean([t.net_pips for t in wins]) if wins else 0.0
    avg_loss = statistics.mean([t.net_pips for t in losses]) if losses else 0.0
    return {
        "label": label, "n": n,
        "wr": len(wins) / n * 100,
        "net": round(sum(nets), 1),
        "avg": round(statistics.mean(nets), 2),
        "median": round(statistics.median(nets), 2),
        "pf": round(pf, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_win": round(max(nets), 1),
        "max_loss": round(min(nets), 1),
    }


def _by_reason(trades: list[TradeResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        out[t.reason] = out.get(t.reason, 0) + 1
    return out


def _walk_forward_thirds(trades: list[TradeResult]) -> list[dict]:
    if not trades:
        return []
    sorted_t = sorted(trades, key=lambda t: t.entry_ts)
    n = len(sorted_t)
    third = n // 3
    parts = [
        sorted_t[:third],
        sorted_t[third:2 * third],
        sorted_t[2 * third:],
    ]
    return [_summarize(f"T{i+1}", p) for i, p in enumerate(parts)]


def _avg_peak_capture(trades: list[TradeResult]) -> float:
    """Средний % захваченного peak-движения. peak_pips > 0 — peak движение,
    net_pips — реализованное. Captured = net / peak (если peak > 0)."""
    cap = []
    for t in trades:
        if t.peak_pips > 0:
            cap.append(max(t.net_pips, 0) / t.peak_pips * 100)
    return round(statistics.mean(cap), 1) if cap else 0.0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="data/fxpro_klines")
    p.add_argument("--out-dir", default="data")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
    )

    bars = load_bars(Path(args.data_dir), "GC=F")
    if not bars:
        log.error("Нет баров GC=F в %s", args.data_dir)
        return
    log.info(
        "GC=F: %d M5 баров (%s → %s)",
        len(bars), bars[0].ts.date(), bars[-1].ts.date(),
    )

    m1_bars = load_m1_bars(Path(args.data_dir), "GC=F")
    m1_ts_index: list[datetime] = []
    if m1_bars:
        m1_ts_index = [b.ts for b in m1_bars]
        log.info(
            "GC=F: %d M1 баров (%s → %s)",
            len(m1_bars), m1_bars[0].ts.date(), m1_bars[-1].ts.date(),
        )
    else:
        log.warning(
            "M1 баров нет — режим M1 будет пропущен. "
            "Скачать: docker exec ... fetch_fxpro_history --interval 1 ...",
        )

    sym = "GC=F"
    ps = pip_size(sym)
    spread = spread_cost_pips(sym) * 1.2

    sigs = find_gold_orb_signals(bars)
    log.info("Найдено %d gold_orb сигналов за период", len(sigs))

    canon: list[TradeResult] = []
    live: list[TradeResult] = []
    intrabar: list[TradeResult] = []
    m1_trades: list[TradeResult] = []
    for s in sigs:
        c = _simulate_canon(
            bars, s["idx"], s["direction"], s["entry_price"],
            s["sl"], s["tp"], s["atr_v"], spread, ps,
        )
        l_ = _simulate_live(
            bars, s["idx"], s["direction"], s["entry_price"],
            s["sl"], s["tp"], s["atr_v"], spread, ps,
        )
        i_ = _simulate_intrabar_trail(
            bars, s["idx"], s["direction"], s["entry_price"],
            s["sl"], s["tp"], s["atr_v"], spread, ps,
        )
        if c:
            canon.append(c)
        if l_:
            live.append(l_)
        if i_:
            intrabar.append(i_)
        if m1_bars:
            m_ = _simulate_m1_trail(
                bars, m1_bars, m1_ts_index,
                s["idx"], s["direction"], s["entry_price"],
                s["sl"], s["tp"], s["atr_v"], spread, ps,
            )
            if m_:
                m1_trades.append(m_)

    print("\n" + "═" * 96)
    print(
        " gold_orb: CANON vs LIVE (5-min) vs M1 (real 1-min) vs INTRABAR (upper bound)"
    )
    print("═" * 96)
    print(
        f" Период: {bars[0].ts.date()} → {bars[-1].ts.date()}, "
        f"{len(bars)} M5 баров"
    )
    if m1_bars:
        print(f" M1 path: {len(m1_bars)} баров для реалистичного 1-мин trail")
    print(
        f" Сигналов: {len(sigs)}, "
        f"CANON={len(canon)}, LIVE={len(live)}, "
        f"M1={len(m1_trades)}, INTRABAR={len(intrabar)}"
    )
    print()

    sc = _summarize("CANON", canon)
    sl_ = _summarize("LIVE", live)
    sm = _summarize("M1", m1_trades)
    si = _summarize("INTRABAR", intrabar)
    print(
        f" {'Метрика':<18}  {'CANON':>10}  {'LIVE':>10}  "
        f"{'M1':>10}  {'INTRABAR':>10}  {'Δ (M1-LIVE)':>13}"
    )
    print(" " + "─" * 80)
    keys = [
        ("n", "trades"),
        ("wr", "win-rate %"),
        ("net", "net pips"),
        ("pf", "profit factor"),
        ("avg", "avg pip"),
        ("median", "median pip"),
        ("avg_win", "avg win"),
        ("avg_loss", "avg loss"),
        ("max_win", "max win"),
        ("max_loss", "max loss"),
    ]
    for key, lab in keys:
        c = sc.get(key, 0)
        l_v = sl_.get(key, 0)
        m_v = sm.get(key, 0)
        iv = si.get(key, 0)
        if isinstance(c, float):
            d = round(m_v - l_v, 2) if isinstance(m_v, (int, float)) else 0.0
            print(
                f" {lab:<18}  {c:>10.2f}  {l_v:>10.2f}  "
                f"{m_v if isinstance(m_v, (int, float)) else 0.0:>10.2f}  "
                f"{iv:>10.2f}  {d:>+13.2f}"
            )
        else:
            d = (m_v - l_v) if isinstance(m_v, int) else 0
            print(
                f" {lab:<18}  {c:>10}  {l_v:>10}  "
                f"{m_v:>10}  {iv:>10}  {d:>+13}"
            )

    cap_c = _avg_peak_capture(canon)
    cap_l = _avg_peak_capture(live)
    cap_m = _avg_peak_capture(m1_trades) if m1_trades else 0.0
    cap_i = _avg_peak_capture(intrabar)
    print(
        f" {'peak captured%':<18}  {cap_c:>10.1f}  {cap_l:>10.1f}  "
        f"{cap_m:>10.1f}  {cap_i:>10.1f}  {cap_m - cap_l:>+13.1f}"
    )

    print()
    print(" Распределение exit reasons:")
    cr = _by_reason(canon)
    lr = _by_reason(live)
    mr = _by_reason(m1_trades)
    ir = _by_reason(intrabar)
    all_reasons = sorted(set(cr) | set(lr) | set(mr) | set(ir))
    for r in all_reasons:
        print(
            f"   {r:<18}  CANON={cr.get(r, 0):>4}  "
            f"LIVE={lr.get(r, 0):>4}  M1={mr.get(r, 0):>4}  "
            f"INTRABAR={ir.get(r, 0):>4}"
        )

    print()
    print(" Walk-forward (трети по времени, NET pips):")
    wf_c = _walk_forward_thirds(canon)
    wf_l = _walk_forward_thirds(live)
    wf_m = _walk_forward_thirds(m1_trades) if m1_trades else [{}, {}, {}]
    wf_i = _walk_forward_thirds(intrabar)
    print(
        f" {'period':<6}  {'n':>4}  "
        f"{'NET_C':>8}  {'NET_L':>8}  {'NET_M':>8}  {'NET_I':>8}  "
        f"{'PF_L':>6}  {'PF_M':>6}  {'PF_I':>6}"
    )
    for c, l_v, m_v, i_v in zip(wf_c, wf_l, wf_m, wf_i):
        m_net = m_v.get("net", 0) if m_v else 0
        m_pf = m_v.get("pf", 0) if m_v else 0
        print(
            f" {c['label']:<6}  {c['n']:>4}  "
            f"{c['net']:>8}  {l_v['net']:>8}  {m_net:>8}  {i_v['net']:>8}  "
            f"{l_v['pf']:>6}  {m_pf:>6}  {i_v['pf']:>6}"
        )

    print()
    print(" Интерпретация:")
    if m1_trades and sl_.get("net"):
        delta_m = round(sm["net"] - sl_["net"], 1)
        delta_m_pct = round((sm["net"] - sl_["net"]) / abs(sl_["net"]) * 100, 1)
        delta_i = round(si["net"] - sl_["net"], 1)
        delta_i_pct = round((si["net"] - sl_["net"]) / abs(sl_["net"]) * 100, 1)
        print(
            f"   Δ NET (M1 - LIVE)       = {delta_m:+.1f} pips "
            f"({delta_m_pct:+.1f}%) ← реалистичная оценка"
        )
        print(
            f"   Δ NET (INTRABAR - LIVE) = {delta_i:+.1f} pips "
            f"({delta_i_pct:+.1f}%) ← upper bound (high/low в той же M5)"
        )
        if abs(delta_m_pct) < 5:
            print(
                "   → M1 близок к LIVE: ускорять trail смысла нет, "
                "5-мин достаточно."
            )
        elif abs(delta_m_pct) < 15:
            print(
                "   → M1 даёт умеренный прирост. Решение по бизнес-логике "
                "(сложность реализации vs +pips)."
            )
        else:
            print(
                "   → M1 даёт значимый прирост. Стоит планировать "
                "fast-trail цикл (но осторожно: операционные риски, "
                "rejected amends, slippage не учтены)."
            )
    elif sl_.get("net"):
        delta_pips = round(si["net"] - sl_["net"], 1)
        delta_pct = round(
            (si["net"] - sl_["net"]) / abs(sl_["net"]) * 100, 1,
        )
        print(
            f"   Δ NET (INTRABAR - LIVE) = {delta_pips:+.1f} pips "
            f"({delta_pct:+.1f}%) — нет M1, только upper bound."
        )

    out_path = Path(args.out_dir) / "gold_orb_trail_speedup.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "variant", "direction", "entry_ts", "entry_price", "exit_ts",
        "exit_price", "sl", "tp", "atr", "reason", "bars_held",
        "peak_price", "peak_pips", "pnl_pips", "net_pips",
    ]
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in canon + live + m1_trades + intrabar:
            w.writerow({
                "variant": t.variant, "direction": t.direction,
                "entry_ts": t.entry_ts.isoformat(),
                "entry_price": round(t.entry_price, 5),
                "exit_ts": t.exit_ts.isoformat(),
                "exit_price": round(t.exit_price, 5),
                "sl": round(t.sl, 5), "tp": round(t.tp, 5),
                "atr": round(t.atr, 5), "reason": t.reason,
                "bars_held": t.bars_held,
                "peak_price": round(t.peak_price, 5),
                "peak_pips": t.peak_pips,
                "pnl_pips": t.pnl_pips, "net_pips": t.net_pips,
            })
    print()
    print(f" Per-trade CSV: {out_path}")
    print("═" * 88)


if __name__ == "__main__":
    main()
