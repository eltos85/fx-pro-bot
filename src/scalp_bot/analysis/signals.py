"""Движок orderflow-сигналов scalp_bot (детерминированные правила).

Сетап — «свип ликвидности + поглощение» (mean-reversion fade), консенсус
проф-источников 2026 (chartwhisperer order-flow, coinxsight delta-system,
bookmap liquidity-vacuum, Kalena CVD):

  Цена сметает стопы за уровень → агрессоров «поглощают» (CVD-дивергенция)
  → толпа перегружена (funding) и/или вылетает (ликвидации) → разворот.

5 микро-правил (бинарные). Вход требует CVD-дивергенцию (обязательна как
ключевой признак поглощения) + ≥ ``min_confluence`` из 5:
  1. SWEEP        — цена сделала свежий локальный экстремум (собрала стопы).
  2. CVD_DIVERG   — цена ↓ low, CVD ↑ low (bull) / зеркально (bear). [ОБЯЗ.]
  3. LIQ_FLUSH    — каскад вынужденных ликвидаций в сторону капитуляции.
  4. FUNDING      — funding-перекос толпы в противоположную сделке сторону.
  5. OB_IMBALANCE — стакан накапливается в сторону сделки.

Все функции чистые → юнит-тестируемы без WS.
"""
from __future__ import annotations

from dataclasses import dataclass

from scalp_bot.data.aggregates import CvdSample, LiqEvent, SymbolSnapshot


@dataclass
class Signal:
    symbol: str
    side: str  # "long" | "short"
    entry_ref: float
    sl_level: float
    tp_level: float
    score: int
    reasons: list[str]


def _split_halves(samples: list[CvdSample]) -> tuple[list[CvdSample], list[CvdSample]]:
    """Делит окно на раннюю и позднюю половины по времени."""
    if len(samples) < 4:
        return ([], [])
    mid = len(samples) // 2
    return (samples[:mid], samples[mid:])


def detect_sweep(samples: list[CvdSample], side: str) -> bool:
    """Свежий экстремум: поздняя половина пробила экстремум ранней."""
    early, late = _split_halves(samples)
    if not early or not late:
        return False
    if side == "long":
        return min(s.price for s in late) < min(s.price for s in early)
    return max(s.price for s in late) > max(s.price for s in early)


def cvd_divergence(samples: list[CvdSample], side: str, min_late: int = 0) -> bool:
    """Дивергенция цена↔CVD (поглощение).

    LONG  (bull): late price-min < early price-min, но late cvd-min > early cvd-min.
    SHORT (bear): late price-max > early price-max, но late cvd-max < early cvd-max.

    Строгое неравенство по CVD (>/<, не ≥/≤): на «тонком» окне почти плоский CVD
    давал ложную дивергенцию при равенстве. ``min_late`` — минимум сделок в
    поздней половине (анти «пустота»: дивергенция на 2-3 тиках = шум).
    """
    early, late = _split_halves(samples)
    if not early or not late:
        return False
    if min_late and len(late) < min_late:
        return False
    if side == "long":
        price_lower_low = min(s.price for s in late) < min(s.price for s in early)
        cvd_higher_low = min(s.cvd for s in late) > min(s.cvd for s in early)
        return price_lower_low and cvd_higher_low
    price_higher_high = max(s.price for s in late) > max(s.price for s in early)
    cvd_lower_high = max(s.cvd for s in late) < max(s.cvd for s in early)
    return price_higher_high and cvd_lower_high


def liq_flush(liqs: list[LiqEvent], side: str, threshold_usd: float) -> bool:
    """Каскад ликвидаций в сторону капитуляции.

    Семантика Bybit allLiquidation (офиц. дока): поле S = POSITION side.
    S="Buy" → ликвидирован ЛОНГ (forced SELL, капитуляция ВНИЗ) → fade ЛОНГОМ.
    S="Sell" → ликвидирован ШОРТ (forced BUY, сквиз ВВЕРХ) → fade ШОРТОМ.
    https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation
    """
    want = "Buy" if side == "long" else "Sell"
    total = sum(e.size_usd for e in liqs if e.side == want)
    return total >= threshold_usd


def funding_supportive(funding: float | None, side: str,
                       pos_extreme: float, neg_extreme: float) -> bool:
    """Перекос толпы против сделки = топливо для разворота (АСИММЕТРИЧНО).

    LONG:  funding ≤ −neg_extreme (толпа в шорте → сквиз вверх).
    SHORT: funding ≥ +pos_extreme (толпа в лонге → каскад вниз).
    Research: crowded long обычно глубже (+0.05%), crowded short мельче (−0.03%).
    """
    if funding is None:
        return False
    return funding <= -neg_extreme if side == "long" else funding >= pos_extreme


def reclaimed(samples: list[CvdSample], side: str, frac: float) -> bool:
    """Reclaim (CAP Rule 2): цена ушла за уровень фитилём, но вернулась внутрь.

    LONG: свипнутый low в поздней половине; цена восстановилась ≥ frac пути
    от свип-экстремума обратно к свипнутому уровню (min ранней половины).
    SHORT — зеркально.
    """
    early, late = _split_halves(samples)
    if not early or not late:
        return False
    last_price = samples[-1].price
    if side == "long":
        prior = min(s.price for s in early)      # свипнутый уровень (поддержка)
        swept = min(s.price for s in late)       # свип-экстремум (ниже)
        excursion = prior - swept
        if excursion <= 0:
            return False
        return last_price >= swept + frac * excursion
    prior = max(s.price for s in early)
    swept = max(s.price for s in late)
    excursion = swept - prior
    if excursion <= 0:
        return False
    return last_price <= swept - frac * excursion


def reversal_momentum(samples: list[CvdSample], side: str, window_sec: float) -> bool:
    """Разворот ленты (CAP Rule 5 / tape-shift): CVD качнулся в сторону сделки.

    LONG: за последние window_sec CVD растёт (агрессия перетекает в buy).
    SHORT: CVD падает. Подтверждает, что разворот НАЧАЛСЯ (не входим в нож).
    """
    if len(samples) < 2:
        return False
    cutoff = samples[-1].ts - window_sec
    recent = [s for s in samples if s.ts >= cutoff]
    if len(recent) < 2:
        return False
    delta_cvd = recent[-1].cvd - recent[0].cvd
    return delta_cvd > 0 if side == "long" else delta_cvd < 0


def ob_supportive(imbalance: float | None, side: str, min_imb: float) -> bool:
    """Стакан накапливается в сторону сделки (top-N bid/(bid+ask))."""
    if imbalance is None:
        return False
    return imbalance >= min_imb if side == "long" else imbalance <= (1.0 - min_imb)


def diagnose(snap: SymbolSnapshot, cfg) -> dict | None:
    """Состояние всех правил/гейтов для лучшей стороны — для funnel-диагностики.

    Возвращает dict с булевыми флагами (наблюдаемость, НЕ влияет на торговлю):
    sweep/div/reclaim/momentum/ob/liq/funding/score/fee_ok/signal.
    """
    if snap.stale or snap.last_price is None or len(snap.cvd_samples) < 6:
        return None
    s = snap.cvd_samples
    best = None
    for side in ("long", "short"):
        div = cvd_divergence(s, side, getattr(cfg, "div_min_late_trades", 0))
        d = {
            "side": side,
            "sweep": detect_sweep(s, side),
            "div": div,
            "liq": liq_flush(snap.liq_events, side, cfg.liq_flush_usd),
            "funding": funding_supportive(snap.funding_rate, side,
                                          cfg.funding_extreme_pos, cfg.funding_extreme_neg),
            "ob": ob_supportive(snap.ob_imbalance, side, cfg.ob_imbalance_min),
            "reclaim": reclaimed(s, side, cfg.reclaim_frac),
            "momentum": reversal_momentum(s, side, cfg.momentum_window_sec),
        }
        d["score"] = sum(1 for k in ("sweep", "div", "liq", "funding", "ob") if d[k])
        if best is None or d["score"] > best["score"]:
            best = d
    sig = evaluate(snap, cfg)
    best["fee_ok"] = sig is not None or not best["div"]  # грубо: дошли до fee-guard
    best["signal"] = sig is not None
    return best


def flow_invalidated(snap: SymbolSnapshot, side: str, window_sec: float) -> bool:
    """Hard invalidation: ордер-флоу (CVD) развернулся ПРОТИВ позиции.

    LONG-позиция инвалидируется, если лента качнулась в short (CVD падает).
    Все скальп-источники: «exit immediately when order flow flips».
    """
    opp = "short" if side == "long" else "long"
    return reversal_momentum(snap.cvd_samples, opp, window_sec)


def _evaluate_side(snap: SymbolSnapshot, side: str, cfg) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    if detect_sweep(snap.cvd_samples, side):
        score += 1
        reasons.append("sweep")
    div = cvd_divergence(snap.cvd_samples, side, getattr(cfg, "div_min_late_trades", 0))
    if div:
        score += 1
        reasons.append("cvd_div")
    if liq_flush(snap.liq_events, side, cfg.liq_flush_usd):
        score += 1
        reasons.append("liq_flush")
    if funding_supportive(snap.funding_rate, side,
                          cfg.funding_extreme_pos, cfg.funding_extreme_neg):
        score += 1
        reasons.append("funding")
    if ob_supportive(snap.ob_imbalance, side, cfg.ob_imbalance_min):
        score += 1
        reasons.append("ob_imb")
    # CVD-дивергенция обязательна (ключевой признак поглощения).
    if not div:
        return (0, [])
    # Подтверждение разворота: reclaim + разворот ленты (анти «ловля ножа»).
    if getattr(cfg, "require_reclaim", False):
        if not reclaimed(snap.cvd_samples, side, cfg.reclaim_frac):
            return (0, [])
        if not reversal_momentum(snap.cvd_samples, side, cfg.momentum_window_sec):
            return (0, [])
        reasons.append("rcl+mom")
    return (score, reasons)


def evaluate(snap: SymbolSnapshot, cfg) -> Signal | None:
    """Главная оценка. Возвращает Signal или None.

    cfg — объект с полями min_confluence, liq_flush_usd, funding_extreme,
    ob_imbalance_min, take_profit_r, sl_buffer_bps (ScalpSettings).
    """
    if snap.stale or snap.last_price is None or len(snap.cvd_samples) < 6:
        return None

    long_score, long_reasons = _evaluate_side(snap, "long", cfg)
    short_score, short_reasons = _evaluate_side(snap, "short", cfg)

    if long_score >= cfg.min_confluence and long_score >= short_score:
        side, score, reasons = "long", long_score, long_reasons
    elif short_score >= cfg.min_confluence:
        side, score, reasons = "short", short_score, short_reasons
    else:
        return None

    entry = snap.best_ask if (side == "long" and snap.best_ask) else (
        snap.best_bid if (side == "short" and snap.best_bid) else snap.last_price
    )
    buf = cfg.sl_buffer_bps / 1e4
    # SL за экстремумом ИМЕННО свежего свипа (поздняя половина окна), а не за
    # глобальным минимумом всех 180с — иначе старый дальний низ раздувает R и
    # TP, и сделка висит до тайм-стопа. Канон: стоп сразу за фитилём свипа.
    _, late = _split_halves(snap.cvd_samples)
    recent = late or snap.cvd_samples
    if side == "long":
        swept = min(s.price for s in recent)
        sl = swept * (1.0 - buf)
        risk = entry - sl
        tp = entry + cfg.take_profit_r * risk
    else:
        swept = max(s.price for s in recent)
        sl = swept * (1.0 + buf)
        risk = sl - entry
        tp = entry - cfg.take_profit_r * risk

    if risk <= 0:
        return None

    # Fee-guard: цель должна быть ≥ min_target_fee_mult × round-trip издержек,
    # иначе комиссии съедают edge (анти fee-trap для мелких целей скальпа).
    tp_move_frac = (cfg.take_profit_r * risk) / entry if entry > 0 else 0.0
    min_move = cfg.min_target_fee_mult * cfg.round_trip_fee_frac
    if tp_move_frac < min_move:
        return None

    return Signal(
        symbol=snap.symbol, side=side, entry_ref=entry,
        sl_level=sl, tp_level=tp, score=score, reasons=reasons,
    )
