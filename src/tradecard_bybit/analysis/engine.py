"""Оркестратор tradecard: прогон детекторов §4 + выбор темы №1 + sample-гейт.

Детекторы запускаются **раздельно по mode** (paper/live) — паттерны режимов не
смешиваются (TASKSPEC §4). paper_live_divergence — единственный, что работает на
обоих режимах сразу. Тема №1 = самый «дорогой» повторяющийся паттерн (по модулю
net убытка среза при достаточной выборке). «Паттерн» становится «темой» только
при прохождении sample-size (n ≥ min_trades_for_theme) — иначе НАБЛЮДЕНИЕ.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from tradecard_bybit.analysis.detectors import (
    PatternFinding, detect_big_game_hunting, detect_exit_left_money,
    detect_factor_noise, detect_grade_not_predictive, detect_overtrading,
    detect_paper_live_divergence, detect_sl_cluster,
    detect_strategy_regime_leak)
from tradecard_bybit.analysis.trade import Trade
from tradecard_bybit.config.settings import TradecardBybitSettings


@dataclass
class DetectionResult:
    findings: list[PatternFinding]
    top_theme: PatternFinding | None
    sample_ok: bool          # тема №1 прошла sample-size (можно гонять 5 Why)


def _findings_for_mode(trades: list[Trade], *, bot: str, mode: str,
                       cfg: TradecardBybitSettings,
                       mfe_fn: Callable[[Trade], float | None] | None,
                       ) -> list[PatternFinding]:
    out: list[PatternFinding] = []
    out += detect_grade_not_predictive(
        trades, bot=bot, mode=mode, buckets=cfg.grade_buckets,
        min_rho=cfg.grade_monotonic_min_rho,
        min_trades=cfg.regime_leak_min_trades)
    out += detect_strategy_regime_leak(
        trades, bot=bot, mode=mode, min_trades=cfg.regime_leak_min_trades)
    out += detect_sl_cluster(
        trades, bot=bot, mode=mode, factor=cfg.sl_cluster_factor,
        min_trades=cfg.sl_cluster_min_trades)
    out += detect_exit_left_money(
        trades, bot=bot, mode=mode, factor=cfg.exit_left_money_factor,
        min_trades=cfg.exit_left_money_min_trades, mfe_fn=mfe_fn)
    out += detect_factor_noise(
        trades, bot=bot, mode=mode, max_exp_frac=cfg.factor_noise_max_exp_frac,
        min_trades=cfg.factor_noise_min_trades)
    # overtrading / big_game — per-strategy (TASKSPEC: страты scalp изучаем
    # раздельно; срез по часам/грейду осмыслен внутри одной страты).
    strats: dict[str, list[Trade]] = {}
    for t in trades:
        strats.setdefault(t.strategy, []).append(t)
    for strat, grp in strats.items():
        out += detect_overtrading(
            grp, bot=bot, mode=mode, spike_factor=cfg.overtrading_spike_factor,
            min_trades=cfg.overtrading_min_trades, strategy=strat)
        out += detect_big_game_hunting(
            grp, bot=bot, mode=mode, max_top_share=cfg.big_game_max_top_share,
            min_trades=cfg.big_game_min_trades, buckets=cfg.grade_buckets,
            strategy=strat)
    return out


def _impact(f: PatternFinding) -> float:
    """«Стоимость» паттерна для ранжирования темы №1: модуль убытка среза.

    Берём отрицательный net (чем убыточнее срез — тем выше приоритет). Для
    структурных паттернов без прямого net (grade/big_game) net тоже информативен.
    """
    return -min(f.net, 0.0)


def run_detection(trades: list[Trade], *, bot: str,
                  cfg: TradecardBybitSettings,
                  mfe_fn: Callable[[Trade], float | None] | None = None,
                  ) -> DetectionResult:
    findings: list[PatternFinding] = []
    for mode in ("paper", "live"):
        mode_trades = [t for t in trades if t.mode == mode]
        if mode_trades:
            findings += _findings_for_mode(mode_trades, bot=bot, mode=mode,
                                           cfg=cfg, mfe_fn=mfe_fn)
    findings += detect_paper_live_divergence(
        trades, bot=bot, min_trades=cfg.paper_live_min_trades)

    # тема №1: самый дорогой паттерн при достаточной выборке (sample-size)
    ranked = sorted(findings, key=_impact, reverse=True)
    top = ranked[0] if ranked else None
    sample_ok = bool(top and top.n >= cfg.min_trades_for_theme)
    return DetectionResult(findings=ranked, top_theme=top, sample_ok=sample_ok)
