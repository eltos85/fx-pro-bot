"""Авто-селектор торговой вселенной flowzone_bot.

Переиспользует методику scalp_bot (TASKSPEC §4): раз в ``universe_refresh_sec``
бот сам выбирает монеты, а не торгует хардкод-список. Два источника:
- Bybit ``get_tickers`` (24h snapshot) — hard-фильтр ликвидность/спред/анти-памп.
  https://bybit-exchange.github.io/docs/v5/market/tickers
- Bybit ``get_kline`` 5м — свежий intraday RVOL по амплитуде (что «в игре
  сейчас»), гейт + ранжирование.

КАЛИБРОВКА ПОД КАНОН (TASKSPEC §4): канон flowzone демонстрировался на NQ —
глубоко-ликвидном рынке; absorption/footprint читаемы только на ликвидности
(STRATEGY §6.1). Селектор уже имеет стражи ликвидности (turnover-floor + spread-
cap). Если на форвард-тесте footprint «шумит» на тонких монетах — сместить отбор
в сторону ликвидности через env-пороги, НЕ новой логикой и НЕ заранее
(no-data-fitting.mdc).

ФИЛЬТРЫ (hard): range% (амплитуда), turnover24h (ликвидность), range cap
(анти-памп >20%/день — stoic.ai 2026), spread cap (скрытая комиссия).
РАНЖИРОВАНИЕ (композитный скор): score = W_VOL·vol_n + W_LIQ·liq_n +
W_SPREAD·(1−spread_n) — ликвидность и волатильность co-equal (Volity 2026).
"""
from __future__ import annotations

# Веса композитного скора (research: ликвидность ≈ волатильность по важности;
# спред уже отсечён hard-фильтром, поэтому малый вес как тонкий tie-break).
W_VOL = 0.45
W_LIQ = 0.45
W_SPREAD = 0.10


def _f(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _norm(vals: list[float]) -> list[float]:
    """Min-max нормировка в [0,1]. Если все равны (span=0) — нейтральные 1.0."""
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if span <= 0:
        return [1.0] * len(vals)
    return [(v - lo) / span for v in vals]


def score_ticker(t: dict) -> dict | None:
    """Метрики одного тикера или None если непригоден (не USDT-перп / нет полей /
    пре-маркет-листинг)."""
    sym = t.get("symbol", "") or ""
    if not sym.endswith("USDT"):
        return None
    if t.get("curPreListingPhase"):
        return None
    last = _f(t.get("lastPrice"))
    hi = _f(t.get("highPrice24h"))
    lo = _f(t.get("lowPrice24h"))
    turn = _f(t.get("turnover24h"))
    if not last or last <= 0 or hi is None or lo is None or turn is None:
        return None
    bid = _f(t.get("bid1Price"))
    ask = _f(t.get("ask1Price"))
    spread_bps = ((ask - bid) / last * 10000.0
                  if (bid and ask and ask > bid) else 0.0)
    return {"symbol": sym, "range_pct": (hi - lo) / last * 100.0,
            "turnover": turn, "spread_bps": spread_bps}


def filter_tickers(tickers: list[dict], *, min_turnover: float,
                   min_range_pct: float, max_range_pct: float,
                   max_spread_bps: float) -> list[dict]:
    """Hard-фильтр по 24h-метрикам (ликвидность/спред/анти-памп)."""
    rows: list[dict] = []
    for t in tickers or []:
        m = score_ticker(t)
        if m is None:
            continue
        if m["turnover"] < min_turnover:
            continue
        if not (min_range_pct <= m["range_pct"] <= max_range_pct):
            continue
        if max_spread_bps > 0 and m["spread_bps"] > max_spread_bps:
            continue
        rows.append(m)
    return rows


def hourly_range_rvol(kline_5m: list[list], window_bars: int = 12) -> float | None:
    """RVOL по амплитуде: текущая часовая амплитуда (rolling 1ч = последние
    ``window_bars`` 5м-баров) / медиана исторических часовых амплитуд за сутки.

    RVOL≈1 — монета двигается как обычно для себя; <1 — затихла; >1.5-2 — «в
    игре» (канон RVOL: TradingSim/Warrior 2026). Self-нормировка по СОБСТВЕННОЙ
    истории — не произвольный абсолютный порог (no-data-fitting).

    Bybit get_kline DESC (новые сверху), элемент: [start,o,h,l,c,vol,turnover].
    """
    rows = list(reversed(kline_5m or []))  # по возрастанию времени
    if len(rows) < window_bars * 2:
        return None

    def _blk_range_pct(block: list[list]) -> float | None:
        his = [_f(b[2]) for b in block]
        los = [_f(b[3]) for b in block]
        cls = _f(block[-1][4])
        if any(x is None for x in his + los) or not cls or cls <= 0:
            return None
        return (max(his) - min(los)) / cls * 100.0  # type: ignore[type-var]

    cur = _blk_range_pct(rows[-window_bars:])
    if cur is None:
        return None
    hist: list[float] = []
    end = len(rows) - window_bars
    i = end - window_bars
    while i >= 0:
        r = _blk_range_pct(rows[i:i + window_bars])
        if r is not None and r > 0:
            hist.append(r)
        i -= window_bars
    if not hist:
        return None
    hist.sort()
    n = len(hist)
    med = hist[n // 2] if n % 2 else (hist[n // 2 - 1] + hist[n // 2]) / 2
    if med <= 0:
        return None
    return cur / med


def rank_rows(rows: list[dict], *, top_n: int,
              vol_metric: dict[str, float] | None = None) -> list[str]:
    """Композитное ранжирование прошедших фильтр строк. ``vol_metric`` (если
    задан) — свежая метрика волатильности (напр. RVOL); иначе 24h range_pct.
    ``top_n`` ≤0 = без капа."""
    if not rows:
        return []
    vm = vol_metric or {}
    vol_vals = [vm.get(m["symbol"], m["range_pct"]) for m in rows]
    vol_n = _norm(vol_vals)
    liq_n = _norm([m["turnover"] for m in rows])
    spr_n = _norm([m["spread_bps"] for m in rows])
    for i, m in enumerate(rows):
        m["score"] = (W_VOL * vol_n[i] + W_LIQ * liq_n[i]
                      + W_SPREAD * (1.0 - spr_n[i]))
    rows.sort(key=lambda m: (m["score"], m["turnover"]), reverse=True)
    picked = rows if top_n <= 0 else rows[:top_n]
    return [m["symbol"] for m in picked]


def pad_universe(ranked: list[str], pool: list[dict],
                 min_symbols: int) -> list[str]:
    """Floor «минимум N монет»: если прошедших фильтр < N — добираем из ``pool``
    (кандидаты, прошедшие стражей ЛИКВИДНОСТИ: turnover/spread/range-cap;
    ослабляется только волатильностный range-floor) самых волатильных по
    range24h. ``min_symbols`` ≤0 = выключено."""
    if min_symbols <= 0 or len(ranked) >= min_symbols:
        return ranked
    have = set(ranked)
    extras = sorted((m for m in pool or [] if m["symbol"] not in have),
                    key=lambda m: m["range_pct"], reverse=True)
    out = list(ranked)
    for m in extras:
        if len(out) >= min_symbols:
            break
        out.append(m["symbol"])
    return out


def apply_pins(ranked: list[str], pinned: list[str], top_n: int) -> list[str]:
    """Force-include «пиннутых» монет в обход фильтра. Пины всегда в итоге (в
    своём порядке, дедуп), ranked добивает остаток до top_n (≤0 = без капа)."""
    pins = [p for p in dict.fromkeys(pinned) if p]
    rest = [r for r in ranked if r not in pins]
    if top_n > 0:
        rest = rest[: max(0, top_n - len(pins))]
    return pins + rest
