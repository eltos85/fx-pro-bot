"""CLI для OAuth2 авторизации cTrader Open API.

Запуск: fx-pro-auth
Шаги:
1. Выводит URL для авторизации в браузере
2. Пользователь авторизуется и копирует code из redirect URL
3. Обменивает code на access/refresh токены
4. Сохраняет токены в data/ctrader_tokens.json
"""

from __future__ import annotations

import re
import sys

from fx_pro_bot.config.settings import Settings
from fx_pro_bot.trading.auth import (
    TokenStore,
    exchange_code_for_tokens,
    get_auth_url,
    refresh_access_token,
)


def main() -> None:
    settings = Settings()
    token_store = TokenStore(settings.ctrader_token_path)

    if not settings.ctrader_client_id or not settings.ctrader_client_secret:
        print("Ошибка: CTRADER_CLIENT_ID и CTRADER_CLIENT_SECRET не заданы в .env")
        sys.exit(1)

    if not settings.ctrader_redirect_uri:
        print("Ошибка: CTRADER_REDIRECT_URI не задан в .env")
        sys.exit(1)

    existing = token_store.load()
    if existing.access_token and existing.refresh_token:
        print(f"Найдены существующие токены (expire: {existing.expires_at})")
        choice = input("Обновить через refresh token? [y/N]: ").strip().lower()
        if choice == "y":
            try:
                new_token = refresh_access_token(
                    existing.refresh_token,
                    settings.ctrader_client_id,
                    settings.ctrader_client_secret,
                )
                token_store.save(new_token)
                print("Токены обновлены!")
                return
            except Exception as exc:
                print(f"Ошибка обновления: {exc}")
                print("Выполняем полную авторизацию...")

    url = get_auth_url(settings.ctrader_client_id, settings.ctrader_redirect_uri)
    print("\n" + "=" * 60)
    print("Откройте эту ссылку в браузере и авторизуйтесь:")
    print(f"\n  {url}\n")
    print("После авторизации вас перенаправит на redirect URI.")
    print("Скопируйте code из URL (параметр ?code=...).")
    print("=" * 60)

    raw = input("\nВставьте code (или полный URL): ").strip()

    code = _extract_code(raw)
    if not code:
        print("Ошибка: не удалось извлечь code")
        sys.exit(1)

    print(f"Code: {code[:20]}...")

    try:
        token = exchange_code_for_tokens(
            code=code,
            client_id=settings.ctrader_client_id,
            client_secret=settings.ctrader_client_secret,
            redirect_uri=settings.ctrader_redirect_uri,
        )
        token_store.save(token)
        print("\nАвторизация успешна!")
        print(f"  Access token: {token.access_token[:20]}...")
        print(f"  Refresh token: {token.refresh_token[:20]}...")
        print(f"  Истекает: через ~30 дней")
        print(f"\nТокены сохранены в: {settings.ctrader_token_path}")
    except Exception as exc:
        print(f"\nОшибка авторизации: {exc}")
        sys.exit(1)


def _extract_code(raw: str) -> str:
    """Извлечь code из полного URL или из чистого значения."""
    match = re.search(r"[?&]code=([^&\s]+)", raw)
    if match:
        return match.group(1)
    return raw


if __name__ == "__main__":
    main()
