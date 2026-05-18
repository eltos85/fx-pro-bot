"""ctrader-token-service — централизованный refresh-сервис для cTrader OAuth.

Решает проблему **rotation conflict** при shared cTrader account между
несколькими ботами (Advisor + fx-ai-trader): cTrader использует
**rotating refresh_tokens** (каждый refresh инвалидирует предыдущий
refresh_token). Если два бота независимо делают refresh — один из них
получает «Access denied» и теряет валидную сессию.

Архитектура:
- один процесс держит **единственный** token-store на диске;
- API: ``GET /token`` (свежий access), ``POST /refresh`` (force refresh
  с dedup-окном), ``POST /token`` (push from bot когда тот сам refresh-нул);
- background-таймер pro-actively refresh-ит за ``REFRESH_MARGIN_SEC``
  до expiry, чтобы боты получали уже свежий токен без задержки;
- HTTP-Bearer auth через shared secret.

Боты используют ``shared_oauth.token_client`` как drop-in замену для
``ensure_valid_token`` / ``ensure_valid_token_race_safe``.

Research basis:
- Spotware OpenAPI docs: refresh_token = single-use, ~6 weeks TTL.
- Auth0 «Refresh Token Rotation» (https://auth0.com/docs/secure/tokens/refresh-tokens/refresh-token-rotation)
- Nango «How to handle concurrency with OAuth token refreshes» (singleflight pattern).
- Coder PR #22904 (optimistic locking) — но у нас в одном процессе,
  поэтому threading.Lock достаточно.
"""
