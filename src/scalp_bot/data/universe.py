"""Авто-селектор торговой вселенной scalp_bot.

Раз в ``universe_refresh_sec`` бот сам выбирает монеты под стратегию, а не
торгует хардкод-список. Два источника (v0.14.0):
- Bybit ``get_tickers`` (24h snapshot) — hard-фильтр ликвидность/спред/анти-памп
  (стабильные 24h-метрики), офдок: https://bybit-exchange.github.io/docs/v5/market/tickers
- Bybit ``get_kline`` 5м — СВЕЖИЙ intraday RVOL по амплитуде (что «в игре
  сейчас»), гейт+ранжирование. Канон отбора: смотреть intraday-активность, не
  лагающие 24h-суммы (RVOL guides TradingSim/Warrior, anomiq scanner 2026).

Принцип «качество, а не количество» (запрос пользователя 2026-05-31): берём
ВСЕ монеты рынка, прошедшие фильтр, а не фиксированные N. Подошло 5 — берём 5;
через 30 мин подошло 2 — берём 2. ``top_n`` — лишь safety-кап на число
WS-подписок (≤0 = без лимита).

ФИЛЬТРЫ (hard, математика fee-guard + практика скальпа, не подгонка):
- ``range% = (high24h − low24h)/last`` — амплитуда. Нужна широкая: стоп не
  бывает у́же пола ``min_risk_fee_mult × round_trip_fee_frac`` = 0.300% цены,
  а дневной range — прокси микро-волатильности свипов. (Прежняя формулировка
  ссылалась на fee-guard и порог 0.22%; замер 31.08 показал, что связывает не
  он, а этот пол — гейт не срабатывает ни разу на 47 172 сигналах,
  см. `scripts/scalp_fee_constant_impact.py`.)
  Live-граница: range 2.5–5.4% (BTC/ETH/SOL/XRP) — сигналы режутся; 9–16%
  (HYPE/NEAR/ZEC) — проходят (BUILDLOG_SCALP 2026-05-30) → floor 6%.
- ``turnover24h`` — ликвидность (грубый прокси). Floor 150M→100M (2026-05-31):
  рынок просел ~2× по обороту, и $150M стал выкидывать рабочие NEAR ($137M)/
  ZEC ($125M) с отличным спредом 0.2–0.4bps. Реальный страж ликвидности для
  скальпа — spread cap (ниже), turnover лишь отсекает совсем «пыль».
- range cap 20% — канон «>20%/день = манипуляция, избегать» (stoic.ai 2026;
  Volity: >5% ATR = hot). 30→20 (v0.12.0, канон-ревизия): 30% пропускал
  манипулятивные пампы (XLM 37%/ALLO 42%). Не подгонка под P&L — research-порог.
- spread cap (bps) — не входить в дорогих по спреду.

РАНЖИРОВАНИЕ (композитный скор, как у проф-скальперов крипты). Раньше сортировка
была чисто по range% (биас в самые «горячие»/рискованные), ликвидность — лишь
tie-break. Профи (Volity «5-filter framework», stoic.ai, dev.to trendrider 2026)
единогласно: ликвидность и волатильность co-equal, спред — «скрытая комиссия»,
съедающая edge на каждом round-trip. Поэтому скор:

    score = W_VOL·vol_n + W_LIQ·liq_n + W_SPREAD·(1 − spread_n)

где *_n — min-max нормировка метрики ВНУТРИ прошедшего фильтр пула (сравниваем
кандидатов между собой). Эффект: ликвидная монета с хорошей (не макс.)
волатильностью обходит «тонкую» гипер-волатильную — меньше слиппедж/стоп-аутов.

ВАЖНО (no-data-fitting.mdc): пороги — конфиг (env), привязаны к fee-guard и
live-границе; веса скора — research-обоснованы, а не оптимизированы под P&L.
"""
from __future__ import annotations

# Веса композитного скора (research: ликвидность ≈ волатильность по важности;
# спред уже отсечён hard-фильтром, поэтому малый вес как тонкий tie-break).
W_VOL = 0.45
W_LIQ = 0.45
W_SPREAD = 0.10

# База-стейблкоины (символ = BASE+USDT, BASE[:-4] ∈ множества). Явный blacklist:
# score_ticker без него разбирал USDCUSDT/USDEUSDT (range≈0, turnover высокий) —
# они не попадали во вселенную лишь случайно (range-floor 6% + padding сортирует
# по range DESC), но на совсем мёртвом рынке pool мог стать < min_symbols и
# стейблкоин бы торговался base sweep_fade (бессмысленно, минус на fees).
# Поддерживать при появлении новых стейблов на Bybit.
STABLE_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDE", "EUR", "USDD", "USD1",
    "USTC", "FRAX", "PYUSD", "GUSD", "USDS", "USDJ", "BCUSD", "UST", "CUSD",
    "USD0", "USDY",
})


def _f(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _norm(vals: list[float]) -> list[float]:
    """Min-max нормировка в [0,1]. Если все равны (span=0) — нейтральные 1.0
    (термин одинаков для всех → не влияет на порядок)."""
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
    if sym[:-4] in STABLE_BASES:  # стейблкоин-пара — не торгуется (range≈0)
        return None
    if t.get("curPreListingPhase"):  # пре-маркет / новый листинг — пропускаем
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
    """Hard-фильтр по 24h-метрикам (ликвидность/спред/анти-памп). Возвращает
    строки-метрики прошедших символов (без ранжирования). 24h здесь уместно:
    ликвидность и спред — стабильные величины, range-cap отсекает пампы."""
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
    истории монеты — не произвольный абсолютный порог (no-data-fitting).

    Bybit get_kline DESC (новые сверху), элемент: [start,o,h,l,c,vol,turnover].
    Возвращает rvol или None если данных мало. Обновляется каждые 5м (окно
    скользит) — свежее 24h-снимка.
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
    # исторические непересекающиеся часовые блоки ДО текущего окна
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
    задан) — свежая метрика волатильности по символу (напр. RVOL); иначе берём
    24h range_pct. ``top_n`` ≤0 = без капа."""
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


def rank_universe(tickers: list[dict], *, top_n: int, min_turnover: float,
                  min_range_pct: float, max_range_pct: float,
                  max_spread_bps: float) -> list[str]:
    """Hard-фильтр + композитное ранжирование по 24h-метрикам (без свежего RVOL).
    Тонкая обёртка filter_tickers+rank_rows — обратная совместимость."""
    rows = filter_tickers(tickers, min_turnover=min_turnover,
                          min_range_pct=min_range_pct, max_range_pct=max_range_pct,
                          max_spread_bps=max_spread_bps)
    return rank_rows(rows, top_n=top_n)


def pad_universe(ranked: list[str], pool: list[dict],
                 min_symbols: int) -> list[str]:
    """Floor «минимум N монет» (P-4, audit 2026-06-10, A-4).

    Гейты range-floor + RVOL на остывшем рынке вырождали вселенную в 1 монету
    (NEARUSDT — 44/76 сделок за сутки): концентрационный риск, а sl_cooldown
    по единственному символу запирает бота целиком. Если прошедших < N —
    добираем из ``pool`` (кандидаты, прошедшие СТРАЖЕЙ ЛИКВИДНОСТИ: turnover,
    spread cap, range-cap анти-памп; ослабляется ТОЛЬКО волатильностный
    range-floor/RVOL) самых волатильных по range24h. Стражи ликвидности не
    трогаем — это та же логика, что у apply_pins, но рыночно-нейтральная:
    добор выбирает лучших из доступных, а не конкретную монету.
    ``min_symbols`` ≤0 = выключено (прежнее поведение)."""
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
    """Force-include «пиннутых» монет В ОБХОД фильтра (запрос пользователя:
    вернуть монету, которую отсекает range-cap/turnover как памп). Пины всегда в
    итоге (в своём порядке, дедуп), ranked добивает остаток до top_n (≤0 = без
    кап). Это осознанный риск памп-н-дампа на КОНКРЕТНОЙ монете, а не общее
    ослабление фильтра для всего рынка."""
    pins = [p for p in dict.fromkeys(pinned) if p]
    rest = [r for r in ranked if r not in pins]
    if top_n > 0:
        rest = rest[: max(0, top_n - len(pins))]
    return pins + rest
