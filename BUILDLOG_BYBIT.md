# Bybit Crypto Bot — Build Log

## 2026-04-12

### Limit PostOnly для открытия позиций (снижение комиссий)

Было: все ордера открываются Market (taker fee 0.055%).
Стало: `executor.execute()` сначала пытает Limit PostOnly (maker fee 0.02%),
при отклонении — fallback на Market. Закрытие остаётся Market.

Avg open fee: $0.165 → ~$0.076 (экономия ~$0.09/сделку, 22% от total fees).
Торговая логика, параметры стратегий, pair TP, KillSwitch — без изменений.

**Файлы:** `trading/client.py` (новый `place_limit_order`), `trading/executor.py`

---

### Revert OPT 1-6: откат оптимизаций стратегий

Откачены все 6 оптимизаций из коммита `6635cab`. Причина: после деплоя OPT 1-6
пошла серия убытков. Ослабленные фильтры VWAP (deviation 1.5, RSI 35/65, ADX 25)
генерировали слишком много слабых сигналов, а Limit PostOnly ордера не успели
показать эффект (рынок тихий, суббота).

Предыдущая логика (до OPT) давала стабильный gross-плюс. Проблема не в стратегии,
а в комиссиях: при taker fee 0.11% roundtrip и notional ~$310 каждая сделка стоит
~$0.34. Нужно снижать fee без изменения торговой логики.

**Откачено:**
- OPT-1: Limit PostOnly (вернулся Market)
- OPT-2: MIN_CORRELATION 0.7 → 0.5
- OPT-3: Z_EXIT 0.3 → 0.5
- OPT-4: VWAP deviation 1.5→2.0, RSI 35/65→30/70, ADX 25→20
- OPT-5: VOLUME_SPIKE_MULT 2.5 → 3.0
- OPT-6: Динамический pair TP → фикс $2.00

**Файлы:** `app/main.py`, `trading/client.py`, `trading/executor.py`,
`strategies/scalping/stat_arb_crypto.py`, `vwap_crypto.py`, `volume_spike.py`

---

### Реальный PnL из Bybit API + entry_price из API + pair take-profit

Аудит показал расхождение DB vs API: бот записывал расчётный PnL (из uPnL на момент
закрытия), а реальный PnL с учётом комиссий и проскальзывания отличался. Также entry_price
бралась из yfinance вместо реальной цены исполнения.

**Что изменено:**

1. **`_close_and_record`**: после закрытия ордера запрашивает `get_closed_pnl(symbol)` из
   Bybit API и записывает реальный `closedPnl` и `avgExitPrice`. Фоллбэк на uPnL если API
   не вернул данные.

2. **`sync_closed`**: вместо `pnl=0` подтягивает реальный PnL из `closed-pnl` API по
   `startTime` = момент открытия позиции.

3. **`_fetch_entry_price`**: после открытия ордера запрашивает `get_positions()` и берёт
   реальный `avgPrice` вместо yfinance close.

4. **`client.fetch_realized_pnl`**: новый метод — обёртка над `get_closed_pnl` с фильтром
   по символу и времени.

5. **Pair take-profit** (`STATARB_PAIR_TP_USD = $0.80`): Stat-Arb пары теперь закрываются
   не только по z-score, но и когда суммарный uPnL пары >= $0.80. Раньше бот упускал
   прибыль, ожидая z-score сигнал.

6. **TP валидация**: добавлена проверка TP по lastPrice (Buy: TP > lastPrice,
   Sell: TP < lastPrice). Ранее проверялся только SL, из-за чего Bybit отклонял
   ордера AVAXUSDT/FILUSDT с невалидным TP.

7. **Pair TP поднят $0.80 → $2.00**: при $0.80 комиссии (~$0.70 за пару: 4 ордера ×
   ~$0.17) съедали почти всю прибыль. При $2.00 чистая прибыль после комиссий ~$1.30.

**Файлы:** `app/main.py`, `trading/client.py`, `trading/executor.py`

---

### KillSwitch: ослабление лимитов для демо + убрано дублирование дефолтов
`b18788d`

Для демо-торговли расширены пороги KillSwitch — бот получает больше свободы для набора статистики.
При переходе на реал — вернуть к консервативным значениям через env-переменные.

| Параметр | Было | Стало | При $500 |
|---|---|---|---|
| max_drawdown_pct | 10% | 25% | $125 |
| max_daily_loss | $15 (3%) | $37.50 (7.5%) | — |
| max_loss_per_trade | $7.50 (1.5%) | $12.50 (2.5%) | — |

Убрано дублирование: `KillSwitchConfig` dataclass больше не имеет дефолтов —
единственный источник значений теперь `Settings` (env-переменные).

**Файлы:** `config/settings.py`, `trading/killswitch.py`, `docker-compose.yml`, `tests/test_bybit_bot.py`

---

### Fix: time-stop по реальному времени + EXIT-CHECK логирование
`0eec615`

Time-stop использовал `opened_bar_idx` (номер цикла при открытии). При перезапуске контейнера
счётчик сбрасывался → позиции жили дольше лимита. Заменено на `opened_at` (ISO timestamp из БД):
`age_sec = (now - opened_at).total_seconds()`, лимит `TIME_STOP_SECONDS = 15000` (~4.2 часа).

Добавлен INFO-лог `EXIT-CHECK` для каждой открытой позиции в каждом цикле:
`EXIT-CHECK: Buy AVAXUSDT uPnL=0.90 age=63min strat=scalp_statarb pair=sa_AVAXUSDT_ETHUSDT_3b68c6`

Подтверждена работа всех exit-механизмов:
- Z-score exit: закрыл пару LTCUSDT/BTCUSDT (z < 0.5)
- Time-stop: теперь корректен при перезапусках
- Max loss / Emergency / Trailing: готовы, ждут условий

**Файлы:** `src/bybit_bot/app/main.py`

---

### Fix: валидация SL/TP по реальной цене Bybit перед отправкой ордера
`92ed496`

yfinance close price может расходиться с реальной ценой на Bybit.
Для Buy: если SL рассчитан от yfinance-цены (выше реальной), SL оказывается выше lastPrice — Bybit отклоняет ордер.
Пример: ATOMUSDT SL=1.7453 > lastPrice=1.7431 → `InvalidRequestError`.

Теперь `execute()` перед отправкой ордера запрашивает `get_tickers(symbol)` и проверяет:
Buy → SL < lastPrice, Sell → SL > lastPrice. Если невалидно — ордер открывается без SL/TP,
exit-логика `_process_exits()` всё равно закроет по time-stop, max_loss или trailing.

**Файлы:** `src/bybit_bot/trading/executor.py`

---

### Fix: round(None) crash для Stat-Arb позиций без SL/TP
`4822d01`

Stat-Arb стратегия устанавливает sl=None и tp=None (exit через z-score и trailing, не через фиксированные стопы).
Но `compute_trade()` безусловно вызывал `round(sl, price_prec)`, что падало с TypeError.
Добавлена проверка `if sl is not None` / `if tp is not None` перед округлением.

**Файлы:** `src/bybit_bot/trading/executor.py`

---

### Fix: 7 критических проблем exit-логики, KillSwitch и Stat-Arb

Анализ 50 закрытых сделок (PnL -$86.77, win-rate 28%) выявил системные проблемы.
Все исправления основаны на офиц. документации Bybit API v5 и лучших практиках.

**Проблема 1 — Нет exit-логики:** бот только открывал позиции, закрытие только по SL/TP Bybit.
Добавлена `_process_exits()` в каждый цикл: max_loss_per_trade ($7.50), time-stop (50 баров),
Stat-Arb z-score exit, trailing stop через Bybit API (0.7 ATR активация, 0.5 ATR дистанция).

**Проблема 2 — KillSwitch не проверял uPnL:** ETH потеряла $37.42 при лимите $7.50.
Теперь `_process_exits` проверяет `unrealisedPnl` каждой позиции и закрывает при превышении.
`record_trade_close()` вызывается после каждого закрытия. Drawdown считается от account_balance.

**Проблема 3 — Stat-Arb ноги закрывались независимо:** SL на одной ноге оставлял вторую открытой.
Добавлен `pair_tag` в БД. При закрытии одной ноги — немедленно закрывается вторая.
Stat-Arb позиции открываются без SL/TP, exit по z-score < 0.5 или emergency ($15 суммарный убыток).

**Проблема 4 — qty округлялся вверх:** floor вместо round, маржа Stat-Arb делится пополам.

**Проблема 5 — Единый SL/TP для всех стратегий:** Signal расширен полями `sl_atr_mult`, `tp_atr_mult`.
VWAP: SL=2.0/TP=1.5, Funding: SL=1.5/TP=1.0, Volume: SL=2.0/TP=2.0, Momentum: SL=2.0/TP=3.0.

**Проблема 6 — scalp_opened всегда 0:** проверял несуществующее поле PositionInfo.strategy.
Заменено на подсчёт через БД (strategy_name).

**Проблема 7 — trip в scalping не закрывал позиции:** добавлен close_all_positions().

**Bybit API (из офиц. доки):**
- `close_position` теперь использует `reduceOnly=True`
- `set_trailing_stop` через `POST /v5/position/trading-stop` (trailingStop + activePrice)
- `get_closed_pnl` через `GET /v5/position/closed-pnl`

**Файлы:** `analysis/signals.py`, `trading/client.py`, `trading/executor.py`,
`stats/store.py`, `app/main.py`, `tests/test_bybit_bot.py`

---

## 2026-04-11

### Fix: margin cap — уменьшать qty вместо отказа от сделки
`10b3930`

**Симптом:** бот находил 2 сигнала (BTCUSDT, ETHUSDT Stat-Arb) каждый цикл,
но все отклонялись: `маржа $566 > лимит $125 (25% от $500), пропускаю`.
При этом min qty BTC = 0.001 → маржа $16.6 — вполне вписывается.

**Причина:** формула risk sizing давала qty по ATR-риску ($25 / SL_distance),
что для BTC = 0.033 BTC → маржа $548. Вместо уменьшения qty executor просто отказывал.

**Решение:** если маржа > лимита, executor теперь пересчитывает qty вниз:
`max_qty = max_margin * leverage / price`, округляет по qtyStep из API.
Если даже min_order_qty не влезает — тогда пропускает.
Пример: BTC $83K, leverage 5x, лимит $125 → max_qty = 0.007 BTC, маржа $116.

**Файлы:** `trading/executor.py`

---

### Динамическая загрузка инструментов с Bybit API вместо хардкода
`a6a41d7`

**Симптом:** PEPEUSDT — "symbol invalid" на демо, хардкод `min_qty_map` на 38 монет мог не соответствовать реальным правилам биржи.

**Решение:** при старте бота вызывается `GET /v5/market/instruments-info` (из оф. документации Bybit).
Загружаются `minOrderQty`, `qtyStep`, `tickSize`, `minNotionalValue`, `maxLeverage` для каждого символа.
Невалидные символы (не в статусе "Trading") автоматически исключаются из `scan_symbols`.
Убран весь хардкод `min_qty_map` (38 строк) — теперь `_round_qty_api` использует данные API.
Также добавлена проверка `minNotionalValue` — Bybit отклоняет ордера меньше $5 notional.

**Файлы:** `trading/client.py` (`InstrumentInfo`, `get_instruments`), `trading/executor.py` (убран хардкод, `_round_qty_api`), `app/main.py` (фильтрация символов при старте), `tests/test_bybit_bot.py`

---

### Fix: position sizing использовал демо-баланс ($175K) вместо настроек ($500)
`cf677eb`

**Симптом:** скальпинг находил 3 сигнала (PEPEUSDT, DOGEUSDT, ATOMUSDT), но все отклонялись:
`маржа $230522 > лимит $25005 (25% баланса)`. Также PEPEUSDT — "symbol invalid" на демо Bybit.

**Причина:** `compute_trade` получал `available_balance` из API ($100K демо),
но настройки risk management рассчитаны на `account_balance = $500`.
Формула `risk_usd = 100000 * 0.05 = $5000` → огромная позиция → маржа не проходит.

**Решение:** размер позиции считается по `settings.account_balance` ($500),
`available_balance` из API используется только для проверки наличия свободной маржи на бирже.
Теперь: `risk_usd = 500 * 0.05 = $25` → адекватная позиция для $500 счёта.

**Файлы:** `trading/executor.py`, `tests/test_bybit_bot.py`

---

### Debug-логирование скальпинг-стратегий
`e3185d2`

После batch-фикса данные загружаются (38/38), но сигналов 0.
Добавлено verbose-логирование в каждую стратегию:
- VWAP: выводит deviation, ADX, RSI, slope для каждого символа
- Stat-Arb: выводит correlation, beta, z-score для каждой пары
- Volume Spike: выводит vol_ratio для каждого символа
- Убрано дублирование scan (раньше скальпинг сканировался дважды — для лога и для исполнения)

Временно включён LOG_LEVEL=DEBUG для диагностики на VPS.

**Файлы:** `strategies/scalping/vwap_crypto.py`, `stat_arb_crypto.py`, `volume_spike.py`, `app/main.py`, `.env`

---

### Batch-загрузка yfinance — 1 запрос вместо 76
`68a7a0a`

Было: 38 вызовов `yf.Ticker().history()` для `bars_map` + ещё 38 внутри `scan_instruments` = **76 HTTP-запросов** за цикл.
Yahoo лимит ~60 req/min → гарантированный rate limit, часть тикеров теряла данные.

Перешли на `yfinance.download(tickers=[...])` — один batch-запрос с многопоточностью.
`scan_instruments` принимает готовый `bars_map`, не загружает повторно.
Результат на VPS: **38/38 тикеров за 7 сек** одним вызовом.

**Файлы:** `market_data/feed.py` (добавлен `fetch_bars_batch`), `app/main.py`, `analysis/scanner.py`

---

### Создание Bybit крипто-бота — начальная структура

Создан автономный бот для торговли криптовалютой на Bybit, в том же репозитории что и fx_pro_bot, но полностью отдельный пакет — своя логика, свои настройки, своя БД.

**Архитектура (по образу fx_pro_bot):**
- `src/bybit_bot/` — отдельный Python-пакет
- Ансамбль 5 индикаторов (MA+RSI, MACD, Stochastic, Bollinger, EMA Bounce)
- Momentum-стратегия с крипто-фильтрами (объём, волатильность, RSI-зоны)
- Bybit клиент через `pybit` (Unified Trading API v5)
- KillSwitch (дневной лимит, просадка, макс позиций)
- SQLite статистика (сигналы + позиции)

**Инфраструктура:**
- `Dockerfile.bybit` — отдельный образ
- `docker-compose.yml` — сервис `bybit-bot` с volume `bybit_data`
- Все настройки через `BYBIT_BOT_*` env-переменные
- Entry point: `bybit-bot` (CLI) или `python -m bybit_bot.app.main`

**Демо-режим:**
- Подключён Bybit Demo Trading (api-demo.bybit.com)
- Баланс $100K виртуальных USDT
- Торговля пока отключена (`TRADING_ENABLED=false`), только сигналы
- 15 крипто-пар из коробки (BTC, ETH, SOL, XRP, DOGE и др.)

**Тесты:** 12 тестов bybit_bot + 161 тест fx_pro_bot — все проходят (173 total).

**Файлы:**
- `src/bybit_bot/` — весь пакет (app, config, market_data, analysis, trading, strategies, stats)
- `Dockerfile.bybit`, `docker/bybit-entrypoint.sh`
- `docker-compose.yml` (добавлен сервис bybit-bot)
- `pyproject.toml` (добавлен pybit, entry point bybit-bot)
- `.env.example`, `.env` (BYBIT_BOT_* переменные)
- `tests/test_bybit_bot.py`

### Скальпинг-стратегии для крипто

Добавлены 4 скальпинг-стратегии + подпакет индикаторов. Все интегрированы в главный цикл бота.

**1. VWAP Mean-Reversion** (`scalping/vwap_crypto.py`)
- Rolling VWAP по последним 50 барам (без привязки к FX-сессиям)
- Вход: отклонение > 2 ATR + RSI < 30 (long) / > 70 (short)
- Фильтры: ADX ≤ 25 (только боковик), EMA slope (не против наклона)
- SL = 2.0 ATR, TP = 1.5 ATR

**2. Stat-Arb крипто-пары** (`scalping/stat_arb_crypto.py`)
- Пары: BTC/ETH, SOL/ETH, LINK/ETH, LTC/BTC
- OLS hedge ratio (β), z-score спреда (окно 50)
- Вход при |z| ≥ 2.0, выход при |z| < 0.5
- Market-neutral: long одну + short другую

**3. Funding Rate Scalp** (`scalping/funding_scalp.py`)
- Уникально для крипто-перпетуалов (funding каждые 8ч)
- Вход за 30 мин до funding при rate > 0.05%
- rate > 0 → short (лонги платят), rate < 0 → long
- Сила сигнала пропорциональна отклонению rate

**4. Volume Spike Detection** (`scalping/volume_spike.py`)
- Альтернатива копи-трейдингу: ловим "китов" по объёму
- Вход: объём бара ≥ 3x от avg_volume(20) + ценовое движение ≥ 0.5 ATR
- Фильтры: RSI не в экстремуме, тренд совпадает с направлением
- Макс 3 сигнала за скан

**Индикаторы** (`scalping/indicators.py`): VWAP, vwap_series, rolling_z_score, z_score_series, ema_slope, ols_hedge_ratio, spread_series, avg_volume.

**Конфигурация:** `BYBIT_BOT_SCALP_VWAP_ENABLED`, `SCALP_STATARB_ENABLED`, `SCALP_FUNDING_ENABLED`, `SCALP_VOLUME_ENABLED`, `SCALP_MAX_POSITIONS=15`.

**Тесты:** 36 тестов bybit_bot (12 базовых + 24 скальпинг) + 161 fx_pro_bot = 197 total.

**Файлы:**
- `src/bybit_bot/strategies/scalping/` — indicators, vwap_crypto, stat_arb_crypto, funding_scalp, volume_spike
- `src/bybit_bot/config/settings.py` — добавлены scalping_* настройки
- `src/bybit_bot/app/main.py` — интеграция всех стратегий в цикл
- `tests/test_bybit_scalping.py`

### Калибровка стратегий по данным из авторитетных источников

Масштабное исследование стратегий по профессиональным и академическим источникам США. Корректировка параметров на основе бэктестов и рекомендаций топ-трейдеров.

**Источники исследования:**
- Springer Nature (Copula-based pairs trading, 2024)
- SSRN (Trend-following and Mean-Reversion in Bitcoin, 2024)
- Theseus (Bitcoin trading strategies 2020-2025)
- Quant Signals (ATR Stop Loss: 9,433 бэктеста)
- StratBase.ai (ADX filter: 763 бэктеста)
- CryptoProfitCalc (Top 5 Scalping Strategies 2026)
- Trader Dale (Volume Profile + Order Flow guide)
- AlgoStorm (Volume Profile trading)
- Bybit Help Center (Funding Fee документация)
- CoinPerps / KangaAnalytics (live funding rate data)
- FullSwing AI (Crypto Correlation Trading 2025)
- Racthera (ETH vs BTC performance 2023-2025)

**Корректировки:**

1. **VWAP Mean-Reversion** — ADX_MAX снижен с 25 → 20.
   Mean reversion работает только в боковике (ADX < 20).
   Зона 20-25 — серая, избегать. Подтверждено бэктестом 763 конфигураций (StratBase).
   Академическое исследование: BB mean reversion превзошёл momentum на часовых данных,
   но на бычьем рынке 9/11 лучших стратегий — trend-following.

2. **Stat-Arb** — добавлен фильтр корреляции MIN_CORRELATION = 0.5.
   BTC-ETH корреляция 0.75-0.82 в среднем (Springer, Racthera).
   При корреляции < 0.5 — коинтеграция нестабильна.
   Добавлен метод _correlation() для Pearson correlation.
   ETH vol 55-75% vs BTC 45-65% — учитывать при позиционировании.

3. **Funding Rate Scalp** — пороги пересмотрены по live-данным.
   Средний rate BTC = 0.005%, ETH = 0.01% (7-day avg, CoinPerps).
   THRESHOLD: 0.0005 → 0.0003 (0.03%, ~6x от среднего BTC).
   STRONG: 0.001 → 0.0008 (0.08%).
   Добавлен FUNDING_BUFFER_SECONDS = 10 (из документации Bybit: не входить за 5с до funding).

4. **Volume Spike** — SL_ATR_MULT 1.5 → 2.0 (Quant Signals: profit factor 1.72 для BTC).
   Добавлен COOLDOWN_BARS = 5 (First Test Rule от Trader Dale: первый тест уровня
   самый надёжный, повторные тесты ослабляют сигнал).

**Тесты:** 197 passed (все 36 bybit + 161 fx_pro_bot).

**Файлы:**
- `src/bybit_bot/strategies/scalping/vwap_crypto.py` — ADX_MAX 25 → 20
- `src/bybit_bot/strategies/scalping/stat_arb_crypto.py` — MIN_CORRELATION, _correlation()
- `src/bybit_bot/strategies/scalping/funding_scalp.py` — пороги rate, buffer
- `src/bybit_bot/strategies/scalping/volume_spike.py` — SL 2.0 ATR, cooldown

### Риск-менеджмент для микро-счёта $500

Перекалиброван весь слой управления капиталом и risk limits под стартовый депозит $500.
Стратегии (пороги индикаторов, условия входа) НЕ затронуты — изменён только sizing и защита.

**Принцип разделения:**
- Стратегии (`strategies/`) → решают КОГДА и КУДА входить. Не знают про баланс.
- Executor + KillSwitch (`trading/`) → решают СКОЛЬКО и МОЖНО ЛИ. Не знают про индикаторы.

**Расчёт:**
- Формула: `effective_risk = balance × pct / leverage = $500 × 0.05 / 5 = $5` = **1% per trade**
- Leverage 5x нужен для технической возможности открывать крипто-позиции на $500
- При 3 одновременных позициях: макс concurrent risk = $15 = 3% счёта

**Изменения параметров:**

| Параметр | Было | Стало | % от $500 |
|---|---|---|---|
| account_balance | 100,000 | 500 | — |
| leverage | 1x | 5x | — |
| max_positions (momentum) | 10 | 3 | — |
| scalping_max_positions | 15 | 3 | — |
| killswitch_max_daily_loss | $50 | $15 | 3% |
| killswitch_max_drawdown_pct | 20% | 10% | $50 |
| killswitch_max_positions | 10 | 5 | — |
| killswitch_max_loss_per_trade | $25 | $7.50 | 1.5% |

**Новая защита — проверка маржи в executor:**
- Добавлен `max_margin_per_trade_pct = 25%` — executor отклоняет сделку если маржа > 25% баланса.
- Логирование: при каждой сделке выводится risk в $ и %, margin в $ и %.
- Пример: BTC слишком дорог для одной позиции → executor откажет → бот перейдёт к ETH/SOL/альтам.

**Тесты:** 199 passed (38 bybit + 161 fx_pro_bot). Добавлены: test_executor_margin_check, test_executor_micro_account_sizing.

**Файлы:**
- `src/bybit_bot/config/settings.py` — новые defaults для $500
- `src/bybit_bot/trading/executor.py` — margin check + risk logging
- `src/bybit_bot/trading/killswitch.py` — defaults $15/$10%/$7.50
- `.env`, `.env.example` — обновлены параметры
- `tests/test_bybit_bot.py` — 2 новых теста

### Расширение до 39 монет — полный набор альткоинов

Было 8 активных монет (только majors). Добавлены 24 альткоина — все проверены на yfinance и доступны на Bybit USDT perp.

**Монеты по категориям (39 шт.):**

| Категория | Монеты |
|---|---|
| Majors (5) | BTC, ETH, SOL, XRP, BNB |
| Large-cap (10) | DOGE, ADA, LINK, AVAX, LTC, DOT, MATIC, NEAR, APT, ARB |
| Mid-cap DeFi/L1 (14) | SUI, UNI, AAVE, ATOM, TRX, FIL, INJ, FET, RENDER, TON, SEI, TIA, ONDO, PENDLE |
| Mid-cap infra (5) | WLD, OP, HBAR, RUNE, ALGO |
| Meme / micro-cap (5) | SHIB, PEPE, WIF, BONK, FLOKI |

**Почему это хорошо для $500 счёта:**
- Альткоины дешевле BTC/ETH → маржа меньше → больше позиций доступно.
- Мем-коины (PEPE, BONK, SHIB) — высокий объём, мизерная маржа, идеальны для скальпинга.
- Больше пар = больше сигналов = больше шансов найти setup.

**Stat-Arb: 10 пар** (было 4): добавлены AVAX/ETH, DOT/ETH, ATOM/ETH, NEAR/SOL, ARB/OP, PEPE/DOGE.

**yfinance маппинг:** SUI→SUI20947-USD, UNI→UNI7083-USD, PEPE→PEPE24478-USD, TON→TON11419-USD (специальные Yahoo ID).

**Тесты:** 199 passed.

**Файлы:**
- `src/bybit_bot/config/settings.py` — DEFAULT_SYMBOLS, DISPLAY_NAMES, TICK_SIZES, BYBIT_TO_YFINANCE
- `src/bybit_bot/trading/executor.py` — min_qty_map для всех 39 монет
- `src/bybit_bot/strategies/scalping/stat_arb_crypto.py` — 10 пар
- `.env`, `.env.example` — SCAN_SYMBOLS со всеми 39 монетами

### Деплой на VPS + включение демо-торговли

Первый деплой bybit-bot на VPS. Два контейнера работают параллельно:
- `fx-pro-bot-advisor-1` — форекс-бот (без изменений)
- `fx-pro-bot-bybit-bot-1` — крипто-бот (новый)

**Первый цикл сканирования:**
- 37 из 39 монет загрузились успешно
- MATIC delisted на yfinance (ребренд в POL, Yahoo не поддерживает) → убран
- APT тикер обновлён: APT-USD → APT21794-USD
- Первый сигнал: Stat-Arb DOT/ETH z=-2.07 (DOT недооценён vs ETH)

**Фиксы по результатам первого запуска:**
- Убран MATICUSDT (38 монет вместо 39)
- Исправлен тикер APTUSDT → APT21794-USD

**Включена демо-торговля:** `TRADING_ENABLED=true`.
Бот теперь открывает позиции на демо-счёте Bybit (виртуальные $100K).
Risk management ($500 профиль) активен — ограничит реальные потери при переходе на live.

**Файлы:**
- `src/bybit_bot/config/settings.py` — удалён MATIC, фикс APT тикера
- `src/bybit_bot/trading/executor.py` — удалён MATIC из min_qty_map
- `.env`, `.env.example` — 38 монет, TRADING_ENABLED=true

### Подключение исполнения скальпинг-сигналов

Скальпинг-стратегии генерировали сигналы, но не передавали их в executor —
только логировали. Добавлена функция `_process_scalping()` в main loop.

**Что делает:**
- После логирования сигналов проверяет KillSwitch и лимит скальп-позиций
- Для каждого скальп-сигнала (VWAP, Stat-Arb, Funding, Volume Spike):
  - Проверяет что символ ещё не открыт
  - Устанавливает leverage, рассчитывает qty/SL/TP через executor
  - Отправляет ордер на Bybit
  - Записывает позицию в SQLite с тегом стратегии (scalp_vwap и т.д.)
- Stat-Arb: открывает ОБЕ ноги (long A + short B)

**Файлы:**
- `src/bybit_bot/app/main.py` — `_process_scalping()`, вызов из `_run_cycle()`
