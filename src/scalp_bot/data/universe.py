"""Авто-селектор торговой вселенной scalp_bot.

Раз в ``universe_refresh_sec`` бот сам выбирает монеты под стратегию, а не
торгует хардкод-список. Источник — Bybit ``get_tickers`` (24h snapshot), офдок:
https://bybit-exchange.github.io/docs/v5/market/tickers

Критерии (математика fee-guard + практика скальпа, не подгонка):
- ``range% = (high24h − low24h)/last`` — амплитуда. Нужна широкая: fee-guard
  требует стоп ``R ≥ 0.22%`` цены (round-trip taker 0.11% × min_target_fee_mult
  / take_profit_r), а дневной range — прокси микро-волатильности свипов.
  Live-граница: range 2.5–5.4% (BTC/ETH/SOL/XRP) — сигналы режутся; 9–16%
  (HYPE/NEAR/ZEC) — проходят (BUILDLOG_SCALP 2026-05-30) → floor 6%.
- ``turnover24h`` — ликвидность (тугой спред/филл). Floor $150M держит топ-тир
  (рабочие монеты были 248–799M$); скальп торгуют только ликвидные перпы.
- range cap 30% — отсечь pump-and-dump (event-пампы XLM 37%/ALLO 43%).
- spread cap (bps) — не входить в дорогих по спреду.
Сортировка: range% убыв. (макс. волатильность под наш edge), tie-break turnover.

ВАЖНО (no-data-fitting.mdc): пороги — конфиг (env), привязаны к fee-guard и
live-границе, а не оптимизированы под прошлый P&L.
"""
from __future__ import annotations


def _f(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
    """Фильтр + ранжирование → топ-N символов под стратегию."""
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
    rows.sort(key=lambda m: (m["range_pct"], m["turnover"]), reverse=True)
    return [m["symbol"] for m in rows[:top_n]]
