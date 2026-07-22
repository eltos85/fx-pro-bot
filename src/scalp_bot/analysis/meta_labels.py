"""Pure shadow meta-labels для evidence-first наблюдения.

Функции в этом модуле только преобразуют уже собранные ``regime``/``setup``
поля в заранее объявленные компоненты. Результат предназначен исключительно
для ``meta_label_features``: он не является торговым ``Signal.score`` и не
может использоваться в resolve/gates/sizing/orders.
"""
from __future__ import annotations

import math
from typing import Any


# Preregistered shadow thresholds, 2026-07-22. До post-cutoff walk-forward
# анализа менять их нельзя; ни один из них не является торговым гейтом.
FADE_RET_AUTOCORR_MAX = -0.05
FADE_ADVERSE_SLOPE_MIN_BPS_MIN = 1.0
FADE_CVD_REVERSAL_MIN = 0.0
FADE_TAPE_ACCEL_MIN = 1.0
FADE_WOULD_KEEP_MIN_SCORE = 3

BREAKOUT_NATR_MIN_PCT = 0.50
BREAKOUT_BB_WIDTH_MIN_PCT = 1.00
BREAKOUT_OI_EXPANSION_MIN_PCT = 0.0
BREAKOUT_CVD_FOLLOW_THROUGH_MIN = 0.0
BREAKOUT_WOULD_KEEP_MIN_SCORE = 3


def _number(value: Any) -> float | None:
    """Конечное число либо None; bool намеренно не принимается как 0/1."""
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _component(value: float | None, predicate) -> int | None:
    return None if value is None else int(bool(predicate(value)))


def _finish(label: str, components: dict[str, int | None],
            values: dict[str, float | None], keep_min: int) -> dict:
    known = sum(v is not None for v in components.values())
    score = sum(v == 1 for v in components.values())
    # Missing telemetry must not masquerade as a negative label.
    would_keep = int(score >= keep_min) if known == len(components) else None
    return {
        "label_type": label,
        **values,
        **components,
        "component_count": known,
        "meta_score": score,
        "would_keep": would_keep,
    }


def fade_exhaustion(regime: dict | None, setup: dict | None, side: str) -> dict:
    """Shadow-score исчерпания для sweep-fade; pure и fail-soft.

    Компоненты: отрицательная автокорреляция, направленный adverse slope,
    side-adjusted CVD reversal из setup и ускорение tape. ``would_keep`` известен
    только при наличии всех четырёх компонентов.
    """
    regime = regime or {}
    setup = setup or {}
    ret_autocorr = _number(regime.get("ret_autocorr"))
    slope = _number(regime.get("price_slope_bps_min"))
    direction = 1.0 if side == "long" else -1.0 if side == "short" else None
    adverse_slope = (-direction * slope) if direction is not None and slope is not None else None
    cvd_reversal = _number(setup.get("cvd_reversal_magnitude"))
    tape_accel = _number(regime.get("tape_accel"))
    components = {
        "ret_autocorr_component": _component(
            ret_autocorr, lambda x: x <= FADE_RET_AUTOCORR_MAX),
        "adverse_slope_component": _component(
            adverse_slope, lambda x: x >= FADE_ADVERSE_SLOPE_MIN_BPS_MIN),
        "cvd_reversal_component": _component(
            cvd_reversal, lambda x: x > FADE_CVD_REVERSAL_MIN),
        "tape_accel_component": _component(
            tape_accel, lambda x: x >= FADE_TAPE_ACCEL_MIN),
    }
    return _finish(
        "fade_exhaustion", components,
        {
            "ret_autocorr_value": ret_autocorr,
            "aligned_adverse_slope_bps_min": adverse_slope,
            "cvd_reversal_value": cvd_reversal,
            "tape_accel_value": tape_accel,
            "natr_pct_value": None,
            "bb_width_pct_value": None,
            "oi_expansion_pct_value": None,
            "cvd_follow_through_value": None,
        },
        FADE_WOULD_KEEP_MIN_SCORE,
    )


def breakout_fuel(regime: dict | None, setup: dict | None, side: str) -> dict:
    """Shadow-score топлива density breakout; pure и fail-soft.

    ``setup`` принят симметрично fade API и зарезервирован для последующих
    geometry-компонентов; текущая preregistration использует regime NATR,
    Bollinger width, OI expansion и side-adjusted CVD follow-through.
    """
    del setup
    regime = regime or {}
    natr = _number(regime.get("htf_natr_pct"))
    bb_width = _number(regime.get("htf_bb_width_pct"))
    oi_expansion = _number(regime.get("oi_delta_pct"))
    cvd_slope = _number(regime.get("cvd_slope"))
    direction = 1.0 if side == "long" else -1.0 if side == "short" else None
    cvd_follow = direction * cvd_slope \
        if direction is not None and cvd_slope is not None else None
    components = {
        "natr_component": _component(
            natr, lambda x: x >= BREAKOUT_NATR_MIN_PCT),
        "bb_width_component": _component(
            bb_width, lambda x: x >= BREAKOUT_BB_WIDTH_MIN_PCT),
        "oi_expansion_component": _component(
            oi_expansion, lambda x: x > BREAKOUT_OI_EXPANSION_MIN_PCT),
        "cvd_follow_through_component": _component(
            cvd_follow, lambda x: x > BREAKOUT_CVD_FOLLOW_THROUGH_MIN),
    }
    return _finish(
        "breakout_fuel", components,
        {
            "ret_autocorr_value": None,
            "aligned_adverse_slope_bps_min": None,
            "cvd_reversal_value": None,
            "tape_accel_value": None,
            "natr_pct_value": natr,
            "bb_width_pct_value": bb_width,
            "oi_expansion_pct_value": oi_expansion,
            "cvd_follow_through_value": cvd_follow,
        },
        BREAKOUT_WOULD_KEEP_MIN_SCORE,
    )


def meta_label_for(strategy: str, side: str, regime: dict | None,
                   setup: dict | None) -> dict | None:
    """Выбрать preregistered label по семейству стратегии."""
    if strategy.startswith("sweep_fade"):
        return fade_exhaustion(regime, setup, side)
    if strategy == "density_break":
        return breakout_fuel(regime, setup, side)
    return None
