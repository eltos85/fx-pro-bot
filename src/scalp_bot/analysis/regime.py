"""Regime-фичи на момент входа — логирование для meta-labeling анализа.

Research basis:
    Marcos Lopez de Prado, «Advances in Financial Machine Learning» (2018,
    Wiley), Chapter 3 «Meta-Labeling». Primary model = торговый сигнал
    страты (direction решает стратегия); meta-модель = бинарный классификатор
    «брать ли сделку с учётом regime-фичей». Фичи описывают качество
    сигнального инстанса + рыночный режим (Ch3: «features from the first
    model concatenated … additional regime features not in the primary»).

    Сами индикаторы каноничны и уже в проекте:
    - ADX (Wilder 1978) — `data/htf.py` (тренд vs range).
    - regime_ratio = |close−open|/avgATR за N 15m-баров (Kaufman Efficiency
      Ratio-аналог, Connors/Raschke «MR в диапазоне, momentum в тренде») —
      `data/levels.py`.
    - CVD-slope / absorption — Kalena/chartwhisperer CAP-канон (`signals.py`).
    - session buckets — канон сессионности скальпа.
    - spread/imbalance/funding/liq — микроструктура (`data/aggregates.py`).

    Расширение v0.18.31 (по итогам анализа первых ~80 записей: гейтящиеся
    фичи страдают range restriction — их дискриминативную силу маскирует
    сам гейт; нужны НЕгейтящиеся измерители режима + shadow-лог):
    - ret_autocorr — автокорреляция lag-1 коротких ретёрнов. Lo & MacKinlay
      1988 «Stock Market Prices Do Not Follow Random Walks» (variance-ratio
      тест): отрицательная автокорреляция = mean-reversion-режим (фейду ЗА),
      положительная = momentum-режим (пробою ЗА). Прямой микро-измеритель
      «MR vs momentum», не используется ни одним гейтом → полный диапазон.
    - price_slope_bps_min — микро-тренд (LS-наклон цены за CVD-окно).
    - rv_burst — вспышка realized vol (σ ретёрнов 60с / σ за всё окно):
      volatility expansion (Bollinger 2001) — пробой хочет экспансию,
      фейд — стабильность.
    - tape_accel — ускорение ленты (trade rate 60с / rate за окно) — канон
      tape reading (плотность принтов растёт на реальном движении).
    - liq_notional_usd / liq_buy_frac — нотионал и сторона ликвидаций:
      liq_count уже показал сигнал (WR 9% при >0), сторона каскада уточняет
      (Kalena 2026: long-side каскады механически злее — market sells into
      declining bids).
    - oi_delta_pct — изменение Open Interest за окно. Murphy 1999 ch.7
      (классика фьючерсов): цена↑+OI↑ = новые деньги (continuation),
      цена↑+OI↓ = short covering (движение без топлива, фейду ЗА).
    - btc_ret_bps — импульс BTC за окно: мейджор ведёт альты (lead-lag);
      гипотеза — вход в альт во время рывка BTC токсичен для MR.
    - near_depth_imb — дисбаланс топ-5 стакана (у касания, Bookmap-канон);
      топ-25 imbalance уже логируется, у касания информативнее для скальпа.
    - htf_natr_pct — NATR(14) 15m (Wilder 1978, нормирован в % для
      кросс-символьного сравнения) — базовый уровень волатильности.
    - htf_bb_width_pct — ширина Bollinger(20, 2σ) 15m в % от SMA
      (Bollinger 2001; Carter 2012 «squeeze» — сжатие предшествует
      экспансии, precondition для density_break).

НАЗНАЧЕНИЕ: ТОЛЬКО логирование. Фичи пишутся в отдельную таблицу
``regime_features`` на каждый вход (и в ``shadow_signals`` на каждый
ОТВЕРГНУТЫЙ гейтом сигнал) и НЕ влияют на торговую логику
(no-data-fitting.mdc: «Добавление метрик/логирования (не влияют на торговлю)»
— допустимо без выборки). Анализ (условный разрез E[trade|regime] на n≥100 +
OOS по Lopez de Prado Ch7/Ch11) делается офлайн в ``scripts/``, по результатам
гейт предлагается отдельно с research-ссылкой (strategy-guard.mdc).

Часы: сэмплы CVD и snap.ts живут на time.monotonic (клок SymbolState),
поэтому все ОКОННЫЕ вычисления используют snap.ts как «сейчас». Wall-clock
``now`` (time.time из main loop) нужен ТОЛЬКО для session-бакета.
Fix 2026-07-03: session раньше считался из snap.ts (monotonic = секунды с
загрузки хоста) — бакеты в исторических данных сдвинуты на константу
(uptime%24h); в офлайн-анализе сессию пересчитывать из колонки ts.
"""
from __future__ import annotations

import math

# 5-секундные бакеты для ретёрн-серий: publicTrade идёт неравномерно,
# автокорреляция/σ на сыром потоке смещены микроструктурным шумом
# (bid-ask bounce). 180с окно → ≤36 бакетов.
_BUCKET_SEC = 5.0
# Короткое подокно для burst/accel-метрик (последняя минута vs всё окно).
_RECENT_SEC = 60.0


def _session_bucket(utc_hour: float) -> str:
    """Сессия по UTC-часу (крипто 24/7, но ликвидность сессионная)."""
    h = int(utc_hour) % 24
    if 0 <= h < 8:
        return "asia"
    if 8 <= h < 13:
        return "europe"
    if 13 <= h < 21:
        return "us"
    return "asia_pm"


def _cvd_slope(samples) -> float | None:
    """Наклон CVD по времени (least-squares). CvdSample = (ts, price, cvd).
    None если <2 точек или нулевой разброс времени. Сырые единицы контрактов/
    сек — кросс-символьно не сравнивать напрямую, нормировать в анализе."""
    if not samples or len(samples) < 2:
        return None
    xs = [float(getattr(s, "ts", None)) for s in samples]
    ys = [float(getattr(s, "cvd", None)) for s in samples]
    if any(v is None for v in xs) or any(v is None for v in ys):
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def _bucketed_prices(samples, bucket: float = _BUCKET_SEC) -> list[float]:
    """Last-price по 5с-бакетам (пустые бакеты схлопываются — сетка
    неравномерна, для автокорреляции знаков это приемлемое приближение)."""
    out: list[float] = []
    cur_key: int | None = None
    for s in samples:
        try:
            key = int(float(s.ts) // bucket)
            px = float(s.price)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        if cur_key is None or key != cur_key:
            out.append(px)
            cur_key = key
        else:
            out[-1] = px
    return out


def _simple_returns(prices: list[float]) -> list[float]:
    return [(b - a) / a for a, b in zip(prices, prices[1:]) if a > 0]


def _autocorr1(rets: list[float], min_n: int = 10) -> float | None:
    """Автокорреляция lag-1 (Pearson x[t] vs x[t+1]). None если точек < min_n
    или нулевая дисперсия. Lo & MacKinlay 1988: <0 = mean-reversion режим."""
    if len(rets) < min_n + 1:
        return None
    x = rets[:-1]
    y = rets[1:]
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = sum((a - mx) ** 2 for a in x)
    vy = sum((b - my) ** 2 for b in y)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def _price_slope_bps_min(samples, last: float | None) -> float | None:
    """LS-наклон цены за окно, нормированный в bps/мин от текущей цены —
    микро-тренд (не гейтится, в отличие от EMA/ADX)."""
    if not samples or len(samples) < 2 or not last or last <= 0:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for s in samples:
        try:
            xs.append(float(s.ts))
            ys.append(float(s.price))
        except (TypeError, ValueError):
            return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom  # цена/сек
    return slope * 60.0 / last * 1e4


def _std(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _rv_burst(samples, ref_ts: float) -> float | None:
    """σ 5с-ретёрнов за последние 60с / σ за всё окно. >1 — vol раскрывается
    (экспансия, Bollinger 2001), <1 — затухает. None если данных мало."""
    all_prices = _bucketed_prices(samples)
    rets_all = _simple_returns(all_prices)
    recent = [s for s in samples
              if float(getattr(s, "ts", 0.0)) >= ref_ts - _RECENT_SEC]
    rets_recent = _simple_returns(_bucketed_prices(recent))
    s_all = _std(rets_all)
    s_recent = _std(rets_recent)
    if s_all is None or s_recent is None or s_all <= 0:
        return None
    if len(rets_all) < 12 or len(rets_recent) < 4:
        return None
    return s_recent / s_all


def _tape_accel(samples, ref_ts: float) -> float | None:
    """Trade rate последних 60с / rate за всё окно. >1 — лента ускоряется.
    None если окно короче 90с (ранний прогрев — рейты несравнимы)."""
    if not samples:
        return None
    try:
        first_ts = float(samples[0].ts)
    except (TypeError, ValueError):
        return None
    span = ref_ts - first_ts
    if span < 90.0:
        return None
    n_recent = sum(1 for s in samples
                   if float(getattr(s, "ts", 0.0)) >= ref_ts - _RECENT_SEC)
    rate_all = len(samples) / span
    if rate_all <= 0:
        return None
    return (n_recent / _RECENT_SEC) / rate_all


def _oi_delta_pct(oi_history) -> float | None:
    """Изменение OI за окно истории, % от начального. None если <2 точек,
    span<60с или нулевой старт. Murphy 1999 ch.7 (open interest)."""
    if not oi_history or len(oi_history) < 2:
        return None
    try:
        t0, oi0 = float(oi_history[0][0]), float(oi_history[0][1])
        t1, oi1 = float(oi_history[-1][0]), float(oi_history[-1][1])
    except (TypeError, ValueError, IndexError):
        return None
    if oi0 <= 0 or t1 - t0 < 60.0:
        return None
    return (oi1 - oi0) / oi0 * 100.0


def _btc_ret_bps(btc_snap) -> float | None:
    """Ретёрн BTC за его CVD-окно (bps, знаковый) — импульс мейджора."""
    if btc_snap is None:
        return None
    prices = _bucketed_prices(getattr(btc_snap, "cvd_samples", None) or [])
    if len(prices) < 2 or prices[0] <= 0:
        return None
    return (prices[-1] - prices[0]) / prices[0] * 1e4


def _near_depth_imb(bids, asks, levels: int = 5) -> float | None:
    """Дисбаланс топ-N стакана у касания: bid_vol/(bid+ask). Bookmap-канон —
    near-touch ликвидность информативнее полной глубины для скальпа."""
    if not bids or not asks:
        return None
    try:
        bv = sum(float(sz) for _, sz in bids[:levels])
        av = sum(float(sz) for _, sz in asks[:levels])
    except (TypeError, ValueError):
        return None
    total = bv + av
    if total <= 0:
        return None
    return bv / total


def compute_regime_features(snap, htf=None, key_levels=None, now: float = 0.0,
                            btc_snap=None) -> dict:
    """Считает regime-фичи из snap + htf + key_levels (+ btc_snap). Pure.

    Любая отсутствующая фича → None (fail-soft: анализ игнорирует null).
    Возвращает dict с фиксированным набором ключей (схема БД, REGIME_COLUMNS).

    ``now`` — wall-clock (time.time) ТОЛЬКО для session; оконные метрики
    считаются на клоке сэмплов (snap.ts, monotonic).
    """
    sym = getattr(snap, "symbol", None)
    last = getattr(snap, "last_price", None)
    bb = getattr(snap, "best_bid", None)
    ba = getattr(snap, "best_ask", None)
    imb = getattr(snap, "ob_imbalance", None)
    fund = getattr(snap, "funding_rate", None)
    cvd = getattr(snap, "cvd_samples", None) or []
    liqs = getattr(snap, "liq_events", None) or []
    bids = getattr(snap, "bids", None) or []
    asks = getattr(snap, "asks", None) or []
    oi_hist = getattr(snap, "oi_history", None) or []

    # spread (bps) — реальный кост скальпа
    spread_bps = None
    if bb is not None and ba is not None and last and last > 0:
        mid = (bb + ba) / 2.0
        if mid > 0:
            spread_bps = (ba - bb) / mid * 1e4

    # funding (bps 8h) — raw fraction × 1e4
    funding_bps = None
    if fund is not None:
        funding_bps = fund * 1e4

    # regime из kline-кэшей
    adx = None
    htf_natr = None
    htf_bbw = None
    if htf is not None:
        adx = htf.trend_strength(sym) if hasattr(htf, "trend_strength") else None
        htf_natr = htf.natr_pct(sym) if hasattr(htf, "natr_pct") else None
        htf_bbw = htf.bb_width_pct(sym) if hasattr(htf, "bb_width_pct") else None
    regime_ratio = None
    day_range_pct = None
    dist_high_pct = None
    dist_low_pct = None
    if key_levels is not None:
        regime_ratio = key_levels.regime_ratio(sym) \
            if hasattr(key_levels, "regime_ratio") else None
        lv = key_levels.levels(sym) if hasattr(key_levels, "levels") else None
        if lv is not None and last and last > 0:
            dh = lv.get("day_high")
            dl = lv.get("day_low")
            if dh is not None and dl is not None and dh > dl:
                day_range_pct = (dh - dl) / last * 100.0
                dist_high_pct = (dh - last) / last * 100.0
                dist_low_pct = (last - dl) / last * 100.0

    # session по UTC wall-clock (fix 2026-07-03: snap.ts — monotonic,
    # для сессии годится только time.time; fallback на snap.ts оставлен
    # для юнит-тестов, где ts задают как wall)
    ts = getattr(snap, "ts", None)
    wall = now if (now is not None and now > 0) else ts
    session = _session_bucket((wall / 3600.0) % 24) if wall is not None else None

    # оконные метрики на клоке сэмплов
    ref_ts = ts if ts is not None else 0.0
    liq_notional = sum(float(getattr(e, "size_usd", 0.0)) for e in liqs)
    liq_buy_frac = None
    if liq_notional > 0:
        buy = sum(float(getattr(e, "size_usd", 0.0)) for e in liqs
                  if str(getattr(e, "side", "")).upper() == "BUY")
        liq_buy_frac = buy / liq_notional

    rets = _simple_returns(_bucketed_prices(cvd))

    return {
        "adx": adx,
        "regime_ratio": regime_ratio,
        "day_range_pct": day_range_pct,
        "dist_high_pct": dist_high_pct,
        "dist_low_pct": dist_low_pct,
        "spread_bps": spread_bps,
        "ob_imbalance": imb,
        "funding_bps": funding_bps,
        "cvd_slope": _cvd_slope(cvd),
        "liq_count": len(liqs),
        "session": session,
        # ── v0.18.31: негейтящиеся измерители режима ──
        "ret_autocorr": _autocorr1(rets),
        "price_slope_bps_min": _price_slope_bps_min(cvd, last),
        "rv_burst": _rv_burst(cvd, ref_ts),
        "tape_accel": _tape_accel(cvd, ref_ts),
        "liq_notional_usd": liq_notional,
        "liq_buy_frac": liq_buy_frac,
        "oi_delta_pct": _oi_delta_pct(oi_hist),
        "btc_ret_bps": _btc_ret_bps(btc_snap),
        "near_depth_imb": _near_depth_imb(bids, asks),
        "htf_natr_pct": htf_natr,
        "htf_bb_width_pct": htf_bbw,
    }


# Порядок колонок = порядок значений в dict (для INSERT в regime_features).
# Должен совпадать с _FEATURE_COLS в state/db.py (тест-инвариант).
REGIME_COLUMNS = (
    "adx", "regime_ratio", "day_range_pct", "dist_high_pct", "dist_low_pct",
    "spread_bps", "ob_imbalance", "funding_bps", "cvd_slope", "liq_count",
    "session",
    "ret_autocorr", "price_slope_bps_min", "rv_burst", "tape_accel",
    "liq_notional_usd", "liq_buy_frac", "oi_delta_pct", "btc_ret_bps",
    "near_depth_imb", "htf_natr_pct", "htf_bb_width_pct",
)
