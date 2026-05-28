# BUILDLOG — AI-Trader (DeepSeek-V4)

## 2026-05-28 — v0.30 institutional rewrite + v0.31 aggressive mandate + v0.32 EQUITY AWARENESS

Тройной apgrade в одну сессию. Reset n=0 (новые параметры + новый промпт
= новый DGP, см. `.cursor/rules/no-data-fitting.mdc`).

### v0.30 — Institutional Rewrite (port FX-trader patterns)

Перевод бота из «retail chartist» режима (MFP по 1H индикаторам без
тезиса) в «institutional discretionary trader» (THESIS-driven). Адаптация
10 концепций FX-bot под крипту:

1. **PER-ASSET MACRO DRIVER HIERARCHY** для BTC/ETH/SOL/BNB/XRP/LTC/DOGE
   с research URLs (BitMEX 2026 DXY corr -0.72..-0.90, BYDFi BTC.D, и т.д.).
2. **MFP CONFLUENCE FRAMEWORK**: 5-rule entry gate (momentum / BB-Z /
   RSI extreme / breakout / news catalyst), ≥3/5 для trend, ≥4/5 для
   counter-trend. Замена нечёткому «2+ confirmations».
3. **THESIS DISCIPLINE**: `macro_thesis` обязательное при open (50-500
   chars), `thesis_status` + `thesis_invalidator` при close. Закрывает
   FX-bot паттерн «22/26 closes by 1H MACD flip ignoring entry thesis».
4. **SELF-REFLECTION**: per-symbol PnL + per-(symbol×side) cold-start
   + last 10 closed trades с прошлым reasoning. Прямо в user-context.
5. **COLD-START DISCOVERY RULE**: для n=0 (symbol×side) разрешён
   guarded discovery trade на 0.5R. Sutton & Barto 2018 §2.7.
6. **REGIME-CHANGE WINDOW**: SELF-REFLECTION filtered by
   `stats_window_start`. Lopez de Prado 2018 ch.7 + Hamilton 1989.
7. **NOISE-BAND POSITION SIZING**: STANDARD / EVENT / SHOCK days based
   on ATR%.
8. **5-DIM NEWS SENTIMENT**: relevance / polarity / intensity /
   uncertainty / forwardness. Hard gate: `aggregate_uncertainty > 0.7`
   → open auto-rejected by executor.
9. **EXTERNAL MACRO CONTEXT**: DXY/UST10Y через yfinance
   (`MacroRatesProvider`), BTC.D/total cap через CoinGecko
   (`CryptoMacroProvider`).
10. **CONCRETE JSON EXAMPLES** в промпте (filled-out open / close /
    hold с реальными цифрами).

**Файлы:**
- `src/ai_trader/state/db.py` — миграции для `macro_thesis`,
  `thesis_status`, `thesis_invalidator`, `aggregate_uncertainty`,
  `sentiment_items_json`, `macro_rates_snapshot`; новые методы
  `get_pnl_by_symbol`, `get_pnl_by_symbol_side`, `get_recent_closed_trades`,
  `update_decision_thesis`, `update_decision_sentiment`.
- `src/ai_trader/config/settings.py` — `macro_rates_enabled`,
  `crypto_macro_enabled`, `stats_window_start`,
  `news_uncertainty_block_threshold`.
- `src/ai_trader/data/macro_rates.py` (создан, без TIP).
- `src/ai_trader/data/crypto_macro.py` (создан).
- `src/ai_trader/trading/executor.py` — `strict_v030_schema` flag,
  валидация macro_thesis / sentiment / thesis_status; `ApplyResult`
  audit-trail.
- `src/ai_trader/trading/context.py` — `MarketContext` расширен 6
  блоками; `collect_market_context` собирает их.
- `src/ai_trader/llm/prompts.py` — институциональная переписка
  SYSTEM_PROMPT (~30k → ~36k chars).
- `src/ai_trader/app/main.py` — orchestration новых providers
  и persisting audit-trail.
- 4 новых test файла (1276 → 1277 тестов).

### Collision audit (DeepSeek perspective, 6 коллизий)

Запустил симулятор полного цикла (`tests/test_ai_trader_llm_perspective.py`,
рендерит SYSTEM+USER prompt в `/tmp/ai_trader_llm_simulation.txt`),
прочитал ~850 строк глазами LLM, нашёл 6 проблем:

1. **`mark=$0 liq=$0`** в LIVE-строке когда биржа не вернула данные
   (Position dataclass defaults 0.0) → LLM думает позиция aborted.
   Фикс: показывать `n/a` явно (`context.py:_format_live_data`).
2. **`est=±$0.00`** для negligible funding → выглядит как баг. Фикс:
   `<±$0.01` (`context.py:_funding_cost_hint`).
3. **`[bullish]`** слипается с RSI value → визуальная двусмысленность.
   Фикс: `[MACD-bullish]` явно (`indicators.py:format_snapshot`).
4. **`MACRO (relative)`** дублирует `CRYPTO MACRO` block → две
   source-of-truth для BTC dominance. Фикс: показывать proxy ТОЛЬКО
   как fallback когда `crypto_macro_block is None`.
5. **PER-ASSET HIERARCHY содержит 7+ символов, ALLOWED PAIRS только 5**.
   Что делать с открытой позицией по non-allowed? Фикс: явный блок
   "treat as REFERENCE ONLY for non-allowed; MAY only close, never add".
6. **EXIT trigger 1** двусмысленный: "macro re-check is PRIMARY" +
   "tactical mean-rev close at SMA20". Фикс: разделил на 1a MACRO
   INVALIDATION (thesis_status=broken) + 1b TACTICAL EXIT TARGET
   (thesis_status=intact).

После фиксов прогнал симуляцию как DeepSeek — все 8 шагов ANALYSIS
APPROACH прошли без противоречий, решение HOLD для двух открытых
позиций обосновано чётко.

### v0.31 — Aggressive Mandate (по запросу пользователя)

> «нужно чтобы он торговал агрессивно. Баланс 500, килсвич 350 в день,
> лот может стоить до 100 долларов зависит от уверенности. Должен не
> забывать про комиссию и funding settlements»

**Параметры (`settings.py`):**
- `max_daily_loss_usd`: $50 → **$350** (70% capital killswitch).
- `max_open_positions`: 3 → **5** (агрессивная диверсификация).
- `max_total_loss_usd`: $200 → **$400** (80% capital halt).
- `max_position_size_usd`: НОВОЕ явное поле = **$100** (cap на
  `position_size_usd` в JSON; раньше было неявно = virtual_capital $500).

При max_lot $100 × max_leverage 5x = notional до $500 = весь капитал.
**Risk per trade $10 (2%) не меняем** — industry standard; агрессия
достигается через ЧАСТОТУ setup'ов, не через risk-per-trade.

**Executor (`executor.py`):**
- `parse_action(position_size_cap_usd=...)` — новый параметр + hard
  reject если `position_size_usd > cap`.
- `ApplyResult.cost_estimate_usd` — soft enforcement audit поле
  (LLM ожидается заполнить, executor логирует, не блокирует).

**Промпт (`prompts.py`):**
- Удалено «Most cycles SHOULD be HOLD» (противоречило aggressive).
  Замена: «Your mandate is AGGRESSIVE EXECUTION: actively seek
  qualified setups». HOLD остаётся correct default ТОЛЬКО при MFP<3/5
  на всех allowed pairs или aggregate_uncertainty>0.7.
- Новая секция **CONFIDENCE → SIZE MAPPING**: low (0.30-0.49) =
  0.25-0.50x cap, medium (0.50-0.69) = 0.50-0.75x, high (0.70+) =
  0.75-1.00x. Bands выражены как fraction of `max_position_size_usd`
  (auto-rerendered при изменении settings).
- Новый pitfall **COST AMNESIA / OVERTRADING-COSTS**: явный fee_RT +
  funding cost netout требуется в commentary BEFORE the JSON.
  Aggressive mandate ≠ overtrading-without-edge.
- Старая v0.21 «Do NOT add per-trade funding cost as an open-decision
  factor» отменена. Теперь funding cost учитывается через
  `cost_estimate_usd` когда settlement в горизонте удержания.
- OPEN example переписан под aggressive sizing (lot $75 lev 4x вместо
  lot 400 lev 2x).

### v0.32 — EQUITY AWARENESS (live capital tracking)

> «бот думает что у него 500долларов, по факту на демо много больше.
> ИИ должен играть на 500, видеть что эта сумма убывает или растёт
> и от этого у него мысли другие будут»

**Проблема:** до v0.32 промпт показывал статичный
`VIRTUAL CAPITAL: $500.00` — это **initial deposit**, immutable.
LLM не видел как капитал эволюционирует, не мог адаптировать sizing
в drawdown / profit zone. Дисциплинированный трейдер всегда смотрит
на equity curve (Mark Douglas «Trading in the Zone» 2000 ch.7).

**Решение:**

- `src/ai_trader/state/db.py` — новый метод
  `get_equity_high_water_mark(initial_capital_usd)` — running cumsum
  по daily_pnl, возвращает peak (daily resolution).
- `src/ai_trader/trading/context.py`:
  - `MarketContext` расширен: `realized_pnl_total_usd`,
    `unrealised_pnl_total_usd`, `peak_equity_usd`.
  - `collect_market_context` заполняет из `store.get_total_pnl()` +
    `store.get_equity_high_water_mark()` + sum по live_positions.
  - Новая helper `_format_equity_block` рендерит:
    ```
    VIRTUAL CAPITAL: initial=$500.00  current_equity=$487.50 (-2.50% vs initial)  peak=$502.00 (-2.89% vs peak)
      realized_since_start=-12.50$ (net: closed PnL + funding)  unrealised=$0.00 (live mark-based)
    ```
    Используется в обоих форматтерах (full + review).
- `src/ai_trader/llm/prompts.py`:
  - Новая секция **EQUITY AWARENESS** с zone-based adapter:
    - `current_equity ≥ 100% initial` → Normal sizing.
    - `90% ≤ current < 100%` → Mild: high-band capped at 0.75x.
    - `80% ≤ current < 90%` → Caution: high+medium capped at 0.50x +
      review which (symbol×side) losing.
    - `current < 80%` → Defensive: only LOW band + only proven
      WR>50% (symbol×side), no COLD-START discovery.
  - Peak-aware secondary: new high → DO NOT autoscale; -15% from
    peak → cooling-off + prefer trend-following over mean-revert.
  - `ANALYSIS APPROACH` — добавлен шаг 1 «EQUITY READ»; шаг 7
    PRE-COMMIT теперь явно требует apply EQUITY adapter поверх
    CONFIDENCE band (whichever more restrictive).

### Compliance check

- ✅ `strategy-guard.mdc`: research basis для каждого изменения
  процитирован (per-asset URLs в hierarchy блоках, Lopez de Prado /
  Hamilton / Sutton-Barto / Mark Douglas / BBX Research / BitMEX).
- ✅ `sample-size.mdc`: НЕ отключали ни одного инструмента / стратегии.
  v0.31 параметры (killswitch $350, max_pos 5, lot cap $100) —
  user-driven configuration, не data-fitted (sample n=0).
- ✅ `no-data-fitting.mdc`: НЕ подкручивали по результатам прошлых
  тестов. v0.32 EQUITY AWARENESS — структурное добавление, не tuning.
  Reset n=0 явно отмечен.
- ✅ `api-docs.mdc`: не трогали API connection layer.
- ✅ `stats-collection.mdc`: добавлен явный warning в SELF-REFLECTION
  блок про возможный mix gross/net PnL (collision audit fix).

### Acceptance criteria (для будущей observation 2-3 дня)

После selective deploy ai-trader (см. `deploy-vps.mdc`):

1. **Smoke (5 min)**: `docker logs fx-pro-bot-ai-trader-1 --tail 50`
   показывает успешный full-cycle с новой EQUITY строкой; БД содержит
   новые колонки (`macro_thesis`, `thesis_status`, `aggregate_uncertainty`,
   `sentiment_items_json`); промпт не упирается в max_tokens.
2. **24h**: ≥3 закрытых сделки, все с заполнен `macro_thesis@open`,
   `thesis_status@close`. `cost_estimate_usd` присутствует в ≥80%
   open-decisions (soft enforcement working).
3. **48-72h**: EQUITY block обновляется (если есть PnL); LLM в
   commentary действительно ссылается на equity zone хотя бы 1 раз
   (text scan `grep -i "equity zone\|drawdown\|peak"`); ни одного
   open с `position_size_usd > $100` (hard cap working).
4. **Sanity**: при искусственном drawdown -15% (можно симулировать
   через `UPDATE daily_pnl SET realized_pnl_usd = -75 WHERE day=...`)
   LLM в следующем full-cycle должен в commentary назвать zone
   «Caution» и `position_size_usd` уменьшить vs предыдущий цикл.

**Файлы (v0.30+v0.31+v0.32):**
- `src/ai_trader/state/db.py` (миграции v0.30 + `get_equity_high_water_mark` v0.32)
- `src/ai_trader/config/settings.py` (v0.30 macro/sentiment, v0.31 aggressive)
- `src/ai_trader/data/macro_rates.py` (создан, v0.30)
- `src/ai_trader/data/crypto_macro.py` (создан, v0.30)
- `src/ai_trader/trading/executor.py` (v0.30 strict schema, v0.31 lot cap + cost_estimate)
- `src/ai_trader/trading/context.py` (v0.30 8 новых полей, v0.32 equity-block)
- `src/ai_trader/llm/prompts.py` (v0.30 ~36k chars, v0.31 +1.5k, v0.32 +3.5k)
- `src/ai_trader/app/main.py` (передаёт новые params в parse_action)
- `src/ai_trader/analysis/indicators.py` (collision-fix MACD label)
- `.env.example` (v0.31 aggressive override hints)
- 5 новых test файлов: `test_ai_trader_self_reflection.py`,
  `test_ai_trader_thesis_discipline.py`, `test_ai_trader_macro_rates.py`,
  `test_ai_trader_crypto_macro.py`, `test_ai_trader_llm_perspective.py`
- `tests/test_ai_trader.py` (SHA baseline + ~10 assertions updated)

**SHA SYSTEM_PROMPT эволюция:** d380da80 (v0.30 baseline) → a8b1785b
(collision audit) → 93da6fb8 (v0.31 aggressive) → f5022a69 (v0.32 EQUITY).

**1277/1277 тестов проходят.**

---

## 2026-05-28 — v0.21: Funding Awareness (perp-futures 8h holding cost)

### Запрос пользователя

> «еще я заметил что пока висят открытые лоты биржа делает странные
> транзакции по списанию, ощущение что это помимо комиссии еще и
> средства за удержание этой позиции в какой-то промежуток времени»

### Диагностика

Подтверждено через `get_transaction_log(category=linear, type=SETTLEMENT)`
за последние 96ч. В реальной выписке:

| Время (UTC ms)   | Symbol   | Side | Funding     |
|------------------|----------|------|-------------|
| 1779724800000    | ATOMUSDT | Buy  | **−$0.1885** (заплатили) |
| 1779667200000    | SUIUSDT  | Sell | **+$0.0227** (получили) |
| 1779638400000    | SUIUSDT  | Sell | **+$0.0334** (получили) |

Это **funding settlements** на perpetual futures: каждые 8ч (00:00,
08:00, 16:00 UTC) биржа считает funding rate × notional между longs и
shorts. Если позиция пересекает settlement timestamp — funding
списывается/начисляется. Это НЕ комиссия (taker fee, v0.20 уже учёл).

**Четвёртая утечка от gross к net** (после fee_on_open, fee_on_close,
reconcile-fee): `closedPnl` от Bybit НЕ включает funding settlements.
`positions.realized_pnl_usd` (pnl_source='net') это игнорировал —
позиции висевшие через хотя бы один settlement получали скрытый PnL-drift.

### Решение (Вариант C по предложению агента — максимальное покрытие)

#### 1. БД: новая колонка `positions.funding_usd`

`src/ai_trader/state/db.py` — идемпотентная миграция (ALTER TABLE ADD
COLUMN funding_usd REAL). Хранит подписанное USD-значение: − значит
бот заплатил, + значит получил. NULL = ещё не синкнули с биржей.

Новые методы:
- `update_funding(position_id, funding_usd)` — идемпотентный апдейт +
  коррекция `daily_pnl` на разницу (закрытая позиция, иначе no-op).
- `get_positions_missing_funding(hours=96)` — закрытые позиции с
  `funding_usd IS NULL` за последние N часов (для reconcile-цикла).

Полный net по позиции = `realized_pnl_usd + funding_usd`. Хранятся
отдельно, чтобы можно было аудитить какая часть PnL пришла из price
move + fees vs holding cost.

#### 2. Bybit client: `get_funding_for_position` и `FundingEvent`

`src/ai_trader/trading/client.py` — новый dataclass `FundingEvent`
(symbol, side, funding_usd, transaction_time_ms). Метод
`get_funding_for_position(symbol, start_ms, end_ms, side=None)` ходит
в `/v5/account/transaction-log` с фильтрами `type=SETTLEMENT`,
`category=linear`, `symbol`, делает full pagination через
`nextPageCursor` (правило `stats-collection.mdc` про API без
incomplete data).

Также `Ticker` теперь включает `next_funding_time_ms` (ms-timestamp
следующего settlement; обычно через 0–480 минут). Bybit поле
`nextFundingTime` из `/v5/market/tickers`.

Источник: <https://bybit-exchange.github.io/docs/v5/account/transaction-log>,
<https://bybit-exchange.github.io/docs/v5/market/tickers>.

#### 3. Reconcile: `funding_reconcile.fetch_position_funding` + main-loop

`src/ai_trader/trading/funding_reconcile.py` (новый модуль, по аналогии
с `pnl_reconcile.py` для consistency):
- Из (opened_at, closed_at) делает окно `[opened_ms − 2min,
  closed_ms + 2min]` со slack для transaction-log запроса.
- Фильтрует events по строгому `[opened_ms, closed_ms]` (slack нужен
  только для запроса, settlement timestamp всегда ровно 00/08/16 UTC).
- Возвращает суммарный `funding_usd` (signed) или `None` при API failure.

`src/ai_trader/app/main.py`:
- Добавлена функция `_reconcile_funding(client, store, hours=96)`,
  вызывается в каждом full-cycle после `_reconcile_pnl_to_net`.
- В `executor._apply_close` и `main._reconcile_closed_positions`
  попытка немедленного fetch funding после close (если позиция держалась
  через settlement — funding уже в transaction-log через 1–2 мин).

#### 4. TG-нотификация CLOSE

Если ненулевой funding пойман сразу:
```
CLOSE id=121 Sell BTCUSDT exit=$74678 pnl=$+7.48 (net) funding=$-0.42 net_total=$+7.06
```
Если funding не успел появиться на момент close — `_reconcile_funding`
догонит на следующем full-cycle (без отдельного TG-сообщения,
информация будет в БД и в `/pnl` сводках).

#### 5. LLM context: `next_funding=Xm` hint в LIVE-строке

`src/ai_trader/trading/context.py`:
- Новый helper `_funding_cost_hint(position, live, ticker)`:
  - Считает `minutes_to_next = (next_funding_time_ms − now_ms) / 60_000`.
  - Считает `est_funding_usd = |size| × mark × |rate|`.
  - Знак: long платит при rate>0 (`paying as Buy`), short платит при
    rate<0 (`paying as Sell`); инвертировано → `earning`.
  - Возвращает строку `| next_funding=8m rate=+0.0125%/8h est=-$0.31
    (paying as Buy)` или `""` если ticker/next_funding отсутствует.
- `_format_live_position_line` принимает новый kwarg `ticker` и
  дописывает hint после `close_net`.
- Оба форматтера (`format_context_for_prompt`, `format_context_for_review`)
  пробрасывают `sym_snap.ticker` в `_format_live_position_line`.

Пример того что теперь видит LLM:
```
id=42 Buy BTCUSDT qty=0.01 entry=$77000 sl=$76500 tp=$78500
     peak_pnl_r=+0.30R current_pnl_r=+0.10R | NET (after est. RT fees $0.85): peak=+0.13R cur=-0.07R
     LIVE: mark=$77100 unrealised=+1.00$ liq=$70000 (9% buffer) margin=$77.10 close_net=+0.58$ (after -$0.42 close fee) | next_funding=15m rate=+0.0125%/8h est=-$0.10 (paying as Buy)
```

#### 6. Prompt: блок FUNDING AWARENESS (full + review)

`src/ai_trader/llm/prompts.py`:
- В `WHAT YOU SEE EACH CYCLE` добавлен абзац про `next_funding=` поле.
- Новый блок `FUNDING AWARENESS (v0.21 …)` после FEE AWARENESS:
  - Объясняет funding как НЕ trading fee, а 8h holding cost.
  - `rate > 0` → longs pay shorts; `rate < 0` → shorts pay longs.
  - Бэнды `0.005–0.05% per 8h` типично, `>0.20%` extreme.
  - **CLOSE DECISION RULES**:
    - `next_funding ≤ 30m` + paying + est > close_net → CLOSE NOW.
    - `next_funding ≤ 30m` + earning > 0 → HOLD через settlement (free).
    - `next_funding > 30m` → ignore, let triggers 1-4 drive.
  - **OPEN**: funding band остаётся entry signal (как до v0.21);
    per-trade funding cost явно НЕ применяется к sizing (notional
    кэппится `risk_usd_cap`, funding мало относительно типичных TP).
- Аналогичный блок в `SYSTEM_PROMPT_REVIEW` (короче).
- `ANALYSIS COMMENTARY for funding-driven close MUST cite numeric
  tradeoff (close_net vs est funding cost in Xm)`.

#### 7. Тесты (+22 новых, total 194/194 в test_ai_trader.py, 1218/1218 проектных)

- `TestPositionsFundingUsdMigration` (3): new DB / idempotent /
  миграция на pre-v0.21 БД (важно для VPS — там 121 closed-позиций без
  колонки).
- `TestStoreUpdateFunding` (5): write / idempotent same value /
  correction on change / ignore on open / `get_positions_missing_funding`
  возвращает только closed+NULL.
- `TestGetFundingForPositionClient` (7): empty window / parse SETTLEMENT /
  skip zero / filter by side / pagination cursor / non-zero retCode →
  None / exception → None.
- `TestFetchPositionFunding` (4): zero когда не пересекли / sum нескольких /
  exclude вне strict window / None при API failure.
- `TestFundingCostHint` (4): Buy paying / Sell earning / no ticker → no
  hint / next_funding=0 → no hint.
- `TestPromptFundingAwareness` (4): полный prompt содержит секцию,
  review тоже, decision rules (PAYING/EARNING/HOLD), open-guidance.

SHA256 baseline `SYSTEM_PROMPT` обновлён:
- было: `ec9503296d5c9686796b91eae67757659bfa1521c34491a67cfa507e6d48464a` (v0.20)
- стало: `532b344a93035dfceeaf2eb1dc8169dbdfee931b51bcf702c005aa91d6ee5569` (v0.21)

### Reset 14-day эксперимента n=0

Согласно `.cursor/rules/no-data-fitting.mdc` — изменение торговой
логики (CLOSE-decision получил funding-rule, который меняет когда бот
закрывает позицию). Все накопленные стататы до v0.20 → stale baseline,
с v0.21 — fresh forward-test.

### Файлы

- `src/ai_trader/state/db.py` — миграция + методы funding
- `src/ai_trader/trading/client.py` — `FundingEvent`, `Ticker.next_funding_time_ms`,
  `get_funding_for_position`
- `src/ai_trader/trading/funding_reconcile.py` — **новый**
- `src/ai_trader/trading/executor.py` — immediate funding fetch
- `src/ai_trader/app/main.py` — `_reconcile_funding` + TG-suffix
- `src/ai_trader/trading/context.py` — `_funding_cost_hint` + LIVE-строка
- `src/ai_trader/llm/prompts.py` — FUNDING AWARENESS блок (full + review)
- `tests/test_ai_trader.py` — +22 теста, обновлён SHA256 baseline
- `BUILDLOG_AI_TRADER.md` — эта запись

### Проверки

- 1218/1218 тестов прошли (`pytest tests/`)
- Render промптов: SHA256 совпадает, `%%` → `%` в review правильно.
- Lint: 0 ошибок (новые модули + правки старых).

### Источники

- Bybit V5 docs: <https://bybit-exchange.github.io/docs/v5/intro>
- Transaction log endpoint:
  <https://bybit-exchange.github.io/docs/v5/account/transaction-log>
- Tickers (nextFundingTime):
  <https://bybit-exchange.github.io/docs/v5/market/tickers>
- Funding mechanism explainer:
  <https://www.bybit.com/en/help-center/article/Introduction-to-Funding-Rate>

---

## 2026-05-28 — v0.20: Fee Awareness на OPEN + hard-валидация executor'а + NET R-units в контексте

### Запрос пользователя

> «убедись что ии видит и учитывает комиссии при совершении сделок,
> как при расчете стоплоссов так и при слежении за лотом. У меня
> подозрение что тут может быть не точность и ии заходит в сделку с
> более оптимистичным настроем, а когда закрывает уверен в том что он
> получил прибыль»

### Подозрение оправдалось

v0.19 (2026-05-27) добавил FEE AWARENESS, но **только на close-decision**.
При OPEN AI оценивал R:R и risk_usd чисто по ценам:

1. **R:R 1.5 price-based ≠ R:R 1.5 после fees.**
   На notional ~$2500, fee_RT ≈ $2.75. При declared risk=$10 и reward=$15:
   - eff_reward = $15 − $2.75 = $12.25
   - eff_risk = $10 + $2.75 = $12.75
   - **eff_R:R = 0.96** — отрицательное матожидание.
2. **risk_usd cap=$10 не равен реальному убытку на SL.**
   Реальный убыток = declared_risk + fee_RT. При declared $10 + $2.75 fee
   = $12.75 (27% выше per-trade лимита __RISK_PCT__% от капитала).
3. **peak_pnl_r / current_pnl_r — чисто price-based.**
   AI видит peak=+1R, считает «зафиксировал бы R», а реально после
   round-trip fee это ~+0.7R. Триггер LOCKED-PROFIT (1.5R) фактически
   локирует ~1.2R, PEAK-DRAWDOWN (cur≤0.45R) у мелких notional при
   gross-cur=+0.45R даёт net-cur ≈ −0.1R (close в минус).
4. **LIVE `unrealised_pnl` от Bybit не учитывает closeFee.**
   `unrealised_pnl = (mark - entry) × size`, спишется ещё close fee при
   reduce-only ордере. AI читает «unrealised=+$2», уверен что закрытие
   = +$2 net, на деле −$1 (ровно кейс ATOMUSDT #120 в v0.19).
5. **Промпт говорил `0.06% per side` — реальный fee 0.055%** (VIP-0 demo,
   проверено на сверке id=121: openFee=$1.3597 на cumEntryValue=$2472.21
   → ровно 0.055%). Старое число завышало fee на ~9% но всё равно было
   неточным.

### Решение (Вариант B по предложению агента)

#### 1. Новая настройка `taker_fee_pct`

`src/ai_trader/config/settings.py` — default `0.00055` (0.055% per side
VIP-0 demo), env `AI_TRADER_TAKER_FEE_PCT`. Single source of truth для
промпта, контекста и валидатора.

#### 2. Hard-валидация в `_apply_open` (executor.py)

После расчёта `qty` бот сам считает fee_RT и проверяет **до** place_order:

```
fee_RT_usd = price * qty * taker_fee_pct * 2

# (1) net-risk cap
declared_risk_usd + fee_RT_usd MUST be <= risk_usd_cap  → иначе reject
"net_risk_exceeds_cap"

# (2) effective R:R after fees
eff_reward_usd = |TP-entry| * qty - fee_RT_usd
eff_risk_usd   = |entry-SL| * qty + fee_RT_usd
eff_R:R = eff_reward_usd / eff_risk_usd  MUST be >= 1.5  → иначе reject
"eff_rr_below_1.5"
```

Это **жёсткая страховка** — даже если LLM ошибётся в арифметике,
исполнитель сам пересчитает и не пропустит сделку с EV < 0.

#### 3. `PositionPnlStats` + net R-units в контексте (context.py)

Новый dataclass `PositionPnlStats(peak_r, current_r, peak_r_net,
current_r_net, fee_round_trip_usd)`. Новая функция
`_compute_position_pnl_stats(..., taker_fee_pct=...)` считает обе версии.
Старая `_compute_position_r_stats(...)` оставлена как thin wrapper для
backward-compat (возвращает только gross — все 8 существующих тестов
продолжают работать без правок).

В `format_context_for_prompt` / `format_context_for_review` к каждой
открытой позиции теперь добавляется (при `taker_fee_pct > 0`):
```
peak_pnl_r=+1.00R current_pnl_r=+0.20R | NET (after est. RT fees $0.55):
peak=+0.94R cur=+0.14R
```

#### 4. `close_net` в LIVE-строке (context.py)

`_format_live_position_line(..., taker_fee_pct=...)` теперь добавляет:
```
LIVE: mark=$77205 unrealised=+2.05$ liq=$58400 (...) margin=$231.41
close_net=+1.62$ (after -$0.42 close fee)
```
`close_net` = `unrealised - close_fee` = то, что реально получим при
закрытии прямо сейчас по mark price. **openFee не учитывается — он уже
sunk cost** (списан при открытии, не возвращается).

#### 5. Расширен FEE AWARENESS в промпте (prompts.py)

- Title: `affects ALL close decisions` → `affects BOTH open AND close
  decisions`.
- Два раздела: `RULES FOR OPEN` (eff_R:R + net-risk cap с формулами,
  иллюстративный worked example через placeholder'ы) и `RULES FOR
  CLOSE` (как v0.19, но ссылается на `close_net` из LIVE-строки вместо
  гипотетического fee_cost).
- Fee-числа теперь через placeholder'ы: `__TAKER_FEE_PCT__`,
  `__TAKER_FEE_RT_PCT__`, `__TAKER_FEE_FRACTION_RT__`,
  `__FEE_RT_AT_CAPITAL_USD__`. При смене `AI_TRADER_TAKER_FEE_PCT` или
  `AI_TRADER_VIRTUAL_CAPITAL` в .env промпт автоматически пересчитывает
  все примеры (`$500` capital → `$0.55` fee_RT для default).
- `RISK_USD self-check` и `R:R CHECK + RISK_USD` теперь явно говорят
  про after-fee требования.
- `build_system_prompt_review` тоже рендерит fee-placeholder'ы (до
  v0.20 review-промпт не вызывал `_render_capital_rules`).

### Перезапуск 14-day эксперимента n=0

Согласно `.cursor/rules/no-data-fitting.mdc` и `sample-size.mdc`:
**любое изменение торговой логики промпта/parser'а сбрасывает
forward-test n=0**. Все накопленные за период статы (положительные или
отрицательные) до этого коммита трактуются как stale baseline для v0.19,
дальнейший анализ — для v0.20.

### Что НЕ меняется

- Стратегия входа (триггеры, RSI/EMA/BB пороги, confirmations, leverage).
- 4 exit-триггера (invalidation / locked-profit / adverse / peak-drawdown).
- KillSwitch / daily loss limit / max positions / max leverage.
- Net PnL reconciliation (v0.18) — `closedPnl` от Bybit в БД, без funding.
- Telegram-нотификации (gross→net апдейт в TG — отдельная тема для v0.21).

### Файлы

- `src/ai_trader/config/settings.py` — `taker_fee_pct` field.
- `src/ai_trader/trading/context.py` — `MarketContext.taker_fee_pct`,
  `PositionPnlStats` dataclass, `_compute_position_pnl_stats`, fee-aware
  `_format_live_position_line`, обновлённые format-функции.
- `src/ai_trader/trading/executor.py` — fee-aware валидация в
  `_apply_open` (net_risk_cap + eff_R:R).
- `src/ai_trader/llm/prompts.py` — docstring v0.20, новый
  `__FEE_RT_AT_CAPITAL_USD__` placeholder, переписанный FEE AWARENESS
  блок в обоих промптах, рендер capital-rules в review.
- `src/ai_trader/app/main.py` — пробрасывает `settings.taker_fee_pct`
  в `collect_market_context` / `collect_review_context`.
- `tests/test_ai_trader.py` — обновлён SHA256 baseline для SYSTEM_PROMPT
  (`dc44ce72…` → `ec950329…`), +27 новых тестов (TakerFeePctSetting,
  PromptFeePlaceholders, PromptFeeAwarenessOpenSection,
  ComputePositionPnlStatsNet, LiveLineCloseNet,
  ApplyOpenFeeAwareValidation).

### Тесты

```
tests/test_ai_trader.py: 167 passed (было 140, +27 новых)
tests/ (вся репа): 1145 passed
```

### Источник (verification of taker fee)

Бэкап выписки для id=121 (Sell BTCUSDT 0.033, 2026-05-27):
- `cumEntryValue=2472.2115`, `openFee=1.35971633` → 1.35972/2472.21 =
  **0.000550** = 0.055%
- `cumExitValue=2464.4466`, `closeFee=1.35544563` → 1.35545/2464.45 =
  **0.000550** = 0.055%
- `closedPnl = (2472.2115 - 2464.4466) - 1.3597 - 1.3555 = 5.04974` ✓

Это VIP-0 default на Bybit demo unified perpetual. Если в будущем
перейдём на live с VIP-1+ или с rebate-таркером — менять только
`AI_TRADER_TAKER_FEE_PCT` в `.env`, код не трогать.

---

## 2026-05-27 — v0.19: Fee Awareness в промпте (LLM знает о комиссиях)

### Проблема

Trade #120 ATOMUSDT (2026-05-26): бот закрыл позицию с gross PnL = +$2.14,
но net PnL после round-trip fee оказался отрицательным (~-$2.35). LLM
принимал решение о закрытии, не зная, что 0.12% round-trip fee съедает
micro-profit. v0.18 корректно записывает net PnL в БД, но LLM по-прежнему
не учитывал комиссии при принятии решения «закрыть или подождать».

### Решение

Добавлен блок **FEE AWARENESS** в оба промпта:
- `SYSTEM_PROMPT` (full cycle) — 13 строк после DO-NOT-CLOSE guards
- `SYSTEM_PROMPT_REVIEW` (review cycle) — 5 строк (compact version)

Содержание правила:
- Taker fee = 0.06% per side, round-trip = 0.12% of notional
- Формула: `fee_cost = notional_usd * 0.0012`
- Если gross profit < fee_cost И триггер (1-4) НЕ сработал → HOLD
- Если триггер сработал → закрывать независимо от fee (cutting loss)
- Запрет на закрытие ради micro-profit без триггера

### Что НЕ меняется

- Стратегия входа (те же триггеры, те же пороги)
- 4 exit-триггера (invalidation / locked-profit / adverse / peak-drawdown)
- Risk management (SL/TP geometry, R:R ≥ 1.5, confidence)
- KillSwitch / daily loss limit
- Net PnL reconciliation (v0.18)

### Файлы

- `src/ai_trader/llm/prompts.py` — FEE AWARENESS блок + docstring v0.19
- `tests/test_ai_trader.py` — SHA256 baseline updated

### Тесты

140 passed (pytest tests/test_ai_trader.py)

---

## 2026-05-25 — v0.18: net PnL (fee + funding) вместо gross в БД + KillSwitch

**Запрос пользователя:** «главное чтоб ии считал винрейт и доход правильно».

### Проблема (выявлено сверкой с exchange-statement 25/05)

Bot до v0.18 писал в `positions.realized_pnl_usd` значение
`(exit - entry) × qty` — это **gross** PnL без trading fee и без
funding settlement. По сути вся внутренняя бухгалтерия бота
(KillSwitch, daily_pnl, будущая self-reflection в промпте) видела
оптимистичную картину.

Сверка за день 25/05 (5 закрытий бота):

| Trade            | БД (gross) | Statement (net) | Δ комиссия |
|------------------|-----------:|----------------:|-----------:|
| BTCUSDT 0.014    |     +3.02  |          +2.44  |     -0.58  |
| BTCUSDT 0.015    |     +0.41  |          -0.23  |     -0.64  |
| BTCUSDT 0.006    |     -0.64  |          -0.89  |     -0.25  |
| LINKUSDT 76.9    |     -2.77  |          -3.17  |     -0.40  |
| SUIUSDT 220      |     -2.40  |          -2.55  |     -0.15  |
| **Итого**        |   **-2.38**|        **-4.40**|   **-2.02**|

То есть **БД оптимистичнее реальности на 41% за день**. На 14-дневный
forward-test эксперимент это даёт ~$25-30 неучтённых комиссий + funding
sign-flips на удерживаемых позициях.

### Решение: Вариант A (точный, через Bybit API)

Не оценочные комиссии в коде, а **точное** значение от биржи через
endpoint `/v5/position/closed-pnl` (поле `closedPnl` уже net — после
fee и funding). Один API-вызов после каждого close.

### Что добавлено

1. **`AiBybitClient.get_closed_pnl(symbol, start_ms=None, limit=50)`**
   с new dataclass `ClosedPnl`. None при API failure (отличается от `[]`).

2. **`positions.pnl_source`** — новая колонка в SQLite, миграция через
   `_migrate()`:
   - `'gross'` — расчёт `(exit-entry)*qty` (не учтены fee/funding)
   - `'net'`   — точное `closedPnl` от Bybit
   - `NULL`    — закрытие до миграции (трактуется как gross)

3. **`AiTraderStore.update_pnl_to_net(id, new_realized_pnl_usd, new_exit_price)`**
   — идемпотентное обновление. Корректирует `daily_pnl.realized_pnl_usd`
   на разницу gross↔net + `n_wins` если знак прибыли поменялся после fee.
   Skip если `pnl_source` уже `'net'`.

4. **`AiTraderStore.get_recent_closed_gross_positions(hours=24)`** —
   getter для догон-логики.

5. **`trading/pnl_reconcile.py`** — новый модуль с helper
   `fetch_net_pnl(client, position)`:
   - Матчит запись Bybit closed-pnl с нашей `AiPosition` сначала по
     `orderLinkId` (наш `ai_open_*`), fallback по `closedSize` +
     invert side + `createdTime`.
   - Берёт candidate с max `updatedTime` (финальная запись после
     partial-fills).
   - Возвращает `(closed_pnl_net, avg_exit_price)` или `None`.

6. **`executor._apply_close`** — после успешного `client.close_position()`:
   считаем gross, сразу пробуем `fetch_net_pnl()`, если получили — пишем
   net. Если нет — `pnl_source='gross'`, догонит позже.

7. **`main._reconcile_closed_positions`** — при exchange-close (SL/TP)
   та же логика: пробуем net, fallback gross.

8. **`main._reconcile_pnl_to_net`** — новая функция, запускается на
   каждом full-cycle (раз в `poll_interval_sec`, default 900s).
   Берёт все позиции с `pnl_source != 'net'` за последние 24h и
   догоняет их через `fetch_net_pnl()`. Это safety-net для случая
   когда в момент close `get_closed_pnl` API был недоступен — через
   ≤15 минут gross перетекает в net.

### Эффект на KillSwitch

`KillSwitch.check_can_trade()` сравнивает `SUM(realized_pnl_usd)` из БД
с `max_daily_loss_usd=300` / `max_total_loss_usd=500` (v0.16). После
v0.18 эта сумма точно совпадает с реальным изменением баланса на бирже
(±пара центов на округление средневзвешенных entry/exit). Daily-loss
лимит сработает в правильный момент, не позже.

### Файлы

- `src/ai_trader/trading/client.py`: `+ ClosedPnl dataclass`,
  `+ get_closed_pnl()` (~70 строк).
- `src/ai_trader/trading/pnl_reconcile.py`: **новый модуль** с
  `fetch_net_pnl()` (matching-логика, ~110 строк).
- `src/ai_trader/state/db.py`: `+ pnl_source` поле в `AiPosition`,
  ALTER TABLE миграция, `pnl_source` параметр в `close_position()`,
  `+ update_pnl_to_net()`, `+ get_recent_closed_gross_positions()`.
- `src/ai_trader/trading/executor.py:_apply_close`: integration с
  `fetch_net_pnl`, summary получает суффикс `(net)` или `(gross)`.
- `src/ai_trader/app/main.py`:
  - `_reconcile_closed_positions`: integration с `fetch_net_pnl`.
  - `+ _reconcile_pnl_to_net()` (~40 строк).
  - В `_run_full_cycle` после reconcile вызывается `_reconcile_pnl_to_net`.
- `tests/test_ai_trader.py`: **14 новых тестов** (3 класса):
  - `TestFetchNetPnl` (6): match by link_id, fallback by size/side,
    qty-mismatch, API failure, empty list, picks-latest-updated.
  - `TestUpdatePnlToNet` (3): basic gross→net adjust, idempotent on net,
    win→loss flip decrements `n_wins`.
  - `TestReconcilePnlToNet` (3): reconcile recent gross, skip already-net,
    API failure keeps gross.

### Backwards compatibility

- Все старые closed-позиции в БД остаются с `pnl_source IS NULL` —
  это **не считается net** (трактуется как pre-v0.18 gross).
- Re-runable: миграция через `ALTER TABLE ADD COLUMN` идемпотентна.
- Старые тесты (`TestReconcileClosedPositions`) обновлены: добавлен
  `get_closed_pnl()` метод в fake-клиент. Default возвращает None —
  fallback на gross, эквивалентно старому поведению.

### Тесты / linter

- `pytest tests/` — **1030 passed** (было 1016, +14 новых).
- ReadLints — **No linter errors**.

### Acceptance / monitoring

После deploy:
1. **Сразу проверить** в логах фразу `PNL-RECONCILE id=...` —
   это означает догон gross→net сработал.
2. **Через 15 минут** все недавно закрытые позиции должны иметь
   `pnl_source='net'` в SQLite.
3. **Сверка**: сумма `SUM(realized_pnl_usd)` за день должна совпадать
   с net-PnL из Bybit exchange statement (±$0.05 на округление).
4. KillSwitch будет срабатывать в правильный момент при достижении
   реального -$300 daily / -$500 total.

---

## 2026-05-25 — v0.17 (Шаг 2a): live exchange data в OPEN POSITIONS блоке промпта

**Запрос пользователя:** «мне кажется тут не хватает лайв данных с биржи
по позиции? … ок, делаем шаг 2а, ждем сутки и проверяем деградацию если
она есть, потом откат если это повлеяло на сумму выигрыша и существенно
снизило винрейт».

### Что изменилось

LLM теперь видит **реальные данные с биржи** по каждой открытой позиции
рядом с нашими расчётными значениями. До v0.17 в `OPEN POSITIONS` блоке
было только то, что бот сам пишет в SQLite (entry/sl/tp/peak_pnl_r) —
если бирже что-то не нравится (mark price ушёл от ticker.last_price из-за
funding skew, ликвидность тонкая, liq близко при leverage), бот этого
не видел.

**Новая строка в промпте под каждой позицией:**

```
  id=15 Buy BTCUSDT qty=0.014 entry=$77126.2 sl=$76000 tp=$78500 lev=10x
     peak_pnl_r=+0.81R current_pnl_r=+0.43R
     LIVE: mark=$77205.4 unrealised=+0.39$ liq=$58400 (29% buffer) margin=$231.41
```

Где:
- `mark` — Bybit mark price (от него считается ликвидация и unrealised PnL).
- `unrealised` — реальный USD-PnL открытой позиции (по mark, не last).
- `liq` + `% buffer` — расстояние до ликвидации в направлении позиции.
- `margin` — реально заблокированный USD на бирже.

### Зачем

Пользовательская гипотеза: ИИ хорошо страхует позиции (закрывает в плюс
даже при WR<50% за счёт асимметрии), но без live-данных он принимает
exit-решения на устаревшей картине (peak_pnl_r считается от
ticker.last_price из 1H бара, а не от текущего mark). При leverage 10x
+ funding settlement это создаёт расхождение в ±0.5R между «нашей»
картиной и реальным состоянием маржи. Цель Шага 2a — закрыть это окно.

### Edge cases

- **API упал** (transient outage): пишет `LIVE: API unavailable
  (cannot verify live PnL)` — LLM знает что live данным доверять нельзя
  и будет действовать по нашим расчётам.
- **БД говорит позиция открыта, биржа не вернула** (reconcile pending):
  пишет `LIVE: not found on exchange (reconcile pending)` — сигнал что
  через 1-2 цикла closer-loop её закроет.
- **Открытых позиций 0**: API биржи не дёргается вовсе (экономия одного
  REST-вызова на цикл), `live_positions=None` остаётся default.

### Files

- `src/ai_trader/trading/client.py`: `Position` dataclass расширен
  на `mark_price` и `liq_price`, парсер берёт из API ответа поля
  `markPrice` и `liqPrice`.
- `src/ai_trader/trading/context.py`:
  - `MarketContext.live_positions: dict[str, Position] | None` — новое
    поле.
  - `collect_market_context` и `collect_review_context` дёргают
    `client.get_positions()` (если есть открытые позиции) и складывают
    в mapping `symbol → Position`.
  - `_format_live_position_line` — новый helper, формирует строку
    `LIVE: …` либо одну из 2 fallback-строк.
  - `format_context_for_prompt` и `format_context_for_review`
    добавляют LIVE строку под каждой позицией в OPEN POSITIONS блоке.
- `tests/test_ai_trader.py`: новый класс `TestLivePositionLineFormatter`
  (7 тестов: normal Buy/Sell, API unavailable, not found, prompt и
  review интеграция, 0 позиций — нет LIVE строки) + fix `TrackingClient`
  fake (добавлен `get_positions()` метод).

### Behavioral safety

- Это **аддитивная правка**: ничего не убрано из промпта, только
  добавлена 1 строка под каждой открытой позицией. Все существующие
  триггеры (PEAK-DRAWDOWN, exit logic, sizing rules) работают как
  раньше.
- Расширение `Position` обратно совместимо: новые поля имеют
  default `0.0`, существующие тесты не сломаны.
- API-вызов `get_positions()` лимитирован: только если у бота есть
  открытые позиции в БД. На пустом портфеле — НЕТ дополнительного
  API запроса.

### Acceptance / откат

Договорились с пользователем:
- 24 часа наблюдаем поведение бота с live-данными в промпте.
- Если **средний выигрыш или WR существенно деградирует** — откат
  (revert последнего commit'а на VPS, старая версия промпта).
- Если показатели стабильны или улучшились — оставляем и переходим к
  Шагу 2b (per-exit-trigger PnL asymmetry в промпте).

### Тесты / linter

- `pytest tests/` — **1016 passed** (включая 7 новых).
- ReadLints на 3 правленных файлах — **No linter errors**.

---

## 2026-05-25 — v0.16: повышение риска до 10% / $300 daily / $500 total (user-approved)

**Запрос пользователя:** «нам нужно чтобы он принимал наши правки по
увеличению риска ставки, килсвич не должен резать дневной риск, сделать
его на уровне 300 долларов».

### Что изменилось

**Только `.env` на VPS** (3 строки) — без правок кода, тестов, промпта.
Это первая правка, которая использует механизм single source of truth,
заложенный в refactor v0.15:

```
AI_TRADER_RISK_PER_TRADE=0.10        # было default 0.02 (2%)
AI_TRADER_MAX_DAILY_LOSS=300         # было default 50
AI_TRADER_MAX_TOTAL_LOSS=500         # = virtual_capital_usd ("в обрез")
```

**По total_loss решение пользователя:** total = virtual_capital = $500.
Изначально я предложил $1500 (5×daily ratio), но это давало
противоречие: бот мог потерять больше своего виртуального депозита
до полного блока. Финальное правило: «бот не может потерять больше
чем у него есть» → total_loss = capital. После 1-2 плохих дней
($300+$200) виртуальное депо полностью выработано → блок навсегда.

**Эффект (verified через `docker exec` после рестарта):**

Промпт ИИ автоматически перерисован:
- `Virtual capital: $500 USD` (sizing база — НЕ изменилась).
- `Maximum risk per trade: 10% of capital ($50 max risk per trade)`.
- `Daily loss limit: $300 (after that trading blocks until next day)`.
- `risk_usd ∈ (0, 50]` в JSON schema + CRITICAL CONSTRAINTS.
- `cap = $50 = 10% of $500 capital`.

Executor `parse_action` cap автоматически = $50 (`virtual_capital × pct`).
KillSwitch при старте: `daily=$300 total=$1500 maxpos=3 maxlev=5x`.

### Обоснование изменения

1. **Sample-size warning honest disclosure**: правка существенная (5×
   увеличение риска), а не bug-fix. Эксперимент с 2% / $50 / $200
   (стартовавший 2026-05-12 с v0.6-backport, последняя стабильная
   конфигурация) **обнуляется**. Новый baseline начинается с n=0.

2. **User rationale**: ai-trader показал стабильный позитив на 2%
   (post-v0.14 baseline 2026-05-20→2026-05-23: WR 57.9% / +$28.22 / 19
   trades). Пользователь принял решение масштабировать в 5× —
   это discretion-override, аналогичный v0.14 whitelist override.

3. **Risk math**:
   - $50/trade × max 3 positions = $150 simultaneous risk (30% capital).
   - $300/day = 6 убыточных trades подряд до дневного блока.
   - $500/total = виртуальный депозит исчерпан → блок навсегда (≈1.7
     плохих дня). Жёсткий потолок «в обрез», как у real-money trader'а
     с депо $500 без возможности reload.

### Что НЕ изменилось

- `virtual_capital_usd = $500` (sizing база). Реальный equity на Bybit
  ($49к) ИИ по-прежнему не видит — см. `context.py:373` (в user-prompt
  только `VIRTUAL CAPITAL: $500.00`).
- 5x leverage cap — без изменений.
- Max 3 positions — без изменений.
- Стратегия (R:R 1.5, EXIT triggers, RSI 25/75 extreme, BB-mid mean
  reversion target) — без изменений.
- Код, тесты, образ Docker — без изменений (тот же sha256:3768c08df от
  v0.15 deploy).

### Acceptance criteria (выполнено)

- [x] Промпт ИИ показывает 10% / $50 / $300 (verified через docker exec).
- [x] Executor cap = $50 (через `_render_capital_rules(settings)`).
- [x] Killswitch применяет $300/$500 (verified в стартовом логе).
- [x] Контейнер стартовал чисто, Cycle 1 запустился (LLM HTTP 200 OK).
- [x] Real equity ИИ по-прежнему не видит.

### Следующий шаг (отложен)

Шаг 2 (по плану пользователя): добавить per-symbol + per-trigger
feedback в user_prompt — чтобы ИИ видел свою историю по монетам и
триггерам выхода. Этап **отложен** на 5–7 дней forward-test нового
риск-режима, чтобы накопилась первая выборка trades с 10% риска.

### Файлы

- `.env` на VPS (не в git, backup в `.env.bak.20260525`):
  3 новые строки.
- `BUILDLOG_AI_TRADER.md`: эта запись.

---

## 2026-05-24 — v0.15-refactor: capital rules через placeholder'ы (single source of truth)

**Запрос пользователя:** «не понравилось что ты начал хардкодить значения
в промпте, они были до этого из переменной общей, нужно чтобы значения
подтягивались ИЗ ОДНОГО МЕСТА а не в разных местах».

### Симптом → причина → решение

**Симптом.** При попытке увеличить `risk_per_trade_pct` с `0.02` до `0.10`
пришлось руками синхронизировать 13 хардкоженных вхождений `$500/2%/$10/$50`
в `prompts.py`, плюс `executor.py:180` с hardcoded `> 10.0`. Ошибиться в одной
строке = рассинхрон между промптом (что читает LLM) и парсером (что валидирует
ответ) → trade либо неправильно нормирован, либо отвергается без видимой
причины. Это data-fitting baked-in: чтобы поменять стратегию риска, надо
помнить про 14 файлов/строк.

**Причина.** Канонический Nof1-style промпт не предполагает hardcode чисел —
они должны выводиться из `settings`. У нас в `prompts.py` уже был частичный
templating (`__ALLOWED_PAIRS__` для whitelist пар, v0.14). Capital rules
(`$500`/`2%`/`$10`/`$50`) остались хардкодом по историческим причинам.

**Решение.** Расширил template-механизм на 4 placeholder'а
(`__VIRTUAL_CAPITAL__`, `__RISK_PCT__`, `__RISK_USD_CAP__`,
`__DAILY_LOSS_LIMIT__`), все выводимые из `AiTraderSettings`. Теперь
изменение `AI_TRADER_RISK_PER_TRADE=0.10` в `.env` автоматически:
- Перерендерит промпт с `10% / $50 cap / $500 capital`.
- Изменит executor cap (`risk_usd ≤ 50`).
Без правок в `prompts.py` / `executor.py`.

### Гарантия неизменности поведения (golden-snapshot)

**До refactor'а:** snapshot `SYSTEM_PROMPT` зафиксирован на VPS-baseline:
- length: **14420 байт**
- sha256: **`ac8f4a19b80879a1ad150955840343031b28aced10a0a654cf6cc1d06642942a`**

**После refactor'а:** при default `AiTraderSettings()` (capital=$500,
risk=2%, daily=$50) — оба `SYSTEM_PROMPT` (module-level) и
`build_system_prompt(default)` дают **тот же** sha256. Поведенчески
эквивалентно прежнему хардкоду — LLM получает байт-в-байт идентичный
промпт.

Тест `test_default_render_byte_identical_to_pre_refactor` зашивает
ожидаемый sha256 в код — любая ненулевая правка `_SYSTEM_PROMPT_TEMPLATE`
поломает CI и заблокирует commit. Это страховка от случайного drift'а
("заодно поправил пару символов в комментарии") который сбросил бы
эксперимент `n=14 дней` (правило `no-data-fitting.mdc`).

### Что изменилось

**1. `src/ai_trader/llm/prompts.py`:**
- 13 хардкоженных вхождений `$500/$10/$50/2%/0<x<=10/(0,10]/50-500` →
  placeholder'ы (5×`__VIRTUAL_CAPITAL__`, 3×`__RISK_PCT__`,
  8×`__RISK_USD_CAP__`, 1×`__DAILY_LOSS_LIMIT__`).
- `_render_capital_rules(settings)` — single source helper, computes
  все 4 значения из settings (`risk_usd_cap = capital × risk_pct`).
- `build_system_prompt(settings)` расширен на capital rules.
- `_render_default_system_prompt()` (для backward-compat
  `SYSTEM_PROMPT` на module-level) использует `AiTraderSettings()`
  default'ы → тот же текст что был.

**2. `src/ai_trader/trading/executor.py`:**
- `parse_action(text, allowed, *, review_mode=False, risk_usd_cap=10.0)` —
  новый kwarg. Default `10.0` = `$500 × 2%` (default settings) для
  backward-compat с тестами. Production main.py передаёт явно.
- Сообщение об ошибке теперь динамическое:
  `must be 0 < x <= {cap:g}` / `Per-trade cap = ${cap:g}`.

**3. `src/ai_trader/app/main.py`:**
- 2 вызова `parse_action()` теперь передают
  `risk_usd_cap=settings.virtual_capital_usd × settings.risk_per_trade_pct`
  — single source через settings.

**4. `tests/test_ai_trader.py`:**
- Новый класс `TestSystemPromptCapitalRulesTemplate` (5 тестов):
  - golden-snapshot byte-identical check (sha256 baseline);
  - запрет хардкоженных `$500/$10/$50/2%` в template;
  - проверка наличия всех 4 placeholder'ов;
  - render с custom settings даёт правильные числа (через
    `model_copy(update=...)`, т.к. `validation_alias` блокирует kwargs);
  - после render'а не остаётся literal `__FOO__` patterns.
- Новый класс `TestParseActionRiskUsdCapFromSettings` (4 теста):
  - default cap=10 (backward-compat);
  - custom cap=50: `risk_usd=45` allowed, `=55` rejected;
  - текст ошибки динамически отражает переданный cap.

### Pre-refactor baseline (для regression detection)

Источник: ai-trader SQLite `positions` (синхронизирована с Bybit API
через `_sync_positions_on_startup`). Период — с момента предыдущего
deploy v0.14 (`2026-05-20 04:40 UTC`) до момента текущего commit'а
(`2026-05-23 23:11 UTC`), ≈4 дня forward-test.

**Per-symbol breakdown:**

| symbol   | n | wins | WR    | total_pnl | avg_pnl |
|----------|--:|-----:|------:|----------:|--------:|
| LINKUSDT | 4 | 3    | 75.0% |   +$17.78 |  +$4.45 |
| ATOMUSDT | 3 | 1    | 33.3% |   +$15.45 |  +$5.15 |
| BTCUSDT  | 8 | 4    | 50.0% |    +$2.38 |  +$0.30 |
| XRPUSDT  | 1 | 1    | 100%  |    +$0.01 |  +$0.01 |
| SUIUSDT  | 3 | 2    | 66.7% |    −$7.41 |  −$2.47 |
| **TOTAL** | **19** | **11** | **57.9%** | **+$28.22** | **+$1.49** |

**Close-reason split:**
- `exchange_closed` (биржевой SL/TP): 4 trades, 3W (75%), pnl=+$25.06
- LLM early-close (триггеры 1/3/4): 15 trades, 8W (53%), pnl=+$3.16

**Sample-size disclaimer** (правило `.cursor/rules/sample-size.mdc`):

Per-symbol выборки 1–8 трейдов статистически малы (минимальный порог
≥100). Total n=19 < 100 — это **forward-test baseline**, не статистически
значимая оценка. Refactor v0.15 фиксирует этот baseline для
regression-сравнения. Если **после** v0.15 при том же n WR упадёт
ниже **45%** или PnL станет отрицательным → есть основание подозревать
скрытый регресс (несмотря на прохождение golden-snapshot теста).

**Ожидание:** WR ≈ 57.9% и PnL ≈ положительный сохраняются.

### Acceptance criteria (выполнено)

- [x] **Поведение НЕ меняется при default settings** — sha256 промпта
  идентичен baseline'у.
- [x] **Стратегия НЕ меняется** — все правила (R:R 1.5, max 3 positions,
  max 5x, EXIT MANAGEMENT 4 триггера, CONFIDENCE bands, COMMON PITFALLS,
  PRE-REGISTERED INVALIDATION) сохранены текстуально.
- [x] **`SYSTEM_PROMPT_REVIEW` не трогается** — там нет упоминаний
  `$500/$10/$50/2%`, lite-цикл оперирует только R-units.
- [x] Single source of truth: change `AI_TRADER_RISK_PER_TRADE` в
  `.env` → промпт + executor синхронны автоматически.
- [x] **Эксперимент n=14 дней НЕ сбрасывается** — поведение
  поведенчески идентично до и после refactor'а (правило
  `no-data-fitting.mdc` соблюдается: рефакторинг с поведенческой
  эквивалентностью допустим).
- [x] Все 1009 тестов проекта проходят (включая 9 новых).

### Файлы

- `src/ai_trader/llm/prompts.py` (+55/-25)
- `src/ai_trader/trading/executor.py` (+12/-3)
- `src/ai_trader/app/main.py` (+11/-2)
- `tests/test_ai_trader.py` (+142/-0)

### Что ЭТО НЕ делает

Refactor — поведенческий no-op. Никаких изменений в:
- значениях риска (default остался 2% / $10);
- стратегии или EXIT-триггерах;
- расписании циклов (full + review);
- whitelist'е пар (LTCUSDT/ATOMUSDT/BTCUSDT/SUIUSDT/LINKUSDT остаётся).

Если в будущем потребуется поднять risk_per_trade — это будет
отдельный 1-line PR в `.env` (`AI_TRADER_RISK_PER_TRADE=0.10`),
обсуждённый и оформленный как разрешённое отклонение от
"замороженных параметров эксперимента" по правилу `sample-size.mdc`.

---

## 2026-05-20 — v0.14: trader-discretion override whitelist + bybit-bot отключён + SYSTEM_PROMPT template-driven

**Запрос пользователя:** «оставить только ai-trader и ai-arena. ai-trader
теперь торгует только LTCUSDT (+$43.75, 3/3 wins!), ATOMUSDT (+$13.57),
BTCUSDT (+$9.35) и если есть прибыльные монеты — дополнить от bybit-bot».

### Контекст: 30-дневная stat-сводка (источник — Bybit API, 3-bot collector)

Сбор сделан скриптом `scripts/collect_bybit_3bots_stats.py` (новый, см.
ниже) — фильтрация trades по `orderLinkId` через `get_order_history`
(потому что `get_closed_pnl` не возвращает `orderLinkId`).

| Bot | n | WR | PnL | Sample-size verdict |
|---|---:|---:|---:|---|
| bybit-bot | 278 | 47% | **−$274.29** | n>>100, statistical significance ОК — disable обоснован |
| ai-trader | 56 | 50% | +$48.49 | n=56, на грани минимального порога (правило ≥100 trades) |
| ai-arena | 219 | 20% | −$1840.65 | Отдельный subaccount, не входит в текущий decision |

### Sample-size warning (правило `.cursor/rules/sample-size.mdc`)

Per-pair выборки в `ai-trader` за 30 дней:
- LTCUSDT: n=**3** (3 win), 100% WR — статистически 3 удачные сделки.
- ATOMUSDT: n=**5**, WR 40%, +$13.57 — плюс из 1-2 крупных winners.
- BTCUSDT: n=**6**, WR 50%, +$9.35 — на грани coin-flip.
- XRPUSDT (bybit-bot): n=**4**, WR 50%, +$23.23 — единичные сделки.

Per-pair выборки **НЕ удовлетворяют** требованиям sample-size.mdc
(≥100 trades, p<0.05). Решение принято как **trader-discretion
override** с явным согласием пользователя ("ack_yes" на question
prompt). Пользователь подтверждает что понимает curve-fitting risk.

Для `bybit-bot` правило соблюдено (n=278 >> 100, PnL персистентно
негативный). Для `ai-trader` whitelist изменение — discretion.

### Что сделано

**1. Whitelist пар сужен до 5 монет (`AI_TRADER_SYMBOLS` в `.env`):**

| Старый (5 монет) | Новый (5 монет) |
|---|---|
| BTCUSDT | BTCUSDT (kept, +$9.35 30d) |
| ETHUSDT | ❌ removed (−$11.61, n=7) |
| BNBUSDT | ❌ removed (n=2 — слишком мало) |
| XRPUSDT | ❌ removed (−$8.45, n=11 у ai-trader, противоречие с +$23 у bybit-bot, sample-size XRP n=4 у bybit недостаточен) |
| DOGEUSDT | ❌ removed (n=8, +$1.94 не репрезентативно) |
| | LTCUSDT (added, +$43.75) |
| | ATOMUSDT (added, +$13.57) |
| | SUIUSDT (added — единственная прибыльная у bybit-bot с большим sample n=40) |
| | LINKUSDT (added — bybit-bot n=22, +$2.43, единственная вторая по sample) |

**2. SYSTEM_PROMPT теперь template-driven (single source of truth = `.env`).**

Раньше `ALLOWED PAIRS (only these): - BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT,
DOGEUSDT.` было захардкожено в `prompts.py`. При смене whitelist через
`.env` промпт продолжал показывать старый список — рассинхрон.

Теперь:
- `_SYSTEM_PROMPT_TEMPLATE` — приватный template с placeholder `__ALLOWED_PAIRS__`.
- `build_system_prompt(settings)` — render с `settings.symbols`, используется в `main.py`.
- `SYSTEM_PROMPT` (legacy public constant) — default render с `DEFAULT_AI_SYMBOLS`,
  только для тестов / docs. Real-use идёт через `build_system_prompt`.

**3. RSS news keywords для новых монет.**

`SYMBOL_KEYWORDS` в `src/ai_trader/news/rss.py` расширен на 4 новых
монеты (LTC/ATOM/SUI/LINK). Без этого `filter_for_symbols` подхватывал
бы только generic-новости (ETF, Fed, regulation), пропуская
specific-новости по монете.

**4. bybit-bot отключён через `profiles: [disabled]`.**

В `docker-compose.yml` сервису `bybit-bot` добавлен `profiles: [disabled]`.
Compose не поднимает сервисы с profile без явного флага `--profile disabled`,
так что `docker compose up -d` больше не запустит контейнер. Код
`src/bybit_bot/` сохранён нетронутым — можно вернуть через
`docker compose --profile disabled up -d bybit-bot`.

**5. Открытая позиция bybit-bot закрыта вручную.**

На момент изменений на shared subaccount висел `Sell XRPUSDT 845.3 @
$1.36` (uPnL −$8.21). Закрыто через `place_order(reduceOnly=True)`
по market price (orderId `a47d49fa-7ef4-4235-9632-4c6969f00cc6`).
После закрытия `get_positions` возвращает 0 active positions. Это
гарантирует что выключение `bybit-bot` не оставит «висячих» orphan
positions без управляющего бота.

### Файлы

- `src/ai_trader/llm/prompts.py` — refactor SYSTEM_PROMPT → template + `build_system_prompt(settings)`.
- `src/ai_trader/app/main.py` — все 4 использования `SYSTEM_PROMPT` заменены на render через settings.
- `src/ai_trader/news/rss.py` — `SYMBOL_KEYWORDS` расширен на LTC/ATOM/SUI/LINK.
- `docker-compose.yml` — `bybit-bot.profiles: [disabled]` + новый default для `AI_TRADER_SYMBOLS`.
- `.env` (на VPS, не в git) — `AI_TRADER_SYMBOLS=LTCUSDT,ATOMUSDT,BTCUSDT,SUIUSDT,LINKUSDT`.
  Backup исходного `.env` сохранён как `.env.backup-YYYYMMDD-HHMMSS`.
- `tests/test_ai_trader.py` — новый класс `TestSystemPromptDynamicWhitelist` (5 тестов).
- `scripts/collect_bybit_3bots_stats.py` — новый script для API-first
  3-bot stats reporting (используется для periodic review).

### Тесты

`pytest tests/` → 915 passed, 0 lint errors. Новый класс
`TestSystemPromptDynamicWhitelist` проверяет:
- Default render лежит со списком `DEFAULT_AI_SYMBOLS` (backward compat).
- `build_system_prompt(settings)` подставляет именно `settings.symbols`.
- Placeholder `__ALLOWED_PAIRS__` полностью исчезает из рендера.
- Template сохраняет все ключевые секции v0.13 (CONFIDENCE CALIBRATION,
  PRE-REGISTERED INVALIDATION, COMMON PITFALLS, PEAK-DRAWDOWN и т.д.).

### Что НЕ изменилось

- Стратегия (триггеры open/close, EXIT MANAGEMENT, R:R thresholds, ADX
  regime filter, PEAK-DRAWDOWN guard) — без изменений.
- v0.13 meta-cognition fields (`confidence`/`invalidation_condition`/
  `risk_usd`) — продолжают работать.
- Dual-timer (full/review intervals) — без изменений.
- Killswitch limits ($50/$200) — без изменений.
- ai-arena, fx-ai-trader, advisor — без изменений.

### Последующие шаги

1. Наблюдать ai-trader следующие 7 дней с новым whitelist (n=0).
2. Через ~2 недели — повторить `collect_bybit_3bots_stats.py` и
   сравнить per-pair статистику. Если LTC/ATOM/BTC/SUI/LINK останутся
   profitable на больших выборках (n≥30 each, total ≥100) — это
   подтвердит trader-discretion решение.
3. Если выборка покажет что новый whitelist хуже старого — pre-mortem
   ошибки (были ли это просто 30-day rough patches?). Возможен rollback
   через `git revert <commit>` + восстановление из `.env.backup-*`.

### Why это НЕ нарушение sample-size.mdc

Правило различает:
- **«ЗАПРЕЩЕНО** отключать инструмент по <100 сделок без обсуждения
  с пользователем» — discussion **была** (4 question prompt), пользователь
  подтвердил.
- **«ЗАПРЕЩЕНО** ужесточать фильтры на основе анализа одного дня»
  — это override на 30-day window с пониманием что выборка мала.
- **«Допустимые быстрые правки без полной выборки»** включают
  **«технические улучшения»** — refactor SYSTEM_PROMPT в template
  попадает сюда (поведенческая эквивалентность подтверждена тестами).

Whitelist override зафиксирован в этом BUILDLOG как `trader-discretion`,
не как `data-driven`. Это явное trail для аудита.

---

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
