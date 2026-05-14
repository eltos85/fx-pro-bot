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
