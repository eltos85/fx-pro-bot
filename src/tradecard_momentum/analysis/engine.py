"""Оркестратор tradecard_momentum: прогон детекторов §4 + выбор темы №1 + sample-гейт.

Бот один (mode='live', единственная стратегия momentum), поэтому в отличие от
tradecard_bybit детекторы прогоняются по всей выборке без mode-разделения. Тема
№1 = самый «дорогой» повторяющийся паттерн (по модулю net убытка среза при
достаточной выборке). «Паттерн» становится «темой» только при прохождении
sample-size (n ≥ min_trades_for_theme) — иначе НАБЛЮДЕНИЕ.
"""
from __future__ import annotations

from dataclasses import dataclass

from tradecard_momentum.analysis.detectors import (
    PatternFinding, detect_loss_cluster, detect_overtrading,
    detect_signal_not_predictive, detect_swap_drag,
    detect_symbol_session_leak)
from tradecard_momentum.analysis.trade import MomentumTrade
from tradecard_momentum.config.settings import TradecardMomentumSettings

_BOT = "momentum"


@dataclass
class DetectionResult:
    findings: list[PatternFinding]
    top_theme: PatternFinding | None
    sample_ok: bool


def run_detection(trades: list[MomentumTrade], *,
                  cfg: TradecardMomentumSettings) -> DetectionResult:
    mode = "live"
    findings: list[PatternFinding] = []
    findings += detect_signal_not_predictive(
        trades, bot=_BOT, mode=mode, buckets=cfg.grade_buckets,
        min_rho=cfg.grade_monotonic_min_rho,
        min_trades=cfg.regime_leak_min_trades)
    findings += detect_symbol_session_leak(
        trades, bot=_BOT, mode=mode, min_trades=cfg.regime_leak_min_trades)
    findings += detect_loss_cluster(
        trades, bot=_BOT, mode=mode, factor=cfg.loss_cluster_factor,
        min_trades=cfg.loss_cluster_min_trades)
    findings += detect_overtrading(
        trades, bot=_BOT, mode=mode, spike_factor=cfg.overtrading_spike_factor,
        min_trades=cfg.overtrading_min_trades)
    findings += detect_swap_drag(
        trades, bot=_BOT, mode=mode, min_frac=cfg.swap_drag_min_frac,
        min_trades=cfg.swap_drag_min_trades)

    ranked = sorted(findings, key=lambda f: -min(f.net, 0.0), reverse=True)
    top = ranked[0] if ranked else None
    sample_ok = bool(top and top.n >= cfg.min_trades_for_theme)
    return DetectionResult(findings=ranked, top_theme=top, sample_ok=sample_ok)
