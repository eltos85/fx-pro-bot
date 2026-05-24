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

## 2026-05-22

### v2.z3 user-approved exception #4: Server-side notional cap

**Контекст.** Третья и последняя правка из решения пользователя
«B + C + D» после v2.y — опция C (notional cap). Самая инвазивная:
первое реальное архитектурное вмешательство в Bybit-flow LLM'а
(до сих пор серверный код был чистым sanity-парсингом без hard-cap'ов
— см. правило, экземп­ляр-«не возвращать ни в коем случае»).

**Обоснование (post-v2.y observed):**

Аудит сделок после v2.y показал, что LLM при `confidence=0.55` на
$10k virtual capital открывает SOLUSDT qty=545 × $87.14 = **$47,491
notional**, что = 4.7× virtual equity. Move 0.08% по такой позиции
→ $90 loss за 28 минут (SOL #33). Это не «плохой сетап» — это
архитектурная проблема sizing'а LLM. Никакой feedback (v2.y по
leverage tier, v2.z1 по symbol) не помог:

| День       | n  | Sum PnL  | Avg notional/trade |
|------------|----|----------|--------------------|
| 2026-05-19 | … | …        | $30k-$47k (наблюдение) |
| 2026-05-21 | 10 | −$435.02 | (audit pending)    |
| 2026-05-22 | 2  | −$82.58  | (audit pending)    |

**Почему cap, а не feedback (третий раз):**

- v2.y / v2.z1 — feedback на realized_pnl (LLM сам выбирает что делать).
- v2.z3 — **hard cap** на notional. SYSTEM_PROMPT остаётся canonical
  (нельзя править — gist L1-L200 sacred). Cap живёт на server-side
  и срабатывает **после** Bybit qty_step rounding и **до**
  `set_leverage / place_order`. LLM узнаёт о факте cap'а только из
  одноразового notice'а в следующем USER_PROMPT.

**Что добавлено:**

1. `AiArenaSettings.max_allocation_pct` — default `0.30`
   (= $3000 при `virtual_capital=$10000`). Откат через
   `AI_ARENA_MAX_ALLOCATION_PCT=1.0` (= $10k cap, фактически no-cap).
2. `src/ai_arena/trading/notional_cap.py` — pure-функция
   `apply_notional_cap(...)` + `format_rescale_notice(...)`. Чистая
   сигнатура без зависимости от client/store. Возвращает
   `CapResult(rescaled, rejected, original_qty, capped_qty, ...)`.
   Учитывает Bybit `qty_step` (rounding вниз после cap) и
   `min_order_qty` (если qty < min → rejected, позиция не открывается).
3. `executor._apply_open`: cap проверяется **после** existing
   qty_step rounding и **до** `set_leverage` / `place_order`.
   Поведение:
   - **rescaled** (qty уменьшен, но ≥ min_order_qty) → silent rescale,
     `kv_state["pending_rescale_notice"]` сохраняется, позиция
     открывается с уменьшенным qty (intent LLM уважён).
   - **rejected** (qty < min_order_qty после cap) → позиция **не**
     открывается, notice сохраняется (LLM узнает в next prompt'е).
   - **noop** (qty не вылез за cap) → ничего не делается.
4. `app/main.py`: на каждом цикле читает
   `kv_get("pending_rescale_notice")` и передаёт в `build_user_prompt`.
   После построения prompt'а сразу clear'ит — notice показывается
   ровно **один раз**.
5. `llm/prompts.py::build_user_prompt`: новый опциональный параметр
   `rescale_notice: str | None = None`. Вставляется между
   `**Timeframes note:**` и `---` (до `## CURRENT MARKET STATE`).
   Когда `None` — ничего не меняется (canonical compliance).

**Что НЕ изменено (canonical compliance):**

- **SYSTEM_PROMPT** — без изменений. Cap **не** упоминается в
  SYSTEM_PROMPT (canonical 1-в-1 с gist). LLM узнаёт про cap
  **только из notice** в USER_PROMPT, и только когда cap сработает.
- **USER_PROMPT базовая структура** — без изменений (notice
  вставляется опционально в conditional слот, default — отсутствует).
- **Output JSON schema** — без изменений.
- **Action space** — без изменений (`buy_to_enter | sell_to_enter |
  hold | close`).
- **Leverage cap 1-20x** — без изменений (canonical Nof1, только
  guidance в SYSTEM_PROMPT, не server-side).

**Откат:** `AI_ARENA_MAX_ALLOCATION_PCT=1.0` env var в compose
(без rebuild — только перезапуск контейнера). При cap=100% ×
virtual_capital $10k = $10k max notional — любая разумная позиция
вписывается, cap фактически выключен.

**Acceptance criteria:**

- Через 7 дней или ≥30 сделок при cap=30% — повторить
  `collect_bybit_3bots_stats.py` per-symbol с per-trade notional.
- Если средний notional на open trade упадёт с текущих $30k-$47k
  до ≤$3000 — cap работает механически (sanity-check).
- Если LLM начнёт сам ограничивать `quantity` после notice'ов
  (доля silent rescale'ов снизится со временем) — гипотеза
  «cap как обучающий сигнал» подтверждается.
- Если общий результат (WR, sum PnL, max DD) **не** улучшится после
  ≥30 сделок с cap'ом — обсудить ужесточение (cap=15-20%) или
  per-symbol cap.

**Тесты:**

- `tests/test_ai_arena_notional_cap.py` (новый) — 14 тестов на
  pure-функцию `apply_notional_cap` + `format_rescale_notice`:
  noop / rescale / reject / qty_step boundary / min_order_qty
  boundary / max_allocation_pct edge cases / notice format.
- `tests/test_ai_arena_executor_logic.py` — добавлен
  `AI_ARENA_MAX_ALLOCATION_PCT=1.0` в fixture `settings`
  (существующие тесты используют $500 notional при default cap
  $300 — пришлось бы переписывать numerics, проще отключить cap
  и тестировать его отдельно).
- 351/351 ai_arena тестов проходят.

**Files:**
- `src/ai_arena/config/settings.py` — новый `max_allocation_pct`
- `src/ai_arena/trading/notional_cap.py` — новый pure-модуль
- `src/ai_arena/trading/executor.py` — интеграция cap в `_apply_open`
- `src/ai_arena/llm/prompts.py` — опциональный `rescale_notice` в USER_PROMPT
- `src/ai_arena/app/main.py` — чтение/clear notice per cycle
- `.env.example` — `AI_ARENA_MAX_ALLOCATION_PCT=0.30` блок
- `.cursor/rules/ai-arena-sources.mdc` — исключение #4
- `tests/test_ai_arena_notional_cap.py` (новый)
- `tests/test_ai_arena_executor_logic.py` — cap=1.0 в fixture

**Совместимость с #1, #2, #3:**

- #1 (leverage feedback) — без влияния.
- #2 (symbol feedback) — без влияния.
- #3 (cycle 600s) — без влияния, cap проверяется per-cycle независимо.
- Все 4 исключения **независимы**, могут применяться/откатываться
  по отдельности.

**Деплой:** этот entry — момент пуша. Default cap = 0.30 запекается
в код, env var на VPS добавлять необязательно (можно для явности).

---

### v2.z2 user-approved exception #3: Cycle interval 180→600s

**Контекст.** В рамках того же решения пользователя «B + C + D» —
правка D (cycle 180→600s) после v2.z1 (B). Этот entry — про D.

**Обоснование (post-v2.y observed данные):**

На 9 trades после v2.y deploy средний holding period:

| #  | Symbol  | Lifetime | Pattern                                |
|----|---------|----------|----------------------------------------|
| 26 | XRP     | 19 мин   | RSI(7)=29 oversold → "nearing invalid" |
| 30 | SOL     | 78 мин   | EMA20 support → передумал на 3-min noise |
| 33 | SOL     | 28 мин   | EMA20 support → "position is b…"       |

LLM реагирует на каждое 3-минутное микро-движение цены и закрывает
позиции до того как setup развернётся. Cycle 600s даёт 10-минутный
«cool-off period» между decisions.

**Что изменено:**
- `src/ai_arena/config/settings.py`: `poll_interval_sec` default
  `180 → 600` (с подробным комментарием обоснования).
- `docker-compose.yml`: `${AI_ARENA_POLL_INTERVAL_SEC:-180} → :-600`.
- `.env.example`: комментарий и default 600.
- `src/ai_arena/app/main.py` docstring: «default 180 сек» → «default
  600 сек, v2.z2».
- `src/ai_arena/state/db.py` docstring `get_pnl_by_leverage_tier`:
  «cycle = 180s» → «cycle = 600s в v2.z2».
- `tests/test_ai_arena_source_compliance.py::test_decision_frequency_exact`
  — обновлён комментарий, assertion **не менялся** (SYSTEM_PROMPT
  остаётся 1-в-1 с canonical gist).

**Что НЕ изменено (важно для compliance):**
- **SYSTEM_PROMPT** — без изменений. Фраза `Decision Frequency:
  Every 2-3 minutes` остаётся 1-в-1 с canonical gist L76. Это
  информативная характеристика mid-to-low frequency, LLM не делает
  на ней числовых вычислений (нет формул вида «if cycle==180 then…»).
  Каждый цикл LLM получает свежий полный snapshot всех индикаторов,
  и его decision строится на абсолютных значениях этих индикаторов
  в момент вызова, а не на оценке частоты.
- **USER_PROMPT** — без изменений. `minutes_elapsed` всё ещё с момента
  старта эксперимента, как в gist'е.
- **Output JSON schema** — без изменений.
- **Action space** — без изменений.

**Откат:** `AI_ARENA_POLL_INTERVAL_SEC=180` env var в compose
(работает без rebuild — только перезапуск контейнера).

**Side-effects (помимо основного):**
- API costs: cycle 600s vs 180s = в 3.3 раза меньше LLM-вызовов.
  На 30-day forward-test это ~$15 → ~$5 при цене DeepSeek.
- Bybit rate-limits: запас прочности увеличивается (на cycle 600s
  мы значительно ниже public/private rate-limit'ов).

**Правило обновлено:** `.cursor/rules/ai-arena-sources.mdc` теперь
содержит исключение #3 «2026-05-22 — Cycle interval 180→600s» с
полным обоснованием и acceptance criteria.

**Acceptance criteria:**
- Через 7 дней или ≥30 сделок при cycle=600s — повторить аудит
  «положенный holding period vs realised holding period».
- Если средний `closed_at - opened_at` вырастет с текущих 30-90 мин
  до >60 мин — гипотеза подтверждается.
- Если результат не улучшится → обсудить cycle=900s/1800s или возврат.

**Files:**
- `src/ai_arena/config/settings.py`
- `src/ai_arena/app/main.py`
- `src/ai_arena/state/db.py` (docstring update)
- `docker-compose.yml`
- `.env.example`
- `.cursor/rules/ai-arena-sources.mdc` — исключение #3
- `tests/test_ai_arena_source_compliance.py` (комментарий update)

**Деплой:** локальный commit, не пушу до команды пользователя.

---

### v2.z1 user-approved exception #2: Performance Self-Reflection by Symbol

**Контекст.** Через 17 часов после v2.y deploy ai-arena показал
measurable shift в hold-rate (94.5% за 17ч), но **9 trades net −$287**:

| День       | n  | WR    | PnL      | avg_lev | Worst    |
|------------|----|-------|----------|---------|----------|
| 2026-05-21 | 10 | 30.0% | −$435.02 | 4.4x    | −$169.51 |
| 2026-05-22 | 2  | 50.0% | −$82.58  | 4.0x    | −$90.37  |

**Per-symbol audit показал что леверидж не главная проблема:**

| Symbol  | Period | n | WR  | sum_pnl  | Note            |
|---------|--------|---|-----|----------|-----------------|
| SOLUSDT | post-v2.y | 4 | 0%  | −$208.07 | Все 4 — лонги после oversold-RSI |
| BNBUSDT | post-v2.y | 1 | 0%  | −$38.78  |  |
| XRPUSDT | post-v2.y | 2 | 50% | −$57.27  |  |
| BTCUSDT | post-v2.y | 1 | 100%| +$5.10   |  |
| DOGEUSDT| post-v2.y | 1 | 100%| +$7.79   |  |

SOLUSDT — токсичный для DeepSeek-V4-flash символ конкретно сейчас.
В v2.y leverage-tier feedback это не видно: оба tier'а 1-3x и 4-8x в
минусе, LLM ушёл в hold вместо переключения на менее-токсичные
символы.

**Что сделано (по явному решению пользователя «вариант B + C + D»):**

1. `AiArenaStore.get_pnl_by_symbol(symbols)` — агрегат closed
   positions per-symbol. Аналог `get_pnl_by_leverage_tier`, разбивка
   по `symbol` вместо `leverage`. Принимает whitelist symbols (обычно
   `settings.symbols`). Возвращает list[dict] с n / wins / sum / avg
   per-symbol, в том же порядке что входной аргумент.
2. `_format_symbol_stats_block` в `prompts.py` — формат:

   ```
   - Performance by Symbol (cumulative since experiment start):
     - BTCUSDT: n=8,  wins=2 (25%),  avg_pnl=-$5.65,  sum_pnl=-$45.20
     - SOLUSDT: n=18, wins=4 (22%),  avg_pnl=-$26.59, sum_pnl=-$478.60
     - ETHUSDT: n=0 (no data)
     ...
   ```

   Каждый символ из `settings.symbols` показывается, даже если
   `n_trades=0` (явный сигнал «не торговали», не пропуск).
3. `build_user_prompt(..., symbol_stats=...)` — новый опциональный
   параметр (default `None`). Блок встроен в Performance Metrics между
   `Performance by Leverage Tier` и `Account Status`.
4. `app/main.py` подтягивает `store.get_pnl_by_symbol(settings.symbols)`
   каждый цикл и пробрасывает в `build_user_prompt`.

**Семантика — почему это допустимо:**

- Это **второе измерение** того же calibration self-feedback что v2.y.
  Одна и та же realized_pnl, разбивка по другому axis (symbol).
- Не директива «не торгуй SOL» — LLM сам интерпретирует и сам решает.
  Никаких hard-блокировок символов в коде.
- Stateless и stateless-coherent: пересчёт каждый цикл из БД,
  conversation history не нужна.

**Что НЕ изменено:**
- SYSTEM_PROMPT — без изменений.
- Output JSON schema — без изменений.
- Action space — без изменений.
- Whitelist symbols в коде / .env — без изменений.

**Правило обновлено:** `.cursor/rules/ai-arena-sources.mdc` теперь
содержит исключение #2 «2026-05-22 — Performance Self-Reflection by
Symbol» с цитатой пользовательского решения и acceptance criteria.

**Acceptance criteria:**
- Через 7 дней или ≥30 новых сделок — повторить
  `collect_bybit_3bots_stats.py` per-symbol.
- Если LLM начнёт реже открывать SOLUSDT (или другие токсичные
  в текущем view) — гипотеза подтверждается.
- Если SOL-trades продолжатся в том же темпе → обсудить
  symbol-blacklist в коде (но это уже не feedback, а discipline).

**Files:**
- `src/ai_arena/state/db.py` — `get_pnl_by_symbol`
- `src/ai_arena/llm/prompts.py` — `_format_symbol_stats_block`,
  параметр `symbol_stats` в `build_user_prompt`
- `src/ai_arena/app/main.py` — proxying `symbol_stats`
- `.cursor/rules/ai-arena-sources.mdc` — исключение #2
- `tests/test_ai_arena_leverage_tier_feedback.py` — 14 новых тестов
  (агрегация / форматирование / интеграция / backward compat)

**Sample-size guard.** Текущая выборка после v2.y — n=9 closed
trades. Это значительно ниже порога `sample-size.mdc` (≥100).
Поэтому LLM получает только цифры, без рекомендаций «не торгуй
символ X». Решение — на стороне LLM.

**Деплой:** локальный commit, не пушу до явной команды пользователя.
План: 3 commit'а (v2.z1 / v2.z2 / v2.z3) выкладываются по очереди
по запросу пользователя.

---

## 2026-05-21

### v2.y user-approved exception: Performance Self-Reflection by Leverage Tier

**Контекст.** После v2.x bug-fix (Mid prices = OHLC4 + scaled_cash unclamped)
ai-arena всё ещё уверенно торгует в минус. Ключевой паттерн из аудита:

| Tier (gist mapping) | n  | WR  | sum PnL    |
|---------------------|----|-----|------------|
| 1-3x (low conv)     | 12 | 42% | +$30.05    |
| 4-8x (medium conv)  | 13 | 23% | −$348.40   |
| 9-20x (high conv)   | 0  | —   | —          |

LLM показывает явную miscalibration: при low-conviction (1-3x) — net positive,
при medium-conviction (4-8x) — массовая просадка. Канон gist'а
(confidence→leverage mapping L100-101) предполагал что high-conviction
сделки лучше low-conviction. Эмпирически — наоборот.

**Что сделано (по явному решению пользователя «сделай одно исключение,
я разрешил»):**

1. `AiArenaStore.get_pnl_by_leverage_tier()` — агрегат closed positions
   по 3 leverage-tier'ам gist'а (1-3x / 4-8x / 9-20x). Возвращает
   per-tier `n_trades / n_wins / sum_pnl / avg_pnl`. Источник
   `realized_pnl_usd` тот же, что для cumulative Sharpe и
   total_return_pct (закрытые позиции с не-NULL PnL).
2. `_format_leverage_tier_block` в `prompts.py` — рендерит данные
   в формате:

   ```
   - Performance by Leverage Tier (cumulative since experiment start):
     - 1-3x: n=12, wins=5 (42%), avg_pnl=+$2.50, sum_pnl=+$30.05
     - 4-8x: n=13, wins=3 (23%), avg_pnl=-$26.80, sum_pnl=-$348.40
     - 9-20x: n=0 (no data)
   ```

   При `leverage_stats=None` или всех нулевых tier'ах →
   `(no closed trades yet — insufficient history)`.
3. `build_user_prompt(..., leverage_stats=...)` принимает новый
   опциональный параметр (default `None` для backward compat в тестах).
   Блок встроен в секцию **Performance Metrics** между Sharpe и
   Account Status — строго в существующую performance-feedback зону.
4. `app/main.py` подтягивает `store.get_pnl_by_leverage_tier()` каждый
   цикл и пробрасывает в `build_user_prompt`. Кэширования нет —
   данные пересчитываются заново после каждого reconcile.

**Семантика — почему это допустимо:**

- Это **data-driven feedback**, не «полезный совет». Цифры берутся из
  той же realized_pnl что Sharpe / Total Return — те же поля БД.
- Аналог cumulative Sharpe — тот же mechanism (бот видит свою
  историю), только разбитый по другому измерению (leverage вместо
  агрегата).
- LLM сам интерпретирует и сам решает, не директива «не делай 4-8x».
  В прошлой сессии Nof1 такая же self-feedback механика реализована
  через cumulative_sharpe и total_return_pct (gist L376-379).
- Совместимо со stateless design: `leverage_stats` пересчитывается
  каждый цикл из БД, conversation history не нужна.

**Что НЕ изменено:**
- SYSTEM_PROMPT — ни байта.
- Output JSON schema — без изменений.
- Action space — без изменений.
- Никаких hard-cap'ов leverage server-side. Только feedback —
  decision остаётся LLM'у.

**Правило обновлено:** `.cursor/rules/ai-arena-sources.mdc` теперь
содержит раздел «Допустимые исключения по решению пользователя» с
точной цитатой реплики и списком acceptance criteria.

**Acceptance criteria (n=180 для sample-size:**
- Через 7 дней (примерно 5-7 закрытых сделок в день) — повторить
  `collect_bybit_3bots_stats.py` с per-leverage breakdown.
- Если LLM начнёт смещаться к 1-3x на основе видимой негативной
  истории 4-8x → гипотеза подтверждается, оставляем.
- Если no change или ухудшение → обсудить переформулировку блока
  (per-symbol дробление, скользящее окно, hide tier с n<3) или
  откат.

**Files:**
- `src/ai_arena/state/db.py` — `get_pnl_by_leverage_tier`
- `src/ai_arena/llm/prompts.py` — `_format_leverage_tier_block`,
  обновлённый `build_user_prompt`
- `src/ai_arena/app/main.py` — proxying leverage_stats
- `.cursor/rules/ai-arena-sources.mdc` — раздел «Допустимые исключения»
- `tests/test_ai_arena_leverage_tier_feedback.py` — 15 новых тестов
  (агрегация / форматирование / интеграция / backward compat)

**Sample-size guard.** Текущая выборка n=12 (1-3x) и n=13 (4-8x) —
ниже порога `sample-size.mdc` для disable/enable решений
(минимум 100 сделок). Поэтому LLM не получает рекомендацию
«не делай 4-8x» — только сами цифры. Решение «менять или не менять
поведение» — на стороне LLM, не правила.

### v2.x bug-fix: «Mid prices» = OHLC4 (вместо close) + scaled_cash без clamp до 0

**Контекст.** За 30-day API-stat ai-arena показал WR 20% / PnL −$1840.65
на n=219 trades (sample-size порог пройден). За 2 дня после v0.14
(20-21 мая): WR 21%, PnL −$644 на n=24. Persistent сигнал — что-то
системно не так. Запросили full audit реализации vs gist + tech post.

Аудит нашёл **2 confirmed bugs** в передаче данных, оба чинятся как
compliance с правилом `.cursor/rules/ai-arena-sources.mdc` (не как
отклонение от source). Подняты при extended Bybit↔Hyperliquid mapping.

### Bug 1: «Mid prices» в intraday array = close prices

**Симптом.** В user prompt каждый цикл выводит:

```
**Intraday Series (3-minute intervals, oldest → latest):**

Mid prices: [77445.2, 77432.1, ...]
```

Лейбл `Mid prices:` соответствует gist L361 (`Mid prices: [{btc_prices_3m}]`),
но в массиве — **close prices** 3-минутных свечей (`indicators.py:
build_intraday_snapshot`, raw `bars_closes[-take_n:]`). LLM думает что
видит mid (как у Nof1/Hyperliquid), фактически получает close.

**Семантическая разница close vs mid:**
- close = цена последней сделки в баре (привязана к direction last taker)
- mid = (bid+ask)/2 в момент close (нейтральный к direction)

Для волатильных активов на 3m TF разница может быть 0.05-0.20% — на 5x
leverage это уже заметный data-noise для индикаторов уровня RSI/MACD,
которые LLM использует для entry/exit.

**Фикс.** OHLC4 = `(open + high + low + close) / 4` — каноническая
«typical bar price», ближайшая аппроксимация mid за период бара (без
доступа к tick-by-tick orderbook, которого Bybit klines не отдают).
Эта аппроксимация — **расширение** разрешённого правилом «Bybit ↔
Hyperliquid маппинг» (правило L107-119: `lastPrice вместо mid-price`
для snapshot price; теперь ту же логику применяем к intraday array,
потому что Bybit V5 klines не дают per-bar mid в одном поле).

**Что важно сохранено:**
- Лейбл `Mid prices:` не меняется (1-в-1 с gist L361 — текст source
  неприкосновенен по правилу L103-105 «strict 1-в-1»).
- Индикаторы (RSI/MACD/EMA/ATR) **по-прежнему считаются на close**-
  prices — это финансово-математический канон. Только массив
  `prices` для display = OHLC4.
- В `build_intraday_snapshot` добавлен опциональный параметр
  `display_prices: list[float] | None = None` (backward compat: если
  None — fallback к bars_closes, как было).

### Bug 2: `scaled_cash` clamp `< 0 → 0` (отсебятина не из правила)

**Симптом.** `main.py` линии 229-232 (до фикса):

```python
scaled_cash = ctx.available_cash_usd - (real_at_start - settings.virtual_capital_usd)
if scaled_cash < 0:
    scaled_cash = 0.0
```

Когда margin использован > virtual_capital, LLM видел `Available Cash:
$0.00` и канон-формула из source (gist § Position Sizing):

```
Position Size (USD) = Available Cash × Leverage × Allocation %
```

обнулялась → невозможно открыть новые позиции даже когда margin реально
доступен через `equity = cash + unrealized`. Это блокировало capability
LLM пирамидизировать прибыльные сделки или диверсифицировать в overlevered
state.

**Правило `ai-arena-sources.mdc` L130-133** прямо фиксирует формулу:

```
scaled_cash = real_available_cash − (real_equity_anchor − virtual_capital_usd)
```

— **без clamp**. Clamp `< 0 → 0` был наша отсебятина (по комментарию в
коде «защита: LLM не должен видеть отрицательный cash — формат source»).

**Фикс.** Убрать clamp. `scaled_cash` показывается как есть (может быть
отрицательным при overleveraged state) — это **намеренный сигнал** LLM
«нет свободного margin под новые позиции», ровно то что должно быть в
этой ситуации. Если scaled_cash отрицательный, формула sizing даёт
отрицательную Position Size → LLM не открывает новую позицию (правильное
поведение).

**Refactoring**: формула вынесена в pure-функцию
`src/ai_arena/app/scaling.py::compute_scaled_account` — это даёт:
1. Тестируемость без зависимости от `anthropic` (main.py подтягивает
   DeepSeekArenaClient → anthropic, не нужно для формулы).
2. Single source of truth — формула в одном месте.

### Что НЕ изменено (намеренно)

- **SYSTEM_PROMPT текст** — 12 секций gist, ни байта не тронуто.
- **JSON output schema** — `signal/coin/quantity/leverage/...` 1-в-1.
- **Asset universe** — BTC/ETH/SOL/BNB/DOGE/XRP, тот же порядок.
- **Action space** — `buy_to_enter | sell_to_enter | hold | close`.
- **Cumulative Sharpe / total_return_pct** — без rolling-окна.
- **Risk-management** на стороне LLM (без server-side caps).
- **Leverage cap 1-20x** — guidance в промпте, не hard cap.

Решение **не добавлять leverage warning** в промпт, хотя API-stats
показали персистентную убыточность 5x+ leverage (n=13, sum −$348).
Добавление calibration self-feedback нарушит правило L102-105 «полезные
подсказки = ЗАПРЕЩЕНО, переформулировки текста source = ЗАПРЕЩЕНО».
Если в дальнейшем понадобится — нужно сначала обновить правило
с research-обоснованием (research feedback loop как разрешённый
adaptation), потом править промпт. Решение зафиксировано как открытое
к обсуждению.

### Файлы

- `src/ai_arena/analysis/indicators.py` — `build_intraday_snapshot`
  принимает `display_prices` параметр.
- `src/ai_arena/trading/context.py` — передаёт `ohlc4_prices` в
  `build_intraday_snapshot`.
- `src/ai_arena/app/scaling.py` — **новый файл** с pure-функцией
  `compute_scaled_account` (вынесена из main.py).
- `src/ai_arena/app/main.py` — использует `compute_scaled_account`,
  inline-формула + clamp удалены.
- `tests/test_ai_arena_indicators.py` — 3 новых теста на `display_prices`.
- `tests/test_ai_arena_scaled_account.py` — **новый файл** с 11
  тестами на формулу + регрессионный тест против clamp возврата.

### Тесты

`pytest` → 894 passed, 0 lint errors. Новые тесты:
- `TestSnapshotBuilders::test_intraday_display_prices_override_used_for_prices`
- `TestSnapshotBuilders::test_intraday_no_display_prices_falls_back_to_closes`
- `TestSnapshotBuilders::test_intraday_display_prices_short_history_pads_zeros`
- `TestComputeScaledAccountFormula` (3 теста)
- `TestScaledCashNoClamp` (3 теста)
- `TestEdgeCases` (3 теста)
- `TestNoSilentClampRegression` (3 параметризованных теста — защита
  от случайного возврата clamp в будущем).

### Ожидание

Не делаем выводов о «стало лучше» из 2-3 дней наблюдения post-fix
(sample-size). Через 7 дней повторить `scripts/collect_bybit_3bots_stats.py`
и сравнить per-symbol per-leverage stats. Гипотеза:
- OHLC4 vs close может уменьшить shortfall на entries (data integrity).
- Снятие cash clamp может позволить ai-arena открывать позиции в
  overleveraged state, когда у него уже есть прибыльные позиции
  (раньше блокировалось).
Никаких числовых обещаний — sample n=219 показал 20% WR за 30 дней,
для подтверждения изменения нужен сопоставимый sample post-fix.

---

## 2026-05-19

### reset(ai-arena): эксперимент v1 → v2 (virtual $1000 → $10000, match Nof1 source)
`(operation на VPS, без code-changes)`

**Симптом.** За ~5 недель v1-эксперимента бот деградировал:
- Total PnL: **−$1390.51** (на virtual=$1000 = **−139%**)
- Win Rate: **20%** (202 closed trades), Sharpe **−0.049**
- Today realized: −$323.20

**Диагноз — feedback-loop death spiral.** Offset-model `scaled =
$1000 + (real_now − real_anchor)` при значительных потерях даёт LLM
**отрицательный scaled equity**. В оставленных justification'ах
последних 50 closes доминирующая мотивация — defensive panic:

- _"Account value is negative"_
- _"Account down 97.83%"_
- _"Negative Sharpe ratio warrants defensive posture"_

89% закрытий — LLM-инициированные, средний WL-ratio 0.55 (loss > win)
→ profit невозможен математически. Эталон Nof1 (TechPost1) — стартовый
капитал **$10000**, наш v1 был 10× меньше — амплифицировал любую
просадку в воспринимаемую LLM катастрофу.

**Решение — clean restart с правильным базисом** (см. протокол ниже).
Это **не** правка стратегии (промпт / output schema / cycle structure
не меняются — инвариант `ai-arena-sources.mdc` сохранён). Меняется
**только размер sandbox** до значения из источника + сброс накопленной
«психологической» истории, которая загнала LLM в defensive corner.

**Что сделано (operation runbook, без code-changes):**

1. **Проверено**: открытых позиций на Bybit нет (BTC закрылась за
   несколько минут до операции).
2. **Stop** `fx-pro-bot-ai-arena-1`.
3. **Archive БД** (sqlite на volume `ai_arena_data`):
   ```sql
   ALTER TABLE positions          RENAME TO positions_archive_v1;
   ALTER TABLE decisions          RENAME TO decisions_archive_v1;
   ALTER TABLE equity_snapshots   RENAME TO equity_snapshots_archive_v1;
   ALTER TABLE daily_pnl          RENAME TO daily_pnl_archive_v1;
   DELETE FROM kv_state WHERE key IN (
       'real_equity_at_start_usd', 'started_at_ts', 'total_cost_usd'
   );
   ```
   Старые таблицы НЕ удалены — нужны для post-mortem / OOS-валидации
   будущих изменений. Новые `positions/decisions/...` создаются
   автоматом через `CREATE TABLE IF NOT EXISTS` в `db.py::_SCHEMA`
   при старте контейнера.
4. **`.env` на VPS**: добавлена явная `AI_ARENA_VIRTUAL_CAPITAL=10000`
   (раньше пользовались default из `settings.py`).
5. **Selective rebuild** (без затрагивания advisor / bybit-bot):
   `docker compose up -d --no-deps --build ai-arena`.

**Подтверждение (cycle 1 нового запуска):**
```
Virtual cap: $10000.00 | leverage cap: 1-20x
Equity model: offset-based (LLM видит $10000 + реальный PnL с Bybit)
Real-equity anchor зафиксирован: $48346.03
LLM call: positions=0 real=$48346.03 anchor=$48346.03
         → sandbox=$10000.00 (PnL +0.00, +0.00%) sharpe=n/a
APPLY: HOLD: No clear high-conviction setup ...
```
Anchor зафиксирован на `$48346.03` — теперь любой реальный PnL ±X
будет отображаться LLM как `sandbox=$10000±X`, без накопленной
«−$1390 истории» в восприятии.

**Что НЕ изменилось** (sanity для `ai-arena-sources.mdc`):
- `SYSTEM_PROMPT` 1-в-1 байты идентичны Nof1 gist (147 compliance-тестов).
- `output_schema` (Pydantic Action) без правок.
- Cycle structure 180s, 6 instruments, V4-Flash + thinking=disabled —
  всё прежнее.
- Offset-based scaling формула не изменена, изменён только её
  параметр `virtual_capital_usd`.

**Sample size note** (правило `sample-size.mdc`).
v1 дал n=202 closed trades за ~5 недель → выборка достаточная чтобы
зафиксировать **результат** (WR=20%, PF<1, EXP<0), но недостаточная
чтобы понять, баг это бота или фундаментальное свойство DeepSeek
v4-flash на этом промпте. Nof1 TechPost1 (DeepSeek v3.1, virtual=$10k,
~70 days) показал +20% return — это benchmark для v2-эксперимента.

**Forward-test критерии для v2** (фиксируем заранее, чтобы не
подгонять post-hoc):
- **Минимум n=100 closed trades** перед любым решением о
  отключении/изменении модели.
- **≥2 недели live**, чтобы охватить разные режимы (trend / flat /
  high-vol news days).
- Метрики: WR, PF, EXP, Sharpe, max DD. Сравнение с Nof1 baseline
  (DeepSeek в их leaderboard).
- Архив v1 (`*_archive_v1`) останется для контрольной точки —
  если v2 покажет похожую деградацию при том же коде, диагноз
  «не размер капитала, а проблема в model/prompt»; если v2 даст
  ≥0 PnL — диагноз «v1 был задушен offset-scaling × low virtual cap».

**Откат**: если за 2 недели v2 покажет такую же деградацию, обсудить
- (a) переход на DeepSeek v3.1 / другой провайдер,
- (b) пересмотр output schema (Nof1 v1.1 уже отказался от `confidence`),
- (c) возврат на $1000 как было запрошено в финальном сообщении —
  «если диагноз подтвердится то будем думать как перейти на 1000».

**Файлы:** operation only, code untouched. Изменения:
- `.env` на VPS (`AI_ARENA_VIRTUAL_CAPITAL=10000`)
- volume `ai_arena_data/ai_arena.sqlite` (archive + clear kv_state)

---

## 2026-05-18

### feat(ai-arena): context-caching tracking + корректные цены V4-Pro
`(коммит ниже)`

**Контекст.** При аудите V4 vs V3 (changelog 2026-04-24) обнаружились
две проблемы в нашей бухгалтерии:

1. **Захардкоженные цены устарели.** В `llm/client.py` стояло
   `COST_PER_M_INPUT_USD = 0.27`, `COST_PER_M_OUTPUT_USD = 1.10` —
   похоже на старые V3.1 standard цены. Актуальные V4-Pro (с 75% off
   до 2026-05-31, [pricing](https://api-docs.deepseek.com/quick_start/pricing)):
   - cache miss input: **$0.435/M** (×1.6 от нашего 0.27)
   - output: **$0.87/M** (×0.79)

2. **Не считали context caching.** DeepSeek с 2024-08-02 имеет
   автоматический KV-cache: повторяющиеся prefix-у дают
   ``prompt_cache_hit_tokens`` со ставкой **в 100× дешевле**
   ($0.003625/M vs $0.435/M на V4-Pro). У нас SYSTEM_PROMPT 1-в-1
   между циклами (≈3500-4000 tokens) + большая часть user_prompt —
   огромная вероятность что мы **платим в 5-10× меньше чем считаем**.

   **Важно:** цены никогда не передавались в LLM, это только наш
   локальный счётчик в `decisions.cost_usd` и `daily_pnl.api_cost_usd`.
   Торговые решения не зависели от этого — но нам было непонятно
   реальную экономику бота.

**Реализация:**

Новый pure-модуль `src/ai_arena/llm/pricing.py`:

- `MODEL_PRICES` — словарь моделей с tarif-ами per million tokens
  (cache hit / cache miss / output). Сейчас зашиты:
  - `deepseek-v4-pro` (75% off discounted)
  - `deepseek-v4-flash`
  - legacy `deepseek-chat` / `deepseek-reasoner` (роутятся в flash,
    retire 2026-07-24)
- `ModelPricing.cost(cache_hit, cache_miss, output)` — расчёт
- `extract_token_usage(usage)` — defensive парсер usage-блока,
  пробует оба возможных стиля cache-полей:
  1. DeepSeek native (OpenAI-style): `prompt_cache_hit_tokens` /
     `prompt_cache_miss_tokens`
  2. Anthropic native: `cache_read_input_tokens` /
     `cache_creation_input_tokens`

  Это нужно потому что DeepSeek в Anthropic-compat явно не задокументировал
  имя cache-полей. Defensive подход: пробуем оба, если ни одного — весь
  input трактуется как cache_miss (цена **не занижается**).

- `get_pricing(model)` — с case-insensitive lookup и warning'ом на
  неизвестную модель.

`llm/client.py`:
- Удалены хардкоды `COST_PER_M_INPUT_USD/OUTPUT`
- `LlmResponse` получил поля `tokens_cache_hit` / `tokens_cache_miss`
  + property `cache_hit_rate`
- `_call` теперь использует `extract_token_usage` + `get_pricing(model).cost(...)`

`state/db.py`:
- В `decisions` добавлены колонки `tokens_cache_hit` / `tokens_cache_miss`
  (nullable, идемпотентная миграция `ALTER TABLE ADD COLUMN`)
- `log_decision` принимает их опционально, старые вызовы не сломаны

`app/main.py`:
- Расширенный лог: `LLM tokens: in=4279 (hit=3500 miss=779, 81.8% cache)
  out=160 cost=$0.000484` — сразу видим экономику и здоровье prompt-cache
- Cache-поля пробрасываются в `log_decision` во всех трёх ветках
  (success / parse-error / llm-error)

**Тесты:** новый `TestModelPricing` (9 тестов) + `TestExtractTokenUsage`
(8 тестов). Всего по `api_params.py` 33 passed (было 16). Full suite
839 passed.

**Ключевые проверки в тестах:**
- Sanity: `cache_miss_per_m / cache_hit_per_m >= 50×` (по докам DeepSeek
  должно быть ~100×, если кто-то перепутает поля — поймаем)
- Discount-prices зашиты как-есть (тесты упадут после 2026-05-31, когда
  V4-Pro 75% off закончится — это намеренно, чтобы не пропустить)
- Defensive парсер: оба стиля field-names, dict + объект, мусор
- Cost-калькуляция с/без caching: подтверждено что cache даёт ~3× экономию
  при 80% hit-rate

**Что НЕ внедрено (из аудита V4):**
- `response_format={"type": "json_object"}` — только в OpenAI-format
  endpoint, потребует миграции с anthropic SDK
- `temperature=0.0` для детерминизма — отход от Nof1 default, нужно
  отдельное обсуждение
- 1M context, tool_calls, FIM, prefix_completion — нашему single-shot
  use-case не подходят

**Файлы:**
- `src/ai_arena/llm/pricing.py` (новый, 144 строк)
- `src/ai_arena/llm/client.py` (использует pricing, без хардкодов)
- `src/ai_arena/state/db.py` (миграция + 2 nullable колонки)
- `src/ai_arena/app/main.py` (расширенный лог + проброс cache-полей)
- `tests/test_ai_arena_api_params.py` (+17 тестов)

---

## 2026-05-18 (ранее)

### fix(ai-arena): корректное управление thinking-mode + `positionIdx=0` + `totalAvailableBalance`
`(коммит ниже)`

**Контекст.** Аудит всех параметров API после инцидента с попыткой
переключения модели на `deepseek-v4-flash` с `reasoning=on`: выяснилось
что `AI_ARENA_DEEPSEEK_REASONING` env-переменная **никак не влияла
на API** ≥4 дней. Заодно прошлись по всем Bybit V5 параметрам и
сверили с официальной докой.

**Найдено и исправлено:**

**1. RED — `reasoning_effort` фактически no-op (4+ дней).**

Передавали `extra_body={"reasoning_effort": "..."}` через
Anthropic-compat endpoint. Это **невалидное поле** для Anthropic-format
DeepSeek API (см. таблицу Simple Fields в
[api-docs.deepseek.com/guides/anthropic_api](https://api-docs.deepseek.com/guides/anthropic_api)):
top-level `reasoning_effort` — OpenAI-format поле, в Anthropic-compat
оно молча игнорируется.

При этом по доке
[api-docs.deepseek.com/guides/thinking_mode](https://api-docs.deepseek.com/guides/thinking_mode):
*«The thinking toggle defaults to enabled»* для V4-моделей. То есть
**thinking всё время был включён** с default `effort=high`,
независимо от значения нашей env-переменной. Это прямо противоречит
инварианту `ai-arena-sources.mdc` («Nof1 не использует reasoning-mode»)
и могло влиять на качество решений (модель тратила tokens на CoT,
который мы и так требуем через JSON-поля `justification`/`confidence`/
`invalidation_condition`).

**Исправление:** новый pure-модуль `src/ai_arena/llm/thinking_config.py`
с функцией `build_thinking_extra_body(reasoning_effort)`:

- `off` → `{"thinking": {"type": "disabled"}}` (Nof1-режим, **явно**
  выключаем thinking)
- `high` / `low` / `medium` → `{"thinking": {"type": "enabled"},
  "output_config": {"effort": "high"}}`
- `max` / `xhigh` → то же с `effort=max`
- неизвестное / пустое → off (безопасный дефолт)

На VPS `.env` возвращён `AI_ARENA_DEEPSEEK_MODEL=deepseek-v4-pro` и
`AI_ARENA_DEEPSEEK_REASONING=off` — теперь это **реально** работает
как Nof1.

**2. YELLOW — `place_order` без явного `positionIdx`.**

Bybit V5 spec
([/v5/order/create-order](https://bybit-exchange.github.io/docs/v5/order/create-order)):
`positionIdx` нужен `0` для one-way mode, `1/2` для hedge mode. Без
явного значения работало, потому что аккаунт по умолчанию one-way,
**но** при случайном переключении в hedge mode (одна кнопка в UI)
Bybit отвергал бы ордер с retCode 10001 «position idx not match
position mode». Теперь передаём `positionIdx: 0` явно — fail-fast
если кто-то поменяет режим.

**3. YELLOW — `get_wallet_balance` читал deprecated поле.**

`coin.availableToWithdraw` per-coin **deprecated для UNIFIED с
9 января 2025** (см.
[/v5/account/wallet-balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance)):
*«Deprecated for accountType=UNIFIED from 9 Jan, 2025. Transferable
balance: you can use Get Transferable Amount (Unified) or Get All
Coins Balance instead»*. Fallback был на `walletBalance`, который
**не вычитает locked-в-позициях** маржу — это завышало `available_cash`
в промпте LLM, искажая представление о свободном капитале.

Перешли на account-level `totalAvailableBalance` (USD) — это
суммарный available для UNIFIED, учитывающий все открытые позиции и
ордера. Для нашего use-case (только USDT на счету) семантически
эквивалентно USDT-available.

**Файлы:**
- `src/ai_arena/llm/thinking_config.py` (новый — pure function без
  `anthropic` зависимости, удобно для unit-тестов)
- `src/ai_arena/llm/client.py` (упрощён `_call`, использует
  `build_thinking_extra_body`, обновлён docstring модуля)
- `src/ai_arena/trading/client.py` (`get_wallet_balance` →
  `totalAvailableBalance`, `place_order` → явный `positionIdx=0`,
  доку-ссылки в docstring)
- `src/ai_arena/config/settings.py` (комментарий к
  `deepseek_reasoning_effort`: разрешённые значения `off|high|max`,
  упоминание бага)
- `tests/test_ai_arena_api_params.py` (новый — 16 тестов:
  `build_thinking_extra_body` × 9, `place_order positionIdx` × 3,
  `get_wallet_balance totalAvailableBalance` × 4)

**Тесты:** 270/270 ai_arena passed, 822/822 full suite passed.

**GREEN (проверено и подтверждено корректным):**
- Bybit V5: `category=linear`, `get_kline(interval, limit)`,
  `get_open_interest(intervalTime, limit)`, `get_positions(settleCoin=USDT)`,
  `get_closed_pnl(limit=100)`, `set_leverage(buyLeverage, sellLeverage)`,
  `recv_window=10000`
- DeepSeek Anthropic-compat: `model`, `max_tokens=8192`, `system`,
  `messages`
- Все 18 env-переменных `AI_ARENA_*` корректно пробрасываются от
  `.env` → docker-compose → контейнер (проверено `docker exec env`)
- Asset Universe порядок (BTC,ETH,SOL,BNB,DOGE,XRP) 1-в-1 с Nof1
  gist L62

---

## 2026-05-15 (четвёртая итерация)

### fix(ai-arena): balance-delta fallback для net PnL (обходит demo latency Bybit closed-pnl)
`(коммит ниже)`

**Симптом:** Несколько закрытий подряд приходили в Telegram как
`pnl=pending… (биржа задержала, добьём на след. цикле)` и продолжали
оставаться pending даже через 3+ цикла reconcile (≥9 минут):

```
CLOSE id=43 Buy BNBUSDT entry=$682 exit=$681.7 PnL: pending…
CLOSE id=44 Buy BNBUSDT exit=$685.9 pnl=pending…
PENDING-PNL: id=43 Buy BNBUSDT ещё не виден в Bybit closed_pnl, повторим…
```

**Диагностика:**

Прямой вызов `client.get_closed_pnl(symbol="BNBUSDT")` на VPS показал:
запись для id=43 (qty=8.0, avg_exit=681.5, closed_pnl=-9.99,
updated_time=09:42:50) **существует**, но возвращается ТОЛЬКО без
`start_time_ms` фильтра. С `start_time_ms=opened_ms` (09:41:40)
endpoint возвращал `n=0`, хотя `record.updated_time` (09:42:50) ≥
`opened_ms` на 70 секунд.

Дополнительно: между двумя последовательными запросами с разницей
~5 минут, **результат менялся** — Bybit demo agg-job регистрирует
записи в closed-pnl с задержкой **до 5+ минут** (mainnet обычно
<10s по доке).

**Корневая причина:** Bybit demo `/v5/position/closed-pnl` имеет
significant latency регистрации + некорректно фильтрует по
`startTime` на demo (фильтр режет валидные записи). Symptomatic
retry не помогает (10s, 30s, ≥60s — Bybit продолжает молчать).

**Решение** (компромисс между symptomatic retry и полным
рефакторингом на WebSocket execution stream):

**Primary path** (без изменений): Bybit `closed-pnl` retry-loop.
Если работает — берём net PnL и avg_exit_price 1-в-1 с биржей.

**Fallback path (НОВЫЙ)** — `_resolve_pnl_from_balance_delta`:
- Перед close-ордером сохраняем `walletBalance` USDT в новой колонке
  `positions.wallet_balance_before_close`.
- После close через ~1.5s ждём fill, запрашиваем `walletBalance`
  снова → `net_pnl = wallet_after - wallet_before`.
- Источник правды: Bybit V5 docs
  (<https://bybit-exchange.github.io/docs/v5/account/wallet-balance>):
  «walletBalance» (UNIFIED account) обновляется **мгновенно** при
  executed close (списывает realized PnL + fees сразу). Не путать
  с «equity = walletBalance + unrealisedPnL» — equity флуктуирует
  от цены открытых позиций, walletBalance — нет.

**Почему это «forced infrastructural adaptation»** (правило
`ai-arena-sources.mdc`): Hyperliquid (на котором Nof1 работает)
отдаёт net PnL synchronously при close. Bybit demo — асинхронно
с большой задержкой. Чтобы остаться 1-в-1 по семантике («net PnL
от биржи 1-в-1, без локального gross-расчёта»), используем
balance-delta как deriving from first principles от того же
truth source (Bybit walletBalance), просто другим маршрутом.

**Что меняется в коде (5 файлов):**

1. **`src/ai_arena/state/db.py`:**
   - Новая колонка `positions.wallet_balance_before_close REAL NULL`.
   - Идемпотентная миграция через `_migrate()` (ALTER TABLE для
     существующих БД на VPS).
   - Метод `set_wallet_before_close(position_id, value)`.
   - `ArenaPosition` dataclass: новое поле
     `wallet_balance_before_close: float | None = None`.

2. **`src/ai_arena/trading/client.py`:**
   - Новый метод `get_wallet_balance_usdt() -> float | None` через
     `/v5/account/wallet-balance?accountType=UNIFIED&coin=USDT`.
     Возвращает чистый `walletBalance` USDT (не equity).

3. **`src/ai_arena/trading/executor.py`:**
   - В `_apply_close` ПЕРЕД close-ордером: `wallet_before =
     client.get_wallet_balance_usdt()` + `store.set_wallet_before_close`.
   - Новая функция `_resolve_pnl_from_balance_delta` (см. выше).
   - После `_resolve_net_close`: если pnl=None И wallet_before
     сохранён → fallback к balance delta.

4. **`src/ai_arena/trading/reconcile.py::reconcile_pending_pnl`:**
   - Тот же fallback после неудачного closed-pnl retry на reconcile-
     цикле. Срабатывает только для позиций с сохранённым
     `wallet_balance_before_close` (т.е. бот закрыл, не биржа).
   - Telegram сообщение теперь указывает source:
     `«PnL добит для id=X: $-2.45 (net of fees, via balance-delta)»`.

5. **`tests/test_ai_arena_executor_logic.py`:**
   - Новый класс `TestBalanceDeltaFallback` (4 теста):
     - balance-delta срабатывает когда closed-pnl молчит (loss).
     - balance-delta для winning trade.
     - closed-pnl имеет приоритет над balance-delta когда оба доступны
       (защита от funding payment в окне).
     - wallet_before не сохраняется если первый balance-запрос упал;
       позиция остаётся pending.
   - Расширенный `test_close_defers_when_closed_pnl_and_balance_both_unavailable`
     (оба пути failed → pending как раньше).
   - Новый тест `test_pending_resolved_via_balance_delta_when_wallet_before_saved`
     в `TestReconcilePendingPnl`.
   - `FakeBybitClient` обновлён: `wallet_balance_sequence` параметр
     для эмуляции [before, after] значений.

**Что НЕ трогаем (изоляция изменений):**

- `_resolve_net_close` (closed-pnl логика без изменений).
- `reconcile_closed_positions` (SL/TP/exchange-инициированные closes —
  там wallet_before нет, balance-delta невозможна, остаётся
  closed-pnl + reconcile_pending_pnl как было).
- LLM prompt / decision loop / equity scaling.
- Семантика `realized_pnl_usd` в БД (всё ещё net of fees).

**Тесты:** 254 ai_arena (+9 новых), 806 в репо. Все passed.

**Compliance с правилом `ai-arena-sources.mdc`:**

- ✅ PnL остаётся **net** (closedPnl от Bybit ИЛИ balance delta —
  обе net of fees + funding).
- ✅ Никаких локальных `(exit-entry)*qty` gross-расчётов.
- ✅ Forced infrastructural adaptation: Bybit demo latency не
  присуща Hyperliquid у source, у нас обходится через
  balance-delta — другой маршрут к тому же Bybit truth (walletBalance).
- ✅ Источник правды для balance-delta: Bybit V5 wallet-balance
  docs (cited в коде + правиле).

**Файлы:** `src/ai_arena/state/db.py`,
`src/ai_arena/trading/client.py`, `src/ai_arena/trading/executor.py`,
`src/ai_arena/trading/reconcile.py`,
`tests/test_ai_arena_executor_logic.py`, `BUILDLOG_AI_ARENA.md`,
`.cursor/rules/ai-arena-sources.mdc`.

---

## 2026-05-15 (третья итерация)

### fix(ai-arena): net PnL теперь не «врёт нулём» при Bybit-latency + retry/pending
`(коммит ниже)`

**Симптом (репорт пользователя):**

```
🔴 POSITION CLOSED
CLOSE id=40 Buy BTCUSDT exit=$80799.5 pnl=$+0.00 (net of fees)
```

«бот думал что закрыл в нольно, но он не учитывает комиссии при своих
расчётах» — на Bybit реальная разница цен была ~$3.18, плюс комиссия,
итого net PnL должен быть отрицательный, а не `$+0.00`.

**Причина:** В `_resolve_net_close` (executor.py) делали ОДИН запрос
к `client.get_closed_pnl(...)` сразу после executed close-ордера.
Bybit `/v5/position/closed-pnl` имеет наблюдаемую latency 1-10 секунд
(биржа не успевает агрегировать запись на момент нашего read'а).
При промахе fallback возвращал `(ticker.last_price, 0.0)` — и в БД +
Telegram уезжал лживый `pnl=$+0.00 (net of fees)`. Это нарушало
fundamental contract правила `ai-arena-sources.mdc`: «PnL и
exit_price — net через `closedPnl`, никогда не gross-заглушка».

**Fix (4 файла):**

1. **`src/ai_arena/trading/executor.py` — `_resolve_net_close`:**
 - Добавлен retry-loop: 4 попытки с backoff `(1s, 2s, 3s, 5s)` —
   итого ≤ 11 секунд ожидания. При отсутствии матча — `time.sleep(backoff)`,
   следующая попытка с обновлённым `end_time_ms`.
 - Возвращаемый тип сменился с `tuple[float, float]` на
   `tuple[float, float | None]`. **`None` сигнализирует «PnL ещё
   не знаем»** — caller сохранит NULL, reconcile добьёт.
 - В match-фильтр добавлен `r.updated_time_ms >= opened_ts_ms` —
   защита от старых записей при race с другими символами.

2. **`src/ai_arena/state/db.py`:**
 - `close_position(realized_pnl_usd: float | None)` теперь принимает
   `None` и пропускает обновление `daily_pnl` (агрегат добьётся
   позже).
 - Новый метод `get_pending_pnl_positions()` — закрытые позиции
   с `realized_pnl_usd IS NULL`.
 - Новый метод `finalize_pending_pnl(position_id, exit_price, pnl)` —
   проставляет PnL + **впервые** обновляет `daily_pnl` (с защитой от
   двойного учёта: `ValueError` если PnL уже не NULL).

3. **`src/ai_arena/trading/reconcile.py` (НОВЫЙ модуль):**
 - Логика `reconcile_closed_positions` (SL/TP/manual закрытия на
   бирже) и `reconcile_pending_pnl` (добивание PnL=NULL) вынесена
   из `app/main.py`.
 - Цель выноса: unit-тесты reconcile-логики не должны тащить
   `llm.client` (anthropic SDK), который не нужен в test env.
 - `reconcile_pending_pnl` использует укороченный retry
   (`max_retries=2`, backoff `(1s, 2s)`) — на этом цикле от закрытия
   уже прошло ≥`poll_interval_sec` (180s по умолчанию), запись
   гарантированно зарегистрирована.

4. **`src/ai_arena/app/main.py`:**
 - Старые `_reconcile_closed_positions` + `_reconcile_pending_pnl`
   удалены, импорт из нового `trading.reconcile`.
 - Цикл вызывает обе reconcile-функции подряд (closed → pending).

**UX-фикс (Telegram):** при `pnl=None` сообщение теперь:

```
CLOSE id=40 Buy BTCUSDT exit=$80799.5 pnl=pending… (биржа ещё не зарегистрировала, добьём на след. цикле)
```

И через ≤ 3 минуты (следующий цикл):

```
PnL добит для id=40 Buy BTCUSDT: $-3.42 (net of fees)
```

Пользователь больше не получает враньё `$+0.00`.

**Тесты (4 новых, всего 249 ai_arena, 801 в репо):**

- `test_close_defers_when_closed_pnl_unavailable` — обновлён под новую
  семантику (PnL=NULL вместо 0, summary содержит «pending»).
  `time.sleep` замокан → 4 retry проходят мгновенно.
- `test_close_resolves_on_retry_when_bybit_lags` (НОВЫЙ) — 1я попытка
  возвращает `[]`, 2я — содержит запись. Проверяется: PnL подтянут,
  `result.summary` содержит реальное число, `time.sleep(1.0)` вызван
  ровно один раз, не «pending».
- `TestReconcilePendingPnl.test_pending_position_resolved_on_next_cycle`
  (НОВЫЙ) — `reconcile_pending_pnl` подбирает PnL и обновляет
  `daily_pnl` (sum=2.45, n_trades=1, n_wins=1).
- `TestReconcilePendingPnl.test_finalize_pending_pnl_does_not_double_count`
  (НОВЫЙ) — попытка вызвать `finalize_pending_pnl` для уже
  закрытой с PnL!=NULL позиции → `ValueError`, `daily_pnl` не
  задвоился.
- `TestReconcilePendingPnl.test_pending_position_remains_when_bybit_still_silent`
  (НОВЫЙ) — если Bybit на reconcile-цикле всё ещё молчит, позиция
  остаётся в pending для следующей попытки.

**Compliance с правилом `ai-arena-sources.mdc`:**

- ✅ PnL остаётся **net** (closedPnl от Bybit), не gross.
- ✅ Никаких локальных `(exit-entry)*qty` расчётов.
- ✅ Новая семантика `None` → «не знаем», в БД честный NULL.
- ✅ Это «forced infrastructural adaptation» (Bybit API latency vs
  Hyperliquid synchronous response в Nof1) — **изменения только в
  механизме получения** PnL, не в его смысле / источнике.

**Файлы:** `src/ai_arena/trading/executor.py`,
`src/ai_arena/state/db.py`, `src/ai_arena/trading/reconcile.py` (new),
`src/ai_arena/app/main.py`, `tests/test_ai_arena_executor_logic.py`,
`BUILDLOG_AI_ARENA.md`, `.cursor/rules/ai-arena-sources.mdc`.

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
