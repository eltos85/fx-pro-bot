"""Моментум-селектор торговой вселенной flowzone_bot (метод «как в ролике»).

Параллельный (изолированный от scalp_bot) альтернативный способ подбора монет,
описанный в ролике SerCrypto «Bybit стратегия трейдинга на 2026»
(https://youtu.be/gCgYS-CsGWc): берём монеты из ТОП по росту/падению за 24 часа
(направленное изменение цены = прокси притока денег/внимания), с порогом по
суточному обороту («от 50 млн можно, в идеале 100+ млн»). «Характер монеты по
истории» из ролика — дискреционная часть, которую мы НЕ автоматизируем.

Включается переключателем ``FLOWZONE_UNIVERSE_METHOD=momentum`` ВМЕСТО штатного
RVOL-селектора (``data/universe.py``) для форвард-сравнения отбора монет. Сама
стратегия flowzone (footprint/absorption/zone) НЕ меняется — меняется только
список символов, который ей подаётся.

ВНИМАНИЕ (канон flowzone, STRATEGY §6.1): footprint/absorption читаемы только на
ЛИКВИДНОСТИ (канон демонстрировался на NQ). Momentum-отбор тянет «то что
стреляет», в т.ч. тонкие памп-альты без анти-памп кэпа — на них order-flow
шумит. Это осознанный риск форвард-теста; вывод «лучше/хуже RVOL» — только по
выборке n≥100 (sample-size.mdc), не по первым сделкам (no-data-fitting.mdc).

Отличие от RVOL-селектора (axis отбора):
- RVOL (``universe.py``): ранг по АМПЛИТУДЕ (range%/RVOL), направление цены
  игнорирует, режет пампы (range-cap 20%).
- Momentum (этот модуль): ранг по МОДУЛЮ 24h-изменения (топ мувёров), порог
  только по обороту, анти-памп кэпа НЕТ.

Поля ``price24hPcnt`` (24h изменение, доля) и ``turnover24h`` — из Bybit
get_tickers. Офдок: https://bybit-exchange.github.io/docs/v5/market/tickers
"""
from __future__ import annotations


def _f(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def score_momentum_ticker(t: dict) -> dict | None:
    """Метрики одного тикера для моментум-отбора или None если непригоден
    (не USDT-перп / пре-маркет-листинг / нет полей)."""
    sym = t.get("symbol", "") or ""
    if not sym.endswith("USDT"):
        return None
    if t.get("curPreListingPhase"):
        return None
    last = _f(t.get("lastPrice"))
    turn = _f(t.get("turnover24h"))
    pcnt = _f(t.get("price24hPcnt"))
    if not last or last <= 0 or turn is None or pcnt is None:
        return None
    bid = _f(t.get("bid1Price"))
    ask = _f(t.get("ask1Price"))
    spread_bps = ((ask - bid) / last * 10000.0
                  if (bid and ask and ask > bid) else 0.0)
    change_pct = pcnt * 100.0
    return {"symbol": sym, "change_pct": change_pct,
            "abs_change_pct": abs(change_pct), "turnover": turn,
            "spread_bps": spread_bps}


def filter_momentum(tickers: list[dict], *, min_turnover: float,
                    min_abs_change_pct: float, max_spread_bps: float,
                    direction: str = "both") -> list[dict]:
    """Hard-фильтр моментум-отбора. ``direction``: both/up/down. ``max_spread_bps``
    ≤0 = выкл (в ролике спред-фильтра нет; оставлен опцией)."""
    rows: list[dict] = []
    for t in tickers or []:
        m = score_momentum_ticker(t)
        if m is None:
            continue
        if m["turnover"] < min_turnover:
            continue
        if m["abs_change_pct"] < min_abs_change_pct:
            continue
        if direction == "up" and m["change_pct"] <= 0:
            continue
        if direction == "down" and m["change_pct"] >= 0:
            continue
        if max_spread_bps > 0 and m["spread_bps"] > max_spread_bps:
            continue
        rows.append(m)
    return rows


def rank_momentum(rows: list[dict], *, top_n: int) -> list[str]:
    """Ранжирование по МОДУЛЮ 24h-изменения (топ мувёров), оборот — тай-брейк.
    ``top_n`` ≤0 = без капа."""
    if not rows:
        return []
    rows.sort(key=lambda m: (m["abs_change_pct"], m["turnover"]), reverse=True)
    picked = rows if top_n <= 0 else rows[:top_n]
    return [m["symbol"] for m in picked]


def select_momentum_universe(tickers: list[dict], *, top_n: int,
                             min_turnover: float, min_abs_change_pct: float,
                             max_spread_bps: float,
                             direction: str = "both") -> list[str]:
    """Hard-фильтр + ранжирование по 24h-моментуму."""
    rows = filter_momentum(tickers, min_turnover=min_turnover,
                           min_abs_change_pct=min_abs_change_pct,
                           max_spread_bps=max_spread_bps, direction=direction)
    return rank_momentum(rows, top_n=top_n)
