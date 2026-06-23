"""CLI tradecard-momentum (advisory-ревьюер fx_momentum_bot).

Команды:
  tradecard-momentum daily  [--since YYYY-MM-DD] [--dry-run]
  tradecard-momentum weekly [--since YYYY-MM-DD] [--dry-run]

Периодический режим (cron/scheduler), НЕ realtime. Читает БД momentum-бота
read-only (контекст входа) и cTrader deal-list (ground truth по P&L), считает
report card, грейдит по силе сигнала, гоняет 5 Why (weekly), отдаёт отчёт
человеку. НИЧЕГО не пишет в БД бота и НЕ меняет его конфиг (advisory-only).
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import UTC, datetime

from tradecard_momentum.analysis.engine import run_detection
from tradecard_momentum.analysis.grading import grade_curve
from tradecard_momentum.analysis.pnl import summarize, summarize_by_symbol
from tradecard_momentum.analysis.small_wins import evaluate_small_win
from tradecard_momentum.analysis.trade import MomentumTrade, decided
from tradecard_momentum.config.settings import (TradecardMomentumSettings,
                                                load_settings)
from tradecard_momentum.data.momentum_db import EntryDecision, MomentumDBReadOnly
from tradecard_momentum.report.digest import build_daily_digest
from tradecard_momentum.report.telegram import TelegramNotifier
from tradecard_momentum.report.weekly import build_weekly_report
from tradecard_momentum.state.db import TradecardDB

log = logging.getLogger("tradecard_momentum")

_BOT = "momentum"
_MODE = "live"


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


def _load_floor(since_ts: float, cfg: TradecardMomentumSettings) -> float:
    mb = cfg.baseline_ts()
    return max(since_ts, mb) if mb is not None else since_ts


def _filter_baseline(trades: list[MomentumTrade], cfg: TradecardMomentumSettings,
                     ) -> tuple[list[MomentumTrade], str | None]:
    base = cfg.baseline_ts()
    if base is None:
        return trades, None
    kept = [t for t in trades if t.ts_open >= base]
    dt = datetime.fromtimestamp(base, tz=UTC)
    fmt = ("%Y-%m-%d" if (dt.hour, dt.minute, dt.second) == (0, 0, 0)
           else "%Y-%m-%d %H:%M")
    note = (f"Точка отсчёта (baseline последней правки логики, UTC): "
            f"{dt.strftime(fmt)}. Более ранние сделки исключены.")
    return kept, note


def _iso_week(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, tz=UTC).isocalendar()
    return f"{y}-{w:02d}"


# ─── data loading ────────────────────────────────────────────────────────

def _load_decisions(cfg: TradecardMomentumSettings, *, since_ts: float,
                    until_ts: float) -> list[EntryDecision]:
    path = cfg.momentum_db_path
    if not os.path.exists(path):
        log.warning("БД momentum не найдена (%s) — контекст входа недоступен, "
                    "грейд/R по силе сигнала будут пусты", path)
        return []
    with MomentumDBReadOnly(path) as db:
        return db.executed_decisions(since_ts=since_ts, until_ts=until_ts)


def _load_trades(cfg: TradecardMomentumSettings, *, since_ts: float,
                 until_ts: float) -> tuple[list[MomentumTrade], bool]:
    """Сделки из cTrader deal-list (ground truth). Возвращает (trades, broker_ok)."""
    if not cfg.broker_pnl_enabled:
        log.info("broker_pnl_enabled=false — пропускаю cTrader deal-list")
        return [], False
    # Решения грузим с запасом по времени (вход мог быть до окна анализа).
    decisions = _load_decisions(cfg, since_ts=since_ts - 7 * 86400,
                                until_ts=until_ts)
    try:
        from tradecard_momentum.data.broker import fetch_momentum_trades
        trades = fetch_momentum_trades(cfg, since_ts=since_ts, until_ts=until_ts,
                                       decisions=decisions)
        return trades, True
    except Exception:  # noqa: BLE001
        log.exception("cTrader deal-list fetch failed — отчёт без ground truth")
        return [], False


# ─── commands ────────────────────────────────────────────────────────────

def cmd_daily(cfg: TradecardMomentumSettings, *, since: str | None,
              dry_run: bool) -> int:
    until = time.time()
    since_ts = _load_floor(_since_ts(since, 24 * 3600), cfg)
    trades, broker_ok = _load_trades(cfg, since_ts=since_ts, until_ts=until)
    trades, baseline_note = _filter_baseline(trades, cfg)

    pnl = summarize(trades)
    result = run_detection(trades, cfg=cfg)
    grade = grade_curve(decided(trades), buckets=cfg.grade_buckets,
                        min_rho=cfg.grade_monotonic_min_rho)

    date_label = datetime.fromtimestamp(until, tz=UTC).strftime("%Y-%m-%d")
    digest = build_daily_digest(date_label=date_label, pnl=pnl,
                                findings=result.findings, grade=grade,
                                baseline_note=baseline_note, broker_ok=broker_ok)
    print(digest)
    if not dry_run:
        _send_telegram(cfg, digest)
    return 0


def cmd_weekly(cfg: TradecardMomentumSettings, *, since: str | None,
               dry_run: bool) -> int:
    until = time.time()
    since_ts = _load_floor(_since_ts(since, 7 * 24 * 3600), cfg)
    week = _iso_week(until)
    trades, broker_ok = _load_trades(cfg, since_ts=since_ts, until_ts=until)
    trades, baseline_note = _filter_baseline(trades, cfg)

    pnl = summarize(trades)
    result = run_detection(trades, cfg=cfg)
    grade = grade_curve(decided(trades), buckets=cfg.grade_buckets,
                        min_rho=cfg.grade_monotonic_min_rho)

    db = TradecardDB(cfg.db_path)
    five_why = None
    momentum_lines: list[str] = []
    try:
        if result.top_theme is not None:
            five_why = _record_theme_and_five_why(db, cfg, week, result, trades,
                                                  dry_run=dry_run)
        momentum_lines = _evaluate_small_wins(db, cfg, week)
        small_win_count = db.small_win_count(_BOT)

        report = build_weekly_report(
            week=week, pnl=pnl, findings=result.findings,
            top_theme=result.top_theme, five_why=five_why, grade=grade,
            small_win_count=small_win_count, momentum_lines=momentum_lines,
            symbol_pnl=summarize_by_symbol(trades), baseline_note=baseline_note,
            broker_ok=broker_ok)

        os.makedirs(cfg.reports_dir, exist_ok=True)
        path = os.path.join(cfg.reports_dir, f"momentum_{week}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report)
        log.info("weekly report card записан: %s", path)
        print(report)

        if not dry_run:
            summary = (f"<b>tradecard momentum</b> — weekly {week}\n"
                       f"тема №1: "
                       f"{result.top_theme.code if result.top_theme else '—'} | "
                       f"small wins накоплено: {small_win_count}\n"
                       f"отчёт: {path}")
            _send_telegram(cfg, summary)
    finally:
        db.close()
    return 0


def _record_theme_and_five_why(db: TradecardDB, cfg: TradecardMomentumSettings,
                               week: str, result, trades: list[MomentumTrade],
                               *, dry_run: bool):
    top = result.top_theme
    theme_id = db.upsert_theme(bot=_BOT, mode=_MODE, code=top.code,
                               scope=top.scope, week=week, strategy=top.strategy)
    n_decided = sum(1 for t in trades if t.is_decided)
    db.record_freq(theme_id=theme_id, bot=_BOT, mode=_MODE, week=week,
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

    from tradecard_momentum.llm.client import DeepSeekClient
    from tradecard_momentum.llm.five_why import run_five_why
    client = DeepSeekClient(cfg.deepseek_api_key, base_url=cfg.deepseek_base_url,
                            model=cfg.deepseek_model,
                            max_tokens=cfg.deepseek_max_tokens,
                            thinking_enabled=cfg.deepseek_thinking)
    sample_ids = set(top.trade_ids)
    samples = [t for t in trades if t.position_id in sample_ids][:5]
    fw = run_five_why(client, code=top.code, scope=top.scope, n=top.n, wr=top.wr,
                      exp_r=top.exp_r, net=top.net, samples=samples)
    if fw.hypothesis and not dry_run:
        import json
        db.set_theme_status(theme_id, "diagnosed")
        db.add_hypothesis(theme_id=theme_id, bot=_BOT, text=fw.hypothesis,
                          five_why=json.dumps(fw.chain, ensure_ascii=False))
    return fw


def _evaluate_small_wins(db: TradecardDB, cfg: TradecardMomentumSettings,
                         week: str) -> list[str]:
    lines: list[str] = []
    for hyp in db.implemented_hypotheses(_BOT):
        theme = db.get_theme(hyp.theme_id)
        if theme is None or hyp.implemented_week is None:
            continue
        chk = evaluate_small_win(
            db, hypothesis_id=hyp.id, theme_id=hyp.theme_id, mode=theme.mode,
            implemented_week=hyp.implemented_week,
            min_trades=cfg.min_trades_for_theme, min_weeks=2,
            significance_p=cfg.significance_p)
        lines.append(f"гипотеза #{hyp.id} ({theme.code}): {chk.detail}")
        if chk.status == "small_win" and not db.has_small_win(hyp.id, week):
            db.add_small_win(
                hypothesis_id=hyp.id, theme_id=hyp.theme_id, bot=_BOT,
                mode=theme.mode, validated_week=week,
                baseline_freq=chk.baseline_freq, oos_freq=chk.oos_freq,
                p_value=chk.p_value or 1.0, n_oos=chk.n_oos)
            db.set_hypothesis_status(hyp.id, "win")
            db.set_theme_status(hyp.theme_id, "small_win")
            lines.append(f"  → 🏆 SMALL WIN зафиксирован (OOS, p={chk.p_value:.3f})")
    return lines


def _send_telegram(cfg: TradecardMomentumSettings, text: str) -> None:
    enabled, token, chat, prefix = cfg.telegram()
    TelegramNotifier(token, chat, enabled=enabled, prefix=prefix).send(text)


# ─── entrypoint ──────────────────────────────────────────────────────────

def run() -> int:
    cfg = load_settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    ap = argparse.ArgumentParser(prog="tradecard-momentum",
                                 description="advisory-ревьюер fx_momentum_bot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("daily", "weekly"):
        p = sub.add_parser(name)
        p.add_argument("--since", default=None, help="YYYY-MM-DD (UTC)")
        p.add_argument("--dry-run", action="store_true",
                       help="не слать Telegram, не писать гипотезы")
    args = ap.parse_args()

    if args.cmd == "daily":
        return cmd_daily(cfg, since=args.since, dry_run=args.dry_run)
    return cmd_weekly(cfg, since=args.since, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(run())
