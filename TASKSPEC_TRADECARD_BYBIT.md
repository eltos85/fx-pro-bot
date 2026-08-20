# TASKSPEC — tradecard (Bybit / scalp_bot + hybrid_bot) — тех-задание

> 2026-08-20: второй покрываемый бот заменён. Вместо удалённого order-flow бота
> ревьюер читает `hybrid_bot` (регулярная фиксация тренда от средней цены входа,
> STRATEGY_HYBRID.md §17.4). Схема `trades` та же, поэтому загрузчик и метрики
> не изменились; исторические абзацы ниже переписаны на новое имя.

Это **тех-задание и промпты для агента** на реализацию аналитического ревьюера
`tradecard` над данными **детерминированных Bybit-ботов** — **`scalp_bot`**
(orderflow sweep/density, Bybit demo) и **`hybrid_bot`** (фиксация тренда от
средней цены входа, Bybit demo) — в **ОТДЕЛЬНОМ чате** (чтобы контекст прошлых правок не
протёк). Канон процесс-фреймворка для этих ботов —
`STRATEGY_TRADECARD_BYBIT.md` (deterministic-чтение ролика
<https://youtu.be/WDdvnd9vLbM>). Универсальный канон — `STRATEGY_TRADECARD.md`.

> ⚠️ Реализацию НЕ начинать в этом чате. Здесь только ТЗ. В новом чате — следовать
> разделам ниже и **сверять каждый компонент с каноном** (ролик +
> STRATEGY_TRADECARD_BYBIT.md).

> ⚠️ `tradecard` — **advisory-ревьюер**, НЕ торговый бот и НЕ источник сигналов и
> НЕ автотюнер конфига. Он читает БД `scalp_bot` / `hybrid_bot` **read-only**,
> считает report card, грейдит сделки по `score`, гоняет 5 Why через LLM и отдаёт
> **рекомендации человеку**. Он **НЕ меняет** пороги/правила/конфиг ботов
> (`no-data-fitting.mdc`, `sample-size.mdc`, `strategy-guard.mdc`).

> ⚠️ Главное отличие от FX-версии: эти боты **детерминированные** (нет LLM, нет
> `decisions`/`thesis`/`sentiment`). «Ошибка» = повторяющийся убыточный **паттерн
> правил** (страта × режим × сессия × символ × score-бакет), а не психология.
> «Маленькая победа» засчитывается **только после OOS-валидации и одобрения
> человеком** правки стратегии. Соблазн подгонки максимален → `no-data-fitting.mdc`
> здесь центральное правило.

---

## 1. Цель и принципы

- Реализовать `tradecard` (сборка под Bybit) — инструмент, который **превращает
  сделки `scalp_bot` / `hybrid_bot` в SMB-style report card**: фиксирует
  повторяющиеся убыточные паттерны → выделяет главный → диагностирует его 5 Why
  (LLM, read-only) → формулирует **гипотезу-решение/playbook-кандидат** → трекает
  «маленькие победы» (OOS-подтверждённое снижение паттерна) и momentum.
- **Advisory-only.** Выход — отчёт (Telegram + markdown) и кандидат-гипотезы в
  СОБСТВЕННОМ хранилище `tradecard`. Никаких автоправок порогов/правил ботов.
  Любое предложение менять стратегию идёт **человеку на одобрение**
  (`strategy-guard.mdc`).
- **Sample-size + OOS прежде всего.** «Паттерн» становится «темой» только на
  достаточной выборке; «маленькая победа» фиксируется только при прохождении
  порогов `sample-size.mdc` (≥100 сделок связки / ≥2 недели / p<0.05 / WR≥10% или
  R:R≥0.3) **на forward/OOS-данных после внедрения**. До этого — НАБЛЮДЕНИЕ.
- **Источник правды по P&L — Bybit `closedPnl` (net).** Использовать
  `pnl_verified=1` (true-up против биржи) как ground truth; `pnl_provisional`
  отделять. Локальная SQLite — для traceability (`stats-collection.mdc`).
  Учитывать `mode` (paper vs live раздельно).
- **Изоляция кодовой базы.** **Отдельный самостоятельный пакет**
  `src/tradecard_bybit/` (НЕ общий с FX — FX-ревьюер живёт в своём `src/tradecard_fx/`),
  своя БД/volume, свой BUILDLOG. Читает БД ботов только на чтение; LLM-клиент
  инлайнится внутри пакета (read-only-инфраструктура).

---

## 2. Идентичность инструмента

- **Имя:** `tradecard` (рабочее). Сборка для Bybit: `tradecard-bybit`.
- **Цели ревью (два детерминированных бота):**
  - **`scalp_bot`** — Bybit demo, orderflow HF-скальп; мультистратегийный
    (`sweep_fade`, `sweep_fade_canon`, `density_break`, `density_bounce`),
    `score` = число факторов сетапа; держание ~секунды-минуты.
  - **`hybrid_bot`** — Bybit demo, фиксация тренда от средней цены входа,
    страта `hybrid_fix_from_avg`, `score` не используется; фаза observe
    (`trading_enabled=False` по умолчанию).
  «Трейдер» из канона = **связка конфиг-стратегии + человек-оператор** (бот без
  дискреции, см. STRATEGY_TRADECARD_BYBIT §2).
- **Режим работы:** **периодический** (daily + weekly), не realtime.
- **LLM:** для 5 Why использовать DeepSeek (тот же `DEEPSEEK_API_KEY`). У
  Bybit-ботов своего LLM нет — клиент **инлайнится внутри** `src/tradecard_bybit/llm/`
  (тонкая обёртка над DeepSeek; за образец — инлайн-клиент `fx_ai_trader`, копией,
  без кросс-зависимости). LLM — **только аналитика**, read-only.

---

## 3. Источники данных

### 3.1 БД ботов (read-only)

Открывать **строго read-only**. Обе БД имеют **идентичную** схему `trades` (см.
`src/scalp_bot/state/db.py`, `src/hybrid_bot/db.py`):

- SQLite `scalp_bot.sqlite` (volume `scalp_bot_data`, bind `/data`).
- SQLite `hybrid_bot.sqlite` (volume `hybrid_data`, bind `/data`).

Таблица **`trades`** (поля-источники для детекторов):

- Вход: `ts_open, symbol, side (long/short), qty, entry, sl, tp`.
- **Решение (детерминированное):** `score` (INTEGER — грейд-сырьё),
  `reasons` (TEXT — какие факторы сетапа сработали), `strategy` (playbook).
- Режим: `mode` (`paper` | `live`).
- Выход: `ts_close, exit, pnl_usd, fees_usd, close_reason`
  (`sl_hit`/`tp_hit`/`flow_exit`/`scratch`/`restart_flat`/`entry_*` и т.п.).
- Качество P&L: `pnl_provisional` (оценка), `pnl_verified` (сверено с биржей).

> Нет таблиц `decisions`/`lessons`/`daily_pnl` (в отличие от fx_ai_trader). Вся
> «причина входа» — в `score` + `reasons` + `strategy`. Это и есть детерминированный
> аналог «llm_reason». `reasons` парсить как список токенов (scalp:
> `sweep,cvd_div,reclaim,mom,ob_imb,key_*`; hybrid: причина закрытия одним
> токеном — `fix_threshold` / `trend_flat` / `broker_flat`).

> Исключать **не-торговые** закрытия из метрик WR/EXP (как делает сам бот):
> `close_reason IN ('restart_flat','entry_Cancelled','entry_Rejected',
> 'entry_Deactivated','entry_timeout')` — это реконсил, не исход.

### 3.2 Bybit `closedPnl` (ground truth по P&L)

- P&L брать как **net** из Bybit `get_closed_pnl` (уже с fees/funding,
  `stats-collection.mdc`). В БД это отражено флагом `pnl_verified=1` (true-up).
  Приоритет: `pnl_verified` > `pnl_provisional`-оценка.
- Для полного аудита (pagination + split shared subaccount по `orderLinkId`
  префиксу) — `scripts/collect_bybit_3bots_stats.py` (full `while cursor:`).
- **ЗАПРЕЩЕНО** делать выводы по `get_closed_pnl(limit=N)` без pagination
  (`stats-collection.mdc`).
- **Timezone:** биржевая выписка обычно MSK (UTC+3), БД — UTC (epoch `ts_*`),
  API — UTC ms. Конвертировать явно перед сравнением.
- API connection/auth/rate-limit — только офиц. дока Bybit (`api-docs.mdc`):
  <https://bybit-exchange.github.io/docs/v5/intro> + pybit.

### 3.3 Цена/MFE после выхода (для «exit too early»)

- Опционально: post-exit траектория (klines Bybit) для измерения упущенного
  движения / MFE (Sweeney 1988). Read-only. Использовать для детектора
  `exit_left_money` (см. §4) — но это свойство **правила выхода**, не психологии.

---

## 4. Таксономия «ошибок системы» (канон §4, deterministic-адаптация)

«Ошибки» — наблюдаемые из данных **повторяющиеся убыточные паттерны правил**.
Каждая — детектор над `trades` (+ post-exit цена где нужно). Пороги
**нейтральные/структурные/относительные**, **не** под желаемый P&L
(`no-data-fitting.mdc`). `paper` и `live` агрегировать **раздельно** (`mode`).
Запланированный SL ≠ ошибка; ошибка = паттерн на достаточной выборке.

| Код паттерна | Определение (из данных) | Поля-источники |
|---|---|---|
| `grade_not_predictive` | `score`-бакеты **не** монотонны по WR/EXP (высокий score не отделяет винов) | `score, pnl_usd, close_reason` |
| `strategy_regime_leak` | страта системно убыточна в срезе (символ / сессия / час) при общем плюсе | `strategy, symbol, ts_open, pnl_usd` |
| `sl_cluster` | повтор `sl_hit` на одной связке (символ×сторона×страта) выше базовой частоты | `close_reason, symbol, side, strategy` |
| `exit_left_money` | `tp_hit`/`flow_exit` систематически до значимого продолжения (MFE ≫ реализ.) | `close_reason, exit` + post-exit MFE |
| `factor_noise` | токен `reasons` не улучшает WR/EXP (присутствие ≈ отсутствие) — кандидат на удаление | `reasons, pnl_usd` |
| `overtrading` | всплеск числа сделок при падении WR/EXP (особенно re-entry после SL) | `ts_open, pnl_usd, strategy` |
| `big_game_hunting` | дрейф к редкому high-`score` (A+) при деградации baseline-страты | `score, strategy, pnl_usd` |
| `paper_live_divergence` | связка валидна на paper, системно проигрывает на live | `mode, pnl_usd` |

> Детекторы — **наблюдатели**, не блокировки. Помечают сделки/срезы кодом(ами)
> для агрегации. Список расширяемый; каждый детектор — со ссылкой на поле БД, без
> magic-numbers (порог env/относительный). Каждый «паттерн» становится «темой»
> только при прохождении `sample-size.mdc`.

> `factor_noise` и аудит факторов — это **родная** для проекта практика (см.
> аудит scalp v0.9.0: funding/liq убраны как factor-noise). `tradecard` её
> формализует как retrospective-детектор, но **выводы** про удаление фактора —
> человеку (strategy-guard).

---

## 5. Грейдинг сделок (канон §7 — поле `score`)

- Грейд берётся **напрямую из `score`** (он уже посчитан ботом на входе). Маппинг
  `score`-бакетов → A+/A/B/C — **конфигурируемый**, по распределению (квантили), а
  не «подогнанный под P&L».
- Цель — **аналитика**: построить кривую «`score`-бакет → WR / EXP / средний R» и
  проверить **монотонность** (выше грейд → лучше). Немонотонность = детектор
  `grade_not_predictive` (§4) = тема №1 для 5 Why.
- Риск-аллокация канона (80/30/15/5%) **не применяется автоматически** (риск-
  модель `risk_per_trade_usd` фиксирована, меняется только с одобрения). Таблица
  канона — референс в отчёте.
- Разрез по `strategy` (для scalp — каждая страта своя кривая грейда).

---

## 6. 5 Why engine (канон §5)

- На **главную повторяющуюся тему периода** запустить **5 Why через DeepSeek**
  (shared LLM-клиент, read-only).
- Вход в промпт: код паттерна, агрегаты (частота, net-эффект, разрезы symbol/
  session/score/strategy), 3-5 репрезентативных сделок с
  `reasons / score / close_reason / mode / net`, и контекст канона стратегии
  (из `STRATEGY_*` / `STRATEGY_HYBRID.md` — чтобы 5 Why знал, что правило
  задумано как research-based, а не «баг»).
- Выход: цепочка 5 «почему» + **гипотеза-решение** (≤200 симв.): фильтр режима /
  session-гейт / перекалибровка score / удаление factor-noise / новый playbook —
  как **кандидат на ручную проверку**, не disable-правило.
- За основу — структура ChatGPT-промпта из описания ролика (адаптировать под наши
  поля). Цитировать канон в docstring.

> Канон §5 (ключ для детерминированных ботов): решение часто «не про систему, а
> про рынок» → не «подкрути порог», а «у страты нет фильтра режима, в котором
> сетап валиден». 5 Why должен в первую очередь искать **режим/условие**, а не
> «сделай стоп туже» (анти-канон §10).

---

## 7. Small wins / momentum tracking (канон §6 — с OOS-гейтом)

- Для выбранной темы трекать **частоту по неделям** (паттерн/100 trades),
  раздельно paper/live, раздельно по `strategy`.
- **Small win** = статистически значимое снижение частоты темы **на forward/OOS-
  выборке ПОСЛЕ одобренного человеком внедрения** гипотезы (`sample-size.mdc`:
  ≥100 сделок, ≥2 недели, p<0.05). До внедрения — ГИПОТЕЗА; после, но до порога —
  НАБЛЮДЕНИЕ; только пройдя порог OOS — SMALL WIN.
- История тем/гипотез/побед — в собственной БД `tradecard`; «momentum-кривая» —
  накопленное число OOS-подтверждённых small wins.
- В отчёт: тема №1, тренд, число накопленных small wins, growth-vs-outcome маркер
  (была ли подгонка под бэктест), статус гипотез (открыта/внедрена/валидируется/
  победа/отклонена).

> ⚠️ Анти-overfit инвариант: `tradecard` **не вправе** объявить small win по
> in-sample улучшению бэктеста. Только forward после внедрения. Это прямое
> следствие `no-data-fitting.mdc` для детерминированных систем.

---

## 8. Выход (отчёты)

1. **Дневной digest** в Telegram (настройки Telegram соответствующего бота),
   префиксы **`[tradecard-scalp]`** / **`[tradecard-hybrid]`** (у ботов
   раздельные telegram-конфиги): net P&L (Bybit closedPnl) за день paper/live,
   топ-3 паттерна, грейд-распределение по `score`, 1 actionable-наблюдение.
2. **Недельный report card** — markdown в `data/tradecard/{scalp|hybrid}_
   YYYY-WW.md`: темы, 5 Why, гипотеза-решение, small-wins/momentum, грейд-
   аналитика (`score`→перформанс), факторный аудит (`reasons`), per-strategy
   разрез, baseline-vs-A+ (big-game-hunting детектор).
3. **Кандидат-гипотезы** — в собственной таблице `tradecard` (advisory). Продвижение
   гипотезы в реальное изменение конфига/правил бота = **только человек**,
   отдельным одобренным коммитом с обновлением `STRATEGY_*` + тестов
   (`strategy-guard.mdc`). `tradecard` **ничего не пишет** в БД ботов.

---

## 9. Архитектура и интеграция

- **Модуль:** **самостоятельный пакет** `src/tradecard_bybit/` (НЕ связан с
  `src/tradecard_fx/` — нет общего кода/импортов между ними) с адаптерами источников
  `tradecard_bybit/data/scalp_*` и `tradecard_bybit/data/hybrid_*`. Подпакеты:
  `data/ analysis/ llm/ report/ state/ app/`. 5 Why / грейдинг / small-wins движок
  живёт внутри этого пакета (если позже захочется общего ядра — выносить в
  нейтральный `src/shared_*`, но не в FX-пакет).
- **CLI:** `tradecard-bybit daily --bot scalp|hybrid` /
  `tradecard-bybit weekly --bot …` (+ `--since/--dry-run/--paper|--live`).
- **Запуск:** scheduler-контейнер/cron; сервис(ы) `tradecard-bybit` в
  `docker-compose.yml`; **read-only** mount `./data` (обе БД ботов) + свой volume
  `tradecard_bybit_data`.
- **Переиспользуемые компоненты** (инфраструктура, read-only, инлайн-копией без
  кросс-зависимости): DeepSeek LLM-клиент (`tradecard_bybit/llm/`); Bybit-клиент на
  чтение closedPnl/klines (за образец — `scalp_bot/trading/client.py`, read-only
  scope); Telegram-нотифаер. **Не тащить торговую логику** ботов и **не импортировать**
  из `src/tradecard_fx/`.

---

## 10. Креды и интеграции (взять с VPS, не хардкодить)

`/root/fx-pro-bot/.env` на VPS (`204.168.149.140`).

- **DeepSeek:** `DEEPSEEK_API_KEY` (общий), модель/base_url по дефолту.
- **Bybit (demo):** ключи ботов — `SCALP_BYBIT_*` / `HYBRID_BYBIT_*` (см.
  `src/scalp_bot/config/settings.py`, `src/hybrid_bot/settings.py`;
  hybrid имеет fallback на общий Bybit-ключ в compose). Только **read** scope
  (closedPnl/klines).
- **БД ботов:** SQLite в bind `./data` (read-only mount): `scalp_bot.sqlite`,
  `hybrid_bot.sqlite`.
- **Telegram:** настройки соответствующего бота (`SCALP_TELEGRAM_*` /
  `HYBRID_TELEGRAM_*`), префиксы `[tradecard-scalp]` / `[tradecard-hybrid]`.
- Для tradecard завести свои `TRADECARD_BYBIT_*` с дефолтами на ключи ботов в
  compose, чтобы аудит был раздельным.

---

## 11. Тесты (strategy-guard.mdc — тесты обязательны)

- **Детекторы §4:** на честных фикстурах (реальные строки БД / правдоподобная
  синтетика), включая `mode`-разделение (paper/live), парсинг `reasons`-токенов
  обоих ботов, исключение non-trade `close_reason`. **НЕ рисовать данные «под
  результат»** (`no-data-fitting.mdc`).
- **Грейдинг §5:** маппинг `score`→бакет по квантилям; кривая грейд-vs-перформанс;
  детектор `grade_not_predictive` на немонотонной выборке.
- **closedPnl ground truth §3.2:** мок Bybit `get_closed_pnl` с pagination; net
  предпочитается `pnl_provisional`-оценке; timezone-конверсия.
- **5 Why §6:** мок LLM; промпт собирается из полей + контекста STRATEGY-канона;
  парсинг гипотезы.
- **Small wins §7:** in-sample улучшение НЕ засчитывается как победа; только
  forward после «внедрения» + порог значимости (sample-size).
- **Read-only инвариант:** запись в БД `scalp_bot`/`hybrid_bot` невозможна.

---

## 12. Применимые правила проекта (соблюдать)

- `strategy-guard.mdc` — advisory-only, изоляция, тесты, изменения стратегии
  только с одобрения; `tradecard` не пишет в БД/конфиг ботов.
- `no-data-fitting.mdc` — **центральное**: детекторы/пороги/гипотезы опираются на
  артефакт анализа; small win только OOS; запрет подгонки конфига под просадку.
- `sample-size.mdc` — «темы»/«победы» только на ≥100 сделок связки, ≥2 недели,
  p<0.05; иначе НАБЛЮДЕНИЕ.
- `stats-collection.mdc` — Bybit closedPnl net + full pagination + явный
  источник/период/timezone; paper и live раздельно.
- `api-docs.mdc` — Bybit endpoints/limits только из офиц. доки.
- `buildlog.mdc` — записи в `BUILDLOG_TRADECARD_BYBIT.md` в том же коммите.
- `deploy-vps.mdc` — деплой через git, селективный rebuild `tradecard-bybit`,
  проверка контейнера/логов.

---

## 13. Фазы реализации (milestones)

1. **Каркас:** Bybit-адаптеры в `src/tradecard_bybit/` (scalp + hybrid), конфиг
   (env `TRADECARD_BYBIT_*`), read-only доступ к обеим БД, своя SQLite, Telegram.
   CLI `daily --bot` печатает базовую сводку (net из БД, paper/live), без анализа.
2. **Ground truth P&L:** Bybit closedPnl (full pagination) + сверка с
   `pnl_verified`, net в отчёте, timezone-корректно (`stats-collection.mdc`).
3. **Детекторы §4:** таксономия паттернов (grade/regime/sl_cluster/exit_left_money/
   factor_noise/overtrading/big_game_hunting/paper_live), пометка срезов, агрегаты.
4. **Грейдинг §5** (`score`→перформанс, монотонность) + per-strategy разрез +
   baseline-vs-A+.
5. **5 Why (LLM) §6** на тему №1 (с контекстом STRATEGY-канона страты) → гипотеза
   в собственную БД.
6. **Small wins / momentum §7** (OOS-гейт) + недельный markdown report card §8.
7. **Тесты, BUILDLOG, деплой** (scheduler/cron), наблюдение первых прогонов.
8. **Накопление выборки** до порогов `sample-size.mdc` перед любой рекомендацией
   менять конфиг; гипотезы остаются advisory до OOS-валидации.

После КАЖДОЙ фазы — сверка с каноном (ролик + STRATEGY_TRADECARD_BYBIT.md):
advisory-only соблюдён, пороги нейтральны, в БД ботов ничего не записано, small
win не объявлен по in-sample.

---

## 14. ГОТОВЫЙ СТАРТОВЫЙ ПРОМПТ для нового чата

> Скопировать в новый чат как первое сообщение.

```
Реализуем advisory-ревьюер tradecard (Bybit / scalp_bot + hybrid_bot) по
тех-заданию TASKSPEC_TRADECARD_BYBIT.md и канону STRATEGY_TRADECARD_BYBIT.md
(deterministic-чтение ролика https://youtu.be/WDdvnd9vLbM — SMB Momentum Model).

Контекст:
- tradecard = АНАЛИТИКА, не торговый бот и не автотюнер. Читает БД scalp_bot и
  hybrid_bot read-only, считает report card, грейдит по score, гоняет 5 Why
  через DeepSeek, отдаёт рекомендации человеку. НЕ меняет пороги/правила/конфиг
  ботов и НЕ пишет в их БД.
- Боты ДЕТЕРМИНИРОВАННЫЕ (нет LLM/decisions/lessons). «Решение входа» = поля
  score + reasons + strategy в таблице trades. «Ошибка» = повторяющийся
  убыточный ПАТТЕРН правил (страта×режим×сессия×символ×score), не психология.
- P&L ground truth = Bybit closedPnl net (pnl_verified=1 / full pagination
  scripts/collect_bybit_3bots_stats.py), не БД-оценка; paper/live раздельно (mode).
- LLM = DeepSeek (DEEPSEEK_API_KEY), инлайн-клиент внутри src/tradecard_bybit/llm/.
- Креды: SCALP_* / HYBRID_* (см. настройки обоих ботов); для tradecard
  свои TRADECARD_BYBIT_* с дефолтами на них.
- Telegram: настройки каждого бота, префиксы [tradecard-scalp] / [tradecard-hybrid].
- Изоляция: ОТДЕЛЬНЫЙ пакет src/tradecard_bybit/ (без импортов из src/tradecard_fx/),
  своя SQLite (volume tradecard_bybit_data), свой сервис, свой
  BUILDLOG_TRADECARD_BYBIT.md.

Прежде чем писать код:
1. Прочитай STRATEGY_TRADECARD_BYBIT.md и TASKSPEC_TRADECARD_BYBIT.md целиком.
2. Сверься со схемой trades обоих ботов (src/scalp_bot/state/db.py,
   src/hybrid_bot/db.py) — поля §3; распарси reasons-токены (signals.py /
   app/main.py).
3. Работай по фазам §13. После каждой — сверка с каноном.
4. Соблюдай правила: no-data-fitting (ЦЕНТРАЛЬНОЕ — запрет подгонки конфига),
   sample-size, strategy-guard (advisory-only), stats-collection, api-docs,
   buildlog, deploy-vps.

Начни с фазы 1 (каркас). small win засчитывается ТОЛЬКО на OOS после одобренного
человеком внедрения — никаких выводов по in-sample бэктесту.
```

---

## 15. Решения и открытые вопросы

**Зафиксировано:**
- Имя = `tradecard` (сборка `tradecard-bybit`), покрывает оба бота (scalp +
  hybrid) одним модулем с двумя адаптерами.
- Режим = периодический (daily + weekly), advisory-only.
- P&L = Bybit closedPnl net (pnl_verified / full pagination), paper/live
  раздельно, §3.2.
- «Трейдер» канона = конфиг-стратегии + человек-оператор; бот сам ничего не
  усваивает (нет lessons), §2 / STRATEGY_TRADECARD_BYBIT §2.
- Грейд = поле `score` (квантильный маппинг), §5.
- small win = только OOS после одобренного внедрения, §7.
- LLM = инлайн DeepSeek-клиент в `src/tradecard_bybit/llm/` (без кросс-зависимости
  с FX-пакетом), §2/§6.
- Изоляция = отдельный пакет `src/tradecard_bybit/`, НЕ общий с FX, §9.

**Открытые (решить в начале нового чата):**
- Точные нейтральные пороги детекторов §4 (квантили score, baseline-частоты
  sl_cluster, MFE-порог exit_left_money) — относительные/структурные, калибровать
  только по данным, не под P&L.
- Один общий сервис `tradecard-bybit` на оба бота vs два сервиса — по удобству
  расписания (флаг `--bot`).
- Продвижение одобренных гипотез в конфиг ботов — отдельный одобренный процесс вне
  scope tradecard.
- Расписание прогонов (cron-времена; timezone отчёта — UTC + MSK).
