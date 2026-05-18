# BUILDLOG — AI-Trader (DeepSeek-V4)

## 2026-05-18 — v0.13 Stage 1: Nof1-style meta-cognition fields (`confidence` / `invalidation_condition` / `risk_usd`)

**Запрос пользователя:** «Вариант B» (после обсуждения как усилить
`ai_trader` инсайтами из `ai_arena` (Nof1 clone), сохранив текущую стратегию
бота). Backport дисциплины мышления из Nof1 Alpha Arena в JSON-схему и
prompt'ы — БЕЗ изменения наших триггеров (PEAK-DRAWDOWN, LOCKED-PROFIT,
ADVERSE-NEW-EVIDENCE), без трогания NEWS, dual-timer, KillSwitch.

Источники research:
- Nof1 Alpha Arena prompt gist:
  https://gist.github.com/wquguru/7d268099b8c04b7e5b6ad6fae922ae83
  (§ Output Schema, § Trading Philosophy, § Risk Management Protocol)
- Nof1 TechPost1: https://nof1.ai/blog/TechPost1
  (раздел про meta-cognition + pre-registered invalidation +
  confidence calibration as anti-overconfidence guard)
- AI_TRADER_PROPOSAL_ALPHA_ARENA.md (внутренний документ, разработан
  при планировании `ai_arena`).

Это **не** изменение стратегии: правила входа/выхода, пороги, R:R, EXIT
MANAGEMENT — без изменений. Меняется только дисциплина self-reporting:
LLM теперь обязан явно посчитать (а не «прикинуть») три метрики на
каждом open-action. Это раннее поймание багов «думал риск $5, реально
$15» и регрессия на overconfidence.

### Что добавлено

**1. JSON schema для `action="open"` — три обязательных поля:**

| Поле | Тип | Диапазон | Что означает |
|---|---|---|---|
| `confidence` | float | `[0.0, 1.0]` | Самооценка уверенности |
| `invalidation_condition` | string | non-empty, ≤500 chars | Pre-registered observable exit-сигнал |
| `risk_usd` | float | `(0, 10]` | Самопросчёт `\|entry-SL\|*qty` (cap = 2% of $500) |

Парсер `parse_action` в `executor.py` отвергает open-JSON если хоть одно
поле отсутствует, неверного типа или вне диапазона. Это hard-guard:
LLM невозможно «забыть» новые поля без явного rejection.

**2. SYSTEM_PROMPT — три новые секции:**

- **CONFIDENCE CALIBRATION**: бэнды 0.30-0.49 (low) / 0.50-0.69 (medium) /
  0.70-1.00 (high) с описанием когда использовать каждый. Замена
  «эмоциональному» рейтингу.
- **PRE-REGISTERED INVALIDATION**: требование сформулировать
  observable signal (price level / indicator value / funding band)
  ДО commit'а ордера, плюс примеры. Это additional exit-trigger
  (не замена) к нашим mechanical triggers 1-4.
- **COMMON PITFALLS**: канонический список из Nof1 Risk Management
  Protocol (Overtrading / Revenge Trading / Analysis Paralysis /
  Ignoring Correlation / Overleveraging). Чёрный список ошибок,
  которые статистически дороже всего стоят retail-трейдерам.

Также добавлены:
- **RISK_USD self-check** мини-секция с формулой `|entry-SL|*qty`.
- В **ANALYSIS APPROACH** добавлен 7-й шаг **PRE-COMMIT CHECK**
  (только для open) — обязывает проговорить confidence band и
  invalidation condition в commentary до JSON.
- В **CRITICAL CONSTRAINTS** прописана обязательность 3 полей с
  диапазонами — explicit за-fail-safe для LLM.

**3. БД миграция (идемпотентная):**

- `ALTER TABLE positions ADD COLUMN confidence REAL`
- `ALTER TABLE positions ADD COLUMN invalidation_condition TEXT`
- `ALTER TABLE positions ADD COLUMN risk_usd_declared REAL`

Миграция в `AiTraderStore._migrate()` через `PRAGMA table_info` — на
существующей VPS-БД ALTER TABLE отрабатывает только для отсутствующих
колонок. Старые позиции получают `NULL` в новых полях (тест
`test_migration_old_db_without_columns_is_upgraded`).

`AiPosition` dataclass расширен 3 nullable полями. `open_position()`
принимает их как опциональные kwargs (default `None`) для backward
compat — реальный fail-safe это парсер, БД остаётся permissive.

**4. Telegram summary:**

`OPEN ...` сообщение теперь включает `conf=0.65 risk_decl=$6.50
inv="..."` (первые 80 символов invalidation). Видимый трекинг для
оператора без захода в БД.

**5. SYSTEM_PROMPT_REVIEW не трогается:**

Review-цикл выдаёт только close|hold, новые поля у этих action не
обязательны. Использование `invalidation_condition` для семантического
exit-trigger в review-цикле — Этап 2 (через неделю наблюдений по
calibration данным confidence ↔ realised PnL).

### Что НЕ трогалось

- Триггеры EXIT MANAGEMENT 1-4 (SETUP INVALIDATION / LOCKED-PROFIT /
  ADVERSE NEW EVIDENCE / PEAK-DRAWDOWN) — все на месте, формулировки
  без изменений.
- Dual-timer (full + review cycle).
- RSS news provider.
- KillSwitch ($50 daily / $200 total / 3 max positions / 5x leverage).
- ATR/RSI/MACD/EMA/BB context — те же индикаторы, те же periods.
- Allowed pairs (BTC/ETH/BNB/XRP/DOGE).
- Risk cap 2% per trade ($10).

### Файлы

- `src/ai_trader/state/db.py`: `AiPosition` dataclass +3 nullable поля,
  `_migrate()` идемпотентная миграция, `open_position()` принимает
  3 новых опциональных kwargs.
- `src/ai_trader/trading/executor.py`: `parse_action` валидирует
  `confidence` / `invalidation_condition` / `risk_usd` для open;
  `_apply_open` извлекает поля, передаёт в `store.open_position()`,
  включает в `summary` для Telegram.
- `src/ai_trader/llm/prompts.py`: docstring v0.13 запись;
  CONFIDENCE CALIBRATION + PRE-REGISTERED INVALIDATION + RISK_USD
  self-check + COMMON PITFALLS секции; обновлены JSON schema, ANALYSIS
  APPROACH (7-й шаг PRE-COMMIT CHECK), CRITICAL CONSTRAINTS,
  `build_user_prompt`.
- `tests/test_ai_trader.py`: +30 тестов в трёх классах
  (`TestOpenSchemaV13Required`, `TestOpenSchemaV13PromptGuidance`,
  `TestOpenSchemaV13DBRoundTrip`); существующие positive open-тесты
  обновлены добавлением 3 полей.
  Локально: 873/873 PASS.

### План Этапа 2 (через неделю наблюдений)

1. Сбор статистики calibration: для каждого закрытого trade'а
   correlate `confidence_at_open` с `realised_pnl_r` — overconfidence
   sanity check.
2. Использование `invalidation_condition` в review-цикле как
   semantic exit-trigger: показываем условие LLM каждый review,
   если LLM ответил «invalidation tripped: ... » → close.
3. (опционально) `equity_snapshots` table + Sharpe в промпт для
   feedback loop как в Nof1.

После недели — решение по Этапу 2 на основе фактических данных.

**Запрос пользователя:** «нужно изучить нашего бота и всю его логику, у меня
есть ощущение что где то есть противоречия которые вводят бота в заблуждения.
все найденое должно быть подкреплено современными данными 2026 из крипто-
валютных источников». После полного аудита подтверждены три бага (НЕ
изменения стратегии, поэтому делаем без sample-size collection, см.
`strategy-guard.mdc` секция «Допустимые быстрые правки»).

### Bug 1: incomplete-bar look-ahead bias

Bybit `get_klines` возвращает все бары **включая текущий незакрытый**.
Эти бары шли напрямую в `compute_snapshot` → RSI/MACD/BB пересчитывались
по open-бару каждый цикл, давая flickering signals и tick-by-tick drift.
Подтверждено на XRPUSDT id=58: decision @ 14:31:48 UTC, в промпте
`last close=1.432` — это open 14:00-бара, а не closed 13:00-бара
(C=1.4336).

- `src/ai_trader/trading/context.py`: новый helper
  `_drop_incomplete_bar(bars, interval_minutes)`. Бар считается
  незакрытым если `ts (start) + interval_ms > now_ms`. Применяется в
  `collect_market_context` (1H и 4H) и `collect_review_context` (1H).
  Ticker.last_price (live цена) остаётся как есть — она нужна для
  current_pnl_r.
- Источник 2026: Freqtrade Look-Ahead Analysis docs, StratBase
  «Look-Ahead Bias: The Hidden Backtest Killer» 2026.

### Bug 2: промпт ссылается на данные, которых нет в контексте

Промпт говорил LLM использовать VWAP / Fear & Greed / retail L/S ratio /
OI delta / liquidation cascade / DVOL — эти сигналы в
`format_context_for_prompt` / `format_context_for_review` НЕ передаются
(v0.3-база без i1–i6). Эффект: LLM либо игнорирует половину инструкций,
либо галлюцинирует значения. Конкретный кейс из истории: «retail
extreme» в reasoning при отсутствии данных о retail positioning.

- `src/ai_trader/llm/prompts.py` SYSTEM_PROMPT:
  - Trigger 1 mean-reversion exit: «return to VWAP region» → «return to
    BB middle band (SMA20)». SMA20 = BB middle, реально передаётся в
    `format_snapshot`, концептуально эквивалентен mean-rev target.
  - Trigger 3 ADVERSE NEW EVIDENCE: удалены пункты про OI extreme
    buildup и liquidation cascade. Funding-flip переписан на видимые
    funding band labels ([NEUTRAL] / [mild lean] / [STRONG]).
    Добавлен «1H RSI cross out of extreme zone» как замена liquidation.
  - **Trigger 4 MACRO REGIME SHIFT (F&G) удалён целиком** — F&G нет в
    контексте. PEAK-DRAWDOWN стал trigger 4 (был 5).
- SYSTEM_PROMPT_REVIEW: переписан раздел WHAT YOU SEE под реальный
  v0.3-контекст (RSI/MACD/ATR/EMA/BB, funding label, last 6 closes,
  peak/current_r). Триггеры пересмотрены под доступные сигналы.
  Добавлен disclaimer: «if a trigger references a signal you do NOT see
  in your current context, that trigger is not actionable — fall
  through or HOLD».
- ANALYSIS COMMENTARY cite-list: 1/2/3/4/5 → 1/2/3/4.
- `build_user_prompt_review`: hint обновлён под новые описания триггеров.

### Bug 3: «RSI extreme» не имеет числового определения

Промпт: «Counter-trend ONLY at strong reversal evidence (RSI extreme +
BB band touch + news catalyst)». Формат-лейбл `[OVERSOLD]` ставился при
RSI≤30, без отличия «extreme» от обычного «oversold». На XRPUSDT id=58
LLM решил что RSI=32.8 — это «oversold» и зашёл counter-trend.

- `src/ai_trader/analysis/indicators.py` `format_snapshot`: добавлены
  лейблы `[EXTREME OVERSOLD]` (RSI≤25) и `[EXTREME OVERBOUGHT]` (RSI≥75)
  поверх существующих `[OVERSOLD]` (26–30) и `[OVERBOUGHT]` (70–74).
- `src/ai_trader/llm/prompts.py` SYSTEM_PROMPT trading rules:
  переписаны counter-trend правила явно: counter-trend long требует
  RSI≤25 (`[EXTREME OVERSOLD]`), counter-trend short — RSI≥75
  (`[EXTREME OVERBOUGHT]`). Plain `[OVERSOLD]` (26–30) или
  `[OVERBOUGHT]` (70–74) НЕ достаточно для counter-trend — это
  нормальные значения в трендовом режиме. Для trend-aligned входов
  пороги 30/70 остаются.
- Источники 2026:
  - Apptrading «How to Use RSI for Crypto Trading in 2026»: dynamic
    thresholds, Bear regime 60/20–25.
  - Tapbit «RSI Indicator Crypto Trading 2026»: «in bear markets,
    20–25 often signals capitulation bottoms rather than 30».

### Тесты

`tests/test_ai_trader.py` (+24 теста):
- `TestDropIncompleteBar` × 5: empty list / partial 1H / closed 1H /
  partial 4H / только last bar проверяется.
- `TestPromptsCleanupNoMissingSignals` × 3: regex-check что
  SYSTEM_PROMPT и SYSTEM_PROMPT_REVIEW не содержат «VWAP», «F&G»,
  «DVOL», «OI extreme/delta», «liquidation cascade», «buy_ratio»,
  «Long/Short ratio»; что trigger 1 использует «BB middle» и
  «MACRO REGIME SHIFT» удалён.
- `TestRsiExtremeThreshold` × 7: RSI 24 → [EXTREME OVERSOLD]; 28 →
  [OVERSOLD]; 32.8 → no label (regression for XRPUSDT id=58);
  72 → [OVERBOUGHT]; 76 → [EXTREME OVERBOUGHT]; 50 → no label;
  SYSTEM_PROMPT содержит «RSI <= 25» / «RSI >= 75».

`tests/test_ai_trader_indicators.py`: обновлён один тест
(RSI=100 теперь даёт `[EXTREME OVERBOUGHT]` вместо `[OVERBOUGHT]`).

**Локально:** 540/540 passed (без regression в bybit_bot, fx_pro_bot,
fx_ai_trader).

**Что НЕ менялось** (намеренно, требует обсуждения с пользователем
и/или sample-size collection):
- Trigger 2 vs Trigger 4 порядковая аномалия (peak 1.5R требует
  invalidation, peak 0.8R — mechanical) — внутреннее противоречие,
  не bug в классическом смысле.
- Regime-aware RSI thresholds (Bull 80/40, Bear 60/20–25, Range 70/30) —
  это strategy change, требует согласования.
- 200-SMA trend filter для mean-reversion entries — strategy change.
- Funding rate bands пересмотр (Lambda 8h vs industry per-day) — discuss.
- Orderflow / CVD / DOM heatmap — большой проект.

**Файлы:** `src/ai_trader/trading/context.py`,
`src/ai_trader/llm/prompts.py`, `src/ai_trader/analysis/indicators.py`,
`tests/test_ai_trader.py`, `tests/test_ai_trader_indicators.py`,
`BUILDLOG_AI_TRADER.md`.

---

## 2026-05-12 — v0.11-backport: PEAK-DRAWDOWN trigger (lock-in of decayed peak profit)

**Запрос пользователя:** «изучи последний лот по эфиру, ИИ опять не зафиксировал
прибыль, хотя она была». Анализ ETHUSDT id=56: позиция держалась 27 циклов,
peak_pnl ≈ +0.99R (≈ +$8.96 в cycle 16), exit по SL = −$1.52. LOCKED-PROFIT
GUARD (1.5R) — слишком высокий порог, не сработал. Нужен новый триггер для
случая «была прибыль 0.8–1.0R, drawdown к 0–0.5R, движение выдохлось».

**Дизайн (hybrid).** Код считает high-water mark; LLM применяет правило через
prompt. Без миграции БД.

- `src/ai_trader/trading/context.py`:
  - Новая функция `_compute_position_r_stats(position, bars_1h, current_price)`
    возвращает `(peak_pnl_r, current_pnl_r)`. peak считается из high/low 1H
    свечей с момента `position.opened_at`; для Buy peak = (max(high) − entry)
    / risk_dist, для Sell — (entry − min(low)) / risk_dist. risk_dist =
    |entry − SL|. Edge-cases: SL/entry отсутствуют → (None, None);
    бары пусты → peak fallback = current; safety-инвариант peak ≥ current.
  - `format_context_for_prompt` и `format_context_for_review` теперь после
    строки позиции выводят `     peak_pnl_r=+X.YYR current_pnl_r=+Z.WWR`.

- `src/ai_trader/llm/prompts.py`:
  - `SYSTEM_PROMPT` (full): добавлен **trigger 5 PEAK-DRAWDOWN** в блок
    EXIT MANAGEMENT. Условие: `peak_pnl_r >= 0.8R` AND `current_pnl_r <= 0.45R`
    → close. Mechanical: срабатывает даже если original setup технически
    intact — peak→drawdown сам по себе является доказательством. В блок
    «Compute R-units» добавлено явное указание читать peak/current прямо
    из строки позиции, а не пересчитывать. ANALYSIS COMMENTARY cite
    обновлён на «trigger (1/2/3/4/5)».
  - `SYSTEM_PROMPT_REVIEW` (review): добавлен **trigger 4 PEAK-DRAWDOWN**
    с теми же порогами. WHAT YOU SEE дополнен описанием peak/current.
  - `build_user_prompt_review`: обновлён hint на 4 close-trigger'а.

- `tests/test_ai_trader.py`: +11 тестов
  (`TestPeakPnlRStats` × 9, `TestPeakDrawdownTriggerInPrompts` × 2):
  buy/sell расчёт peak, SL=None → None, risk_dist=0 → None, no-bars fallback,
  safety-инвариант peak≥current, фильтр баров до opened_at, format-output
  содержит peak/current, system-prompts содержат «PEAK-DRAWDOWN», «0.8R»,
  «0.45R», «peak_pnl_r», «current_pnl_r», «1/2/3/4/5».

**Какие данные используются (v0.3-контекст):** только `bars_1h` (есть у нас
в SymbolSnapshot и для full, и для review циклов) + ticker.last_price.
Никаких новых API-вызовов, никаких миграций БД. peak пересчитывается на
каждом цикле — это OK, потому что 1H high/low за окно открытой позиции
монотонны: peak только растёт с временем.

**Обоснование порогов 0.8R / 0.45R (из анализа ETHUSDT id=56):**
peak был ≈ 0.99R в cycle 16, к cycle 27 откатился < 0R и закрылся по SL.
- Триггер на peak ≥ 1.0R пропустил бы id=56 (peak = 0.99R, чуть ниже).
- Триггер на peak ≥ 0.8R с current ≤ 0.45R — поймал бы id=56 примерно
  на cycle 18–20 (peak зафиксирован 0.99R, current скатился ниже 0.45R),
  фиксация ≈ +0.45R × $9.47 risk ≈ +$4.26 вместо текущего −$1.52.
- Threshold 0.45R = «половина 0.9R bucket» — даёт буфер от шума 1H баров
  и одновременно достаточно агрессивен чтобы не упустить decay.

**Тесты:** локально 525/525 passed (49 ai_trader-specific + остальные).

**Файлы:** `src/ai_trader/trading/context.py`, `src/ai_trader/llm/prompts.py`,
`tests/test_ai_trader.py`, `BUILDLOG_AI_TRADER.md`.

---

## 2026-05-12 — backport: dual-timer 15+5 мин (v0.10 → v0.3 база)

**Запрос пользователя:** «надо вернуть функционал слежения за лотом
обновления событий раз в 5 минут».

**Что сделано.** Cherry-pick коммита `c6084b9 feat(v0.10): двойной таймер`
из ветки `backup-pre-rollback-20260511-172246`. Конфликты в 4 файлах
решены вручную под v0.3-базу (без i1–i6, без MacroProvider/OptionsIv):

- `src/ai_trader/app/main.py`: dual-timer структура взята из v0.10
  (один `cycle` счётчик, два `time.monotonic()` таймера для full/review),
  но вызов `_run_cycle` без `macro_provider`/`options_iv_provider`
  параметров. Imports — добавлены `SYSTEM_PROMPT_REVIEW`,
  `build_system_prompt_review`, `build_user_prompt_review`.
- `src/ai_trader/trading/context.py`: `_funding_band_label` сохранён;
  `collect_review_context` упрощён — без `positioning` (нет OI/L-S/funding-
  history полей в v0.3 SymbolSnapshot), `compute_snapshot` вызывается без
  VWAP/RV параметров; `format_context_for_review` — без macro/positioning
  блоков, только ticker + 1H closes + 1H indicators + funding band.
- `src/ai_trader/config/settings.py`: `review_interval_sec=300` поле
  добавлено (auto-merge).
- `src/ai_trader/llm/prompts.py`: `SYSTEM_PROMPT_REVIEW` константа +
  `build_system_prompt_review` + `build_user_prompt_review` (auto-merge).
- `src/ai_trader/trading/executor.py`: `parse_action(review_mode=True)`
  hard-guard от open в review-цикле (auto-merge).
- `tests/test_ai_trader.py`: +15 тестов из v0.10 (auto-merge).
- `.env.example`: только `AI_TRADER_POLL_INTERVAL_SEC` /
  `AI_TRADER_REVIEW_INTERVAL_SEC` (без single-source-of-truth блока v0.4).

**Что работает на v0.3-контексте:**
- Full-cycle каждые 900s — RSI/MACD/ATR/EMA/BB по 1H/4H + news + open.
- Review-cycle каждые 300s — только открытые позиции, 1H indicators,
  ticker, funding label, без 4H/news. Только close/hold.
- LLM получает 3× больше точек реакции для exit-decisions (по правилу
  EXIT MANAGEMENT, добавленному в предыдущей записи).

**Что НЕ перенесено** (зависит от i1–i6, остаётся в backup-ветке):
- VWAP-deviation / RV в indicators (i1).
- OI/funding-history / Long-Short ratio / orderbook L2 / liquidations
  в positioning (i2/i4/i5).
- F&G / BTC dominance / stables / DVOL macro-context (i3/i6).

В `SYSTEM_PROMPT_REVIEW` ссылки на VWAP/L-S/liquidation cascade
сохранены, но модель проигнорирует их (данных нет в lite-контексте);
рабочие триггеры — RSI cross, MACD flip, funding flip, R-units.

**Тесты:** `pytest tests/test_ai_trader.py` — все passed.

**Файлы:**
- `src/ai_trader/app/main.py`
- `src/ai_trader/config/settings.py`
- `src/ai_trader/llm/prompts.py`
- `src/ai_trader/trading/context.py`
- `src/ai_trader/trading/executor.py`
- `tests/test_ai_trader.py`
- `.env.example`
- `BUILDLOG_AI_TRADER.md`

---

## 2026-05-12 — backport: EXIT MANAGEMENT block (v0.6 prompt → v0.3 база)

**Запрос пользователя:** «нужно найти в ветке backup функционал для
сохранения прибыли — кейс когда бот видит новости, что лот не наберёт
плюс, и должен сохранить прибыль».

**Что сделано.** Cherry-pick коммита `9ef3a1f feat(prompt v0.6):
EXIT MANAGEMENT block` из ветки `backup-pre-rollback-20260511-172246`.
Конфликты в `src/ai_trader/llm/prompts.py` (3 шт.) и в
`BUILDLOG_AI_TRADER.md` решены вручную:

- Версия в docstring: HEAD-база v0.3, поверх неё помечено
  `v0.6-backport (2026-05-12)`. Описания v0.4/v0.5 (parametrized
  symbols и P0 collision audit) НЕ возвращены, так как зависят от
  i1–i6 индикаторов, которые откачены.
- ANALYSIS APPROACH: оставлена 6-пунктовая структура v0.3, добавлен
  один новый пункт — `4) OPEN POSITIONS REVIEW (skip if none)` для
  каждой open position: setup validity, unrealised R, contrary new
  evidence. Итого 7 пунктов (TREND → VOL → SENT → OPEN POS REVIEW →
  CONFIRMATIONS → R:R → DECISION). Упоминание VWAP-return удалено
  (VWAP-индикатора нет в v0.3-контексте).
- Блок EXIT MANAGEMENT (4 trigger'а CLOSE EARLY + 4 DO-NOT-CLOSE
  guards + R-units formula) перенесён **целиком**.

**Что реально работает на v0.3-контексте (RSI/MACD/ATR/EMA/BB):**
- LOCKED-PROFIT GUARD на >= 1.5R + ослабление setup (главный триггер
  «сохранить прибыль»).
- ADVERSE NEW EVIDENCE: counter-direction high-impact news, funding
  flip против позиции.
- SETUP INVALIDATION (trend): 4H EMA20/50 flip против позиции.
- SETUP INVALIDATION (news): catalyst aged 24h+ без follow-through.

**Что в EXIT MANAGEMENT упомянуто, но НЕ применимо** (модель проигнорирует,
т.к. полей нет в market context): VWAP-return для mean-reversion,
retail L/S buy_ratio, F&G zone, OI Δ24h, liquidation cascade.

**`build_user_prompt`** обновлён под 7-пунктовую структуру.

**Тесты:** `pytest tests/test_ai_trader.py` — все passed (после правок
ниже).

**Файлы:**
- `src/ai_trader/llm/prompts.py` (v0.6-backport)
- `BUILDLOG_AI_TRADER.md`

**Не cherry-pick'нуто:** ничего из i1–i6 (VWAP, RV, OI, funding cum,
F&G, BTC dom, L/S, L2 orderbook, liquidations, DVOL), v0.4/v0.5
prompts, v0.7 fees prompt, v0.8 conviction sizing, v0.9–v0.13.1 —
по запросу пользователя сохранено в `backup-pre-rollback-20260511-172246`.

---

## 2026-05-11 — rollback: HEAD → f3ce9795 (откат всех изменений 07–11 мая)

**Запрос пользователя:** откатить все ai_trader-коммиты ПОСЛЕ `f3ce9795`
(включая расширение пула 5→10 пар, индикаторы i1–i6, EXIT MANAGEMENT,
conviction sizing, dual-timer, SL discipline, ADX regime filter,
SL cooldown, closed-bars-only, price-drift guard).

**Что показали данные (Bybit closed-pnl API + local DB):**

| Период | n trades | WR (API) | Total PnL (API) | PF | Expectancy/tr |
|---|---|---|---|---|---|
| ДО b5c2c679 (05-04 → 05-07 07:58 UTC) | 12 | 58.3% | +$2.66 | 1.22 | +$0.39 |
| ПОСЛЕ b5c2c679 (05-07 → 05-11 14:10 UTC) | 42 | 35.7% | −$120.63 | 0.70 | −$1.61 |

Главные потери: AVAX (−$65), WLD (−$25), ATOM (−$20) — все из расширения
пула в `6d51360`.

**Sample-size warning (по правилу `sample-size.mdc`):**
- n=12 ДО — слишком мало для статистической уверенности.
- n=42 ПОСЛЕ < 100 (порог правила).
- Fisher exact test разницы WR: p ≈ 0.20 — **не значимо**.
- Однако −$120 (−24% capital) — финансовый факт, USER OVERRIDE правила.

**Как сделан откат:**
1. Backup всего предшествующего HEAD в ветке
   `backup-pre-rollback-20260511-172246` (запушена на origin для
   полной сохранности истории — все 28 ai_trader-коммитов и текст
   BUILDLOG_AI_TRADER.md с детальным разбором каждой версии).
2. `git reset --hard f3ce9795` (откат всех 32 коммитов после `f3ce9795`,
   включая 28 ai_trader-фичей и 3 фикса других ботов).
3. Cherry-pick 3 фикса по другим ботам обратно (правило
   `strategy-guard.mdc` про изоляцию кодовых баз):
   - `2856293` ← `5d73b13` fix(ctrader): exponential backoff
   - `a203362` ← `1a1a75a` fix(bybit-bot): dedup closedPnl
   - `fb0ffd1` ← `1699b29` fix(ctrader): proactive token-refresh
4. Тесты: 101 passed (test_ai_trader + test_bybit_bot + test_ctrader).
5. Force-push main с `--force-with-lease`.

**Что вернулось в строй (старое поведение):**
- 5 пар: BTC, ETH, BNB, XRP, DOGE
- `max_positions=3`, `risk_per_trade=2%` ($10/trade при $500 capital)
- `max_daily_loss=$50`, `max_total_loss=$200`
- Базовые индикаторы 1H/4H: RSI, MACD, ATR, EMA, BB (без VWAP/RV/OI/funding/F&G/dom/L-S/L2/liquidations/DVOL)
- Промпт без EXIT MANAGEMENT, без SL DISCIPLINE, без compliance-JSON
- Цикл 15 мин (без review 5 мин)
- Без ADX regime filter, без SL cooldown, без price-drift guard

**Что НЕ откатывали (правило `strategy-guard.mdc`, изоляция):**
- Фиксы advisor/cTrader (token-refresh, exponential backoff)
- Фикс bybit_bot (dedup closedPnl)

**Восстановление если понадобится:**
```
git checkout backup-pre-rollback-20260511-172246
# или cherry-pick конкретных фичей из этой ветки
```

**Файлы:** `git reset --hard` + cherry-pick (см. выше).

---

## 2026-05-07 — fix(reconcile): не помечать позицию closed при API failure биржи

`75d85a7`

**Симптом** (Telegram, 04:29 МСК = 01:29 UTC, cycle 74):

```
❌ ERROR in LLM
Connection error.
```

Позиция **id=5 BTCUSDT Buy 0.006 @ $82184.9** на бирже Bybit demo
осталась открытой (size=0.006, SL=80541, TP=84651, unrealised PnL ≈ −$5.46),
а в локальной БД `ai_trader.sqlite` была помечена closed с маркерами:

```
exit_price = 82184.9          ← равен entry_price
realized_pnl_usd = $0.00      ← подозрительно ровный ноль
close_reason = "exchange_closed (SL/TP/manual)"
closed_at = 2026-05-07T00:21:44 UTC
```

Pattern PnL=$0.00 + exit=entry — визитная карточка фейк-клоза.

**Причина.** В Cycle 71 (00:21:16 UTC) на VPS отказал DNS на ~30 минут:

```
2026-05-07 00:21:44 [ERROR] ai_trader.trading.client: get_positions failed
NameResolutionError: Failed to resolve 'api-demo.bybit.com'
2026-05-07 00:21:44 [INFO] ai_trader: RECONCILE closed:
  id=5 Buy BTCUSDT qty=0.006 | entry=$82184.9 exit=$82184.9 | PnL: $+0.00
```

`AiBybitClient.get_positions(symbol="BTCUSDT")` поймал
`requests.ConnectionError` и **молча возвращал `[]`**. Реконсилятор
в `app/main.py:_reconcile_closed_positions` интерпретировал пустой
список как «позиция исчезла с биржи → её закрыли SL/TP» и обновил БД.
`get_ticker` тоже упал, поэтому `exit_price` упал на fallback
`db_pos.entry_price` → PnL=$0.00.

После этого LLM API тоже упал с тем же DNS-symptom — отсюда
«❌ ERROR in LLM / Connection error» в Telegram (это сообщение
дошло позже, когда DNS Telegram-API восстановился раньше биржевого).

**Решение.**

1. **`get_positions` теперь возвращает `list[Position] | None`**
   (`src/ai_trader/trading/client.py`):
   - `None` ⇐ network exception **или** `retCode != 0`.
   - `[]` ⇐ API ответил успешно, открытых позиций нет.
   - Вызывающий код ОБЯЗАН отличать `None` от `[]`:
     «нет ответа» ≠ «нет позиций».

2. **`_reconcile_closed_positions`** (`src/ai_trader/app/main.py`):
   - Собирает positions per-symbol; если `get_positions` вернул
     `None` для символа — этот символ помечается `failed_symbols`
     и **полностью пропускается**, ни одна его позиция не помечается
     closed.
   - Дополнительно: даже при успешном `get_positions=[]`, если
     `get_ticker` тоже упал — позиция **не помечается closed**
     (без exit-цены нельзя посчитать корректный PnL; ждём следующего
     цикла, когда биржа отвечает).
   - Логируем `WARNING` с причиной отложенного reconcile, чтобы
     видеть в журнале реальные blackouts.

3. **Hot-fix БД на VPS.** Восстановил позицию id=5:
   - `UPDATE positions SET closed_at=NULL, exit_price=NULL,
     realized_pnl_usd=NULL, close_reason=NULL WHERE id=5;`
   - `UPDATE daily_pnl SET n_trades=n_trades-1 WHERE day='2026-05-07';`
     (`realized_pnl_usd` и `n_wins` не трогал — фейк-клоз был с
     PnL=$0, won=0, эти счётчики не сместились).
   - После восстановления состояние БД 1:1 совпадает с биржей
     (Buy 0.006 BTCUSDT @ 82184.9, SL=80541, TP=84651).

4. **9 regression-тестов** (`tests/test_ai_trader.py`):
   - `TestGetPositionsApiFailureMarker` (4 теста):
     network-exception → None, non-zero retCode → None,
     empty list → [], success c позициями.
   - `TestReconcileClosedPositions` (5 тестов):
     `test_api_failure_does_not_close_position` (главный регресс),
     `test_ticker_failure_does_not_close_position`,
     `test_position_still_open_no_change`,
     `test_position_actually_closed_marks_closed` (happy path с
     корректным PnL=$14.7966 на TP),
     `test_partial_api_failure_isolates_failed_symbol` (BTC failed,
     ETH ОК — изолированно обрабатываются).

5. Все 450 тестов в репозитории зелёные.

**Файлы:**
- `src/ai_trader/trading/client.py` — `get_positions` → `| None`
- `src/ai_trader/app/main.py` — guard в `_reconcile_closed_positions`
- `tests/test_ai_trader.py` — +9 regression-тестов

---

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
