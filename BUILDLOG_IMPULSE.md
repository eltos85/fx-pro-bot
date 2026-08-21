# Build Log — impulse-bot

Изолирован от `scalp_bot` / `horizon_bot` / `flowzone`. Bybit linear, демо.

## 2026-08-21

### change(impulse): риск от виртуальных $1000, не от живого демо

Решение пользователя. Риск 1.5% считался от живого счёта (~$47 600 → $714,
лот до $280k, биржа обрезала по maxMktOrderQty). Теперь капитал —
`min(живой, VIRTUAL_CAPITAL)`, по умолчанию $1000 → риск $15. Сигнал и
SL/TP не менялись. Открытых позиций не было.

**Файлы:** `src/impulse_bot/settings.py`, `src/impulse_bot/app/main.py`,
`tests/test_impulse_bot.py`, `docker-compose.yml`, `.env.example`

## 2026-08-19

### fix(impulse): обрезать Market qty до maxMktOrderQty
`eae6961`

Вчера 22 входа (лента+кластер ок) отклонены Bybit 10001: расчётный лот
(1.5% equity / 0.25% SL ≈ $274k нотионал) больше лимита контракта.
Сигнал не меняем. Обрезаем до `lotSizeFilter.maxMktOrderQty`
(офдок https://bybit-exchange.github.io/docs/v5/market/instrument),
не `maxOrderQty` — тот для лимиток. Риск в $ на дешёвых альтах станет
меньше потолка 1.5%.

**Файлы:** `src/impulse_bot/signals.py`, `client.py`, `app/main.py`,
`tests/test_impulse_bot.py`, `STRATEGY_RATIONALE_IMPULSE.md`

## 2026-08-18

### feat(impulse): Telegram вход/выход на SCALP_TELEGRAM_*
`16024ca`

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
