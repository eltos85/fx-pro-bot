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

## 2026-05-14

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
