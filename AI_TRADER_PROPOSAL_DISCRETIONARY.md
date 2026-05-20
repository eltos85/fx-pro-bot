# Proposal: AI-Trader — Roadmap расширения market context (после v0.13)

> **Статус:** DRAFT / PENDING APPROVAL. Не реализовано.
> **Дата создания:** 2026-05-18 (после деплоя v0.13 Nof1-style meta-cognition).
> **Реализация:** не раньше 2026-05-19. Сегодня — наблюдение за v0.13 baseline.
> **Запрос пользователя:** «что из {Data Layer / Analysis Layer / Decision /
> Execution / Monitoring / Agents} можно добавить нашему текущему боту».

---

## 1. Цель

Расширить **Data Layer** `ai_trader`-а несколькими бесплатными Bybit-сигналами,
чтобы у LLM появился больший набор реальных микро-структурных и
позиционных входов. Это **расширение контекста**, а не изменение
стратегии — все наши триггеры (PEAK-DRAWDOWN, LOCKED-PROFIT,
ADVERSE-NEW-EVIDENCE, SETUP INVALIDATION) и dual-timer остаются как есть.

Параллельно — закрыть Этап 2 плана v0.13 (self-feedback по calibration
данным confidence ↔ realised PnL).

## 2. Принципы

- **Без изменения стратегии.** Старые правила и триггеры на месте.
  Новые сигналы — это пища для CoT LLM, не новые механические
  правила входа/выхода.
- **Бесплатные источники.** Только Bybit endpoint'ы, никаких
  Glassnode/Twitter/Nansen ($30-300/мес — не оправдано на $500 demo).
- **Lesson learned (v0.4–v0.5 rollback i1-i6, 2026-05-11):** если
  сигнал упоминается в промпте, он ДОЛЖЕН реально передаваться в
  market context, иначе LLM начнёт галлюцинировать значения. Каждая
  новая фича = (1) data fetch + (2) формат в `format_context_for_*`
  + (3) упоминание в промпте + (4) unit-тест что данные действительно
  доходят. Иначе не мержим.
- **Sample-size правило** (`.cursor/rules/sample-size.mdc`): tune /
  rollback фичи разрешено только после ≥100 сделок или ≥2 недель
  данных и p-value < 0.05. До этого — наблюдаем, не подкручиваем.
- **No-data-fitting** (`.cursor/rules/no-data-fitting.mdc`): пороги
  интерпретации сигналов (что считать «extreme L/S ratio»,
  «persistent funding bias») — из канонической литературы / Bybit
  research, не из интуиции и не подгонкой под последние N сделок.
- **Каждая фича = отдельный коммит** + отдельная неделя наблюдения,
  чтобы можно было точно атрибутировать impact и при необходимости
  атомарно откатить (revert конкретного коммита).

## 3. Предложения по приоритету ROI

### Step 1: Long/Short ratio (Bybit `get_long_short_ratio`)

**Что добавляем:** retail-positioning ratio (`buyRatio`/`sellRatio`)
для каждого символа в полном цикле.

**Источник:** Bybit V5 endpoint `/v5/market/account-ratio`. Free.
Параметры: `category=linear`, `symbol`, `period=4h` (≈ наш 4H
horizon). Возвращает `buyRatio` (0–1) и `sellRatio` (0–1).

**Каноническая интерпретация (Bybit Research 2024 + Coinglass 2026):**
- `buyRatio ≥ 0.65` AND price падает 24h → классический **long-squeeze
  setup**, потенциал contrarian-short если есть второй сигнал.
- `buyRatio ≤ 0.35` AND price растёт 24h → **short-squeeze setup**,
  contrarian-long потенциал.
- `0.40–0.60` — нейтральная зона, не интерпретируем.

**Формат в контексте:**
`SYMBOL: L/S ratio 4h = 0.71 [retail long extreme — contrarian short risk]`

**Где интерпретация записана:** в `SYSTEM_PROMPT` появится 2-3 строки
«How to use L/S ratio» (с явными порогами), и в `EXIT MANAGEMENT
trigger 3 ADVERSE NEW EVIDENCE` — добавим один bullet «L/S ratio
flipped against position from extreme zone».

**Почему НЕ механический триггер:** мы уже откачивали i1-i6 в
2026-05-11 из-за галлюцинаций. На этот раз — только context-add для
LLM CoT, без обязательной интерпретации.

**Объём:**
- `src/ai_trader/trading/client.py`: метод `get_long_short_ratio(symbol, period)`.
- `src/ai_trader/trading/context.py`: дотягиваем в `collect_market_context`
  (только full cycle, не review — review-цикл lite).
- `src/ai_trader/llm/prompts.py`: 3-5 строк описания в `WHAT YOU SEE` +
  `MARKET CONTEXT` секциях.
- Тесты: 4-6 шт. (data parsing / extreme labels / включение в context /
  отсутствие галлюцинаций когда API вернул None).

**Время:** 2-3 часа.

**Риск:** **средний.** Прецедент отката i1-i6 → mitigation = (a) явный
test что данные передаются в промпт, (b) graceful degrade если API
endpoint молчит (label `[L/S unavailable]`, не упоминать в выводах).

---

### Step 2: Расширенная funding history (Bybit `get_funding_rate_history`)

**Что добавляем:** последние 8 funding periods (Bybit funding каждые
8 часов = ~2.5 суток истории) для каждого символа.

**Источник:** Bybit V5 `/v5/market/funding/history`. Free.
Параметры: `category=linear`, `symbol`, `limit=8`.

**Каноническая интерпретация** (Lambda Finance 2026 funding bands,
уже есть в нашем v0.3 промпте):
- Текущее значение — как сейчас.
- НОВОЕ: **persistent bias** — «funding был positive (≥0.01%) в
  N из 8 периодов». N ≥ 6 = «sustained bullish positioning» →
  накопленный squeeze risk (Bybit Research 2024, BBX Trade Mgmt 2026).

**Формат:**
`SYMBOL: funding=+0.012% [mild lean: longs paying] | history 8p: +6 / -1 / 0=1 → persistent positive bias`

**Зачем:** одиночное значение funding = слабый signal; **persistent**
bias на 6/8 периодов — это уже накопленная фьючерсная очередь, которая
исторически коррелирует со squeeze risk на дистанции 24-48 часов.

**Объём:**
- `client.py`: метод `get_funding_history(symbol, limit=8)`.
- `context.py`: один формат-блок в snapshot.
- `prompts.py`: расширение `MARKET CONTEXT → Funding rate framework`
  абзаца, 4-5 строк про persistent bias.
- Тесты: 3-4 шт. (history fetch / persistent label / graceful
  degrade).

**Время:** 1-2 часа.

**Риск:** низкий. Funding rate уже есть в контексте, добавляем только
расширенную history. Не вводим новых триггеров.

---

### Step 3: Calibration self-feedback (Этап 2 плана v0.13)

**Что добавляем:** агрегированная статистика последних 10-20
закрытых сделок в user_prompt — «свой track record» для LLM.

**Источник:** локальная БД `positions` (уже есть с v0.13 поля
`confidence`, `invalidation_condition`).

**Формат:**
```
YOUR RECENT PERFORMANCE (last 20 closed trades):
- Win rate: 60% (12W / 8L)
- Avg confidence on wins: 0.68
- Avg confidence on losses: 0.61
- Best invalidation cite rate: 70% (14/20 closed for invalidation, not SL/TP)
- Realised PnL: +$12.40 (avg R = +0.31 per trade)
```

**Зачем:** Nof1 называет это **cumulative metrics in prompt** — LLM
видит свой реальный track record и начинает корректировать
overconfidence. Mechanically free, 1 SQL query на цикл.

**Когда включать:** после **≥10 закрытых сделок с v0.13** (т.е. после
наблюдения, когда есть данные). До этого блок выглядел бы как
`(insufficient data — collecting)`.

**Объём:**
- `state/db.py`: метод `get_recent_performance_stats(limit=20)`.
- `trading/context.py`: вставка блока в `collect_market_context`.
- `prompts.py`: упоминание блока в `WHAT YOU SEE EACH CYCLE` +
  один абзац как использовать («если ваш avg confidence на losses
  > 0.65, вы overconfident — снижайте по умолчанию»).
- Тесты: 4-5 шт. (SQL aggregation / format / graceful degrade с
  малой выборкой / round-trip с v0.13 полями).

**Время:** 2-3 часа.

**Риск:** низкий. БД-only, без новых API вызовов.

**Sample-size caveat:** статистика на ≥10 сделок ≠ статистически
значимая. Это **psychological anchor** для LLM, не statistical truth.
Промпт должен это явно признавать (Nof1 формулировка: «for
self-reflection, not statistical inference»).

---

### Step 4 (опционально): Orderbook bid-ask imbalance (Bybit `get_orderbook`)

**Что добавляем:** top-5 уровней bid/ask depth + spread в bps.

**Источник:** Bybit V5 `/v5/market/orderbook`. Free.
Параметры: `category=linear`, `symbol`, `limit=25`. Суммируем
объём топ-5 bids и топ-5 asks, считаем imbalance = `(B-A)/(B+A)`.

**Каноническая интерпретация:**
- `imbalance > +0.4` = bids dominate → краткосрочный buy pressure.
- `imbalance < -0.4` = asks dominate → краткосрочный sell pressure.
- Spread > 5 bps = тонкая ликвидность, осторожнее с size.

**Формат:**
`SYMBOL: book imbalance top-5 = +0.34 [bids dominate], spread = 1.2 bps`

**Зачем:** микро-структурный signal на масштабе минут — комплементарен
1H/4H барам.

**Caveat:** intraday signal, актуален на минутах, не часах. Наш цикл
5-15 мин — на границе полезности. Поэтому это step **4 (опционально)**:
включаем только если шаги 1-3 показали измеримый lift, иначе
overhead не оправдан.

**Время:** 2 часа.

**Риск:** средний. Микроструктурные данные шумные, можно
галлюцинировать «sell pressure» из 1-discrepancy snapshot.
Mitigation = брать **3 последовательных снапшота** с интервалом 30с
для стабильности, либо вообще откладывать до step 5.

## 4. Что НЕ предлагается и почему

| Идея | Почему НЕТ |
|---|---|
| On-chain (Glassnode/Nansen) | $30-300/мес. Polluted для 15-минутного intraday. ROI на $500 demo ≈ 0 |
| Twitter/X sentiment | Twitter API ≥ $100/мес. RSS news уже даёт narrative |
| CCXT вместо pybit | Рефакторинг ради рефакторинга. Регресс-риск без upside |
| WebSocket вместо REST | HFT-инструмент. На 5-15 мин циклах REST достаточен |
| Multi-agent (Analyst→Risk→Executor) | Overengineering. Nof1 побеждает в Alpha Arena одним LLM. 3-5× API cost за маржинальный gain |
| FreqAI / классический ML | **Другая парадигма** (replace LLM). Это отдельный бот, не правка `ai_trader` |
| Retraining LLM | LLM = foundation model, не дотренировывается user'ом. Эквивалент = step 3 (calibration self-feedback) |
| Дополнительные пары (SOL/ADA/AVAX...) | Allowed pairs (BTC/ETH/BNB/XRP/DOGE) фиксированы стратегией. Расширять — отдельное решение со sample-size validation |
| Liquidation heatmap / OI delta | Возможно в будущем, но Bybit `get_open_interest` уже один раз провалился (i1-i6 rollback). Отложить до пост-Step 3 |

## 5. Implementation order и timeline

| Шаг | Когда | Условие старта | Длительность | Окно наблюдения |
|---|---|---|---|---|
| **0. Наблюдение v0.13 baseline** | now → +7 дней | v0.13 deployed | 7 дней | ≥10 закрытых сделок с confidence/inv/risk_usd |
| 1. L/S ratio | после step 0 | sample достаточен | 0.5 дня | 7 дней |
| 2. Funding history | после step 1 | step 1 не сломал ничего | 0.5 дня | 7 дней |
| 3. Calibration feedback | после step 2 | ≥20 closed trades | 0.5-1 день | 14 дней |
| 4. Orderbook (опц.) | после step 3 | steps 1-3 дали измеримый lift | 0.5 дня | 14 дней |

**Total elapsed:** ~5-6 недель (1 шаг + 1 неделя наблюдения = ~неделя
на итерацию). Это features-velocity ниже чем «всё за один спринт», но
именно так выглядит data-driven development без curve-fitting.

## 6. Acceptance criteria (per step)

Каждый шаг считается **готовым к мержу** только когда:

1. Pytest зелёный (873/873 + новые тесты на этот шаг).
2. Unit-тест на «данные реально передаются в промпт» (защита от
   i1-i6 рецидива).
3. Запись в `BUILDLOG_AI_TRADER.md` с (a) источником интерпретации,
   (b) формулировкой в промпте, (c) запросом deploy и (d) первой
   проверкой контейнера.
4. Селективный rebuild только `ai-trader` контейнера
   (`docker compose up -d --no-deps --build ai-trader`) —
   не трогаем bybit-bot / advisor / ai-arena (правило
   `deploy-vps.mdc`).
5. Telegram-уведомление о первом цикле с новым сигналом для
   verification.

Каждый шаг считается **успешным** если:

1. После недели наблюдения нет регрессий по WR/avg-R.
2. LLM в commentary явно цитирует новый сигнал хотя бы раз (значит
   данные доходят, не галлюцинируются).
3. Нет stuck-states или прерываний цикла из-за новых API вызовов.
4. p-value сравнения PnL до/после > 0.05 — НЕ значит фича плохая,
   значит выборки малой; продолжаем.

## 7. Risk mitigation (lessons from v0.4-v0.5 rollback)

В мае 2026 мы откатили i1-i6 (OI / F&G / L-S / liquidations / DVOL)
потому что:
- Сигналы упоминались в промпте, но **не** передавались в
  `format_context_for_prompt` → LLM галлюцинировал.
- Слишком много новых полей одним коммитом → невозможно
  атрибутировать impact.

На этот раз:

- **Один сигнал = один коммит.** Atomic-rollback возможен.
- **Test-first:** unit-тест проверяет что строка с новым сигналом
  присутствует в `format_context_for_prompt` output до того как
  упомянуть это в `SYSTEM_PROMPT`.
- **Graceful degrade:** если Bybit endpoint вернул `None` —
  label `[L/S unavailable]`, и в промпте disclaimer «если сигнал
  отсутствует в контексте — не используй».
- **Sample-size честно:** делаем не «хочется быстрее», а «как надо
  по правилу».

## 8. Out of scope (не делаем в рамках этого proposal)

- Multi-agent split (Analyst / Risk / Executor) — отдельный proposal
  если когда-нибудь понадобится.
- Замена DeepSeek-V4-flash на V4-Pro — параллельный эксперимент
  через `ai_arena`, не трогаем `ai_trader` модель.
- Real-money переход — отдельный proposal `MIGRATION_AI_TRADER_TO_REAL_MONEY.md`
  (которого пока нет; аналог для `ai_arena` уже существует —
  `MIGRATION_AI_ARENA_TO_REAL_MONEY.md`).
- Изменение allowed pairs или risk cap 2% — стратегические решения,
  не data layer.

## 9. Связанные документы

- `BUILDLOG_AI_TRADER.md` — история изменений, включая 2026-05-18 v0.13
  и предыдущий rollback i1-i6.
- `AI_TRADER_PROPOSAL_ALPHA_ARENA.md` — план полного перехода на
  Nof1-clone (другой бот, не пересекается).
- `.cursor/rules/sample-size.mdc` — пороги для tune/rollback.
- `.cursor/rules/no-data-fitting.mdc` — research как источник
  правды для параметров.
- `.cursor/rules/strategy-guard.mdc` — какие изменения требуют
  research-ссылки vs simple bug-fix.
- `.cursor/rules/deploy-vps.mdc` — селективный rebuild для
  изоляции от других ботов.
