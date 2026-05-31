"""Авто-селектор торговой вселенной scalp_bot.

Раз в ``universe_refresh_sec`` бот сам выбирает монеты под стратегию, а не
торгует хардкод-список. Источник — Bybit ``get_tickers`` (24h snapshot), офдок:
https://bybit-exchange.github.io/docs/v5/market/tickers

Принцип «качество, а не количество» (запрос пользователя 2026-05-31): берём
ВСЕ монеты рынка, прошедшие фильтр, а не фиксированные N. Подошло 5 — берём 5;
через 30 мин подошло 2 — берём 2. ``top_n`` — лишь safety-кап на число
WS-подписок (≤0 = без лимита).

ФИЛЬТРЫ (hard, математика fee-guard + практика скальпа, не подгонка):
- ``range% = (high24h − low24h)/last`` — амплитуда. Нужна широкая: fee-guard
  требует стоп ``R ≥ 0.22%`` цены (round-trip taker 0.11% × min_target_fee_mult
  / take_profit_r), а дневной range — прокси микро-волатильности свипов.
  Live-граница: range 2.5–5.4% (BTC/ETH/SOL/XRP) — сигналы режутся; 9–16%
  (HYPE/NEAR/ZEC) — проходят (BUILDLOG_SCALP 2026-05-30) → floor 6%.
- ``turnover24h`` — ликвидность (грубый прокси). Floor 150M→100M (2026-05-31):
  рынок просел ~2× по обороту, и $150M стал выкидывать рабочие NEAR ($137M)/
  ZEC ($125M) с отличным спредом 0.2–0.4bps. Реальный страж ликвидности для
  скальпа — spread cap (ниже), turnover лишь отсекает совсем «пыль».
- range cap 30% — отсечь pump-and-dump (event-пампы XLM 37%/ALLO 43%).
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


def rank_universe(tickers: list[dict], *, top_n: int, min_turnover: float,
                  min_range_pct: float, max_range_pct: float,
                  max_spread_bps: float) -> list[str]:
    """Hard-фильтр + композитное ранжирование → все прошедшие символы.

    ``top_n`` — safety-кап на число WS-подписок: ≤0 = без лимита (берём все,
    прошедшие фильтр). Количество определяется КАЧЕСТВОМ (фильтрами), а не
    фиксированным числом."""
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
    if not rows:
        return []
    vol_n = _norm([m["range_pct"] for m in rows])
    liq_n = _norm([m["turnover"] for m in rows])
    spr_n = _norm([m["spread_bps"] for m in rows])
    for i, m in enumerate(rows):
        m["score"] = (W_VOL * vol_n[i] + W_LIQ * liq_n[i]
                      + W_SPREAD * (1.0 - spr_n[i]))
    # tie-break turnover (детерминизм при равных скорах — напр. одинаковые тикеры)
    rows.sort(key=lambda m: (m["score"], m["turnover"]), reverse=True)
    picked = rows if top_n <= 0 else rows[:top_n]
    return [m["symbol"] for m in picked]


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
