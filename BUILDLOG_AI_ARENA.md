# BUILDLOG — AI Arena (Nof1 Alpha Arena clone)

Лог изменений бота `src/ai_arena/`. Отдельная экосистема от
`ai_trader`, `fx_ai_trader`, `bybit_bot`, `fx_pro_bot`.

**Архитектура зафиксирована** правилом
`.cursor/rules/ai-arena-sources.mdc`: любые правки в стратегии,
промптах, output schema, индикаторах и структуре цикла обязаны
ссылаться на один из двух источников:

- <https://nof1.ai/blog/TechPost1>
- <https://gist.github.com/wquguru/7d268099b8c04b7e5b6ad6fae922ae83>

См. также `AI_TRADER_PROPOSAL_ALPHA_ARENA.md` — детальный план перехода,
который реализован в этом боте.

---

## 2026-05-15 (вторая итерация)

### fix(ai-arena): 5 пропущенных отклонений в SYSTEM_PROMPT — финальный 1-в-1
`(коммит ниже)`

**Контекст:** После первой итерации фиксов (10 расхождений) запросили
программный аудит — выгрузили **живой** SYSTEM_PROMPT с VPS из последнего
decision и сделали построчный diff с canonical SYSTEM_PROMPT из gist'а
(с применёнными разрешёнными Bybit-адаптациями). Compliance-тесты
проверяли подстроки, а не буквальные литералы — пропустили 5 отклонений.

**5 расхождений найдены и исправлены:**

| # | Где | Source (gist L62-L194) | Было у нас | Fix |
|---|---|---|---|---|
| 1 | Asset Universe | `BTC, ETH, SOL, BNB, DOGE, XRP (perpetual contracts)` | `BTC, ETH, SOL, BNB, XRP, DOGE` (порядок DOGE↔XRP, нет хвоста) | Поменян `DEFAULT_ARENA_SYMBOLS` (DOGE перед XRP) + добавлен ` (perpetual contracts)` |
| 2 | Starting Capital format | `$10,000 USD` | `$1000 USD` | `virtual_capital_usd=10000.0`, `equity_scale_divisor=5.0` (50k Bybit demo / 5 = 10k = virtual_capital) |
| 3 | Decision Frequency | `Every 2-3 minutes` | `Every 3 minutes` (динамически из cycle_min) | Захардкожено `2-3` буквально (это описание характера mid-to-low frequency, не точная конфигурация) |
| 4 | **coin enum в JSON schema** | `"coin": "BTC" \| "ETH" \| "SOL" \| "BNB" \| "DOGE" \| "XRP"` | `"coin": "<one of BTC, ETH, ...>"` | Pipe-separated с кавычками для каждого значения 1-в-1 |
| 5 | Position Size + Sharpe formulas | без отступа (плоские строки) | с 4-пробельным отступом (как code-block) | Убран отступ — модель видит формулу как часть текста, не как отдельный синтаксический блок |

**ВАЖНО про #2 (Starting Capital):** изменение `virtual_capital_usd` с
$1000 на $10,000 требует одновременной правки `equity_scale_divisor`
с 50 на 5, чтобы Bybit demo $50k → scaled $10k = virtual_capital.
Иначе модель увидела бы противоречие «start $10k, current $1k» и решила
что просела на 90% (паника-режим). Теперь оба значения консистентны
и совпадают с Nof1 budget на Hyperliquid ($10k).

**Файлы:**
- `src/ai_arena/llm/prompts.py` — coin_enum через pipe-join, Starting
  Capital с разделителем тысяч, Asset Universe с `(perpetual contracts)`,
  убраны отступы у Position Size и Sharpe формул.
- `src/ai_arena/config/settings.py` — `virtual_capital_usd=10000.0`,
  `equity_scale_divisor=5.0`, порядок `DEFAULT_ARENA_SYMBOLS` (DOGE, XRP).
- `tests/test_ai_arena_source_compliance.py` — +4 новых compliance-теста:
  `test_starting_capital_format_exact` (проверяет `$10,000 USD` буквально),
  `test_asset_universe_format_exact` (проверяет хвост и порядок),
  `test_position_size_formula_no_indent` / `test_sharpe_formula_no_indent`
  (регресс-страховки на возврат отступа), усилен
  `test_coin_enum_exact_arena_format` (теперь проверяет буквальный
  pipe-formatted литерал, а не подстроки + регресс на `<one of `).

**Метод аудита:** `/tmp/verify_1to1.py` — извлекает canonical SYSTEM/USER
prompt из gist'а (между ` ```markdown ... ``` ` блоками), применяет
только разрешённые Bybit-адаптации (Hyperliquid→Bybit, MODEL_NAME,
funding schedule), делает unified-diff с live-prompt'ами с VPS. Будет
переиспользован при будущих audit'ах.

**Прогон:** 245 ai_arena тестов (+4 новых), 797 тестов весь suite — все проходят.

**Деплой:** селективный rebuild ai-arena (как обычно через SSH `--no-deps --build`).

---

## 2026-05-15

### feat(ai-arena): 1-в-1 alignment с Nof1 — net PnL, real entry/exit, cumulative metrics, Python repr (10 фиксов + 147 тестов)
`(коммит ниже)`

**Контекст:** Пользователь запросил статистику с Bybit API и обнаружил
расхождение PnL: бот показал +$74.71 (gross), Bybit — −$29.62 (net
после fees + funding). Заявил: «у нас должна быть вся логика 1 в 1,
почему это ни как у первоисточника?». Провели полный аудит логики
ai_arena против gist'а — нашли 4 critical, 2 medium, 4 low фикса.

**10 расхождений с source — все исправлены:**

| # | Категория | Severity | Где было | Стало |
|---|---|---|---|---|
| 1 | Gross vs net PnL | **CRITICAL** | `(exit-entry)*qty` локально | `closedPnl` из Bybit `/v5/position/closed-pnl` (после fees + funding) |
| 2 | Entry price slippage | **CRITICAL** | `ticker.last_price` ДО ордера | `avgPrice` из `get_positions` ПОСЛЕ market-fill |
| 3 | Exit price slippage | **CRITICAL** | `ticker.last_price` после close + fallback на `entry_price` (давал PnL=0) | `avgExitPrice` из `get_closed_pnl` |
| 4 | Total return baseline drift | **CRITICAL** | Rolling 14d window | Cumulative с первого equity_snapshot ever |
| 5 | Open positions block | **MEDIUM** | `json.dumps(...)` (double quotes, `null`) | Python literal repr (single quotes, `None`) |
| 6 | Sharpe rolling vs cumulative | **MEDIUM** | `rolling_sharpe_14d` + `cutoff_ts_14d_ago` | `cumulative_sharpe` на всех snapshot'ах |
| 7 | Coin naming в prompt | **LOW** | `BTCUSDT` в headers и JSON enum | `BTC` (gist L73, L168), маппинг через `arena_to_bybit` только на границе Bybit-API |
| 8 | Funding rate `+` модификатор | **LOW** | `Funding Rate: +0.0123%` | `Funding Rate: 0.0123%` (нейтральный, как source) |
| 9 | Total return `+` модификатор | **LOW** | `+1.50%` | `1.50%` (нейтральный) |
| 10 | Лишний артикль в SYSTEM_PROMPT | **LOW** | `>40% of capital in **a** single position` | `>40% of capital in single position` (gist L128) |

**Файлы (производственный код):**

| Файл | Изменения |
|---|---|
| `src/ai_arena/trading/symbols.py` | **NEW**: `arena_to_bybit("BTC")="BTCUSDT"`, `bybit_to_arena("BTCUSDT")="BTC"`. Маппинг между Nof1-форматом (голые тикеры) и Bybit V5 (USDT-суффикс). |
| `src/ai_arena/trading/client.py` | +`ClosedPnlRecord` dataclass, +`get_closed_pnl(symbol, start_time_ms, end_time_ms)` — endpoint `/v5/position/closed-pnl` (net PnL + avgExitPrice). |
| `src/ai_arena/trading/executor.py` | `_apply_close` → `_resolve_net_close` (берёт net PnL + avg_exit_price из Bybit, fallback на ticker + PnL=0 при API outage). `_apply_open` → `_resolve_real_open` (poll `get_positions` после ордера, реальный `avgPrice` + `size`). `parse_action` валидирует coin против `arena_symbols` (без USDT-суффикса). |
| `src/ai_arena/trading/context.py` | `format_symbol_block` использует `bybit_to_arena` для headers (`### ALL BTC DATA`). Funding rate без `+`. `format_open_positions_block` → Python repr через новые `_python_repr_list` / `_repr_value` / `_py_str_literal`. Symbol в open positions конвертируется в arena-формат. |
| `src/ai_arena/llm/prompts.py` | `symbols_csv` через `arena_symbols` (голые тикеры). `total_return_pct` без `+` модификатора. Удалён артикль `a` в diversification rule. |
| `src/ai_arena/state/db.py` | +`update_position_realized(position_id, exit_price, realized_pnl_usd)` — backfill API: перезаписывает PnL+exit, пересчитывает `daily_pnl` агрегат на дельту. +`get_all_equity_snapshots()` для cumulative Sharpe. +`get_first_equity_snapshot()` для cumulative total_return baseline. |
| `src/ai_arena/analysis/sharpe.py` | `rolling_sharpe_14d` → `cumulative_sharpe`. Удалён `cutoff_ts_14d_ago`. |
| `src/ai_arena/app/main.py` | `_reconcile_closed_positions` использует `_resolve_net_close`. Sharpe + total_return через cumulative API. |
| `src/ai_arena/telegram/bot.py` | `requests.Session` + retry с exponential backoff (1→30s) для `ConnectionResetError(104)`/`Timeout` — гасит spam stack-trace'ов от idle TCP RST на long-poll getUpdates. Не влияет на торговую логику. |

**Файлы (поддержка):**

| Файл | Назначение |
|---|---|
| `scripts/ai_arena_backfill_pnl.py` | **NEW**: разовый backfill для существующих закрытых позиций — заменяет gross PnL на net из Bybit `get_closed_pnl`. Поддерживает `--dry-run`. |

**Тесты — 147 новых, все валидируют 1-в-1 с source:**

| Файл | Тестов | Что проверяет |
|---|---|---|
| `tests/test_ai_arena_source_compliance.py` | **78** (NEW) | Построчное совпадение SYSTEM_PROMPT/USER_PROMPT/per-symbol block/open positions block с gist'ом. Каждый тест дословно цитирует source (с указанием line number) и assert'ит точную фразу. Регресс-страховки на удалённые отклонения (`(band:`, `(20×5min)`, `Average Volume (20)`, JSON dumps, USDT в headers, `+` для positive чисел, `Do NOT multiply by leverage` строка). |
| `tests/test_ai_arena_executor_logic.py` | **20** (NEW) | Бизнес-логика executor с фейковым `AiArenaBybitClient` (in-memory, запись всех вызовов): coin mapping (BTC→BTCUSDT для всех Bybit-вызовов), real entry price из `get_positions`, net PnL из `get_closed_pnl` (+ negative-net-when-positive-gross, short позиция, fallback при API outage), no-pyramiding, direction sanity (4 кейса), no-server-side-caps (parser принимает leverage=20/50, R:R 1:5). |
| `tests/test_ai_arena_baseline.py` | **12** (NEW) | `get_first_equity_snapshot`/`get_all_equity_snapshots` API, baseline = первый snapshot ever (не rolling), регресс-страховки на удалённые `cutoff_ts_14d_ago` и `rolling_sharpe_14d`, `update_position_realized` (delta, daily_pnl агрегат, n_wins flip, raises для open/unknown). |
| `tests/test_ai_arena_context.py` | **17** (NEW) | Per-symbol headers с arena-форматом, формат funding rate без `+`, open positions Python literal style. |
| `tests/test_ai_arena_symbols.py` | **6** (NEW) | `arena_to_bybit` / `bybit_to_arena` / `arena_symbols` — корректность и идемпотентность. |
| `tests/test_ai_arena_executor.py` | **5** обновлены | Coin mapping в parse_action: BTCUSDT теперь reject'ится. |
| `tests/test_ai_arena_prompts.py` | **3** обновлены | `total_return_pct` без `+`, diversification без `a`, нет USDT в JSON enum. |
| `tests/test_ai_arena_sharpe.py` | **5** переименованы | `rolling_sharpe_14d` → `cumulative_sharpe`. |

**Прогон:** 241 ai_arena теста (было 94, +147 новых), 793 теста весь
suite — все проходят.

**Источник правды для каждого фикса** — gist nof1-prompt.md. Конкретные
line numbers зафиксированы в комментариях каждого теста compliance-набора.

**Деплой:** селективный rebuild ai-arena (через SSH `--no-deps --build`),
не задевая `advisor` / `bybit-bot` / `ai-trader`. После rebuild —
запуск backfill-скрипта на VPS для замены gross PnL на net в
существующих 37 закрытых позициях.

---

## 2026-05-14

### chore: strict 1-в-1 ревью source vs код (9 расхождений в prompt'ах + dead code)
`(коммит ниже)`

**Контекст:** Пользователь зафиксировал требование «полный клон» Nof1 —
никаких отклонений от gist'а кроме физически вынужденных Bybit-адаптаций.
Полный построчный аудит SYSTEM_PROMPT / USER_PROMPT / open positions
block / индикаторов / источников данных vs gist line-by-line обнаружил
9 расхождений (8 в USER_PROMPT, 1 в SYSTEM_PROMPT) и 1 dead-code метод.

**Удалённые отклонения (исправлено в этом коммите):**

| # | Где | Source (gist) | Было у нас | Категория |
|---|---|---|---|---|
| 1 | per-symbol headers | `**Current Snapshot:**` | `Current Snapshot:` | косметика markdown |
| 2 | OI annotation | `Average: Y` | `Average (20×5min): Y` | лишняя аннотация |
| 3 | funding rate | `Funding Rate: 0.0123%` | `Funding Rate: 0.0123%  (band: mild)` | **наша интерпретация** |
| 4 | volume label | `Average Volume: Y` | `Average Volume (20): Y` | лишняя аннотация |
| 5 | RSI 3m label | `(7-Period)`, `(14-Period)` | `(7-period)`, `(14-period)` | капитализация |
| 6 | RSI 4h label | `(14-Period, 4h)` | `(14, 4h)` | сжатие |
| 7 | open positions | без поля `side`, signed quantity | `'side': 'long'/'short'` + positive qty | **наше добавление** |
| 8 | SYSTEM_PROMPT risk_usd | `Calculate as: \|Entry - SL\| × Position Size` | + строка `Do NOT multiply by leverage` | **наше усиление** |
| 9 | spacing per-symbol block | пустые строки между блоками индикаторов | сжатый layout без пустых строк | косметика |

**Дополнительно удалено:**
- ✂️ `funding_band_label()` функция в `analysis/indicators.py` (теперь
 unused, source не использует band-категоризацию).
- ✂️ Тесты `TestFundingBands` в `tests/test_ai_arena_indicators.py` (3 шт.).
- ✂️ `get_funding_rate_history()` метод в `trading/client.py` (dead code:
 source требует только current funding из ticker, история не нужна).
- ✂️ Соответствующие docstring и упоминания.

**Изменённые файлы:**

| Файл | Что |
|---|---|
| `src/ai_arena/trading/context.py` | переписан `format_symbol_block` 1-в-1 c gist; `format_open_positions_block` использует signed quantity |
| `src/ai_arena/llm/prompts.py` | удалена строка `Do NOT multiply by leverage` из SYSTEM_PROMPT § risk_usd |
| `src/ai_arena/analysis/indicators.py` | удалена `funding_band_label` |
| `src/ai_arena/trading/client.py` | удалён `get_funding_rate_history` + связанные docstring |
| `tests/test_ai_arena_indicators.py` | удалены 3 теста `TestFundingBands` |
| `.cursor/rules/ai-arena-sources.mdc` | добавлен раздел «Что НЕЛЬЗЯ добавлять в prompt'ы» с историческим списком отклонённых правок (защита от регресса) |

**Что осталось как обоснованная Bybit-адаптация (документировано в правиле):**

- `lastPrice` вместо mid-price (Bybit V5 ticker не отдаёт mid одним полем).
- Funding schedule 8h (Bybit) vs 1h (Hyperliquid) — одна строка в § Trading Mechanics.
- Asset universe с USDT-suffix (Bybit perp formal naming).
- `equity_scale_divisor=50` (Bybit demo $50k → LLM видит $1000).
- `set_leverage` per-position перед `place_order` (Bybit V5 требование).
- signed quantity конверсия для open positions (Bybit `size+side` → Hyperliquid `signed qty`).

**Верификация:**
- `python3 -m pytest tests/` → **624 passed** (-3 funding_band тестов = баланс).
- `ReadLints` чистый.
- Цикл бота на VPS подхватит после selective rebuild.

**Принципиальное правило (зафиксировано в `ai-arena-sources.mdc`):**
> Если адаптация **не вынуждена API биржи или sandbox-окружением** —
> её быть не должно. «Полезные подсказки», аннотации к числам,
> дополнительные поля, переформулировки текста source = ЗАПРЕЩЕНО.

**Файлы:** см. таблицу выше + `BUILDLOG_AI_ARENA.md`.

---

### feat: подключён Telegram-бот @winline_notify_bot (переиспользован от sport_bet)
`(только VPS .env, без коммита кода)`

**Контекст:** В прошлой записи (см. ниже) Telegram оставался выключенным —
не было креденшалов. Sport-бот (`/root/sport_bet/.env` на
`178.253.38.121`) больше не разрабатывается, контейнеры остановлены,
конфликта polling одного и того же токена не будет. Переиспользуем
готовый `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` под наш ai_arena.

**Действия (только VPS, кода не трогали):**
- На `178.253.38.121`: `grep ^TELEGRAM /root/sport_bet/.env` → токен +
 chat_id (group `-5232948719`).
- На `204.168.149.140`: `cp .env .env.bak.tg.<ts>`, прописали
 `AI_ARENA_TELEGRAM_BOT_TOKEN`, `AI_ARENA_TELEGRAM_CHAT_ID`,
 `AI_ARENA_TELEGRAM_ENABLED=true`.
- Селективный rebuild: `docker compose up -d --no-deps --build ai-arena`.
 Остальные 4 контейнера (advisor, ai-trader, fx-ai-trader, bybit-bot)
 не задеты (`--no-deps` + явный сервис).

**Верификация (cycle 1 после рестарта, 08:37:32 UTC):**
```
Telegram: ON
ai_arena.telegram.bot: telegram: подключён как @winline_notify_bot
LLM call: positions=1 real_equity=$50006.97 scaled=$1000.14 sharpe=0.326
```
- TG handshake прошёл (`getMe` принят).
- В тот же цикл подхватилась позиция BNBUSDT с предыдущего рестарта
 (LIVE state восстановлен из БД + reconcile с Bybit).
- Sharpe уже считается (≥3 equity-snapshots накопилось).

**Безопасность:** Токены ТОЛЬКО в `.env` на VPS (никогда не в git).
В случае утечки — пересоздать бота через @BotFather и обновить .env.

**Файлы:** только VPS `/root/fx-pro-bot/.env` (не в репозитории).

---

### feat: switch to LIVE on Bybit demo + add SOLUSDT (Asset Universe 1-в-1 с Nof1)
`(коммит ниже)`

**Контекст:** После refactor'а под Nof1 (см. запись ниже) бот стартовал
в `PAPER`. Пользователь указал, что счёт уже demo — место для бумаги
неоправданно, пора в LIVE. Параллельно вылез старый кост: я раньше
держал 5 пар (без SOLUSDT) с обоснованием «SOL занят bybit_bot». Это
противоречит правилу `.cursor/rules/ai-arena-sources.mdc` — Nof1 gist
**жёстко** прописывает 6 монет в Asset Universe и в JSON output schema:

> `Asset Universe: BTC, ETH, SOL, BNB, DOGE, XRP (perpetual contracts)`
> *gist line 62*

> `"coin": "BTC" | "ETH" | "SOL" | "BNB" | "DOGE" | "XRP"`
> *gist line 157 (output schema)*

Состав монет — часть архитектуры Nof1, а не наш произвольный выбор.
Мы должны быть 1-в-1, иначе SYSTEM_PROMPT (где LLM видит «6 монет
доступно») сам себе противоречит. Аргумент про «bybit_bot» неактуален:
`bybit_bot` живёт на mainnet, `ai_arena` — на отдельном demo-аккаунте,
конфликта по позициям нет.

**Изменения:**

| Файл | Что |
|---|---|
| `src/ai_arena/config/settings.py` | `DEFAULT_ARENA_SYMBOLS` → 6 пар (добавлен `SOLUSDT`); комментарий с цитатой gist |
| `docker-compose.yml` | `AI_ARENA_SYMBOLS` default → 6 пар |
| `.env.example` | пример `AI_ARENA_SYMBOLS` → 6 пар + комментарий про источник |
| `.cursor/rules/ai-arena-sources.mdc` | удалён пункт "5 пар вместо 6"; теперь "1-в-1 с Nof1, USDT-suffix — Bybit naming" |
| `src/ai_arena/llm/prompts.py` | docstring: убрано "6 → 5", оставлено только USDT-suffix как адаптация |
| `AI_TRADER_PROPOSAL_ALPHA_ARENA.md` | § 3.3 "По умолчанию остаётся" + иллюстрации SYSTEM_PROMPT — обновлены до 6 пар; § 5.1 помечен как «исторический draft» |
| `tests/test_ai_arena_executor.py` | `SYMBOLS` фикстура → 6 пар; `test_coin_not_in_whitelist` теперь использует `LTCUSDT` (не входящую в новый whitelist) |
| **VPS** `.env` | `AI_ARENA_TRADING_ENABLED=true` — переключение в LIVE на demo-аккаунте Bybit (`AI_ARENA_BYBIT_DEMO=true` оставлен) |

**LIVE-проверка (cycle 1, 08:25 UTC):**
DeepSeek V4-Pro отдал решение `buy_to_enter BNBUSDT qty=2.99 @ $670.10
SL=$668.5 TP=$673 lev=5x conf=0.50 R:R=1.81 risk=$4.78 notional=$2003.60`.
Парсер принял (никаких server-side reject — Nof1-философия), `place_order`
ушёл на Bybit demo. Latency LLM ~2 минуты (4036 output токенов на CoT
через required JSON-поля). На 3-мин цикл укладываемся, но с запасом ~1
минута — если латентность вырастет, придётся либо `AI_ARENA_POLL_INTERVAL_SEC=300`,
либо урезать `AI_ARENA_DEEPSEEK_MAX_TOKENS` (сейчас 8192).

**Верификация:**
- `python3 -m pytest tests/` → **627 passed** (включая 75 ai_arena).
- `docker compose ps` после селективного rebuild ai-arena: остальные
 4 контейнера (advisor, ai-trader, fx-ai-trader, bybit-bot) — Up,
 не задеты.
- VPS `.env`: `grep AI_ARENA_SYMBOLS` → not set, бот подхватит default
 из docker-compose.yml (6 пар).

**Файлы:** `src/ai_arena/config/settings.py`,
`src/ai_arena/llm/prompts.py`, `tests/test_ai_arena_executor.py`,
`docker-compose.yml`, `.env.example`,
`.cursor/rules/ai-arena-sources.mdc`,
`AI_TRADER_PROPOSAL_ALPHA_ARENA.md`, `BUILDLOG_AI_ARENA.md`.

---

### refactor: full alignment with Nof1 source (KillSwitch + hard-checks REMOVED)
`(коммит ниже)`

**Контекст:** При первом ревью пользователь зафиксировал нарушение
правила `.cursor/rules/ai-arena-sources.mdc`: я добавил server-side
`KillSwitch`, hard-cap'ы (`max_risk_per_trade=$10`, `max_open_positions=3`,
`max_leverage=5x`, `min_RR=1.5`, `max_daily_loss=$50`,
`max_total_loss=$200`) и переписал conviction → leverage mapping
(1-2x/2-3x/3-5x вместо source 1-3x/3-8x/8-20x). Source Nof1 (gist §
RISK MANAGEMENT PROTOCOL + nof1.ai/blog/TechPost1) НЕ имеет ни одной из
этих server-side hard-checks — risk management полностью на стороне LLM.

**Что удалено (отсебятина):**
- `src/ai_arena/safety/killswitch.py` — весь файл (включая папку `safety/`).
- `executor.py`: убраны hard-checks `max_risk_per_trade`, `max_open_positions`,
  `min_RR`, `max_leverage`, `notional_cap_base_usd`. Оставлены только
  sanity-парсинг (типы, диапазоны, signal ∈ allowed, coin ∈ whitelist),
  direction sanity (LONG/SHORT formal requirement из source) и Bybit-rounding
  (`qty_step`/`tick_size` — Bybit API требование, не Nof1).
- `settings.py`: убраны `max_daily_loss_usd`, `max_total_loss_usd`,
  `max_open_positions`, `max_risk_per_trade_usd`, `min_risk_reward_ratio`.
  `max_leverage` (5x) → `leverage_max` (default 20x как в source).
- `app/main.py`: убраны KillSwitch init, killswitch.check_can_trade(),
  killswitch.check_can_open_position(), pause-логика (store.is_paused()).
- `telegram/bot.py`: убраны `notify_killswitch`, `/pause`, `/resume`
  команды. Telegram теперь read-only мониторинг.
- `docker-compose.yml` + `.env.example` + VPS `.env`: убраны env vars
  `AI_ARENA_MAX_DAILY_LOSS`, `AI_ARENA_MAX_TOTAL_LOSS`,
  `AI_ARENA_MAX_POSITIONS`, `AI_ARENA_MAX_LEVERAGE`,
  `AI_ARENA_MAX_RISK_PER_TRADE`, `AI_ARENA_MIN_RR`. Добавлен
  `AI_ARENA_LEVERAGE_MAX=20`.

**Что переписано 1-в-1 по source:**
- `src/ai_arena/llm/prompts.py`: SYSTEM_PROMPT полностью переписан в
  12 секций gist'а (ROLE, ENVIRONMENT, ACTION SPACE, POSITION SIZING
  FRAMEWORK, RISK MANAGEMENT PROTOCOL, OUTPUT FORMAT, PERFORMANCE
  METRICS, DATA INTERPRETATION, OPERATIONAL CONSTRAINTS, TRADING
  PHILOSOPHY, CONTEXT WINDOW MANAGEMENT, FINAL INSTRUCTIONS).
- Conviction → leverage mapping: 0.3-0.5 → 1-3x, 0.5-0.7 → 3-8x,
  0.7-1.0 → 8-20x (gist § POSITION SIZING).
- Risk management в prompt'е: stop_loss «1-3% of account value per
  trade», profit_target «minimum 2:1 R:R», invalidation_condition
  «objective and observable» (gist § RISK MANAGEMENT PROTOCOL).
- Position sizing формула: `Position Size (USD) = Available Cash ×
  Leverage × Allocation %` (gist § POSITION SIZING FRAMEWORK).
- Common Pitfalls: Overtrading, Revenge Trading, Analysis Paralysis,
  Ignoring Correlation, Overleveraging (gist § TRADING PHILOSOPHY).
- Liquidation Risk «>15% away from entry», Diversification «<40% in
  single position», Fee Impact «<$500 erodes profits» — все
  включены как guidance в SYSTEM_PROMPT (gist § POSITION SIZING).

**Что оставлено как обоснованная Bybit-адаптация:**
- `equity_scale_divisor=50` (Bybit demo $50k / 50 → LLM видит $1000) —
  обсуждено и согласовано с пользователем. У Hyperliquid Nof1 даёт
  модели $10k бюджет, у нас аналогичная семантика через scaling.
- 5 пар без SOL (правило `strategy-guard.mdc` — изоляция от bybit_bot).
- Bybit V5 API технические правки (set_leverage per-symbol,
  qty_step/tick_size rounding, lastPrice вместо mid-price, funding 8h).
- SQLite БД (`state/db.py`) — нужна для rolling 14d Sharpe из source
  требований и для reconcile позиций между cycles.
- Telegram (`telegram/bot.py`) — read-only мониторинг (status / pnl /
  last_decision / history). На будущее, сейчас env пустой.

**Обновлено правило `.cursor/rules/ai-arena-sources.mdc`:**
- Секция «Что разрешено брать ИЗ ЭТИХ ИСТОЧНИКОВ» расширена ссылками
  на конкретные source-фразы.
- Секция «Что ЗАПРЕЩЕНО» дополнена прямым списком всех server-side
  hard-cap'ов, которые НЕЛЬЗЯ возвращать.
- Секция «Что МОЖНО менять» переименована в «вынужденные
  инфраструктурные адаптации» и сужена до 6 конкретных пунктов.

**Тесты:** 75/75 ai_arena тестов проходят. В `test_ai_arena_executor.py`
добавлены тесты `test_high_leverage_15x_accepted` и `test_low_rr_accepted`
— гарантия что parser больше НЕ блокирует high-leverage / low-R:R
(они теперь решение LLM). В `test_ai_arena_prompts.py` добавлены тесты
`TestSystemPromptSourceCompliance` (15+ checks что все source-параметры
буквально в prompt'е) и `TestSystemPromptNoOversteppingSource` (3
теста-щита что KillSwitch / max_positions / daily_loss НЕ просочились
обратно в prompt).

**Файлы:** удалена `src/ai_arena/safety/`; изменены
`src/ai_arena/config/settings.py`, `src/ai_arena/trading/executor.py`,
`src/ai_arena/llm/prompts.py`, `src/ai_arena/app/main.py`,
`src/ai_arena/telegram/bot.py`, `docker-compose.yml`, `.env.example`,
VPS `.env`, `.cursor/rules/ai-arena-sources.mdc`,
`tests/test_ai_arena_executor.py`, `tests/test_ai_arena_prompts.py`.

---

### fix: scaled equity для prompt + notional cap (compounding)
`(коммит ниже)`

**Проблема (наблюдение в Cycle 1 на VPS):**
LLM получил `Available Cash: $50000.00, Current Account Value: $50000.00`
от реального Bybit demo-баланса и посчитал `quantity=2.183 ETH` (notional
~$4946 при leverage=2). Наш executor cap-нул это: `notional $4946.05 >
cap $1000.00 (virtual_capital × leverage)` — защита сработала, но LLM
оперировал не теми числами, что предполагает sandbox.

Дополнительно нашёлся баг: `total_return_pct = (real_equity -
virtual_capital) / virtual_capital × 100` давал 9900% при $50k Bybit и
$500 virtual — мусор в prompt.

**Решение — масштабирование Bybit equity:**
Вводим `AI_ARENA_EQUITY_SCALE_DIVISOR` (default 50). Bybit demo
$50k / 50 → LLM видит $1000.

- `settings.py`: новое поле `equity_scale_divisor: float = 50.0`.
- `app/main.py`: `scaled_equity = real_equity / divisor`,
  `scaled_cash = available_cash / divisor` — передаются в `build_user_prompt`.
- `app/main.py`: `total_return_pct` пересчитан корректно — берётся
  baseline из самого раннего `equity_snapshot`, формула
  `(current - baseline) / baseline × 100` (инвариантна к scale).
- `executor.py`: `apply_action` принимает опциональный
  `notional_cap_base_usd`; если задан — `max_notional = base × leverage`
  (compounding по реальному equity), иначе fallback на
  `settings.virtual_capital_usd × leverage` (для unit-тестов).
- `app/main.py`: передаёт `notional_cap_base_usd=scaled_equity` при
  вызове `apply_action`.
- `AI_ARENA_VIRTUAL_CAPITAL` поднят с $500 до $1000 в env (`docker-compose.yml`,
  `.env.example`, VPS `.env`) — это номинальная метка для SYSTEM_PROMPT
  («Starting virtual capital: $1000»), синхронна со scaled-equity.

**Что НЕ затронуто (по принципу минимального вмешательства):**
- `max_risk_per_trade_usd=$10` — остаётся на инфраструктурных $-значениях.
- `max_daily_loss=$50`, `max_total_loss=$200`, `max_open_positions=3`,
  `max_leverage=5x`, `min_RR=1.5` — все killswitch-лимиты в долларах.
- Indicators (RSI/MACD/EMA/ATR), signal validation, Sharpe — без изменений.

**Соответствие источникам (правило `ai-arena-sources.mdc`):**
Nof1 у себя $10k виртуального капитала на каждый бота, не реальный
exchange-баланс — gist § «Account Status: Total Equity / Cash» подаётся
как virtual capital. Наш scaling — ровно эта семантика, адаптированная
под Bybit demo (фиксированные $50k стартового баланса).

**Тесты:** 61/61 ai_arena тестов проходят (executor работает с обоими
вариантами — с `notional_cap_base_usd` и без).

**Файлы:** `src/ai_arena/config/settings.py`, `src/ai_arena/app/main.py`,
`src/ai_arena/trading/executor.py`, `docker-compose.yml`, `.env.example`,
VPS `.env`.

---

### v0.1 — skeleton (NOT yet committed, NOT yet deployed)

Создан отдельный бот `src/ai_arena/`, изолированный от существующего
`ai_trader`. Реализована полная Nof1-style архитектура (single 3-min
cycle, output JSON schema из gist'а, набор индикаторов RSI(7/14)/MACD/
EMA20/50/ATR(3/14)/Volume avg/OI/Funding bands).

**Источники архитектуры (зафиксированы правилом):**
- nof1.ai/blog/TechPost1 — официальный tech-post Nof1 Alpha Arena.
- gist `nof1-prompt.md` by @wquguru — реверс System+User prompt'ов,
  формула `risk_usd = |entry - stop_loss| × quantity` (без leverage —
  правка автора по фидбэку @eatgrass / @xu4wang в комментариях).

**Что реализовано:**

- `.cursor/rules/ai-arena-sources.mdc` — source-of-truth правило с
  явным списком разрешённого/запрещённого. Активируется на правках в
  `src/ai_arena/**/*.py`, `Dockerfile.ai-arena`, `BUILDLOG_AI_ARENA.md`.
- `src/ai_arena/config/settings.py` — pydantic-settings, env-префикс
  `AI_ARENA_*`. Defaults: `poll=180s`, `virtual_cap=$500`, 5 пар
  (BTCUSDT/ETHUSDT/BNBUSDT/XRPUSDT/DOGEUSDT — без SOL: занят
  bybit_bot, правило strategy-guard.mdc), `max_lev=5x`, `risk=$10/trade`,
  daily=$50, total=$200, R:R≥1.5, model=`deepseek-v4-pro`,
  `reasoning_effort=off`.
  **DeepSeek-ключ ОТДЕЛЬНЫЙ** (`AI_ARENA_DEEPSEEK_API_KEY`, не общий
  `DEEPSEEK_API_KEY`): даёт независимый rate-limit pool на
  account-level + независимый billing/audit от ai_trader.
- `src/ai_arena/state/db.py` — `ai_arena.sqlite` со схемой:
  - `positions` с полями `confidence/invalidation_condition/risk_usd/
    profit_target/llm_justification` (Nof1 schema).
  - `decisions` с `signal/confidence/invalidation/risk_usd/
    sharpe_at_decision/minutes_elapsed`.
  - `equity_snapshots` (id, ts, total_equity_usd, available_cash,
    total_return_pct, sharpe_rolling_14d, cycle_no) — основа для
    rolling Sharpe.
  - `daily_pnl`, `kv_state` — как в ai_trader, для killswitch и UX.
- `src/ai_arena/trading/client.py` — Bybit V5 клиент (`pybit.unified_trading.HTTP`).
  В дополнение к стандартному набору: **`get_open_interest`** (V5 endpoint
  `/v5/market/open-interest`, intervalTime=5min, limit=20) и
  **`get_funding_rate_history`** (V5 endpoint `/v5/market/funding/history`,
  limit=8 ≈ 2.5 дня funding-снапшотов на Bybit 8h-schedule).
- `src/ai_arena/analysis/indicators.py` — RSI(7/14), MACD(12,26,9),
  EMA20/50, ATR(3/14), Volume avg(20). Канон Wilder (1978) / Appel
  (2005). `funding_band_label` возвращает `neutral`/`mild`/`strong`
  по порогам из gist'а (0.05% / 0.20%).
- `src/ai_arena/analysis/sharpe.py` — rolling 14d Sharpe из
  `equity_snapshots`. Формула канонична (Sharpe 1966), risk-free=0
  (Nof1 phrasing).
- `src/ai_arena/trading/context.py` — `collect_market_context` собирает
  per-symbol: ticker, 3m × 50, 4h × 60, OI 20×5min. `format_per_symbol_blocks`
  и `format_open_positions_block` форматируют под Nof1 layout (per-coin
  блоки с «### ALL BTC DATA», warning'и oldest→newest).
- `src/ai_arena/llm/prompts.py` — `build_system_prompt(settings)`
  (адаптация Приложения A из proposal'а под Bybit + наши лимиты),
  `build_user_prompt(...)` (Nof1 layout с 4 повторениями
  «OLDEST → NEWEST» + Sharpe feedback).
- `src/ai_arena/trading/executor.py` — `parse_action(text, allowed_symbols)`
  ищет последний balanced JSON, валидирует Nof1 schema. `apply_action`
  валидирует R:R ≥ 1.5, `risk_usd ≤ $10` (формула без leverage),
  notional ≤ virtual_capital × leverage, направление SL/TP, killswitch.
- `src/ai_arena/safety/killswitch.py` — daily/total/positions/leverage
  caps. Ровно те же лимиты, которые прописаны в SYSTEM_PROMPT —
  синхронизация обязательна (если меняешь env vars — правь и промпт).
- `src/ai_arena/llm/client.py` — `DeepSeekArenaClient` через
  Anthropic-compat. **БЕЗ thinking-блоков** (Nof1 НЕ использует
  reasoning-mode — gist цитата @wquguru: «No, they don't use reasoning
  mode. … Chain-of-thought is implemented through prompt engineering
  with required JSON-fields»). `reasoning_effort=off` → direct аналог
  V3.1 standard. Цены: $0.27/$1.10 за 1M токенов (input/output) —
  оценка на момент release V4-Pro 24.04.2026, может уточниться по
  api-docs.deepseek.com.
- `src/ai_arena/telegram/bot.py` — отдельный токен
  `AI_ARENA_TELEGRAM_BOT_TOKEN` (UX-изоляция от ai_trader).
  Команды `/start /help /status /pnl /last_decision /history /pause /resume`,
  push на open/close/killswitch/error.
- `src/ai_arena/app/main.py` — single 3-min cycle (Nof1 7-node loop):
  reconcile → killswitch → Sharpe → context → prompt → LLM → parse →
  apply → persist → equity_snapshot. Без dual-timer, без review-режима.
- `Dockerfile.ai-arena` + сервис `ai-arena` в `docker-compose.yml`.
  Volume `ai_arena_data` (`/data` внутри контейнера, отдельный от
  `ai_trader_data`). Restart `unless-stopped`, log-rotation 50MB×3.
- `pyproject.toml` — `src/ai_arena` в packages, `ai-arena` script
  entrypoint.
- `.env.example` — секция «AI ARENA» с полным списком переменных
  и комментариями.

**Что НЕ реализовано (по плану — Step 2 после 14d OOS):**
- `reasoning_effort=high|max` (Step 2, AI_TRADER_PROPOSAL_ALPHA_ARENA.md §15).
- Short-term memory (последние 5 closed decisions в prompt).
- Multi-agent (Analyst → Trader → Risk).
- Расширение coin universe.
- Unit tests (отдельной задачей).

**Деплой:**
- VPS 204.168.149.140, путь `/root/fx-pro-bot`.
- Контейнер `fx-pro-bot-ai-arena-1` после `docker compose up -d`.
- Селективный rebuild: `docker compose up -d --no-deps --build ai-arena`
  (см. правило `deploy-vps.mdc`).
- Перед первым запуском: положить `AI_ARENA_BYBIT_API_KEY/SECRET` в
  `.env` на VPS (отдельный subaccount от ai_trader / bybit_bot).
- Без ключей бот стартует, логирует ошибку и выходит (не крашит loop).

**Файлы:** новые `src/ai_arena/**`, `Dockerfile.ai-arena`,
`.cursor/rules/ai-arena-sources.mdc`, изменены `pyproject.toml`,
`docker-compose.yml`, `.env.example`.
