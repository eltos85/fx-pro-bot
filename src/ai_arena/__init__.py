"""AI Arena — клон публичной архитектуры Nof1.ai Alpha Arena на Bybit.

Источники правды (см. правило `.cursor/rules/ai-arena-sources.mdc`):
- https://nof1.ai/blog/TechPost1
- https://gist.github.com/wquguru/7d268099b8c04b7e5b6ad6fae922ae83

Изолированная кодовая база — НЕ импортирует из ai_trader / bybit_bot /
fx_pro_bot / fx_ai_trader. Своя БД, свой Bybit API ключ, свой
Docker-сервис, свой Telegram-токен.
"""
