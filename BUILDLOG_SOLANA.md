# Build Log — solana-bot

Изолирован от Bybit-ботов. Скан щитков Solana; свап выключен по умолчанию.

## 2026-08-24

### change(solana): бот выключен

Решение пользователя: не сканит, не пишет в TG. `SOLANA_ENABLED=false`
по умолчанию, телеграм тоже выкл. Контейнер можно остановить.

**Файлы:** `src/solana_bot/settings.py`, `app/main.py`, `docker-compose.yml`

## 2026-08-18

### feat(solana): Telegram кандидат/вход/выход на SCALP_TELEGRAM_*
`16024ca`

Исходящий sendMessage без поллинга. Пока свап выкл — кандидат (антиспам
30 мин на минт). https://core.telegram.org/bots/api#sendmessage

**Файлы:** `src/solana_bot/telegram.py`, `app/main.py`, `settings.py`,
`docker-compose.yml`, `tests/test_solana_bot.py`

### chore(solana): heartbeat цикла в лог
`c43ec76`

Строка `цикл open/seen` чтобы скан было видно без кандидата.

**Файлы:** `src/solana_bot/app/main.py`

### feat(solana): отдельный бот щитков (скан, Jupiter опционально)
`398dcd0`

Пользователь: отдельный бот на Solana, не в одном контейнере с impulse.
Teletype lexdollar: объём ≥$100k / 5 мин, цели +7% / кап +30%.
Стоп −12%, возраст пула ≥30 мин, ликвидность ≥$25k — риск-капы
(в посте стопа нет).

**Скан.** GeckoTerminal `GET /api/v2/networks/solana/trending_pools?duration=5m`
(CoinGecko onchain: https://docs.coingecko.com/reference/trending-pools-network).
Цена открытой: Dexscreener `GET /tokens/v1/solana/{mint}`
https://docs.dexscreener.com/api/reference

**Исполнение.** Jupiter Swap API v2 `/order` + `/execute`
https://developers.jup.ag/docs/swap/order-and-execute
Keyless 0.5 RPS. `SOLANA_TRADING_ENABLED=false`. `solders` только в
extra `[solana]`.

**Файлы:** `src/solana_bot/`, `Dockerfile.solana-bot`, `docker-compose.yml`,
`tests/test_solana_bot.py`, `STRATEGY_RATIONALE_SOLANA.md`
