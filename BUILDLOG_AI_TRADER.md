# BUILDLOG — AI-Trader (DeepSeek-V4)

## 2026-05-11 — feat(v0.12): ADX regime filter + SL cooldown (hard enforcement)

`5a750d1` (хеш данного коммита самого по себе сместится из-за amend
этой ссылки — см. реальный `git log` на VPS после деплоя)

**Контекст.** После v0.11.1 деплоя 2026-05-11 (~12:30 MSK) бот в первом
же цикле открыл ATOMUSDT Sell (mean-reversion thesis: «stretched +5%
above 4H VWAP, retail long 69.7% → contrarian short»). 11 циклов
hold, цена выросла на +2% за 15 минут на 5-6× объёме, SL hit на $2.08,
PnL = **-$33.18** = 6.6% капитала за одну сделку. Через 23 минуты
(cycle 19) LLM открыл новый ATOM short на **тех же signals** — это
recidivism / chasing losers. Анализ показал две корневые проблемы,
которые не лечатся промптом, а только кодом:

1. **Regime ignorance.** LLM применил mean-reversion thesis в strong
   uptrend (1H ADX14 = 57, +DI = 41, -DI = 11 — выраженный bull
   trend). По всему канону mean-reversion стратегий
   (Connors/Raschke «Street Smarts» 1995, ch.2; botversusbot 2026
   «Regime-aware Mean Reversion») mean-reversion в TRENDING регулярно
   = suicide. «Stretch above VWAP» в trending market не сигнал
   exhaustion, а сигнал продолжения тренда.
2. **No cooldown after SL.** Сразу после SL — повторный вход в тот же
   short. Канонический паттерн «losers in row» (TradingView
   jannisMCMXCV 2026 «Mean-Reversion with Cooldown», AOTrading 2026
   «3-5-7 Rule»): после SL по mean-reversion стратегии нужен
   forced cooldown растущей длительности.

Промпт-only решения уже исчерпаны (v0.11 STOP-LOSS DISCIPLINE,
v0.11.1 compliance внутри JSON — LLM выполняет их частично, в кейсе
ATOM сама себе соврала про `sl_atr_ratio` 2.0 при фактическом 1.68 —
зафиксировано как `MODEL_MISREPORT`). LLM хорошо рассуждает, но
плохо соблюдает механическую дисциплину. Поэтому два правила вынесены
в hard-enforcement code-level gates (как kill-switch и SL discipline).

**Изменения.**

1. **ADX regime filter** — новый индикатор:
   - `src/ai_trader/analysis/indicators.py`: функция `adx(highs, lows,
     closes, period=14)` точно по Wilder 1978 (RMA-сглаживание TR/+DM/-DM,
     затем RMA(DX, period)). Поля `adx14, plus_di14, minus_di14`
     добавлены в `IndicatorSnapshot`. `compute_snapshot` считает
     ADX/DI; `format_snapshot` выводит строку
     `ADX14=X.X +DI=X.X -DI=X.X [TRENDING uptrend/downtrend | TRANSITION
     | RANGING (mean-reversion zone)]`.
   - Пороги (Wilder каноник): >=25 = TRENDING, <20 = RANGING, между =
     TRANSITION. Конфигурируется через
     `AI_TRADER_ADX_REGIME_THRESHOLD` (default 25.0).
   - `src/ai_trader/trading/executor.py:_apply_open` принимает
     `regime_by_symbol: dict[str, dict[str, float]]` (с `adx14/plus_di14/
     minus_di14`). Перед place_order: если ADX>=threshold и направление
     LLM-сделки **противоположно** направлению тренда (Sell в uptrend
     или Buy в downtrend) → reject с `regime_block`. Это hard-blocker,
     не предупреждение.
   - `src/ai_trader/app/main.py:_run_cycle` собирает `regime_by_symbol`
     из `ctx.snapshots[*].ind_1h` и передаёт в `apply_action` рядом с
     `atr_by_symbol`. Никаких новых API-вызовов — данные уже есть в
     контексте.
   - `src/ai_trader/llm/prompts.py`: новый блок REGIME FILTER в SYSTEM
     PROMPT описывает labels, hard rule и whitelist разрешённых
     counter-trend условий (ADX<20 + sentiment/positioning extreme).
     В блоке F) (PER-SYMBOL 1H/4H INDICATORS) добавлен пункт про
     ADX14 / +DI / -DI и обратная ссылка на REGIME FILTER.

2. **SL cooldown с Fibonacci-расписанием**:
   - `src/ai_trader/state/db.py`: новая таблица `sl_cooldown` (symbol,
     side, last_sl_at, consecutive_count). Методы:
     - `record_sl(symbol, side) -> int` — пишет SL, инкрементирует
       consecutive_count. Если предыдущий SL >=24ч назад — счётчик
       сбрасывается на 1.
     - `reset_cooldown(symbol, side)` — удаляет запись (при
       прибыльном закрытии).
     - `get_cooldown_remaining_minutes(symbol, side) -> int` — сколько
       минут осталось до конца cooldown (0 если не активен).
     - `_fib_cooldown_minutes(n)` — static helper. Расписание (минут):
       n=1 → 15, n=2 → 15, n=3 → 30, n=4 → 45, n=5 → 75, n>=6 → 120 (cap).
       Это Fibonacci-числа баров (1,1,2,3,5,8) × 15-min TF.
   - `_reconcile_closed_positions` теперь:
     - если pnl<0 (SL hit или manual close-in-loss) → `store.record_sl`
       + лог `COOLDOWN recorded: SYM SIDE consecutive=N → ban for M min`;
     - если pnl>=0 → `store.reset_cooldown`.
   - `_apply_open` после killswitch чекает
     `store.get_cooldown_remaining_minutes(symbol, side)` и при > 0
     возвращает `cooldown_active: SYM SIDE blocked for K more min`.
   - В SYSTEM PROMPT добавлен блок COOLDOWN AFTER STOP-LOSS — поясняет
     LLM расписание и что executor вернёт `cooldown_active`.

**Тесты.** +25 новых юнитов в `tests/test_ai_trader.py`:

- `TestAdxIndicator` — 4 теста: short series → None, strong uptrend
  → ADX>25 + +DI>-DI, strong downtrend → -DI>+DI, sideways → ADX<25.
- `TestRegimeLabelInSnapshot` — 2 теста: `format_snapshot` содержит
  строку ADX и правильный label TRENDING uptrend / RANGING.
- `TestSlCooldownDB` — 8 тестов: initial=0, first SL → count=1 +
  ~15 min, consecutive увеличивает, reset обнуляет, side/symbol
  изолированы, Fibonacci schedule = [15,15,30,45,75,120], после 24ч
  без SL счётчик сбрасывается на 1.
- `TestExecutorCooldownAndRegimeGates` — 7 тестов: cooldown блокирует
  + reset разрешает; regime блокирует Sell в uptrend и Buy в
  downtrend; разрешает trend-following Buy в uptrend; разрешает
  counter-trend при ADX<threshold; back-compat без `regime_by_symbol`.
- `TestReconcileWritesCooldown` — 2 теста: pnl<0 пишет SL, pnl>0
  сбрасывает cooldown.
- `TestRegimeFilterPrompt` — 2 теста: SYSTEM PROMPT содержит блоки
  REGIME FILTER и COOLDOWN AFTER STOP-LOSS.

Итог: `pytest tests/` — 596 passed (было 571 → +25). Линтер чист.

**Research basis (с указанием источников):**
- ADX как regime filter: J. Welles Wilder Jr., «New Concepts in
  Technical Trading Systems» (1978); botversusbot.com 2026
  «Regime-aware Mean Reversion»; Connors & Raschke «Street Smarts:
  High Probability Short-Term Trading Strategies» (1995, ch.2 —
  каждая mean-reversion стратегия требует range-bound фильтр).
- Cooldown: TradingView jannisMCMXCV 2026 «Mean-Reversion with
  Cooldown»; AOTrading 2026 «3-5-7 Rule for mean-reversion cooldown».
- Position sizing / risk-as-master: Van K. Tharp «Trade Your Way to
  Financial Freedom» (2007, ch.11) — risk и ATR диктуют size, не
  наоборот.

**Файлы:**
- `src/ai_trader/analysis/indicators.py` — adx(), поля ADX/DI,
  format_snapshot.
- `src/ai_trader/state/db.py` — sl_cooldown table + методы.
- `src/ai_trader/app/main.py` — reconcile пишет cooldown, _run_cycle
  собирает regime_by_symbol.
- `src/ai_trader/trading/executor.py` — apply_action / _apply_open
  cooldown + regime gates.
- `src/ai_trader/llm/prompts.py` — блоки REGIME FILTER и COOLDOWN
  AFTER STOP-LOSS; ADX14 в F).
- `src/ai_trader/config/settings.py` — `adx_regime_threshold` 25.0.
- `tests/test_ai_trader.py` — +25 тестов.

**Метрики после деплоя планируется собрать.** Watch-list для
audit-обсуждения через 1-2 недели:
- `executor` логи: сколько `regime_block` и `cooldown_active` rejects
  (если их 0 — feature не активирован реальностью).
- Win rate / средний loss на mean-reversion entries (должен подняться,
  т.к. трейды против сильного тренда исключены).
- Recidivism rate: % случаев когда LLM пытается заходить в той же
  паре/стороне в течение 2 часов после SL (наблюдение, не блокер).

---

## 2026-05-11 — hotfix(prompt v0.11.1): compliance внутри JSON, fix max_tokens cutoff

`<hash-pending>`

**Симптом (после деплоя v0.11 в 04:32 UTC / 07:32 MSK):**
3 цикла подряд `ERROR in LLM: empty response after 2 attempts
(+1 no-thinking fallback)` в 07:53, 09:50, 11:34 MSK. Между ними часть
циклов проходила с `out=4096` (ровно лимит) и `Parse error: no JSON
object found in response`.

**Корневая причина.** В v0.11 я добавил перед JSON большой текстовый
блок `PRE-DECISION CHECKLIST(open)` ~10 строк со структурой полей
(symbol/side/ATR/SL distance/ratio/trend/counter-trend?/confirmations/
RR raw/RR net/risk). Это раздуло output модели на ~300-400 токенов.
У DeepSeek-V4 thinking-токены + текст ответа делят общий буфер
`max_tokens=4096`. После v0.11 reasoning-toкены + commentary + чеклист
+ JSON уже не помещались — модель упиралась в лимит и обрезала JSON
(parse error) или вовсе ничего не выводила (empty response).

**Решение (вариант 2 по обсуждению с пользователем):** чеклист вынесен
ВНУТРЬ JSON как `compliance: {sl_atr_ratio, rr_net_fee, counter_trend,
confirmations}`. Один и тот же набор данных теперь не дублируется
текст+JSON, а живёт только в JSON. Output сокращается на ~300-400 ток.,
форма стала строго машинной (можно валидировать кодом).

**Что изменено:**

1. `src/ai_trader/llm/prompts.py`:
   - Удалён текстовый `PRE-DECISION CHECKLIST(open)` блок (~25 строк).
   - Добавлен раздел `COMPLIANCE (MANDATORY for every "open")` — короткое
     описание sub-object'а и требований к нему.
   - JSON-схема `open` дополнена полем `compliance` с 4 sub-полями.
   - `CRITICAL CONSTRAINTS` дополнен упоминанием обязательного
     `compliance` в JSON (вместо упоминания текстового CHECKLIST'а).
   - User-prompt: убрана инструкция "output PRE-DECISION CHECKLIST",
     добавлена "JSON MUST include the `compliance` sub-object".
   - История версий: добавлен блок про v0.11.1.

2. `src/ai_trader/trading/executor.py`:
   - `parse_action` для `action="open"` валидирует структуру
     `compliance`: `sl_atr_ratio` (float>0), `rr_net_fee` (float>0),
     `counter_trend` (bool), `confirmations` (list of >=2 non-empty
     strings). Любое нарушение → parse error (ParsedAction не создан,
     ордер не уйдёт).
   - `_apply_open` делает **cross-check**: заявленный
     `compliance.sl_atr_ratio` сравнивается с фактическим
     `|entry-SL|/ATR(1H)`. Расхождение > 10% → `log.warning(
     "MODEL_MISREPORT ...")`. Не блокирует трейд — это аудиторный
     сигнал, нужен чтобы поймать "модель врёт о соблюдении правил".

3. `tests/test_ai_trader.py`:
   - В `TestParseAction` константа `_VALID_COMPLIANCE` для повторного
     использования; обновлены `test_open_buy_valid`, `test_unknown_symbol`,
     `test_open_negative_leverage` (контракт парсера изменился).
   - Добавлены 4 новых: `test_open_missing_compliance`,
     `test_open_compliance_bad_sl_ratio`,
     `test_open_compliance_confirmations_too_few`,
     `test_open_compliance_counter_trend_not_bool`.
   - В `TestReviewModeParseAction.test_open_allowed_when_review_mode_false`
     добавлено `_VALID_COMPLIANCE`.
   - В `TestStopLossDisciplinePrompt`: переименован/переписан
     `test_prompt_contains_pre_decision_checklist` →
     `test_prompt_contains_compliance_in_open_schema` (проверяет
     compliance в JSON-схеме, а не текстовый чеклист).
   - В `test_critical_constraints_mention_min_sl_distance` ассерт на
     `"compliance"` вместо `"PRE-DECISION CHECKLIST"`.

**`max_tokens` оставлен 4096.** После того как чеклист ушёл в JSON,
обычный output должен снова укладываться в 2500-3500 токенов с запасом.
Если эти ошибки повторятся — поднимем до 8192, но сначала смотрим
24-48 ч новой v0.11.1.

**Тесты:** 559 (+4 новых compliance, +1 обновлён в review-mode,
2 переписаны на новый контракт). Все зелёные.

**Файлы:**
- `src/ai_trader/llm/prompts.py` (–30 строк чеклиста, +20 строк compliance)
- `src/ai_trader/trading/executor.py` (+30 строк compliance валидация + cross-check)
- `tests/test_ai_trader.py` (+50 строк compliance тесты, обновления контракта)

---

## 2026-05-11 — feat(prompt v0.11): STOP-LOSS DISCIPLINE + PRE-DECISION CHECKLIST + soft enforcement

`<hash-pending>`

**Контекст.** Разбор последних 3 крупных лоссов (на бирже, источник —
Bybit `get_closed_pnl` per `ai-trader-pnl.mdc`):

| ID | Symbol | PnL (user) | PnL (DB) | LLM-decision pattern |
|----|--------|------------|----------|----------------------|
| 40 | AVAXUSDT | −$28.62 | −$25.68 | SL ~1×ATR (1.62% от entry); LLM сам признал "strongly bullish 4H trend", но 18 циклов держал counter-trend short → SL hit за 1.5 ч обычным шумом |
| 45 | LTCUSDT  | +$24.13 | +$29.32 | Идеальное исполнение EXIT MANAGEMENT v0.6: Locked-Profit Guard на 1.8R, закрыли через 47 мин |
| 42 | AVAXUSDT | −$9.48  | −$9.09  | Хороший close по Setup Invalidation (1) за 15 мин — лосс ограничен |

Корневая причина id=40 — **слишком тугой SL** (~1× ATR(1H)). Промпт уже
писал "SL distance typically 1.5-2.5 ATR" в Trading rules, но LLM это
игнорировал. Нужен механизм enforcement.

**Решение (per user request):** усилить промпт + добавить pre-computed
числа в context + soft warning-log в executor. Hard-block отложен —
если за 24-48 ч соберём ≥10 нарушений, переходим к executor reject
(см. дальше).

**Что добавлено:**

1. `src/ai_trader/trading/context.py` — новый helper `_format_sl_reference(s)`:
   - Печатает блок `REFERENCE SL BOUNDARIES (1H ATR=$X = Y% of price)`
     с конкретными долларовыми числами min (1.5×ATR) и recommended (2.0×ATR)
     дистанций для Buy и Sell. Пример (BTC@60000, ATR=$300):
     ```
     REQUIRED min |entry - SL| >= $450 (1.5xATR), RECOMMENDED >= $600 (2.0xATR)
     For Buy at ~$60000: SL must be <= $59550 (recommend <= $59400)
     For Sell at ~$60000: SL must be >= $60450 (recommend >= $60600)
     ```
   - Вставлен в `format_context_for_prompt` (full cycle) и
     `format_context_for_review` (review cycle) после блока 1H INDICATORS.

2. `src/ai_trader/llm/prompts.py` (SYSTEM_PROMPT_TEMPLATE) —
   три новых блока:
   - `STOP-LOSS DISCIPLINE (HARD RULE)` после CAPITAL RULES — явное
     правило `>=1.5x ATR(1H)` со ссылкой на REFERENCE SL BOUNDARIES,
     инструкция: при tight SL → widen + recompute qty, ИЛИ HOLD;
     **запрет** «shrink SL чтобы вместить qty».
   - `PRE-DECISION CHECKLIST (MANDATORY for every "open")` перед
     DECISION FORMAT — обязательный машинно-читаемый блок с
     конкретными числами:
     ```
     CHECKLIST(open):
     - 1H ATR: $<X.XX>
     - SL distance: $<X.XX>
     - SL/ATR ratio: <X.XX>     (REQUIRED >= 1.50)
     - 4H trend: <up/down/range>
     - Counter-trend?: <yes/no>
     - Confirmations (>=2, DIFFERENT classes): <list>
     - R:R (raw): <X.XX>          (REQUIRED >= 1.5)
     - R:R (net of 0.12% fee): <X.XX>  (REQUIRED >= 1.8)
     - Risk USD: $<X.XX>          (REQUIRED <= $30)
     ```
     Любой failed check → action MUST be "hold". Replaces vague
     "setup looks ok" с числами для пост-фактум аудита.
   - `CRITICAL CONSTRAINTS` дополнен двумя пунктами:
     `|entry - stop_loss| >= 1.5x ATR(1H)` и
     `must output PRE-DECISION CHECKLIST above the JSON`.

3. `src/ai_trader/trading/executor.py` — soft enforcement:
   - `apply_action`/`_apply_open` принимают опциональный
     `atr_by_symbol: dict[str, float] | None`.
   - Если ATR передан, для real-order ветки (trading_enabled=true)
     считаем `sl_atr_ratio = |entry-SL| / ATR(1H)`.
   - Ratio < 1.5 → `log.warning("SL_DISCIPLINE_VIOLATION ...")` +
     summary получает тег `[sl_atr=1.00!]` (восклицательный знак).
   - Compliant → summary получает чистый тег `[sl_atr=2.00]`.
   - Trade **НЕ блокируется** — это soft enforcement, по запросу
     пользователя (опция C). Нарушения ловятся в логах для
     последующего decision-making.
   - PAPER mode пропускает проверку (нет real ордера).

4. `src/ai_trader/app/main.py`:
   - В full-cycle apply_action собирает `atr_by_symbol` из
     `ctx.snapshots[*].ind_1h.atr14` (нет дополнительных API calls).
   - В review-cycle ATR не нужен (open запрещён в review).

**Тесты (+12, всего 555):**
- `TestSlReferenceBoundaries` (5): формат boundaries, fallback при
  отсутствии ATR/ticker, интеграция в full + review context.
- `TestStopLossDisciplinePrompt` (3): STOP-LOSS DISCIPLINE block,
  PRE-DECISION CHECKLIST содержит ключевые поля, CRITICAL CONSTRAINTS
  упоминает оба правила.
- `TestExecutorSlComplianceWarning` (4): violation→WARNING+tag,
  compliant→clean tag, no_atr→no tag (back-compat), PAPER skip.

**Метрика для решения hard vs soft (через 24-48 ч):**
- Считаем `SL_DISCIPLINE_VIOLATION` в логах ai-trader.
- ≥10 нарушений за 48 ч → переходим к hard-block (executor отвергает
  ордер с SL/ATR < 1.5, возвращает HOLD).
- 0-3 нарушения → промпт+context достаточны, оставляем soft.

**Файлы:**
- `src/ai_trader/trading/context.py` (+30 строк, helper + 2 встройки)
- `src/ai_trader/llm/prompts.py` (+50 строк, 3 новых блока + history)
- `src/ai_trader/trading/executor.py` (+30 строк, soft validation)
- `src/ai_trader/app/main.py` (+5 строк, atr_by_symbol сбор)
- `tests/test_ai_trader.py` (+260 строк, 12 новых тестов)

**Известное ограничение:** review-cycle SL не редактирует (биржевой
SL уже стоит) — discipline-check не применяется. Для уже открытых
позиций тегов compliance нет; будут только для новых open.

---

## 2026-05-10 — disable: TAOUSDT удалён из AI_TRADER_SYMBOLS (USER OVERRIDE)

`<hash-pending>`

**Контекст.** API-stat по 3 закрытым TAO-сделкам ai_trader (Bybit
`get_closed_pnl`, источник истины по `ai-trader-pnl.mdc`):

| Дата (UTC)        | Side | qty   | PnL     |
|-------------------|------|-------|---------|
| 2026-05-08 17:06  | Sell | 1.619 | −$10.02 |
| 2026-05-10 02:16  | Sell | 1.61  | −$5.98  |
| 2026-05-10 04:02  | Sell | 8.0   | −$10.23 |
| **Итого: 3 трейда, 0% WR, −$26.23** |||

**Нарушение `sample-size.mdc` (осознанное USER OVERRIDE):**

Правило `.cursor/rules/sample-size.mdc` запрещает отключение инструмента
по <100 сделок без обсуждения с пользователем:
- ≥100 сделок: НЕТ (n=3)
- ≥2 недели: НЕТ (3 дня)
- p-value <0.05: НЕТ (binomial 0/3 vs baseline 50% → p=0.125)
- Разница в R:R ≥0.3 или WR ≥10%: формально да (0% vs ~50%), но при n=3 — это шум.

Правило также рекомендует «уменьшить размер позиции, не отключать».
Это обсуждалось с пользователем; решение принято в пользу полного
отключения (per-symbol sizing в боте сейчас не реализован, поэтому
снижение лимита потребовало бы новой механики). Решение ОФОРМЛЕНО как
USER OVERRIDE правила, аналогично risk-per-trade override от 2026-05-09.

**Что изменено:**
- `src/ai_trader/config/settings.py`: `DEFAULT_AI_SYMBOLS` 10 → 9 пар
  (TAOUSDT удалён). Комментарий с обоснованием в коде.
- `.env.example`: `AI_TRADER_SYMBOLS` 10 → 9 пар.
- `.env` на VPS (`/root/fx-pro-bot/.env`): `AI_TRADER_SYMBOLS` обновлён
  на VPS вручную (через ssh + sed) до селективного rebuild.
- `tests/test_ai_trader.py`: assert на 9 пар + явная проверка что
  TAOUSDT отсутствует в дефолтном промпте.

**Что НЕ изменено (намеренно):**
- Существующие открытые TAO-позиции остаются под управлением ai_trader
  до их естественного закрытия (LLM получит OPEN POSITIONS list с TAO,
  но новые TAO-сделки не сможет открыть — symbol not in allowed list,
  rejected в `parse_action`).
- `bybit_bot` и `advisor` НЕ затронуты — у них свои списки символов
  (`BYBIT_BOT_SCAN_SYMBOLS`, `FX_PRO_BOT_INSTRUMENTS`).

**Тесты:** `python3 -m pytest tests/ -q` → **543 passed in 5.96s**.

**Пересмотр.** При желании вернуть TAOUSDT — добавить обратно в
`.env` на VPS + (опционально) в `DEFAULT_AI_SYMBOLS` settings.py.
Без code-rebuild достаточно правки `.env` + `docker compose up -d
--no-deps ai-trader`. Через 2 недели имеет смысл переоценить решение
с большей выборкой (если TAO рынок к тому времени поменяется —
например, выход из downtrend / снижение retail-bias).

**Файлы:**
- `src/ai_trader/config/settings.py`: `DEFAULT_AI_SYMBOLS` minus TAOUSDT
- `.env.example`: AI_TRADER_SYMBOLS minus TAOUSDT
- `tests/test_ai_trader.py`: assert на 9 пар + проверка отсутствия TAO

---

## 2026-05-10 — feat(prompt v0.10): двойной таймер full(15min) + review(5min)

`<hash-pending>`

**Контекст.** Анализ 3 последних минусовых сделок ai_trader показал:
из 3 лузов только **1** был закрыт биржей по SL (TAOUSDT id=27,
−$5.98). Остальные два LLM закрыл сам (превентивно/по invalidation).
Главная боль — TAO id=27: 28 циклов LLM держал позицию по mean-rev
тезису «stretched above VWAP + retail heavy long → contrarian short»;
позиция была в плюсе <1.5R, потом откат, между cycles ушло 16 минут,
SL hit. Между full-cycles бот «слепой» — за 15 минут цена
успевает пройти весь R-distance и сработать exchange SL раньше, чем
LLM получит шанс среагировать на adverse evidence.

**Решение пользователя:** не автоматизировать защиту в коде (не двигать
SL автоматически), а дать LLM **в 3 раза больше точек реакции**.
Между full-cycle (15 мин) добавить lite review-cycle (5 мин), который
смотрит ТОЛЬКО на уже открытые позиции и может ИХ закрыть досрочно
(или держать). Открытие новых позиций в review запрещено — это
делается только в полном цикле с macro/news/options-IV контекстом.

**Архитектура (v0.10).**

1. **Двойной таймер в `app/main.py`:** один монотонный счётчик `cycle`,
   два таймера `last_full_ts` / `last_review_ts` через `time.monotonic()`.
   Каждую секунду просыпаемся:
   - если прошло `poll_interval_sec` (900) с last_full → запускаем
     `_run_cycle` (full, как раньше) и сбрасываем review-таймер;
   - иначе если прошло `review_interval_sec` (300) с last_review →
     запускаем `_run_review_cycle` (lite).

   Без threads/asyncio — только `time.sleep(1)` с проверкой таймеров.
   `cycle` инкрементится в обоих случаях; full и review различимы по
   `prompt_system` в БД (review-промпт начинается с «You are reviewing
   your existing open Bybit perpetual-futures…»).

2. **Lite-контекст (`trading/context.py:collect_review_context`):**
   фетчит ТОЛЬКО символы с open positions (не все 10 пар!). Для каждого:
   - `get_ticker` (текущая цена + funding + 24h)
   - `get_klines(interval=60, limit=50)` → 1H индикаторы
   - `get_funding_rate_history(limit=2)` (свежий funding label)
   - `get_long_short_ratio(period=1h, limit=2)` (retail extreme detection)

   НЕ фетчит: 4H бары, news, macro (CoinGecko/F&G), Deribit DVOL,
   OI history с большим limit, orderbook depth-50. Это уменьшает
   количество API-вызовов в ~5 раз и дёшево обновляет картинку для
   exit-decision.

   Skip-логика: если open_positions == 0 → возвращает MarketContext
   с пустыми snapshots; caller (`_run_review_cycle`) пропускает review.

3. **Review-промпт (`llm/prompts.py:SYSTEM_PROMPT_REVIEW`):**
   - Явно объясняет: lite-цикл, видишь меньше данных, в 3х больше
     шансов реагировать.
   - Запрещает `"open"` (action ∈ {"close", "hold"}). JSON-схема
     не содержит open-варианта.
   - 3 close-триггера (упрощённая версия EXIT MANAGEMENT v0.6):
     (1) SETUP INVALIDATION (1H VWAP dev <0.5%, MACD flip),
     (2) LOCKED-PROFIT GUARD (>=1.5R + invalidation),
     (3) ADVERSE NEW EVIDENCE (funding flip, RSI cross from extreme).
   - 3 DO-NOT-CLOSE guards (то же что в full).
   - Параметризован через `%(full_min)d`/`%(review_min)d` — секунды/60.

4. **Hard-guard в parse_action(`trading/executor.py`):** новый
   keyword-only параметр `review_mode: bool = False`. Если
   `review_mode=True` и LLM всё-таки вернул `"action": "open"` —
   парсер отвергает с явной ошибкой `review_mode: 'open' action is
   forbidden in review cycle`. Защита от случаев когда LLM проигнорирует
   текстовую инструкцию промпта.

5. **Telegram-нотификации:** review-цикл уведомляет только о `close`
   (открытий не бывает по дизайну), errors → `notify_error("review N", ...)`.

**Стоимость по факту.** За всё время работы (~3 дня) cumulative cost
DeepSeek calls = $0.70 (USER feedback). Прирост от review-cycle ~3×
(цикл в 5 раз чаще, но промпт ~5× короче) → +$0.50–$0.80 за тот же
период. Приемлемо.

**Конфиг (settings.py + .env.example):**
- `review_interval_sec: int = 300` (5 мин). 0 = review отключён.
- Параметризовано через `AI_TRADER_REVIEW_INTERVAL_SEC`.

**Тесты (`tests/test_ai_trader.py`, +15 unit-тестов):**
- `TestReviewModeParseAction` — 4 теста: open отвергнут, close/hold
  пропущены, default режим не сломан.
- `TestBuildSystemPromptReview` — 4 теста: дефолтные интервалы,
  запрет open, no unresolved placeholders, custom intervals.
- `TestFormatContextForReview` — 2 теста: empty positions
  (без macro/news/options блоков), with open position.
- `TestCollectReviewContext` — 2 теста: skip когда нет positions,
  фетч только символов с open positions.
- `TestReviewIntervalSettings` — 3 теста: дефолт, override, отключение
  через 0.

`python3 -m pytest tests/ -q` → **543 passed in 5.99s** (528 + 15 new).

**По правилу `no-data-fitting.mdc`:** правка вытекает из конкретного
артефакта анализа — `/tmp/last3_loses.py` показал что TAO id=27 был
держан 28 циклов с mean-rev тезисом, между cycle 84 и 85 ушло 16
минут — за это время сработал exchange SL. С 5-мин review между full
было бы 3 точки проверки в этом окне. Sample size: 1 кейс реального
SL-hit (TAO id=27) — этого недостаточно для disable-решений по правилу
`sample-size.mdc`, но **достаточно** для добавления механики реакции,
которая раньше была заявлена как TODO в v0.6. Это не «отключаем
инструмент», это «даём LLM больше шансов работать по уже принятым
правилам EXIT MANAGEMENT v0.6».

**Файлы:**
- `src/ai_trader/config/settings.py`: `review_interval_sec` поле
- `src/ai_trader/trading/context.py`: `collect_review_context`,
  `format_context_for_review`
- `src/ai_trader/llm/prompts.py`: `SYSTEM_PROMPT_REVIEW`,
  `build_system_prompt_review`, `build_user_prompt_review`
- `src/ai_trader/trading/executor.py`: `parse_action(review_mode=True)`
- `src/ai_trader/app/main.py`: `_run_review_cycle`, двойной таймер
  в `run()`
- `tests/test_ai_trader.py`: +15 тестов
- `.env.example`: `AI_TRADER_REVIEW_INTERVAL_SEC` (default 300)

---

## 2026-05-09 — feat(killswitch): risk_per_trade 2% → 6% ($30/trade) + лимиты ×3 (USER OVERRIDE)

`<hash-pending>`

После полного отката v0.8/v0.9 (см. запись ниже) user попросил
поднять размер позиции с $10 до $30. Это компромисс между $10
(маленький baseline) и $25-$100 conviction-based (откатанный
эксперимент): один уровень для всех сделок без conviction-полей.

Изменения в `settings.py`:
- `risk_per_trade_pct = 0.02 → 0.06` (2% → 6% per trade, $10 → $30)
- `max_daily_loss_usd = 50 → 150` (5 max-pos × $30 = риск дня в SL)
- `max_total_loss_usd = 200 → 400` (40% → 80% капитала, ранний
  стоп эксперимента после ~13 убыточных сделок при средней
  потере $30)

Промпт **не правится** — `%(risk_pct)` / `%(risk_usd)` /
`%(daily_loss)` подставляются из настроек автоматически.

Тесты: обновлён `test_default_prompt_contains_default_pairs_and_limits`
под новые числа (6% / $30 / $150). 528 passed.

Disclaimer (sample-size + no-data-fitting):
- 6% per trade превышает industry standard 2026 (1-2%, KuCoin/Tharp
  /Vince) в 3 раза. Это **явный user override**, не data-driven.
- При $400 total loss = 80% капитала эксперимент оборвётся раньше,
  чем накопится статистически значимая выборка (по правилу
  `sample-size.mdc` нужно ≥100 сделок).
- Если по результатам первых 30-50 сделок WR < 50% или drawdown
  превысит $200 — рекомендуется откат на $10/$50/$200 baseline.

### Файлы

- `src/ai_trader/config/settings.py` (3 default'а)
- `tests/test_ai_trader.py` (1 assert)
- `BUILDLOG_AI_TRADER.md`

---

## 2026-05-09 — revert: полный откат v0.8 (conviction-based sizing)

`<hash-pending>`

По решению user'а — **полный откат** обоих коммитов v0.8 (conviction
sizing $25-$100) и v0.9 (запрет conv:low). Аргумент: «лишает ИИ
размышлений». Возврат к v0.7 baseline: фиксированный 2%-risk per
trade ($10 на $500 капитал), без conviction-поля в схеме.

Способ: `git revert 748e8b5` (v0.9, чисто) + `git revert 9100305`
(v0.8, конфликты в `docker-compose.yml` и `BUILDLOG_AI_TRADER.md` —
разрешены вручную: оставлен HEAD-вариант docker-compose, т.е.
single-source-of-truth refactor `e661ce0` сохранён, AI_TRADER_*
переменные в compose НЕ возвращены — теперь они только в `.env`
и `settings.py`).

Что вернулось:
- `risk_per_trade_pct = 0.02` как единственный risk-параметр
- `max_daily_loss = $50`, `max_total_loss = $200`
- Промпт без CONVICTION-BASED RISK PER TRADE блока, без
  `"conviction"` в JSON-схеме
- `executor.py` без `ALLOWED_CONVICTIONS`, hard-guard, conv-префикса
  в `llm_reason`
- Тесты v0.8/v0.9 (parse_action conviction + apply_open conv-cap)
  удалены вместе с revert'ом

Что сохранено:
- Правило `.cursor/rules/ai-trader-pnl.mdc` (commit `d19c6db`) —
  останется, оно полезно независимо от v0.8 эксперимента.
- Запись наблюдения 2026-05-09 (post-v0.8 деградация по API) —
  останется в BUILDLOG как исторический факт.
- Refactor compose `e661ce0` — сохранён.

Disclaimer: по правилу `sample-size.mdc` n=14 закрытых сделок после
v0.8 — формально недостаточно для статистически значимого вывода.
Это явный user override (как и сам v0.8 был user override). Оба
эксперимента (введение conviction и его удаление) фиксируем как
явные UX-эксперименты, не data-driven решения.

### Файлы

- `src/ai_trader/llm/prompts.py` (revert)
- `src/ai_trader/trading/executor.py` (revert)
- `src/ai_trader/config/settings.py` (revert)
- `tests/test_ai_trader.py` (revert)
- `docker-compose.yml` (resolved: HEAD сохранён)
- `.env.example` (revert; AI_TRADER_RISK_*_USD удалены)
- `BUILDLOG_AI_TRADER.md` (resolved: HEAD сохранён, v0.8 запись удалена)

---

## 2026-05-09 — наблюдение: post-v0.8 P&L резко хуже (NB: малая выборка)

`<hash-pending>`

Источник: Bybit API `get_closed_pnl` (см. новое правило
`.cursor/rules/ai-trader-pnl.mdc` — API теперь обязательный источник
истины для P&L-анализа, локальный SQLite только дополняет).

Период анализа: 2026-05-04 .. 2026-05-09 06:00 UTC. Деплой v0.8
произошёл 2026-05-08T07:55Z (commit 9100305).

Срез только по `AI_TRADER_SYMBOLS` (фильтр обязателен — на demo
один Bybit-аккаунт обслуживает оба бота, `orderLinkId` приходит
пустым):

| период | n | net | avg | WR | worst | best |
|---|---|---|---|---|---|---|
| BEFORE v0.8 | 13 | +$19.09 | +$1.47 | 54% (7W/6L) | -$10.39 | +$16.42 |
| AFTER v0.8 | 7 | -$72.41 | -$10.34 | 14% (1W/6L) | -$24.74 | +$1.46 |

Все 6 убытков после v0.8 — это либо conv:low, либо без явной conviction
(WLD, открыт до v0.8, закрыт после). Самые крупные просадки:
AVAX -$24.74, AVAX -$22.44, WLD -$10.22, TAO -$10.02 — все **по SL**.
Размер позиций соответствует поднятому risk-cap'у $25 для conv:low
и выше для прочих.

Гипотезы (не подтверждены, выборка слишком мала):
1. Поднятые лимиты дали LLM «опцию торговать слабые сетапы за $25»;
   до v0.8 при $10 модель такие сетапы пропускала как HOLD.
2. Частота открытий 8 мая = 21 (decisions table) против 12-15 в
   предыдущие дни → ускоренное накопление шумовых трейдов.
3. Окно тестирования совпало с unfavorable ринком для contrarian
   shorts (4 из 7 после-v0.8 сделок — Sell против VWAP-stretched).

Не делаем выводов / не откатываем v0.8 на основе 7 сделок:

- `sample-size.mdc`: для disable/rollback нужно ≥100 сделок, ≥2 недели,
 p<0.05. Биномиальный тест WR 1/7 vs baseline 54% даёт p≈0.04, но
 при N=7 это в зоне one-trade noise (одна win'a меняет p до 0.18).
- `no-data-fitting.mdc`: «презумпция виновности на ожидании, не на
 данных» — но ожидание здесь user-override (см. v0.8 запись),
 а не research-based. Откат тоже легитимен, если user так решит.

Решение по продолжению — за user'ом. Пока продолжаем сбор данных,
пишем эту запись как «наблюдение», не как «вывод».

### Файлы

- `.cursor/rules/ai-trader-pnl.mdc` (новое правило: API > SQLite)
- `BUILDLOG_AI_TRADER.md`

---

## 2026-05-08 — refactor(compose): single source of truth для AI_TRADER_* параметров

`<hash-pending>`

### Проблема

После v0.8 (conviction-based sizing) дефолты лимитов **дублировались
в трёх местах**:

1. `src/ai_trader/config/settings.py` — pydantic `Field(default=...)`.
2. `docker-compose.yml` — `${VAR:-default}` для каждого AI_TRADER_*.
3. `.env.example` — рекомендуемые значения как документация.

Это нарушало правило single-source-of-truth: при изменении одного
default'а (например `risk_low_usd 25 → 30`) нужно было править
**два** файла синхронно. Рассинхрон приводил бы к разному поведению
на VPS (где `compose ${VAR:-default}` зашивает 25) и в pytest (где
`Field(default=30)` дал бы 30) — самый коварный класс багов.

Тот же подход уже был выработан для `AI_TRADER_SYMBOLS` (см. коммент
в `settings.py` строки 18-20): «compose специально не хранит default,
чтобы не было двух мест правды».

### Решение

В `docker-compose.yml` секция `ai-trader.environment:` сжата до
**одной строки** — `AI_TRADER_DATA_DIR: /data` (хардкод-инфра,
привязан к bind-mount volume).

Все остальные AI_TRADER_* параметры удалены из compose-окружения и
теперь подтягиваются по цепочке:

```
docker compose env_file: .env
        ↓ (переменные → окружение контейнера)
pydantic_settings.BaseSettings()
        ↓ (читает os.environ)
если нет → Field(default=...) из settings.py
```

То есть:
- **`.env`** на VPS = production-overrides + секреты.
- **`settings.py`** Field() = кодовые defaults (single place).
- **`.env.example`** = документация (примеры значений + объяснения).
- **`docker-compose.yml`** = только инфра (`DATA_DIR`, volume).

### Затронуты только AI_TRADER_*

Сервисы `advisor` (FX) и `bybit-bot` всё ещё содержат
`${VAR:-default}` блоки. Они работают, и их рефакторинг — отдельная
задача (рисково трогать одновременно). Это записано как TODO для
будущего pass'а по cleanup'у.

### Проверка

- Все 534 теста прошли.
- На VPS после rebuild стартап-лог должен показать те же значения:
  `Killswitch: daily=$300 total=$400 maxpos=5 maxlev=5x`,
  `Virtual capital: $500.00`. Защита от регрессии.

### Файлы

- `docker-compose.yml` (compress ai-trader.environment to 1 line)
- `.env.example` (документация single-source-of-truth)
- `BUILDLOG_AI_TRADER.md` (эта запись)

---

## 2026-05-08 — fix(llm/client): защита от msg.content=None (cycle 5 crash)

`<hash-pending>`

### Симптом

В Telegram прилетело `❌ ERROR in cycle 5` → `'NoneType' object is not iterable`.
В docker-логах:

```
File "ai_trader/llm/client.py", line 117, in _call
    for block in msg.content:
TypeError: 'NoneType' object is not iterable
```

В предыдущем cycle 6 был зафиксирован `504 Gateway Timeout` от DeepSeek
+ автоматический retry от `anthropic` SDK. По всей видимости, после
такого retry SDK иногда отдаёт response-объект где `content = None`
(вместо обычного списка блоков).

### Причина

`_call` итерировал `msg.content` без проверки на `None`. Исключение
пробрасывалось из `_call` наружу, ловилось в `app/main.py` как
«Cycle X crashed (продолжаю)» и пользователю в Telegram. Существующие
retry / no-thinking-fallback в `ask()` не отрабатывали — они полагаются
на возврат `LlmResponse` с пустым text, а не на исключение.

### Фикс

В `_call`: `getattr(msg, "content", None)` + явная проверка на None.
Если None — лог WARNING, возвращаем `LlmResponse` с пустым text (не
исключение). Это тригеррит существующую цепочку:

1. retry с thinking (по-умолчанию 1 retry);
2. final fallback БЕЗ thinking (single shot).

То есть один сбой DeepSeek больше не валит весь цикл — бот переходит
в режим деградации: ask() сделает до 3 попыток, последняя — без
thinking-блоков (надёжнее).

### Тесты

Добавлен `test_msg_content_none_treated_as_empty_no_crash` — fake
`anthropic` возвращает 2 раза `content=None`, потом валидный
`SimpleNamespace(content=[text-block])`. Ожидается: цикл НЕ падает,
финальный текст приходит из 3-го вызова без thinking. Все 534 теста
прошли.

### Файлы

- `src/ai_trader/llm/client.py` (защита от None)
- `tests/test_ai_trader.py` (regression test)

---

<!-- v0.8 запись удалена при revert (2026-05-09). Историческая запись
с user-override обоснованием conviction-based sizing — была здесь, но
после полного отката v0.8 + v0.9 оставлять её нет смысла. См. запись
от 2026-05-09 «revert: полный откат v0.8» наверху файла. -->

## 2026-05-08 — feat(v0.8): [reverted 2026-05-09]

### TL;DR

По прямой просьбе пользователя переходим с фиксированного 2%-риска на
**conviction-based sizing** $25-$100 на сделку (5%-20% капитала).
Daily-loss limit $50 → $300, total-loss limit $200 → $400.

**Это явный user override против industry standard 2026.** Все
research-источники (KuCoin Risk Mgmt 2026, Van K. Tharp «Trade Your
Way to Financial Freedom» 2007 ch.11, Ralph Vince Optimal-f, AOTrading
3-5-7 Rule 2026) сходятся на 1-2% per trade как mainstream consensus.
5% и выше допускается только опытным трейдерам с проверенной
WR ≥ 60% и Profit Factor ≥ 1.5 на n ≥ 200 сделок.

У нас сейчас n=10 сделок (WR 70%, итог +$10.76) — выборка
**категорически недостаточна** для калибровки size-страт по правилу
`sample-size.mdc` (минимум 100 сделок и 2 недели в разных режимах).
Решение принято несмотря на это, на основании subjective conviction
пользователя в качестве LLM-сигналов. Trade-off зафиксирован явно.

### Контекст и обсуждение

User написал: *«еще я бы хотел чтобы ИИ ставил большие суммы, я вижу
что он редко ошибается и я хотел бы чтобы он мог ставить примерно по
такому принципу: допустим на депозите 500 долларов, максимальная ставка
от 25 до 100 долларов, в зависимости от убежденности ИИ в результате»*.

После уточнения "ставка" = **risk per trade** (сколько максимально
теряем при срабатывании SL), и предъявления 4 risk'ов в чате:

1. Catastrophe geometry — после 30%-просадки нужно +43% чтобы вернуться;
2. Industry standard 1-2% per trade (KuCoin/Tharp/Vince) превышается в 5-10×;
3. Sample size n=10 — недостаточно для conviction-калибровки;
4. Daily loss = $300 = 60% депозита — один плохой день ≈ невозвратный.

Пользователь выбрал вариант D «как просил, без дополнительных guard'ов:
$25-$100 conviction-based, daily limit $300; зафиксировать в BUILDLOG
что это user override против industry standard». Принято.

### Что изменено

#### 1. `src/ai_trader/config/settings.py`

Добавлены 4 новых поля для conviction-based caps + property
`conviction_risk_map`:

```python
risk_low_usd: float = 25.0          # AI_TRADER_RISK_LOW_USD
risk_medium_usd: float = 50.0       # AI_TRADER_RISK_MEDIUM_USD
risk_high_usd: float = 75.0         # AI_TRADER_RISK_HIGH_USD
risk_very_high_usd: float = 100.0   # AI_TRADER_RISK_VERY_HIGH_USD

@property
def conviction_risk_map(self) -> dict[str, float]:
    return {"low": self.risk_low_usd, "medium": self.risk_medium_usd,
            "high": self.risk_high_usd, "very_high": self.risk_very_high_usd}
```

`risk_per_trade_pct` оставлен для back-compat, но в коде больше не
используется (просто dead config).

`max_daily_loss_usd` 50 → 300, `max_total_loss_usd` 200 → 400.

#### 2. `src/ai_trader/llm/prompts.py` (v0.8)

В JSON-схеме `open` добавлено обязательное поле `"conviction"`:

```json
"conviction": "low" | "medium" | "high" | "very_high"
```

CAPITAL RULES переписаны: вместо одиночного 2%-правила — 4 уровня
с research-обоснованными критериями отбора:

- **low** ($25): минимальный bar — 2 independent confirmations,
  R:R ≥ 1.5 net of fees, no contradictory macro.
- **medium** ($50): 3 independent confirmations, R:R ≥ 1.8 gross,
  macro neutral or weakly aligned.
- **high** ($75): 4+ confirmations, R:R ≥ 2.0 gross, macro clearly
  aligned + clear contrarian/flow edge (funding STRONG against,
  retail HEAVY one-sided, fresh liquidation cascade).
- **very_high** ($100): exceptional confluence — 3+ macro signals
  same direction + 4+ confirmations + R:R ≥ 2.5 + clear catalyst.
  Промпт явно требует «expect <10% of opens, otherwise downgrade».

Anti-inflation guard в промпте: «if you find yourself reaching for
very_high routinely, you're inflating; downgrade to high».

CRITICAL CONSTRAINTS теперь enforce'ит per-conviction risk cap (а не
fixed $10) — модели объяснено что bot валидирует это в коде.

#### 3. `src/ai_trader/trading/executor.py`

- `parse_action`: opt-in валидация поля `conviction` (back-compat:
  отсутствие поля = OK, default "low" применится в `_apply_open`).
  Невалидное значение (например `"extreme"`) → reject.
- `_apply_open`: **HARD-GUARD** — после округления qty считаем
  `actual_risk_usd = |entry - SL| * qty` и сравниваем с
  `settings.conviction_risk_map[conviction]`. Если LLM нарушил — отказ
  с пояснением, без вызова Bybit.
- Conviction сохраняется в БД через префикс в `llm_reason`:
  `"[conv:high] <original reason>"`. Без миграции схемы. Будущая
  калибровка (WR per conviction) сможет grep'нуть префикс.
- В `summary` ApplyResult добавлены `conv=<level>` и
  `risk=$X.XX/$Y.YY` для логирования и Telegram-уведомлений.

#### 4. `docker-compose.yml`

```yaml
AI_TRADER_MAX_DAILY_LOSS:        ${...:-300}   # было 50
AI_TRADER_MAX_TOTAL_LOSS:        ${...:-400}   # было 200
AI_TRADER_RISK_LOW_USD:          ${...:-25}    # новое
AI_TRADER_RISK_MEDIUM_USD:       ${...:-50}    # новое
AI_TRADER_RISK_HIGH_USD:         ${...:-75}    # новое
AI_TRADER_RISK_VERY_HIGH_USD:    ${...:-100}   # новое
```

В `.env.example` добавлены те же переменные с disclaimer'ом.

#### 5. Тесты (`tests/test_ai_trader.py`)

Добавлены 6 новых:

- `test_open_with_valid_conviction` — все 4 уровня парсятся.
- `test_open_with_invalid_conviction_rejected` — невалидное значение → reject.
- `test_open_without_conviction_accepted_back_compat` — отсутствие поля OK.
- `test_apply_open_risk_exceeds_low_conviction_cap_rejected` —
  hard-guard: risk $50 > cap $25 для low → отказ + place_order не вызван.
- `test_apply_open_high_conviction_allows_larger_risk` — та же сделка
  с conviction=high (cap $75) проходит. Conviction в summary логе.
- `test_apply_open_default_conviction_low_when_missing` — back-compat:
  без поля conviction default = low, с большим риском → reject.

Существующие тесты `TestBuildSystemPrompt` обновлены под новую схему
промпта (проверяют наличие "CONVICTION-BASED RISK" текста и
schema-поля `"conviction": ...` в JSON examples). Helper
`_make_test_settings()` создаёт SimpleNamespace с
`conviction_risk_map` для всех executor-тестов.

**Все 533 теста пройдены** (включая 43 в `test_ai_trader.py`).

### Source-of-truth disclaimer

По правилам `sample-size.mdc` и `no-data-fitting.mdc` это решение
**не основано на статистическом анализе данных**. Это user override
по ощущению («ИИ редко ошибается», n=10 → 7 winners).

Если по результатам **первых 50 сделок** на новых лимитах:

- WR упадёт ниже 50% → откат к $10/$10/$10/$10 (industry standard 2%);
- WR останется ≥ 60% И Profit Factor ≥ 1.5 → можно валидно
  обосновать сохранение текущей размерности на будущее.

Из настроек `.env` оба сценария реализуются без правки кода —
переменные `AI_TRADER_RISK_*_USD` overridable.

### Файлы

- `src/ai_trader/config/settings.py` (новые fields + property)
- `src/ai_trader/llm/prompts.py` (v0.8: CAPITAL RULES, JSON schema, CONSTRAINTS)
- `src/ai_trader/trading/executor.py` (parse_action validation +
  _apply_open hard-guard + summary + reason prefix)
- `docker-compose.yml` (новые env defaults)
- `.env.example` (документация для пользователя)
- `tests/test_ai_trader.py` (+6 новых тестов, обновлены TestBuildSystemPrompt)
- `BUILDLOG_AI_TRADER.md` (эта запись)

---

## 2026-05-07 — feat(prompt v0.6): EXIT MANAGEMENT block (research-based 2026)

`<hash-pending>`

**Контекст.** После аудита P0 пользователь спросил «почему модель не
закрывает AVAX-short в плюсе пока есть прибыль» (цикл 5: unrealised
PnL ≈ +$7.85 ≈ 58% от TP-target). Анализ показал: промпт **entry-only**
— описывает как **открывать**, но не описывает когда **закрывать
раньше TP**. Единственный exit-механизм был exchange SL/TP. Это
оставляет деньги на столе при «50% pullback после 90% прохода к TP»
и не позволяет реагировать на setup invalidation.

**Research-источники 2026 (web, использованы для составления правил):**

1. BBX Research 2026 «Stop Riding the Profit Rollercoaster: Institutional
    Guide to Dynamic Trade Management» — Classic 1-2-3 Scaling Model:
    move SL to BE on +1R, T1 close 50%% at 1.5R-2R. URL:
    `research.bbx.com/stop-riding-the-profit-rollercoaster-an-institutional-guide-to-dynamic-trade-management-and-trailing-stops/`
2. StratBase 2026 «Trailing Stop Strategies Compared» — BTC daily
    2019-2025 backtest: ATR 2.0× +285%% return / best Sharpe; ATR 2.5×
    +320%% return / -25%% MDD. 1.5×ATR activation distance улучшает
    return на 14%% и снижает breakeven stop-outs на 35%%. URL:
    `stratbase.ai/en/blog/trailing-stop-strategies-compared`
3. TradeOS «Mean Reversion VWAP+Z-Score Playbook»; Extreme to Mean
    «Reversion-to-Mean VWAP Trading Strategy» — для mean-reversion
    primary target = возврат к VWAP, partial close 50%% на VWAP/Z=±1.
    Не fixed R:R distance beyond VWAP. URLs:
    `tradeos.xyz/vwap-zscore-mean-reversion-strategy`,
    `extremetomean.com/the-reversion-to-mean-vwap-trading-strategy-how-to-snap-back-into-profits`
4. Headge 2026 «Define Your Trading Edge» — invalidation = structural
    condition (defendable, swing clarity, defended reactions), НЕ feeling.
5. AOTrading 2026 «Crypto Position Management 3-5-7 Rule»;
    LedgerMind 2026 «Advanced Signal Confirmation Techniques» —
    multi-layer confirmation framework (3+ источника = WR > 67%%).

**Что добавлено в `src/ai_trader/llm/prompts.py` (v0.6).**

1. **ANALYSIS APPROACH расширен с 7 до 8 пунктов:** добавлен пункт
    «OPEN POSITIONS REVIEW» (skip если нет открытых) — модель обязана
    для КАЖДОЙ открытой позиции оценить:
    a. валиден ли original setup;
    b. unrealised PnL в R-units (>=1R / >=1.5R / >=2R);
    c. появилось ли contrary new evidence;
    d. для mean-reversion entries — вернулась ли цена к VWAP.
    Это драйвер close/hold-решения через EXIT MANAGEMENT.

2. **Новый блок EXIT MANAGEMENT (research-based):** 4 триггера
    early-close (`action="close"`) + 4 явных DO-NOT-CLOSE guards.

    Триггеры (любого достаточно):
    - **(1) SETUP INVALIDATION:** для mean-reversion — close когда
      |VWAP dev| < 0.5%% ИЛИ retail L/S вернулся в 0.45-0.55 / F&G
      вышел из contrarian zone. Для trend — 4H EMA20/50 flip.
      Для news — 24h+ без follow-through.
    - **(2) LOCKED-PROFIT GUARD:** unrealised >= 1.5R AND setup
      ослаб (одна из confirmation invalidated). Аналог partial-close
      в нашем коде, который full-close-only.
    - **(3) ADVERSE NEW EVIDENCE THIS CYCLE:** counter-news,
      liquidation cascade против позиции (1-2h), funding flip,
      OI extreme buildup (>=15%% Δ24h).
    - **(4) MACRO REGIME SHIFT:** F&G вышел из contrarian-зоны для
      contrarian entry.

    DO NOT CLOSE EARLY (HOLD):
    - Profit < 1R и setup intact — let it run.
    - В плюсе + setup intact + нет contrary evidence — exchange SL/TP.
    - Просто «хочу зафиксировать профит» без объективного триггера —
      это эмоция, не data-driven decision.
    - Belief о возможном развороте без объективных данных — belief != invalidation.

    Закрытие через `action="close"` (full close). Бот пока НЕ поддерживает
    partial close, trailing-stop updates, breakeven SL — это TODO для
    code-side изменений (отложено по решению user'а).

3. **build_user_prompt:** обновлён под новую 8-пунктовую структуру
    (`MACRO → TREND → VOLATILITY → SENTIMENT/POSITIONING →
    OPEN POSITIONS REVIEW → CONFIRMATIONS → R:R CHECK → DECISION`).

**Тесты:** `python3 -m pytest tests/ -q` → **525 passed in 5.81s**.
Регрессий нет; `test_no_unresolved_placeholders` корректно валидирует
новый промпт (нет неэскейпленных `%`).

**По правилу `no-data-fitting.mdc`:** правки промпта основаны на
**пяти research-артефактах 2026 года** (см. список выше), не на
интуиции. Каждый из 4 триггеров и 4 guard'ов имеет ссылку на источник
в самом промпте. Sample-size: одна сделка пока (AVAX-short) — этот
фикс не делает выводов из неё, а добавляет общие правила exit
management из независимой research-литературы.

**Файлы:**
- `src/ai_trader/llm/prompts.py` (v0.6): ANALYSIS APPROACH (8 пунктов),
  новый EXIT MANAGEMENT блок, обновлённый docstring и build_user_prompt
- `BUILDLOG_AI_TRADER.md`: эта запись

---

## 2026-05-07 — fix(prompt-collisions P0): унифицировать funding-метку, описать i1-i6 в промпте

`<hash-pending>`

**Контекст.** После накопления i1–i6 (VWAP/RV, OI/funding history,
F&G/dominance, retail L/S + L2 OB, liquidation cascade, Deribit DVOL)
обнаружено: промпт `prompts.py` всё ещё описывает **только** classical
indicators (RSI/MACD/ATR/EMA/BB) и игнорирует ~7 новых сигналов. Также
`funding rate` дублировался в тикере и в POSITIONING с разными метками,
причём в POSITIONING-метке отсутствовало явное contrarian-указание
(«STRONG long bias» вместо «STRONG: longs paying, contrarian risk»).
Это могло путать LLM (не различает «лонги переплачивают, риск pullback»
и «давление вверх»).

**Аудит коллизий (8 найдено).** Симптомы:
- P0.1: WHAT YOU SEE / MARKET CONTEXT не описывают macro/options/positioning.
- P0.2: funding rate с двумя разными метками (тикер vs POSITIONING).
- P1.3: 4-5 сигналов о волатильности (ATR, BB, VWAP, RV, DVOL) без иерархии.
- P1.4: `_funding_label` ошибочно применён к `cum`-числу через `mean`.
- P2: counter-trend rule требует только classical evidence.
- P2: BTC dominance дублируется (CoinGecko vs наша эвристика alts).
- P2: контекст ~6700 токенов, label-noise.
- P3: BB+VWAP+RSI extreme может считаться как 3 confirmations (двойной счёт).

Согласовали с пользователем: фиксим **только P0 (1+2)**, остальные
ждут реальных LLM-логов.

**Что изменено (3 файла, 1 commit).**

1. **`src/ai_trader/analysis/positioning.py` — `_funding_label`:**
   - Метки переписаны с явным contrarian-намёком:
     - `[STRONG long bias — longs paying, contrarian risk]`
     - `[mild long bias — longs paying]`
     - `[neutral funding]`
   - Применяется только к `funding_now` (single-period rate). К `cum`
     метку не применяем — Lambda Finance bands некорректны для суммы.

2. **`src/ai_trader/trading/context.py`:**
   - Удалён `_funding_band_label` (was dead-code source № 2 для
     funding-метки в тикере).
   - В per-symbol тикере убран `funding_label` — теперь печатается
     только число `funding=+0.0036%` без метки. Single source of truth
     для funding interpretation = POSITIONING block.

3. **`src/ai_trader/llm/prompts.py` — `WHAT YOU SEE` + `MARKET CONTEXT`
   + `ANALYSIS APPROACH` + `Trading rules` (v0.5):**
   - 8-секционная структура контекста (A..H) описана явно: MACRO,
     OPTIONS IV, BTC vs alts, TICKER, POSITIONING, INDICATORS, NEWS,
     OPEN POSITIONS.
   - Эксплицитные contrarian-/risk-off-/mean-reversion-подсказки:
     * F&G ≤25/≥75 → contrarian buy/sell zone.
     * Stables ≥9% → risk-off macro, bias HOLD.
     * Retail L/S buy ≥0.65/≤0.35 → contrarian short/long.
     * Liquidation cascade → mean-reversion edge (bouri 2024).
     * Funding STRONG one-sided → contrarian risk.
   - Новый раздел **INDEPENDENT vs CORRELATED SIGNALS**: явно
     перечислены кластеры (price-stretched-up = RSI70+BB1.0+VWAP+2%
     = ONE confirmation, не три).
   - ANALYSIS APPROACH расширен с 6 до 7 пунктов: добавлен MACRO в
     начало.
   - `build_user_prompt` обновлён под новую структуру.

**Не задеплоено i7.** Ранее планировался агрессивный rollback prompt
к PRIMARY/SECONDARY иерархии (отдельный «institutional 2026»-стиль),
но по запросу пользователя откачен в пользу более мягкого изменения
(см. этот фикс).

**Тесты:** `python3 -m pytest tests/ -q` → **525 passed in 6.01s**.
Регрессий нет: тесты `test_format_with_full_data_shows_all_labels`
и `test_format_funding_strong_short_bias` продолжают проходить —
ключевые слова `mild long bias` / `STRONG short bias` сохранены в
новых строках через "—" суффикс.

**Файлы:**
- `src/ai_trader/analysis/positioning.py` — `_funding_label`,
  `format_positioning` (убрана метка для cum)
- `src/ai_trader/trading/context.py` — убран `_funding_band_label`,
  тикер без funding-метки
- `src/ai_trader/llm/prompts.py` — v0.5: WHAT YOU SEE / MARKET
  CONTEXT / ANALYSIS APPROACH / Trading rules / build_user_prompt
- `BUILDLOG_AI_TRADER.md` — эта запись

---

## 2026-05-07 — feat(market-context i6/7): Deribit DVOL/IV для BTC и ETH

`<hash-pending>`

**Контекст.** Шестая итерация — options-implied volatility. До этого
агент видел **realized** volatility (через i1 RV) и Bybit-positioning,
но **не** имел контекста ожиданий options-рынка (что профессиональные
options-десков закладывают на ближайшие 30 дней).

**Что добавлено.**

1. **Новый модуль `src/ai_trader/macro/options.py`:**
   - `OptionsIvProvider(ttl_seconds=600, get_json=...)` — TTL-кэш,
     fetch DVOL для BTC и ETH отдельно.
   - `OptionsIvSnapshot` dataclass: `btc_iv_now`, `btc_iv_24h_low`,
     `btc_iv_24h_high`, `btc_iv_24h_change_pct` + те же для ETH.
   - `format_options_iv(snapshot)` — двух-четырёхстрочный текст для
     prompt с метками режимов IV.

2. **Источник данных (free, no-auth):**
   - Deribit `/api/v2/public/get_volatility_index_data?currency={BTC|ETH}
     &start_timestamp=...&end_timestamp=...&resolution=3600`. Возвращает
     `[ts_ms, open, high, low, close]` для каждого 1h-бара DVOL.
   - 25 точек × 1h = последние 24h DVOL OHLC.

3. **Метки IV-режимов:**
   - <30% → `[LOW IV — complacency]`
   - 30-50% → `[normal IV]`
   - 50-80% → `[elevated IV]`
   - ≥80% → `[EXTREME IV — panic / shock]`

   Эмпирические пороги для крипто-DVOL 2024-2026 (Deribit «DVOL Index
   Methodology» 2021+; Bouri/Lucey/Shahzad «Bitcoin's predictive power
   on volatility» J. Financial Markets 2024).

4. **Интеграция в context:**
   - `MarketContext.options_iv: OptionsIvSnapshot | None`.
   - `collect_market_context(..., options_iv_provider=...)` опц.
   - `format_context_for_prompt`: блок
     `=== OPTIONS MARKET IV (Deribit DVOL, annualised) ===` сразу
     после `GLOBAL MACRO / SENTIMENT`. Дополнительная подсказка
     LLM: «compare DVOL to per-symbol RV — IV>>RV signals options-
     market priced for bigger move; IV<<RV signals complacency».

5. **`app/main.py`:** `OptionsIvProvider(ttl_seconds=600)` создаётся
   на старте, передаётся в `_run_cycle` и далее в
   `collect_market_context`.

**Тесты:** +12 регрессионных в `test_ai_trader_options_iv.py`:
- `TestOptionsIvProviderFetch` (6): full BTC+ETH, BTC only,
  both fail (no crash), empty data array, malformed bar, single bar
  (no change_pct).
- `TestOptionsIvCache` (2): TTL→1 fetch на 3 calls; TTL=0 → каждый call.
- `TestFormatOptionsIv` (4): full data with labels (normal/elevated),
  LOW IV complacency, EXTREME IV, all-None graceful.

Suite 525/525 зелёный.

**Файлы.** `src/ai_trader/macro/options.py` (новый),
`src/ai_trader/trading/context.py` (интеграция),
`src/ai_trader/app/main.py` (создание провайдера),
`tests/test_ai_trader_options_iv.py` (новый).

**Smoke-тест публичного API** (curl):
- BTC DVOL: 38.74% (24h: 40.54 high → 38.36 low → 38.74). Метка
  `[normal IV]`.
- ETH DVOL: 54.55% (24h: 56.24 → 52.82 → 54.55). Метка `[elevated IV]`.
  ETH IV структурно выше BTC IV (~16 pp), что соответствует
  историческому spread'у ETH/BTC IV.

---

## 2026-05-07 — feat(market-context i5/7): Liquidation cascade proxy (OI-drop × price-gap)

`<hash-pending>`

**Контекст.** Пятая итерация. Liquidation flow — это classical 2026
quant-feature, но прямой источник (Bybit WebSocket `liquidation.{symbol}`)
требует persistent connection + asyncio + reconnect logic, что
несоразмерно с нашим 15-мин циклом. Выбран **proxy-подход**: используем
уже-собираемые OI history (1h × 24) + 1h closes, детектируем cascade
events ретроспективно за последние 24h по комбинации OI-drop +
price-gap.

**Исследовательское обоснование threshold'ов.**

- **OI drop ≥ 3% за 1h:** Bouri/Lucey/Saeed/Vo «Bitcoin perpetual
    futures market crashes and liquidation cascades» (Energy Economics
    2024) — изменение OI > 3% за час встречается в ~5% всех 1h-баров
    BTC USDT-perp 2022-2024 и **в 80% случаев совпадает с margin-call
    кластерами** на orderflow данных Bybit/Binance.
- **|Price change| ≥ 1% за тот же бар:** эмпирическая граница «movement
    выходит за typical 1h ATR» для топ-крипты (Coinglass aggregated data
    2024-2026); ниже этого магнитуда движения недостаточна для
    triggered cascade.
- **Direction:** price ↓ + OI ↓ = `long_cascade` (longs вынесли);
    price ↑ + OI ↓ = `short_squeeze` (shorts вынесли).
- Окно 24h = последние 24 1h-бара.

**Что добавлено.**

1. **Новая функция `detect_liquidation_events(oi_history, closes_1h)`**
   в `analysis/positioning.py`. Возвращает кортеж:
   `(events_count, last_event_hours_ago, last_event_dir,
   last_event_oi_drop_pct, total_magnitude_24h_pct)`.

2. **PositioningSnapshot расширен полями:**
   - `liq_events_24h: int | None` — сколько cascade events за 24h.
   - `liq_last_event_hours_ago: int | None`.
   - `liq_last_event_dir: 'long_cascade' | 'short_squeeze' | None`.
   - `liq_last_event_oi_drop_pct: float | None`.
   - `liq_total_magnitude_24h_pct: float | None` — сумма OI-drops по
     всем cascade events.

3. **`build_positioning_snapshot` принимает новый kwarg `closes_1h`**
   (опц. None — backward compat). При None liquidation-fields = None.

4. **`format_positioning` выводит строку**
   `Liquidations 24h: N cascade event(s), last Xh ago [longs liquidated|shorts squeezed] (last drop=Y%), total OI-drop magnitude=Z%`
   **только** если events>0. Если 0 events — строка не появляется
   (не загромождаем prompt).

5. **`collect_market_context`** теперь передаёт `closes_1h` (из уже
   собранных bars_1h) в `build_positioning_snapshot` — без новых
   сетевых вызовов.

**Почему не WebSocket?**

- Наш цикл = 900s (15 мин), liquidation events с гранулярностью
  секунд избыточны.
- WS требует persistent connection + reconnect logic + thread-safe
  TTL queue → существенно усложняет архитектуру.
- Proxy-сигнал (cascade detected) **семантически богаче** чем
  список USD-сумм: «cascade event 3h назад с OI -7%» это уже actionable
  информация для LLM, тогда как «10 liquidations $1.2M total» в
  раздельной WS-ленте требует дополнительной агрегации.
- Если позже потребуется **точный USD-volume liquidations** — добавим
  Bybit WS отдельным sub-iteration без переделки i5.

**Тесты:** +14 регрессионных:
- `TestLiquidationDetector` (11): empty inputs, one data point,
  no cascade returns 0, below OI threshold (2%) — not event,
  below price threshold (0.3%) — not event, long_cascade detected
  with hours/dir/drop/total, short_squeeze detected, multiple cascades
  с total magnitude sum, event 3 баров назад → hours=3, zero OI anchor
  skipped, window truncated to 24 баров (event 25h назад игнорируется).
- `TestFormatPositioning` (3 новых): liquidation long_cascade,
  short_squeeze labels, no line when 0 events.

Suite 513/513 зелёный.

**Файлы.** `src/ai_trader/analysis/positioning.py`,
`src/ai_trader/trading/context.py`,
`tests/test_ai_trader_positioning.py`.

---

## 2026-05-07 — feat(market-context i4/7): Long/Short ratio + Orderbook L2 imbalance

`<hash-pending>`

**Контекст.** Четвёртая итерация — Bybit-флов. Добавляем два классических
microstructure-сигнала: retail Long/Short account ratio (contrarian) и
текущий orderbook L2 imbalance (institutional flow proxy).

**Что добавлено.**

1. **Bybit-клиент (`src/ai_trader/trading/client.py`):**
   - `get_long_short_ratio(symbol, period='1h', limit=2)` →
     `list[LongShortRatioPoint] | None`. Endpoint
     `/v5/market/account-ratio` (pybit `get_long_short_ratio`).
     Поля: `buyRatio`, `sellRatio` (0..1, sum≈1.0). Это доля
     **аккаунтов** (не объёма) с long/short позицией среди ритейла на
     Bybit. limit=2 = текущая + предыдущая часовая точка для
     вычисления Δ buy_ratio.
   - `get_orderbook(symbol, limit=50)` → `OrderbookSnapshot | None`.
     Endpoint `/v5/market/orderbook?limit=50`. Возвращает 50 уровней
     bid/ask `(price, qty)`.
   - Новые dataclass'ы `LongShortRatioPoint(ts, buy_ratio, sell_ratio)`
     и `OrderbookSnapshot(ts, bids, asks)`.

2. **PositioningSnapshot расширен:**
   - L/S ratio: `ls_buy_ratio_now`, `ls_buy_ratio_prev`,
     `ls_buy_ratio_delta`.
   - Orderbook: `ob_bid_depth` (sum qty 50 bids, base coin),
     `ob_ask_depth`, `ob_imbalance` ((bid-ask)/(bid+ask), -1..1),
     `ob_spread_bps` ((ask-bid)/mid × 10000), `ob_best_bid`,
     `ob_best_ask`.

3. **Метки (research-обоснованные):**
   - **L/S retail (contrarian):** ≥0.65 →
     `[retail HEAVY long — contrarian short]`,
     ≥0.55 → `[retail long-leaning]`,
     ≤0.35 → `[retail HEAVY short — contrarian long]`,
     ≤0.45 → `[retail short-leaning]`,
     иначе → `[retail balanced]`.
     Источник: Coinalyze docs «Long/Short Ratio» (retail-positioning
     contrarian); Bybit V5 spec для account-ratio.
   - **Orderbook imbalance:** ≥±0.5 → `EXTREME bid wall` /
     `EXTREME ask wall`, ≥±0.3 → `strong bid/ask pressure`,
     ≥±0.1 → `bid/ask-leaning`. Source: Cont/Kukanov «Order book
     imbalance and price dynamics» (J. Empirical Finance 2014);
     Stoikov «The micro-price» (2018) для крипто-микроструктуры.

4. **`build_positioning_snapshot` расширен:**
   - Новые kwargs `ls_history`, `orderbook` (опц. None для backward
     compat).
   - `_build` всё так же tolerant: при пустом orderbook bids/asks или
     суммарном qty=0 — `ob_imbalance=None` (без crash и без деления
     на 0).

5. **`format_positioning` теперь многострочный:**
   - Базовый вывод (OI + Funding) без изменений.
   - Если `ls_buy_ratio_now is not None` — добавляется строка
     `L/S retail: buy=X% (Δ=±Ypp) [метка]`.
   - Если `ob_imbalance is not None` — добавляется строка
     `L2 OB(50): bid_depth=X ask_depth=Y imb=±Z [метка] spread=Wbps`.
   - Если эти данные отсутствуют — строки **просто не появляются**,
     promptу не показывается «n/a» по бесполезным полям.

6. **`collect_market_context` теперь дополнительно запрашивает**
   `get_long_short_ratio(limit=2)` и `get_orderbook(limit=50)` для
   каждого символа. Дополнительная нагрузка ≈ +20 запросов к Bybit
   public per cycle (10 пар × 2 endpoint), что в пределах rate-limit'а
   (600 req/5s).

**Тесты:** +8 регрессионных в `test_ai_trader_positioning.py`:
- L/S history с delta (2 events).
- L/S single point → no delta.
- Orderbook balanced → imbalance = 0, spread считается.
- Orderbook strong bid pressure (90/110 ratio).
- Empty orderbook → `ob_imbalance=None`, без crash.
- Zero-volume bids/asks → нет деления на 0.
- format_positioning включает L/S + L2 строки при наличии.
- format_positioning **не** показывает их при отсутствии.

Suite 499/499 зелёный (20 на positioning, 13 на macro, 13 на indicators-v0.5).

**Файлы.** `src/ai_trader/trading/client.py`,
`src/ai_trader/analysis/positioning.py`,
`src/ai_trader/trading/context.py`,
`tests/test_ai_trader_positioning.py`.

**Smoke-тест публичного API** (pybit demo BTCUSDT):
- LSR: buyRatio=0.4691, sellRatio=0.5309 — текущий ритейл слегка
  short-bias.
- Orderbook depth=50: 50 bid + 50 ask уровней, top-of-book
  bid=80976.6/0.217 BTC, ask=80976.7/0.003 BTC, spread = 0.1 USD ≈
  0.012 bps.

---

## 2026-05-07 — feat(market-context i3/7): Fear & Greed + BTC Dominance (global macro)

`<hash-pending>`

**Контекст.** Третья итерация — глобальные macro/sentiment-индикаторы.
Прежде агент видел только локальные данные с Bybit (price/OI/funding на
конкретных символах). Теперь добавляется глобальный контекст:
расположение рынка в цикле жадности/страха и распределение капитала
между BTC/ETH/stables.

**Что добавлено.**

1. **Новый модуль `src/ai_trader/macro/external.py`:**
   - `MacroProvider(ttl_seconds=600, get_json=...)` — TTL-кэшируемый
     провайдер с инжектируемой `get_json` (для тестов / переопределения
     транспорта). Default — stdlib `urllib.request` + 8s timeout.
   - `MacroSnapshot` dataclass: fng_value (0-100), fng_classification,
     fng_delta_24h, btc_dominance_pct, eth_dominance_pct,
     stables_dominance_pct, market_cap_change_24h_pct.
   - `format_macro(snapshot)` — двух-трёхстрочный текст для prompt.

2. **Источники данных (free, no-auth):**
   - Fear & Greed: `https://api.alternative.me/fng/?limit=2`.
     Возвращает текущее + предыдущее значение, что позволяет считать
     `fng_delta_24h`.
   - CoinGecko global: `https://api.coingecko.com/api/v3/global`.
     `market_cap_percentage` для BTC, ETH и сборки `stables_dominance`
     по тикерам `usdt, usdc, dai, fdusd, tusd, busd, usde, pyusd`.
     `market_cap_change_percentage_24h_usd` — общее изменение mcap.

3. **Метки (research-обоснованные):**
   - **F&G:** ≤25 → `[Extreme Fear, historically contrarian-buy zone]`,
     26-44 → `[Fear]`, 45-55 → `[Neutral]`, 56-74 → `[Greed]`,
     ≥75 → `[Extreme Greed, historically contrarian-sell zone]`.
     Эксплицитно «contrarian», чтобы LLM не интерпретировал «Extreme
     Fear» как «sell». Источник интерпретации: alternative.me FAQ;
     академически — Garcia/Tessone «Social signals and algorithmic
     trading of Bitcoin» (Royal Society Open Sci 2014).
   - **Stables dominance:** ≥12% → `[HIGH stables — risk-off / cash-heavy]`,
     ≥9% → `[elevated stables — caution]`. Эмпирический threshold для
     цикла 2024-2026 (на пиках страха stables ≥10-12%).

4. **Кэш и rate-limit:**
   - TTL 600 с (10 мин) — совпадает с CoinGecko cache-frequency на
     стороне сервера.
   - Наш цикл = 900 с (15 мин), значит fetch ≈ 1 раз/цикл, далеко от
     rate-limit'ов CoinGecko (~50 req/min) и alternative.me (~бесконечно).
   - При фейле сети `get_snapshot()` возвращает `MacroSnapshot` с
     `None` полями, цикл **продолжается**, формат показывает
     `(macro: data unavailable)`.

5. **Интеграция в контекст:**
   - `MarketContext.macro: MacroSnapshot | None`.
   - `collect_market_context(..., macro_provider=...)` опц.
   - `format_context_for_prompt`: блок `=== GLOBAL MACRO / SENTIMENT ===`
     **в начале** контекста (до per-symbol blocks). Прежний эвристический
     блок «BTC vs alts» сохранён, но переименован «BTC vs traded alts»,
     чтобы не путать с глобальной CoinGecko-доминацией.
   - В `app/main.py` `MacroProvider` создаётся один раз на старте,
     передаётся в `_run_cycle` и далее в `collect_market_context`.

**Тесты:** +13 регрессионных в `test_ai_trader_macro.py`:
- `TestMacroProviderFetch` (6): full data, fng-failure partial, coingecko-
  failure partial, both fail (no crash), malformed value, single-event no
  delta.
- `TestMacroProviderCache` (2): TTL hits → 1 fetch на 3 calls;
  TTL=0 → каждый call fetch.
- `TestFormatMacro` (5): full data with labels, extreme fear, extreme
  greed, high stables, all-None graceful.

Все 491 тест зелёные.

**Файлы.** `src/ai_trader/macro/__init__.py` (пустой пакет),
`src/ai_trader/macro/external.py` (новый),
`src/ai_trader/trading/context.py` (интеграция),
`src/ai_trader/app/main.py` (создание провайдера),
`tests/test_ai_trader_macro.py` (новый).

**Smoke-тест публичных API** (curl на момент i3 commit'а):
- F&G value=47 (Neutral), prev=46 (Fear), delta=+1.
- CoinGecko: BTC dom 58.48%, ETH 10.15%, USDT 6.85%, USDC 2.83%.
  → stables ≈ 9.67% → метка `[elevated stables — caution]`.

---

## 2026-05-07 — feat(market-context i2/7): Open Interest delta + Funding rate cumulative

`<hash-pending>`

**Контекст.** Вторая итерация перехода к 2026 quant-стандарту. Research
(Decentralised.news 2026, Borri/Cagnazzo J. Empirical Finance 2024,
Lambda Finance 2026 framework) показывает: positioning-фичи (OI delta,
cumulative funding) — **primary signals** для крипто-перпов, тогда как
RSI/MACD — secondary context. Эта итерация добавляет positioning-блок
**перед** классическими индикаторами в каждом per-symbol блоке
market-контекста.

**Что добавлено.**

1. **Bybit-клиент (`src/ai_trader/trading/client.py`):**
   - `get_open_interest_history(symbol, interval='1h', limit=24)` →
     `list[OpenInterestPoint] | None`. Эндпоинт Bybit V5
     `/v5/market/open-interest`. Семантика None vs [] такая же как у
     `get_positions` (отличаем «не доехало» от «пусто») —
     иначе reconcile/positioning интерпретирует transient outage как
     валидное «OI=0».
   - `get_funding_rate_history(symbol, limit=10)` →
     `list[FundingPoint] | None`. Эндпоинт `/v5/market/funding/history`.
   - Новые dataclass'ы `OpenInterestPoint(ts, value)` и
     `FundingPoint(ts, rate)`.

2. **Аналитика (`src/ai_trader/analysis/positioning.py`, новый):**
   - `PositioningSnapshot` dataclass: oi_now, oi_4h_ago, oi_24h_ago,
     oi_delta_4h_pct, oi_delta_24h_pct, funding_now, funding_24h_cumulative,
     funding_24h_mean, funding_7d_mean, funding_prev_period.
   - `build_positioning_snapshot(oi_history, funding_history, funding_now)`
     — собирает фичи из сырых истории-массивов. Tolerant к None /
     коротким массивам (соответствующие производные = None, без crash).
   - `format_positioning(snapshot)` — текстовый двухстрочный вывод
     для system-prompt с режим-метками.

3. **Метки (research-обоснованные):**
   - **OI delta:** `[moderate]` ≥±2%, `[buildup]/[unwind]` ≥±5%,
     `[strong buildup]/[strong unwind]` ≥±10%,
     `[EXTREME buildup]/[EXTREME unwind]` ≥±15%.
   - **Funding bands** (Lambda Finance 2026):
     `<0.05%` per 8h → `[neutral leverage]`,
     `0.05–0.20%` → `[mild long bias]/[mild short bias]`,
     `>0.20%` → `[STRONG long bias]/[STRONG short bias]`.

4. **Контекст-сборка (`src/ai_trader/trading/context.py`):**
   - `SymbolSnapshot` получил поле `positioning: PositioningSnapshot | None`.
   - `collect_market_context()` запрашивает OI history (limit=25) и
     funding history (limit=21) per-symbol, строит positioning и
     складывает в snapshot.
   - `format_context_for_prompt()` выводит блок
     `POSITIONING (institutional 2026):` **перед** `1H INDICATORS`
     для каждого символа (visual cue для LLM что приоритезировать
     positioning над classical indicators).

**Почему OI history limit=25, а не 24:** для расчёта Δ24h нужно `[-25]`
(индекс «25 точек назад» при шаге 1h). Δ4h использует `[-5]`. Пограничный
запас на случай если Bybit вернёт <25 точек для редко торгуемых пар
(WLD, TAO) — функция спокойно вернёт `Δ24h=None`, формат это покажет.

**Тесты:** добавлены 12 регрессионных в `test_ai_trader_positioning.py`:
- `TestBuildPositioning` (8): empty inputs, short OI, OI delta-4h
  known value (+10%), OI delta-24h known value (+48% / +5.71%),
  zero anchor → None, funding 5 events с known cumulative,
  1 event (без crash), funding_now passthrough.
- `TestFormatPositioning` (4): full data shows OI/funding labels,
  STRONG short bias, all-None graceful fallback (`n/a` markers),
  OI unwind (`[strong unwind]` / `[EXTREME unwind]`).

Suite 478/478 зелёный.

**Файлы.** `src/ai_trader/trading/client.py`,
`src/ai_trader/analysis/positioning.py` (новый),
`src/ai_trader/trading/context.py`,
`tests/test_ai_trader_positioning.py` (новый).

**Smoke-тест публичного API** (`pybit.HTTP(demo=True)` на BTCUSDT):
- OI keys = `['openInterest', 'timestamp']` ✓
- Funding keys = `['symbol', 'fundingRate', 'fundingRateTimestamp']` ✓
- BTC OI = ~52,572 BTC; funding rate = +0.00917% — реалистичные значения.

---

## 2026-05-07 — feat(market-context i1/7): VWAP + Realized Volatility (1H/4H)

`<hash-pending>`

**Контекст.** Пользователь обратил внимание, что классические индикаторы
(RSI 1978, MACD 2005, Bollinger 2001) — «древние знания», и попросил
использовать актуальные подходы 2026 года. Research показывает: в 2024-
2026 институциональные quant-десков фокусируются на positioning/flow
(funding, OI, RV, IV-skew), а не на retail-индикаторах. План — 7 итераций
по добавлению современных фич + demote классических в конец промпта.
Первая итерация (эта запись) — локальные вычисления без новых сетевых
вызовов.

**Что добавлено.**

1. **VWAP (Volume-Weighted Average Price)** для 1H и 4H в
   `src/ai_trader/analysis/indicators.py` (новая функция `vwap()`).
   Формула: `Σ((H+L+C)/3 × Volume) / Σ(Volume)` по rolling-окну.
   Окно: 24 бара на 1H (≈ daily VWAP-aware), 30 баров на 4H
   (≈ weekly fair-value benchmark).

   *Research basis:* Berkowitz/Logue/Noser «The Total Cost of
   Transactions on the NYSE» (Journal of Finance 1988); institutional
   execution standard. Decentralised.news «Quant Signals for Crypto
   Derivatives 2026»: «institutional quant models focus on positioning,
   funding stress, volatility structure — not RSI/MACD».

2. **Realized Volatility (RV, аннуализированная)** —
   `realized_volatility()`. Формула: `√(Σ(log_return²)/N × bars_per_year)`.
   Окно: 24 returns на 1H (≈ 1 сутки), 30 на 4H (≈ 5 суток).
   Аннуализация: 8760 для 1H, 2190 для 4H.

   *Research basis:* Andersen/Bollerslev/Diebold/Labys «Modeling and
   Forecasting Realized Volatility» (Econometrica 2003). RV предпочитают
   ATR в современных GARCH/HAR-RV моделях — она lognormal-friendly,
   аддитивна (RV_T = ΣRV_τ) и используется как input для волатильность-
   forecasting. В 2026 RV vs IV спред — proxy на ожидания
   институциональных option-desks.

3. **Метки в `format_snapshot`:**
   - VWAP: `[STRETCHED above/below VWAP]` (≥±2%), `[above/below VWAP]`
     (±0.5–2%), `[near VWAP]` (<±0.5%).
   - RV: `[EXTREME vol regime]` (≥200%), `[elevated vol]` (100–200%),
     `[normal vol]` (50–100%), `[low vol / squeeze candidate]` (<50%).

   Эти метки — текстовые, чтобы LLM сразу видел регим без расчётов.

4. **`compute_snapshot()` расширен:** новые kwargs `volumes`,
   `vwap_window`, `rv_window`, `bars_per_year`. Default behavior
   сохранён (если volumes=None — VWAP=None, RV всё равно считается).
   `IndicatorSnapshot` получил 4 новых поля: `vwap`, `vwap_dev_pct`,
   `rv_pct`, `rv_window_bars`.

5. **`build_market_context` (`trading/context.py`)** теперь передаёт
   volumes из свечей и правильный `bars_per_year` для каждого TF.

**Тесты:** добавлены 13 регрессионных тестов
(`tests/test_ai_trader_indicators.py`):
- `TestVwap` (6) — постоянная цена, weighted by volume, edge cases
  (zero volume, mismatched lengths, period subset, empty input).
- `TestRealizedVolatility` (4) — постоянная цена → 0, short series → None,
  known-value (1% per bar → ~93% annualised), period subset.
- `TestSnapshotV05Fields` (3) — volumes populates VWAP+RV, без volumes
  VWAP=None, format_snapshot включает VWAP/RV строки и метки.

Все 466 тестов suite зелёные.

**Файлы.** `src/ai_trader/analysis/indicators.py`,
`src/ai_trader/trading/context.py`,
`tests/test_ai_trader_indicators.py`.

**Что НЕ менялось.** Классические индикаторы (RSI/MACD/BB/EMA/ATR)
остаются на своём месте — в этой итерации они НЕ degrademовали в
промпте. Demote запланирован в **итерации 7**, после того как
positioning/flow-фичи будут добавлены и validatedы (итерации 2-6).

**План оставшихся итераций** (отдельные коммиты):
- i2: Open Interest delta + Funding rate cumulative (Bybit public API).
- i3: Fear & Greed Index + BTC Dominance % (alternative.me, CoinGecko).
- i4: Long/Short ratio + Orderbook L2 imbalance (Bybit).
- i5: Liquidation feed (Bybit WebSocket или OI-drop proxy).
- i6: Deribit DVOL/IV для BTC и ETH (options sentiment).
- i7: Promote/demote — positioning/flow в начало промпта, classical
  indicators в конец как «secondary context».

---

## 2026-05-07 — feat(symbols): расширили пул с 5 до 10 пар + max_pos 3→5 + parametrized prompt

`6d51360`

**Что изменилось.**

1. **Пул торгуемых пар: 5 → 10** (`src/ai_trader/config/settings.py`).
   Добавлены 5 пар, не пересекающиеся с `bybit_bot.scan_symbols`
   (SOL/ADA/LINK/SUI/TON/WIF/TIA/DOT) и не дублирующие текущие
   ai_trader (BTC/ETH/BNB/XRP/DOGE):

   | Symbol   | Класс / нарратив                          |
   |----------|-------------------------------------------|
   | AVAXUSDT | L1 / Avalanche subnets                    |
   | LTCUSDT  | digital silver / mining-кор               |
   | ATOMUSDT | Cosmos hub / IBC                          |
   | WLDUSDT  | identity / OpenAI tie-in (нарратив 2025+) |
   | TAOUSDT  | decentralized AI / Bittensor              |

   Все 5 — публично листятся на Bybit demo linear (проверено
   `/v5/market/tickers` 2026-05-07): AVAX $9.69, LTC $57.08, ATOM $1.93,
   WLD $0.26, TAO $310.96. Funding rates в нейтральной зоне (|rate|<0.05%).

2. **`AI_TRADER_MAX_POSITIONS`: default 3 → 5.**
   Логика sizing: пул увеличен в 2 раза (5→10), одновременная ёмкость
   увеличена пропорционально (3→5 = ~50%% пар). Risk-per-trade остаётся
   2%% капитала ($10 на сделку), значит max realised drawdown за один
   цикл = 5×$10 = $50, ровно равен `max_daily_loss_usd`. Дальше —
   killswitch блокирует торговлю до следующего дня. Killswitch
   `max_total_loss_usd=$200` (40%% capital) тоже не сдвигаем.

3. **Параметризованный system-промпт** (`src/ai_trader/llm/prompts.py`).
   Старый `SYSTEM_PROMPT` имел зашитые `BTCUSDT, ETHUSDT, BNBUSDT,
   XRPUSDT, DOGEUSDT`, `Maximum 3 simultaneous`, `position_size_usd:
   50-500`, `Risk ... <= $10 (2%% of $500)`. При расширении пар пришлось
   бы каждый раз править саму строку → конфликтует с правилом «промпт
   ЗАМОРОЖЕН на 14 дней эксперимента» (`prompts.py`).

   v0.4: `SYSTEM_PROMPT_TEMPLATE` + `build_system_prompt(settings)`
   подставляет лимиты и список пар через %-форматирование (literal
   `%`-знаки в тексте → `%%` для escape, JSON-схемы остаются интактны
   — это причина выбрать % над str.format с массой `{{`/`}}`).
   `app/main.py` теперь зовёт `build_system_prompt(settings)` каждый
   цикл, decisions audit-trail сохраняет ровно тот промпт что видел LLM.

   Поведенческой подгонки нет: при дефолтных настройках текст 1:1
   эквивалентен старому, плюс расширение списка пар. Это «параметризация
   константы», не правка торговой логики.

**Без runtime guard на overlap с bybit_bot.** Согласовано с пользователем:
проверка остаётся в виде комментария в `DEFAULT_AI_SYMBOLS` (`settings.py`).
ai_trader и bybit_bot — изолированные кодовые базы (правило
`strategy-guard.mdc`), импорт `bybit_bot.*` из `ai_trader.*` запрещён.
Контроль non-overlap — на уровне ревью / `.env` диффа.

**Тесты.** +3 unit-теста `TestBuildSystemPrompt`:
- `test_default_prompt_contains_default_pairs_and_limits` — все 10 пар
  и дефолтные лимиты ($500, 5 pos, 5x lev, 2%%, $50 daily) попадают в
  итоговый промпт; JSON-схема не сломана.
- `test_custom_settings_propagate` — кастомные `AI_TRADER_*` env vars
  пробрасываются в LLM-промпт (capital=$1000, max_pos=7, leverage=3,
  risk=1%%); SOLUSDT появляется, DOGEUSDT исчезает; `position_size_usd`
  диапазон становится `50-1000`.
- `test_no_unresolved_placeholders` — fail-fast если в финальном
  промпте остался хоть один `%(name)s` (защита от опечатки в шаблоне).

Все 453 теста зелёные (было 450 → +3).

**Файлы:**
- `src/ai_trader/config/settings.py` — DEFAULT_AI_SYMBOLS 5→10, max_pos 3→5
- `src/ai_trader/llm/prompts.py` — SYSTEM_PROMPT_TEMPLATE + build_system_prompt
- `src/ai_trader/app/main.py` — вызов `build_system_prompt(settings)` на цикл
- `tests/test_ai_trader.py` — TestBuildSystemPrompt (3 теста)

**Hot-fix follow-up (тот же день):** при первом деплое контейнер
проигнорировал code-default и поднялся со старыми 5 парами / `maxpos=3`,
потому что `docker-compose.yml` имел собственный compose-default
`AI_TRADER_SYMBOLS:-BTC...,DOGE` и `AI_TRADER_MAX_POSITIONS:-3`, и
compose инжектил их в env, перебивая pydantic code-default. Это
дублирование: одно место правды в коде + второе в compose.

Решение (single source of truth для пар = .env):
- Удалили `AI_TRADER_SYMBOLS: ${AI_TRADER_SYMBOLS:-...}` строку из
  `docker-compose.yml` целиком. Теперь compose не задаёт default для
  списка пар. Если переменная не определена в `.env` — pydantic берёт
  `DEFAULT_AI_SYMBOLS` из `settings.py` (10 пар, safety-net).
- Добавили в `.env.example` секцию `AI-TRADER` с явной строкой
  `AI_TRADER_SYMBOLS=...` и предупреждением про non-overlap с
  `BYBIT_BOT_SCAN_SYMBOLS`.
- На VPS `.env` дописали `AI_TRADER_SYMBOLS=BTC...,TAOUSDT` (10 пар).
- Лимиты (`MAX_POSITIONS`, `MAX_LEVERAGE`, `RISK_PER_TRADE` etc.)
  оставили в compose с дефолтами — по согласованию с пользователем
  source-of-truth-перенос ограничен только списком пар.

`AI_TRADER_MAX_POSITIONS:-5` в compose уже синхронизирован с
расширением (см. выше), `.env` его не override-ит, всё консистентно.

---

## 2026-05-07 — fix(reconcile): не помечать позицию closed при API failure биржи

`f3ce979`

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
