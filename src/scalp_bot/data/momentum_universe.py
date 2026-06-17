"""Моментум-селектор торговой вселенной (метод «как в ролике»).

Альтернативный способ подбора монет, описанный в ролике SerCrypto «Bybit
стратегия трейдинга на 2026» (https://youtu.be/gCgYS-CsGWc): берём монеты из
ТОП по росту/падению за 24 часа (направленное изменение цены = прокси
притока денег/внимания), с порогом по суточному обороту (в ролике: «от 50 млн
уже можно рассматривать, в идеале 100+ млн»). «Характер монеты по истории» из
ролика — дискреционная часть, которую мы НЕ автоматизируем здесь.

Это ОТДЕЛЬНЫЙ метод (переключатель ``SCALP_UNIVERSE_METHOD``), запускаемый
параллельно/вместо штатного RVOL-селектора (``data/universe.py``) для
форвард-сравнения «какой отбор монет даёт лучший результат на sweep_fade».
Сама стратегия НЕ меняется — меняется только список символов, который ей
подаётся.

Отличие от RVOL-селектора (axis отбора):
- RVOL (``universe.py``): ранжирует по АМПЛИТУДЕ (range%/intraday RVOL),
  направление цены игнорирует, и явно режет пампы (range-cap 20% = анти-
  манипуляция). Подобран под order-flow в обе стороны на ликвидных рейнджах.
- Momentum (этот модуль): ранжирует по МОДУЛЮ 24h-изменения цены (топ
  мувёров — и гейнеры, и лузеры), порог только по обороту. Анти-памп кэпа
  НЕТ — в ролике берут именно сильно сдвинувшиеся монеты (+44%, +100%).

ВАЖНО (no-data-fitting.mdc): этот модуль — РЕАЛИЗАЦИЯ описанного в ролике
метода, а не оптимизация порогов под наш P&L. Любое решение «momentum лучше/
хуже RVOL» принимается по форвард-выборке n≥100 (sample-size.mdc), а не по
первым сделкам.

Поле ``price24hPcnt`` (24h изменение, доля: "0.44" = +44%) и ``turnover24h``
берутся из Bybit get_tickers.
Офдок: https://bybit-exchange.github.io/docs/v5/market/tickers
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
    if t.get("curPreListingPhase"):  # пре-маркет / новый листинг — пропускаем
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
    """Hard-фильтр моментум-отбора. Возвращает строки-метрики прошедших
    символов (без ранжирования).

    ``direction``: "both" — гейнеры и лузеры (по модулю 24h-движения; sweep_fade
    сам решает сторону по HTF-гейту); "up" — только рост; "down" — только
    падение. Канон ролика — «топ по росту» (для шортов «топ по падению»);
    "both" даёт боту максимум кандидатов «в движении».
    ``max_spread_bps`` ≤0 = выкл (в ролике спред-фильтра нет; оставлен опцией,
    т.к. sweep_fade имеет fee-guard и широкий спред съест мелкую цель)."""
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
    """Hard-фильтр + ранжирование по 24h-моментуму. Тонкая обёртка
    filter_momentum + rank_momentum."""
    rows = filter_momentum(tickers, min_turnover=min_turnover,
                           min_abs_change_pct=min_abs_change_pct,
                           max_spread_bps=max_spread_bps, direction=direction)
    return rank_momentum(rows, top_n=top_n)
