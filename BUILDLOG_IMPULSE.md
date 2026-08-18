# Build Log — impulse-bot

Изолирован от `scalp_bot` / `horizon_bot` / `flowzone`. Bybit linear, демо.

## 2026-08-18

### feat(impulse): Telegram вход/выход на SCALP_TELEGRAM_*
`pending`

Исходящий sendMessage без поллинга (тот же токен, что у scalp). Старт,
вход, выход (SL/TP/scratch/broker_flat). Торговая логика не менялась.
https://core.telegram.org/bots/api#sendmessage

**Файлы:** `src/impulse_bot/telegram.py`, `app/main.py`, `settings.py`,
`docker-compose.yml`, `tests/test_impulse_bot.py`

### chore(impulse): heartbeat цикла в лог
`c43ec76`

Без удара контейнер писал только строку старта. Добавлен `цикл open/sess/uni`.
Торговая логика не менялась.

**Файлы:** `src/impulse_bot/app/main.py`

### feat(impulse): автомат удара на perp-альтах (лента+кластер)
`398dcd0`

Пользователь: полный автомат на Bybit perp-альтах из скрина форумов.
Не опираться на P&L scalp и старый замер «удар $30k» как на причину
не делать. Параметры — посты, не тюнинг.

**Правила.** Bitcointalk 5577812: не BTC/ETH/SOL, оборот $100k–$15M,
удар ≥$30k / ~15с и ход ≥0.2%. CScalp: вход только если лента и кластер
подтверждают сторону. FF 1014708: лондон 07–16 UTC, тейк 0.45% ≫ VIP 0
RT 0.110%. Плечо 10×, риск 1.5% equity, scratch 90с. Market + SL/TP на
ордере. Чужие позиции на общем демо не трогает.

**API.** tickers / recent-trade / create-order — Bybit v5
https://bybit-exchange.github.io/docs/v5/market/recent-trade
https://bybit-exchange.github.io/docs/v5/order/create-order

**Файлы:** `src/impulse_bot/`, `Dockerfile.impulse-bot`, `docker-compose.yml`,
`tests/test_impulse_bot.py`, `STRATEGY_RATIONALE_IMPULSE.md`
