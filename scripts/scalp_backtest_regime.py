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
import math
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


def load_regime(symbol: str, start: str, end: str, htf_interval: str = "60",
                slope_lookback: int = 5):
    """Возвращает regime_at(ts)->('trend'|'range'|'mixed', adx, htf_dir, slope_pct),
    где htf_dir ∈ {'long','short','n/a'} — направление по EMA200 на ``htf_interval``
    (как прод-фильтр require_htf_trend), slope_pct — нормированный наклон EMA200
    ((ema[i]-ema[i-lk])/close[i])×100 за ``slope_lookback`` баров (research:
    TradingView «EMA Slope Pro» — нормировка на close даёт cross-instrument
    сопоставимость). Для A/B HTF-фильтра (EMA200-1H vs EMA200-15m: research — для
    скальпа контекст 15m, ChartScout/DYOR/VWAP-guide 2026). interval в минутах."""
    from pybit.unified_trading import HTTP
    sess = HTTP(testnet=False)
    interval_ms = int(htf_interval) * 60_000
    # warmup ≥200 баров: 15 дней с запасом для 1H (≈8д) и 15m (≈2д)
    start_ms = int(date.fromisoformat(start).strftime("%s")) * 1000 - 15 * 86400_000
    end_ms = (int(date.fromisoformat(end).strftime("%s")) + 86400) * 1000
    rows = []
    cur = start_ms
    while cur < end_ms:
        r = sess.get_kline(category="linear", symbol=symbol, interval=htf_interval,
                           start=cur, limit=1000)["result"]["list"]
        if not r:
            break
        rows.extend(r)
        oldest = min(int(x[0]) for x in r)
        newest = max(int(x[0]) for x in r)
        if newest <= cur:
            break
        cur = newest + interval_ms
    # уникализируем и сортируем по времени (Bybit отдаёт свежие первыми)
    kl = sorted({int(x[0]): x for x in rows}.values(), key=lambda x: int(x[0]))
    if len(kl) < 30:
        return lambda ts: ("n/a", 0.0, "n/a", 0.0, 0.0, "n/a")
    ts_arr = [int(x[0]) / 1000 for x in kl]
    high = [float(x[2]) for x in kl]
    low = [float(x[3]) for x in kl]
    close = [float(x[4]) for x in kl]
    adx, pdi, ndi = _adx(high, low, close, 14)
    ema200 = _ema(close, 200)
    lk = max(1, slope_lookback)

    # SMA20/std20 для Z-score (extreme-deviation gate, канон Bollinger 20,2 /
    # Keltner — фейд только на статэкстремуме; не-EMA, не страдает от лага тренда)
    W = 20
    sma20 = [0.0] * len(close)
    std20 = [0.0] * len(close)
    for i in range(len(close)):
        if i + 1 >= W:
            seg = close[i - W + 1:i + 1]
            m = sum(seg) / W
            sma20[i] = m
            std20[i] = (sum((x - m) ** 2 for x in seg) / W) ** 0.5

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
        slope = 0.0
        if e > 0 and i >= lk and ema200[i - lk] > 0 and close[i] > 0:
            slope = (ema200[i] - ema200[i - lk]) / close[i] * 100.0
        zdev = 0.0
        if std20[i] > 0:
            zdev = (close[i] - sma20[i]) / std20[i]
        di_dir = "long" if pdi[i] > ndi[i] else "short"  # Wilder DMI направление
        return reg, a, htf, slope, zdev, di_dir
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
    return adx, pdi, ndi  # pdi/ndi — направление (Wilder DMI), быстрее EMA-кросса


# ─── replay одного символа ─────────────────────────────────────────────────
def _blocked(side: str, htf: str, adx: float, mode: str, adx_thresh: float,
             slope: float = 0.0, slope_thresh: float = 0.05) -> bool:
    """Фильтр входа. ema: направленный EMA200 (как прод require_htf_trend) — фейд
    только ПО тренду. adx: режим-гейт по СИЛЕ тренда (канон MR). slope: вето на
    фейд против НАКЛОНА EMA — лечит лаг EMA (цена прокалывает лаговую EMA на
    отскоке, но EMA падает → лонг-фейд режем). none: без фильтра.

    Комбо-режимы: ema+adx, ema+slope, ema+adx+slope (additive-гейты, канон
    профи — слои фильтров: направление+сила+наклон, CryptoProfitCalc 2026)."""
    if mode.startswith("ema"):
        # 1) направление (price vs EMA200) — базовый require_htf_trend
        if htf == "long" and side == "short":
            return True
        if htf == "short" and side == "long":
            return True
        # 2) ADX-режим (сила): фейд запрещён в сильный тренд (Connors/Raschke,
        #    Dalton: «never fade a one-timeframe trending market»)
        if "adx" in mode and adx >= adx_thresh:
            return True
        # 3) slope-вето (наклон EMA): даже если price>EMA (htf=long, но это лаговый
        #    отскок), при падающей EMA (slope<=-thresh) лонг-фейд режем; зеркально
        #    для short. Плоский наклон (|slope|<thresh) = range → фейд разрешаем
        #    (родная среда MR). Research: EMA как bias + slope-фильтр, flat=neutral
        #    (CryptoProfitCalc/PipRider/FXNX 2026; TradingView EMA Slope Pro).
        if "slope" in mode:
            if side == "long" and slope <= -slope_thresh:
                return True
            if side == "short" and slope >= slope_thresh:
                return True
        return False
    if mode == "adx":
        return adx >= adx_thresh
    return False


# ─── тег значимости свипнутого уровня (тест канон-разрыва №1) ───────────────
# Реплика near_round_hier из analysis/strategies.py (канон Osler 2003 / Данилов:
# стопы кластеризуются на видимых уровнях — круглых и PDH/PDL). НЕ подгонка:
# та же формула шага 10^(порядок−1), что в проде; PDH/PDL — предыдущий UTC-день.
def _round_tier(price: float, frac: float) -> str | None:
    if price <= 0:
        return None
    step = 10.0 ** (math.floor(math.log10(price)) - 1)
    if step <= 0:
        return None

    def _near(s: float) -> bool:
        nearest = round(price / s) * s
        return abs(price - nearest) <= frac * price
    if _near(step):
        return "round00"
    if _near(step / 2.0):
        return "round50"
    return None


def _near_level(price: float, level: float | None, frac: float) -> bool:
    return level is not None and level > 0 and abs(price - level) <= frac * price


def replay(symbol: str, ticks: list, cfg, regime_at, scratch_cf: bool = False,
           filter_mode: str = "none", adx_thresh: float = 25.0,
           sl_cooldown: float = 0.0, session_hours: set | None = None,
           slippage_bps: float = 0.0, post_out: list | None = None,
           post_window_sec: float = 1800.0, slope_thresh: float = 0.05,
           regime_at2=None, z_thresh: float = 0.0,
           dir_di: bool = False, dir_di_longs: bool = False) -> list[dict]:
    """slippage_bps>0: стресс-модель транзакционных издержек. Каждое плечо
    (entry+exit) кросит ``slippage_bps`` б.п. цены сверх комиссии — round-trip
    стоимость = fee + 2·slippage_bps/1e4 (доля нотионала). Канон cost-sensitivity
    (Roll 1984 effective spread; market-order пересекает спред + impact на тонкой
    книге). Path-эффект (TP/SL сдвиг от худшего филла) — 2-го порядка, не
    моделируем; net_R интерпретируем как cost-adjusted edge. Цель — найти
    breakeven-слиппедж по монете: переживёт ли edge реальную книгу."""
    """sl_cooldown>0: после выхода по SL блокируем НОВЫЙ вход в ТУ ЖЕ сторону на
    sl_cooldown секунд (канон: не перефейдивать провалившийся уровень сразу,
    Connors/Raschke). Противоположную сторону не трогаем."""
    """scratch_cf=True: КОНТРФАКТУАЛ — не закрываем по flow_scratch, а помечаем
    момент (would_scratch, scratch_R) и даём сделке дойти до естественного конца
    (TP/SL/flow_exit). Так видим: скретч спас от SL или зарезал отскок к TP."""
    """post_out!=None: STOP-REVERSE диагностика. После каждого sl_hit ставим
    «наблюдателя» на post_window_sec секунд и меряем, ушла ли цена в нашу сторону
    уже ПОСЛЕ стопа (MFE от стопа и от входа в R, дошла ли до исходного TP).
    Отвечает: систематически ли стопы выносят перед движением (stop-hunt rate)."""
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
    post_watch: list = []  # активные stop-reverse наблюдатели
    day_hl: dict[int, list[float]] = {}  # день(UTC)->[lo,hi] для PDH/PDL
    lvl_frac = getattr(cfg, "density_round_frac", 0.003)  # та же близость, что round-гейт
    for ts, side, size, price in ticks:
        clk.t = ts
        state.on_trade(price, size, side)
        _d = int(ts // 86400)
        hl = day_hl.get(_d)
        if hl is None:
            day_hl[_d] = [price, price]
        else:
            if price < hl[0]:
                hl[0] = price
            if price > hl[1]:
                hl[1] = price
        if post_out is not None and post_watch:
            still = []
            for w in post_watch:
                if w["side"] == "short":
                    w["lo"] = min(w["lo"], price)
                else:
                    w["hi"] = max(w["hi"], price)
                if ts - w["t0"] >= post_window_sec:
                    post_out.append(_finalize_post(w))
                else:
                    still.append(w)
            post_watch = still
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
                trades.append(_close(pos, hit[0], hit[1], ts, fee, slippage_bps))
                if hit[0] == "sl_hit":
                    last_sl_ts[pos["side"]] = ts
                    if post_out is not None:
                        post_watch.append({
                            "side": pos["side"], "entry": pos["entry"],
                            "sl": hit[1], "tp": pos["tp"],
                            "risk": pos["risk"] or 1e-9, "t0": ts,
                            "lo": hit[1], "hi": hit[1]})
                pos = None
        if ts < eval_next:
            continue
        eval_next = ts + cfg.eval_interval_sec
        snap = state.snapshot()
        if pos is None:
            sig = strat.update(snap, ts)
            if sig is not None:
                reg, adx, htf, slope, zdev, di_dir = regime_at(ts)
                # источник направления: DMI везде / DMI только для лонгов / EMA
                if dir_di:
                    dir_h = di_dir
                elif dir_di_longs and sig.side == "long":
                    # асимметрия: лонг блокируем если EMA ИЛИ DMI смотрят вниз;
                    # шорты остаются на чистом EMA (там EMA уже хорош)
                    dir_h = "short" if (htf == "short" or di_dir == "short") else htf
                else:
                    dir_h = htf
                if _blocked(sig.side, dir_h, adx, filter_mode, adx_thresh,
                            slope, slope_thresh):
                    continue  # фильтр входа зарезал сигнал
                if regime_at2 is not None:  # MTF-согласие: 2-й ТФ тоже за фейд
                    htf2 = regime_at2(ts)[2]
                    if (htf2 == "long" and sig.side == "short") or \
                       (htf2 == "short" and sig.side == "long"):
                        continue
                if z_thresh > 0:  # extreme-deviation: фейд только на статэкстремуме
                    if sig.side == "long" and zdev > -z_thresh:
                        continue  # цена недостаточно НИЗКО (не дип)
                    if sig.side == "short" and zdev < z_thresh:
                        continue  # цена недостаточно ВЫСОКО (не пик)
                if sl_cooldown > 0 and ts - last_sl_ts[sig.side] < sl_cooldown:
                    continue  # cooldown после SL в ту же сторону
                if session_hours is not None:
                    hr = int((ts % 86400.0) // 3600.0)  # UTC-час входа
                    if hr not in session_hours:
                        continue  # вне активной торговой сессии
                prev = day_hl.get(int(ts // 86400) - 1)  # PDH/PDL пред. UTC-дня
                pdl, pdh = (prev[0], prev[1]) if prev else (None, None)
                ep = sig.entry_ref
                pos = {"side": sig.side, "entry": ep, "sl": sig.sl_level,
                       "tp": sig.tp_level, "ts_open": ts, "regime": reg, "adx": adx,
                       "risk": abs(sig.entry_ref - sig.sl_level),
                       "lvl_round": _round_tier(ep, lvl_frac),
                       "lvl_pdhpdl": (_near_level(ep, pdh, lvl_frac)
                                      or _near_level(ep, pdl, lvl_frac))}
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
                    trades.append(_close(pos, ex[0], ex[1], ts, fee, slippage_bps))
                    pos = None
    if post_out is not None:  # финализируем хвост наблюдателей (окно не истекло)
        for w in post_watch:
            post_out.append(_finalize_post(w))
    return trades


def _finalize_post(w: dict) -> dict:
    """Итог stop-reverse наблюдателя: насколько цена ушла в нашу сторону после
    стопа (MFE от стопа и от входа, в R по исходному risk), дошла ли до TP."""
    risk = w["risk"]
    if w["side"] == "short":
        ext = w["lo"]
        fav_from_stop = w["sl"] - ext
        fav_from_entry = w["entry"] - ext
        hit_tp = ext <= w["tp"]
    else:
        ext = w["hi"]
        fav_from_stop = ext - w["sl"]
        fav_from_entry = ext - w["entry"]
        hit_tp = ext >= w["tp"]
    return {"side": w["side"], "mfe_from_stop_R": fav_from_stop / risk,
            "mfe_from_entry_R": fav_from_entry / risk, "hit_tp": hit_tp}


def _close(pos, reason, exit_price, ts, fee, slippage_bps=0.0):
    e = pos["entry"]; risk = pos["risk"] or 1e-9
    fav = (exit_price - e) if pos["side"] == "long" else (e - exit_price)
    cost = fee + 2.0 * slippage_bps / 1e4  # round-trip: fee + слиппедж на оба плеча
    gross_R = fav / risk
    net_frac = fav / e - cost
    net_R = (fav - cost * e) / risk
    return {"regime": pos["regime"], "adx": pos["adx"], "side": pos["side"],
            "reason": reason, "gross_R": gross_R, "net_R": net_R,
            "net_frac": net_frac, "hold": ts - pos["ts_open"],
            "e_over_risk": e / risk,  # для аналитического slip-sweep
            "lvl_round": pos.get("lvl_round"),
            "lvl_pdhpdl": pos.get("lvl_pdhpdl", False),
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
    tp_r = _argf("--tp-r", None)             # fixed take_profit_r override (OOS-свип)
    if tp_r is not None:
        upd["take_profit_r"] = tp_r
    cb = _argf("--confirm-bar", None)        # confirm_bar_sec (v0.14.0: 60→0 tape)
    if cb is not None:
        upd["confirm_bar_sec"] = cb
    slb = _argf("--sl-buffer-bps", None)     # буфер SL за свип-уровнем (б.п.)
    if slb is not None:
        upd["sl_buffer_bps"] = slb
    slm = _argf("--sl-mult", None)           # множитель ширины SL (гипотеза шире-стоп)
    if slm is not None:
        upd["sl_risk_mult"] = slm
    rf = _argf("--reclaim-frac", None)       # доля reclaim (CAP Rule 2: база 0.5 → канон 1.0)
    if rf is not None:
        upd["reclaim_frac"] = rf
    cfg = load_settings().model_copy(update=upd)
    sweep = "--sweep" in sys.argv
    fmode = sys.argv[sys.argv.index("--filter") + 1] if "--filter" in sys.argv else "none"
    adx_thresh = _argf("--adx-thresh", 25.0)
    slope_thresh = _argf("--slope-thresh", 0.05)
    slope_lk = int(_argf("--slope-lookback", 5))
    sl_cd = _argf("--sl-cooldown", 0.0)
    slip_bps = _argf("--slippage-bps", 0.0)
    # default ДОЛЖЕН совпадать с прод (settings.htf_interval="15", v0.16.0):
    # иначе прогон без флага молча тестирует старый 1H-конфиг, не прод.
    htf_iv = (sys.argv[sys.argv.index("--htf-interval") + 1]
              if "--htf-interval" in sys.argv else "15")
    # MTF-согласие: 2-й (старший) ТФ EMA200 тоже должен быть за фейд (анти
    # контртренд-лонг на whipsaw 15m). None = выкл.
    mtf_iv = (sys.argv[sys.argv.index("--mtf-interval") + 1]
              if "--mtf-interval" in sys.argv else None)
    z_thresh = _argf("--z-thresh", 0.0)      # extreme-deviation gate (Z vs SMA20)
    dir_di = "--dir-di" in sys.argv          # направление по DMI (+DI/-DI) вместо EMA
    dir_di_longs = "--dir-di-longs" in sys.argv  # DMI-подтверждение только для лонгов
    stop_rev = "--stop-reverse" in sys.argv
    post_win = _argf("--post-window", 1800.0)
    session_hours = None
    if "--session-hours" in sys.argv:
        raw = sys.argv[sys.argv.index("--session-hours") + 1]
        session_hours = {int(h) for h in raw.split(",") if h.strip()}
    print(f"конфиг: fe_activate={cfg.flow_exit_activate_r}R "
          f"scratch_adverse={cfg.scratch_min_adverse_r}R "
          f"scratch_on={cfg.scratch_on_flow_flip} tp={cfg.take_profit_r}R "
          f"reclaim_frac={cfg.reclaim_frac} "
          f"ob-гейт=ВЫКЛ | filter={fmode} (adx_thresh={adx_thresh} "
          f"slope_thresh={slope_thresh}% lk={slope_lk}) "
          f"htf_tf={htf_iv}m slippage={slip_bps}bps/side")
    slip_sweep = "--slip-sweep" in sys.argv
    per_coin: dict[str, list[dict]] = {}
    post_all: list = []
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
        regime_at = load_regime(sym, start, end, htf_iv, slope_lk)
        regime_at2 = (load_regime(sym, start, end, mtf_iv, slope_lk)
                      if mtf_iv else None)
        scratch_cf = "--scratch-cf" in sys.argv
        tr = replay(sym, ticks, cfg, regime_at, scratch_cf=scratch_cf,
                    filter_mode=fmode, adx_thresh=adx_thresh, sl_cooldown=sl_cd,
                    session_hours=session_hours, slippage_bps=slip_bps,
                    post_out=(post_all if stop_rev else None),
                    post_window_sec=post_win, slope_thresh=slope_thresh,
                    regime_at2=regime_at2, z_thresh=z_thresh, dir_di=dir_di,
                    dir_di_longs=dir_di_longs)
        all_trades.extend(tr)
        per_coin[sym] = tr
        if slip_sweep:
            continue
        if not sweep:
            net = sum(r["net_R"] for r in tr)
            w = len([r for r in tr if r["net_R"] > 0])
            n = max(len(tr), 1)
            print(f"{sym}: тиков={raw:>9}→бинов={len(ticks):>8} сделок={len(tr):>4} "
                  f"WR={w/n*100:>3.0f}% netR={net:>+7.1f} avgR={net/n:>+6.3f}")
    if stop_rev:
        tp_r = cfg.take_profit_r
        n = len(post_all)
        if n == 0:
            print("нет SL-выходов для анализа"); return
        from_stop = sorted(r["mfe_from_stop_R"] for r in post_all)
        hit_tp = sum(1 for r in post_all if r["hit_tp"])
        ge = lambda thr: sum(1 for r in post_all if r["mfe_from_stop_R"] >= thr)

        def pct(k):
            return f"{k/n*100:.0f}%"
        med = from_stop[n // 2]
        avg = sum(from_stop) / n
        print(f"\n===== STOP-REVERSE (filter={fmode} htf_tf={htf_iv}m, "
              f"окно после стопа={post_win:.0f}с) =====")
        print(f"SL-выходов: n={n} | TP={tp_r}R, SL=1R")
        print(f"\nДошла бы до ИСХОДНОГО TP после стопа: {hit_tp} ({pct(hit_tp)})")
        print(f"  → во столько случаев стоп выбил перед полным движением к TP\n")
        print("MFE ПОСЛЕ стопа (как далеко ушла в нашу сторону ОТ цены стопа), в R:")
        print(f"  среднее {avg:+.2f}R | медиана {med:+.2f}R")
        print(f"  ушла ≥0.5R после стопа: {ge(0.5)} ({pct(ge(0.5))})")
        print(f"  ушла ≥1.0R после стопа: {ge(1.0)} ({pct(ge(1.0))})")
        print(f"  ушла ≥2.0R после стопа: {ge(2.0)} ({pct(ge(2.0))})")
        print(f"  ушла ≥{tp_r}R (=TP) после стопа: {ge(tp_r)} ({pct(ge(tp_r))})")
        print("\nЧтение: высокий % ≥1R = нас часто выносят прямо перед движением")
        print("(stop-hunt). ~0% = стопы срабатывают по делу (сетап реально сломан).")
        return
    if slip_sweep:
        fee = cfg.round_trip_fee_frac
        levels = [0.0, 1.0, 2.0, 3.0, 5.0, 8.0]  # bps на сторону

        def net_at(rows, s):
            c = fee + 2.0 * s / 1e4
            return sum(r["gross_R"] - c * r["e_over_risk"] for r in rows)

        def breakeven(rows):
            sg = sum(r["gross_R"] for r in rows)
            se = sum(r["e_over_risk"] for r in rows)
            if se <= 0:
                return None
            return (sg / se - fee) * 1e4 / 2.0  # bps/side где total net=0

        print(f"\n===== SLIP-SWEEP (filter={fmode}) avg net_R/сделку по слиппеджу =====")
        hdr = "  ".join(f"{int(s)}bp" for s in levels)
        print(f"{'symbol':10} {'n':>4}  {hdr}   {'breakeven':>10}")
        for sym in syms:
            rows = per_coin.get(sym) or []
            if not rows:
                print(f"{sym:10} n=0"); continue
            n = len(rows)
            cells = "  ".join(f"{net_at(rows, s)/n:>+4.2f}" for s in levels)
            be = breakeven(rows)
            bestr = f"{be:>+7.1f}bp" if be is not None else "   n/a"
            print(f"{sym:10} {n:>4}  {cells}   {bestr:>10}")
        allrows = all_trades
        n = max(len(allrows), 1)
        cells = "  ".join(f"{net_at(allrows, s)/n:>+4.2f}" for s in levels)
        be = breakeven(allrows)
        print(f"{'ALL':10} {len(allrows):>4}  {cells}   "
              f"{(f'{be:>+7.1f}bp' if be is not None else 'n/a'):>10}")
        print("\nbreakeven = слиппедж (bps/сторону), при котором edge монеты → 0.")
        print("сравни с реальным спредом монеты: если breakeven >> спреда — edge живой.")
        return
    if "--by-side" in sys.argv:
        t = all_trades
        print(f"\n===== BY-SIDE (filter={fmode} htf_tf={htf_iv}m) =====")
        print(f"{'side':6} {'n':>4} {'WR':>4} {'netR':>7} {'avgR':>7} "
              f"{'SLn':>4} {'SLshare':>7} {'FXn':>4} {'FXavgR':>7}")
        for sd in ("long", "short"):
            g = [r for r in t if r["side"] == sd]
            n = len(g)
            if not n:
                print(f"{sd:6} n=0"); continue
            w = sum(1 for r in g if r["net_R"] > 0)
            net = sum(r["net_R"] for r in g)
            sl = [r for r in g if r["reason"] == "sl_hit"]
            fx = [r for r in g if r["reason"] == "flow_exit"]
            fxavg = sum(r["net_R"] for r in fx) / max(len(fx), 1)
            print(f"{sd:6} {n:>4} {w/n*100:>3.0f}% {net:>+7.1f} {net/n:>+7.3f} "
                  f"{len(sl):>4} {len(sl)/n*100:>6.0f}% {len(fx):>4} {fxavg:>+7.2f}")
        return
    if "--by-symbol-side" in sys.argv:
        print(f"\n===== BY-SYMBOL×SIDE (filter={fmode} htf_tf={htf_iv}m) =====")
        print(f"{'symbol':10} {'side':5} {'n':>4} {'WR':>4} {'netR':>7} "
              f"{'avgR':>7} {'SLshare':>7}")
        for sym in syms:
            rows = per_coin.get(sym) or []
            for sd in ("long", "short"):
                g = [r for r in rows if r["side"] == sd]
                n = len(g)
                if not n:
                    print(f"{sym:10} {sd:5} n=0"); continue
                w = sum(1 for r in g if r["net_R"] > 0)
                net = sum(r["net_R"] for r in g)
                sl = sum(1 for r in g if r["reason"] == "sl_hit")
                print(f"{sym:10} {sd:5} {n:>4} {w/n*100:>3.0f}% {net:>+7.1f} "
                      f"{net/n:>+7.3f} {sl/n*100:>6.0f}%")
        return
    if "--adx-buckets" in sys.argv:
        t = all_trades

        def bk(a):
            if a is None:
                return "n/a"
            if a < 20:
                return "0 <20 range"
            if a < 25:
                return "1 20-25 gray"
            if a < 30:
                return "2 25-30 trend"
            return "3 >=30 strong"
        from collections import defaultdict
        G: dict[str, list] = defaultdict(list)
        for r in t:
            G[bk(r.get("adx"))].append(r)
        print(f"\n===== ADX-BUCKETS (filter={fmode} htf_tf={htf_iv}m, ADX@вход) =====")
        print(f"{'bucket':14} {'n':>4} {'WR':>4} {'netR':>7} {'avgR':>7} "
              f"{'SLn':>4} {'SLshare':>7} {'SLnetR':>7} {'FXn':>4} {'FXavgR':>7}")
        for k in sorted(G):
            g = G[k]
            n = len(g)
            w = sum(1 for r in g if r["net_R"] > 0)
            net = sum(r["net_R"] for r in g)
            sl = [r for r in g if r["reason"] == "sl_hit"]
            fx = [r for r in g if r["reason"] == "flow_exit"]
            slnet = sum(r["net_R"] for r in sl)
            fxavg = sum(r["net_R"] for r in fx) / max(len(fx), 1)
            print(f"{k:14} {n:>4} {w/max(n,1)*100:>3.0f}% {net:>+7.1f} "
                  f"{net/max(n,1):>+7.3f} {len(sl):>4} {len(sl)/max(n,1)*100:>6.0f}% "
                  f"{slnet:>+7.1f} {len(fx):>4} {fxavg:>+7.2f}")
        return
    if "--level-decomp" in sys.argv:
        t = all_trades

        def lblk(label, rows):
            n = len(rows)
            if not n:
                print(f"  {label:<22} n=0"); return
            w = sum(1 for r in rows if r["net_R"] > 0)
            net = sum(r["net_R"] for r in rows)
            print(f"  {label:<22} n={n:>4} WR={w/n*100:>3.0f}% "
                  f"netR={net:>+7.1f} avgR={net/n:>+6.3f}")
        print(f"\n===== LEVEL-DECOMP (filter={fmode} htf_tf={htf_iv}m) — "
              f"тест канон-разрыва №1: WR у значимого уровня vs микро =====")
        print(f"близость = {getattr(cfg,'density_round_frac',0.003)*100:.2f}% цены "
              f"(round-tier + PDH/PDL пред. дня)")
        print(">>> ROUND-уровень (Osler/Данилов):")
        lblk("round00", [r for r in t if r["lvl_round"] == "round00"])
        lblk("round50", [r for r in t if r["lvl_round"] == "round50"])
        lblk("любой round", [r for r in t if r["lvl_round"]])
        lblk("НЕ round (микро)", [r for r in t if not r["lvl_round"]])
        print(">>> PDH/PDL (пред. UTC-день):")
        lblk("у PDH/PDL", [r for r in t if r["lvl_pdhpdl"]])
        lblk("НЕ у PDH/PDL", [r for r in t if not r["lvl_pdhpdl"]])
        print(">>> ЗНАЧИМЫЙ (round ИЛИ PDH/PDL) vs микро:")
        sig_ = [r for r in t if r["lvl_round"] or r["lvl_pdhpdl"]]
        lblk("значимый уровень", sig_)
        lblk("микро-экстремум", [r for r in t if not (r["lvl_round"] or r["lvl_pdhpdl"])])
        print(f"\nВсего сделок: {len(t)}")
        return
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
    if "--filter" in sys.argv or "--sl-cooldown" in sys.argv \
            or "--session-hours" in sys.argv:
        t = all_trades
        net = sum(r["net_R"] for r in t)
        gross = sum(r["gross_R"] for r in t)
        w = len([r for r in t if r["net_R"] > 0])
        n = max(len(t), 1)
        sh = "24h" if session_hours is None else f"{len(session_hours)}h"
        print(f"\n--- per-coin (filter={fmode} htf_tf={htf_iv}m) ---")
        for sym in syms:
            rows = per_coin.get(sym) or []
            if not rows:
                print(f"{sym:10} n=0"); continue
            cn = len(rows); cnet = sum(r["net_R"] for r in rows)
            cw = len([r for r in rows if r["net_R"] > 0])
            print(f"{sym:10} n={cn:>4} WR={cw/cn*100:>3.0f}% "
                  f"netR={cnet:>+7.1f} avgR={cnet/cn:>+6.3f}")
        print(f"\nFILTER={fmode} htf_tf={htf_iv}m sl_cd={sl_cd:.0f}s sess={sh} | "
              f"n={len(t)} WR={w/n*100:.0f}% netR={net:+.0f} (avg {net/n:+.3f}) "
              f"grossR={gross:+.0f} (avg {gross/n:+.3f})")
        return
    report(all_trades)


if __name__ == "__main__":
    main()
