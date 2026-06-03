"""Один прогон дайджеста (для cron / отладки).

python -m ru_stocks_analyst.app.run_once
"""
from __future__ import annotations

import logging
import sys

from ru_stocks_analyst.app.main import run_cycle
from ru_stocks_analyst.news.rss import DEFAULT_FEEDS, RuNewsAggregator
from ru_stocks_analyst.config.settings import load_settings
from ru_stocks_analyst.telegram.notifier import TelegramNotifier
from ru_stocks_analyst.state.store import SignalStore
from ru_stocks_analyst.tinkoff.accounts import pick_brokerage_account
from ru_stocks_analyst.tinkoff.rest_client import TinkoffInvestError, TinkoffRestClient


def main() -> None:
    cfg = load_settings()
    logging.basicConfig(level=cfg.log_level)
    if not cfg.tinkoff_token:
        print("RU_STOCKS_TINKOFF_TOKEN?", file=sys.stderr)
        sys.exit(1)
    client = TinkoffRestClient(cfg.tinkoff_token, cfg.effective_api_base)
    accounts = client.get_accounts()
    account = pick_brokerage_account(accounts, preferred_id=cfg.account_id)
    tg = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id,
                          enabled=cfg.telegram_enabled)
    store = SignalStore(cfg.data_dir)
    news_agg = None
    if cfg.news_enabled:
        feeds = cfg.parse_rss_feeds()
        news_agg = RuNewsAggregator(
            feeds=feeds if feeds else DEFAULT_FEEDS,
            max_age_hours=cfg.news_max_age_hours,
        )
    try:
        run_cycle(cfg, client, account, tg, store, news_agg=news_agg, force_digest=True)
    except TinkoffInvestError as e:
        print(e, file=sys.stderr)
        sys.exit(2)
    print("OK")


if __name__ == "__main__":
    main()
