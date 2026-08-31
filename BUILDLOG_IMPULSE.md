# Build Log — impulse-bot

Изолирован от `scalp_bot` / `horizon_bot` / `flowzone`. Bybit linear, демо.

## 2026-08-31

### docs(impulse): фактическая комиссия 0.128% за круг вместо 0.110%

Наблюдение, торговая логика не менялась. Канон обосновывал тейк 0.45%
как «~4× комиссии» при VIP 0 RT 0.110%. Замер по `execFee` показал
**0.0642% на сторону = 0.128% за круг**, то есть тейк это 3.5×, а не 4×.
Порог TP/SL оставлен прежним по решению пользователя: выборки не хватает.

Заодно исправлена методика атрибуции. `closed_pnl.orderId` — это ID
**закрывающего** ордера, а закрытия по биржевому SL/TP Bybit создаёт сам,
без префикса `impulse_`. Фильтр только по префиксу занижал выборку вдвое
(38 сделок вместо 80) и убыток втрое ($199.81 вместо $637.87). Теперь
закрытия матчатся вторым проходом по (symbol, время) из локальной SQLite,
а скрипт печатает проверку согласованности (API = БД = филлы входа = филлы
выхода).

**Артефакт:** `scripts/collect_impulse_stats.py --days 10`
**Выборка:** 80 сделок, 2026-08-21 07:39 → 2026-08-31 07:39 UTC (1.4 недели),
demo, оборот $936 109, 160 филлов, проверка согласованности пройдена.

**Метрики:**
- Net PnL −$637.87, WR 28.7% (23/80), средний win $12.87 / loss $16.38, R:R 0.79
- Комиссии $600.64 = **94% убытка**; PnL без комиссий −$37.23 (−0.008% на сделку)
- Средний ход цены −0.0112% при пороге безубытка +0.128% → разрыв 0.14 п.п.
- Выходы: биржевой SL/TP 42 (SL 30 / TP 12, медиана 16с), scratch 38 (медиана 92с)
- До целей канона доходит 35%: TP 9 (11%), SL 19 (24%), обрезано между 52 (65%)
- p(WR≠50%)=0.0002, t(PnL≠0)=−4.49 p<0.0001 — значимо, но 80<100 сделок
  и 1.4<2 недели, поэтому по `sample-size.mdc` вердикт «мало данных»:
  пороги, scratch и отключение инструментов не трогаем.

Проверенная и отклонённая гипотеза: вход лимиткой post-only вместо Market
сэкономил бы 0.044% на сделку ($207 за период), убыток стал бы −$431 —
стратегия осталась бы убыточной, эффекта не хватает.

**Файлы:** `STRATEGY_RATIONALE_IMPULSE.md`, `src/impulse_bot/settings.py`
(комментарий), `scripts/collect_impulse_stats.py` (новый)

## 2026-08-24

### change(impulse): в Telegram только вход и выход

Убран стартовый «impulse старт demo=…». Логи цикла в чат не шли — спамил
рестарт. Порог, риск и сессия не менялись.

**Файлы:** `src/impulse_bot/app/main.py`

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
