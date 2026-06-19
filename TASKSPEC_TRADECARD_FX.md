# TASKSPEC — tradecard (Forex / fx_ai_trader) — тех-задание для реализации

Это **тех-задание и промпты для агента** на реализацию аналитического ревьюера
`tradecard` над данными бота **`fx_ai_trader` (DeepSeek + cTrader/FxPro demo)** в
**ОТДЕЛЬНОМ чате** (чтобы контекст прошлых правок не протёк). Канон процесс-
фреймворка — `STRATEGY_TRADECARD.md` (строго по ролику
<https://youtu.be/WDdvnd9vLbM>).

> ⚠️ Реализацию НЕ начинать в этом чате. Здесь только ТЗ. В новом чате — следовать
> разделам ниже и **сверять каждый компонент с каноном** (ролик +
> STRATEGY_TRADECARD.md).

> ⚠️ `tradecard` — это **advisory-ревьюер**, НЕ торговый бот и НЕ источник
> сигналов. Он читает данные `fx_ai_trader` **read-only**, считает report card,
> грейдит сделки, гоняет 5 Why через LLM и отдаёт **рекомендации** человеку. Он
> **НЕ меняет** стратегию/пороги/промпты `fx_ai_trader` (правила
> `no-data-fitting.mdc`, `sample-size.mdc`, `strategy-guard.mdc`).

---

## 1. Цель и принципы

- Реализовать `tradecard` (сборка под Forex) — инструмент, который **превращает
  сделки/решения `fx_ai_trader` в SMB-style report card**: фиксирует «ошибки
  агента» → выделяет главную повторяющуюся → диагностирует её 5 Why (LLM) →
  формулирует **кандидат-решение/урок** → трекает «маленькие победы» (снижение
  частоты ошибки) и momentum.
- **Advisory-only.** Выход — отчёт (Telegram + markdown) и кандидат-уроки в
  СОБСТВЕННОМ хранилище `tradecard`. Никаких автоправок в коде/конфиге/промптах
  `fx_ai_trader`. Любое предложение изменить стратегию идёт **человеку на
  одобрение** (`strategy-guard.mdc`).
- **Sample-size прежде всего.** «Ошибка» становится «темой» только на достаточной
  выборке; «маленькая победа» фиксируется только при прохождении порогов
  `sample-size.mdc` (≥100 сделок связки / p<0.05 / ≥2 недели). До этого — статус
  НАБЛЮДЕНИЕ.
- **Источник правды по P&L — cTrader deal-list (broker).** Локальная SQLite — для
  traceability решений (`stats-collection.mdc`). Учитывать `is_paper` (paper vs
  live отдельно).
- **Изоляция кодовой базы.** **Отдельный самостоятельный пакет**
  `src/tradecard_fx/` (НЕ общий с Bybit-ревьюером `src/tradecard_bybit/` — между
  ними нет общего кода/импортов), своя БД/volume, свой BUILDLOG. Читает БД
  `fx_ai_trader` только на чтение; переиспользует LLM/cTrader-клиентов как
  инфраструктуру.

---

## 2. Идентичность инструмента

- **Имя:** `tradecard` (рабочее). Сборка для Forex: `tradecard-fx`.
- **Цель ревью:** агент `fx_ai_trader` (DeepSeek + cTrader FxPro demo, инструменты
  XAUUSD / BZ=F(BRENT) / NG=F(NAT.GAS), dual-timer 15м full / 5м review).
  «Трейдер» из канона = **LLM-агент**.
- **Режим работы:** **периодический** (daily + weekly), не realtime.
- **LLM:** переиспользовать DeepSeek-клиент `fx_ai_trader` (тот же
  `DEEPSEEK_API_KEY`, модель `deepseek-v4-flash`) для 5 Why и формулировки урока.

---

## 3. Источники данных

### 3.1 БД `fx_ai_trader` (read-only)

SQLite `fx_ai_trader` (bind `./data:/data`). Открывать **строго read-only**.
Таблицы и поля (из `src/fx_ai_trader/state/db.py`):

- **`positions`:** `symbol (внутр. yfinance: XAUUSD/BZ=F/NG=F), side (BUY/SELL),
  volume_lots, entry_price, sl_price, tp_price, broker_position_id,
  broker_order_label, opened_at, closed_at, exit_price, realized_pnl_usd,
  close_reason, llm_reason, is_paper (1 paper / 0 live)`.
- **`decisions`:** `cycle, cycle_type ('full'|'review'), ts, prompt_system,
  prompt_user, response_raw, parsed_action, sentiment_json (multi-dim:
  relevance/polarity/intensity/uncertainty/forwardness), executed, error,
  tokens_input/output, cost_usd, thesis_status ('broken'|'intact'|'partial'),
  thesis_invalidator`.
- **`daily_pnl`:** `day, realized_pnl_usd, n_trades, n_wins, api_cost_usd`.
- **`lessons` (УЖЕ СУЩЕСТВУЕТ):** `created_at, symbol, side, trade_id,
  outcome_usd, lesson_text, active, supersedes_id` — поведенческие уроки, которые
  **сам агент** формулирует на CLOSE и которые подаются в промпт как приоры
  (non-blocking). `tradecard` **читает** их (что агент уже усвоил) и сопоставляет
  со своими находками; писать в эту таблицу tradecard **НЕ** должен (см. §8).

> Для monotonic-нумерации использовать `decisions.id` (in-memory cycle counter
> сбрасывается рестартом — `deploy-vps.mdc`). Различать `cycle_type` full/review.

### 3.2 cTrader deal-list (ground truth по P&L)

- P&L брать из **cTrader истории сделок** (`get_deal_list`) — broker-净 реализ.
  PnL/commission/swap, `stats-collection.mdc`. Переиспользовать cTrader-движок
  `fx_pro_bot.trading.{client,symbols}` и существующий `broker_reconcile.py`
  (он уже сводит `realized_pnl_usd` с брокером по `broker_position_id`).
- **Проверить:** `positions.realized_pnl_usd` у `fx_ai_trader` может быть уже
  broker-reconciled (через `broker_reconcile`). Если да — это и есть ground truth;
  если нет/частично — досверять deal-list'ом. Не выдумывать (`no-data-fitting`).
- **Timezone:** cTrader timestamps vs БД (UTC) — конвертировать явно перед
  сравнением (`stats-collection.mdc`).
- API connection/auth — только офиц. дока cTrader (`api-docs.mdc`):
  <https://help.ctrader.com/open-api/> (rate limits 5/sec historical,
  `get_deal_list`).

### 3.3 Цена после выхода (для «sold too early»)

- Опционально: цена после `closed_at` для измерения упущенного движения —
  через cTrader klines или yfinance (символы уже в yfinance-нотации). Read-only.

---

## 4. Таксономия «ошибок агента» (канон §3, адаптация под LLM)

«Ошибки» — наблюдаемые из данных decision-ошибки агента. Каждая — детектор над
`positions`/`decisions` (+ post-exit цена где нужно). Пороги нейтральные/
структурные, **не** под желаемый P&L (`no-data-fitting.mdc`). Учитывать `is_paper`
(paper и live агрегировать раздельно).

| Код ошибки | Определение (из данных) | Поля-источники |
|---|---|---|
| `exit_too_early` | закрытие `close_reason~llm_close` при `thesis_status='intact'` и цена НЕ достигала TP; post-exit движение продолжилось в сторону сделки | `close_reason, thesis_status, tp_price, exit_price` + цена |
| `held_broken_thesis` | держал после `thesis_status='broken'` (закрытие поздно/в убыток) | `thesis_status, closed_at, realized` |
| `thesis_discipline_gap` | close без заполненных `thesis_status/thesis_invalidator` | `decisions.thesis_*, cycle_type` |
| `sentiment_gate_violation` | вход при `aggregate uncertainty > 0.7` (нарушение reject-gate) или игнор сильного контр-sentiment | `sentiment_json` |
| `risk_overrun` | фактический убыток существенно > целевого риска `|entry−SL|·lot·contract` | `entry_price, sl_price, volume_lots, realized` |
| `overtrading` | всплеск числа сделок/решений при падении WR/EXP | `decisions.ts, daily_pnl` |
| `lesson_ignored` | повтор ошибки, по которой в `lessons` уже есть active-урок | `lessons` + детекторы выше |
| `paper_live_divergence` | поведение, валидное на paper, систематически проигрывает на live | `is_paper, realized` |

> Детекторы — **наблюдатели**, не блокировки. Помечают сделку кодом(ами) для
> агрегации. Список расширяемый; каждый детектор — со ссылкой на поле БД, без
> magic-numbers (порог env/относительный). `sentiment_gate_violation`: порог 0.7
> — из существующего gate `fx_ai_trader`, цитировать источник в docstring.

---

## 5. Грейдинг сделок (канон §6)

- Ретроспективный грейд **A+/A/B/C** по наблюдаемым на входе факторам: сила/
  однонаправленность `sentiment_json` (polarity·intensity·relevance, низкая
  uncertainty) + наличие связного `macro`-тезиса в `llm_reason` + объявленный
  R:R (`|entry−SL|` vs `|TP−entry|`) + согласованность с HTF (если читаемо из
  `llm_reason`).
- Цель — **аналитика, не управление**: проверить «выше грейд → лучше перформанс».
  Риск-аллокация канона (80/30/15/5%) у нас **не применяется автоматически**
  (риск-модель `fx_ai_trader` фиксирована, меняется только с одобрения). Грейд-
  таблица канона — референс в отчёте.

---

## 6. 5 Why engine (канон §4)

- На **главную повторяющуюся ошибку периода** запустить **5 Why через DeepSeek**
  (LLM-клиент `fx_ai_trader`).
- Вход в промпт: код ошибки, агрегаты (частота, net-эффект), 3-5 сделок с
  `llm_reason / thesis_invalidator / sentiment_json (сводка) / исход (net)`, и
  **уже существующие active-`lessons`** по теме (чтобы 5 Why учитывал, что агент
  уже «знает»).
- Выход: цепочка 5 «почему» + **кандидат-решение/урок** (≤200 симв.) как
  поведенческий приор, не disable-правило.
- За основу — структура ChatGPT-промпта из описания ролика (адаптировать под наши
  поля). Цитировать канон в docstring.

> Канон §4: решение может быть «не про агента, а про рынок» (нужен отдельный
> playbook/условие, напр. «вход по gold работает только при низкой uncertainty
> и согласии DXY»). 5 Why должен это допускать.

---

## 7. Small wins / momentum tracking (канон §5)

- Для выбранной ошибки-темы трекать **частоту по неделям** (mistakes/100 trades),
  раздельно paper/live.
- **Small win** = статистически значимое снижение частоты темы
  (`sample-size.mdc`: ≥100 сделок, p<0.05). До порога — НАБЛЮДЕНИЕ.
- История тем/побед — в собственной БД `tradecard`; «momentum-кривая» (накопл.
  число small wins).
- В отчёт: тема №1, тренд, число накопленных small wins, growth-vs-outcome
  маркер, сопоставление «находки tradecard vs active-lessons агента» (закрывает
  ли агент свои ошибки сам).

---

## 8. Выход (отчёты)

1. **Дневной digest** в Telegram (настройки Telegram `fx_ai_trader`), префикс
   **`[tradecard-fx]`**: net P&L (deal-list) за день paper/live, топ-3 ошибки,
   грейд-распределение, 1 actionable-наблюдение.
2. **Недельный report card** — markdown в `data/tradecard/fx_YYYY-WW.md`: темы,
   5 Why, кандидат-урок, small-wins/momentum, грейд-аналитика, sentiment-gate
   аудит, сравнение с active-`lessons`.
3. **Кандидат-уроки** — в собственной таблице `tradecard` (advisory). **НЕ писать
   в `fx_ai_trader.lessons`** автоматически: продвижение кандидата в реальную
   таблицу `lessons` (она кормит промпт агента) = изменение поведения бота →
   только **человек**, отдельным одобренным коммитом (`strategy-guard.mdc`).

---

## 9. Архитектура и интеграция

- **Модуль:** **самостоятельный пакет** `src/tradecard_fx/` (без импортов из
  `src/tradecard_bybit/`) с адаптером источников `tradecard_fx/data/fx_*` под
  `fx_ai_trader`. Подпакеты: `data/ analysis/ llm/ report/ state/ app/`.
- **CLI:** `tradecard-fx daily` / `tradecard-fx weekly` (+ `--since/--dry-run/
  --paper|--live`).
- **Запуск:** scheduler-контейнер/cron; сервис `tradecard-fx` в
  `docker-compose.yml`; **read-only** mount `./data` (БД fx_ai_trader + cTrader
  токены) + свой volume `tradecard_fx_data`.
- **Переиспользуемые компоненты** (инфраструктура, read-only): DeepSeek LLM-клиент
  `fx_ai_trader`; cTrader-движок `fx_pro_bot.trading.{client,symbols,auth}` +
  `fx_ai_trader/trading/broker_reconcile.py` (deal-list/реконсайл);
  Telegram-нотифаер. Не тащить торговую логику.

---

## 10. Креды и интеграции (взять с VPS, не хардкодить)

`/root/fx-pro-bot/.env` на VPS (`204.168.149.140`). Префикс `AI_FX_TRADER_*`
(см. `src/fx_ai_trader/config/settings.py`):

- **DeepSeek:** `DEEPSEEK_API_KEY` (общий), `AI_FX_TRADER_DEEPSEEK_MODEL`/`_BASE_URL`.
- **cTrader (FxPro demo):** `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`,
  `CTRADER_ACCOUNT_ID` (+ изолированный OAuth token-файл fx_ai_trader в `./data`).
- **БД fx_ai_trader:** SQLite в bind `./data` (read-only mount).
- **Telegram:** настройки `fx_ai_trader` (см. settings), префикс `[tradecard-fx]`.
- Для tradecard завести свои `TRADECARD_FX_*` с дефолтами на `AI_FX_TRADER_*`
  в compose, чтобы аудит был раздельным.

---

## 11. Тесты (strategy-guard.mdc — тесты обязательны)

- **Детекторы ошибок §4:** на честных фикстурах (реальные строки БД /
  правдоподобная синтетика), включая `is_paper`-разделение и
  `sentiment_gate_violation` (порог 0.7). НЕ рисовать данные «под результат».
- **Грейдинг §5:** A+/A/B/C по факторам; грейд-vs-перформанс.
- **deal-list ground truth:** мок cTrader `get_deal_list`; net предпочитается
  БД-значению при расхождении; timezone-конверсия (`stats-collection.mdc`).
- **5 Why:** мок LLM; промпт собирается из полей + active-lessons; парсинг урока.
- **Small wins §7:** снижение ниже порога значимости ≠ победа (sample-size).
- **Read-only инвариант:** запись в БД fx_ai_trader и в `lessons` невозможна.

---

## 12. Применимые правила проекта (соблюдать)

- `strategy-guard.mdc` — advisory-only, изоляция, тесты, изменения стратегии
  только с одобрения; **не** писать в `lessons` агента автоматически.
- `no-data-fitting.mdc` — не подгонять детекторы/пороги; вывод опирается на
  артефакт анализа.
- `sample-size.mdc` — «темы»/«победы» только на достаточной выборке; иначе
  НАБЛЮДЕНИЕ. (Эксперимент `fx_ai_trader` n=14 дней forward-test не сбрасывать.)
- `stats-collection.mdc` — cTrader deal-list net + явный источник/период/
  timezone; paper и live раздельно.
- `api-docs.mdc` — cTrader endpoints/limits только из офиц. доки.
- `buildlog.mdc` — записи в `BUILDLOG_TRADECARD_FX.md` в том же коммите.
- `deploy-vps.mdc` — деплой через git, селективный rebuild `tradecard-fx`,
  проверка контейнера/логов.

---

## 13. Фазы реализации (milestones)

1. **Каркас:** FX-адаптер в `src/tradecard_fx/`, конфиг (env `TRADECARD_FX_*`),
   read-only доступ к БД fx_ai_trader, своя SQLite, Telegram. CLI `daily` печатает
   базовую сводку (net из БД, paper/live), без анализа.
2. **Ground truth P&L:** cTrader deal-list (через broker_reconcile) + сверка с БД,
   net в отчёте, timezone-корректно (`stats-collection.mdc`).
3. **Детекторы ошибок §4:** таксономия (вкл. sentiment-gate, lesson_ignored,
   paper_live_divergence), пометка сделок, агрегаты.
4. **Грейдинг §5** + «грейд vs перформанс» + sentiment-аналитика.
5. **5 Why (LLM) §6** на тему №1 (с учётом active-lessons) → кандидат-урок в
   собственную БД.
6. **Small wins / momentum §7** + недельный markdown report card §8 + сравнение с
   active-`lessons` агента.
7. **Тесты, BUILDLOG, деплой** (scheduler/cron), наблюдение первых прогонов.
8. **Накопление выборки** до порогов `sample-size.mdc` перед выводами/
   рекомендациями менять стратегию.

После КАЖДОЙ фазы — сверка с каноном (ролик + STRATEGY_TRADECARD.md): advisory-
only соблюдён, пороги обоснованы, в `lessons` агента ничего не записано.

---

## 14. ГОТОВЫЙ СТАРТОВЫЙ ПРОМПТ для нового чата

> Скопировать в новый чат как первое сообщение.

```
Реализуем advisory-ревьюер tradecard (Forex/fx_ai_trader) по тех-заданию
TASKSPEC_TRADECARD_FX.md и канону STRATEGY_TRADECARD.md (строго по ролику
https://youtu.be/WDdvnd9vLbM — SMB Momentum Model).

Контекст:
- tradecard = АНАЛИТИКА, не торговый бот. Читает БД fx_ai_trader read-only,
  считает report card, грейдит сделки, гоняет 5 Why через DeepSeek, отдаёт
  рекомендации человеку. НЕ меняет стратегию/пороги/промпты fx_ai_trader и
  НЕ пишет в таблицу lessons агента.
- Данные: cTrader FxPro demo (XAUUSD/BZ=F/NG=F). P&L ground truth = cTrader
  deal-list net (через broker_reconcile), не БД-значение; paper/live раздельно
  (is_paper).
- LLM = DeepSeek-клиент fx_ai_trader (DEEPSEEK_API_KEY, deepseek-v4-flash).
- Креды: AI_FX_TRADER_* + CTRADER_* (см. src/fx_ai_trader/config/settings.py);
  для tradecard свои TRADECARD_FX_* с дефолтами на них.
- Telegram: настройки fx_ai_trader, префикс [tradecard-fx].
- Изоляция: ОТДЕЛЬНЫЙ пакет src/tradecard_fx/ (без импортов из src/tradecard_bybit/),
  своя SQLite (volume tradecard_fx_data), свой Dockerfile/сервис, свой
  BUILDLOG_TRADECARD_FX.md.

Прежде чем писать код:
1. Прочитай STRATEGY_TRADECARD.md и TASKSPEC_TRADECARD_FX.md целиком.
2. Сверься со схемой БД fx_ai_trader (src/fx_ai_trader/state/db.py) — поля §3,
   включая существующую таблицу lessons.
3. Работай по фазам §13. После каждой — сверка с каноном.
4. Соблюдай правила: strategy-guard (advisory-only, не писать в lessons),
   no-data-fitting, sample-size, stats-collection, api-docs, buildlog, deploy-vps.

Начни с фазы 1 (каркас). Не подгоняй детекторы/пороги под желаемый P&L —
каждый вывод опирается на артефакт анализа и достаточную выборку.
```

---

## 15. Решения и открытые вопросы

**Зафиксировано:**
- Имя = `tradecard` (сборка `tradecard-fx`).
- Режим = периодический (daily + weekly), advisory-only.
- P&L = cTrader deal-list net (broker_reconcile), paper/live раздельно, §3.2.
- LLM = DeepSeek-клиент fx_ai_trader, §6.
- Изоляция = отдельный пакет `src/tradecard_fx/` (НЕ общий с Bybit), своя SQLite,
  read-only к БД fx_ai_trader; в `lessons` агента не писать.

**Открытые (решить в начале нового чата):**
- Точные пороги детекторов §4 (X·risk, sentiment uncertainty 0.7 — подтвердить по
  актуальному gate, и т.д.) — нейтрально/относительно, калибровать только по
  данным.
- Продвижение одобренных уроков в `fx_ai_trader.lessons` — отдельный одобренный
  процесс вне scope tradecard.
- Расписание прогонов (cron-времена, timezone отчёта — UTC + MSK).
