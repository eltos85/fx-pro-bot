# Proposal: AI-Trader v2 → Alpha Arena Clone (DeepSeek V4-Pro)

> **Статус:** DRAFT / PENDING APPROVAL. Не реализовано. Реализация в отдельной
> ветке `feat/alpha-arena-clone` после явного go-сигнала пользователя.
>
> **Дата создания:** 2026-05-13
> **Запрос пользователя:** «нужно подготовить план перехода на их подход. От
> нашей страты ничего не надо будет сохранять, только код по работе с апи.
> Модель берем DeepSeek V4-Pro».
>
> Предыдущий, более мягкий вариант перехода — `AI_TRADER_PROPOSAL_DISCRETIONARY.md`
> (там сохраняли промпт и часть правил). Этот документ — **полный rewrite**
> стратегии на основе публично известной архитектуры Nof1 Alpha Arena.

---

## 1. Источники и факты

### Публичные материалы Nof1, на которых построен план

| Источник | Тип | Что взяли |
|---|---|---|
| [nof1.ai/blog/TechPost1](https://nof1.ai/blog/TechPost1) | Официальный tech-post Nof1 | Архитектура harness, action space, output schema, выводы Season 1, реальные snippets промптов |
| [gist: nof1-prompt.md by @wquguru](https://gist.github.com/wquguru/7d268099b8c04b7e5b6ad6fae922ae83) | Реверс-инжиниринг System+User prompt | Полный текст промптов, model-specific advice, формула risk_usd |
| [FMZ Quant replication guide](https://blog.mathquant.com/2026/02/05/alphaarena-ai-model-battle-a-hands-on-guide-to-replicating-deepseeks-leading-ai-quantitative-trading-system.html) | Implementation guide с кодом | Точная структура data flow, 7-node loop, JS-код подсчёта индикаторов |
| [DeepSeek V4-Pro overview](https://deepseekai.guide/models/deepseek-v4-pro/) | Официальные характеристики | 1.6T MoE, 1M context, `reasoning_effort`, бенчмарки vs V3.2 |
| [HuggingFace blog: DeepSeek V4 for agents](https://huggingface.co/blog/deepseekv4) | Agent-focused обзор | «Closer to an agent model than a long-context one» — почему V4 заточен под наш use case |

### Ключевые факты, важные для плана

1. **Nof1 использует DeepSeek V3.1 standard, НЕ reasoning-mode.** Подтверждено
   автором gist [@wquguru]: «their documentation explicitly mentions DeepSeek
   v3.1 — the standard version, not R1». Chain-of-thought принуждается
   **через обязательные JSON-поля** (`justification`, `confidence`,
   `invalidation_condition`), а не через `reasoning_effort`.

2. **DeepSeek V4-Pro** (релиз 24 апреля 2026) — преемник V3.x. 1.6T параметров
   MoE, 49B активных. `reasoning_effort=off|high|max`. На `off` это прямой
   аналог V3.1 standard, но **на ~12-22% умнее** по reasoning-бенчмаркам
   (MMLU-Pro 73.5 vs 65.5; HumanEval 76.8 vs 62.8). Latency на `off`
   сопоставима с V3.1.

3. **Реплицируем Nof1 на V4-Pro c `reasoning_effort=off`.** Это даёт нам
   архитектуру Nof1 + чистый upgrade модели. Включать `reasoning_effort=high`
   на critical-циклах — отдельный шаг (Step 2 ниже).

4. **Cycle Nof1 — 2-3 минуты.** У нас стартуем с 3 минут (`AI_TRADER_POLL_INTERVAL_SEC=180`),
   с возможностью расширить до 5 минут если упрёмся в latency LLM / rate limits Bybit.

5. **Никаких новостей.** Nof1 в Season 1 явно не даёт news/social. Это
   ключевое архитектурное решение — модель **должна выводить narrative
   только из price+funding+OI**. Наш RSS-провайдер выбрасывается.

---

## 2. Цель

Заменить нашу стратегию (RSI/BB/SMA + наши custom-триггеры PEAK-DRAWDOWN /
LOCKED-PROFIT / ADVERSE NEW EVIDENCE) на **архитектуру Nof1 Alpha Arena**:

- Жёсткая структурная output schema, в которой сами поля принуждают LLM к
  meta-cognition (`confidence`, `invalidation_condition`, `risk_usd`,
  `justification`).
- Богатый, но компактный data feed (3m intraday × 10 + 4h longer-term, RSI(7/14),
  MACD, EMA20/50, ATR(3/14), Volume, Open Interest, Funding Rate).
- Sharpe Ratio как self-calibration signal в каждом промпте.
- 3-минутный stateless цикл (single-cycle, без dual-timer).
- DeepSeek V4-Pro вместо V4-Flash.

---

## 3. Что сохраняем и что выбрасываем

### 3.1 Сохраняем (infrastructure layer)

| Модуль | Файл(ы) | Что оставляем |
|---|---|---|
| Bybit API client | `src/ai_trader/trading/client.py` | Полностью. Расширяем методами `get_open_interest`, `get_funding_rate_history` |
| DeepSeek LLM client | `src/ai_trader/llm/client.py` | Полностью. Меняем только модель → `deepseek-v4-pro`, убираем `thinking={"type":"enabled"}` (Nof1 не использует) |
| SQLite engine + миграции | `src/ai_trader/state/db.py` | Engine оставляем. Схему расширяем (новые поля) |
| Telegram уведомления | `src/ai_trader/telegram/bot.py` | Полностью. Меняем только формат сообщений под новые поля |
| KillSwitch shell | `src/ai_trader/safety/killswitch.py` | Полностью. Содержание лимитов не меняется |
| Config | `src/ai_trader/config/settings.py` | Расширяем (см. §6) |
| Docker / deploy | `docker-compose.yml`, `scripts/deploy-on-vps.sh`, `.github/workflows/deploy-vps.yml` | Без изменений |

### 3.2 Выбрасываем (strategy layer — полный rewrite)

| Что | Файл | Причина |
|---|---|---|
| Все наши промпты (SYSTEM_PROMPT, SYSTEM_PROMPT_REVIEW, build_user_prompt, build_user_prompt_review) | `src/ai_trader/llm/prompts.py` | Несовместимы с Nof1-схемой. Переписываем с нуля. |
| Наши custom-триггеры (PEAK-DRAWDOWN, LOCKED-PROFIT, ADVERSE NEW EVIDENCE, MACRO REGIME SHIFT) | `src/ai_trader/llm/prompts.py` | Nof1 даёт `invalidation_condition` в schema — pre-registered exit логика, не triggers |
| Наши индикаторы (RSI(14), ATR(14), EMA20/200, BB(20,2), SMA20) | `src/ai_trader/analysis/indicators.py` | Меняется набор: RSI(7+14), MACD, EMA20/50, ATR(3+14), Volume avg |
| Наш context layout | `src/ai_trader/trading/context.py` | Полностью другой layout (3m × 10 + 4h, OI, funding bands) |
| Наш executor parser + validation | `src/ai_trader/trading/executor.py` | Action schema другая (signal/quantity/leverage/confidence/...) |
| Dual-timer (full + review циклы) | `src/ai_trader/app/main.py` | Nof1 — single 3-мин цикл, без режимов |
| RSS news provider | `src/ai_trader/news/rss.py`, `src/ai_trader/news/__init__.py` | Nof1 не использует news |
| Все наши тесты стратегии | `tests/test_ai_trader.py`, `tests/test_ai_trader_indicators.py` | Переписываем под новую логику |
| Параметры conviction-based sizing (если есть остатки) | `settings.py`, executor | Nof1 — модель сама считает quantity, бот валидирует limits |

### 3.3 По умолчанию остаётся

- Список пар: BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, DOGEUSDT (5 шт., наша
  изоляция от bybit_bot). Nof1 берёт 6 (+ SOL) — у нас SOL занят bybit_bot,
  оставляем 5.
- Виртуальный капитал: $500 (не $10k как у Nof1 — наша песочница).
- KillSwitch: max 3 одновременных, max 5x leverage, $50/day loss,
  $200 total loss, $10 per-trade risk. Это **наша infrastructure-граница**,
  не часть стратегии; Nof1 её не имеет, мы оставляем.

---

## 4. Архитектура (новая)

```
src/ai_trader/
├── trading/
│   ├── client.py                # СОХРАНЯЕМ + 2 новых метода
│   ├── context.py               # ПЕРЕПИСАТЬ под Nof1-layout
│   └── executor.py              # ПЕРЕПИСАТЬ под новую schema
├── analysis/
│   ├── indicators.py            # ПЕРЕПИСАТЬ (RSI7/14, MACD, EMA20/50, ATR3/14)
│   └── sharpe.py                # НОВЫЙ файл (rolling Sharpe)
├── llm/
│   ├── client.py                # МЕНЯЕМ модель → v4-pro, убираем thinking
│   └── prompts.py               # ПЕРЕПИСАТЬ полностью (Nof1 layout)
├── state/
│   └── db.py                    # МИГРАЦИЯ: +confidence, +invalidation,
│                                #            +risk_usd, +sharpe_at_decision
│                                #            +equity_snapshots таблица
├── safety/
│   └── killswitch.py            # БЕЗ ИЗМЕНЕНИЙ
├── telegram/
│   └── bot.py                   # ФОРМАТ сообщений под новые поля
├── app/
│   └── main.py                  # ПЕРЕПИСАТЬ loop (single 3-min cycle)
├── config/
│   └── settings.py              # РАСШИРИТЬ (см. §6)
└── news/                        # УДАЛИТЬ всю папку
```

### 4.1 Новый цикл (по шагам, аналог Nof1 7-node loop)

1. **Heartbeat trigger.** Каждые `poll_interval_sec` (default 180).
2. **Reset account snapshot.** equity, available cash, total return %,
   накопленный invocation count `minutes_elapsed`.
3. **Compute Sharpe.** Rolling Sharpe из таблицы `equity_snapshots` за
   последние 14 дней (или с начала сессии если меньше).
4. **Market data acquisition (per symbol, параллельно):**
   - `get_klines(interval="3", limit=50)` → last 10 bars → 3m indicators.
   - `get_klines(interval="240", limit=60)` → last 10 bars → 4h indicators.
   - `get_open_interest(intervalTime="5min", limit=20)` → latest + avg.
   - `get_funding_rate_history(limit=8)` → latest + 24h avg.
5. **Position acquisition.** `get_positions()` → список с unrealised PnL,
   liquidation price. Merge с invalidation_condition / confidence / risk_usd
   из БД (LLM их registered при open).
6. **Build user prompt.** Один string по шаблону Nof1 (см. §5).
7. **LLM call.** DeepSeek V4-Pro, `reasoning_effort="off"`, max_tokens=8192.
   Ожидание ~15-30 сек.
8. **Parse JSON action.** Validate схему (см. §5.2).
9. **KillSwitch & R:R hard-check.** R:R ≥ 1.5, risk_usd ≤ $10, leverage ≤ 5,
   позиций < 3, daily loss < $50, total loss < $200. Reject если нарушено.
10. **Execute on Bybit.** `set_leverage` → `place_order` (market + SL + TP).
11. **Persist decision.** Запись в `decisions` со всеми полями LLM (включая
    confidence, invalidation, risk_usd, sharpe_at_decision).
12. **Telegram notify** только при open / close. Hold → silent.
13. **Equity snapshot.** Запись в `equity_snapshots` (для следующего Sharpe).

---

## 5. Промпты и output schema

### 5.1 SYSTEM_PROMPT (адаптация Nof1 под Bybit + наши лимиты)

> Полный текст — Приложение A. Здесь — структура.

Адаптировано из [gist nof1-prompt.md](https://gist.github.com/wquguru/7d268099b8c04b7e5b6ad6fae922ae83)
с правками: Hyperliquid → Bybit, 20x leverage → 5x, $10k capital → $500,
allowed coins → наши 5, действие `close` без partial exits.

Структура (8 секций):

```
# ROLE & IDENTITY
You are AI Trading Model deepseek-v4-pro on Bybit perp futures (demo).
Mission: maximize risk-adjusted return (PnL) through systematic, disciplined trading.

# TRADING ENVIRONMENT
- Exchange: Bybit USDT-perp
- Asset universe: BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, DOGEUSDT
- Virtual capital: $500
- Decision cycle: every 3 minutes
- Leverage: 1x-5x (hard cap; bot rejects above)
- Funding schedule: 00:00 / 08:00 / 16:00 UTC

# ACTION SPACE (exactly four)
1. buy_to_enter  — open new LONG (one position per coin, no pyramiding)
2. sell_to_enter — open new SHORT
3. hold          — no change to current positions
4. close         — full close existing position (no partial exits)

# CAPITAL SAFETY (bot-enforced — bot rejects violations)
- Max 3 simultaneous positions
- Max 5x leverage
- Max $10 risk per trade (|entry - stop_loss| * quantity ≤ 10)
- Daily realised loss ≤ $50
- Total realised loss ≤ $200
- R:R ≥ 1.5 mandatory (bot rejects, return HOLD if your idea has R:R < 1.5)

# POSITION SIZING
Position notional (USD) = quantity * current_price
Risk USD = |entry - stop_loss| * quantity      # NOTE: NOT multiplied by leverage
Conviction → leverage:
  confidence 0.30-0.50 → 1-2x
  confidence 0.50-0.70 → 2-3x
  confidence 0.70-1.00 → 3-5x

# OUTPUT FORMAT (single JSON, last in response)
{
  "signal": "buy_to_enter" | "sell_to_enter" | "hold" | "close",
  "coin":   "BTCUSDT" | "ETHUSDT" | "BNBUSDT" | "XRPUSDT" | "DOGEUSDT",
  "quantity": <float>,
  "leverage": <integer 1-5>,
  "stop_loss":   <float>,
  "profit_target": <float>,
  "invalidation_condition": "<observable signal that voids your thesis>",
  "confidence": <float 0-1>,
  "risk_usd":   <float ≤ 10>,
  "justification": "<max 500 chars>"
}

# DATA INTERPRETATION
Technical indicators in user prompt:
- EMA20/50: trend direction
- MACD: momentum
- RSI(7) for intraday, RSI(14) for trend (>70 overbought, <30 oversold; ≥75 / ≤25 extreme)
- ATR(3) vs ATR(14): volatility regime change
- Volume current vs avg: participation
- Open Interest latest vs avg: crowd positioning
- Funding rate: sentiment skew
  - |fr| < 0.05% → neutral
  - 0.05-0.20% → mild
  - > 0.20% → strong skew (potential reversal)

# DATA ORDERING (critical, repeated 4× in user prompt)
ALL price/signal arrays are OLDEST → NEWEST. Last element = most recent.

# OPERATIONAL CONSTRAINTS — WHAT YOU DON'T HAVE
- No news feed, no social media, no narratives — infer everything from price+funding+OI
- No conversation history — each decision is stateless
- No external APIs, no orderbook depth, no limit orders (market only)
- No partial exits, no hedging, no pyramiding

# PHILOSOPHY (4 lines, не checklist)
- Capital preservation first
- Discipline over emotion — follow your invalidation_condition, don't move stops
- Quality over quantity — fewer high-conviction trades > many low-conviction
- Hold is a valid action (not "is safe", but "is valid")

# SHARPE FEEDBACK
You will receive your rolling 14d Sharpe in user prompt. Use it to self-calibrate:
- Sharpe < 0  → reduce size, tighten stops, be more selective
- Sharpe > 1  → strategy working, maintain discipline
```

### 5.2 USER_PROMPT layout (per cycle)

```
It has been {minutes_elapsed} minutes since you started trading.

⚠️ ALL PRICE/SIGNAL DATA BELOW IS ORDERED: OLDEST → NEWEST ⚠️
Intraday series are at 3-minute intervals unless stated otherwise.

═════════════════════════════════════════════════
CURRENT MARKET STATE FOR ALL COINS
═════════════════════════════════════════════════

### BTCUSDT
current_price = {p}, current_ema20 = {e}, current_macd = {m}, current_rsi(7) = {r7}
Open Interest:  Latest: {oi_latest}  |  Average (20×5min): {oi_avg}
Funding Rate:   Latest: {fr}  (band: {neutral|mild|strong})

Intraday (3m × 10, oldest→newest):
  Mid prices:    [...]
  EMA20:         [...]
  MACD:          [...]
  RSI(7):        [...]
  RSI(14):       [...]

Longer-term (4h):
  EMA20 vs EMA50:   {e20} vs {e50}
  ATR(3) vs ATR(14): {atr3} vs {atr14}
  Volume current vs avg: {v} vs {v_avg}
  MACD (4h, ×10):   [...]
  RSI(14, 4h, ×10): [...]

### ETHUSDT
[same structure]
...

═════════════════════════════════════════════════
ACCOUNT & PERFORMANCE
═════════════════════════════════════════════════
Current Total Return: {pct}%
Sharpe Ratio (rolling 14d): {sharpe}
Available Cash: ${cash}
Current Account Value: ${equity}

Open Positions:
[
  {
    "symbol": "ETHUSDT", "quantity": 0.05, "entry_price": 3120.5,
    "current_price": 3145.2, "liquidation_price": 2850.0,
    "unrealized_pnl": 1.23, "leverage": 3,
    "exit_plan": {
      "profit_target": 3250.0,
      "stop_loss": 3050.0,
      "invalidation_condition": "4H RSI below 45 & MACD turns negative"
    },
    "confidence": 0.65, "risk_usd": 3.50, "notional_usd": 157.26
  }
]

⚠️ DATA ORDER: OLDEST → NEWEST ⚠️
Based on the above, return your trading decision in the JSON format defined in system prompt.
```

### 5.3 Output JSON schema (точная Nof1 schema)

```json
{
  "signal": "buy_to_enter | sell_to_enter | hold | close",
  "coin": "BTCUSDT",
  "quantity": 0.001,
  "leverage": 3,
  "stop_loss": 65000.0,
  "profit_target": 67500.0,
  "invalidation_condition": "4H RSI breaks back below 40",
  "confidence": 0.72,
  "risk_usd": 6.50,
  "justification": "BTC breaking above EMA20 with strong 4h momentum; OI rising while funding neutral suggests organic flow not crowded longs..."
}
```

**Бот валидирует:**

1. `signal` ∈ allowed values.
2. `coin` ∈ allowed_symbols (whitelist из config).
3. Если `signal=hold`: остальные поля игнорируются (placeholder ОК).
4. Если `signal=close`: нужен match на open position по coin.
5. Если `buy_to_enter|sell_to_enter`:
   - `quantity > 0` и кратна `qty_step` инструмента.
   - `leverage ∈ [1,5]`.
   - Для `buy_to_enter`: `stop_loss < current_price < profit_target`.
   - Для `sell_to_enter`: `profit_target < current_price < stop_loss`.
   - R:R = `|profit_target - current_price| / |current_price - stop_loss| ≥ 1.5`.
   - `risk_usd = |current_price - stop_loss| * quantity ≤ $10`.
   - `confidence ∈ [0, 1]`.
   - Itoгово KillSwitch: max_open_positions, daily_loss, total_loss.

При любом нарушении — `signal` интерпретируется как `hold` + лог error в БД
(в поле `error` в таблице `decisions`).

---

## 6. Конфигурация (`settings.py` → новые поля)

| Поле | Default | Env var |
|---|---|---|
| `deepseek_model` | `deepseek-v4-pro` | `AI_TRADER_DEEPSEEK_MODEL` |
| `deepseek_reasoning_effort` | `off` | `AI_TRADER_DEEPSEEK_REASONING` |
| `deepseek_max_tokens` | `8192` | `AI_TRADER_DEEPSEEK_MAX_TOKENS` |
| `poll_interval_sec` | `180` (3 мин) | `AI_TRADER_POLL_INTERVAL_SEC` |
| `review_interval_sec` | `0` (отключён) | `AI_TRADER_REVIEW_INTERVAL_SEC` |
| `news_enabled` | `False` (удалить вообще) | — |

Цены DeepSeek V4-Pro в `llm/client.py`:
- COST_PER_M_INPUT_USD = TBD (по api-docs.deepseek.com на момент реализации)
- COST_PER_M_OUTPUT_USD = TBD

Сметa: при `reasoning_effort=off`, ~5K input + 1.5K output на цикл, 480 циклов/сут →
~$1-2/день. При `reasoning_effort=high` → ~$3-5/день.

---

## 7. БД миграция

### Таблица `decisions` (existing)
Добавляем:
- `signal` TEXT (`buy_to_enter|sell_to_enter|hold|close`)
- `confidence` REAL
- `invalidation_condition` TEXT
- `risk_usd` REAL
- `sharpe_at_decision` REAL
- `minutes_elapsed` INTEGER

Поле `parsed_action` (TEXT JSON) сохраняется — туда кладём весь LLM JSON.

### Таблица `positions` (existing)
Добавляем:
- `confidence` REAL
- `invalidation_condition` TEXT
- `risk_usd` REAL

### Новая таблица `equity_snapshots`
```sql
CREATE TABLE IF NOT EXISTS equity_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,                     -- unix sec
  total_equity_usd REAL NOT NULL,
  available_cash_usd REAL NOT NULL,
  total_return_pct REAL NOT NULL,
  sharpe_rolling_14d REAL,
  cycle_no INTEGER
);
CREATE INDEX idx_equity_ts ON equity_snapshots(ts);
```

Заполняется каждый цикл (1 row / 3 мин = 480 rows/сут = ~175K rows/год, размер копеечный).

### Миграция выполняется `ALTER TABLE ADD COLUMN` (sqlite поддерживает,
не ломает старые записи). Если ветка откатывается на main — старая логика
игнорирует новые поля.

---

## 8. Bybit-специфичные адаптации (vs Hyperliquid у Nof1)

| Аспект | Hyperliquid (Nof1) | Bybit (наш клон) |
|---|---|---|
| Цена для prompt | mid-price (avg bid/ask) | `lastPrice` из get_tickers |
| Funding cycle | каждые 1ч | каждые 8ч (00/08/16 UTC) |
| Open Interest API | встроено в snapshot | `get_open_interest(intervalTime=5min, limit=20)` |
| Min lot size | gas-эффективный, fractional | `get_instruments_info.lotSizeFilter.qtyStep` — нужно округление qty |
| Leverage setting | per-position | `set_leverage(symbol, lev)` перед `place_order` |
| Market order | one-shot | `place_order(orderType=Market)` + опц. SL/TP в одном вызове |
| Reduce-only | implicit | `reduceOnly=True` при close |

Все эти моменты уже реализованы в `trading/client.py` (см. §3.1). Добавить
нужно только:

```python
def get_open_interest(self, symbol: str, interval_time: str = "5min", limit: int = 20) -> list[dict]: ...
def get_funding_rate_history(self, symbol: str, limit: int = 8) -> list[dict]: ...
```

Оба метода есть в pybit V5 (`get_open_interest`, `get_funding_rate_history`) —
обёртка тривиальная.

---

## 9. Поэтапный план реализации

| День | Этап | Что делаем |
|---|---|---|
| D0 | Pre-clone | Создать тег `pre-alpha-arena-clone` на текущем main. BUILDLOG запись «v0.12 final baseline» |
| D1 | Branch | `git checkout -b feat/alpha-arena-clone`. Удалить `news/` папку. Удалить старые промпты |
| D2 | API | `trading/client.py` + `get_open_interest`, `get_funding_rate_history`. Unit-тесты с моками |
| D3 | Indicators | `analysis/indicators.py` rewrite: `compute_rsi(period=7|14)`, `compute_macd`, `compute_ema(period)`, `compute_atr(period=3|14)`, `compute_volume_avg`. Unit-тесты на reference series (Wilder smoothing для RSI/ATR) |
| D4 | Sharpe | `analysis/sharpe.py`: rolling 14d Sharpe из `equity_snapshots`. Unit-тесты на известных рядах |
| D5 | Context | `trading/context.py` rewrite: `collect_market_context` (per-symbol parallel), `format_user_prompt` (Nof1 layout). Тесты на снапшотах |
| D6 | Prompts | `llm/prompts.py` rewrite: `SYSTEM_PROMPT` (адаптация из Приложения A), helpers для funding band labels. Regex-тесты что промпт содержит «OLDEST → NEWEST» (×4), все required JSON fields |
| D7 | Executor | `trading/executor.py` rewrite: `parse_action(text) -> ParsedAction`, `validate(action, account, killswitch) -> ValidationResult`, `apply_action`. Unit-тесты на 20+ JSON-кейсов (valid, malformed, R:R fail, killswitch fail, signal=hold, signal=close) |
| D8 | Main loop | `app/main.py` rewrite: single-cycle, без dual-timer, без review-режима. Telegram уведомления на open/close |
| D9 | DB | `state/db.py`: миграция, новая таблица `equity_snapshots`, save_decision сигнатура. Тесты на чтении/записи |
| D10 | LLM client | `llm/client.py`: модель → v4-pro, убрать `thinking={"type":"enabled"}`, добавить `extra_body={"reasoning_effort": "off"}` (или эквивалент через Anthropic API headers — уточнить по docs DeepSeek) |
| D11 | Tests | Integration-тест end-to-end (mock Bybit + mock LLM) на 1 cycle. Полный test-suite green |
| D12 | Dry-run | `AI_TRADER_TRADING_ENABLED=False` на demo Bybit. 24ч логирование decisions без реальных ордеров. Убедиться что промпт валидный, парсер не ломается, latency приемлемая |
| D13 | Shadow live | `AI_TRADER_TRADING_ENABLED=True` на demo Bybit. Старт OOS-наблюдения |
| D14+ | OOS | 14 дней наблюдения. Метрики: WR, PF, Sharpe, max DD, cost/day. Сравнение с baseline v0.12 |

---

## 10. Тестирование

### Unit-тесты (обязательные)
- **Indicators**: RSI(7), RSI(14), MACD, EMA(20), EMA(50), ATR(3), ATR(14), Volume avg —
  на reference сериях (например, [TA-Lib reference values](https://ta-lib.org/d_api/d_api.html#TA_RSI)).
- **Sharpe**: rolling Sharpe на синтетических equity curves (известный ответ).
- **Parser**: 20+ JSON-кейсов:
  - valid buy_to_enter / sell_to_enter / hold / close
  - malformed JSON
  - missing fields
  - signal value out of enum
  - quantity ≤ 0
  - leverage > 5
  - R:R < 1.5
  - risk_usd > $10
  - SL/TP в неправильную сторону
- **Validator + killswitch**: max_positions, daily_loss, total_loss, allowed_symbols.
- **Prompt regex**: «OLDEST → NEWEST» ≥4 раз, все 9 required JSON fields в schema-блоке.

### Integration-тест
- Mock `AiBybitClient` (фиксированные klines, tickers, OI, funding).
- Mock `DeepSeekClient` (фиксированный JSON ответ).
- Прогнать 1 цикл: должен сделать `set_leverage` → `place_order` → запись в БД → Telegram уведомление.

### Regression-тест (на baseline v0.12)
- В ветке хранить snapshot формата старого SYSTEM_PROMPT — для документации
  отличий, не для совместимости.

---

## 11. Риски и митигации

| Риск | Признак | Митигация |
|---|---|---|
| V4-Pro latency > 60 сек на 3-мин цикле | timing-логи каждого этапа | Расширить interval до 5 мин (`AI_TRADER_POLL_INTERVAL_SEC=300`) |
| Стоимость > $3/день при reasoning=off | cost_usd в БД, daily aggregate | Уменьшить max_tokens до 4096, сжать prompt (5 coins вместо 6) |
| Bybit OI rate limit (10 req/sec public) | retCode 10006 | Параллельный сбор с jitter; кэш 30 сек если упрёмся |
| LLM путает signal с coin | invalid action в БД >5% циклов | Reinforce в SYSTEM_PROMPT через few-shot example |
| LLM генерит quantity=0 или nonsense | validator reject >10% циклов | Hard validator + reject (уже в плане) |
| LLM забывает invalidation_condition после рестарта бота | поле NULL в БД | invalidation_condition хранится в БД per-position; перевыдаём в каждом prompt |
| Bybit ставит SL/TP в не ту сторону | `place_order` non-zero retCode | Pre-validate (уже в executor) |
| Модель «галлюцинирует» news (хотя их нет в prompt) | justification ссылается на «catalyst», «headline» | Regex-тест justification ловит forbidden words в build-time + alerting; в SYSTEM_PROMPT прямо сказано «no news» |
| Drawdown >15% за 7 дней | total_loss tracking | Killswitch уже срабатывает на $200 (=40% от $500 virtual). Альтернативно — ручной shutdown |

---

## 12. OOS план и критерии go/no-go

### Метрики, которые сравниваем с baseline v0.12 (после 14 дней shadow)

| Метрика | Формула | Целевая (минимум для go) |
|---|---|---|
| Win Rate | wins / total_closes | ≥ baseline − 5% |
| Profit Factor | sum(wins) / sum(losses) | ≥ 1.2 |
| Expectancy (R) | avg(R per trade) | > 0 |
| Sharpe (rolling 14d) | стандартная формула | ≥ 0.5 |
| Max Drawdown | peak-to-trough | ≤ 20% от $500 |
| Cost / day | сумма cost_usd | ≤ $3 |
| LLM invalid-action rate | invalid / total cycles | ≤ 5% |
| Telegram notification fidelity | sent_msgs / open_close_events | = 100% |

### Решение по итогам 14 дней
- **GO**: merge `feat/alpha-arena-clone` → main, тег `v2.0-alpha-arena-clone`.
- **NO-GO**: остаёмся на v0.12. Документируем что не сработало (model? prompt?
  data? cycle? latency? rate-limits?). Возможно итерируем в ветке или
  переходим на менее радикальный путь из `AI_TRADER_PROPOSAL_DISCRETIONARY.md`.

---

## 13. Откат

- Тег `pre-alpha-arena-clone` на коммите v0.12.
- `git reset --hard pre-alpha-arena-clone` локально + force-push если merge
  уже произошёл (с подтверждением пользователя).
- БД совместима в обе стороны (новые поля nullable, старая логика игнорирует).
- `.env`-флаги через `AI_TRADER_DEEPSEEK_MODEL=deepseek-v4-flash` позволяют
  откатить только модель не трогая код.

---

## 14. Что НЕ делаем в этом proposal

- Не переходим на Hyperliquid (Bybit остаётся).
- Не отключаем killswitch (даже если Nof1 его не имеет — у нас $500, ему критично).
- Не отключаем БД (сохраняем для analytics).
- Не делаем pyramiding / partial close (Nof1 тоже не делает — Season 1 ограничение).
- Не добавляем VWAP / L-S ratio / F&G / orderflow (Nof1 их тоже не даёт).
- Не пробуем `reasoning_effort=high` сразу — сначала точная Nof1-реплика на `off`.
- Не пробуем multi-agent (Analyst/Trader/Risk) — Nof1 single-agent, упростим.

---

## 15. Шаг 2 (после успешного 14-дневного OOS)

| Опция | Эффект | Стоимость |
|---|---|---|
| Включить `reasoning_effort=high` | +12-22% reasoning quality | ×2-3 cost (~$3-5/день) |
| Short-term memory: последние 5 closed decisions в prompt | adaptation to recent regime | +~500 tokens/cycle |
| Multi-agent: Analyst → Trader → Risk | специализация ролей | ×3 cost, +complexity |
| Добавить VWAP / OI delta % / F&G / L-S ratio | Шаг 2 из proposal_discretionary | +API calls |
| Расширить coin universe (5 → 10) | больше opportunities | риск нагрузки на context window |

---

## Приложение A. Полный SYSTEM_PROMPT (черновик)

> На реализации: переписать в финальный вид с подстановкой config-значений
> через `%-formatting` (как у нас сейчас в `prompts.py`).

```
# ROLE & IDENTITY
You are AI Trading Model deepseek-v4-pro, an autonomous cryptocurrency trading
agent operating on Bybit USDT-perp futures (demo account). Your mission:
maximize risk-adjusted return (PnL) through systematic, disciplined trading
across a stateless 3-minute decision cycle.

# TRADING ENVIRONMENT
- Exchange: Bybit, category=linear (USDT-perp)
- Asset universe: BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, DOGEUSDT
- Starting virtual capital: $500
- Cycle: every 3 minutes (mid-to-low frequency trading)
- Leverage: 1x-5x (bot rejects above 5x)
- Funding schedule: 00:00 / 08:00 / 16:00 UTC

# ACTION SPACE — exactly FOUR per decision
1. buy_to_enter  — open new LONG (bullish thesis)
2. sell_to_enter — open new SHORT (bearish thesis)
3. hold          — no change (positions valid, or no edge for new entry)
4. close         — full exit of existing position (no partial closes)

Constraints:
- One position per coin (no pyramiding)
- No hedging (cannot hold long+short same coin)
- No partial exits (close = full)

# CAPITAL SAFETY — bot-enforced HARD limits (bypass impossible)
- Max 3 simultaneous positions
- Max 5x leverage
- Max $10 risk per trade (|entry - stop_loss| * quantity ≤ 10)
- Daily realised loss ≤ $50; total realised loss ≤ $200
- R:R ≥ 1.5 mandatory — if your idea has R:R < 1.5, bot will reject; return HOLD instead

# POSITION SIZING
notional_usd = quantity * current_price
risk_usd     = |entry - stop_loss| * quantity      # do NOT multiply by leverage

Conviction → leverage mapping (guidance, not rule):
  confidence 0.30-0.50 → 1-2x
  confidence 0.50-0.70 → 2-3x
  confidence 0.70-1.00 → 3-5x

# OUTPUT FORMAT — single VALID JSON, last in response
{
  "signal": "buy_to_enter" | "sell_to_enter" | "hold" | "close",
  "coin":   <one of allowed symbols>,
  "quantity": <float, > 0 for entries>,
  "leverage": <integer 1-5>,
  "stop_loss":     <float>,
  "profit_target": <float>,
  "invalidation_condition": "<observable signal that voids your thesis>",
  "confidence": <float, 0.0-1.0>,
  "risk_usd":   <float, ≤ 10>,
  "justification": "<concise reasoning, max 500 chars>"
}

Output rules:
- All numeric fields positive (except when signal=hold, placeholders OK)
- LONG: stop_loss < current_price < profit_target
- SHORT: profit_target < current_price < stop_loss
- justification: concise prose, max 500 chars
- When signal=hold: set quantity=0, leverage=1, placeholders for prices

# DATA INTERPRETATION
You will receive per-coin:
- EMA20: short-trend direction (price > EMA20 = uptrend)
- EMA50: medium-trend (4h timeframe only)
- MACD: momentum (positive = bullish, negative = bearish)
- RSI(7): intraday overbought/oversold; ≤25 extreme oversold, ≥75 extreme overbought
- RSI(14): trend-level; standard 30/70 thresholds
- ATR(3) vs ATR(14): volatility regime — if ATR(3) > ATR(14) × 1.5 = vol expansion
- Volume current vs avg(20): participation
- Open Interest latest vs avg: crowd positioning
  - rising OI + rising price = strong uptrend
  - rising OI + falling price = strong downtrend
  - falling OI = trend weakening
- Funding rate (interpretation bands):
  - |fr| < 0.05%   → neutral
  - 0.05%-0.20%   → mild skew
  - > 0.20%       → strong skew, potential reversal

# DATA ORDERING (CRITICAL)
⚠️ ALL price/indicator arrays are ORDERED: OLDEST → NEWEST
⚠️ The LAST element is the MOST RECENT data point
⚠️ This is repeated in the user prompt — do not confuse the order

# OPERATIONAL CONSTRAINTS — what you DON'T have
- No news, no social media, no narratives — infer everything from price + funding + OI
- No conversation history — each decision is stateless
- No external APIs, no orderbook depth, no limit orders (market only)
- No partial exits, no hedging, no pyramiding

# PHILOSOPHY
- Capital preservation comes first
- Discipline over emotion: follow your invalidation_condition, don't move stops
- Quality over quantity: fewer high-conviction trades beat many low-conviction
- Hold is a valid action — not "safe", but valid when edge is unclear

# SHARPE FEEDBACK
You will receive your rolling 14-day Sharpe in each user prompt.
- Sharpe < 0   → reduce size, tighten stops, be more selective
- Sharpe 0-1   → positive but volatile, refine entries
- Sharpe > 1   → strategy working, maintain discipline
- Sharpe > 2   → excellent, but beware overconfidence (mean reversion in metrics)

# FINAL INSTRUCTIONS
1. Read the user prompt in full before deciding
2. Verify your sizing math: notional, risk_usd, R:R
3. Ensure JSON is valid (single object, all required fields)
4. Provide honest confidence — don't overstate
5. Be consistent with prior invalidation_conditions on open positions

Real money (demo capital but real reasoning). Every decision compounds.
Trade systematically. Manage risk religiously. Let edge compound over time.
```

---

## Приложение B. Полный USER_PROMPT template

```python
USER_PROMPT_TEMPLATE = """
It has been {minutes_elapsed} minutes since you started trading.

⚠️ ALL PRICE/SIGNAL DATA BELOW IS ORDERED: OLDEST → NEWEST ⚠️
Intraday series are at 3-minute intervals unless stated otherwise.

═══════════════════════════════════════════════════════════
CURRENT MARKET STATE
═══════════════════════════════════════════════════════════

{per_symbol_blocks}

═══════════════════════════════════════════════════════════
ACCOUNT & PERFORMANCE
═══════════════════════════════════════════════════════════
Current Total Return: {total_return_pct:+.2f}%
Sharpe Ratio (rolling 14d): {sharpe:.3f}
Available Cash: ${cash:.2f}
Current Account Value: ${equity:.2f}

Open Positions:
{open_positions_json}

⚠️ DATA ORDER: OLDEST → NEWEST ⚠️

Return your trading decision as a single valid JSON object,
per the schema defined in system prompt.
"""

PER_SYMBOL_BLOCK_TEMPLATE = """
### {symbol}
current_price = {price}, current_ema20 = {ema20}, current_macd = {macd}, current_rsi(7) = {rsi7}

Open Interest:  Latest: {oi_latest}  |  Average (20×5min): {oi_avg}
Funding Rate:   Latest: {fr}  (band: {fr_band})

Intraday (3m × 10, oldest→newest):
  Mid prices:    {prices_3m}
  EMA20:         {ema20_3m}
  MACD:          {macd_3m}
  RSI(7):        {rsi7_3m}
  RSI(14):       {rsi14_3m}

Longer-term (4h):
  20-Period EMA: {ema20_4h}  vs.  50-Period EMA: {ema50_4h}
  3-Period ATR:  {atr3_4h}   vs.  14-Period ATR: {atr14_4h}
  Current Volume: {vol_4h}    vs.  Average Volume: {vol_avg_4h}
  MACD (×10):    {macd_4h}
  RSI(14, ×10):  {rsi14_4h}
"""
```

---

## Приложение C. Сравнительная таблица: наш v0.12 → Alpha Arena Clone v2.0

| Аспект | v0.12 (текущий) | v2.0 (Alpha Arena Clone) |
|---|---|---|
| Модель | deepseek-v4-flash | **deepseek-v4-pro** (`reasoning_effort=off`) |
| Цикл | 15min full + 5min review | 3 min single |
| Action space | open/close/hold | buy_to_enter / sell_to_enter / hold / close |
| Output schema | action/symbol/side/leverage/position_size_usd/stop_loss/take_profit/reason | signal/coin/quantity/leverage/SL/TP/invalidation/confidence/risk_usd/justification |
| Прескриптивные exit triggers | 4 trigger'а (PEAK-DRAWDOWN/LOCKED/ADVERSE/MR) | НЕТ — LLM использует pre-registered `invalidation_condition` |
| Sharpe feedback | НЕТ | YES, в каждом prompt |
| OI / Funding band | OI нет, funding raw | OI latest/avg, funding с band labels (neutral/mild/strong) |
| News (RSS) | YES | НЕТ (выбрасываем) |
| Indicators 1H | RSI14, BB(20,2), SMA20, EMA200, ATR14, MACD | НЕТ (заменено 3m) |
| Indicators 4H | RSI14, EMA50, ATR14 | EMA20/50, MACD, RSI14, ATR3/14, Volume avg |
| Indicators intraday | НЕТ | 3m × 10: prices, EMA20, MACD, RSI7, RSI14 |
| LLM thinking | Anthropic thinking blocks (~1500 tokens) | НЕТ (Nof1 пушит CoT через JSON fields) |
| Stoimosть в день | ~$0.10-0.15 | ~$1-2 (за качество reasoning V4-Pro + богатый prompt) |
| Promptный размер | ~10K input tokens | ~5K input tokens (Nof1 компактнее) |
| LLM-критика «галлюцинации» (VWAP/L-S/F&G) | Решена в v0.12 (clean-up) | Полностью исключена архитектурно |

---

## Приложение D. Цитаты пользователя (мотивация)

- «нужно подготовить план перехода на их подход. От нашей страты ничего не
  надо будет сохранять, только код по работе с апи».
- «Модель берем DeepSeek V4-Pro».
- «нужны будут их промпты и все правила с метриками и все что нужно для
  работы ии».

---

## Приложение E. Чек-лист реализации (по дням)

- [ ] D0: тег `pre-alpha-arena-clone` + BUILDLOG_AI_TRADER.md «v0.12 final baseline»
- [ ] D1: ветка `feat/alpha-arena-clone`, удалить `news/`, удалить `prompts.py`/`context.py`/`executor.py`/`indicators.py` (clean slate стратегии)
- [ ] D2: `trading/client.py` + 2 новых метода + тесты
- [ ] D3: `analysis/indicators.py` rewrite + тесты на reference series
- [ ] D4: `analysis/sharpe.py` + тесты
- [ ] D5: `trading/context.py` + тесты
- [ ] D6: `llm/prompts.py` + regex-тесты
- [ ] D7: `trading/executor.py` + 20+ тестов на JSON-кейсах
- [ ] D8: `app/main.py` single-cycle
- [ ] D9: `state/db.py` миграция + `equity_snapshots`
- [ ] D10: `llm/client.py` модель → v4-pro
- [ ] D11: integration-тест end-to-end
- [ ] D12: dry-run demo (trading_enabled=False, 24ч)
- [ ] D13: shadow live demo (trading_enabled=True)
- [ ] D14+: 14 дней OOS наблюдения + сравнение метрик с baseline v0.12
- [ ] D28: go/no-go решение — merge или revert на `pre-alpha-arena-clone`
