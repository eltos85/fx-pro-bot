"""Главный цикл RU Stocks Analyst — советы в Telegram.

Запуск: ``ru-stocks-analyst`` или ``python -m ru_stocks_analyst.app.main``
Один прогон: ``RU_STOCKS_POLL_INTERVAL_SEC=0 ru-stocks-analyst`` (не реализовано —
используйте ``python -m ru_stocks_analyst.app.run_once``).
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import UTC, datetime, timedelta, timezone

from ru_stocks_analyst.analysis.screener import SwingIdea, scan_universe
from ru_stocks_analyst.config.settings import load_settings
from ru_stocks_analyst.data.universe import load_moex_shares, rank_by_last_price
from ru_stocks_analyst.digest.builder import build_morning_digest
from ru_stocks_analyst.llm.brief import summarize_ideas
from ru_stocks_analyst.llm.market_brief import build_market_analysis
from ru_stocks_analyst.news.portfolio import portfolio_tickers
from ru_stocks_analyst.news.rss import DEFAULT_FEEDS, RuNewsAggregator
from ru_stocks_analyst.state.store import SignalStore
from ru_stocks_analyst.telegram.notifier import TelegramNotifier
from ru_stocks_analyst.tinkoff.accounts import pick_brokerage_account
from ru_stocks_analyst.tinkoff.rest_client import TinkoffInvestError, TinkoffRestClient

log = logging.getLogger("ru_stocks")
_shutdown = False
_last_morning_date: str | None = None

MSK = timezone(timedelta(hours=3))


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    _shutdown = True
    log.info("Сигнал %d — выход", signum)


def _should_run_morning_digest(cfg) -> bool:
    global _last_morning_date
    now_msk = datetime.now(MSK)
    today = now_msk.date().isoformat()
    if _last_morning_date == today:
        return False
    if (
        now_msk.hour > cfg.morning_digest_hour_msk
        or (
            now_msk.hour == cfg.morning_digest_hour_msk
            and now_msk.minute >= cfg.morning_digest_minute_msk
        )
    ):
        return True
    return False


def run_cycle(
    cfg,
    client: TinkoffRestClient,
    account: dict,
    tg: TelegramNotifier,
    store: SignalStore,
    *,
    news_agg: RuNewsAggregator | None = None,
    force_digest: bool = False,
) -> str | None:
    """Полный цикл: портфель + скринер. Возвращает дату утреннего дайджеста если отправлен."""
    account_id = account["id"]
    portfolio = client.get_portfolio(account_id)

    shares = load_moex_shares(client)
    ranked = rank_by_last_price(
        client,
        shares,
        min_price_rub=cfg.min_price_rub,
        top_n=cfg.universe_top_n,
    )
    log.info("Скринер: свечи для %d тикеров", len(ranked))
    ideas = scan_universe(client, ranked, candle_days=cfg.candle_days)

    from ru_stocks_analyst.tinkoff.rest_client import quotation_to_float

    tickers = portfolio_tickers(portfolio)
    news_by_ticker: dict = {}
    news_market: list = []
    if cfg.news_enabled and news_agg is not None:
        _, news_by_ticker, news_market = news_agg.collect(tickers)
        log.info(
            "Новости: рынок=%d, по портфелю=%d тикеров с заголовками",
            len(news_market),
            sum(1 for t in tickers if news_by_ticker.get(t)),
        )

    total = quotation_to_float(portfolio.get("totalAmountPortfolio"))
    llm_note = ""
    if cfg.llm_enabled and cfg.deepseek_api_key:
        if cfg.news_enabled:
            llm_note = build_market_analysis(
                api_key=cfg.deepseek_api_key,
                base_url=cfg.deepseek_base_url,
                model=cfg.deepseek_model,
                portfolio_tickers=tickers,
                portfolio_total_rub=total,
                by_ticker=news_by_ticker,
                market=news_market,
                tech_ideas=ideas[: cfg.max_ideas_per_digest],
            )
        elif ideas:
            llm_note = summarize_ideas(
                api_key=cfg.deepseek_api_key,
                base_url=cfg.deepseek_base_url,
                model=cfg.deepseek_model,
                ideas=ideas[: cfg.max_ideas_per_digest],
                portfolio_total_rub=total,
            )

    global _last_morning_date
    morning_sent_date: str | None = None
    now_msk = datetime.now(MSK)
    if force_digest or _should_run_morning_digest(cfg):
        text = build_morning_digest(
            portfolio=portfolio,
            account=account,
            ideas=ideas,
            max_ideas=cfg.max_ideas_per_digest,
            risk_pct=cfg.risk_per_trade_pct,
            portfolio_tickers=tickers,
            news_by_ticker=news_by_ticker,
            news_market=news_market,
            llm_note=llm_note,
            include_news=cfg.news_enabled,
        )
        if not cfg.dry_run:
            tg.send(text)
        else:
            log.info("DRY RUN digest:\n%s", text[:500])
        morning_sent_date = now_msk.date().isoformat()
        _last_morning_date = morning_sent_date

    # Алерты по новым идеям (если утренний дайджест уже отправлен — не дублируем)
    if morning_sent_date:
        return morning_sent_date

    for idea in ideas[: cfg.max_ideas_per_digest]:
        if store.was_sent(idea.ticker, idea.direction):
            continue
        alert = _format_alert(idea)
        if not cfg.dry_run:
            tg.send(alert)
        else:
            log.info("DRY alert: %s", alert[:200])
        store.mark_sent(idea.ticker, idea.direction, datetime.now(UTC).isoformat())

    return morning_sent_date


def _format_alert(idea: SwingIdea) -> str:
    tag = "🟢" if idea.direction == "long" else "🔴"
    return (
        f"{tag} <b>RU Stocks — {idea.ticker}</b> ({idea.direction.upper()}, 1–3 дн.)\n"
        f"~{idea.entry_hint:.2f} ₽ | SL {idea.stop:.2f} | TP {idea.target:.2f}\n"
        f"RSI {idea.rsi14} | {idea.reason}\n"
        f"<i>Не инвестрекомендация. Брокерский счёт.</i>"
    )


def run() -> None:
    cfg = load_settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if not cfg.tinkoff_token:
        log.error("RU_STOCKS_TINKOFF_TOKEN не задан — выход")
        sys.exit(1)

    tg = TelegramNotifier(
        cfg.telegram_bot_token,
        cfg.telegram_chat_id,
        enabled=cfg.telegram_enabled,
    )
    store = SignalStore(cfg.data_dir)

    try:
        client = TinkoffRestClient(cfg.tinkoff_token, cfg.effective_api_base)
        accounts = client.get_accounts()
        account = pick_brokerage_account(accounts, preferred_id=cfg.account_id)
    except (TinkoffInvestError, ValueError) as e:
        log.error("%s", e)
        sys.exit(2)

    log.info(
        "RU Stocks Analyst старт | счёт=%s %s | sandbox=%s | tg=%s",
        account.get("id"),
        account.get("name"),
        cfg.use_sandbox,
        tg.active,
    )

    news_agg: RuNewsAggregator | None = None
    if cfg.news_enabled:
        feeds = cfg.parse_rss_feeds()
        news_agg = RuNewsAggregator(
            feeds=feeds if feeds else DEFAULT_FEEDS,
            cache_ttl_sec=cfg.news_cache_ttl_sec,
            max_age_hours=cfg.news_max_age_hours,
        )

    if tg.active:
        tg.send(
            "🚀 <b>RU Stocks Analyst</b> запущен\n"
            f"Счёт: {account.get('name')} (брокерский)\n"
            f"Новости RSS: {'вкл' if cfg.news_enabled else 'выкл'}\n"
            f"ИИ: {'вкл' if cfg.llm_enabled and cfg.deepseek_api_key else 'выкл'}\n"
            "<i>Только советы, без автосделок.</i>"
        )

    while not _shutdown:
        try:
            run_cycle(cfg, client, account, tg, store, news_agg=news_agg)
        except Exception:
            log.exception("Цикл скринера")
            if tg.active:
                tg.send("❌ RU Stocks: ошибка цикла — см. логи")
        if _shutdown:
            break
        log.info("Сон %d с", cfg.poll_interval_sec)
        for _ in range(cfg.poll_interval_sec):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Остановлен")


if __name__ == "__main__":
    run()
