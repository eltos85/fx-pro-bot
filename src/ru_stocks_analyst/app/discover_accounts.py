"""Показать счета Tinkoff — выбрать RU_STOCKS_ACCOUNT_ID для брокерского.

Запуск: python -m ru_stocks_analyst.app.discover_accounts
"""
from __future__ import annotations

import logging
import sys

from ru_stocks_analyst.config.settings import load_settings
from ru_stocks_analyst.tinkoff.accounts import format_accounts_list, pick_brokerage_account
from ru_stocks_analyst.tinkoff.rest_client import TinkoffInvestError, TinkoffRestClient


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_settings()
    if not cfg.tinkoff_token:
        print("Задайте RU_STOCKS_TINKOFF_TOKEN в .env", file=sys.stderr)
        sys.exit(1)
    try:
        client = TinkoffRestClient(cfg.tinkoff_token, cfg.effective_api_base)
        accounts = client.get_accounts()
    except TinkoffInvestError as e:
        print(f"Ошибка API: {e}", file=sys.stderr)
        sys.exit(2)

    print("Счета Tinkoff Invest:\n")
    print(format_accounts_list(accounts))
    try:
        picked = pick_brokerage_account(accounts, preferred_id=cfg.account_id)
        print(f"\nРекомендуемый брокерский: RU_STOCKS_ACCOUNT_ID={picked.get('id')}")
    except ValueError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
