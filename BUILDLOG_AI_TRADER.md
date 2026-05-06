# BUILDLOG — AI-Trader (DeepSeek-V4)

## 2026-05-06 — fix(qty rounding): instruments-info + qtyStep/tickSize округление

`коммит при deploy`

**Симптом** (Telegram bybit_notif_bot, cycle 2 в 05:41:37 UTC):

```
OPEN | × not executed
error: open_failed: exception: Qty invalid (ErrCode: 10001)
Request → POST /v5/order/create
{"category":"linear","symbol":"XRPUSDT","side":"Buy",
 "orderType":"Market","qty":"341.0343","stopLoss":"1.3853",
 "takeProfit":"1.4586"}
```

**Причина.** В `executor.py:_apply_open` qty считалось как
`round(notional_usd / price, 4)` — жёстко 4 знака. Но Bybit V5
требует чтобы `qty` был кратен `lotSizeFilter.qtyStep`, который
зависит от инструмента:

| Symbol | qtyStep | Пример |
|---|---|---|
| BTCUSDT | 0.001 | OK на 4 знаках до floor |
| ETHUSDT | 0.01 | OK |
| XRPUSDT | **1.0** | 341.0343 → отказ Bybit |
| DOGEUSDT | **1.0** | аналогично |

То же для SL/TP — Bybit `priceFilter.tickSize` определяет шаг цены,
LLM выдавал значения с лишними знаками (например 1.38531 при
tickSize 0.0001).

**Решение.**

1. **`AiBybitClient.get_instrument_info(symbol)`**
   (`src/ai_trader/trading/client.py`) — получает `qtyStep`,
   `minOrderQty`, `maxOrderQty`, `tickSize` через
   `/v5/market/instruments-info` с in-memory кэшем (контракты не
   меняются часто).
2. **`InstrumentInfo` dataclass** — типизированная обёртка фильтров.
3. **`_floor_to_step` / `_round_to_step` хелперы**
   (`src/ai_trader/trading/executor.py`) — округление qty **вниз**
   под qtyStep (не превышать notional), цены SL/TP — к ближайшему
   tick. Корректное число десятичных знаков выводится из step.
4. **`_apply_open` использует instruments-info** — округляет qty,
   проверяет `min_order_qty` (с понятной ошибкой без вызова Bybit
   при заведомо отказе), capпит к `max_order_qty`.
5. **5 unit-тестов** (`tests/test_ai_trader.py`):
   - `_floor_to_step` для XRP integer и BTC milli
   - `_round_to_step` для tick price
   - регрессия XRPUSDT 341.0343 → 341 + place_order успешен
   - qty < min_order_qty → отказ без вызова place_order
   - 441/441 полный suite passed.

**Compliance.** Это infra-fix без изменения торговой логики
(`strategy-guard.mdc` exception). Параметры стратегии и LLM-промпта
НЕ менялись. Baseline n=0 (от 05.05) НЕ сдвигается.

**Файлы:** `src/ai_trader/trading/client.py`,
`src/ai_trader/trading/executor.py`, `tests/test_ai_trader.py`,
`BUILDLOG_AI_TRADER.md`

---

## 2026-05-06 — fix(LLM empty response): max_tokens 2000→4096 + no-thinking fallback

`коммит при deploy`

**Симптом.** В логах `fx-pro-bot-ai-trader-1`:

- 2026-05-06 04:34:06 cycle 69: `Parse error: no JSON object found in
  response: \`\`\`json {...` — обрезанный JSON, output_tokens=2000
  (упёрся в потолок).
- 2026-05-06 05:06:06 cycle 71: `LLM error: empty response after 2
  attempts` — два HTTP 200, но `text=""` (весь бюджет ушёл на
  thinking-блоки, на answer-блоки не осталось).

**Причина.** `max_tokens=2000` слишком мало для extended-thinking
mode. Когда DeepSeek генерирует длинный chain-of-thought —
thinking-блоки забирают весь бюджет, а text-блоков либо нет, либо
они обрезаются на середине JSON. После v0.3 «fine-grained task
decomposition» (см. запись 05.05) запросы стали тяжелее, проблема
проявилась.

**Решение.**
1. **Увеличен `AI_TRADER_DEEPSEEK_MAX_TOKENS`** дефолт с 2000 до
   **4096** (`src/ai_trader/config/settings.py`,
   `src/ai_trader/llm/client.py`, `docker-compose.yml`).
2. **Final fallback без thinking** (`src/ai_trader/llm/client.py`):
   если после `retry_on_empty` попыток `text` всё ещё пуст и нет
   `error` — клиент делает одну дополнительную попытку **без**
   `thinking={"type":"enabled"}`. Это reliable выход — без
   thinking-tax всё output_tokens идёт в text.
3. Параметры самой модели (`thinking_enabled=true`,
   `retry_on_empty=1`, `retry_sleep=5s`) НЕ меняются — fallback
   срабатывает только в edge-case'е, обычная работа без изменений.

**Compliance.** Это fix без изменения торговой логики
(`strategy-guard.mdc` exception): инфра LLM-клиента, не правила
входа/выхода. Baseline n=0 (от 05.05) НЕ сдвигается.

**Файлы:** `src/ai_trader/llm/client.py`,
`src/ai_trader/config/settings.py`, `docker-compose.yml`,
`BUILDLOG_AI_TRADER.md`

---

## 2026-05-05 — v0.3: Crypto Strategies 2026 audit + research-driven changes (n=0 reset)

**Контекст.** Пользователь запросил полный аудит крипто-стратегий и
ИИ-агента на актуальность 2026 года. Результаты собраны в
[`AUDIT_2026.md`](AUDIT_2026.md) — без воды, понятным языком. Этот
файл содержит обоснование всех изменений + ссылки на 2024–2026 research.

Краткие findings, которые повлияли на ИИ-агент:

- Industry standard 2026 риск на сделку = **1–2%**, не 5%. Источники:
  KuCoin Risk Management 2026, Atlas Peak Research, Hyper-Quant.
  Position sizing определяет 70–80% long-term returns; 5% соответствует
  full Kelly с edge ~10% и опасен из-за drawdown-риска.
- LLM-trading research 2025 (FinDebate arXiv:2509.17395, TradingAgents
  arXiv:2412.20138, ATLAS NeurIPS 2025) показывает: **fine-grained task
  decomposition + chain-of-thought** даёт лучше risk-adjusted returns,
  чем coarse single-step instructions.
- Funding rate framework 2026 (Lambda Finance): **bands** `<0.05%` /
  `0.05–0.20%` / `>0.20%`. Раньше LLM видел голое число.
- Post-ETF (Jan-2024) BTC и альты частично декоррелировали — не
  считать blindly что движение BTC переносится 1:1 на альты.
- Macro (Fed/DXY) теперь больше 4-летнего цикла (Bybit Outlook 2026,
  Galaxy Research). Новостной фид должен это ловить.

**Также найдены P0 баги** (диагноз из БД на 187 decisions, 22 ошибки):

- 12× `place_order returned None` — Bybit отказывал в ордерах, executor
  не знал почему (логи теряли `retCode/retMsg`). Чинится логированием.
- 8× `parse_error: empty response` — DeepSeek изредка возвращает пусто.
  Чинится retry (1 попытка, sleep 5s).

**Изменения v0.3:**

*Промпт (`src/ai_trader/llm/prompts.py`):*
- CAPITAL RULES: `risk_per_trade` 5% → **2%** ($25 → **$10** на сделку),
  `daily loss limit` $125 → **$50**.
- Добавлен **MARKET CONTEXT 2026** блок: perp-доминирование, post-ETF
  decoupling, funding bands, macro > 4-year cycle.
- ANALYSIS APPROACH теперь **structured**: TREND → VOLATILITY →
  SENTIMENT → CONFIRMATIONS → R:R CHECK → DECISION (chain-of-thought
  через предписанный шаблон).
- Жёсткое требование **R:R >= 1.5** для любого open: иначе hold.
- Формат ответа изменён: **commentary + JSON** (раньше JSON only).
  Парсер обновлён до устойчивого извлечения последнего balanced
  JSON-блока.

*Конфиг (`src/ai_trader/config/settings.py`):*
- `max_daily_loss_usd`: 125 → 50
- `max_total_loss_usd`: 500 → 200
- Новое поле `risk_per_trade_pct = 0.02`

*Контекст (`src/ai_trader/trading/context.py`):*
- Funding rate теперь выводится с band-меткой
  `[NEUTRAL]` / `[mild lean: longs paying]` / `[STRONG: shorts paying, contrarian risk]`.
- Добавлена строка `MACRO: BTC vs alts (24h): BTC=+1.2% avg-alt=-0.8%
  → BTC outperforming alts (alt-weakness)` — эвристическая замена
  глобальному BTC dominance %.

*Новости (`src/ai_trader/news/rss.py`):*
- GENERIC_KEYWORDS расширены: `ibit`, `fbtc`, `etha`, `etf flow/inflows/outflows`,
  `powell`, `yellen`, `dxy`, `btc dominance`, `liquidation`, `deleveraging`,
  `open interest`, `funding rate`, и др. (raтcionale: 2026 ETF-флоу + macro
  как driver).

*P0 bug-fixes (применены вне рамок reset, т.к. это баги, не тюнинг):*
- `src/ai_trader/llm/client.py`: retry на пустой LLM-ответ (`retry_on_empty=1`,
  `retry_sleep_sec=5.0`). Теперь cycle не пропускается.
- `src/ai_trader/trading/client.py`: `place_order` теперь возвращает
  `{"ok": True/False, "error": <bybit retMsg>, ...}` вместо `None`.
- `src/ai_trader/trading/executor.py`: пробрасывает Bybit `retCode/retMsg`
  в БД (`decisions.error`). Будем видеть что именно отказывает (min order
  size / margin / leverage).
- `src/ai_trader/trading/executor.py`: `parse_action` устойчив к
  commentary перед JSON (ищет последний balanced `{...}`).

*Bybit-стратегии (P0 doc-fixes, не торговая логика):*
- `vwap_crypto.py`: docstring «ADX≤20» → «ADX≤25 (приведено к коду)».
- `crypto_overbought_fader.py`: docstring «13:00–21:00 UTC» →
  «13:00–20:59 UTC (range(13, 21))».
- `funding_scalp.py`, `volume_spike.py`: добавлен канонический блок
  `─── Research basis ───`.
- `.cursor/rules/strategy-guard.mdc`: stat_arb Z 2.5 → **2.0** (приведено
  к коду + research GitHub abailey81 2025); добавлены записи про
  `FundingScalpStrategy` и `VolumeSpikeStrategy`.

**Сброс эксперимента n=0.** Это тюнинг + изменение контракта промпта
(commentary + JSON), а не bug-fix → реcет по правилу `no-data-fitting.mdc`.

**Reset = изменение условий, не уничтожение БД.** Volume `ai_trader_data`
сохранён, потому что на момент применения у ИИ была одна **открытая
позиция** (BTCUSDT Buy 0.005 @ 80249.8, SL=79356, TP=82000, R:R=1.96,
unrealized PnL ~+$3.5). Стирание БД оставило бы её на бирже без
управления (Bybit OCO сработал бы сам, но `_reconcile_closed_positions`
не записал бы PnL). Это финансовая дыра — недопустимо.

Маркер начала v0.3 пишется в `kv_state.v03_start_decision_id` и
`kv_state.v03_start_ts` — все последующие аналитические скрипты должны
фильтровать `decisions.id >= v03_start_decision_id` для сравнения
поведения старого/нового промпта.

Объём собранной до сброса статистики: 187 decisions / 33 часа /
2 открытых трейда (один закрылся по SL −$5.13, второй сейчас в плюсе).
Это малая выборка, статистически нерепрезентативна — потеря для
финансовых выводов минимальная (правило `sample-size.mdc`: ≥100
закрытых сделок и ≥2 недели для значимых выводов; у нас ни того ни
другого).

**ЗАМОРОЗКА**: v0.3 промпт и параметры заморожены на 14 дней (до 19.05.2026).
Никаких правок до конца forward-test (исключая bug-fix-категорию).

**Файлы:**
- `AUDIT_2026.md` (новый файл)
- `src/ai_trader/llm/prompts.py`
- `src/ai_trader/llm/client.py`
- `src/ai_trader/config/settings.py`
- `src/ai_trader/trading/context.py`
- `src/ai_trader/trading/client.py`
- `src/ai_trader/trading/executor.py`
- `src/ai_trader/news/rss.py`
- `src/bybit_bot/strategies/scalping/vwap_crypto.py`
- `src/bybit_bot/strategies/scalping/crypto_overbought_fader.py`
- `src/bybit_bot/strategies/scalping/funding_scalp.py`
- `src/bybit_bot/strategies/scalping/volume_spike.py`
- `.cursor/rules/strategy-guard.mdc`
- `docker-compose.yml`
- `BUILDLOG_AI_TRADER.md` — эта запись

---

## 2026-05-03 — v0.2.1: risk-per-trade 2% → 5% (n=0 reset)

**Контекст.** v0.2 запустился в LIVE и прошёл 1 цикл (HOLD). По запросу
пользователя поднимаем риск на сделку с 2% до 5% — более агрессивный
режим, ближе к "trader mindset" (на $500 капитала 2% = $10 = слишком
осторожно для discretionary трейдера).

**Связанные правки** (чтобы пропорция не сломалась):
- `risk_per_trade`: 2% → **5%** ($10 → **$25** макс убыток на сделку)
- `daily_loss_limit`: $50 → **$125** (паритет: 5 SL до блока)
- `total_loss_limit`: $200 → **$500** (= virtual capital, "доедание депо")
- `max_positions` 3, `max_leverage` 5x — без изменений.

Логика паритета: при 5%-риске × 3 макс позиции = $75 макс одновременный
риск. Daily $125 = ровно 5 полных SL подряд до killswitch — такой же
буфер как был при 2%/$50.

**Это тюнинг, не bug-fix → сброс эксперимента n=0** (правило
`no-data-fitting.mdc`). Потеря минимальная: до этого был 1 цикл с HOLD,
статистики не накопилось.

**Файлы:**
- `src/ai_trader/llm/prompts.py` — обновлён CAPITAL RULES
- `src/ai_trader/config/settings.py` — новые дефолты killswitch
- `docker-compose.yml` — новые env defaults
- `BUILDLOG_AI_TRADER.md` — эта запись

**ЗАМОРОЗКА**: при v0.2.1 промпт и параметры опять заморожены на 14 дней
(до 17.05). Никаких правок до конца forward-test'а.

---

## 2026-05-03 — v0.2: Wave 2 + Wave 3 + Wave 4 (полный сброс n=0)

**Контекст.** v0.1 (запущен этим же утром) был MVP: голый LLM на ценах +
funding rate, без новостей и без Telegram. Прошёл 1 успешный LIVE-цикл
(HOLD), но после ревью `BUILDLOG_AI_TRADER.md` пользователь напомнил
исходный запрос: «опытный криптотрейдер, следит за новостями, …, подключён
к telegram». v0.1 был слишком урезан. Расширяем до полного спека за один
заход и стартуем заново.

**Сброс эксперимента.** v0.1 → выбрасываем (n=1, статистически бесполезно
+ промпт изменён). Эксперимент v0.2 стартует с n=0. 14 дней forward-test
(до 17.05) — на этих условиях промпт и контекст ЗАМОРОЖЕНЫ
(`no-data-fitting.mdc`).

**Что добавилось в v0.2:**

### Wave 2 — Технические индикаторы

`src/ai_trader/analysis/indicators.py`. Канонические реализации без
внешних зависимостей:
- RSI(14) — Wilder's smoothing
- MACD(12/26/9) — EMA-based
- ATR(14) — Wilder + ATR%
- EMA20 / EMA50 — для определения тренда
- Bollinger Bands(20, 2σ) — для overbought/oversold

В контекст вкладывается **на двух TF**:
- **1H** × 100 свечей (краткосрочные сигналы)
- **4H** × 50 свечей (крупный тренд)

В `format_snapshot()` добавлены человекочитаемые метки:
`[OVERBOUGHT]`/`[OVERSOLD]` (RSI), `[bullish]`/`[bearish]` (MACD),
`[uptrend]`/`[downtrend]`/`[mixed]` (EMA), `[above/below upper/lower BB]`.

**Research basis:** Wilder (1978) RSI/ATR; Appel (2005) MACD; Bollinger
(2001) BB. Параметры — канонические, не подкручивались.

### Wave 3 — News feed

`src/ai_trader/news/rss.py`. RSS-агрегатор с фильтрацией:
- Источники по умолчанию: CoinDesk, CoinTelegraph, Decrypt (RSS, без auth)
- Кэш в памяти 10 минут (1-2 fetch на цикл, не нагружаем источники)
- Фильтр по ключевым словам:
  - `BTCUSDT` ← bitcoin/btc/satoshi
  - `ETHUSDT` ← ethereum/eth/vitalik
  - `BNBUSDT` ← binance coin/bnb
  - `XRPUSDT` ← xrp/ripple
  - `DOGEUSDT` ← dogecoin/doge
  - Generic crypto: ETF, SEC, Fed, FOMC, stablecoin
- Top-N (default 8) свежих за last 6h, дедуп по URL
- Если `feedparser` недоступен / RSS падает — блок news просто пустой,
  торговля продолжается без него (graceful degradation)

В system prompt добавлена инструкция: *«News sensitivity: major bullish
news on a coin during weakness = potential long setup; bearish news during
strength = potential short setup. Ignore headlines unrelated to your
symbols.»*

### Wave 4 — Telegram

`src/ai_trader/telegram/bot.py`. Минимальный клиент **на чистом requests**
(без `python-telegram-bot` SDK — меньше зависимостей, нет async-сложности).
Polling в отдельном daemon-thread, 30-сек long-poll.

**Команды:**
- `/start`, `/help` — приветствие + справка
- `/status` — режим, баланс, позиции, killswitch
- `/pnl` — daily/total PnL, WR, кол-во сделок
- `/last_decision` (alias `/last`) — последнее решение LLM с reasoning
- `/history [N]` — последние N решений (default 5, max 20)
- `/pause` — приостановить торговлю (флаг `paused` в kv_state)
- `/resume` — возобновить

**Push-уведомления:**
- 🟢 при открытии позиции (`apply.executed`)
- 🔴 при закрытии (по reconcile или /close)
- ⚠️ при срабатывании killswitch
- ❌ при ошибках (LLM API, парсинг, crash цикла)

**Auto-detect chat_id:** при первой команде от пользователя бот сохраняет
`chat_id` в `kv_state.telegram_chat_id` и далее шлёт push туда. Если
`TELEGRAM_CHAT_ID` задан в .env — используется он (фиксированный режим
для безопасности на проде).

**Graceful degradation:** если `TELEGRAM_BOT_TOKEN` пустой — модуль
просто не стартует. Никаких ошибок, основной цикл работает как обычно.

### Прочие изменения

- `state/db.py` — новая таблица `kv_state` (key→value), методы
  `is_paused()/set_paused()`, `get/set_telegram_chat_id()`,
  `get_recent_decisions()`, `get_closed_positions_count()`.
- `app/main.py` — интеграция всех модулей: pause-проверка перед LLM,
  передача `news_provider` в context-сборщик, `tg.notify_*` на ключевых
  событиях, push при `cycle crashed`.
- `pyproject.toml` — добавлен `feedparser>=6.0`.
- `docker-compose.yml` — добавлены env vars: `AI_TRADER_NEWS_*`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `AI_TRADER_TELEGRAM_ENABLED`.

### Тестовое покрытие

Всего по AI-trader:
- `test_ai_trader.py` — 17 тестов (parser, killswitch, store)
- `test_ai_trader_indicators.py` — 22 теста (RSI/MACD/ATR/EMA/BB +
  edge cases на коротких рядах, чистом тренде, постоянстве)
- `test_ai_trader_news.py` — 14 тестов (классификация, фильтр по
  символам, generic-relevance, dedup, кэш, fixture-RSS через mock
  `feedparser.parse`)
- `test_ai_trader_telegram.py` — 22 теста (split_message, KV-state,
  все команды на пустой и наполненной БД, mock TelegramBot)

Итого 75 unit-тестов на AI-trader. Полный проект: 425 / 425 ✓.

### План v0.2 наблюдения

1. **Cycle 1** — sanity-проверка: индикаторы посчитались (RSI/MACD не None
   на 5 символах × 2 TF = 10 snapshot'ов), новости пришли (хотя бы
   1 заголовок в кэше), Telegram молчит (token пуст — это норма).
2. **Day 1-3** — наблюдаем как часто LLM ссылается на индикаторы и новости
   в `reason` поле. Если игнорирует — значит system prompt недоучёл, можем
   усилить (но это reset n=0!).
3. **Day 14** (17.05) — финальный анализ: total PnL, WR, PF, сравнение с
   v0.1 baseline (если данных хватит) и HODL BTC за тот же период.

**Файлы (новые/изменённые):**
- `src/ai_trader/analysis/{__init__,indicators}.py`
- `src/ai_trader/news/{__init__,rss}.py`
- `src/ai_trader/telegram/{__init__,bot}.py`
- `src/ai_trader/state/db.py` (kv_state, helpers)
- `src/ai_trader/trading/context.py` (intregration)
- `src/ai_trader/llm/prompts.py` (расширен)
- `src/ai_trader/app/main.py` (TG + news + pause)
- `src/ai_trader/config/settings.py` (telegram + news vars)
- `tests/test_ai_trader_indicators.py`, `test_ai_trader_news.py`,
  `test_ai_trader_telegram.py`
- `pyproject.toml`, `docker-compose.yml`

---


Изолированный экспериментальный модуль. Не пересекается с `fx_pro_bot` и
`bybit_bot` (см. правило `strategy-guard.mdc` про разделение модулей).

Гипотеза: автономный LLM-агент (DeepSeek-V4 Flash) принимает торговые решения
на криптовалютных perpetual'ах Bybit, опираясь только на market context
(цены, funding, history). Цель — оценить, способен ли LLM в принципе
показать положительный edge на 14-дневном forward-test'е.

## 2026-05-03 — n=0, старт эксперимента

Создан скелет AI-трейдера, изолированного от существующих ботов.

**Архитектура** (`src/ai_trader/`):
- `app/main.py` — главный цикл, 15 минут на итерацию
- `llm/client.py` — DeepSeek-V4 через `anthropic` SDK
 (`base_url=https://api.deepseek.com/anthropic`, model=`deepseek-v4-flash`,
 thinking mode включён)
- `llm/prompts.py` — заморожен на 14 дней (никаких правок промпта в процессе
 эксперимента, см. `no-data-fitting.mdc`)
- `trading/client.py` — Bybit-клиент на `pybit` (БЕЗ импортов из `bybit_bot`)
- `trading/context.py` — сбор market context (1h свечи × 24, ticker, funding,
 24h range, открытые позиции из БД)
- `trading/executor.py` — парсер JSON-ответа LLM + исполнение
- `state/db.py` — отдельная SQLite (`ai_trader.sqlite`):
 `positions`, `decisions` (полный audit-trail промптов/ответов/токенов/cost),
 `daily_pnl` (для killswitch)
- `safety/killswitch.py` — глобальные стопы:
 - daily loss ≥ $50 → блок до завтра
 - total loss ≥ $200 → полная остановка
 - max 3 открытых позиций
 - max 5x leverage

**Изоляция от bybit_bot**:
- AI-трейдер торгует на `BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, DOGEUSDT`.
 `bybit_bot` торгует на `SOL/ADA/LINK/SUI/TON/WIF/TIA/DOT` — пересечений нет.
- Все ордера AI-трейдера маркируются `orderLinkId='ai_<uuid>'` —
 однозначное опознание в любых отчётах Bybit.
- Отдельная БД, отдельный Docker-сервис, отдельный volume.
- В `bybit_bot/app/main.py:_sync_positions_on_startup` добавлен
 фильтр по `scan_symbols`: при старте бот игнорирует позиции на
 чужих символах (не подбирает их в свою exit-логику). Логирует как
 `SYNC IGNORE: <side> <symbol> qty=… — символ вне scan_symbols`.

**Параметры эксперимента (заморожены на 14 дней)**:
- Виртуальный капитал: $500 (qty считается от него, не от реального demo-equity)
- Цикл: 15 минут (96 решений в сутки, ≈1344 за весь эксперимент)
- Free tier DeepSeek: 5M вход + 5M выход tokens. Грубая оценка
 ~3K input + ~500 output на цикл = 4M+0.7M tokens за 14 дней.
 Должно полностью уложиться в free tier.
- KillSwitch: $50/день, $200 total, 3 позиции, 5x leverage
- Mode: PAPER при первом запуске (`AI_TRADER_TRADING_ENABLED=false`).
 Решения принимаются и логируются в `decisions`, но ордера на биржу
 не отправляются. Включаем LIVE после проверки 1-2 циклов под наблюдением.

**Параметры, которые ЗАПРЕЩЕНО менять в процессе эксперимента**:
- system prompt
- список allowed symbols
- цикл 15 минут
- лимиты killswitch
- набор features в market context

Любая правка → перезапуск эксперимента с n=0.

**Допустимые правки без сброса n**:
- bug-fix в парсере (если LLM выдаёт валидный JSON, а мы его не принимаем)
- bug-fix в reconcile (если SL/TP закрылись на бирже, а в БД позиция висит)
- логирование, метрики (не влияют на торговые решения)

**План наблюдения**:
1. Час 1: запуск в PAPER mode. Проверяем — промпты валидные, ответы парсятся,
 нет ошибок API, нет ошибок lint в JSON.
2. День 1: переключаем в LIVE (`AI_TRADER_TRADING_ENABLED=true`). Наблюдаем
 первые 5-10 ордеров: правильные ли SL/TP, не выходят ли за пределы 5x leverage,
 правильно ли считается qty, killswitch не триггерится случайно.
3. День 14: разбор. Метрики из `decisions` + `positions`:
 - total PnL за 14 дней
 - Win Rate, PF, средний R:R
 - частота open/close/hold
 - top-3 убыточных решения (с rationale из LLM) — что не сработало
 - стоимость API в $ (из `daily_pnl.api_cost_usd`)
 - сравнение: «AI-трейдер vs HODL BTC» за тот же период

**Файлы**:
- `src/ai_trader/**` — новый модуль
- `tests/test_ai_trader.py` — 17 тестов (parse_action, KillSwitch, Store)
- `Dockerfile.ai-trader`
- `docker-compose.yml` — добавлен сервис `ai-trader`
- `pyproject.toml` — добавлен `anthropic>=0.39.0`, package `src/ai_trader`
- `src/bybit_bot/app/main.py` — `_sync_positions_on_startup` теперь
 фильтрует по `managed_symbols`
