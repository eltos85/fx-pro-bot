"""CLI tradecard-bybit (advisory-ревьюер scalp_bot / flowzone_bot).

Команды:
  tradecard-bybit daily  --bot scalp|flowzone [--since YYYY-MM-DD] [--dry-run]
  tradecard-bybit weekly --bot scalp|flowzone [--since YYYY-MM-DD] [--dry-run]

Периодический режим (cron/scheduler), НЕ realtime. Читает БД ботов read-only,
считает report card, грейдит по score, гоняет 5 Why (weekly), отдаёт отчёт
человеку. НИЧЕГО не пишет в БД ботов и НЕ меняет их конфиг (TASKSPEC §1/§9).
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import UTC, datetime

from tradecard_bybit.analysis import grading
from tradecard_bybit.analysis.engine import run_detection
from tradecard_bybit.analysis.pnl import ModePnl, bybit_net, summarize_mode
from tradecard_bybit.analysis.small_wins import evaluate_small_win
from tradecard_bybit.analysis.trade import Trade
from tradecard_bybit.config.settings import (TradecardBybitSettings,
                                             load_settings)
from tradecard_bybit.data.bot_db import BotDBReadOnly
from tradecard_bybit.report.digest import build_daily_digest
from tradecard_bybit.report.telegram import TelegramNotifier
from tradecard_bybit.report.weekly import build_weekly_report
from tradecard_bybit.state.db import TradecardDB

log = logging.getLogger("tradecard_bybit")


# ─── period helpers ──────────────────────────────────────────────────────

def _since_ts(since: str | None, default_back_sec: float) -> float:
    if since:
        try:
            dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC)
            return dt.timestamp()
        except ValueError:
            log.warning("--since %r не распознан (нужно YYYY-MM-DD) — беру дефолт",
                        since)
    return time.time() - default_back_sec


def _apply_baseline(since_ts: float, cfg: TradecardBybitSettings, bot: str,
                    ) -> tuple[float, str | None]:
    """Поднять нижнюю границу анализа до baseline бота (последняя правка логики).

    Сделки до baseline — «другая стратегия» (разные режимы через границу
    правки нельзя смешивать, no-data-fitting + sample-size). Возвращает
    (effective_since_ts, note для отчёта)."""
    base = cfg.baseline_ts(bot)
    if base is None:
        return since_ts, None
    eff = max(since_ts, base)
    label = datetime.fromtimestamp(base, tz=UTC).strftime("%Y-%m-%d")
    note = (f"Точка отсчёта анализа: {label} (UTC) — baseline последней правки "
            f"логики {bot}; более ранние сделки исключены.")
    return eff, note


def _iso_week(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, tz=UTC).isocalendar()
    return f"{y}-{w:02d}"


# ─── data loading ────────────────────────────────────────────────────────

def _load_trades(cfg: TradecardBybitSettings, bot: str, *, since_ts: float,
                 until_ts: float | None) -> list[Trade]:
    path = cfg.bot_db_path(bot)
    with BotDBReadOnly(path, bot) as db:
        return db.closed_trades(since_ts=since_ts, until_ts=until_ts)


def _maybe_bybit_client(cfg: TradecardBybitSettings, bot: str):
    if not cfg.closed_pnl_enabled:
        return None
    key, sec = cfg.bybit_keys(bot)
    if not key or not sec:
        log.info("Bybit closedPnl: нет ключей %s — пропускаю ground-truth сверку", bot)
        return None
    try:
        from tradecard_bybit.data.bybit_client import TradecardBybitReadOnly
        return TradecardBybitReadOnly(key, sec, demo=cfg.bybit_demo,
                                      category=cfg.bybit_category)
    except Exception:  # noqa: BLE001
        log.exception("Bybit read-only клиент не создан")
        return None


def _pnl_summaries(trades: list[Trade], cfg: TradecardBybitSettings, bot: str,
                   *, since_ts: float, until_ts: float, client,
                   ) -> tuple[ModePnl, ModePnl]:
    paper = summarize_mode(trades, "paper")
    live = summarize_mode(trades, "live")
    if client is not None:
        try:
            rows = client.fetch_closed_pnl(start_ms=int(since_ts * 1000),
                                           end_ms=int(until_ts * 1000))
            net, n = bybit_net(rows)
            # closedPnl относится к РЕАЛЬНЫМ ордерам = live (paper не торгуется).
            live.bybit_net = net
            live.bybit_trades = n
        except Exception:  # noqa: BLE001
            log.exception("Bybit closedPnl fetch failed")
    return paper, live


def _grade_by_strategy(trades: list[Trade], cfg: TradecardBybitSettings,
                       ) -> dict[str, grading.GradeCurve]:
    from collections import defaultdict
    by_strat: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        if t.is_decided:
            by_strat[t.strategy].append(t)
    out: dict[str, grading.GradeCurve] = {}
    for strat, grp in by_strat.items():
        curve = grading.grade_curve(grp, buckets=cfg.grade_buckets,
                                    min_rho=cfg.grade_monotonic_min_rho,
                                    strategy=strat)
        if curve is not None:
            out[strat] = curve
    return out


# ─── commands ────────────────────────────────────────────────────────────

def cmd_daily(cfg: TradecardBybitSettings, bot: str, *, since: str | None,
              dry_run: bool, mode: str | None = None) -> int:
    until = time.time()
    since_ts = _since_ts(since, 24 * 3600)
    since_ts, baseline_note = _apply_baseline(since_ts, cfg, bot)
    trades = _load_trades(cfg, bot, since_ts=since_ts, until_ts=until)
    if mode:
        trades = [t for t in trades if t.mode == mode]
    client = _maybe_bybit_client(cfg, bot)
    paper, live = _pnl_summaries(trades, cfg, bot, since_ts=since_ts,
                                 until_ts=until, client=client)
    result = run_detection(trades, bot=bot, cfg=cfg)
    grades = _grade_by_strategy(trades, cfg)
    # для digest берём грейд первой страты по объёму (или None)
    grade = max(grades.values(), key=lambda c: sum(b.n for b in c.buckets),
                default=None) if grades else None

    date_label = datetime.fromtimestamp(until, tz=UTC).strftime("%Y-%m-%d")
    digest = build_daily_digest(bot=bot, date_label=date_label, pnl_paper=paper,
                                pnl_live=live, findings=result.findings,
                                grade=grade, baseline_note=baseline_note)
    print(digest)
    if not dry_run:
        _send_telegram(cfg, bot, digest)
    return 0


def cmd_weekly(cfg: TradecardBybitSettings, bot: str, *, since: str | None,
               dry_run: bool, mode: str | None = None) -> int:
    until = time.time()
    since_ts = _since_ts(since, 7 * 24 * 3600)
    since_ts, baseline_note = _apply_baseline(since_ts, cfg, bot)
    week = _iso_week(until)
    trades = _load_trades(cfg, bot, since_ts=since_ts, until_ts=until)
    if mode:
        trades = [t for t in trades if t.mode == mode]
    client = _maybe_bybit_client(cfg, bot)
    paper, live = _pnl_summaries(trades, cfg, bot, since_ts=since_ts,
                                 until_ts=until, client=client)

    mfe_fn = None
    if cfg.mfe_enabled and client is not None:
        from tradecard_bybit.data.mfe import make_mfe_provider
        mfe_fn = make_mfe_provider(client)

    result = run_detection(trades, bot=bot, cfg=cfg, mfe_fn=mfe_fn)
    grades = _grade_by_strategy(trades, cfg)

    db = TradecardDB(cfg.db_path)
    five_why = None
    momentum_lines: list[str] = []
    try:
        # фиксируем тему №1 + частоты + (опц.) 5 Why
        if result.top_theme is not None:
            five_why = _record_theme_and_five_why(
                db, cfg, bot, week, result, trades, dry_run=dry_run)
        # OOS-проверка внедрённых гипотез → small wins
        momentum_lines = _evaluate_small_wins(db, cfg, bot, week)
        small_win_count = db.small_win_count(bot)

        report = build_weekly_report(
            bot=bot, week=week, pnl_paper=paper, pnl_live=live,
            findings=result.findings, top_theme=result.top_theme,
            five_why=five_why, grade_by_strategy=grades,
            small_win_count=small_win_count, momentum_lines=momentum_lines,
            baseline_note=baseline_note)

        os.makedirs(cfg.reports_dir, exist_ok=True)
        path = os.path.join(cfg.reports_dir, f"{bot}_{week}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report)
        log.info("weekly report card записан: %s", path)
        print(report)

        if not dry_run:
            summary = (f"<b>tradecard {bot}</b> — weekly {week}\n"
                       f"тема №1: "
                       f"{result.top_theme.code if result.top_theme else '—'} | "
                       f"small wins накоплено: {small_win_count}\n"
                       f"отчёт: {path}")
            _send_telegram(cfg, bot, summary)
    finally:
        db.close()
    return 0


def _record_theme_and_five_why(db: TradecardDB, cfg, bot: str, week: str,
                               result, trades: list[Trade], *, dry_run: bool):
    top = result.top_theme
    theme_id = db.upsert_theme(bot=bot, mode=top.mode, code=top.code,
                               scope=top.scope, week=week, strategy=top.strategy)
    # частота темы за неделю (паттерн/100 trades) — для momentum/OOS
    n_decided = sum(1 for t in trades
                    if t.is_decided and (top.mode in ("paper", "live")
                                         and t.mode == top.mode or top.mode == "mixed"))
    db.record_freq(theme_id=theme_id, bot=bot, mode=top.mode, week=week,
                   n_pattern=top.n, n_trades=max(n_decided, top.n))

    if not result.sample_ok:
        log.info("тема №1 (%s) ниже порога sample-size (n=%d<%d) — НАБЛЮДЕНИЕ, "
                 "5 Why не запускаю", top.code, top.n, cfg.min_trades_for_theme)
        return None
    if not cfg.five_why_enabled:
        return None
    if not cfg.deepseek_api_key:
        log.info("5 Why: нет DEEPSEEK_API_KEY — пропускаю LLM-диагностику")
        return None

    from tradecard_bybit.llm.client import DeepSeekClient
    from tradecard_bybit.llm.five_why import run_five_why
    client = DeepSeekClient(cfg.deepseek_api_key, base_url=cfg.deepseek_base_url,
                            model=cfg.deepseek_model,
                            max_tokens=cfg.deepseek_max_tokens,
                            thinking_enabled=cfg.deepseek_thinking)
    samples = [t for t in trades if t.id in set(top.trade_ids)][:5]
    fw = run_five_why(client, code=top.code, strategy=top.strategy,
                      scope=top.scope, n=top.n, wr=top.wr, exp_r=top.exp_r,
                      net=top.net, samples=samples)
    if fw.hypothesis and not dry_run:
        import json
        db.set_theme_status(theme_id, "diagnosed")
        db.add_hypothesis(theme_id=theme_id, bot=bot, text=fw.hypothesis,
                          five_why=json.dumps(fw.chain, ensure_ascii=False))
    return fw


def _evaluate_small_wins(db: TradecardDB, cfg, bot: str, week: str) -> list[str]:
    """OOS-проверка внедрённых человеком гипотез (implemented_week проставлен)."""
    lines: list[str] = []
    for hyp in db.implemented_hypotheses(bot):
        theme = db.get_theme(hyp.theme_id)
        if theme is None or hyp.implemented_week is None:
            continue
        chk = evaluate_small_win(
            db, hypothesis_id=hyp.id, theme_id=hyp.theme_id, mode=theme.mode,
            implemented_week=hyp.implemented_week,
            min_trades=cfg.min_trades_for_theme,
            min_weeks=2, significance_p=cfg.significance_p)
        lines.append(f"гипотеза #{hyp.id} ({theme.code}): {chk.detail}")
        if chk.status == "small_win" and not db.has_small_win(hyp.id, week):
            db.add_small_win(
                hypothesis_id=hyp.id, theme_id=hyp.theme_id, bot=bot,
                mode=theme.mode, validated_week=week,
                baseline_freq=chk.baseline_freq, oos_freq=chk.oos_freq,
                p_value=chk.p_value or 1.0, n_oos=chk.n_oos)
            db.set_hypothesis_status(hyp.id, "win")
            db.set_theme_status(hyp.theme_id, "small_win")
            lines.append(f"  → 🏆 SMALL WIN зафиксирован (OOS, p={chk.p_value:.3f})")
    return lines


def _send_telegram(cfg: TradecardBybitSettings, bot: str, text: str) -> None:
    enabled, token, chat, prefix = cfg.telegram_for(bot)
    notifier = TelegramNotifier(token, chat, enabled=enabled, prefix=prefix)
    notifier.send(text)


# ─── entrypoint ──────────────────────────────────────────────────────────

def run() -> int:
    cfg = load_settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    ap = argparse.ArgumentParser(prog="tradecard-bybit",
                                 description="advisory-ревьюер scalp/flowzone")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("daily", "weekly"):
        p = sub.add_parser(name)
        p.add_argument("--bot", required=True, choices=["scalp", "flowzone"])
        p.add_argument("--since", default=None, help="YYYY-MM-DD (UTC)")
        p.add_argument("--dry-run", action="store_true",
                       help="не слать Telegram, не писать гипотезы")
        mg = p.add_mutually_exclusive_group()
        mg.add_argument("--paper", action="store_const", dest="mode",
                        const="paper", help="только paper-сделки")
        mg.add_argument("--live", action="store_const", dest="mode",
                        const="live", help="только live-сделки")
        p.set_defaults(mode=None)
    args = ap.parse_args()

    if args.cmd == "daily":
        return cmd_daily(cfg, args.bot, since=args.since, dry_run=args.dry_run,
                         mode=args.mode)
    return cmd_weekly(cfg, args.bot, since=args.since, dry_run=args.dry_run,
                      mode=args.mode)


if __name__ == "__main__":
    raise SystemExit(run())
