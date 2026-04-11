# Bybit Crypto Bot — Build Log

## 2026-04-11

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
