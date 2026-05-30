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


def cvd_divergence(samples: list[CvdSample], side: str) -> bool:
    """Дивергенция цена↔CVD (поглощение).

    LONG  (bull): late price-min < early price-min, но late cvd-min ≥ early cvd-min.
    SHORT (bear): late price-max > early price-max, но late cvd-max ≤ early cvd-max.
    """
    early, late = _split_halves(samples)
    if not early or not late:
        return False
    if side == "long":
        price_lower_low = min(s.price for s in late) < min(s.price for s in early)
        cvd_higher_low = min(s.cvd for s in late) >= min(s.cvd for s in early)
        return price_lower_low and cvd_higher_low
    price_higher_high = max(s.price for s in late) > max(s.price for s in early)
    cvd_lower_high = max(s.cvd for s in late) <= max(s.cvd for s in early)
    return price_higher_high and cvd_lower_high


def liq_flush(liqs: list[LiqEvent], side: str, threshold_usd: float) -> bool:
    """Каскад ликвидаций в сторону капитуляции.

    LONG-fade: ликвидируются ЛОНГИ (side="Sell", forced sell) — капитуляция вниз.
    SHORT-fade: ликвидируются ШОРТЫ (side="Buy", forced buy) — выброс вверх.
    """
    want = "Sell" if side == "long" else "Buy"
    total = sum(e.size_usd for e in liqs if e.side == want)
    return total >= threshold_usd


def funding_supportive(funding: float | None, side: str, extreme: float) -> bool:
    """Перекос толпы против сделки = топливо для разворота.

    LONG:  funding ≤ −extreme (толпа в шорте → сквиз вверх).
    SHORT: funding ≥ +extreme (толпа в лонге → каскад вниз).
    """
    if funding is None:
        return False
    return funding <= -extreme if side == "long" else funding >= extreme


def ob_supportive(imbalance: float | None, side: str, min_imb: float) -> bool:
    """Стакан накапливается в сторону сделки (top-N bid/(bid+ask))."""
    if imbalance is None:
        return False
    return imbalance >= min_imb if side == "long" else imbalance <= (1.0 - min_imb)


def _evaluate_side(snap: SymbolSnapshot, side: str, cfg) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    if detect_sweep(snap.cvd_samples, side):
        score += 1
        reasons.append("sweep")
    div = cvd_divergence(snap.cvd_samples, side)
    if div:
        score += 1
        reasons.append("cvd_div")
    if liq_flush(snap.liq_events, side, cfg.liq_flush_usd):
        score += 1
        reasons.append("liq_flush")
    if funding_supportive(snap.funding_rate, side, cfg.funding_extreme):
        score += 1
        reasons.append("funding")
    if ob_supportive(snap.ob_imbalance, side, cfg.ob_imbalance_min):
        score += 1
        reasons.append("ob_imb")
    # CVD-дивергенция обязательна.
    if not div:
        return (0, [])
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
    if side == "long":
        swept = min(s.price for s in snap.cvd_samples)
        sl = swept * (1.0 - buf)
        risk = entry - sl
        tp = entry + cfg.take_profit_r * risk
    else:
        swept = max(s.price for s in snap.cvd_samples)
        sl = swept * (1.0 + buf)
        risk = sl - entry
        tp = entry - cfg.take_profit_r * risk

    if risk <= 0:
        return None

    return Signal(
        symbol=snap.symbol, side=side, entry_ref=entry,
        sl_level=sl, tp_level=tp, score=score, reasons=reasons,
    )
