"""Faithful replay-бэктест sweep_fade на исторических тиках Bybit + анализ по
РЕЖИМУ рынка (тренд vs range). Цель — проверить гипотезу: mean-reversion fade
сливает в сильном тренде и плюсует в range (canon: MR работает в диапазоне,
momentum — в тренде; Wilder 1978 ADX>25 = тренд).

ЧТО FAITHFUL:
  - CVD/sweep/divergence/reclaim/momentum/bar-close: реальные SymbolState +
    SweepReclaimDetector (те же чистые функции, что в проде).
  - Выходы flow_exit/flow_scratch: реальный SweepFadeStrategy.should_exit.
  - TP@take_profit_r / SL@−1R: проверка по тиковому пути цены.
  - Комиссия: round_trip_fee_frac на оба плеча.

ИЗВЕСТНЫЕ ОТСТУПЛЕНИЯ (нет L2-стакана в публичных тиках):
  - ob_imbalance недоступен → require_ob_imbalance=False (ob был бы бонусом).
  - best_bid/ask нет → entry = last_price (без maker-спреда).
  - maker non-fill (вживую ~63% сигналов не наливаются) НЕ моделируется →
    АБСОЛЮТНЫЙ netPnL оптимистичен. Но СРАВНЕНИЕ режимов (trend vs range)
    к этому устойчиво — это и есть deliverable.
  - тики коалесцируются в 250мс-бины (скорость): CVD кумулятивна, путь
    сохраняется; теряется лишь суб-250мс разрешение цены (для 30-300с окон ok).
  - ob-гейт ВЫКЛ → бэктест стреляет ЧАЩЕ live (в live ob отсекает ~часть);
    абсолютная частота нерелевантна, сравниваем режимы между собой.

Запуск (локально, пакет scalp_bot импортируется):
  python3 scripts/scalp_backtest_regime.py ALLOUSDT,BNBUSDT,NEARUSDT 2026-05-20 2026-05-31
"""
from __future__ import annotations

import gzip
import io
import logging
import os
import sys
import urllib.request
from datetime import date, timedelta
from types import SimpleNamespace

logging.disable(logging.INFO)  # глушим play-логи детектора

from scalp_bot.config.settings import load_settings              # noqa: E402
from scalp_bot.data.aggregates import SymbolState                 # noqa: E402
from scalp_bot.analysis.strategies import SweepFadeStrategy       # noqa: E402

CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "scalp_ticks")
BASE = "https://public.bybit.com/trading"


# ─── загрузка тиков ────────────────────────────────────────────────────────
def fetch_day(symbol: str, day: str) -> list[tuple]:
    """(ts, side, size, price) за день. Кэш в data/scalp_ticks/."""
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, f"{symbol}{day}.csv.gz")
    if not os.path.exists(fp):
        url = f"{BASE}/{symbol}/{symbol}{day}.csv.gz"
        try:
            urllib.request.urlretrieve(url, fp)
        except Exception as e:
            print(f"  ! нет {symbol} {day}: {e}")
            return []
    out = []
    with gzip.open(fp, "rt") as f:
        f.readline()  # header
        for line in f:
            p = line.split(",", 5)
            try:
                out.append((float(p[0]), p[2], float(p[3]), float(p[4])))
            except (ValueError, IndexError):
                continue
    return out


def bin_ticks(ticks: list, bin_sec: float = 0.25) -> list:
    """Коалесцируем тики в bin_sec-бины: per-bin net-delta (сумма знакового
    объёма) + last price. CVD КУМУЛЯТИВНА → её путь на границах бинов сохраняется
    точно; теряем лишь суб-bin_sec разрешение цены (для окон 30-300с пренебрежимо).
    Нужно для скорости: liquid-символы дают млн тиков/день."""
    if not ticks:
        return []
    out = []
    cur_bin = int(ticks[0][0] / bin_sec)
    net = 0.0; last_px = ticks[0][3]; last_ts = ticks[0][0]
    for ts, side, size, price in ticks:
        b = int(ts / bin_sec)
        if b != cur_bin:
            sd = "Buy" if net >= 0 else "Sell"
            out.append((last_ts, sd, abs(net), last_px))
            cur_bin = b; net = 0.0
        net += size if side.upper() == "BUY" else -size
        last_px = price; last_ts = ts
    out.append((last_ts, "Buy" if net >= 0 else "Sell", abs(net), last_px))
    return out


def daterange(a: str, b: str):
    d0 = date.fromisoformat(a); d1 = date.fromisoformat(b)
    d = d0
    while d <= d1:
        yield d.isoformat()
        d += timedelta(days=1)


# ─── режим: ADX(14) на 1H-клинах ───────────────────────────────────────────
def _ema(vals: list[float], n: int) -> list[float]:
    """EMA длиной len(vals); прогрев = SMA первых n (как HtfTrend в проде)."""
    out = [0.0] * len(vals)
    if len(vals) < n:
        return out
    sma = sum(vals[:n]) / n
    out[n - 1] = sma
    k = 2 / (n + 1)
    ema = sma
    for i in range(n, len(vals)):
        ema = vals[i] * k + ema * (1 - k)
        out[i] = ema
    return out


def load_regime(symbol: str, start: str, end: str):
    """Возвращает regime_at(ts)->('trend'|'range'|'mixed', adx, htf_dir), где
    htf_dir ∈ {'long','short','n/a'} — направление H1 по EMA200 (как прод-фильтр
    require_htf_trend). Для A/B HTF-фильтра (EMA200-H1 vs ADX-режим)."""
    from pybit.unified_trading import HTTP
    sess = HTTP(testnet=False)
    start_ms = int(date.fromisoformat(start).strftime("%s")) * 1000 - 15 * 86400_000
    end_ms = (int(date.fromisoformat(end).strftime("%s")) + 86400) * 1000
    rows = []
    cur = start_ms
    while cur < end_ms:
        r = sess.get_kline(category="linear", symbol=symbol, interval="60",
                           start=cur, limit=1000)["result"]["list"]
        if not r:
            break
        rows.extend(r)
        oldest = min(int(x[0]) for x in r)
        newest = max(int(x[0]) for x in r)
        if newest <= cur:
            break
        cur = newest + 3600_000
    # уникализируем и сортируем по времени (Bybit отдаёт свежие первыми)
    kl = sorted({int(x[0]): x for x in rows}.values(), key=lambda x: int(x[0]))
    if len(kl) < 30:
        return lambda ts: ("n/a", 0.0, "n/a")
    ts_arr = [int(x[0]) / 1000 for x in kl]
    high = [float(x[2]) for x in kl]
    low = [float(x[3]) for x in kl]
    close = [float(x[4]) for x in kl]
    adx = _adx(high, low, close, 14)
    ema200 = _ema(close, 200)
    def regime_at(ts: float):
        i = 0
        for j in range(len(ts_arr)):
            if ts_arr[j] <= ts:
                i = j
            else:
                break
        a = adx[i]
        reg = "trend" if a >= 25 else ("range" if a < 20 else "mixed")
        e = ema200[i]
        htf = "n/a" if e <= 0 else ("long" if close[i] > e else "short")
        return reg, a, htf
    return regime_at


def _adx(high, low, close, n=14):
    """Wilder ADX. Возвращает список длиной len(close) (первые n*2 ≈ прогрев)."""
    tr = [0.0]; pdm = [0.0]; ndm = [0.0]
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]),
                      abs(low[i] - close[i - 1])))
        up = high[i] - high[i - 1]; dn = low[i - 1] - low[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)

    def wilder(x):
        out = [0.0] * len(x)
        if len(x) <= n:
            return out
        s = sum(x[1:n + 1]); out[n] = s
        for i in range(n + 1, len(x)):
            s = s - s / n + x[i]; out[i] = s
        return out

    atr = wilder(tr); pdmS = wilder(pdm); ndmS = wilder(ndm)
    pdi = [100 * (pdmS[i] / atr[i]) if atr[i] else 0.0 for i in range(len(close))]
    ndi = [100 * (ndmS[i] / atr[i]) if atr[i] else 0.0 for i in range(len(close))]
    dx = [100 * abs(pdi[i] - ndi[i]) / (pdi[i] + ndi[i]) if (pdi[i] + ndi[i]) else 0.0
          for i in range(len(close))]
    adx = [0.0] * len(close)
    if len(dx) > 2 * n:
        s = sum(dx[n + 1:2 * n + 1]) / n; adx[2 * n] = s
        for i in range(2 * n + 1, len(close)):
            s = (s * (n - 1) + dx[i]) / n; adx[i] = s
    return adx


# ─── replay одного символа ─────────────────────────────────────────────────
def _blocked(side: str, htf: str, adx: float, mode: str, adx_thresh: float) -> bool:
    """Фильтр входа. ema: направленный EMA200-H1 (как прод require_htf_trend) —
    фейд только ПО тренду H1. adx: режим-гейт по СИЛЕ тренда (канон MR) — фейд в
    ОБЕ стороны, но только когда тренд не сильный (adx < порога). none: без фильтра."""
    if mode == "ema":
        if htf == "long" and side == "short":
            return True
        if htf == "short" and side == "long":
            return True
        return False
    if mode == "adx":
        return adx >= adx_thresh
    return False


def replay(symbol: str, ticks: list, cfg, regime_at, scratch_cf: bool = False,
           filter_mode: str = "none", adx_thresh: float = 25.0,
           sl_cooldown: float = 0.0) -> list[dict]:
    """sl_cooldown>0: после выхода по SL блокируем НОВЫЙ вход в ТУ ЖЕ сторону на
    sl_cooldown секунд (канон: не перефейдивать провалившийся уровень сразу,
    Connors/Raschke). Противоположную сторону не трогаем."""
    """scratch_cf=True: КОНТРФАКТУАЛ — не закрываем по flow_scratch, а помечаем
    момент (would_scratch, scratch_R) и даём сделке дойти до естественного конца
    (TP/SL/flow_exit). Так видим: скретч спас от SL или зарезал отскок к TP."""
    clk = SimpleNamespace(t=0.0)
    state = SymbolState(symbol, cvd_window_sec=cfg.cvd_window_sec,
                        liq_window_sec=cfg.liq_window_sec, ob_levels=cfg.ob_levels,
                        now=lambda: clk.t)
    strat = SweepFadeStrategy(cfg, [symbol])
    fee = cfg.round_trip_fee_frac
    trades = []
    pos = None
    eval_next = 0.0
    last_sl_ts = {"long": -1e18, "short": -1e18}  # для sl_cooldown
    for ts, side, size, price in ticks:
        clk.t = ts
        state.on_trade(price, size, side)
        # интрабар TP/SL по тиковой цене
        if pos is not None:
            hit = None
            if pos["side"] == "long":
                if price <= pos["sl"]:
                    hit = ("sl_hit", pos["sl"])
                elif price >= pos["tp"]:
                    hit = ("tp_hit", pos["tp"])
            else:
                if price >= pos["sl"]:
                    hit = ("sl_hit", pos["sl"])
                elif price <= pos["tp"]:
                    hit = ("tp_hit", pos["tp"])
            if hit:
                trades.append(_close(pos, hit[0], hit[1], ts, fee))
                if hit[0] == "sl_hit":
                    last_sl_ts[pos["side"]] = ts
                pos = None
        if ts < eval_next:
            continue
        eval_next = ts + cfg.eval_interval_sec
        snap = state.snapshot()
        if pos is None:
            sig = strat.update(snap, ts)
            if sig is not None:
                reg, adx, htf = regime_at(ts)
                if _blocked(sig.side, htf, adx, filter_mode, adx_thresh):
                    continue  # фильтр входа зарезал сигнал
                if sl_cooldown > 0 and ts - last_sl_ts[sig.side] < sl_cooldown:
                    continue  # cooldown после SL в ту же сторону
                pos = {"side": sig.side, "entry": sig.entry_ref, "sl": sig.sl_level,
                       "tp": sig.tp_level, "ts_open": ts, "regime": reg, "adx": adx,
                       "risk": abs(sig.entry_ref - sig.sl_level)}
        else:
            tr = SimpleNamespace(ts_open=pos["ts_open"], entry=pos["entry"],
                                 sl=pos["sl"], side=pos["side"], strategy="sweep_fade")
            ex = strat.should_exit(tr, snap, ts)
            if ex is not None:
                if ex[0] == "flow_scratch" and scratch_cf:
                    if not pos.get("would_scratch"):
                        e = pos["entry"]; risk = pos["risk"] or 1e-9
                        fav = (ex[1] - e) if pos["side"] == "long" else (e - ex[1])
                        pos["would_scratch"] = True
                        pos["scratch_R"] = (fav - fee * e) / risk
                    # НЕ закрываем — держим до естественного конца (контрфактуал)
                else:
                    trades.append(_close(pos, ex[0], ex[1], ts, fee))
                    pos = None
    return trades


def _close(pos, reason, exit_price, ts, fee):
    e = pos["entry"]; risk = pos["risk"] or 1e-9
    fav = (exit_price - e) if pos["side"] == "long" else (e - exit_price)
    gross_R = fav / risk
    net_frac = fav / e - fee
    net_R = (fav - fee * e) / risk
    return {"regime": pos["regime"], "adx": pos["adx"], "side": pos["side"],
            "reason": reason, "gross_R": gross_R, "net_R": net_R,
            "net_frac": net_frac, "hold": ts - pos["ts_open"],
            "would_scratch": pos.get("would_scratch", False),
            "scratch_R": pos.get("scratch_R")}


# ─── агрегаты ──────────────────────────────────────────────────────────────
def report(trades: list[dict]):
    if not trades:
        print("нет сделок"); return

    def block(rows, label):
        if not rows:
            print(f"  {label:8} n=0"); return
        wins = [r for r in rows if r["net_R"] > 0]
        net = sum(r["net_R"] for r in rows)
        gross = sum(r["gross_R"] for r in rows)
        print(f"  {label:8} n={len(rows):>4} win={len(wins)/len(rows)*100:>3.0f}% "
              f"netR={net:>+7.1f} (avg {net/len(rows):>+5.2f}) "
              f"grossR={gross:>+7.1f} (avg {gross/len(rows):>+5.2f})")

    print(f"\n===== ИТОГО n={len(trades)} =====")
    print(">>> ПО РЕЖИМУ (ADX 1H):")
    for reg in ("trend", "range", "mixed", "n/a"):
        block([r for r in trades if r["regime"] == reg], reg)
    print(">>> ПО ПРИЧИНЕ ВЫХОДА:")
    for rs in ("tp_hit", "flow_exit", "flow_scratch", "sl_hit"):
        block([r for r in trades if r["reason"] == rs], rs)
    # контрфактуал: судьба сделок, которые БЫЛИ БЫ скретчнуты, но мы дали им жить
    cf = [r for r in trades if r.get("would_scratch")]
    if cf:
        print(f"\n>>> КОНТРФАКТУАЛ flow_scratch (n={len(cf)} «скретч-кандидатов»):")
        scratch_net = sum(r["scratch_R"] for r in cf)       # что дал бы скретч
        natural_net = sum(r["net_R"] for r in cf)            # что дало удержание
        print(f"  Если РЕЗАТЬ (scratch): netR={scratch_net:>+7.1f} "
              f"(avg {scratch_net/len(cf):>+5.2f})")
        print(f"  Если ДЕРЖАТЬ (отскок): netR={natural_net:>+7.1f} "
              f"(avg {natural_net/len(cf):>+5.2f})")
        diff = natural_net - scratch_net
        verdict = "СКРЕТЧ ТЕРЯЕТ деньги (надо держать)" if diff > 0 \
            else "СКРЕТЧ СПАСАЕТ деньги (резать верно)"
        print(f"  Δ(держать−резать) = {diff:>+7.1f}R → {verdict}")
        print("  Естественная судьба скретч-кандидатов (если держать):")
        for rs in ("tp_hit", "flow_exit", "sl_hit"):
            block([r for r in cf if r["reason"] == rs], rs)


def _argf(flag, default):
    if flag in sys.argv:
        return float(sys.argv[sys.argv.index(flag) + 1])
    return default


def main():
    syms = sys.argv[1].split(",")
    start, end = sys.argv[2], sys.argv[3]
    # оверрайды порогов выхода для sweep (OOS-исследование, не тюнинг под live)
    fe = _argf("--fe-activate", None)        # flow_exit_activate_r
    sa = _argf("--scratch-adverse", None)    # scratch_min_adverse_r
    upd = {"require_ob_imbalance": False}
    if fe is not None:
        upd["flow_exit_activate_r"] = fe
    if sa is not None:
        upd["scratch_min_adverse_r"] = sa
    if "--no-scratch" in sys.argv:
        upd["scratch_on_flow_flip"] = False
    cfg = load_settings().model_copy(update=upd)
    sweep = "--sweep" in sys.argv
    fmode = sys.argv[sys.argv.index("--filter") + 1] if "--filter" in sys.argv else "none"
    adx_thresh = _argf("--adx-thresh", 25.0)
    sl_cd = _argf("--sl-cooldown", 0.0)
    print(f"конфиг: fe_activate={cfg.flow_exit_activate_r}R "
          f"scratch_adverse={cfg.scratch_min_adverse_r}R "
          f"scratch_on={cfg.scratch_on_flow_flip} tp={cfg.take_profit_r}R "
          f"ob-гейт=ВЫКЛ | filter={fmode} (adx_thresh={adx_thresh})")
    all_trades = []
    for sym in syms:
        ticks = []
        for day in daterange(start, end):
            ticks.extend(fetch_day(sym, day))
        ticks.sort(key=lambda x: x[0])
        if not ticks:
            print(f"{sym}: нет тиков"); continue
        raw = len(ticks)
        ticks = bin_ticks(ticks, 0.25)
        regime_at = load_regime(sym, start, end)
        scratch_cf = "--scratch-cf" in sys.argv
        tr = replay(sym, ticks, cfg, regime_at, scratch_cf=scratch_cf,
                    filter_mode=fmode, adx_thresh=adx_thresh, sl_cooldown=sl_cd)
        all_trades.extend(tr)
        if not sweep:
            net = sum(r["net_R"] for r in tr)
            print(f"{sym}: тиков={raw:>9}→бинов={len(ticks):>8} сделок={len(tr):>4} "
                  f"netR={net:>+7.1f}")
    if sweep:
        t = all_trades
        net = sum(r["net_R"] for r in t)
        w = len([r for r in t if r["net_R"] > 0])
        fx = [r for r in t if r["reason"] == "flow_exit"]
        print(f"SWEEP fe={cfg.flow_exit_activate_r} sa={cfg.scratch_min_adverse_r} "
              f"scr={cfg.scratch_on_flow_flip} | n={len(t)} WR={w/max(len(t),1)*100:.0f}% "
              f"netR={net:+.0f} avgR={net/max(len(t),1):+.3f} | "
              f"flow_exit n={len(fx)} avgR={sum(r['net_R'] for r in fx)/max(len(fx),1):+.2f}")
        return
    if "--filter" in sys.argv or "--sl-cooldown" in sys.argv:
        t = all_trades
        net = sum(r["net_R"] for r in t)
        gross = sum(r["gross_R"] for r in t)
        w = len([r for r in t if r["net_R"] > 0])
        n = max(len(t), 1)
        print(f"\nFILTER={fmode} sl_cd={sl_cd:.0f}s | n={len(t)} WR={w/n*100:.0f}% "
              f"netR={net:+.0f} (avg {net/n:+.3f}) grossR={gross:+.0f} (avg {gross/n:+.3f})")
        return
    report(all_trades)


if __name__ == "__main__":
    main()
