# BUILDLOG — FX AI Trader (DeepSeek-V4 на cTrader FxPro: gold + Brent oil + Natural Gas)

## 2026-05-29

### feat(data-feeds): 5 бесплатных усилений контекста — FRED real-yields / VIX / CFTC COT / GDELT / econ-calendar

`коммит при deploy`

#### Контекст (запрос пользователя)

«Нужно посмотреть откуда аналитик берёт данные, может есть более сильные
источники, но бесплатные» → «делаем все усиления». Аудит источников
(cTrader / yfinance / EIA / NOAA / RSS) выявил пробелы между тем, что
SYSTEM_PROMPT **требует** в иерархии драйверов, и тем, что реально
подавалось: real yields инферились (не было живого ряда), «ETF/COT»
упоминался но не подавался, не было risk-regime (VIX), агрегированного
sentiment и event-proximity (хотя промпт требует «scale size near FOMC»).

#### Что добавлено (5 провайдеров, каждый graceful-degrade + feature-flag)

- **B. FRED real-yields** (`data/macro_rates.py`): с `AI_FX_TRADER_FRED_API_KEY`
  тянем DFII10 (точный 10Y real yield — gold-driver #1) + T10YIE (breakeven).
  Без ключа → остаётся TIP-прокси (yfinance). Real-yield выводится ПЕРВЫМ
  в US MACRO RATES. Endpoint: api.stlouisfed.org/fred/series/observations
  (офиц. дока FRED API).
- **C. VIX risk-regime** (`data/risk_regime.py`, новый): yfinance `^VIX`,
  без ключа. Сырое значение + 24h/5d Δ; интерпретацию (calm/stress) делает
  LLM. Research: Whaley 2000 (fear gauge), Baur & Lucey 2010 (gold safe-haven).
- **A. CFTC COT** (`data/cot.py`, новый): Disaggregated Futures-Only,
  Socrata resource `72hh-3qpy` (public API, без ключа). Managed-Money net
  по COMEX Gold / NYMEX Brent Last Day / NAT GAS NYME + недельная Δ.
  Контракт-имена проверены против live API (report week 2026-05-19).
  Research: Sanders/Boris/Manfredo 2004, Briese 2008 (extremes → развороты).
- **D. GDELT news tone** (`news/gdelt.py`, новый): DOC 2.0 `timelinetone`,
  без ключа. Global media sentiment (avg/latest/trend) поверх точечных RSS.
  Polite inter-request spacing 1.5s (429-защита), cache 3ч. Research:
  Leetaru/Schrodt 2013, Tetlock 2007.
- **E. Economic calendar** (`data/econ_calendar.py`, новый): pure-compute,
  без сети/ключа. Recurring (EIA Wed/Thu, NFP первая пятница) — по правилам
  + zoneinfo (DST-корректно); FOMC/CPI — статический sourced-список 2026
  (federalreserve.gov + bls.gov). Окно 7 дней. Закрывает требование промпта
  «scale to half size through FOMC».

#### Интеграция

- `MarketContext` +5 полей; `collect_market_context` принимает 5 новых
  провайдеров; `format_context_for_prompt` рендерит блоки (calendar высоко —
  sizing-critical; VIX/COT/GDELT — кросс-символьные оверлеи).
- `main.py`: провайдеры строятся по флагам, прокидываются в full-cycle
  (scheduled + event). Лог `Data feeds: ...` на старте.
- `SYSTEM_PROMPT`: driver #1 (real yields) и #5 (ETF/COT) переписаны на
  «читай ЖИВОЙ блок» вместо stale-хардкода (~+94k); добавлены оверлеи
  RISK-REGIME (VIX), EVENT-PROXIMITY (calendar), NEWS-TONE (GDELT) —
  все как confluence, НЕ standalone-триггеры (анти-FOMO рамка).
- `.env.example`: задокументированы все флаги + FRED-ключ.

#### Тесты / smoke

- `tests/test_fx_ai_trader_data_feeds.py` (новый, 23 теста): форматтеры,
  dataclass-проперти, symbol-maps, parsing (mock requests), trend-классификация,
  NFP/FOMC/CPI-вычисление, horizon-фильтр, context-блоки. Полный fx_ai_trader
  suite: 307 passed.
- Live smoke: COT (gold MM +93540 / NG −96303 / Brent +12930), VIX 15.84,
  calendar (EIA Wed/Thu в окне). GDELT 429 при ручном бёрсте → graceful None
  (в проде 3 запроса/3ч).

#### Follow-up hardening (GDELT, тот же деплой)

На VPS GDELT отдал SSL-handshake/read timeout (api.gdeltproject.org медленный/
egress-slow с этого хоста). Graceful-degrade отработал (цикл не упал), но: (1)
полный traceback на **ожидаемый** сетевой сбой опционального фида = шум; (2)
20s read-timeout × 3 символа тормозил full-цикл. Фикс: `timeout=(5,10)`
(connect,read) — быстрый отказ; `log.warning` без traceback; **break** на
первом сетевом сбое. Диагностика с VPS: GDELT достижим (connect 0.13s), но
отдаёт **429** (rate-limit, частично от dev-бёрста) — а при провале кэш не
обновлялся → ретрай каждые 15 мин сам подогревал 429. Добавлен
**failure-backoff 30 мин** (`fail_backoff_sec`): после сбоя/429 не ретраим
полчаса. Если GDELT стабильно бесполезен с VPS — `AI_FX_TRADER_GDELT_ENABLED=
false` (остальные 4 фида не зависят).

#### Compliance

- `api-docs.mdc`: CFTC `72hh-3qpy`, FRED series_observations, GDELT DOC 2.0,
  FOMC/CPI даты — все из официальных источников (ссылки в docstring'ах).
- `no-data-fitting.mdc`: новые data-feeds, не подгонка стратегии; пороги
  «экстремум/calm/stress» НЕ зашиты — решает LLM.
- `strategy-guard.mdc`: правки промпта согласованы («делаем все усиления»);
  driver-иерархия не менялась — обновлены только data-availability заметки.

**Файлы:** `src/fx_ai_trader/data/macro_rates.py`,
`src/fx_ai_trader/data/risk_regime.py` (new),
`src/fx_ai_trader/data/cot.py` (new),
`src/fx_ai_trader/data/econ_calendar.py` (new),
`src/fx_ai_trader/news/gdelt.py` (new),
`src/fx_ai_trader/trading/context.py`, `src/fx_ai_trader/app/main.py`,
`src/fx_ai_trader/config/settings.py`, `src/fx_ai_trader/llm/prompts.py`,
`.env.example`, `tests/test_fx_ai_trader_data_feeds.py` (new)

### feat(event-trigger): Phase 3.1 — передавать сигнал датчика аналитику (EVENT TRIGGER блок)

`коммит при deploy`

#### Контекст (вопрос пользователя)

«А разве не должен аналитик принимать данные датчика и сопоставлять со
своими данными?» Аудит показал реальный пробел: Phase 3 датчики
(entry-breakout / adverse-move / locked-profit) будили внеплановый цикл,
но **триггер шёл только в лог** — в `_run_full_cycle` передавалась голая
метка `trigger="event"`, а контекст и USER_PROMPT собирались идентично
плановому циклу. Аналитик просыпался «вслепую»: заново выводил всё из
баров и НЕ знал, что именно сработало (символ, направление, уровень).
Сигнал датчика выбрасывался до промпта.

#### Что добавлено

- `prompts.py::format_event_trigger(triggers)` — рендерит блок
  `=== EVENT TRIGGER (why you are being consulted now) ===` со списком
  сработавших сигналов и **нейтральной** guidance по категориям
  (breakout / adverse / locked-profit). Пусто/None → "" (плановый цикл).
- `build_user_prompt` / `build_user_prompt_review` — новый параметр
  `event_trigger`, блок идёт ПЕРВЫМ (framing «почему тебя позвали»),
  перед историей и рынком.
- `SYSTEM_PROMPT` — секция EVENT TRIGGER: как трактовать блок (cue, не
  рекомендация; breakout = момент INTO level, а edge = pullback по MFP
  rule 3). `SYSTEM_PROMPT_REVIEW` — нота: locked-profit cue это только
  timing, своя R главнее датчика.
- `main.py` — прокинут `event_triggers` из `full_dec.triggers` /
  `review_dec.triggers` в соответствующие циклы.

#### Анти-FOMO рамка (compliance)

Ключевой риск: пробойный сигнал толкает аналитика в FOMO-вход на хаях,
что противоречит его MFP-философии (вход на структурном откате, не на
пробое). Поэтому блок подан НЕЙТРАЛЬНО: «ATTENTION CUE, NOT a
recommendation», «Breakout alone is NOT an entry», «event … NEVER, by
itself, a reason to open». Событие меняет только TIMING консультации, не
понижает confluence-планку.

#### Research basis

- KenMacro MFP (SETUP rule 3): вход на pullback, не на momentum INTO high.
- Lopez de Prado «Advances in Financial ML» (2018) ch.2 — event-based
  sampling: значимое ценовое событие = повод посмотреть, не повод войти.

#### Тесты

`tests/test_fx_ai_trader_event_trigger.py` (12): format_event_trigger по
категориям + нейтральная рамка; builders include/omit блок (scheduled →
нет блока); SYSTEM_PROMPT секция + анти-FOMO инвариант. Полный
fx_ai_trader-набор: 284 passed.

#### Rollback

Параметр `event_trigger` по дефолту "" — при откате call-site'ов в main.py
поведение возвращается к Phase 3 (блока нет). Промпт-секции изолированы.

**Файлы:** `src/fx_ai_trader/llm/prompts.py`,
`src/fx_ai_trader/app/main.py`,
`tests/test_fx_ai_trader_event_trigger.py`

### chore(self-reflection): сдвиг regime-change cutoff на деплой Phase 0–3 (clean slate)

`коммит при deploy`

#### Контекст (запрос пользователя)

«А не будет ли лучше чтобы бот начал с чистого листа, чтобы предыдущие
решения не путали новую логику?» После выкатки Фаз 0–3 (event-driven
архитектура, завершено 2026-05-29 08:26 UTC) reasoning/exit-rules
изменились фундаментально. Главное — Phase 0 (Review Guardian): review
больше НЕ закрывает по 1H-шуму. Все pre-Phase-0 убытки (закрытия 22/26
позиций на техническом шуме) — outcome уже исправленной логики и в
self-reflection «пугают» бота за поведение, которого больше нет
(loss-aversion bias, ранее зафиксирован как пункт E в
NEXT_PHASE_AI_FX_TRADER.md).

#### Что изменено

- `stats_window_start` default: `2026-05-26T07:42` (Phase 1 deploy) →
  `2026-05-29T08:26:00+00:00` (Phase 0–3 deploy). Closed trades с
  `opened_at` < cutoff не показываются LLM в SELF-REFLECTION блоках.
- **Данные НЕ удаляются** — full audit trail остаётся в БД, фильтр
  только query-time (механизм regime-change cutoff из 2026-05-28,
  «вариант 2»). Полностью обратимо через env / откат default.
- Выбран **non-destructive** путь (не wipe БД): сохраняет аудит,
  счётчик эксперимента и reconcile открытых позиций. На момент сдвига
  открытых позиций нет (`positions=0`).

#### Эффект

- По всем парам symbol×side `n=0` → повторно активируется COLD-START
  DISCOVERY RULE (Sutton & Barto 2018 §2.7) — бот может брать малые
  разведочные сделки под новой логикой.
- `window_label` в промпте автоматически рендерит «since 2026-05-29
  regime-change cutoff» (строится из timestamp, хардкода нет).

#### Research / compliance

- Lopez de Prado «Advances in Financial ML» (2018) ch.7 — structural
  breaks инвалидируют pre-break outcomes как evidence; Hamilton (1989)
  regime-switching. Фазы 0–3 — реальный structural break.
- sample-size.mdc: cutoff двигаем **один раз** на реальный слом, НЕ на
  каждую правку (иначе выборка никогда не накопится до порога). Зафиксировано
  в комментарии у поля. strategy-guard.mdc: торговая логика не тронута.

**Файлы:** `src/fx_ai_trader/config/settings.py` (default + rationale),
`tests/test_fx_ai_trader_regime_cutoff.py` (assert нового default)

### feat(event-full): Фаза 3 — event-driven вызов аналитика (entry-breakout + adverse-move)

`коммит при deploy`

#### Контекст (идея пользователя)

«Почему сразу не слушать датчики, а когда срабатывают — звать аналитика?
График показал сетап → аналитик изучил глобальные новости → решил
входить или нет». До Фазы 3 OPEN-решения были только по расписанию
(раз в 15 мин); event-датчик (Фаза 2) будил лишь review-guardian
(open запрещён). Фаза 3 даёт event-driven вызов **аналитика** (full).

#### Что добавлено (две event-ветки к full-циклу)

1. **EntryBreakoutSensor** — пробой Donchian-канала живой ценой будит
   ранний full-цикл, аналитик решает open/hold. Уровни (20-баровый 1H
   hi/lo + ATR) кэшируются из full-цикла (бесплатно), датчик сравнивает
   живую цену без API. `slots_free`-gate: будим только если есть слот
   под позицию. buffer_atr — confirmation band (анти-шум).
2. **AdverseMoveSensor** — открытая позиция ушла в минус ≥ threshold_r
   → ранний full-цикл, стратег с macro пересматривает тезис. Согласуется
   с Phase 0 (тезис судит full с macro, не review-guardian по 1H).

Плановый full-цикл остаётся пульс-страховкой (каждые 15 мин), события
сверху (пользователь выбрал keep15). FULL приоритетнее event-review:
если сработали и full-датчик, и locked-profit — идёт full (делает всё +
macro). Datчики бесплатны по API (spot-кэш + локальная БД).

#### Research basis

| Источник | Положение |
|---|---|
| Donchian (1960s); Faith «Way of the Turtle» (2003) | 20-period channel breakout — каноничный lookback |
| Lopez de Prado «Advances in Financial ML» (2018) ch.2 | event-based sampling по значимым ценовым событиям |
| Phase 0 (наш audit) | тезис судит full-цикл с macro, не review на 1H |

threshold_r=1.0 (adverse) и lookback=20 (Donchian) — натуральные/
каноничные единицы, не подгонка под результат (no-data-fitting.mdc).

#### Файлы

- `price_sensor.py`: `AdverseMoveSensor`, `EntryBreakoutSensor`,
  `ReferenceLevels`, `EventDecision.triggers`.
- `app/main.py`: инициализация датчиков, `_check_event_sensors`
  (full vs review маршрутизация), обновление Donchian-референса в
  `_run_full_cycle` (param `entry_sensor`), `trigger=scheduled/event`.
- `config/settings.py` + `.env.example`: `event_full_enabled`,
  `entry_breakout_*` (lookback 20, buffer 0.05ATR, cooldown 300s,
  max 4/ч), `adverse_move_*` (threshold 1.0R, cooldown 300s, max 4/ч).

#### Стоимость

Worst case: +4 entry +4 adverse = +8 внеплановых full/час поверх 4
плановых = ≤12 full/час (≈$0.018/ч) и только при реальных пробоях/
движениях. Cooldown 300s + rate-cap на каждый датчик.

#### Тесты

- Новый `tests/test_fx_ai_trader_event_full.py` (14 тестов): adverse
  rising-edge/re-arm/cooldown/prune; entry up/down-break, buffer,
  slots-gate, re-arm, cooldown+rate-cap, missing price.
- Полный прогон: **1335 passed**.

#### Откат

`AI_FX_TRADER_EVENT_FULL_ENABLED=false` → только плановый full
(поведение Phase 0/1/2). Sub-флаги `ENTRY_BREAKOUT_ENABLED` /
`ADVERSE_MOVE_ENABLED` отключают ветки по отдельности.

#### Связанное (отложено)

Loss-aversion bias на собственных закрытиях-в-минус — записан в
`NEXT_PHASE_AI_FX_TRADER.md` раздел E (по просьбе пользователя: сперва
все фазы, потом учесть; требует post-Phase-2/3 выборки по sample-size).

### feat(event-review): Фаза 2 — событийный датчик locked-profit (внеплановый review)

`коммит при deploy`

#### Контекст

Phase 1 дал живую цену. Phase 2 использует её для event-driven реакции:
плановый review раз в 5 мин мог пропустить спайк позиции в зону
locked-profit (≥1.5R), который откатывался внутри окна. Теперь датчик
будит внеплановый review в момент входа в зону.

#### Что это НЕ (strategy-guard.mdc)

- НЕ открывает позиции, НЕ двигает SL/TP, НЕ закрывает сам.
- НЕ меняет exit-правила: решение по-прежнему за LLM-guardian (Phase 0,
  close ТОЛЬКО на locked-profit ≥1.5R).
- Меняет лишь *когда* запускается review — это execution-timing
  (изначальная цель dual-timer: «больше точек реакции»), не торговая
  логика. Прайс берётся из живого стрима, exit-критерий неизменен.

#### Research basis

| Источник | Положение |
|---|---|
| Lopez de Prado «Advances in Financial ML» (2018) ch.2 | event-based sampling (threshold/CUSUM) — сэмплировать по значимым ценовым событиям, не по календарю |
| Sutton & Barto «Reinforcement Learning» (2018) §3 | event-driven реакция эффективнее фиксированного опроса при разреженных значимых событиях |

#### Что изменилось

`src/fx_ai_trader/trading/price_sensor.py` (новый):
- `compute_unrealised_r(side, entry, sl, price)` — R = signed
  (price−entry)/|entry−SL|; None при отсутствии SL/цены/вырожд. риска.
- `LockedProfitSensor` — rising-edge детектор входа в зону ≥ threshold_r
  с гистерезисом (re-arm при падении ниже threshold−hysteresis),
  cooldown, rate-cap (max/час), prune закрытых позиций.

`client_adapter.py`: `get_live_spot_mid()` — ТОЛЬКО spot-кэш, БЕЗ
фолбэка на trendbars (датчик опрашивается часто → не должен дёргать API).

`app/main.py`: датчик в главном цикле (3-я ветка после full/review),
опрос каждые `sensor_interval_sec` (15с) по локальной БД + spot-кэшу
(0 API-вызовов); при fire — внеплановый `_run_review_cycle(trigger=
"event")`, сбрасывает таймер планового review. Review-лог теперь
различает `scheduled`/`event`.

`config/settings.py` + `.env.example`: `event_review_enabled=True`,
`threshold_r=1.5` (совпадает с порогом промпта), `hysteresis_r=0.3`,
`cooldown_sec=120`, `sensor_interval_sec=15`, `max_per_hour=6`.

#### Стоимость (контроль)

Датчик сам по себе бесплатен (in-memory + локальная БД). Внеплановые
review ограничены: cooldown 120с + max 6/час. Плановый review = 12/час;
event-review добавляет ≤6/час и только при реальных спайках в прибыль.

#### Тесты

- Новый `tests/test_fx_ai_trader_event_review.py` (17 тестов): R-расчёт
  BUY/SELL/None, rising-edge (один fire на вход), re-arm с гистерезисом,
  cooldown, rate-cap со скользящим окном, prune закрытых.
- Полный прогон: **1321 passed**.

#### Откат

`AI_FX_TRADER_EVENT_REVIEW_ENABLED=false` → остаётся только плановый
review (поведение Phase 0/1). Полный откат — revert коммита.

#### Дальше (Фаза 3, не реализовано)

Adverse-move триггер для внепланового **full**-цикла (strategist с
macro) при резком движении против позиции / новостном событии.

### feat(live-price): Фаза 1 — живой spot-стрим цены (ProtoOASubscribeSpots) вместо H1-close

`коммит при deploy`

#### Контекст и симптом

`get_current_price` отдавал **последний M1-close** через
`ProtoOAGetTrendbarsReq` (discrete polling). Это давало две боли:
- устаревшая цена в решениях (между запросами цена не обновлялась);
- наблюдали «current price unavailable for BZ=F» во время illiquid
  часов — discrete-запрос баров мог вернуть пусто.

Фаза 1 переводит цену на **живой поток**. Хорошая новость из доки:
отдельный websocket НЕ нужен — cTrader Open API отдаёт спот по тому же
TCP+protobuf соединению, что уже держит `client.py` (Twisted reactor).
Нужна лишь подписка.

#### Источник правды (api-docs.mdc)

| Док | Положение |
|---|---|
| [help.ctrader.com/open-api/messages/#protooasubscribespotsreq](https://help.ctrader.com/open-api/messages/) | «After successful subscription you'll receive technical ProtoOASpotEvent with latest price, after which you'll start receiving updates» |
| ProtoOASpotEvent | bid/ask `Optional`, в 1/100000 единицы цены (`123000`→`1.23`, `53423782`→`534.23782`); первый event содержит latest price даже при закрытом рынке |
| ProtoOAUnsubscribeSpotsReq | остановка потока по символам |

#### Что изменилось

`src/fx_pro_bot/trading/client.py` (общий cTrader-клиент; Advisor
остановлен, на клиенте остаётся только fx_ai_trader):
- spot-кэш `_spot_prices: {symbol_id → {bid, ask, ts}}` под отдельным
  `_spot_lock` (чтобы high-frequency spot-апдейты не конкурировали с
  waiter-диспетчером за `_lock`).
- `subscribe_spots(symbol_ids)` / `unsubscribe_spots(...)` —
  `ProtoOASubscribeSpotsReq` с `subscribeToSpotTimestamp=True`.
- `get_spot_price(symbol_id, max_age_sec)` → `{bid, ask, mid, ts,
  age_sec}` или None (нет/устарела). `mid`=(bid+ask)/2.
- `_handle_spot_event` в диспетчере `_on_message` (ловится ПЕРВЫМ среди
  non-heartbeat, payloadType закэширован); merge частичных bid/ask.
- `_resubscribe_spots` в `_do_auth` — подписки account-scoped, после
  reconnect/reauth переоформляются; кэш цен сбрасывается (stale).

`src/fx_ai_trader/trading/client_adapter.py`:
- `subscribe_live_prices()` — подписка по всем торгуемым символам.
- `get_current_price()` — предпочитает spot `mid`; фолбэк на M1-close
  (нет стрима / устарел / live disabled). Graceful во всех ветках.

`config/settings.py`: `live_price_enabled=True`,
`live_price_max_age_sec=300` (backstop на молчащее соединение).
`app/main.py`: `adapter.subscribe_live_prices()` после старта.
`.env.example`: документированы новые переменные.

#### Свежесть и reconnect (важно)

При живом TCP (heartbeat 8s) и открытом рынке spot обновляется
суб-секундно. «Устаревший» кэш при живом соединении = рынок реально не
двигался → цена корректна. `max_age_sec` — лишь backstop на dead-connection
до срабатывания reconnect (heartbeat ловит обрыв за ~10-20s). После
reconnect `_resubscribe_spots` переоформляет подписку и чистит кэш;
первый SpotEvent сразу наполняет его заново, в окне пустого кэша
`get_current_price` падает на M1-close.

#### Тесты

- Новый `tests/test_fx_ai_trader_live_price.py` (14 тестов): scaling
  /100000, merge частичных bid/ask, mid, свежесть, unknown symbol,
  resubscribe-clear; adapter prefers spot / fallback / disabled / none.
- Полный прогон: **1304 passed**.

#### Откат

Флаг `AI_FX_TRADER_LIVE_PRICE_ENABLED=false` → `get_current_price`
возвращается к M1-close, spot-стрим не используется (код подписки
остаётся, но neutral). Полный откат — revert коммита.

#### Дальше (Фазы 2-3, не реализовано)

На свежей цене — событийные датчики: резкое движение против позиции,
близость к SL/TP, новостной триггер. Без живого потока (Фаза 1) они
смысла не имели.

### feat(review-guardian): Фаза 0 — review-цикл закрывает ТОЛЬКО по locked-profit ≥1.5R

`коммит при deploy`

#### Контекст и симптом

`fx_ai_trader` закрыл BZ=F BUY (id=30) и NG=F SELL (id=31) в минус.
Разбор id=30 вскрыл **архитектурный конфликт двух циклов**:

- **Full-цикл** (15 мин, Phase 1 thesis discipline): «держи позицию,
  если macro-тезис цел, несмотря на 1H шум».
- **Review-цикл** (5 мин): имел право закрыть по **Trigger 1 (1H
  setup invalidation)** и **Trigger 3 (1H adverse technical)** —
  то есть ровно по тому 1H-сигналу, который Phase 1 запретил
  использовать как повод для выхода.

Review «перекрывал» full: закрывал по 1H MACD-флипу позицию, которую
full держал по интактному тезису. Вход был mean-reversion, а выход —
по trend-following инвалидации (концептуальный mismatch).

Аудит истории бота: **22 / 26 LLM-закрытий** были инициированы 1H
техникой в одиночку — классический failure mode.

#### Research basis

| Источник | Положение |
|---|---|
| Mark Douglas, «Trading in the Zone» (2000) | реакция на шум без edge = эмоциональный выход; дисциплина тезиса важнее краткосрочной техники |
| Наш аудит (Phase 1, 2026-05-26) | 22/26 LLM-closes по 1H технике в одиночку → systematic over-trading на шуме |

Это **не новый research** — это распространение уже принятого Phase 1
правила (thesis discipline) на review-цикл. Источник правды тот же.

#### Что изменилось (только prompt, обратимо)

`src/fx_ai_trader/llm/prompts.py`:

- `SYSTEM_PROMPT_REVIEW`: переписана роль — **GUARDIAN, NOT
  STRATEGIST**. Единственный авторизованный close — **locked-profit
  ≥1.5R** (защита заработанной прибыли не требует macro-контекста).
  Удалены как самостоятельные close-поводы: «SETUP INVALIDATION» и
  «ADVERSE TECHNICAL EVIDENCE». Явно: убыточную позицию review **не
  закрывает** на 1H weakness — работает broker SL (пол), а тезис
  судит full-цикл с macro/news/EIA.
- Примеры: «Example CLOSE on trigger 1 (partial)» → заменён на два
  HOLD-примера (1H weakness на прибыльной <1.5R; убыточная с 1H
  против при не сработавшем SL).
- `build_user_prompt_review` TASK RESTATEMENT: «3 close-triggers» →
  guardian-правило (CLOSE только ≥1.5R, иначе HOLD).
- THESIS DISCIPLINE-маркеры сохранены (совместимость).

#### Тесты

- Новый `tests/test_fx_ai_trader_review_guardian.py` (13 тестов):
  guardian-роль, единственный close-повод, удаление старых триггеров,
  HOLD-примеры, research-цитаты, корректность `%`-форматирования.
- Полный прогон: **1290 passed**.

#### Trade-off (зафиксировано осознанно)

Review больше не даёт «раннего выхода» на убыточной стороне — отдаём
это broker SL + full-циклу. Это намеренно: ранние закрытия по 1H шуму
статистически вредны (22/26). Контроль риска сохранён: broker SL —
hard floor, ставится при открытии.

#### Откат

Вернуть прошлую версию `SYSTEM_PROMPT_REVIEW` (Trigger 1/2/3) из git
history — изменение чисто prompt-level, без миграций БД.

#### Дальше (план Фаз 1+, ещё не реализовано)

Фаза 0 убирает конфликт на уровне промпта, но не делает систему
event-driven. Следующий фундаментальный шаг — живой поток цены
(websocket/streaming spots cTrader) вместо polling H1-close. План
готовится отдельно.

## 2026-05-28 — feat(regime-cutoff): фильтр self-reflection trades с Phase 1 deploy ts

`коммит при deploy`

### Контекст и trigger

После успешного COLD-START deploy в 08:32:32 UTC бот открыл и
закрыл первую discovery-сделку на XAUUSD BUY (+$6.95 за 49 мин).
В следующем full cycle (09:37 UTC) в SELF-REFLECTION блоке бот
сам прокомментировал:

> «NG=F BUY record 2/12 (17%) for −$43 – catastrophic; avoid NG
> long without structural macro shift. BZ=F BUY 1/6 but +$78 sum
> due to one outlier; recent BZ=F BUY trades failing (<30min closes)»

Пользователь обоснованно указал что **эти 12 NG=F BUY trades и
все BZ=F trades — pre-Phase-1 (до 2026-05-26 07:42 UTC)**, то
есть outcome **другой** версии стратегии:

| Symbol × Side | Trades | Все pre-Phase-1? |
|---|---:|:---:|
| NG=F BUY | 12 | ✅ (18-25 мая) |
| BZ=F BUY | 6 | ✅ (13-25 мая) |
| BZ=F SELL | 5 | ✅ (14-25 мая) |
| XAUUSD SELL | 3 | ✅ |
| XAUUSD BUY | 1 | ❌ (только сегодняшний COLD-START) |

После Phase 1 deploy (26 мая) — **0 трейдов на NG/BZ**. Бот сейчас
SELF-REFLECTION «наказывает» себя за поведение, которое уже исправлено
тремя последовательными правками: Phase 1 (persistent thesis,
2026-05-26), D1 (DXY+UST10Y+TIPS, 2026-05-27), COLD-START rule
(2026-05-28). Это **systematic continuation bias**: данные ≠
strategy, они outcome **другой** strategy.

### Research basis

| Источник | Положение |
|---|---|
| Lopez de Prado «Advances in Financial ML» (2018) ch.7 «Cross-Validation in Finance» | Structural breaks invalidate use of pre-break outcomes as evidence for post-break performance. Cross-validation внутри одного regime — обязателен. |
| Hamilton (1989) «A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle» Econometrica 57(2) | Regime-switching framework: финансовые временные ряды содержат discrete regime changes; параметры до и после change — разные DGP, mixing inadmissible. |

В нашем случае точная дата regime break — **2026-05-26 07:42 UTC**
(Phase 1 deploy timestamp = первое фундаментальное изменение
reasoning-rules после старта эксперимента 12 мая).

### Что изменено

| Файл | Что |
|---|---|
| `src/fx_ai_trader/config/settings.py` | new `stats_window_start: str` (default `"2026-05-26T07:42:00+00:00"`, env `AI_FX_TRADER_STATS_WINDOW_START`). Пустая строка отключает фильтр (legacy). |
| `src/fx_ai_trader/state/db.py` | `get_pnl_by_symbol`, `get_pnl_by_symbol_side`, `get_recent_closed_trades` — добавлен kwarg `since: str | None = None` (filter on `opened_at >= since`). Backward-compat: `since=None` == legacy behavior (passes existing tests). |
| `src/fx_ai_trader/llm/prompts.py` | `format_performance_by_symbol`, `format_performance_by_symbol_side`, `format_recent_trades` — добавлен kwarg `window_label: str | None = None` для header tag («since YYYY-MM-DD regime-change cutoff»). Backward-compat: `window_label=None` == legacy header. |
| `src/fx_ai_trader/llm/prompts.py::SYSTEM_PROMPT` | новый раздел **`REGIME-CHANGE WINDOW`** после `COLD-START DISCOVERY RULE`: объясняет что header «since YYYY-MM-DD» значит, цитирует Lopez de Prado + Hamilton, описывает interaction с COLD-START (cold-start re-trigger допустим, но 4 guards остаются обязательны). |
| `src/fx_ai_trader/app/main.py` | `_run_full_cycle` и `_run_review_cycle` пробрасывают `since=settings.stats_window_start` в DB-вызовы и `window_label=f"since {date} regime-change cutoff"` в format helpers. |
| `tests/test_fx_ai_trader_regime_cutoff.py` | 23 теста: DB filter (since/None/empty/inclusive boundary/cold-start re-trigger), format helpers (window_label + default), SYSTEM_PROMPT content asserts (section header + Lopez de Prado citation + Hamilton + cold-start interaction + "NOT a loophole" warning), Settings (default ts + env override + empty disables). |

### Effect для бота после deploy

Per-side stats теперь покажут только post-cutoff trades:

```
=== PERFORMANCE BY SYMBOL × SIDE (live, since 2026-05-28 regime-change cutoff) ===
- XAUUSD BUY: n=1, wins=1 (100.0%), avg_pnl=+6.95$, sum_pnl=+6.95$
- XAUUSD SELL: n=0 (NO live trades yet — COLD-START, see DISCOVERY RULE)
- BZ=F BUY: n=0 (NO live trades yet — COLD-START, see DISCOVERY RULE)
- BZ=F SELL: n=0 (NO live trades yet — COLD-START, see DISCOVERY RULE)
- NG=F BUY: n=0 (NO live trades yet — COLD-START, see DISCOVERY RULE)
- NG=F SELL: n=0 (NO live trades yet — COLD-START, see DISCOVERY RULE)
```

NG=F BUY / BZ=F BUY / BZ=F SELL / XAUUSD SELL / NG=F SELL re-активируют
**COLD-START DISCOVERY RULE** — то есть теоретически бот может попробовать
по одной discovery-сделке каждым из них, но только при выполнении ВСЕХ 4 guards:
1. MACRO supportive (research-driven)
2. SENTIMENT clean (aggregate_uncertainty ≤ 0.5)
3. SIZE strictly minimum (0.01 lot)
4. CADENCE ≤ 1 per (symbol × side) per week

**Защита от злоупотребления**: SYSTEM_PROMPT раздел REGIME-CHANGE WINDOW
явно говорит «This is **NOT a loophole** to ignore prior losses. Cold-start
discovery still requires ALL FOUR guards». Plus — pre-cutoff trades остаются
в БД для аудита, разработчик в любой момент может откатить cutoff на пустой
string (`AI_FX_TRADER_STATS_WINDOW_START=""`) и бот опять увидит всю
историю.

### Compliance check

| Rule | Соблюдение |
|---|---|
| `strategy-guard.mdc` (user approval для изменения торговой логики) | Пользователь явно одобрил «вариант 2 (query-time filter)» в AskQuestion 2026-05-28 12:58 UTC+3 |
| `no-data-fitting.mdc` (research as source of truth) | Lopez de Prado 2018 ch.7 + Hamilton 1989 процитированы в коде (`settings.py` docstring + SYSTEM_PROMPT новый раздел) и в этом BUILDLOG |
| `sample-size.mdc` (n<100 → не отключать) | Инструменты **НЕ** отключаются. Изменилось только что бот видит в SELF-REFLECTION — статистическая выборка перезапускается с точки regime change. БД физически сохранена. |
| `buildlog.mdc` (логирование изменений) | Эта запись |
| `api-docs.mdc` | Не применимо — query-time filter, никаких API-параметров |

### Acceptance criteria (smoke + observability)

**Сразу после deploy:**
- `docker logs fx-pro-bot-fx-ai-trader-1 --tail 80` показывает
  `LLM call (full) … us_rates=on self_reflection=closed_trades:N`
  где `N` — это количество post-cutoff trades (на момент deploy = 1
  только XAUUSD BUY от сегодняшнего COLD-START)
- USER_PROMPT в логах содержит блок `=== PERFORMANCE BY SYMBOL × SIDE
  (live, since 2026-05-26 regime-change cutoff) ===` (header с window
  label вместо «since experiment start»)
- 5 из 6 (symbol × side) пар должны показывать `n=0 (... COLD-START
  ...)` (только XAUUSD BUY остаётся с n=1)

**В течение 48 часов:**
- В analysis/thinking блоке хотя бы раз должно появиться явное
  упоминание «regime-change», «cutoff», или «cold-start» в контексте
  re-activation на NG/BZ (LLM прочитал новый SYSTEM_PROMPT раздел)
- БД сохраняет все pre-cutoff trades (audit invariant)
- daily_pnl таблица **не** должна разъехаться с новым SELF-REFLECTION
  view — daily_pnl не использует regime cutoff (это agreement reporting,
  не self-reflection input)

**Что НЕ acceptance criterion:**
- Открытие discovery trade на NG/BZ в 48h — это curve-fitting под
  желаемый результат (`no-data-fitting.mdc`). Если 4 guards не
  выполняются, HOLD — правильное поведение.
- Прибыльность post-cutoff — sample слишком мал (`sample-size.mdc`),
  rule оценивается через 2-4 недели на полной выборке.

### Что НЕ сделано в этом коммите

- Не меняется `daily_pnl` агрегат (financial reporting должен
  отражать всю историю, не cutoff window)
- Не меняется `decisions` таблица (audit-only)
- Не меняется `broker_reconcile` (operational integrity)

### Reversal procedure

Если через 2 недели окажется что cutoff создаёт side-effects (например,
discovery trades на NG=F BUY систематически проигрывают), можно:
- Отключить cutoff без deploy: `AI_FX_TRADER_STATS_WINDOW_START=""`
  в `.env` → бот опять видит всю историю (legacy v1.X behavior)
- Сдвинуть cutoff: `AI_FX_TRADER_STATS_WINDOW_START="2026-06-01T00:00:00+00:00"`
  если найдём ещё более позднюю regime-change точку

---

## 2026-05-28 — feat(cold-start): per-(symbol × side) PnL + DISCOVERY RULE в SYSTEM_PROMPT

`коммит при deploy`

### Контекст и trigger

После деплоя D1 (macro-rates, 2026-05-27 ~07:30 UTC) пользователь
снова поднял красный флаг: «он всё ещё не открыл ни одной позиции».

Расследование показало две причины (см. AskQuestion 2026-05-28 ~10:30
UTC+3):

1. **Strict confluence policy** (~69% всех hold reasons): SYSTEM_PROMPT
   раздел `THE SETUP — MACRO-FLOW CONFLUENCE PULLBACK` требует full
   8-rule confluence. Даже когда macro stack благоприятен после D1
   (real yields easing, DXY weakening), но структура/триггер не
   совпадают (например, цена в downtrend без reversal pattern), бот
   возвращает HOLD. Это правильное поведение — менять confluence
   policy без long-term статистики запрещено (`sample-size.mdc`).

2. **Self-reflection bias / cold-start trap** (~оставшиеся hold'ы):
   `get_pnl_by_symbol` агрегирует через side. Для XAUUSD выдаёт
   `n=3, WR 100%, +$21` — но все 3 трейда были SHORT, а LONG = 0
   trades в истории. LLM в SELF-REFLECTION блоке видит «3/3 wins»
   и думает «золото идёт» в обе стороны, но при анализе bullish
   setup'а на gold у него **нет** ни одной успешной long-сделки
   как референса → cold-start trap (canonical RL failure mode).

Пользователь явно выбрал **fix-trauma** опцию: «Снять травму по
золоту: добавить правило 'если ты никогда не торговал инструмент в
эту сторону — попробуй один раз минимальным размером для
эксперимента'».

### Research basis

| Источник | Цитата / положение |
|---|---|
| Sutton & Barto (2018) "Reinforcement Learning: An Introduction" §2.7 «Optimistic Initial Values» | «optimism encourages action-value methods to explore. Whichever actions are initially selected, the reward is less than the starting estimates; the learner switches to other actions, being 'disappointed'. The result is that all actions are tried several times before the value estimates converge» |
| Contextual Bandits literature (Personizely 2025; classic Sutton/Barto contextual bandit chapter) | «When adding new actions, initialize them with optimistic priors or guaranteed minimum exposure to ensure they get explored. Without this, new options might never be tried if existing options have a strong estimated performance» |
| Lopez de Prado (2018) "Advances in Financial ML" — exploration-exploitation в systematic trading | «Asset managers should focus their efforts on research developing theories, not backtesting trading rules» — applied: untested (symbol × side) — это unexplored hypothesis, не known-failure |

### Что изменено

| Файл | Что |
|---|---|
| `src/fx_ai_trader/state/db.py` | новая функция `AiFxTraderStore.get_pnl_by_symbol_side(symbols)` — агрегаты per-(symbol × side); явный `n=0` для untested pair; only `is_paper=0` (consistent с `get_pnl_by_symbol`); BUY первой per symbol |
| `src/fx_ai_trader/llm/prompts.py` | новый помощник `format_performance_by_symbol_side(stats)` + новый раздел SYSTEM_PROMPT **`COLD-START DISCOVERY RULE`** + новый JSON example `Example OPEN — COLD-START discovery (gold long, n=0)` + bullet в SELF-REFLECTION step 5 + tightening в DECISION TYPES OPEN: для discovery `aggregate_uncertainty` gate 0.7→0.5 + `volume_lots = 0.01` мандат + `reason` prefix `COLD-START discovery:` |
| `src/fx_ai_trader/llm/prompts.py::build_user_prompt` | новый optional kwarg `performance_by_symbol_side` (вставляется между per-symbol и recent-trades); порядок blocks: per-symbol → per-side → recent-trades; backward-compat (без kwarg prompt идентичен v1.X) |
| `src/fx_ai_trader/app/main.py` | вызов `store.get_pnl_by_symbol_side(...)` + проброс `format_performance_by_symbol_side(...)` в `build_user_prompt`; log message не менялся (per-side block — это просто расширение history) |
| `tests/test_fx_ai_trader_cold_start.py` | 23 теста: DB-aggregation (5: cold-start flag/side split/paper exclusion/order/mixed WR), format helper (4: empty/cold-start marker/full data/no $ on n=0 line), SYSTEM_PROMPT content asserts (7: section header/Sutton citation/four guards/reason prefix/«NOT» clauses/decision tightening/SELF-REFLECTION bullet), JSON example asserts (3: present/min size/uncertainty≤0.5), build_user_prompt integration (4: include/omit/backward-compat/order) |

### Дизайн правила (что именно разрешается)

**Гейт** (все 4 guards должны выполниться одновременно):
1. **MACRO supportive** — research-backed driver aligned с direction
2. **SENTIMENT clean** — `aggregate_uncertainty ≤ 0.5` (tightened
   с 0.7 для full-conviction trades — chcемо CLEAN сигнал для
   exploration trade)
3. **SIZE strictly minimum** — `volume_lots = 0.01` (broker step)
4. **CADENCE** — at most ONE discovery trade per (symbol × side)
   per week

**Что НЕ позволяется:**
- Bypass STRUCTURE/TRIGGER confluence (rules 2–8 of THE SETUP) —
  cold-start unlocks только size + sentiment gate
- Revenge-trade или «make something happen» on a quiet day
- Discovery trade на (symbol × side) с **любым** closed trade в
  истории (literal gate — `n=0` only)

**Outcome interpretation в SYSTEM_PROMPT:**
- Win на discovery ≠ доказательство (n=1 — статистический шум,
  `sample-size.mdc` принципиально)
- Loss на discovery ≠ доказательство по тем же причинам
- SELF-REFLECTION на следующем discovery должен flag «single
  observation, not yet evidence»

### Compliance check

| Rule | Соблюдение |
|---|---|
| `strategy-guard.mdc` (изменение торговой логики только с согласия пользователя) | Пользователь явно одобрил опцию `fix_trauma` в AskQuestion 2026-05-28 |
| `no-data-fitting.mdc` (research как источник правды) | Sutton & Barto 2018 §2.7 + contextual bandits literature процитированы в коде (SYSTEM_PROMPT блок «Research basis») и в этом BUILDLOG |
| `sample-size.mdc` (n=1 ≠ evidence) | Явно прописано в SYSTEM_PROMPT «Discovery-trade outcome interpretation»; n=1 win НЕ scale up, n=1 loss НЕ disable |
| `buildlog.mdc` (логирование изменений стратегии) | Эта запись |
| `api-docs.mdc` | Не применимо — правка чисто prompt+DB-aggregation, никаких API-параметров |

### Acceptance criteria (smoke + observability)

**Сразу после deploy:**
- `docker logs fx-pro-bot-ai-trader-1 --tail 80` показывает `LLM
  call (full) … us_rates=on` (regression-check D1 не сломан)
- Один full cycle прошёл без exception в коде (BUILDLOG отметит
  cycle_id)
- USER_PROMPT в логах содержит `=== PERFORMANCE BY SYMBOL × SIDE
  (live, since experiment start) ===` блок с `COLD-START` маркером
  на untested направлениях

**В течение 48 часов:**
- В analysis (thinking) блоке хотя бы раз должно появиться явное
  упоминание «COLD-START» или «discovery» (LLM прочитал новый
  раздел)
- Если бот откроет discovery trade — он должен соответствовать
  всем guards: `volume_lots == 0.01`, reason начинается с
  `COLD-START discovery:`, sentiment.aggregate_uncertainty ≤ 0.5

**Что НЕ acceptance criterion:**
- Открытие хотя бы одной discovery trade в 48h — это
  curve-fitting под желаемый результат (`no-data-fitting.mdc`).
  Если 4 guards не выполняются, HOLD — правильное поведение.
- Изменение general win-rate за 48h — sample слишком мал
  (`sample-size.mdc`).

### Что НЕ сделано в этом коммите

- Re-evaluation strict confluence policy (~69% hold'ов). Это
  отдельный вопрос требующий long-term статистики; pas changement
  без обсуждения (sample-size.mdc + strategy-guard.mdc).
- D2-D5 из NEXT_PHASE_AI_FX_TRADER.md (review-noise guard, DXY
  context — уже сделан в D1, mandatory sentiment, reason length
  alignment). Остаются в плане.

---

## 2026-05-27 — feat(macro-rates D1): DXY + UST10Y + TIPS ETF в context (Phase 2)

`коммит при deploy`

### Контекст и trigger

После деплоя Phase 1 (persistent thesis, 2026-05-26 07:42 UTC) +
v4 prompt-tune (08:13 UTC) — за **22 часа** 0 opens на 116 full-cycles.
Пользователь поднял красный флаг «странно, ни одного лота не открыто».
Расследование (см. AskQuestion 2026-05-27 09:23 +3 UTC):

| Период | Решений | Avg uncertainty | Opens | Holds |
|---|---:|---:|---:|---:|
| AFTER v4-tune (22ч) | 116 | **0.68** | 1 | 115 |
| Pre-deploy 2 дня | 170 | 0.54 | 2 | 166 |
| 4 дня раньше | 351 | 0.56 | 16 | 335 |

**Главный фактор** — рынок: Hormuz ceasefire / EIA mixed / NG storage build
подняли uncertainty с 0.54 → 0.68 (+25%). Базовая стат-проверка по
`sample-size.mdc`: ожидаем 116×0.026=3 opens, фактически 1 → Poisson p≈0.20
(не значимо). До правок дни 17/22/24 мая были 0 opens на полный день —
это **норма для стратегии**.

**Но**: в 100% hold reason'ов LLM пишет *«gold lacks DXY/real-yield
confirmation»* и *«lacks macro confirmation»*. Это **прямое указание на
известный баг D1** из NEXT_PHASE_AI_FX_TRADER.md (Phase 1 нестыковка #4):
SYSTEM_PROMPT за ~20 мест ссылается на DXY и real yields как primary
gold-драйверы, прямо обещает «We see DXY proxy 24h change in context»
(prompts.py:170), но `collect_market_context` исторически их не отдаёт.
Бот по этой причине **физически не может** торговать золото по своей
же стратегии. Пользователь явно одобрил ускоренное закрытие D1.

### Что изменено

| Файл | Что |
|---|---|
| `src/fx_ai_trader/data/__init__.py` | новый package |
| `src/fx_ai_trader/data/macro_rates.py` | `MacroRatesProvider` + `MacroRatesSnapshot` + `format_macro_rates_snapshot` |
| `src/fx_ai_trader/trading/context.py` | `MarketContext.macro_rates_block`; `collect_market_context(macro_rates_provider=…)`; рендер в `format_context_for_prompt` ПЕРЕД per-symbol macro (canonical hierarchy) |
| `src/fx_ai_trader/app/main.py` | init `MacroRatesProvider`, проброс в `_run_full_cycle`, поле `us_rates=on/off` в log line `LLM call (full)` |
| `src/fx_ai_trader/config/settings.py` | `macro_rates_enabled: bool = True` + `macro_rates_cache_ttl_sec: int = 1800` |
| `.env.example` | задокументированы `AI_FX_TRADER_MACRO_RATES_ENABLED` и `..._CACHE_TTL_SEC` |
| `NEXT_PHASE_AI_FX_TRADER.md` | секция D1 помечена done |
| `tests/test_fx_ai_trader_macro_rates.py` | 19 тестов (format + provider happy/cache/degradation + context integration + prompt rendering + review **не** содержит rates) |

### Данные, которые добавились в context

```
=== US MACRO RATES (gold/oil drivers; gold-canonical hierarchy: real yields → DXY) ===
DXY (US Dollar Index, ICE futures DX-Y.NYB): 99.12 (24h=-0.03%, 5d=-0.19%)
UST10Y nominal yield (CBOE TNX): 4.49% (24h=-6.5bps, 5d=-13.0bps)
TIP (iShares TIPS ETF, real-yields proxy — price↑ ↔ real yields↓): $110.82 (24h=+0.40%, 5d=+0.31%)
(fetched 2026-05-27T06:42:49+00:00 UTC)
```

Это **live snapshot** локального smoke-теста 2026-05-27 06:42 UTC.
DXY<100 + real yields easing + TIP rising = canonical gold long
confluence, которое бот сейчас слепо игнорирует.

### Research artifact

| Тезис | Источник |
|---|---|
| Gold ↔ DXY inverse corr -0.6 … -0.8 | Erb & Harvey (2013) «The Golden Dilemma», NBER WP 18706 |
| Gold ↔ real yields R² ≈ 0.55 (2003-2024) | World Gold Council (2024) «Gold and real interest rates» |
| TIPS yield = real cost of money proxy | Fed Board (2007) «TIPS and the inflation risk premium» |
| Oil ↔ DXY corr -0.3 … -0.5 (weaker) | Akram (2009) Energy Economics |

### Источники данных (компiance с `api-docs.mdc`)

| Ticker | Источник правды | Цитата по спецификации |
|---|---|---|
| `DX-Y.NYB` | ICE «US Dollar Index Futures» <https://www.theice.com/products/194/US-Dollar-Index-Futures> | canonical US Dollar Index используется KenMacro / institutional desks |
| `^TNX` | CBOE 10-Year Treasury Yield Index | values уже в % (4.31 = 4.31%); legacy yfinance возвращал ×10, нормализация в коде если raw>25% |
| `TIP` | iShares TIPS Bond ETF <https://www.ishares.com/us/products/239467/> | price↑ ↔ real yields↓ (inverse) |

yfinance — third-party библиотека уже используется в `src/fx_pro_bot/`
для bars; не вводим новой зависимости. Без API-ключа. Cache 30 мин
(достаточно freshness, без HTTP-перегруза).

### Что НЕ сделано (с обоснованием)

1. **Точное real-yield число (10Y TIPS yield)**: требует FRED `DFII10` +
   FRED_API_KEY. Не вводим лишний секрет ради ±5 bps точности — `TIP`
   ETF даёт **направление** (rising TIP ↔ real yields easing), что
   достаточно для confluence-check.
2. **Изменения SYSTEM_PROMPT**: ни одной строки промпта не меняем.
   Промпт уже умеет с этими рядами работать — мы закрываем дырку
   между обещанием промпта и фактическим контекстом. Чистый quasi-bugfix.
3. **Включение rates в review-cycle**: NO. SYSTEM_PROMPT_REVIEW явно
   говорит «NO macro feed, NO news, NO EIA, NO 4H bars» — rates это
   macro feed; review остаётся lite.

### Compliance

- **`strategy-guard.mdc`**: НЕ меняем торговую логику (нет новых
  thresholds, exit-логики, R:R). Quasi-bugfix: промпт обещал данные,
  кода не было. Пользователь явно одобрил ускоренное закрытие D1.
- **`no-data-fitting.mdc`**: 4 research-источника выше + сам код без
  hardcoded thresholds (только cache TTL и normalize-эвристика).
  Никакого тюнинга «под результат».
- **`api-docs.mdc`**: ссылки на ICE / CBOE / iShares + WGC research
  оставлены в docstring и в этом BUILDLOG. yfinance — уже
  существующая dep, не новая.
- **`sample-size.mdc`**: текущее «0 opens за 22ч» **не достигает**
  порога p<0.05 (Poisson p≈0.20). Не отключаем ничего, **добавляем
  данные**. После деплоя ожидаем естественный rebound open-rate
  по золоту (по reason'ам holds — это и есть блокирующий фактор).

### Acceptance criteria (после деплоя)

После ≥7 дней наблюдения:

1. **100% full-cycle prompts** содержат `=== US MACRO RATES ===` блок
   (modulo yfinance outages; cache 30 мин гасит транзиентные сбои).
2. **Reasoning hold'ов по золоту**: фраза «lacks DXY/real-yield
   confirmation» падает с ~100% (текущее) до <30% (LLM либо открывает,
   либо приводит конкретный аргумент типа «DXY rising → not going long»).
3. **Open-rate по XAUUSD**: ожидаем рост vs последняя неделя
   (нейтрально-положительная гипотеза). НЕ оптимизируем под это число.
4. **WR на XAUUSD trades** (если будут): не должен деградировать ниже
   baseline (по правилу `sample-size.mdc` требуется ≥30 trades для
   значимого вывода — собираем дольше).
5. **Errors / yfinance failures**: <5% циклов с `us_rates=off` в логе.

### Smoke-check план (после deploy)

1. `docker logs fx-pro-bot-fx-ai-trader-1 --tail 50` после первого
   full-cycle — должно быть `us_rates=on` в log line.
2. `decisions.prompt_user` последнего цикла должен содержать `US MACRO
   RATES` и три тикера с числами.
3. `decisions.parsed_action.reason` по золоту — проверить отсутствие
   «lacks DXY» в новых hold'ах.

---

## 2026-05-26 — feat(v4-prompt-tune): A+B+C+D — task sandwich, concrete JSON examples, prime expected output, output_config.effort=high

`коммит при deploy`

### Контекст

После Phase 1 (persistent thesis discipline, запись ниже того же дня)
проведён audit DeepSeek-V4 prompt engineering best practices из
официальных источников + community-проверенного guide. Цель — найти
**обоснованные** улучшения подачи существующего промпта, без изменения
торговой логики и доменных правил.

### Research artifact (источники)

| # | Источник | URL | Ключевая цитата |
|---|---|---|---|
| 1 | DeepSeek API Docs — Anthropic-compat | [api-docs.deepseek.com/guides/anthropic_api](https://api-docs.deepseek.com/guides/anthropic_api) | «thinking — Supported (`budget_tokens` is ignored); output_config — Only `effort` is supported; cache_control — Ignored» |
| 2 | Anthropic Adaptive Thinking | [platform.claude.com/docs/en/build-with-claude/adaptive-thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking) | «effort: low \| medium \| high (default) \| max; controls thinking allocation» |
| 3 | DeepSeek V4 Practitioner's Guide | [deepseekai.guide/tutorials/deepseek-prompt-engineering](https://deepseekai.guide/tutorials/deepseek-prompt-engineering/) | «DeepSeek weighs early tokens more heavily ... put a one-line restatement of the task after the context too — a 'task sandwich'» + «End your prompt with the first characters of the expected output ... dramatically reduces preamble drift on V4-Flash» + «show a small example schema in the prompt — not just describe it» |
| 4 | DeepSeek V4 Templates (12 patterns) | [deepseekai.guide/tools/deepseek-prompt-templates](https://deepseekai.guide/tools/deepseek-prompt-templates/) | «System block — role, constraints, output format. Stable across calls» (наш паттерн уже соответствует) |
| 5 | Pydantic-AI thinking docs | [github.com/pydantic/pydantic-ai/blob/.../thinking.md](https://github.com/pydantic/pydantic-ai/blob/7f5214c6/docs/thinking.md) | «Anthropic effort: top-level request parameter separate from thinking object» |

### Что у нас уже соответствовало best practices (НЕ трогаем)

- ✅ Чёткая role в SYSTEM_PROMPT
- ✅ JSON schema inline в промпте (но как описание, не как concrete example — фиксится в B)
- ✅ Слово "JSON object" используется многократно
- ✅ `max_tokens=8000` с обоснованием в коде (защита от truncation)
- ✅ try/except + truncation guard в коде
- ✅ Thinking enabled (`thinking_enabled=True`)
- ✅ English-only промпт
- ✅ Стабильный SYSTEM_PROMPT, volatile в USER

### Что НЕ применяем (с обоснованием)

| Что предлагалось | Почему отклонено | Источник |
|---|---|---|
| Менять `temperature` | thinking mode её игнорирует на большинстве моделей; у нас нет evidence для tuning | community guide |
| Cache-friendly переупорядочивание | `cache_control` игнорируется на Anthropic-compat endpoint DeepSeek — бесполезная работа | DeepSeek API docs (источник #1) |
| CO-STAR rewrite | Наша domain-specific структура (5-driver / 4-channel / NG framework) уже адекватна | — |
| Раздувать SYSTEM_PROMPT новыми доменными правилами | Уже ~14k tokens после Phase 1; убывающая отдача | — |

### Что меняем (A+B+C+D)

**A. Task sandwich.** Добавляем 1-2 строки task-summary в **начале** SYSTEM_PROMPT (после role) и **в конце** USER_PROMPT (после market_context, перед outro). Обоснование: deepseekai.guide guide — «role and task should land before any long context block» + «put a one-line restatement of the task after the context ... so the instruction is not buried». Применимо: SYSTEM_PROMPT ~14k tokens, USER_PROMPT 2-4k tokens — instruction действительно может «потеряться».

**B. Concrete JSON examples.** Рядом со schema Open/Close/Hold добавляем **заполненный пример** с реалистичными значениями (включая новые thesis_status / thesis_invalidator). Обоснование: deepseekai.guide guide — «show a small example schema, not just describe it». Особенно важно для thesis_status/thesis_invalidator (введены вчера, у LLM нет precedent в training).

**C. Prime expected output.** Конец `build_user_prompt` / `build_user_prompt_review` заканчивается явной директивой: «Begin your reply with: `## ANALYSIS\n1) MACRO DRIVER:`». Обоснование: deepseekai.guide guide — «dramatically reduces preamble drift on V4-Flash» (наша модель `deepseek-v4-flash`).

**D. `output_config.effort='high'`.** Передаём явно через `extra_body` в `client.messages.create(...)`. Сейчас используется default (по Anthropic docs — `high`, но для DeepSeek-side не задокументирован). Делаем explicit + логируем для audit. Обоснование: DeepSeek API docs — «output_config — Only `effort` is supported», Anthropic docs — `high` даёт «deep reasoning on complex tasks» (multi-driver commodity analysis это и есть complex task).

### Compliance

- **`strategy-guard.mdc`**: A/B/C — изменение **формата подачи** существующих правил (никаких новых thresholds, новых триггеров, изменения exit-логики). D — технический tuning API-параметра. Все 4 одобрены пользователем через AskQuestion (scope=abcd).
- **`no-data-fitting.mdc`**: research artifact — 5 источников выше с прямыми цитатами и URL. Без подгонки под результат (никаких numeric thresholds не меняем).
- **`sample-size.mdc`**: combined с Phase 1 — единое 1-неделя observation окно после деплоя. Acceptance criteria Phase 1 (≥90% close с непустым thesis_status, ≤30% intact close) остаются валидны.
- **`api-docs.mdc`**: D ссылается на официальную DeepSeek API doc + Anthropic effort spec, оставлен URL-комментарий в `llm/client.py` рядом с параметром.

### Acceptance criteria для prompt-tune

После ≥30 closed trades + ≥1 недели наблюдения:
1. **Preamble drift** должен снизиться: доля responses начинающихся с **точного** «## ANALYSIS\n1) MACRO DRIVER:» (или близко) ≥ 80% (сейчас ~50% по audit, остальное — wandering preamble).
2. **JSON парс ошибок** (`parse_error` в decisions.error): не должны вырасти. Любая регрессия → откатываем prompt-tune отдельным коммитом.
3. **Token budget**: SYSTEM_PROMPT не должен превысить ~15k tokens (мы добавляем ~150-300 tokens на task summary + examples).
4. **`thesis_status` заполняемость** не должна упасть (concrete examples с thesis_status должны подкрепить Phase 1 effect).

### Файлы

- `BUILDLOG_AI_FX_TRADER.md` (эта запись)
- `src/fx_ai_trader/llm/prompts.py` (SYSTEM_PROMPT + SYSTEM_PROMPT_REVIEW + build_user_prompt + build_user_prompt_review)
- `src/ai_trader/llm/client.py` (output_config.effort через extra_body)
- `tests/test_fx_ai_trader_persistent_thesis.py` (расширить asserts на новые prompt markers)

---

## 2026-05-26 — feat(persistent-thesis): требование явного `thesis_status` при close + audit 10 нестыковок промпта (Phase 1)

`коммит при deploy`

### Контекст и observed (research artifact, ДО изменения кода)

После добавления self-reflection (запись ниже, тот же день) проведён
полный audit decision-making бота. Цель — найти **системные противоречия**,
которые self-reflection один не починит (`AskQuestion 2026-05-26`,
пользователь выбрал A+B — журнал + главный фикс, C+D перенесены в
`NEXT_PHASE_AI_FX_TRADER.md`).

**Источник:** анализ всей истории VPS (1497 LLM-запросов, 27 closed live
trades, 12 дней) + полный re-read обоих промптов
(`SYSTEM_PROMPT`, `SYSTEM_PROMPT_REVIEW`) против реальных decision'ов.
Соответствует `.cursor/rules/no-data-fitting.mdc` («research artifact
обязателен перед любой правкой торговой логики»).

### Корень проблемы (single-sentence)

**Открытие позиции — макро-thesis-driven, закрытие — техническое-noise-driven,
без обязательной перепроверки исходного thesis.** LLM открывает по
«Hormuz tension + 4H breakout», закрывает через 5-16 мин по «MACD flip /
BB middle break», и в `close_reason` НИ РАЗУ не пишет «macro thesis
broken because X». Self-reflection видит «open→close <30мин» как noise-
паттерн, но review-цикл сам и генерирует эти кейсы.

### Конкретные кейсы из VPS (доказательство паттерна)

**Кейс 1 — NG=F id=27, 2026-05-25 (uplink в self-reflection-записи ниже):**

| | open | close (16 мин позже) |
|---|---|---|
| reason | "NOAA cold anomaly East Coast + STEO bullish + 1H breakout above BB" | "Macro bearish: storage build + rising production + **mild weather**; 4H downtrend" |

Один и тот же NOAA-источник интерпретирован прямо противоположно
(«холодно» → «мягко»). SL не сработал. Macro thesis при закрытии
**не процитирован** — нет фразы «NOAA-cold thesis broken because».

**Кейс 2 — BZ=F id=2, прибыль +$92.82:**

| | open | close |
|---|---|---|
| reason | «Chinese tanker testing Hormuz Strait passage signals **persistent tension** → bullish crude» | «Chinese tanker passage signals **de-escalation** → SELL» |

Один и тот же факт (китайский танкер) — противоположные выводы.

**Кейс 3 — NG=F кластер 20 мая (6 итераций open→close):**

6 раз подряд бот открывает одну и ту же позицию по одному и тому же
макро-обоснованию («Australia LNG strikes + Israel field shutdown +
LNG supply tightening»). Каждый раз закрывает через 5-50 мин по
техническому сигналу (MACD против позиции). **В 6 close-reason'ах
макро-thesis ни разу не процитирован как «broken»**.

### Систематика (полная выборка)

- 18 / 27 closed live trades живут **< 60 мин** (большинство 5-16 мин).
- 22 / 26 LLM-закрытий (исключая broker_auto SL/TP) — **чисто технические**:
  «MACD flip», «BB middle break», «1H close against direction».
- WR = 33% (9 / 27 winners) за 12 дней.
- Net PnL в плюс **за счёт одного крупного винера** id=2 (+$92), без
  него медиана и mean отрицательные.

### 10 HIGH-severity нестыковок (audit `SYSTEM_PROMPT` + `SYSTEM_PROMPT_REVIEW`)

| # | Что обещано / прописано | Что реально / противоречие | Severity |
|---|---|---|---|
| 1 | «`aggregate_uncertainty > 0.7` → return HOLD» (anti-hallucination gate, строка 439 промпта) | Для XAUUSD/BRENT/NG=F **inherent macro uncertainty высокая** (раздел `WHAT YOU DO NOT SEE`: нет TIPS feed, нет COT, infer из price+news). Дисциплинированное следование = perpetual hold | HIGH |
| 2 | «If 6-7 of the 8 align, it is a setup developing WATCH, **not a trade**» (MFP, строка 394) | «LOW-conviction setup (1 driver aligned): risk ~0.5% of capital» (строка 328) | HIGH |
| 3 | Conflicting signals **внутри** symbol: gold real-yields ↓ vs DXY ↑; NG EIA bullish vs NOAA bearish | **Нет правил приоритета**. LLM сам выбирает «что больше нравится» — отсюда reasoning flips между циклами на тех же данных | HIGH |
| 4 | «DXY proxy 24h direction» обещано в `WHAT YOU SEE EACH FULL CYCLE` (строка 451) | `context.py` (`collect_market_context`) **не передаёт DXY** — LLM либо галлюцинирует, либо игнорирует. Driver #2 для gold (correlation -0.6 до -0.8) фактически слепой | HIGH |
| 5 | Entry: «4H structural» (MFP rule 2, «4H trend direction + key level» в ANALYSIS) | Review-close triggers: «1H EMA20 / BB middle / RSI / MACD» (SYSTEM_PROMPT_REVIEW, строки 720-729). **4H invalidation не определён** — нет shared invalidation между entry/exit | HIGH |
| 6 | Full-cycle close triggers (строки 476-482): «macro driver flipped», «4H trend broke», «adverse news» — макро + news | Review-cycle close triggers (строки 720-729): «1H EMA20», «BB middle», «MACD flip» — чисто тех. **22/26 закрытий пошли через review-путь — макро-проверка не делалась** | HIGH |
| 7 | Self-reflection (новая v1.X, добавлена этой же датой): «если паттерн `open→reverse-close <30мин by reasoning` повторяется → raise the bar» | Review-cycle **сам генерирует** эти короткие reversals на 1H шуме, **и НЕ получает recent_trades** (review остаётся lightweight — phase 1 design) — система критикует то, что сама же создаёт | HIGH |
| 8 | Multi-source context: news + EIA/NOAA + technicals + (теперь) recent_trades | **Нет правил resolution conflict**. LLM сам решает что важнее, отсюда reasoning flips и противоположные интерпретации одного факта (Кейс 2: танкер) | HIGH |
| 9 | `OpenAction.sentiment: Optional[SentimentBlock] = None` (executor.py:176) | Если LLM не прислал sentiment → `aggregate_uncertainty > 0.7` gate **не срабатывает** (строка 304: `if model.sentiment is not None and ...`). Bypass возможен через простое omission | HIGH |
| 10 | SYSTEM_PROMPT JSON schema: `"reason": "<≤200 chars>"` (строки 535, 552, 557) | Pydantic `ClampedReason = Field(max_length=300)` — **расхождение лимита 100 chars**. После clamp-fix 2026-05-25 это уже не reject, но LLM может тратить токены думая «300 ок» когда промпт говорит «200» | HIGH (документация-driven) |

### Compliance

- **`.cursor/rules/strategy-guard.mdc`**: B — изменение торговой логики
  (требование `thesis_status` при close). **Одобрение пользователя
  получено** через `AskQuestion 2026-05-26 scope=ab`.
- **`.cursor/rules/no-data-fitting.mdc`**: research artifact = эта
  таблица + 3 конкретных кейса + 12-дневная VPS-история (1497 LLM
  calls, 27 trades). Без подгонки параметров под результат — фикс
  про **обязательность поля**, не про новый numeric threshold.
- **`.cursor/rules/sample-size.mdc`**: B не отключает символ, не
  меняет thresholds. Это **дисциплинарное требование** (как
  «обязан вернуть sentiment» — schema-уровень). После деплоя — собрать
  ≥30 closed trades (≈1 неделя), сравнить:
  - avg trade duration (ожидаем рост с ~30мин до 60+ мин)
  - % `thesis_status="intact"` среди close (ожидаем падение с ~80% до <30%)
  - WR (нейтральный hypothesis: не должна упасть; рост был бы бонус).

### Что меняем (Phase 1, B)

**Кратко:** добавляем два поля в `CloseAction` + блок THESIS DISCIPLINE
в SYSTEM_PROMPT + idempotent миграция БД + soft-валидация (WARN, не
reject — чтобы не сломать первые часы после деплоя пока LLM учится
заполнять новые поля).

Подробности по файлам — см. секцию «Файлы» ниже после deploy.

### Что НЕ меняется (Phase 1 out of scope, → `NEXT_PHASE_AI_FX_TRADER.md`)

- **C. Review-noise guard** — запрет close через review в первые 30 мин.
- **D1. DXY в `context.py`** — добавить proxy в передаваемый блок.
- **D2. `OpenAction.sentiment` obligatory** — убрать `Optional` (нестыковка #9).
- **D3. Reason length alignment** — выровнять 200/300 (нестыковка #10).

Эти задачи **уже задокументированы** с evidence/acceptance в файле
второй фазы; могут быть выбраны отдельными правками без re-audit.

### Acceptance criteria для Phase 1

После ≥30 closed live trades (≈1 неделя на текущей частоте):
1. ≥90% close-decisions имеют непустой `thesis_status` (заполняемость поля).
2. Доля `thesis_status="intact"` среди close ≤ 30% (сейчас ≈80%).
3. Среди close с `thesis_status="intact"` — ≥80% сопровождаются news/SL
   trigger или time-decay >24ч (соответствие правилу «закрытие при
   intact thesis разрешено только при ...»).
4. **НЕ деградирует** WR (нейтральная гипотеза).
5. Зафиксировать `thesis_status` distribution в follow-up BUILDLOG-записи.

### Файлы

- `BUILDLOG_AI_FX_TRADER.md` (эта запись)
- `NEXT_PHASE_AI_FX_TRADER.md` (новый)
- `src/fx_ai_trader/llm/prompts.py` (SYSTEM_PROMPT: THESIS DISCIPLINE)
- `src/fx_ai_trader/trading/executor.py` (CloseAction: thesis_status, thesis_invalidator)
- `src/fx_ai_trader/state/db.py` (миграция: 2 колонки в `decisions`)
- `src/fx_ai_trader/app/main.py` (запись новых полей при `log_decision`)
- `tests/test_fx_ai_trader_persistent_thesis.py` (новый, schema + DB migration + prompt content)

---

## 2026-05-26 — feat(v1.X self-reflection): per-symbol performance + recent closed trades в USER_PROMPT

`коммит при deploy`

### Контекст и observed (evidence из 2026-05-25)

Два убыточных трейда подряд, оба по одной reasoning-патологии:

| id | symbol | side | entry | exit | pnl | duration | trigger | open justification | close justification |
|----|--------|------|-------|------|-----|----------|---------|--------------------|----|
| **27** | NG=F | BUY | $3.05 | $3.044 | **−$14.40** | **16 мин** | LLM full-cycle close | NOAA cold anomaly + STEO + 1H breakout above BB | "Macro bearish: storage build, rising production, mild weather; 4H downtrend intact" |
| **28** | BZ=F | BUY | $94.904 | $94.759 | **−$3.42** | **5 мин** | LLM review-cycle close | US-Iran Hormuz strikes + oversold 4H RSI + 1H divergence + EIA draw, fade panic | "Broke below lower BB/EMA20, setup invalidated (trigger 1), adverse evidence (trigger 3)" |

**Паттерн:** «**LLM передумал через 5-16 минут**». SL **ни разу не сработал** —
обе позиции закрылись по reasoning. Это reasoning-проблема (entry triggers
too noisy / не дано setup'у развернуться), не sizing-проблема и не
symbol-проблема. По правилу `.cursor/rules/sample-size.mdc` 2 трейда —
**не статистическое основание** для disable символа, hard-cap'а notional
или ужесточения per-symbol лимитов. Допустимо только **информативное
feedback** для reasoning'а.

### Решение пользователя (цитата выбора в чате 2026-05-26)

> «нужно добавить обучение модели на своих ошибках, вчера бот торговал
> позицию газа и нефти в минус, нужно чтобы он учитывал свои ошибки при
> входе и слежении за позицией»

После обсуждения выбран **вариант C + окно all** (выбор зафиксирован
через AskQuestion):
- **C**: per-symbol агрегаты (Performance by Symbol) **+** последние
  10 closed trades с open/close reason. Максимум контекста для выводов.
- **all**: since experiment start (база ~30 closed live trades). Окно
  эквивалентно 30d на текущем этапе эксперимента.

### Что добавлено (8 файлов)

**1. `src/fx_ai_trader/state/db.py` — 2 новых метода (порт паттерна из
`ai_arena/state/db.py` v2.z1):**

- `get_pnl_by_symbol(symbols)` — per-symbol агрегаты
  (`n / wins / win_rate_pct / avg_pnl_usd / sum_pnl_usd`). Фильтр
  `closed_at IS NOT NULL AND is_paper = 0` (paper-fills из
  `paper_reconcile.py` могут расходиться с реальностью — не учитываем).
  Включает символы с `n=0` (явный сигнал «не торговали» — как в
  ai_arena).
- `get_recent_closed_trades(limit=10, reason_clamp=180)` — последние N
  closed live trades. Поля: `id`, `symbol`, `side`, `volume_lots`,
  `entry_price`, `exit_price`, `realized_pnl_usd`, `opened_at`,
  `closed_at`, `duration_minutes`, `llm_reason` (clamp 180 chars),
  `close_reason` (clamp 180 chars). Возврат в порядке **oldest →
  newest** для prompt readability. Clamp нужен потому что executor
  schema допускает до 300 chars (см. entry 2026-05-25), но для prompt'а
  180 достаточно — иначе на 10 trades распухнет budget.

**2. `src/fx_ai_trader/llm/prompts.py` — форматтеры + расширение
сигнатур:**

- `format_performance_by_symbol(stats)` → блок
  `=== PERFORMANCE BY SYMBOL (live, since experiment start) ===`,
  одна строка на символ. Пустой `stats` → пустая строка (canonical
  для cycle'ов до первой сделки).
- `format_recent_trades(trades)` → блок
  `=== RECENT CLOSED TRADES (last N, oldest -> newest) ===`,
  multi-line компактный layout (id / symbol / side / lots / entry /
  exit / pnl / duration + open: / close:).
- `build_user_prompt(market_context, *, performance_by_symbol=None,
  recent_trades=None)` — оба новых параметра опциональны (backward
  compat: оба `None` → prompt идентичен v1.0). Блоки идут **в начале**
  user prompt'а перед `market_context` — LLM сначала видит свою
  историю, потом текущий рынок (тематически логично, не загромождает
  `trading/context.py`).
- `build_user_prompt_review(market_context, *,
  performance_by_symbol=None)` — только агрегаты, **без** `recent_trades`
  (review остаётся lightweight, см. SYSTEM_PROMPT_REVIEW «NO macro
  feed, NO news, NO EIA, NO 4H bars»).
- SYSTEM_PROMPT (ANALYSIS STRUCTURE BEFORE JSON): добавлен **пункт 5
  SELF-REFLECTION** — guidance что делать с history (cross-check
  pattern, raise bar at WR<30% n≥5, явный запрет revenge trading со
  ссылкой на Mark Douglas «Trading in the Zone»). Никаких жёстких
  правил «не торгуй если sum_pnl<0».

**3. `src/fx_ai_trader/app/main.py` — интеграция в loop'ы:**

- `_run_full_cycle`: перед `build_user_prompt` вызов
  `store.get_pnl_by_symbol(settings.symbols)` +
  `store.get_recent_closed_trades(limit=10)`, оба форматируются и
  передаются в prompt. Лог дополнен метрикой
  `self_reflection=closed_trades:N`.
- `_run_review_cycle`: перед `build_user_prompt_review` только
  `get_pnl_by_symbol` (без recent_trades).

### Что НЕ изменилось (явно)

- **НЕТ** disable NG=F / BZ=F / XAUUSD по 1-2 убыткам
  (`sample-size.mdc`, нужно n≥100, p<0.05, ≥2 недели).
- **НЕТ** cooldown-after-loss / hard-cap notional / ужесточения
  per-symbol caps (мало данных).
- **НЕТ** lesson-per-trade с отдельным LLM-call (дорого, premature).
- **НЕТ** изменения SL/TP defaults / R:R / volume formulas / killswitch
  thresholds.
- **НЕТ** review-cycle hold-by-default правила («не закрывай <30 мин
  без strong adverse evidence») — это вариант E, не выбран; можно
  добавить позже отдельной правкой если C не сработает за 30 trades.
- **НЕ тронуты** `trading/context.py`, `trading/executor.py`,
  `safety/killswitch.py`, `trading/paper_reconcile.py`,
  `trading/broker_reconcile.py`, `news/*`.

### Тесты

`tests/test_fx_ai_trader_self_reflection.py` (новый, 23 теста):

- **DB:** empty store → n=0 для каждого символа; aggregates only live
  (paper ignored); preserves symbol order; win-rate `pnl=0` не = win;
  empty/limit/oldest-newest order / clamp long reason / duration_minutes
  non-negative.
- **Форматтеры:** empty list → empty string; n=0 символ показывается
  явно «no closed live trades yet»; header + lines + signed pnl;
  `close_reason=""` → `(broker auto / SL or TP)` fallback.
- **`build_user_prompt`:** backward compat (default None → v1.0
  layout); empty strings treated as None; порядок (Performance →
  Recent Trades → Market Context); упоминание SELF-REFLECTION в outro.
- **`build_user_prompt_review`:** backward compat; performance block
  inserted; **по дизайну** review **не принимает** `recent_trades`
  параметр (проверяется через `inspect.signature`).

**Результат:** `103/103 fx_ai_trader тестов проходят` (80 существующих
+ 23 новых, backward compat не сломан). Полный test suite репо:
`1053/1053 passed`.

### Compliance

- `.cursor/rules/sample-size.mdc`: feedback ≠ hard-cap; никаких
  изменений параметров стратегии (SL/TP/lots/thresholds). Любые выводы
  на основании injected stats — за LLM, не на server-side.
- `.cursor/rules/strategy-guard.mdc`: пользователь явно одобрил выбор
  C+all через AskQuestion перед началом работы. SYSTEM_PROMPT правка —
  только добавление пункта SELF-REFLECTION в существующую структуру,
  без изменения торговых параметров.
- `.cursor/rules/no-data-fitting.mdc`: блок Performance показывает
  **фактические** агрегаты из `positions` (источник правды), без
  пост-hoc интерпретации. Окно `all` (since start) — нет cherry-picking
  по датам.
- `.cursor/rules/api-docs.mdc`: правка не трогает cTrader / DeepSeek
  reconnect/heartbeat/rate-limit параметры.

### Acceptance criteria

- Через **≥30 closed live trades после deploy** (target: 2-3 недели
  при текущем темпе) — сравнить:
  - **avg `duration_minutes` open→close** для symbol'ов с присутствующим
    history. Гипотеза: LLM меньше передумывает на noise, средняя
    длительность вырастет с текущих ~10-30 мин в сторону положенного
    holding period setup'а.
  - **per-symbol WR / sum_pnl trend**. Не должен УХУДШИТЬСЯ
    statistically significantly (если WR упал на ≥10% с p<0.05 при
    n≥30 — false alarm, откатить через `git revert`).
- Если результат не улучшится за n=30 trades **и** не ухудшится → C
  не помог, но и не вреден. Тогда обсудить вариант E (review hold-by-
  default first 30 min) как дополнение, не замену.

### Откат

`git revert <commit_hash>` — никаких env-флагов / kv_state-настроек.
Default параметров prompt'а: оба `None` → polite no-op (старый v1.0
layout). Если в production обнаружится регрессия — однострочный
revert + redeploy `fx-ai-trader` контейнера.

### Token budget impact

- Performance block: ~150 токенов (3 строки на 3 символа).
- Recent trades (10): ~1100 токенов (компактный layout с clamp 180).
- **Full cycle:** +1.3k токенов (текущий ~4-6k → ~5-7k). DeepSeek
  cache hit пока ~30-35% (см. ai_arena cache stats) → cost impact
  минимален.
- **Review cycle:** +150 токенов (только performance).

### Точки входа (шпаргалка для будущих правок)

| Что | Файл |
|-----|------|
| `get_pnl_by_symbol` / `get_recent_closed_trades` | `src/fx_ai_trader/state/db.py` |
| Форматтеры + signature update | `src/fx_ai_trader/llm/prompts.py` |
| SYSTEM_PROMPT SELF-REFLECTION step | `src/fx_ai_trader/llm/prompts.py` |
| Интеграция в `_run_full_cycle` / `_run_review_cycle` | `src/fx_ai_trader/app/main.py` |
| Тесты | `tests/test_fx_ai_trader_self_reflection.py` |
| Эта запись | `BUILDLOG_AI_FX_TRADER.md` |
| План в чате | `.cursor/plans/fx-ai-trader_self-reflection_*.plan.md` |

**Файлы:** `src/fx_ai_trader/state/db.py`,
`src/fx_ai_trader/llm/prompts.py`, `src/fx_ai_trader/app/main.py`,
`tests/test_fx_ai_trader_self_reflection.py`,
`BUILDLOG_AI_FX_TRADER.md`.

---

## 2026-05-25 — fix(executor schema): clamp длинного `reason`/`title_snippet` через BeforeValidator вместо reject

`коммит при deploy`

### Симптом

В live-логе fx_ai_trader 2026-05-25 10:44:49 UTC:

```
[ERROR] fx_ai_trader: Parse error: schema validation error:
[{'type': 'string_too_long', 'loc': ('reason',),
  'msg': 'String should have at most 300 characters',
  'input': "No high-conviction setup across commodities. Oil's Iran deal
           unwind is clear macro driver but price is near oversold lower BB
           and uncertainty remains moderate. Gold lacks fresh real-yield/DXY
           catalyst. NatGas bearish storage+weather but oversold and no
           catalyst for entry. Wait for cleaner confluence.",
  'ctx': {'max_length': 300}}]
```

LLM прислал валидное `hold`-решение (325 символов в `reason`), но pydantic
schema validation с `Field(max_length=300)` отвергла **всё** decision-block.
Парсер вернул error-string → executor пропустил цикл → решение потеряно.

### Причина

Жёсткий `max_length=300` на полях `OpenAction.reason`, `CloseAction.reason`,
`HoldAction.reason` и `SentimentItem.title_snippet` (200). При этом ниже в
коде (`apply_action`, строка 531):
```python
reason = m.reason[:300]
```
— то есть лимит 300 нужен для **бюджета хранения в БД**, а не для
бизнес-валидации решения. Если LLM написал длиннее — мы должны обрезать,
а не выбрасывать сигнал.

В файле уже был тот же паттерн для unit-float'ов (`_coerce_unit`,
`_coerce_signed_unit` через `BeforeValidator + Field(ge/le)`): если LLM
прислал out-of-range — clamp, не reject. Это рекомендованный Pydantic
подход для LLM-output:
- Pydantic blog «Minimize LLM Hallucinations with Pydantic Validators»
  (<https://blog.pydantic.dev/blog/2024/01/18/llm-validation/>)
- Instructor «Validation & Retry»
  (<https://python.useinstructor.com/learning/validation/>)

### Решение

Добавлен factory `_coerce_capped_str(max_len)` → `BeforeValidator`,
который усекает строку **до** проверки `Field(max_length=...)`, поэтому
constraint всегда проходит. Два новых типа:

```python
ClampedReason = Annotated[
    str, BeforeValidator(_coerce_capped_str(300)), Field(max_length=300)
]
ClampedTitleSnippet = Annotated[
    str, BeforeValidator(_coerce_capped_str(200)), Field(max_length=200)
]
```

Заменены поля в `OpenAction` / `CloseAction` / `HoldAction.reason` и
`SentimentItem.title_snippet`. `m.reason[:300]` в `apply_action`
остаётся идемпотентным (уже усечённую строку повторно `[:300]` — no-op).

### Тесты

`tests/test_fx_ai_trader.py`:
- `test_hold_long_reason_is_clamped_not_rejected` — repro точного
  325-char reason из live-лога; ожидаем `ParsedAction` + `len(reason)==300`.
- `test_open_long_reason_is_clamped_not_rejected` — 500-char для
  `OpenAction`.

Все 80 тестов fx_ai_trader зелёные.

### Что НЕ изменилось

- Лимит 300 символов на хранение `reason` сохранён (схема + clamp в
  apply_action).
- Торговая логика (SL/TP / volume / uncertainty gate / killswitch /
  review_mode) — не тронута. Это исключительно фикс robustness парсера.
- Поведение для коротких `reason` (<300) — идентично.

**Файлы:** `src/fx_ai_trader/trading/executor.py`,
`tests/test_fx_ai_trader.py`, `BUILDLOG_AI_FX_TRADER.md`.

---

## 2026-05-22 — feat(cross-contamination fix): per-symbol macro routing + word-boundary classifier + exclude rules

`коммит при deploy`

### Триггер: гипотеза пользователя «ИИ путает данные между ресурсами»

Запрос: «возможно при принятии решений ИИ путает данные между ресурсами,
то есть золото с газом и нефтью и наоборот».

Diagnostic в 3 измерениях:
1. **Структура prompt** — данные price/indicators изолированы через
   явные `[XAUUSD]` / `[BZ=F]` / `[NG=F]` теги. **Cross-pollution
   архитектурно невозможна** ✅.
2. **News filtering** — на 12 RSS items нашёл **17% контаминации**:
   - `[XAUUSD, BZ=F]` «Geologic Hydrogen Could Produce Clean Fuel»
   - `[BZ=F, NG=F]` «India Explores Alternative Energy Amid Oil Supply
     Shock»
3. **LLM reasoning на 25 live-трейдах** — атрибуция драйверов
   **корректная** (gold = yields/Fed/DXY, oil = Hormuz/OPEC, gas =
   LNG/NOAA). **0/25 trades** с реальной путаницей ✅.

Однако обнаружены **три механические утечки**:

1. **`eia` в OIL_KEYWORDS** — substring ловил «EIA: Natural Gas
   Storage Report» → попадало в [BZ=F] bucket.
2. **`goldman` substring** — «gold» substring в "Goldman Sachs"
   ловил **каждую** Goldman oil/OPEC-news в [XAUUSD] bucket. Это
   огромная утечка (Goldman публикует oil/macro ежедневно).
3. **`biogas` / `boiler` substring** — потенциальные false positives
   через "gas" в "biogas" и "oil" в "boiler".

### Изменения

#### 1. Word-boundary classifier (`src/fx_ai_trader/news/rss.py`)

Новая функция `_matches_keyword(keyword, text)`:
- Multi-word phrase (содержит пробел) → substring match как раньше
- Single word → `\b{keyword}\b` regex (word-boundary)

Результат:
- `"gold"` НЕ матчит «Goldman»/«Goldilocks»/«Marigold»
- `"oil"` НЕ матчит «Boiler»/«Toiletries»
- `"gas"` НЕ матчит «Biogas»/«Gasoline» (gasoline отдельный keyword
  если нужен)
- `"natural gas"`, `"strait of hormuz"` — fraz, substring как раньше

#### 2. Узкий EIA keyword в OIL (`OIL_KEYWORDS` в rss.py)

Удалён голый `"eia"` и `"api report"`. Заменены на конкретные фразы:
- `"eia crude"`, `"eia weekly petroleum"`
- `"crude inventory"`, `"crude inventories"`, `"crude stocks"`
- `"api crude"`

Гарантирует что EIA gas-news НЕ попадёт в OIL bucket даже без
exclude-rule.

#### 3. Exclude-keywords per symbol (`SYMBOL_EXCLUDE_KEYWORDS`)

Двухэтапная фильтрация (INCLUDE + EXCLUDE):

- **GOLD_EXCLUDE**: natural gas / lng / henry hub / crude oil /
  brent / wti / opec. Если в text есть gas/oil термины — не в gold.
- **OIL_EXCLUDE**: natural gas storage / ng storage / henry hub /
  lng cargo / lng feedgas / feedgas / noaa / cpc outlook / hdd / cdd /
  heating degree / cooling degree. Weather и gas-specific phrases
  отсекают gas news из oil bucket.
- **GAS_EXCLUDE**: crude oil / crude inventory / brent / wti /
  petroleum / opec / strait of hormuz / houthi / red sea. Oil-specific
  phrases отсекают oil news из gas bucket.

#### 4. Per-symbol macro routing (`news/eia.py`, `trading/context.py`)

`format_eia_by_symbol(snap)` возвращает `dict[symbol, block_text]`:
- `BZ=F` ← EIA Weekly Petroleum (crude stocks, refinery, SPR)
- `NG=F` ← EIA Weekly NG storage + STEO forecast (HH price, production,
  exports) + NOAA discussion (HDD/CDD outlook)
- `XAUUSD` ← пусто (нет EIA-релевантного macro для gold)

`MarketContext.macro_per_symbol: dict[str, str]` заменяет глобальные
`eia_block_text` / `noaa_block_text`. `format_context_for_prompt`
печатает новую секцию:

```
=== PER-SYMBOL MACRO CONTEXT (each block ONLY applies to the
labelled symbol — do NOT cross-apply) ===

[BZ=F] macro:
EIA Weekly Petroleum (Wednesday update):
Crude oil stocks: 445013k barrels (-7863k vs prev week) ...

[NG=F] macro:
EIA Weekly Natural Gas (Thursday update) + STEO 18m forecast:
Working gas in storage (Lower 48): 2290 Bcf (+85 Bcf build) ...
Henry Hub spot price forecast ($/mcf, monthly): 2026-06=3.04 ...
NOAA CPC 6-10 / 8-14 day Prognostic Discussion (fetched 2026-05-22 ...):
...
```

LLM физически **не может** перепутать какой EIA-блок к какому
инструменту — каждый помечен `[SYMBOL]` тегом.

#### 5. Логирование (`app/main.py`)

`LLM call (full): positions=X news_total=Y macro_symbols=BZ=F,NG=F` —
заменено старое `eia=YES noaa=YES`. Видно какие именно инструменты
получили macro в данном цикле.

### Тесты (`tests/test_fx_ai_trader.py`)

Добавлены **10 новых тестов**:
- `test_news_no_cross_contamination_eia_gas_to_oil` — gas EIA не в OIL
- `test_news_oil_news_blocked_from_gas_bucket` — oil не в NG
- `test_news_gold_news_pure` / `test_news_pure_oil_news_isolated` /
  `test_news_pure_gas_news_isolated` — изоляция чистых сигналов
- `test_news_word_boundary_goldman_not_gold` — Goldman ≠ gold (но
  legit gold-news через other keywords проходят)
- `test_news_word_boundary_biogas_not_gas` — biogas ≠ gas
- `test_news_word_boundary_boiler_not_oil` — boiler ≠ oil
- `test_format_eia_by_symbol_routes_petroleum_to_oil` — Petroleum
  только в BZ=F
- `test_format_eia_by_symbol_routes_ng_to_gas_only` — NG storage +
  STEO только в NG=F

Полный suite: **997 tests passed** (+10 новых).

### Live-проверка classifier (8 тестов, все ок)

```
[oil]  Goldman: oil stockpiles falling, Hormuz at 5%        → BZ=F
[gold] Goldman Sachs cuts gold price forecast for 2026      → XAUUSD
[gold] Goldman: gold rally extends as DXY weakens           → XAUUSD
[gas]  EIA: Natural Gas Storage Report shows +85 Bcf build  → NG=F
[oil]  OPEC+ ramps oil output as Hormuz tensions ease       → BZ=F
[gold] Fed hawkish, real yields surge, dollar climbs        → XAUUSD
[gas]  NOAA 6-10 day outlook above-normal temps, HDD declining → NG=F
[gas]  Henry Hub spot, Marcellus production at record       → NG=F
[none] Biogas plant opens in Texas                          → []
[oil]  Boiler manufacturer reports record quarter           → []
       (boiler ≠ oil substring; нет других oil keywords → пусто)
```

### Что НЕ менялось

- **Промпт стратегии** не трогали
- **Sentiment / uncertainty gates** не трогали
- **Per-symbol risk limits NG=F** (max_lot=0.25 + max_pos=1) — остались
- **EIA / NOAA провайдеры**: без изменений, только новый format-функция

### Файлы

- `src/fx_ai_trader/news/rss.py`: word-boundary classifier, узкий
  EIA keyword, SYMBOL_EXCLUDE_KEYWORDS, GOLD/OIL/GAS_EXCLUDE
- `src/fx_ai_trader/news/eia.py`: `format_eia_by_symbol()` функция,
  `format_eia_snapshot` deprecated
- `src/fx_ai_trader/trading/context.py`: `macro_per_symbol` поле в
  `MarketContext`, per-symbol routing в `collect_market_context`,
  «PER-SYMBOL MACRO CONTEXT» секция в `format_context_for_prompt`
- `src/fx_ai_trader/app/main.py`: новый формат логирования
- `tests/test_fx_ai_trader.py`: 10 новых тестов

---

## 2026-05-21 — feat(NG): NOAA + EIA STEO + расширенные RSS keywords + per-symbol limits для NG=F (v1.2 NG enhancement)

`коммит при deploy`

### Триггер: 11 NG=F live-трейдов, WR 18%, net −$29.01

С момента включения NG=F (2026-05-18 ночь, BUILDLOG ниже) fx-ai-trader
сделал **11 live-сделок по NG**, **все BUY**. Wins 2 (+$2.60), losses 9
(−$31.61). **9 убыточных** открыты подряд 20 мая в одной торговой
сессии — mean-reversion long на трендовом downtrend'е (все exit reasons
вариации «MACD bearish, price below EMA20/lower BB, mean-reversion
invalidated»). По выборке n=11 ещё не дотягиваем до порога
`sample-size.mdc` (≥100 сделок) → инструмент **не отключаем**.

### Корневая причина (нашёл аудитом): EIA API key отсутствовал в .env

`grep eia /root/fx-pro-bot/.env → no eia line`. С момента деплоя
fx-ai-trader 2026-05-13 переменная `AI_FX_TRADER_EIA_API_KEY` **никогда
не была установлена**. В логах при старте: `EIA: OFF`. То есть весь
70-строчный NG framework в промпте (EIA Weekly Storage, Henry Hub
forecast, dry production, LNG exports — «follow религиозно») —
**LLM никогда этих данных не видел**. Бот работал только на RSS news +
цены + sentiment.

Файлы и интернет-источники (Aegis Factor Matrix, NatGas Central, NGI
LNG Data Suite, EIA STEO) подтверждают: для NG=F intraday минимум 5
источников необходимы — Storage / Weather (HDD/CDD) / LNG feedgas /
Production / Rig count. У нас работал только Storage (RSS news), и то
без EIA-числовой подписки.

### Изменения

#### 1. EIA API key добавлен в .env на VPS (`/root/fx-pro-bot/.env`)

```
AI_FX_TRADER_EIA_API_KEY=<key>
```

После перезапуска контейнера EIA snapshot activates → LLM получает
Weekly Petroleum + Weekly NG Storage в каждом full cycle (15 мин).

#### 2. EIA модуль расширен: STEO forecast (`src/fx_ai_trader/news/eia.py`)

Добавлены 3 STEO-серии (monthly, 18-month forward):
- `NGHHMCF`: Henry Hub spot price forecast ($/mcf)
- `NGPRPUS`: US dry natural gas production (Bcf/d)
- `NGEXPUS`: US natural gas total gross exports (Bcf/d, LNG+pipeline)

Endpoint `/v2/steo/data` с фильтром `start=<current YYYY-MM>` +
`sort asc + length=6` даёт 6 ближайших месяцев forecast. Best-effort:
каждая серия независимо ловит исключения. `format_eia_snapshot`
расширен новым блоком «EIA STEO forecast».

**Live-проверка (2026-05-21 08:10 UTC):**
- HH price forecast: $2.90 (май) → $3.22 (октябрь) — лёгкий up-trend
- Production: 110.2 → 111.3 Bcf/d (расширение → bearish overhang)
- Exports: 26.9 → 26.2 Bcf/d (~stable, лёгкое снижение)
- Storage actual: 2290 Bcf (+85 Bcf build) on 2026-05-08

#### 3. Новый модуль NOAA CPC outlook (`src/fx_ai_trader/news/weather.py`)

`NoaaOutlookProvider` тянет prognostic discussion с
`https://www.cpc.ncep.noaa.gov/products/predictions/6-10_day/fxus06.html`.
HTML-strip + extract по маркерам «Prognostic Discussion» …
«FORECAST CONFIDENCE FOR THE 8-14 DAY PERIOD». Cache TTL 6 часов
(CPC publishes 15:00-16:00 ET ежедневно). Защищено try/except — на
fail возвращает кэш или None. Без API-ключа.

**Live-проверка:** discussion ~6190 chars, валиден формат. Сегодня:
above-normal temps Upper MS Valley/Great Lakes >80% confidence,
below-normal West, ridging east-central Canada.

#### 4. Расширение GAS_KEYWORDS (`src/fx_ai_trader/news/rss.py`)

С 18 keywords до 60+. Добавлены:
- LNG terminals: Cameron LNG, Cove Point, Elba Island, Calcasieu Pass,
  Plaquemines, Rio Grande
- Basins: Marcellus, Appalachia, Haynesville, Permian, Eagle Ford,
  Barnett, Utica
- Basis hubs: Waha, Algonquin, Transco zone, Michcon
- Storage cycle: injection season, withdrawal season
- Weather: arctic blast, warm/mild winter, CPC outlook, 6-10 day
- TTF/JKM: dutch ttf, european gas, asia jkm
- Production: associated gas, dry gas production
- Industry analysts: John Kemp, JKempEnergy, S&P Global natgas

#### 5. Per-symbol limits для NG=F (`config/settings.py`, `safety/killswitch.py`, `trading/executor.py`)

По правилу `sample-size.mdc`: «Если риск критичный — уменьшить размер
позиции (position_size), не отключать». Без подгонки thresholds,
без отключения инструмента.

- `per_symbol_max_lot_size: dict[str, float] = {"NG=F": 0.25}`
  (override общего 0.50 для NG)
- `per_symbol_max_positions: dict[str, int] = {"NG=F": 1}`
  (override общего 3 для NG — одна позиция на инструмент в каждый
  момент, защита от revenge-trading серий как 9 проигрышей подряд 20.05)
- Хелперы `settings.effective_max_lot_size(symbol)` и
  `effective_max_positions_per_symbol(symbol)` — fallback к общим
  лимитам если нет override
- KillSwitch использует `get_max_positions_for(symbol)` — в reason
  пишет «(per-symbol override; default 3)» для аудита
- Executor применяет `effective_max_lot_size` в clamp-секции с
  отдельным INFO-логом «FX-AI clamp (per-symbol NG=F): ...»

XAUUSD / BZ=F не задеты.

#### 6. Wiring data sources в context (`trading/context.py`, `app/main.py`)

- `MarketContext` получил поле `noaa_block_text: str | None`
- `collect_market_context` принимает `noaa_provider` и вызывает только
  если `NG=F in symbols` (экономия HTTP)
- EIA вызывается теперь и для NG=F (раньше только BZ=F/CL=F)
- `_run_full_cycle` логирует `noaa=YES/no` рядом с `eia=YES/no`
- `format_context_for_prompt` добавляет два секции в prompt_user:
  «=== EIA MACRO (oil + gas: Weekly + STEO forecast) ===»
  «=== NOAA CPC WEATHER OUTLOOK (key NG=F driver: HDD/CDD demand) ===»

### Что НЕ менялось (по правилам)

- **Промпт стратегии не трогали** — NG framework уже подробный, без
  изменения thresholds (по `no-data-fitting.mdc`).
- **Sentiment/uncertainty gates не трогали** — `n=11` << 100 (по
  `sample-size.mdc`).
- **NG=F не отключали** — по тому же правилу. Только уменьшение
  размера и количества (явное разрешение в правиле).
- **Advisor / fx-ai-trader other strategies** — не задеты.

### Тесты (`tests/test_fx_ai_trader.py`)

Добавлены 11 новых:
- `TestNoaaOutlookProvider`: parse HTML с/без discussion, format с/без
  snapshot
- `TestEiaSteoFormatting`: STEO block presence, fallback, combine с
  storage
- `TestPerSymbolLimits`: effective_max_lot_size/positions для всех
  трёх инструментов; killswitch NG=F блокирует после 1 позиции;
  XAUUSD/BZ=F не задеты

Полный suite: **928 tests passed, 0 failed**.

### Ожидание после деплоя

LLM в каждом full-cycle теперь видит:
1. EIA Weekly Petroleum (oil block, существующее)
2. EIA Weekly NG Storage (Bcf + week-on-week change) — **впервые**
3. EIA STEO 6-month forecast (HH price + production + exports) — **впервые**
4. NOAA CPC 6-10 / 8-14 day prognostic discussion — **впервые**
5. RSS news по 60+ gas-keywords (раньше 18)
6. По NG: размер ≤0.25 lot, ≤1 одновременная позиция

Stat-baseline: продолжаем сбор статистики до n≥100 NG-трейдов перед
любыми изменениями торговой логики. Решение об отключении или сдвиге
порогов — **только** при достижении выборки + p-value <0.05.

### Файлы

- `src/fx_ai_trader/news/weather.py` (NEW)
- `src/fx_ai_trader/news/eia.py` (STEO support)
- `src/fx_ai_trader/news/rss.py` (расширение keywords)
- `src/fx_ai_trader/config/settings.py` (per-symbol overrides)
- `src/fx_ai_trader/safety/killswitch.py` (get_max_positions_for)
- `src/fx_ai_trader/trading/executor.py` (effective_max_lot_size)
- `src/fx_ai_trader/trading/context.py` (NOAA + EIA NG wiring)
- `src/fx_ai_trader/app/main.py` (NoaaOutlookProvider init)
- `tests/test_fx_ai_trader.py` (+11 тестов)
- VPS `.env`: `AI_FX_TRADER_EIA_API_KEY=<set>`

### Известные ограничения (вне scope)

- **Refinery utilization series возвращает 16642%** — pre-existing bug
  в `_SERIES_REFINERY_UTIL = "PET.WGIRIUS2.W"` (это не utilization rate,
  а gross inputs barrels per day). Не задевает NG-функционал, исправим
  отдельно когда понадобится для oil.
- **HDDPUS/CDDPUS не доступны через `/v2/steo/data`** — это weather
  sub-endpoint EIA, требует другой path. NOAA discussion полностью
  компенсирует — текстовое описание HDD/CDD prognosis на 6-10/8-14
  дней по регионам US.

---

## 2026-05-20 (день, 06:30 UTC) — fix(PnL): broker NET вместо gross в БД + backfill + post-mortem 4 убыточных + удаление fx-ai-trend + возврат Advisor

`коммит при deploy`

### 1. Bug-fix: realized_pnl_usd хранит broker NET, не idealized gross

**Симптом:** локальная БД `fx_ai_trader.sqlite` за 18-20 мая показывала
`net +$2.72` по 7 closed live-трейдам, а cTrader app History — `−$7.98`.
Разница **−$10.70 (≈ −$1.53/trade)** — это swap (overnight) +
commissions, которые `_calc_pnl_usd` не учитывает.

**Корень:** `_apply_close` в LIVE-режиме после успешного
`adapter.close_position()` писал `realized_pnl_usd = _calc_pnl_usd(...)`
— gross на основе entry/exit/lots/pip_value. Broker фактически списывает
NET = `cpd.grossProfit + cpd.swap + cpd.commission` через
`ProtoOAClosePositionDetail`.

**Фикс** (`src/fx_ai_trader/trading/executor.py:336-465`):
- После успешного broker-close делаем до 3 попыток с `time.sleep(1.0)`
  достать closing deal через `adapter.get_closing_deal_for_position(
  broker_pid, lookback_hours=1)`. Spotware фиксирует deal с latency
  ~0.5–2с — sleep+retry это покрывает.
- Если deal получен → `realized_pnl_usd = gross + swap + commission`,
  `exit_price = deal.exit_price`. Summary в логах включает breakdown
  (`pnl=$X (net: gross=$A + swap=$B + comm=$C)`).
- Если 3 попытки не дали deal → fallback на idealized gross + WARNING
  в логах с broker_pid, чтобы backfill-скриптом догнать позже.

POSITION_NOT_FOUND path (broker_auto recovery) уже использует broker
NET с 2026-05-13 — не трогаем.

Paper-mode (`is_paper=True`) и `paper_reconcile.py` — оставляем gross
через `_calc_pnl_usd`. Paper не имеет broker swap/commission по природе
(симуляция без слипажа).

### 2. Backfill: пересчёт исторических PnL

Скрипт `scripts/fx_ai_backfill_net_pnl.py`:
- Берёт все closed live-позиции с `broker_position_id` из БД.
- Для каждой запрашивает closing deal через
  `get_closing_deal_for_position` (lookback по умолчанию 30 дней).
- Сравнивает `realized_pnl_usd` с broker NET, печатает diff.
- С флагом `--apply`: UPDATE + пересборка `daily_pnl` с нуля.
- Без флага: dry-run.

Запуск:
```bash
docker exec fx-pro-bot-fx-ai-trader-1 python /tmp/backfill_net_pnl.py        # dry-run
docker exec fx-pro-bot-fx-ai-trader-1 python /tmp/backfill_net_pnl.py --apply  # commit
```

### 3. Post-mortem 4 убыточных трейдов с 18 мая

Read-only анализ из `decisions` table (`parsed_action.reason` +
`sentiment_json`). Sample size n=4 << 100 → **никаких изменений
порогов не делаем** (правило `sample-size.mdc`).

| pos | open thesis | uncertainty | NET | разбор |
|---|---|---|---|---|
| id=7 BZ=F SELL | China refiners cut + real-yield USD strength + 4H break ниже EMA20 | 0.2 | −$10.48 | **Не LLM**. Жертва label-guard incident (commit 6b3665e уже откачен). LLM был прав, Brent действительно упал к SL. |
| id=10 NG=F BUY | Australian LNG strike + 4H uptrend + pullback к mid-BB | 0.38 | −$2.51 | **Mixed thesis**: trend-follow + mean-reversion в одной сделке. Чистый exit в безубыток, минус только от overnight swap −$1.11. |
| id=11 BZ=F SELL | Trump паузнул Iran strike → geopolitical premium decay | 0.2 | −$7.47 | **SL слишком узкий**: 10 центов от entry (entry ~106.93, SL 107.10). Brent intraday range ~$1-2, шумовое движение легко сбило. Концепция верная, **risk sizing провал**. |
| id=12 BZ=F BUY | Massive API crude draw + Iran risk premium + pullback к 4H EMA20 | **0.45** | −$2.99 | **High uncertainty open** (0.45 — гранична). Через 2h тренд развернулся, LLM закрыл по invalidation. Exit правильный, but entry questionable. |

**Чистый LLM-fault counter:**
- "Невинная жертва нашего бага": id=7 (0 LLM trades)
- "Mixed thesis": id=10 (0.5)
- "SL too tight": id=11 (1.0 — concrete bug)
- "High uncertainty open": id=12 (0.5)
- Итого: **2.0 LLM-faults из 4 трейдов**

**Общий паттерн:** Все 4 убыточных — BZ=F или NG=F. Все 3 XAUUSD-трейда
(id=6, id=8) — wins. Это **наблюдение**, не сигнал что-то менять
(n слишком мал, на разных днях / разных режимах гипотеза перевернётся).

### 4. Удаление fx-ai-trend, возврат Advisor

**fx-ai-trend** (trend-follower LLM):
- За 14ч жизни 3 paper-трейда подряд, все убыточные (−$16.05 paper).
- Потом 30+ часов тишины — Donchian/Turtle-фильтры не находили clean
  breakouts. Поведение **корректное по research** (Faith/Covel: 50-70%
  трейдов trend-следования убыточны на тестах с маленьким горизонтом),
  но для пользователя — бесполезен.
- **Удалён полностью**: `src/fx_ai_trend/`, `Dockerfile.fx-ai-trend`,
  `tests/test_fx_ai_trend.py`, service block в `docker-compose.yml`,
  entrypoint в `pyproject.toml`. БД `fx_ai_trend.sqlite` оставлена на
  диске VPS как реликвия эксперимента (3 paper-trades + decisions
  audit), удалим вручную при следующей подаче.

**Advisor (fx_pro_bot)** возвращён:
- Раскомментирован service block в `docker-compose.yml`.
- Старт через `docker compose up -d --build advisor` на VPS.
- БД `advisor_stats.sqlite` цела с 2026-05-18 (backup в
  `advisor_stats.sqlite.backup-stop-20260518T114956Z`).
- При старте Advisor сделает reconcile позиций — stale `ef25d270`
  (GC=F long, status=open в БД, но на брокере закрылась 18 мая 12:58
  с +$39.80 net) синкнется автоматически через
  `reconcile_broker_positions`-механизм Advisor'а.

**Файлы:**
- `src/fx_ai_trader/trading/executor.py` — broker NET path + retry +
  fallback с WARNING (новый код 336-465)
- `scripts/fx_ai_backfill_net_pnl.py` (NEW)
- `tests/test_fx_ai_trader.py` — новый тест
  `test_llm_close_stores_broker_net_not_idealized_gross`
- `docker-compose.yml` — advisor uncomment + fx-ai-trend remove
- `pyproject.toml` — убран fx-ai-trend entrypoint и package
- `src/fx_ai_trend/`, `Dockerfile.fx-ai-trend`, `tests/test_fx_ai_trend.py` — DELETED
- `scripts/fx_ai_broker_history_audit.py` — убран match по fx_ai_trend.sqlite

**Тесты:** 910 passed (55 в test_fx_ai_trader.py, +1 новый).

**Источники:**
- cTrader Open API ProtoOAClosePositionDetail: `grossProfit` + `swap` +
  `commission` ([docs](https://help.ctrader.com/open-api/model-messages/#protooaclosepositiondetail))
- FxPro Trading Conditions: overnight swap rollover на 5pm NY
- BUILDLOG 2026-05-18 (label-guard incident, контекст для id=7)
- BUILDLOG 2026-05-13 (broker_auto NET path, прецедент использования
  get_closing_deal_for_position)

---

## 2026-05-20 (утро, 04:30 UTC) — broker-truth audit + находка: БД хранит GROSS, брокер списывает NET

`commit при deploy — diagnostic + buildlog`

**Контекст.** Пользователь обратил внимание, что после добавления газа
(NG=F) и запуска Trend-follower бота Discretionary стал торговать «в
минус», но локальная БД показывала **net +$2.72** за 7 трейдов с 18 мая.
Заподозрили расхождение.

**Что сделал.** Написал `scripts/fx_ai_broker_history_audit.py` — он
тянет `ProtoOAGetDealListReq` за окно (с 2026-05-18 00:00 UTC) +
`ProtoOAReconcileReq` (открытые сейчас), сшивает каждый
`positionId` с тремя локальными БД (`fx_ai_trader`, `fx_ai_trend`,
`advisor`) и печатает **NET** = `gross + swap + commission`
(broker-truth, как в cTrader app History).

**Находки:**

1. **БД fx-ai-trader хранит GROSS, а брокер списывает NET.**
   - DB sum 7 трейдов: **+$2.72** (gross без вычета swap/comm).
   - Broker app sum: **−$7.98 net** (что реально списано со счёта).
   - Delta = **−$10.70** в 7 трейдах = swap/commission, в среднем **−$1.53/trade**.
   - Источник: `realized_pnl_usd` в `positions` (схема `db.py:66`) пишется
     из `_calc_pnl_usd()` (формула gross на основе entry/exit/lots/pip_value),
     либо из `broker.grossProfit` без вычета `swap` и `commission`.

2. **Орфанов на брокере нет.** `ProtoOAReconcileRes.position = []`.
   Та запись Advisor (`ef25d270` GC=F long, status=open в БД advisor)
   на брокере закрылась 2026-05-18 12:58 с **+$39.80 net**. Локальная
   advisor.sqlite stale (контейнер выключен с 11:50 UTC того же дня).

3. **XAUUSD ≡ GC=F на FxPro** (`symbolId=41`). Один и тот же
   gold-инструмент под двумя internal именами. Advisor торговал
   `GC=F`, fx-ai-trader — `XAUUSD`. Это **одинаковая экспозиция**,
   разные labels позволяли управлять независимо.

4. **Итог broker-truth с 18 мая (sample size 9 deals):**
   - Advisor: 2 trades, +$32.50 net (одна большая сделка +$39.80)
   - fx-ai-trader: 7 trades, **−$7.98 net** (W=3, L=4)
   - Σ = **+$24.52** ✓ (== Realised P&L в cTrader app)
   - Без −$10.48 label-guard incident → fx-ai-trader **+$2.50 net**
   - Без overnight swap −$1.11 (id=10 NG=F): **+$3.61 net**

**По правилу `sample-size.mdc` (n=7 < 100) делать вывод что Discretionary
"стал торговать в минус из-за добавления газа" нельзя.** Продолжаем
наблюдение. Менять стратегию по такому объёму запрещено.

**Что НЕ делал:**
- Не менял `_calc_pnl_usd` (gross-формула корректна для своей цели —
  оценки theoretical PnL вне зависимости от broker fee structure).
- Не правил БД-схему (рефакторинг хранения net потребует backfill
  через `ProtoOADealListReq` для всей истории + миграция, отдельная
  задача).

**Что выявлено к решению (TODO):**

a. Локальная advisor.sqlite stale — `ef25d270` показана open хотя
   на брокере closed. Не критично (Advisor выключен), но если будем
   делать сравнение Advisor pre/post — нужен one-shot reconcile-сcript.

b. `broker_reconcile.py` для **non-broker-auto** закрытий (когда LLM
   сам решил выйти) сейчас пишет `gross` в `realized_pnl_usd`. Должен
   писать `net` = `cpd.grossProfit + cpd.swap + cpd.commission` (точно
   так же, как в audit-скрипте). Иначе все будущие dashboard'ы будут
   врать.

c. Прометей/dashboards в БД считают W/L и avg по `realized_pnl_usd`
   → текущая метрика **оптимистична**. После фикса (b) → реальные
   цифры.

**Файлы:**
- `scripts/fx_ai_broker_history_audit.py` (NEW, read-only diagnostic)
- `BUILDLOG_AI_FX_TRADER.md` (эта запись)

**Источники:**
- cTrader Open API: `ProtoOAGetDealListReq` / `ProtoOAClosePositionDetail`
  — поля `grossProfit`, `swap`, `commission` (см. `compare_stats.py:50-56`
  и `src/fx_pro_bot/trading/executor.py:599-627`).
- FxPro Trading Conditions: gold/oil без commission, swap считается
  на rollover (5pm NY) — объясняет −$1.11 swap у NG=F overnight position.

---

## 2026-05-18 (вечер, 14:50 UTC) — CRITICAL FIX: откатан label guard, восстановлены 2 потерянные позиции

`коммит при deploy — INCIDENT FIX`

**Симптом — 2 позиции БРОШЕНЫ ботом за 1.5 часа:**

| id | symbol | broker_pid | opened | "closed" (false) | реальный статус |
|---|---|---|---|---|---|
| 7 | BZ=F SELL | 150837215 | 13:20:19 | 14:01:36 (label_guard_orphan) | broker auto-close по SL ($106.059, PnL −$10.48) |
| 8 | XAUUSD SELL | 150839089 | 14:33:16 | 14:38:37 (label_guard_orphan) | **ЖИВАЯ на broker'е**, ботом не управлялась 16+ минут |

**Корень — мой собственный фикс**. Belt-and-suspenders label guard,
добавленный 2026-05-18 (commit `6b3665e`) для «multi-bot isolation»,
выглядел так:

```python
active_ours = adapter.get_open_positions()
if pos.broker_position_id not in {p.position_id for p in active_ours}:
    # Считаем что pid принадлежит "другому боту" → не дёргаем close,
    # маркируем locally как label_guard_orphan
```

`adapter.get_open_positions()` под капотом дёргает
`ProtoOAReconcileReq`, который из-за **Spotware per-session caching
bug** (обнаружен в этой же сессии при разборе grace-period fix'а
13:50 UTC) systematically НЕ ОТДАЁТ свежие positionIds через
долгоживущую TCP-сессию бота. Контрольный эксперимент с короткоживущей
сессией (`fx_ai_dump_all_positions.py`) подтверждает: pid 150839089
жив, label `ai-fx-trader` корректный, но reconcile() через основную
сессию его не показывает.

Label guard принял этот глюк за «orphan другого бота» и **бросил**
обе активные позиции.

**Recovery (sequence):**

1. `scripts/fx_ai_recover_label_guard_orphans.py` — диагностика +
   рекавери:
   - id=7 BZ=F: `get_closing_deal_for_position(150837215)` → нашли
     deal_id=331969996, exit $106.059, gross=-$10.48 →
     `close_reason='broker_auto_recovered'`, `realized_pnl_usd=-$10.48`.
   - id=8 XAUUSD: `UPDATE positions SET closed_at=NULL, close_reason=
     NULL, exit_price=NULL, realized_pnl_usd=NULL WHERE id=8` →
     восстановлена как open.
2. Label guard **полностью удалён** из `_apply_close` (в обоих ботах:
   fx_ai_trader + fx_ai_trend).
3. Тест `test_label_guard_skips_close_for_orphan_broker_pid` удалён.
4. Rebuild + deploy.

**Почему cross-bot interference и без guard'а невозможна**:

| Слой | Где | Что защищает |
|---|---|---|
| OPEN | `place_market_order` | label=settings.order_label → broker сохраняет на позицию |
| LLM CONTEXT | `store.get_open_positions()` | LLM видит только записи из НАШЕЙ БД, где чужих физически нет |
| DB ISOLATION | `fx_ai_trader.sqlite` ≠ `fx_ai_trend.sqlite` | разные файлы, разные таблицы |
| _apply_close LOOKUP | `pos = next(p for p in db_positions if p.id == pos_id)` | LLM передаёт internal DB-id, не broker_pid |
| BROKER PID SOURCE | `pos.broker_position_id` из НАШЕЙ БД | гарантия что это наша позиция, записана при OPEN |
| broker_reconcile | `get_active_broker_position_ids` label-filtered | даже массовый reconcile не трогает чужие |

Чтобы fx_ai_trader смог закрыть позицию fx_ai_trend, должно случиться
ОДНОВРЕМЕННО: (а) `fx_ai_trader.sqlite` содержит запись с чужим
broker_pid, (б) LLM передаёт её internal-id. Сценарий физически
невозможен.

**Тесты.** 72/72 fx_ai_trader+fx_ai_trend зелёные после удаления
теста label_guard.

**Файлы:** `src/fx_ai_trader/trading/executor.py`,
`src/fx_ai_trend/trading/executor.py`, `tests/test_fx_ai_trader.py`,
`scripts/fx_ai_recover_label_guard_orphans.py` (recovery one-shot).

---

## 2026-05-18 (ночь, 13:50 UTC) — fix: broker_reconcile grace-period + log-level + fx_ai_trend rename consistency

`коммит при deploy`

**Симптом.** Свежеоткрытая позиция id=7 (BZ=F SELL, broker_pid=150837215,
opened 13:20:19 UTC) триггерила WARNING'и каждые 5 минут:

```
13:25:20 broker reconcile: позиция id=7 закрыта broker'ом сам — ищу closing deal
13:25:21 [WARNING] closing deal не найден за 48h — оставляю позицию open (manual review)
13:30:29 broker reconcile: позиция id=7 закрыта broker'ом сам — ищу closing deal
13:30:30 [WARNING] closing deal не найден ...
13:35:20 ... та же история
```

После ~15 минут (cycle 13:35+) ситуация саморазрешилась — позиция
появилась в `get_active_broker_position_ids()` set'е и WARNING'и
прекратились.

**Диагноз.** Spotware reconcile session-state latency: после
`ProtoOAExecutionEvent` (с позицией) `ProtoOAReconcileReq` через
**ту же** TCP-сессию иногда не видит свежий positionId до 10-15 минут.
Контрольный эксперимент: дамп позиций через **новую** сессию (через
`docker exec ... fx_ai_dump_all_positions.py`) показал pid 150837215
активным — то есть это per-session caching артефакт у Spotware,
не общая проблема broker'а.

Защита `closing_deal_not_found → keep open` сработала — позиция в БД
**не** закрылась. Бот корректно держал её через `positions=1` в каждом
review-цикле (`13:25 / 13:30 / 13:35: REVIEW APPLY: HOLD: Sell setup
intact`). Никакой реальной потери ни данных, ни PnL — только log-noise
и misleading wording ("закрыта broker'ом сам — ищу closing deal" звучит
тревожнее чем "проверяю гипотезу — не закрылась ли").

**Фикс (`src/fx_ai_trader/trading/broker_reconcile.py` + зеркально
`src/fx_ai_trend/trading/broker_reconcile.py`).**

1. **GRACE_PERIOD_SEC = 900** (15 мин). Позиции младше grace вообще
   пропускаются в reconcile-loop — Spotware всё равно еще catch-up'ит
   session-state, polling бесполезен.
2. **Conditional log level**: для свежих позиций (age < 24h) "deal not
   found" → `INFO` ("позиция жива у broker'а, оставляю open"); для
   старых (>24h) — `WARNING` (реальная аномалия).
3. **Misleading wording** убран: вместо "закрыта broker'ом сам" теперь
   просто "проверяю не закрыта ли broker'ом".

Trade-off: broker-auto SL/TP в первые 15 минут жизни позиции будет
обнаружен на следующем цикле (~3 review-cycle worst-case). Это
приемлемо. Если латентность когда-нибудь окажется >15 минут, поднимем
до 30 (или перейдём на event-stream listener).

**Bonus fix.** При bulk-rename `fx_ai_trader → fx_ai_trend` я пропустил
переименование класса `AiFxTraderStore` → `AiFxTrendStore` в
`src/fx_ai_trend/state/db.py:112` (и его импорты в 7 файлах). Класс
был **локальным** в `fx_ai_trend.state.db` (не импортировался из
`fx_ai_trader`), поэтому функционально fx_ai_trend работал
корректно — БД `fx_ai_trend.sqlite` создавалась изолированно. Но
имя класса вводило в заблуждение → потенциальный footgun при будущей
правке. Переименован.

**Тесты.** 73/73 fx_ai_trader + fx_ai_trend, 810/810 total — зелёные.
+ 2 новых теста: `test_grace_period_skips_fresh_positions`,
`test_grace_period_lets_through_aged_positions`. Старые тесты
`test_closes_broker_closed_position` и `test_uses_broker_net_pnl_not_local_calc`
адаптированы — backdate `opened_at` на 30 мин чтобы пройти grace.

**Файлы:** `src/fx_ai_trader/trading/broker_reconcile.py`,
`src/fx_ai_trend/trading/broker_reconcile.py`,
`src/fx_ai_trend/state/db.py`, `src/fx_ai_trend/app/main.py`,
`src/fx_ai_trend/safety/killswitch.py`, `src/fx_ai_trend/trading/{context,executor,paper_reconcile}.py`,
`tests/test_fx_ai_trader.py`.

---

## 2026-05-18 (ночь) — feat: belt-and-suspenders label guard в _apply_close (multi-bot isolation)

`коммит при deploy`

**Что.** В `src/fx_ai_trader/trading/executor.py::_apply_close` и зеркально
в `src/fx_ai_trend/trading/executor.py::_apply_close` добавлен явный
guard, который **непосредственно перед** live `close_position()` API
call'ом проверяет что `broker_position_id` из нашей БД сейчас активен у
broker'а **с нашим label**. Если broker_pid не в нашем label-filtered
set'е:
1. Пытаемся подтянуть closing deal за 48h — если есть, это broker_auto
   SL/TP close, маркируем с broker-true net PnL.
2. Если deal не найден — отказываемся от `close_position()` API call'а
   во избежание cross-bot interference и помечаем позицию closed
   локально с `close_reason='label_guard_orphan'`, `pnl=0`.

**Зачем.** На одном cTrader account (46883073, FxPro demo) сейчас
живут два LLM-бота:
- `fx-ai-trader` (Discretionary), `order_label="ai-fx-trader"`,
  БД `fx_ai_trader.sqlite`.
- `fx-ai-trend` (Trend-follower), `order_label="ai-fx-trend"`,
  БД `fx_ai_trend.sqlite`.

cTrader OAuth-токен один на account (через `ctrader-token-service`), но
наша архитектура изоляции построена на label-фильтрации:

| Слой | Где | Что делает |
|---|---|---|
| 1. OPEN | `place_market_order` | каждый ордер кладётся с `label=settings.order_label` |
| 2. CONTEXT | `client_adapter.get_open_positions` | возвращает ТОЛЬКО позиции с нашим label → LLM не видит чужие в context |
| 3. БД-isolation | `state.db.AiFx*Store` | отдельный sqlite-файл на бот, чужие позиции физически отсутствуют |
| 4. RECONCILE | `get_active_broker_position_ids` | label-filtered set для `broker_reconcile` |
| 5. **CLOSE-guard** | `_apply_close` (новое) | belt-and-suspenders проверка перед close API call'ом |

Layer 5 защищает от edge-case'ов:
- manual вмешательство в cTrader Web (закрыли/реоткрыли позицию руками,
  broker_pid в БД устарел),
- корраптион БД,
- race-condition если два процесса одного бота шли бы параллельно.

**Файлы.** `src/fx_ai_trader/trading/executor.py`,
`src/fx_ai_trend/trading/executor.py`, `tests/test_fx_ai_trader.py`
(+ new test `test_label_guard_skips_close_for_orphan_broker_pid` +
update `_FakeAdapter.get_open_positions`).

**Тесты.** 71/71 fx_ai_trader+fx_ai_trend, 808/808 total — зелёные.

---

## 2026-05-18 (вечер) — feat: добавлен NG=F (Natural Gas, NAT.GAS / Henry Hub)

`коммит при deploy`

**Что.** Discretionary бот теперь следит и торгует gold + Brent +
**natural gas** (NG=F → cTrader NAT.GAS id=1118 на FxPro demo).
По правилу `no-data-fitting.mdc` это instrument-add, не стратегическое
изменение — экспериментальный n=0 счётчик прошлого forward-test'а
**не сбрасывается** (стратегические thresholds и правила оставлены
один к одному, см. prompts.py v1.1 docstring).

**Разведка.**
- Новый скрипт `scripts/fx_ai_scout_gas_symbols.py` — однократно
  запущен на VPS, дампит ProtoOASymbol для всех инструментов с gas-
  keywords (NAT, GAS, NG, TTF, HENRY).
- На FxPro demo доступен **только NAT.GAS** (NG / Henry Hub).
  TTF (европейский Dutch front-month) **отсутствует** — торгуем
  только US-bench.
- Ещё 8 инструментов `#NGas_*26` — это месячные futures, ненужны
  для CFD-стратегии.

**Pip-value research (правило `no-data-fitting.mdc`, ≥2 confirmation).**
1. CME NYMEX Henry Hub Natural Gas Futures canonical spec:
   contract size 10 000 MMBtu, minimum tick $0.001/MMBtu = **$10/tick**.
2. cTrader Open API ProtoOASymbol(id=1118, NAT.GAS, ctid=46883073):
   `digits=3`, `pipPosition=3`, `lotSize=1_000_000`,
   `swapLong=-$11.11/3d`, `swapShort=+$1.81/3d` (contango carry).
   pip-value = `(10^-pipPosition) × (lotSize/100)` = `0.001 × 10_000` =
   **$10/pip/lot**.
3. Sanity: на 0.01 lot pip-value = $0.10/pip — идентично BRENT.
   1-lot $0.10 movement = 100 pips × $10 = $1000 PnL.

**Код.**
- `src/fx_ai_trader/trading/executor.py`:
  - `_pip_size_for("NG=F") = 0.001` (digits=3).
  - `_PIP_VALUE_USD_PER_STD_LOT["NG=F"] = 10.0` (с research-блоком
    в комментариях).
- `src/fx_ai_trader/config/settings.py`:
  - `DEFAULT_AI_FX_SYMBOLS = ("XAUUSD", "BZ=F", "NG=F")`.
- `src/fx_ai_trader/llm/prompts.py` (v1.1):
  - Header: «You trade ONLY three instruments» + NAT.GAS contract spec.
  - Новая секция «NATURAL GAS — STORAGE / WEATHER / LNG FRAMEWORK»
    (5 драйверов: storage cycle, weather HDD/CDD, LNG exports,
    production / rig count, geopolitics; mistakes-to-avoid block).
  - Noise-band sizing: NG standard $0.10–0.20, EIA Thu $0.20–0.40,
    cold-snap $0.50–1.00+/MMBtu.
  - Worked sizing examples: NG entry 3.250 / SL 3.100 / 0.017 lot для
    risk $25; WARN на 50-pip stops (inside hourly noise).
  - Trading windows: добавлены Thu 14:30 UTC (EIA NG storage) + Fri
    16:00 UTC (Baker Hughes rigs).
  - JSON schema: `"symbol": "XAUUSD" | "BZ=F" | "NG=F"`.
  - Review prompt: добавлено NAT.GAS в шапку.
- `src/fx_ai_trader/news/rss.py`:
  - `GAS_KEYWORDS` (storage, EIA, NOAA, HDD/CDD, LNG terminals,
    rig count, Henry Hub, TTF, pipeline outages).
  - `SYMBOL_KEYWORDS["NG=F"] = GAS_KEYWORDS`.
- `src/fx_ai_trader/news/eia.py`:
  - `_SERIES_NG_STORAGE = "NG.NW2_EPG0_SWO_R48_BCF.W"` (Weekly Working
    Underground Storage, Lower 48, Bcf — headline EIA Thursday).
  - `EiaSnapshot.ng_storage_*` поля + format_eia_snapshot печатает
    отдельный «EIA Weekly Natural Gas (Thursday update)» блок.
- `docker-compose.yml`: default `AI_FX_TRADER_SYMBOLS` →
  `XAUUSD,BZ=F,NG=F`.

**Тесты (13 новых, все зелёные).**
- `TestPipValueTable`:
  - `test_ng_pip_value_is_10usd_per_lot`.
  - `test_ng_pip_size_is_0_001`.
  - `test_ng_pnl_canonical` (0.10 lot, $0.10 move = ~$100).
  - `test_ng_short_pnl` (0.05 lot SHORT, $0.10 move = ~$50).
- `TestRssGasClassification`:
  - `test_ng_storage_headline_matched` (EIA storage report → NG=F).
  - `test_lng_terminal_headline_matched` (Freeport LNG outage → NG=F).
  - `test_weather_forecast_headline_matched` (NOAA polar vortex → NG=F).
  - `test_oil_headline_not_classified_as_gas` (false-positive guard).
- `TestSettings.test_defaults`: ожидание обновлено на
  `("XAUUSD", "BZ=F", "NG=F")`.

Полная регрессия `tests/test_fx_ai_trader.py + test_ctrader_token_service.py`
74 passed.

**Что _не_ менялось** (важно для n-counter): R:R/risk-budget rules,
sentiment-uncertainty gate (0.7), max-positions (3), max-lot-size
(0.50), KillSwitch caps, paper/live mode flags — все одинаковы.

**Файлы.**
- `src/fx_ai_trader/trading/executor.py`
- `src/fx_ai_trader/config/settings.py`
- `src/fx_ai_trader/llm/prompts.py`
- `src/fx_ai_trader/news/rss.py`
- `src/fx_ai_trader/news/eia.py`
- `docker-compose.yml`
- `scripts/fx_ai_scout_gas_symbols.py` (new)
- `tests/test_fx_ai_trader.py`
- `BUILDLOG_AI_FX_TRADER.md`

---

## 2026-05-18 (ночь) — fix: max_tokens regression + truncation-guard

`коммит при deploy`

**Симптом.**
```
10:11:23 LLM tokens: in=1460 out=4096 cost=$0.00135
10:11:23 [ERROR] Parse error: JSON parse error: not a decision dict (missing 'action'): dict
```

**Причина.** `AI_FX_TRADER_DEEPSEEK_MAX_TOKENS` в `docker-compose.yml`
стоял default `4096` — регрессия с ранней эпохи бота. В коде
`settings.py` default `8000` с явным комментарием «С 4096 наблюдался
out=4096 и оборванный JSON». LLM упёрся в потолок, JSON-блок
с `"action"` не дописался. Парсер `_extract_last_json_object` идёт
с конца и нашёл валидный `{...}` обрубок (sentiment-блок или часть
рассуждения), но без ключа `"action"` → ошибка.

Full-cycle output состоит из: thinking-блок (DeepSeek-V4 reasoning)
+ commentary (4–8 строк) + JSON с multi-dim sentiment (5 measures ×
N items) + decision. С двумя инструментами это легко 5–7k токенов.

**Фикс.**
1. `docker-compose.yml`: default поднят `4096 → 8192` (hard cap у
   DeepSeek Anthropic-compat API).
2. `src/fx_ai_trader/app/main.py`: добавлен **truncation-guard** в
   `_run_full_cycle` и `_run_review_cycle`. Если `tokens_output >=
   max_tokens - 16` → бот логирует `WARNING` (видно регрессию), пишет
   `error=llm_truncated_at_max_tokens` в БД, **не парсит** broken
   payload (избегаем ложного `Parse error`). Цикл пропускается, LLM
   попробует снова на следующем тике.

Truncation-guard также защитит от будущих случаев: если LLM
по какой-то причине станет жадным до токенов, проблема будет
**сразу** видна в логах, а не маскироваться под parse-error.

**Файлы:** `docker-compose.yml`, `src/fx_ai_trader/app/main.py`.

---

## 2026-05-18 (вечер) — refactor: убран local-mirror, сервис = single source

`коммит при deploy`

После реализации token-service (см. предыдущую запись) обнаружилась
концептуальная ошибка: бот всё ещё писал token-копию в локальный
`ctrader_tokens_ai_fx.json` ради paranoid-fallback. Это **возвращало**
split-brain (две rotation chains на одном аккаунте) — именно то от
чего token-service спасает.

**Фикс.** `ensure_valid_token_race_safe()` и `save_refreshed_token()`
больше не пишут в локальный файл когда `CTRADER_TOKEN_SERVICE_URL`
задан — сервис единственный owner. Файл создаётся **только** как
fallback если push в сервис провалился (защита от потери single-use
refresh_token при downtime сервиса).

`AI_FX_TRADER_CTRADER_TOKEN_PATH` default переключён на
`/data/ctrader_tokens.json` (общий файл с Advisor) — используется
только в fallback-режиме.

**Файлы:** `src/fx_ai_trader/trading/token_lock.py`, `docker-compose.yml`,
тесты `test_save_refreshed_token_skips_file_when_service_accepts_push`
+ `test_save_refreshed_token_falls_back_to_file_when_service_down`.

---

## 2026-05-18 — feat: централизованный ctrader-token-service

`коммит при deploy`

**Симптом.** fx-ai-trader попадал в петлю
`ConnectionError: cTrader: token refreshed, reconnect required`
каждые ~15 минут (после 5 reconnect-failures клиент уходит в backoff
delays=[5,10,20,30,60]), при этом Advisor работал стабильно. Логи
показывали LLM-вызовы на **пустых** market-data (`get_trendbars(...)
failed: cTrader: нет подключения`), что приводило к тратe DeepSeek
tokens впустую.

**Причина.** Архитектурная: оба бота используют один cTrader demo-аккаунт
(`ctid=46883073`), но **разные** token-файлы (`/data/ctrader_tokens.json`
у Advisor vs `/data/ctrader_tokens_ai_fx.json` у fx-ai-trader,
управляется `AI_FX_TRADER_CTRADER_TOKEN_PATH`). cTrader OAuth
использует rotating refresh_tokens — каждый refresh инвалидирует
предыдущий. Два независимых rotation chain-а на одном аккаунте =
Spotware silent-rotation отстреливает сессию того, чей токен «отстал».

Существующий `fx_ai_trader.trading.token_lock.flock` защищал от
concurrent file-write **внутри одного файла**, но не от того, что
файлы **разные**. Это был не race condition — это был split-brain.

**Решение.** Новый микросервис `ctrader-token-service`
(см. `BUILDLOG.md 2026-05-18` для общих деталей). Для fx-ai-trader
конкретно:

- `ensure_valid_token_race_safe()` теперь сначала пробует HTTP-fetch
  у сервиса. flock-путь остаётся как fallback, если сервис недоступен.
- `save_refreshed_token()` (callback для `CTraderClient.on_token_refreshed`)
  сначала пушит токен в сервис, потом зеркалирует в локальный файл.
- `CTraderClient._try_refresh_token()` при silent rotation сначала
  `GET /token` у сервиса (другой бот мог уже обновить — берём готовый),
  затем `POST /refresh` с dedup-окном 5с (защита от burst-запросов
  обоих ботов в одну секунду). Локальный refresh — только fallback.

ENV-переменные (одинаковые для Advisor и fx-ai-trader, оба сервиса
ходят к общему контейнеру):
- `CTRADER_TOKEN_SERVICE_URL=http://ctrader-token-service:8080`
- `CTRADER_TOKEN_SERVICE_SECRET=...` (HTTP-Bearer)

Backward-compat: если URL/SECRET пустые — fx-ai-trader работает по
старому через flock-файл. Это позволяет постепенный rollout.

**Тесты.** Покрыты scenarios «service выдал более свежий токен —
клиент его взял без refresh», «service вернул тот же — клиент дёрнул
force_refresh с dedup», «service down — fallback на flock-путь»,
«race_safe → mirror в файл», см. `tests/test_ctrader_token_service.py`
(24 теста). Полный suite 897 passed, без регрессий.

**Файлы:**
- `src/fx_ai_trader/trading/token_lock.py` — service-first +
  `_push_to_service` helper
- `src/fx_pro_bot/trading/client.py` — `_try_refresh_via_service` (общий
  для обоих ботов, fx-ai-trader тоже использует `CTraderClient` через
  `client_adapter.py`)
- `docker-compose.yml` — `depends_on: ctrader-token-service:
  service_healthy`, env vars

---

## 2026-05-13 (вечер) — bug-fix: broker-side reconcile (stale live-позиции)

`коммит при deploy`

**Симптом.** Позиция id=3 (BRENT BUY 0.01 lot @ 105.031, opened 15:20 UTC)
в нашей БД до сих пор `closed_at=None, exit_price=None,
realized_pnl_usd=None`, при том что **cTrader давно её закрыл по
SL=104.7** (deal_id=331875628, exit $104.721, broker gross −$3.32,
balance после $423.12).

В период 16:02 → 16:44 UTC (9 циклов подряд) LLM правильно решал CLOSE
(setup invalidated: цена ниже SL, MACD bearish flip, EMA20 пробит),
но получал от cTrader:
```
err=broker close_failed: cTrader error POSITION_NOT_FOUND: Position
not found with id 150428404
```
→ бот не записывал close в БД → следующий цикл LLM снова видел
"открытую" позицию → опять CLOSE → опять 404. Бесконечный фантом.

**Root cause.** `_apply_close()` опирался **только на локальную БД**
(`store.get_open_positions()`), без проверки broker-side активности.
Когда cTrader сам закрывал позицию по SL/TP (нормальный механизм
broker-side execution для серверных SL/TP), бот не дёргал ни
`client.reconcile()`, ни `client.get_deal_list()` для синхронизации.

**Финансовое последствие.** `realized_pnl_usd` broker-закрытых позиций
(в т.ч. убыточных по SL) **НЕ попадал в `daily_pnl`** → KillSwitch
`max_daily_loss_usd` видел $0 вместо реальных потерь. На demo —
косметика, на live — финансовая дыра.

**Fix.** Two-pronged sync на ровне с paper-reconcile:

1. **`adapter.get_active_broker_position_ids()`** — обёртка над
   `client.reconcile()`, возвращает set активных broker-pid с нашим
   `label='ai-fx-trader'`. Returns `None` при API-error (не пустой
   set! правило `None != []` из Bybit-агента 2026-05-07).
2. **`adapter.get_closing_deal_for_position(broker_pid, lookback_h)`**
   — обёртка над `ProtoOADealListReq`, возвращает dict с broker-true
   `exit_price`, `gross_pnl_usd`, `swap_usd`, `commission_usd`
   из `ProtoOAClosePositionDetail`.
3. **Новый модуль `broker_reconcile.py`**: `reconcile_broker_positions()`.
   Для каждой live-позиции в БД, отсутствующей в active broker set →
   подтягивает closing deal → пишет в БД `closed_at, exit_price,
   realized_pnl_usd = gross + swap + commission, close_reason='broker_auto'`.
   Вызывается в начале каждого full + review цикла, **сразу после**
   `reconcile_paper_positions()`.
4. **POSITION_NOT_FOUND recovery в `_apply_close`**: если LLM
   решила CLOSE и broker вернул POSITION_NOT_FOUND — executor пытается
   достать closing deal и записать broker-true PnL вместо ошибки.
   Это страховка на случай если main-loop reconcile ещё не успел
   отработать между SL-fire и LLM-CLOSE-decision.

**Источники / docs.**
- cTrader Open API `ProtoOAReconcileReq` / `Res` — list open positions:
  https://help.ctrader.com/open-api/messages/#protooareconcilereq
- `ProtoOADealListReq` / `ProtoOAClosePositionDetail` —
  `grossProfit`, `swap`, `commission` per `moneyDigits` divisor:
  https://help.ctrader.com/open-api/model-messages/#protooaclosepositiondetail
- Reuse Advisor pattern: `src/fx_pro_bot/trading/client.py:494,505` —
  готовые методы `get_unrealized_pnl()` и `get_deal_list()`.
- Polling-based reconcile pattern (не realtime event-stream) —
  стандарт для async OCO/SL/TP cleanup в retail trading bots
  (см. Bybit Two-Phase Commit аналог в `BUILDLOG_BYBIT.md`).

**Файлы:**
- `src/fx_ai_trader/trading/client_adapter.py` — +2 метода.
- `src/fx_ai_trader/trading/broker_reconcile.py` — новый модуль.
- `src/fx_ai_trader/trading/executor.py` — POSITION_NOT_FOUND recovery.
- `src/fx_ai_trader/app/main.py` — `reconcile_broker_positions()` в
  full и review циклах.
- `tests/test_fx_ai_trader.py` — `TestBrokerReconcile` (6 кейсов:
  closes broker-closed pos, skips active, no-op on API down, no-op
  if deal not found, broker-net != our_calc, apply_close recovers
  from POSITION_NOT_FOUND).
- `scripts/fx_ai_inspect_position_3.py` — read-only diag.

**Тесты.** 44/44 fx_ai зелёные, 552/552 общих pass. Особенно важные:
- `test_no_op_when_broker_api_unreachable` — гарантирует что при
  сетевом сбое мы **НЕ закрываем все позиции как фантомные**.
- `test_uses_broker_net_pnl_not_local_calc` — broker gross +$92.82
  пишется в БД, не our_formula +$101.53 (см. предыдущий BACKLOG
  item «idealized PnL»).

**Эффект после deploy.**
- Текущая stale id=3 закроется автоматически в первом full-cycle
  после рестарта контейнера (broker reconcile → deal 331875628 →
  exit $104.721, PnL −$3.32, close_reason='broker_auto').
- `daily_pnl` обновится с реальным убытком.
- LLM перестанет видеть фантомную позицию в context → review-циклы
  перестанут спамить POSITION_NOT_FOUND.

---

## 2026-05-13 (день) — broker-side verification PnL формулы (XAUUSD + BRENT)

`коммит при deploy`

**Контекст.** После bug-fix BRENT pip-value (10×) пользователь попросил
прямое подтверждение что **обе** формулы (XAUUSD $1/pip/lot, BRENT
$10/pip/lot) совпадают с тем что считает сам брокер, а не косвенное
через сравнение с Advisor / RoboForex spec.

**Что сделано.** Создан read-only скрипт
`scripts/fx_ai_verify_pnl_from_history.py`, который:

1. Дёргает `ProtoOADealListReq` через `CTraderFxAdapter` за последние
   48 часов (read-only, побочных эффектов нет).
2. Для каждого закрытого XAUUSD / BRENT deal берёт `grossProfit` из
   `ProtoOAClosePositionDetail` — это **ground truth от cTrader-бэкенда**
   (целое × 10^moneyDigits, точный расчёт их движка).
3. Параллельно считает наш PnL через `_calc_pnl_usd(side, entry, exit,
   volume_lots, symbol)` с теми же entry/exit/volume что у брокера.
4. Сравнивает.

**Результат (5 реальных сделок за 48h, ctid 46883073, ALL deltas $0.0000):**

| Deal ID | Symbol | Side | Lots | Entry → Exit | Broker gross | Наш расчёт | Δ |
|---|---|---|---|---|---|---|---|
| 331862418 | XAUUSD | BUY | 0.07 | 4701.19→4696.34 | −$33.95 | −$33.95 | $0.0000 |
| 331862269 | BRENT | BUY | 0.13 | 104.864→105.578 | +$92.82 | +$92.82 | $0.0000 |
| 331861259 | XAUUSD | BUY | 0.07 | 4703.81→4703.96 | +$1.05 | +$1.05 | $0.0000 |
| 331797394 | XAUUSD | SELL | 0.06 | 4692.60→4701.52 | −$53.52 | −$53.52 | $0.0000 |
| 331796613 | XAUUSD | SELL | 0.06 | 4693.41→4690.80 | +$15.66 | +$15.66 | $0.0000 |

**Σ |Δ| = $0.0000** на 4 XAUUSD сделках (BUY и SELL) и 1 BRENT сделке.
Формула fx-ai-trader **бит-в-бит** воспроизводит то что считает cTrader
backend. pip_value математика верна для обоих инструментов.

**Бонус.** Помнишь "discrepancy" $101.53 vs $92.82 по BRENT
(BACKLOG-item ниже)? Бот в логах писал idealized PnL с `current_price`
($105.605), broker реально закрыл по `executionPrice` $105.578. На
broker'ской exit-цене ($105.578) наша формула даёт **точно** $92.82.
Это подтверждает что разница — чисто архитектурная (current_price ≠
fill_price), а не баг в pip_value.

**Файлы:**
- `scripts/fx_ai_verify_pnl_from_history.py` — read-only verification.

**Источники.** cTrader Open API:
- `ProtoOADealListReq` — официальный endpoint для истории deals.
- `ProtoOAClosePositionDetail.grossProfit` — broker-side точный PnL.
- `compare_stats.py` (Advisor) — эталонный pattern декодирования
  `grossProfit / 10^moneyDigits`.

**Категория.** Documentation / verification. Никаких изменений в торговую
логику — формула уже была корректной (bug-fix BRENT pip-value 13-May
закрыл единственный известный дефект).

---

## BACKLOG (отложено до релиза)

### Использовать broker-reported PnL / fill_price из ProtoOAExecutionEvent

**Проблема (обнаружено 2026-05-13).** На LIVE-close бот пишет в БД
**idealized** PnL: использует `current_price` из last M1 close как
exit_price и считает PnL формулой. Реальный fill на брокере хуже на
slippage (≈5-10 pip на BRENT для market sell). Пример:
позиция id=2 BRENT — наш расчёт `+$101.53` (exit $105.605), реально
у брокера `+$92.82` (fill ~$105.538). Дельта $8.71 = slippage 6.7 pip +
commission.

**Почему важно перед релизом.**
- Статистика бота врёт в нашу пользу (overestimate winners, underestimate
  losers). Forward-test метрики (Sharpe, max DD, expectancy) искажены.
- **KillSwitch daily/total_loss считает по нашему PnL, не broker's**.
  При накапливании потерь бот может не остановиться вовремя.
- На demo это косметика, на live — реальные деньги.

**Что нужно сделать.**
1. `adapter.close_position()` → ждать `ProtoOAExecutionEvent` с
   `executionType=ORDER_FILLED` для close, вернуть `OrderResult` с
   реальным `fill_price` и `realized_pnl_usd` от broker.
   В cTrader execution event есть `closePositionDetail.grossProfit`
   и `closePositionDetail.commission` — оба нужны.
2. `executor._apply_close()` → использовать broker-данные вместо
   `current_price` / `_calc_pnl_usd()`.
3. На paper-mode — оставить локальный расчёт (там нет broker'a).
4. Аналогично для open — сохранять реальный `fill_price` от execution
   event, не quote (сейчас уже частично делается, но нужно сверить).

**Источники / docs.**
- cTrader Open API ProtoOAExecutionEvent:
  https://help.ctrader.com/open-api/model-messages/#protooaexecutionevent
- ClosePositionDetail fields: grossProfit, commission, swap, balance.

**Эффорт.** ~2-3 часа: модификация adapter, тесты на mock execution
event, документация в коде.

**Категория.** Не блокирует Phase 1 (paper observation + demo live),
блокирует Phase 2 (production live).

---

## 2026-05-13 (день) — bug-fix: pip-value для BRENT занижен в 10×

`коммит при deploy`

**Симптом.** Пользователь видел в FxPro cTrader-приложении floating PnL
по позиции id=2 (BUY 0.13 lot BRENT @ 104.824) **$39**, в то время как
бот в reviews писал «profit ~0.2R» и не срабатывал locked-profit guard
(≥1.5R). При profit-per-pip $10 на 1 lot и 0.13 lot = $1.30/pip, move
30 pip = $39 floating. По старой формуле бот считал $1/pip/lot =
$0.13/pip × 30 = $3.9 — **в 10× меньше**.

**Root cause.** В `executor.py` функция `_pip_value_per_std_lot()`
возвращала hardcoded `$1.0` для всех символов, включая BRENT. Это
было сделано как «Phase 1 baseline» (паттерн docstring так и говорил
«уточняется при paper-observation»). Реальное значение для FxPro
BRENT — **$10/pip/lot**, потому что 1 std lot BRENT = 1000 barrels
(canonical ICE/RoboForex/FxPro spec), а pip = $0.01/barrel.

**Источники** (правило `no-data-fitting.mdc` — ≥2 confirmation):

1. **ICE Brent Crude Futures** (canonical worldwide):
   theice.com/products/219 — contract size **1000 barrels**, minimum
   fluctuation **$0.01/barrel = $10/contract**.
2. **RoboForex Spot Brent Pro spec page**:
   https://roboforex.com/forex-trading/trading/specifications/card/pro-stan/BRENT/
   — «1 Pip Size = 0.01, Size of 1 lot = 1000 barrels, term currency
   = USD». Подтверждает что retail-broker'ы тоже следуют ICE-стандарту.
3. **Эмпирическое подтверждение** на FxPro demo (ctid=46883073,
   2026-05-13): позиция id=2 BUY 0.13 lot @ 104.824, move ≈30 pip до
   ≈105.12. Floating PnL в cTrader-приложении = **$39**. Расчёт:
   30 × 0.13 × $10 = $39.0 ✓ (со старой формулой было бы $3.9).

**Влияние bug'а на торговую логику.**

LLM получал в context'е заниженные `risk_usd` / `R-multiple` / paper-PnL:

| Метрика | Что видел LLM (×$1) | Реально на FxPro (×$10) |
|---|---|---|
| risk на текущую позицию | $17 | $172 |
| profit floating при move 30 pip | $4 | $39 |
| R-multiple при +$39 floating | 0.23R | 0.23R (только знаменатель другой) |
| TP-hit profit | $26 | $258 |

R-multiple сам по себе **верный** (числитель и знаменатель множатся
на одинаковый фактор и сокращаются). Поэтому locked-profit guard
(≥1.5R) **формально** работал. Но:

- **Sizing был неправильный**. LLM думал что 0.13 lot = $17 risk
  (2.8% от $500 virtual_capital), реально — $172 risk (34%).
- **KillSwitch daily/total cap не учитывал реальный risk**. При
  daily_loss=$150 одна потеря по реальной формуле ($172) превышает
  cap одной сделкой.
- **Прогнозируемый TP-profit в логах** искажён в 10×.

**Что делает фикс.**

- `_pip_value_per_std_lot()` теперь словарь per-symbol:
  - `XAUUSD` = $1.0/pip/lot (canonical 100 oz × $0.01).
  - `BZ=F`   = $10.0/pip/lot (canonical 1000 barrels × $0.01).
  - fallback $1.0 для незнакомых символов (поведение pre-fix).
- В промпт добавлены явные worked sizing examples — LLM теперь видит
  что для BRENT pip-value в 10× больше, и подгоняет lots-formula.
- При старте бот логирует фактический pip-value по каждому символу
  (`pip_size=0.0100, pip_value=$10.00/pip/lot`) — для будущей
  верификации.

**Влияние на текущую открытую позицию id=2.**

- SL/TP в cTrader — абсолютные цены, **остаются на брокере как есть**.
- В нашей БД `volume_lots / entry_price` остаются как есть.
- Только пересчёт R-multiple для LLM-reviews изменится (теперь будет
  правильный). Текущие +$39 floating ≈ +0.23R — всё ещё ниже 1.5R
  trigger, бот продолжит держать.

**Категория.** Bug-fix критичный (не curve-fitting): корректируем
расчёт под реальную работу брокера, основано на canonical spec + 2
дополнительных confirmation source'ов. По правилу `no-data-fitting.mdc`
— допустимая правка, эксперимент n=0 не сбрасывается.

**XAUUSD pip-value тоже проверен** (без правки — формула была верной):
Advisor открыл XAUUSD 0.07 lot через стратегию `gold_orb`
(broker_position_id=150420246, entry $4702.33). Эта позиция стала
независимым test-vehicle для проверки нашей формулы $1/pip/lot. При
current_price $4695.71 наш расчёт даёт PnL = (4695.71-4702.33) ×
0.07 × $100 = -$46.34. Реально в cTrader-приложении floating PnL =
**-$48**. Дельта $1.66 объясняется spread'ом FxPro (~0.5 pip × 7 oz)
и lag'ом current_price в Advisor-БД (1-2 минуты от last poll).
Источники: RoboForex Pro spec для XAUUSD (1 lot=100 oz, pip=0.01,
USD), FxPro contract specs, LBMA canonical. Не правим — спецификация
универсальна для всех brokeров на spot gold.

**Открытый вопрос для пользователя.** При новой формуле KillSwitch
caps (daily=$150, total=$300) становятся слишком тесными:
- 1 потеря на 0.13 lot BRENT с SL distance 132 pip = -$172 → выходит
  за daily-cap одной сделкой.
- Нужно поднять daily_loss / total_loss пропорционально новому
  пониманию реального риска (обсуждается отдельно).

**Файлы:**
- `src/fx_ai_trader/trading/executor.py` — словарь
  `_PIP_VALUE_USD_PER_STD_LOT` с per-symbol значениями + 3-source citation.
- `src/fx_ai_trader/trading/client_adapter.py` — логирование
  pip-value при resolve symbols.
- `src/fx_ai_trader/llm/prompts.py` — обновлены worked sizing examples
  с правильным $10/pip/lot для BRENT.
- `tests/test_fx_ai_trader.py` — новый `TestPipValueTable` (5 тестов,
  включая эмпирический $39).
- `scripts/fx_ai_inspect_symbols.py` — diag-script для опроса cTrader
  ProtoOASymbol (read-only). Не используется при работе бота, только
  для ручной верификации.

---

## 2026-05-13 (утро) — bug-fix: clamp out-of-range sentiment vs hard-reject

`коммит при deploy`

**Симптом.** Cycle 03:50 UTC:
```
[ERROR] fx_ai_trader: Parse error: schema validation error:
  [{'type': 'greater_than_equal',
    'loc': ('sentiment', 'items', 2, 'forwardness'),
    'msg': 'Input should be greater than or equal to 0',
    'input': -0.3, 'ctx': {'ge': 0.0}}]
```

LLM прислал `forwardness=-0.3` для 3-й новости. Pydantic `Field(ge=0.0,
le=1.0)` отвергнул **всё** решение целиком — потеряли decision (включая
core: open / close / hold).

**Root cause.** LLM путает `forwardness` (∈ [0, 1], где 0 = backward-
looking) с `polarity` (∈ [-1, 1], единственное signed-измерение). Это
известный failure-mode structured outputs от LLM: out-of-range numeric
values при отсутствии provider-level grammar-constrained sampling.
DeepSeek через Anthropic-compat прокси такой gen-time enforcement не
гарантирует (в отличие от Claude `strict: true` или OpenAI strict).

**Решение (исследовано по тематическим ресурсам).** Многоуровневая
защита по best-practices (см. ниже). Применили L2 + усиление L0 (промпт):

- **L0 (prompt)** — уточнение в SYSTEM_PROMPT: явные ranges с inequality
  notation, специальная строка «DO NOT use negative values for
  relevance / intensity / uncertainty / forwardness — that is a
  frequent slip from polarity confusion».
- **L1 (provider strict)** — НЕ применимо: DeepSeek-V4 через Anthropic-
  compat прокси не поддерживает Claude `strict: true` reliably (по
  Anthropic docs strict даётся только в их собственных моделях с
  grammar-constrained sampling).
- **L2 (Pydantic coerce)** — **выбран**. Annotated pattern с
  `BeforeValidator` + `Field(ge/le)` constraint. Clamp out-of-range
  ДО валидации; Field constraint остаётся как формальная спецификация
  схемы. Безопасно к `None` / `NaN` / `inf` / нечисловым типам (всё →
  0.0 как neutral default).
- **L3 (retry-with-feedback)** — не применили (это бо́льший рефакторинг,
  Instructor-style auto-retry с error feedback в prompt). Отложено на
  потом если clamp недостаточен.
- **L4 (graceful degradation)** — частично уже было (parse_action
  возвращает string-error → log_decision записывает error, цикл не
  крашится).

**Источники.**

- [Pydantic ofic docs «Validators»](https://docs.pydantic.dev/latest/concepts/validators)
  — 4 типа валидаторов (After/Before/Plain/Wrap), annotated pattern,
  пример с `truncate` через `WrapValidator` (длинная строка обрезается,
  не реджектится).
- [Pydantic blog «Minimize LLM Hallucinations with Pydantic Validators»](https://blog.pydantic.dev/blog/2024/01/18/llm-validation/)
  — «Pydantic validators minimize LLM hallucinations by enforcing
  constraints on model outputs». Подтверждение что **defensive coerce
  vs hard reject** — это рекомендованный паттерн для LLM-payloads.
- [Instructor «Validation & Retry»](https://python.useinstructor.com/learning/validation/retry_mechanisms/)
  — auto-retry pattern: validation error feeds back to LLM как
  context, modelл регенерирует. Configuration: `max_retries`,
  `retry_if_parsing_fails`.
- [Anthropic «Strict tool use»](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use)
  — grammar-constrained sampling (`strict: true`) на provider-side.
  Идеальный L1, но доступен только в Anthropic native, не через
  DeepSeek-compat прокси.
- [tianpan.co «Structured Outputs Not a Solved Problem», 2026](https://tianpan.co/blog/2026-04-18-structured-output-json-mode-failure-modes)
  — three-tier recovery strategy: detect → log → retry/fallback/surface.
- [callsphere.ai «Handling Structured Output Failures»](https://callsphere.ai/blog/handling-structured-output-failures-retries-fallbacks-partial-parsing.md)
  — graceful degradation pattern (safe default vs crash).
- [The Neural Base «Validator functions»](https://theneuralbase.com/structured-outputs/learn/beginner/validator-functions/)
  — `@field_validator` баг-гарды для LLM JSON.

**Имплементация.**

```python
def _coerce_unit(value: Any) -> float:
    """Clamp к [0, 1]; defensive к None/NaN/inf/strings → 0.0."""
    if value is None: return 0.0
    try: v = float(value)
    except (TypeError, ValueError): return 0.0
    if math.isnan(v) or math.isinf(v): return 0.0
    return max(0.0, min(1.0, v))

UnitFloat = Annotated[float, BeforeValidator(_coerce_unit), Field(ge=0.0, le=1.0)]
SignedUnitFloat = Annotated[float, BeforeValidator(_coerce_signed_unit), Field(ge=-1.0, le=1.0)]

class SentimentItem(BaseModel):
    title_snippet: str = Field(default="", max_length=200)
    relevance: UnitFloat
    polarity: SignedUnitFloat
    intensity: UnitFloat
    uncertainty: UnitFloat
    forwardness: UnitFloat
```

**Тесты.** Добавлен `test_open_sentiment_out_of_range_clamped` — кейс
с тремя видами ошибок одной partition:
- `forwardness=-0.3` → 0.0 (исходный реальный bug)
- `polarity=-2`, `relevance=1.5`, `forwardness=2.0` → clamp к границам
- `intensity="N/A"` (string), `uncertainty=null` → 0.0 как safe default

33/33 в `test_fx_ai_trader.py`, 515/515 в полной панели зелёные.

**Категория.** **Bug-fix схемы**, не curve-fitting стратегии (правило
`no-data-fitting.mdc`): торговая логика, R:R, sentiment-uncertainty
gate, threshold'ы — НЕ менялись. Эксперимент n=0 НЕ перезапускается.
Аналог fix'у бага в коде, не tuning'у стратегии.

**Файлы:**
- `src/fx_ai_trader/trading/executor.py` — annotated pattern с
  BeforeValidator, type aliases `UnitFloat`/`SignedUnitFloat`,
  defensive coerce для None/NaN/inf/non-numeric.
- `src/fx_ai_trader/llm/prompts.py` — уточнение в sentiment-блоке:
  явные inequality ranges + строка про anti-polarity confusion.
- `tests/test_fx_ai_trader.py` — новый позитивный тест на clamp.

---

## 2026-05-12 (вечер) — prompt v1.0 «discretionary commodity trader» + KillSwitch redesign + experiment **n=0 reset**

`коммит при deploy`

**Контекст.** Пользователь явно указал: «из нашего кода по решениям и
стратегиям ничего брать не надо было... цель ии агента не повторять
нашего бота, а принимать решения самому». v0.1 / v0.2 промптов копировали
нашу advisor-математику (R:R ≥ 1.5, risk $25 hard, correlation haircut
0.7, same-direction concentration block) — LLM упирался в эти микро-
ограничения и за 13 decisions не выполнил ни одной сделки. Подход
был концептуально неправильным.

**Что переделано.** Полная переработка промпта и safety layer:

1. **Промпт v1.0 — discretionary commodity trader.** Содержание построено
   на реальных тематических ресурсах для gold/oil-трейдеров:

   - **KenMacro «How to Trade Gold (XAUUSD) 2026: Macro Trader's
     Institutional Guide»** (Ken Chigbo, 18+ years London FX, upd
     06-May-2026, https://kenmacro.com/how-to-trade-gold-xauusd-2026/):
     5-driver hierarchy (real yields → DXY → central banks → geopol →
     ETF/COT), noise-band sizing ($15–25 normal / $30–50 FOMC-NFP /
     $100–200 macro shocks), trading windows (London open / NY open /
     COMEX close), top-5 retail failure modes, Macro-Flow Confluence
     Pullback (MFP) setup.
   - **KenMacro «How to Trade Oil: The Macro Trader's Guide»**
     (https://kenmacro.com/how-to-trade-oil/): 4-channel framework
     (supply / demand / dollar / geopol), DXY correlation **flips**
     по режиму (supply-led = positive, demand-led = inverse), OPEC+
     quota-compliance-spare-capacity.
   - **FXMacroData «Gold vs. Real Yields»**: real-yields объясняют
     45–55% квартальной gold-return variance.
   - **Sprott Money / GetARC «Gold COT Report Analysis» May 2026**:
     managed money net long +94 254 contracts (down from +302 508 в
     Feb despite higher price — short-covering rally, exhaustion).
   - **Middle East Insider «OPEC+ Spare Capacity April 2026»** (22-Apr-
     2026): spare capacity ~5M b/d, highest since 2009, compresses
     risk premium / caps rallies.
   - **Middle East Insider «Brent Crude Q2 2026 Forecast»**: $72–88
     institutional band.
   - **East Daley / Investing.com «Brent-WTI Spread»**: May 2026 spread
     ~$8–12 vs historical $3.85 avg (Hormuz disruption + light/heavy
     mismatch).
   - **GlobalMarketRaiders «EIA Edge: WTI Crude Counter-Trend»**:
     EIA Wed 10:30 ET = single biggest scheduled vol event, fade-the-
     spike setup, API Tue evening preliminary.
   - **Mark Douglas «Trading in the Zone»** (2000, Penguin/Prentice
     Hall): probabilistic mindset, 5 fundamental truths, accept-risk-
     emotionally framework, casino-operator vs gambler distinction.
   - **Van K. Tharp «Definitive Guide to Position Sizing Strategies»**
     (2008, IITM Press) + R-multiple framework: P = C/R, position
     sizing accounts for ~91% of performance variation среди profi-
     managers.

   Промпт не содержит больше «v0.x» формул из нашего executor'а.
   LLM сам решает entry-confirmations, R:R, position size по Tharp
   R-multiple, когда close, когда hold. Hold — default; «patience is
   the edge».

2. **KillSwitch v1.0 — broker-safety only.** Сняты:
   - `correlation_haircut=0.7` (gold↔oil 2-я same-side → 0.7×).
   - `same-direction concentration block` (3-я same-side в correlated
     set отвергалась).
   - `R:R ≥ 1.5` hard cap в `executor.py`.
   - `risk_per_trade_usd ≤ $25` hard cap в `executor.py`.

   Оставлены ТРИ класса защиты:
   - **Catastrophic loss caps**: `max_daily_loss_usd=$150`,
     `max_total_loss_usd=$300` — полная остановка эксперимента, НЕ
     tuning-параметр.
   - **Broker margin safety**: `max_open_positions=3` (runaway-loop
     protection), `max_positions_per_symbol=3` (= общий, sanity),
     `max_lot_size=0.50` (clamp; на demo $1500 и XAUUSD margin
     ~$3000/lot = 0.5 лот ≈ весь капитал).
   - **Anti-hallucination gate**: `aggregate_uncertainty > 0.7` →
     reject open (LLM сам должен был вернуть hold; backstop в
     `parse_action`).

3. **Эксперимент перезапущен n=0** от **12-May-2026 ~11:30 UTC**
   (deploy v1.0). 13 предыдущих decisions с 0 executed не дают
   статистических данных — терять нечего. 14-day forward-test
   стартует заново. Эта правка эквивалентна **смене стратегии**, не
   bug-fix (правило `no-data-fitting.mdc`: «Если хотя бы одно условие
   не выполнено — не отключаем»; здесь же отключаем всю торговую
   логику, поэтому n=0 reset обязателен).

**Файлы:**
- `src/fx_ai_trader/llm/prompts.py` — полностью переписан SYSTEM_PROMPT
  + SYSTEM_PROMPT_REVIEW, docstring с реальными источниками.
- `src/fx_ai_trader/safety/killswitch.py` — убраны `_correlated_with`,
  same-direction block, correlation haircut. `KillSwitchConfig` без
  `correlation_haircut`.
- `src/fx_ai_trader/trading/executor.py` — убран R:R ≥ 1.5 hard check,
  убран `risk_usd > settings.risk_per_trade_usd` hard check;
  `risk_usd`/`r_r` остались для audit-логов.
- `src/fx_ai_trader/config/settings.py` — удалены `risk_per_trade_usd`,
  `correlation_haircut`; `max_positions_per_symbol` 2 → 3.
- `src/fx_ai_trader/app/main.py` — log строка адаптирована.
- `tests/test_fx_ai_trader.py` — переписаны KillSwitch и Settings
  тесты под v1.0 API; добавлены `test_v1_no_correlation_haircut` и
  `test_v1_no_same_direction_block`.
- `.env.example` — секция FX AI Trader обновлена с заметкой о
  снятых env vars.

**Тесты:** 32/32 в `test_fx_ai_trader.py` зелёные. Полный прогон
`tests/` — 514/514 проходят.

**Что ожидаем после deploy.** LLM прочитает promt v1.0 с 5 драйверами
gold и 4 каналами oil. Будут ли реальные open'ы или продолжение hold'ов
— решит сам LLM по реальным данным feed'а (price + DXY + EIA когда
есть + RSS news). Метрика успеха Phase 1: НЕ количество сделок (hold
ok), а **качество reasoning** (макро-driver упоминается? noise-band
sizing? real-yields/DXY check?) + отсутствие тех. ошибок parser/
направления SL.

---

## 2026-05-12 (утро) — prompt v0.2 bug-fix: LLM pip-confusion для XAUUSD/BRENT (ОТМЕНЁН)

> **Status:** эта версия отменена в тот же день вечером (см. v1.0 выше).
> Причина: подход «уточняем pip-math в промпте через формулы из нашего
> executor'а» — это копирование advisor-логики, чего пользователь явно
> просил не делать. Запись сохранена для исторического контекста.

## 2026-05-12 — prompt v0.2 bug-fix: LLM pip-confusion для XAUUSD/BRENT

`коммит при deploy`

**Симптом.** За 3 часа paper-mode (07:51 → 10:56 UTC, 13 decisions) — **0
executed**, 11 errors. Распределение:

| Ошибка | Кол-во | Что произошло |
|---|---|---|
| `risk_usd > $25` | 4 | LLM ставит SL distance 1050–5228 pips (50× больше разумного) |
| `R:R < 1.5` | 3 | LLM думает R:R=1.11–1.37 приемлемо |
| `parse_error` | 3 | id 1+2 до max_tokens-fix (4096), id 7 после (рецидив на 8000) |
| `SL direction` | 1 | BUY с SL=4690 выше price=4686.54 |
| `hold` | 2 | LLM сам пропустил — корректно |

Все 8 попыток открыться — **BUY XAUUSD**. Ни одной SELL, ни одного
BRENT. Все 8 — заблокированы executor'ом.

**Причина (root cause).** LLM путает **определение pip** для XAUUSD/BRENT
spot CFD:

- XAUUSD pip = **0.01 USD/oz** (corrected) — НЕ 0.0001 как для EUR/USD.
- BRENT pip = **0.01 USD/barrel** — то же самое.

При цене XAUUSD ~4690, LLM-генерируемые SL ~4670 → реальная distance в
*price* = $20, в *pips* = 2000. LLM, видимо, calculcates distance в
pips как `int(distance × 10000)` (привычка из EUR/USD), получая 2-3
порядка отклонения. В коде `executor.py:_pip_size_for("XAUUSD")=0.01`
правильный — executor рассчитывает risk_usd корректно (2000 pips ×
0.5 lots × $1/pip = $1000). KillSwitch блокирует. Не баг кода — баг
LLM understanding.

**Решение (prompt v0.2).** Целевая правка `SYSTEM_PROMPT` без изменения
стратегических порогов:

1. **Новый блок «PIP CALCULATION — CRITICAL»** перед DECISION FORMAT:
   - Явное «pip = 0.01 USD per ounce/barrel, NOT 0.0001»;
   - Два numerical примера (правильный + неправильный) для XAUUSD;
   - HARD CEILING: SL distance > 100 pips XAUUSD / > 80 pips BRENT —
     return "hold" не задумываясь;
   - Memorise-formula `risk_usd = SL_pips × $1 × lots`, ≤ 25;
   - 3 примера допустимых (0.5×50, 0.2×30, 0.1×80) + 2 ранее
     отвергнутых для контраста (cargo cult anti-pattern).

2. **MANDATORY SANITY-CHECK** перед JSON: 4 шага explicit compute
   (SL_distance_pips, direction inequalities, R:R, risk_usd) — LLM
   должен напечатать их в commentary до DECISION, иначе hold.

3. **Усилен HOLD-default**: «A rejected entry costs 0; a wrong entry
   costs up to $25. 0 trades for a day is fine. Never force a trade.»

**Что НЕ менялось (стратегические пороги под защитой `strategy-guard.mdc`):**

- `R:R ≥ 1.5` (BBX Research «Classic 1-2-3 Scaling»).
- `risk_per_trade_usd = $25` (1% от $500 виртуального капитала).
- `aggregate_uncertainty > 0.7` → hold (arxiv 2603.11408 sentiment gate).
- `max_lot_size = 0.50`.
- `MAX_POSITIONS_PER_SYMBOL = 2`, `MAX_OPEN_POSITIONS = 3`.
- `CORRELATION_HAIRCUT = 0.7` (finaur 2026 «correlations spike»).
- Multi-dim sentiment структура (5 dimensions per news).
- EXIT MANAGEMENT 4 trigger'а.

**Compliance с правилами репо:**

| Правило | Статус | Обоснование |
|---|---|---|
| `no-data-fitting.mdc` | OK | Это bug-fix LLM-понимания (pip definition), не curve-fitting под результаты бэктеста. Аналог Advisor `MIN_BARS=5→50` от 28.04 — clarification existing semantics, не optimization. |
| `strategy-guard.mdc` | OK | Stratagic thresholds (R:R, risk, sentiment-gate, lot caps) не тронуты. Меняется только **clarification** definition'а pip и **explicit sanity-check** перед entry — это user-prompt engineering, не торговая логика. |
| `sample-size.mdc` | OK | На момент правки n=0 executed trades. Решение не основано на P&L stats, а на 100% parse/validation error rate (8/8 attempts rejected на 1-уровневой semantic ошибке). |

**Эксперимент НЕ перезапущен.** 14-day counter продолжает идти от
исходного MVP deploy (12.05.2026). Если за 24-48 часов после prompt v0.2
LLM продолжит давать 100% rejection rate — это уже системная проблема
с LLM (или с симбиозом промпт+модель), потребуется другой подход
(например, переход на pure pip-based JSON schema без price-units, или
явная конвенция в executor'е принимать SL/TP в USD distance).

**Тесты.** 34/34 fx-ai-trader pass; полный suite не запускался —
изменение в одном файле prompts.py (string constant), не затрагивает
executor / killswitch / parser. Логику парсера НЕ меняли.

**Файлы:**
- `src/fx_ai_trader/llm/prompts.py` (новый раздел + sanity-check + docstring v0.2)
- `BUILDLOG_AI_FX_TRADER.md` (эта запись)

---

## 2026-05-12 — token rotation hardening (изоляция OAuth-токенов)

`коммит при deploy`

**Контекст.** В тот же день что и MVP deploy случился инцидент с OAuth:
`refresh_token` в shared `/data/ctrader_tokens.json` оказался spent
после single-use rotation. Advisor с 09.05 не торговал, fx-ai-trader не
смог стартануть. Детальный post-mortem — в `BUILDLOG.md` 12.05
«defensive token sync + startup token-status log».

После полного re-auth через `fx-pro-auth`-flow тот же риск остаётся
для будущего: если callback `_on_token_refreshed → token_store.save`
упадёт между OAuth-call и `save()` — refresh_token потеряется.

**Защита на 3 уровнях (см. BUILDLOG.md детали):**

**A. Defensive sync** в `CTraderClient._do_auth` — после каждого
успешного auth in-memory токены пишутся в shared store через
callback (идемпотентно). Закрывает race «refresh прошёл, callback
упал».

**B. Startup logging** через `auth.log_token_status` — INFO/WARN/ERROR
в `docker logs` обоих ботов про сколько дней до expiration. Видимость.

**C. Изоляция token-store** между Advisor и fx-ai-trader. Дефолтный
`AiFxTraderSettings.ctrader_token_path` сменён с
`/data/ctrader_tokens.json` (shared) на `/data/ctrader_tokens_ai_fx.json`
(отдельный grant). После этого refresh одного бота **не задевает** refresh
другого: они живут на двух независимых OAuth grant'ах одного приложения
client_id.

**Что НЕ изменилось.**

- `client_id` / `client_secret` остаются общие (это credentials самого
  приложения, не пары access/refresh).
- `CTRADER_ACCOUNT_ID=46883073` (demo-аккаунт) общий — оба бота
  торгуют на одном счёте, но с **разными labels** (`fx-pro-bot` для
  Advisor, `ai-fx-trader` для AI). Reconcile-логика отделяет позиции
  по label (см. `client_adapter.get_open_positions`).
- `token_lock.py` (file-flock advisory lock) остаётся, но теперь
  имеет academic смысл — два разных файла не конкурируют. Хранится
  как defence-in-depth: если кто-то в `.env` пропишет тот же path —
  flock спасёт от corrupted writes.

**OAuth-flow для fx-ai-trader (выполнено вручную перед push'ем).**

1. Сгенерирован тот же `grantingaccess/?client_id=...&redirect_uri=...`
   URL как для Advisor (один client_id = одно приложение cTrader).
2. Пользователь авторизовался в браузере **второй раз** — cTrader
   выдал **новый** authorization code (тот же приложение, тот же
   аккаунт, но второй независимый grant — refresh_token будет
   собственный).
3. На VPS внутри образа `fx-pro-bot:local` сделан
   `exchange_code_for_tokens(...)` → атомарная запись в
   `/data/ctrader_tokens_ai_fx.json`.
4. После этого запушен код-change (default path) и сделан selective
   rebuild fx-ai-trader.

**Verification после деплоя:**
- Логи fx-ai-trader при старте: `FX-AI-Trader cTrader OAuth: токен
  валиден до 2026-06-11..., осталось 30.0 дней` (INFO).
- Логи Advisor при старте: `Advisor cTrader OAuth: токен валиден до
  2026-06-11..., осталось 30.0 дней` (INFO).
- Файлы:
  - `/data/ctrader_tokens.json` — Advisor (refresh_token = `Slyfr...`)
  - `/data/ctrader_tokens_ai_fx.json` — fx-ai-trader (другой refresh_token)
- Оба бота подключаются к cTrader через свои OAuth grant'ы.

**Operational follow-up.** Раз в 2-3 недели смотреть `docker logs` обоих
ботов на наличие `WARNING ... токен истекает через X дней` — если
появилось, пробросить `fx-pro-auth` заранее на оба бота (отдельные
OAuth-flow для каждого token-файла).

**Файлы:** `BUILDLOG.md` 12.05 «defensive token sync + startup
token-status log» содержит полный список.

---

## 2026-05-12 — Phase 1 deploy + fix LLM max_tokens 4096→8000

`коммит при deploy`

**Контекст.** После коммита `871e67c` (MVP scaffold) — selective rebuild
fx-ai-trader на VPS. Контейнер изначально упал в restart-loop:

```
RuntimeError: cTrader refresh error: Access denied
```

**Причина (НЕ наш код).** Shared `/data/ctrader_tokens.json` содержал
`refresh_token`, который Spotware уже признал недействительным (cTrader
OAuth2: refresh_token rotation = single-use grant, RFC 6749 §6).
Последний успешный refresh-callback Advisor'а датируется ≈9 апреля
(вычислено по `expires_at = 1778349598` минус 30-дневное окно). С тех пор
файл не обновлялся, а в `client.py` proactive-refresh (`fb0ffd1`,
11.05.2026) использует тот же spent token.

На момент моей диагностики Advisor сам тоже шёл с `cTrader: торговля
отключена` (видно в его свежих логах) — то есть проблема общая, не у
fx-ai-trader специфическая.

**Решение.** Полный re-auth через `fx-pro-auth`-flow:
1. Сгенерировал auth URL, пользователь авторизовался в браузере
   ([id.ctrader.com](https://id.ctrader.com/my/settings/openapi/grantingaccess/)).
2. На VPS внутри образа `fx-pro-bot:local` (`docker run --rm
   --env-file .env`) сделал `exchange_code_for_tokens(...)` →
   `TokenStore.save(...)`. Новый `expires_at = 1781200153` (≈11 июня).
3. `docker compose restart advisor` + `docker compose up -d --no-deps
   fx-ai-trader` → оба контейнера подхватили свежие токены из
   `/data/ctrader_tokens.json` через свои `TokenStore.load()` / наш
   `ensure_valid_token_race_safe()`.

После рестарта:
- Advisor: `cTrader: аккаунт 46883073 авторизован, готов к торговле`.
- fx-ai-trader: `SymbolCache 252`, `XAUUSD → XAUUSD (id=41)`,
  `BZ=F → BRENT (id=1117)`, `Full cycle 1`, RSS 40 items (8–9 после
  gold+oil фильтра), DeepSeek 200 OK, цена цикла $0.00174.

**Урок и follow-up для будущего.** Когда оба бота держат `ctrader_tokens.json`
в общем volume — потеря синхронизации возможна (Advisor рефрешит in-memory,
но если callback `_on_token_refreshed → token_store.save` падает между
вызовом OAuth-endpoint и `json.dump` — token-store остаётся со spent refresh).
Наш `token_lock.py` решает concurrent refresh между процессами, но не
решает «callback failed mid-write». Phase 2: либо отдельный
`/data/ctrader_tokens_ai_fx.json` (полная изоляция OAuth у каждого бота),
либо health-check «is refresh-token still valid» в Advisor с alert через
RSS/Telegram. Сейчас зафиксировано как known-risk, без code-fix.

---

### fix(fx_ai_trader): LLM max_tokens 4096 → 8000 (JSON режется)

`коммит при deploy`

**Симптом.** После успешного старта в `07:50–08:08 UTC` 12.05 контейнер
работал, но 2 full-cycle подряд:

```
LLM tokens: in=4247 out=4096    ← упёрся в max_tokens
LLM response: ## Analysis Commentary 1. TREND: XAUUSD 4H EMA20 (4699)...
Parse error: no JSON object with 'action' found
Parse error: JSON parse error: not a decision dict (missing 'action'): dict
```

LLM выдавал полный analysis commentary (TREND / VOLATILITY / MACRO /
SENTIMENT для двух символов) и **обрезался по `max_tokens=4096`** до того
как добирался до финального JSON-блока. В результате `parse_action` не
находил decision-объект → `apply_action` не вызывался → бот не торговал.

**Причина (тех-параметр, не торговая логика).** Анти-Anthropic-compat
endpoint DeepSeek-V4 включает thinking-блок (внутренний reasoning) поверх
max_tokens. На bybit-варианте `ai_trader` 4096 достаточно (1 символ,
без EIA блока). У `fx_ai_trader` промпт ×2 длиннее:
- 2 символа × (current/1H × 24 / 4H × 30 / индикаторы)
- macro-block (DXY + EIA для oil)
- multi-dim sentiment блок per news × 5 items
- двойной EXIT MANAGEMENT блок (full vs review)

Соответственно ответ тоже ×2 длиннее. 4096 = thinking + commentary, на
JSON ничего не остаётся.

**Решение.** В `AiFxTraderSettings.deepseek_max_tokens` дефолт
`4096 → 8000`. Anthropic-compat API DeepSeek поддерживает до 8192
(см. [api-docs.deepseek.com/guides/anthropic_api](https://api-docs.deepseek.com/guides/anthropic_api)).
Стоимость full-cycle растёт с $0.00174 до ~$0.0028 (+60%) — для
paper-mode наблюдения за ~14 дней при 96 циклах/сутки = $3.9/мес вместо
$2.4, незначимо.

**Без изменений.** Промпт `SYSTEM_PROMPT` НЕ редактируется (он заморожен
правилом `no-data-fitting.mdc` на ≥14 дней paper-observation). Только
бюджет на output. KillSwitch, multi-dim sentiment, R:R 1.5 gate, paper
reconcile — без изменений.

**Compliance.** Не нарушает `strategy-guard.mdc` (тех-параметр клиента
LLM, не торговый порог), `no-data-fitting.mdc` (выход режется по
объективной причине: `out=4096` в логе, не подгонка под результаты
бэктеста), `sample-size.mdc` (не connection между сделками, n=0 на
момент фикса).

**Файлы:**
- `src/fx_ai_trader/config/settings.py` (default 4096 → 8000 + комментарий)
- `.env.example` (раздел AI_FX_TRADER, новый default)
- `BUILDLOG_AI_FX_TRADER.md` (эта запись)

**Тесты.** 34/34 fx-ai-trader pass; полный suite 513/513 pass (max_tokens
не влияет ни на parser, ни на бизнес-логику).

---

## 2026-05-12 — Phase 1 MVP scaffold (paper-mode)

**Запрос пользователя:** «создать AI-агента для FX (gold + oil), аналог
Bybit ai_trader». Источник: chat-thread `[fx_ai_oil_trader_mvp_44cb1e89]`
и план `/.cursor/plans/fx_ai_oil_trader_mvp_44cb1e89.plan.md`.

**Что сделано.** Создан изолированный пакет `src/fx_ai_trader/` — третий
бот в репо (после `fx_pro_bot/` advisor и `bybit_bot/`/`ai_trader/`).
Phase 1: paper-mode на XAUUSD (spot gold CFD) + BZ=F (Brent oil →
cTrader `BRENT`), dual-timer 15+5 мин, DeepSeek-V4 через
Anthropic-compatible endpoint.

### Структура пакета

```
src/fx_ai_trader/
├── __init__.py / __main__.py
├── app/main.py                  # dual-timer 15+5 cycle (full + review)
├── config/settings.py           # AI_FX_TRADER_* env-prefix, paper-by-default
├── llm/
│   ├── prompts.py              # SYSTEM_PROMPT FX + multi-dim sentiment блок
│   └── client.py               # shim над ai_trader.llm.client (DeepSeek-V4)
├── trading/
│   ├── client_adapter.py       # CTraderFxAdapter поверх fx_pro_bot.CTraderClient
│   ├── token_lock.py           # race-safe OAuth refresh (fcntl.flock + re-check)
│   ├── executor.py             # Pydantic schema + parse_action + apply_action
│   ├── paper_reconcile.py      # SL/TP touch detection через M1 свечи
│   └── context.py              # collect + format full/review
├── news/
│   ├── rss.py                  # ForexLive/Investing/OilPrice/Kitco + double filter
│   └── eia.py                  # EIA Open Data weekly petroleum
├── state/db.py                 # AiFxTraderStore (positions/decisions/daily_pnl)
├── safety/killswitch.py        # FX-параметры + correlation-aware checks
└── analysis/indicators.py      # shim над ai_trader.analysis.indicators
```

### Ключевые решения и source-of-truth

**Изоляция от существующего Advisor (cTrader Gold-ORB):**

- Advisor торгует **GC=F futures** (`scalping_gold_orb.py`, MAX_POSITIONS=10),
  AI-агент торгует **XAUUSD spot** + **BRENT** — это другие cTrader
  `symbolId` → полная broker-side изоляция, никаких пересечений на
  margin pool.
- Все наши ордера маркируются `label="ai-fx-trader"`. Advisor ставит
  `label="fx-pro-bot"`. Reconcile у обоих фильтрует по своему label —
  никто не закроет чужую позицию как «orphan». Спецификация cTrader OpenAPI
  forum 41177 + FAQ подтверждают что `label` — string ≤100 chars,
  устойчивый к рестартам и виден в `ProtoOAReconcileRes.position`.
- Отдельная БД: `/data/fx_ai_trader.sqlite` (vs `stats.sqlite` advisor'а).
- OAuth token-store шарится: `/data/ctrader_tokens.json` — общий demo-аккаунт
  FxPro. Concurrent refresh защищён через `fcntl.flock` advisory lock с
  re-check после acquire (см. ниже Risk 4).

**Multi-dim sentiment block (Risk 1 mitigation):**

- Промпт требует от LLM 5-мерный sentiment per news item:
  `relevance / polarity / intensity / uncertainty / forwardness ∈ [0,1]`
  + `aggregate_uncertainty` per cycle.
- `executor.parse_action(max_uncertainty=0.7)` — gate: при
  `aggregate_uncertainty > 0.7` open-decisions reject'ятся ДО broker
  call'а. Cost-savings + предотвращает entry на low-conviction LLM.
- Sentiment JSON пишется в `decisions.sentiment_json` для post-hoc
  валидации калибровки uncertainty.

**Pydantic schema validation:**

- `OpenAction / CloseAction / HoldAction` — все три варианта решения.
- Структурные ошибки (неверный тип, отсутствующее поле, side ≠
  BUY/SELL, lots ≤ 0, lots > 10, uncertainty out of [0,1]) ловятся
  ДО apply-стадии (Risk 1: schema at agent boundaries — Tauric Research
  PR #458).

**KillSwitch — FX-параметры + correlation-aware:**

| Параметр | Значение | Обоснование |
|---|---|---|
| `MAX_DAILY_LOSS_USD` | 150 | `$25 risk × 6 убыточных = $150` |
| `MAX_TOTAL_LOSS_USD` | 300 | halt эксперимента (60% капитала) |
| `MAX_POSITIONS` | 3 | для 2 инструментов |
| `MAX_POSITIONS_PER_SYMBOL` | 2 | защита от over-allocation (Janus Henderson 2026) |
| `RISK_PER_TRADE_USD` | 25 | от virtual capital $500 (5%) |
| `MAX_LOT_SIZE` | 0.50 | hard cap, защита от extreme-волатильности |
| `CORRELATION_HAIRCUT` | 0.7 | gold↔oil correlated в risk-off (finaur 2026) |

Дополнительные guards:
- **Same-direction concentration**: 3-я позиция в одну сторону по
  correlated assets (XAUUSD + BZ=F = correlated group) REJECT'ится.
- **Correlation-haircut**: 2-я позиция в ту же сторону по correlated set
  → `volume_lots × 0.7` (research: finaur «correlation spikes in crisis»).

**Paper-mode reconcile через M1 свечи:**

В paper-mode broker не отрабатывает SL/TP (мы не ставили реальный ордер).
`paper_reconcile.reconcile_paper_positions()` тянет M1 свечи с момента
`opened_at` и для каждого бара проверяет touch SL/TP. Gap-day
(одновременный пробой SL и TP в одном баре) → preferred SL (conservative,
worst execution assumption).

NB: используются M1 свечи **самого cTrader** (`get_trendbars` period=1),
а не yfinance — это уменьшает «фантомные» touch'и из-за wide-spread
wicks. Weekend gaps корректны (бары просто отсутствуют в этом промежутке).

### Risks & mitigations (community-verified)

#### Risk 1 — LLM hallucinations / invalid output

**Mitigation:** Pydantic schema at boundary + multi-dim sentiment +
uncertainty gate `> 0.7 → reject open`.

**Source:**
- Tauric Research/TradingAgents PR #458 «schema at agent boundaries».
- Medium 2026 «Hallucination prevention out of the prompt and into the
  schema».
- arxiv 2603.11408 «Beyond Polarity: Multi-Dimensional LLM Sentiment
  Signals for WTI Crude Oil Futures Return Prediction».

#### Risk 2 — RSS / news noise

**Mitigation:**
- Source weights (OilPrice/Kitco = 0.7, ForexLive/Investing = 1.0).
- 12h time-window filter.
- Dedupe by normalized title (lowercase + strip punctuation, не по URL).
- Двойной keyword filter (gold-set + oil-set, news может попасть в оба).

**Source:**
- stock-market.live 2026 «Build a News-Driven Trade Bot — selectivity > speed».
- stockalpha.ai sentiment guide — entity extraction + time-window.

#### Risk 3 — cTrader rate-limit (50 req/s non-historical, 5 req/s historical)

**Mitigation:**
- Реюз `fx_pro_bot.trading.client.CTraderClient` — он уже имеет
  heartbeat 8s, smart-reset backoff `(5,10,30,60,120,300,900)`,
  `STABLE_UPTIME_SEC=300` (см. `BUILDLOG.md` 06-11.05.2026).
- Один общий клиент на процесс через `CTraderFxAdapter`.
- Bars-запросы только в начале каждого full-cycle (15 мин) + review
  (5 мин) — нагрузка ~5–10 req/cycle, далеко от лимитов.

**Source:** cTrader Open API docs `help.ctrader.com/open-api/connection/`,
community forum 45954 (silent token rotation).

#### Risk 4 — OAuth refresh race condition (shared token-store с Advisor)

**Mitigation:** `fx_ai_trader/trading/token_lock.py` —
`fcntl.flock` advisory exclusive lock + re-read под локом + atomic
write через `os.rename`. Если другой процесс уже refresh'нул токен
пока мы ждали — используем on-disk значение, refresh НЕ вызывается
(single-use refresh_token cTrader защищён).

**Source:**
- Coder PR #22904 «singleflight + optimistic locking».
- Nango blog «How to handle concurrency with OAuth token refreshes».
- openai/codex issue #10332 «file lock + re-check pattern».

#### Risk 5 — Paper-mode статистическая значимость

**Mitigation:** Phase 1 = ≥14 дней paper-observation (≥30 трейдов,
p-value < 0.05). LIVE не включаем без подтверждённой стабильности
парсера, калибровки uncertainty-gate, в разных режимах рынка
(тренд/флет/новости). Правило `sample-size.mdc` соблюдено.

**Source:**
- NexusTrade 2026 «30–90 days paper trading before live».
- Kiploks «weeks, not hours» calibration guide.
- `.cursor/rules/sample-size.mdc` (this repo).

#### Risk 6 — Gold instrument overlap с Advisor (GC=F)

**Mitigation:** Advisor торгует **GC=F futures** (cTrader: `GOLD_*` —
front-month contract), AI-агент торгует **XAUUSD spot** — это разные
символы на cTrader, разные `symbolId`, **независимый margin pool на
аккаунте**. На FxPro demo они существуют параллельно как разные
instruments. Дополнительно `label`-isolation на уровне reconcile.

**Source:**
- cTrader OpenAPI symbols catalog (`get_symbols` + `get_symbol_details`).
- FxPro symbols specification.

#### Risk 7 — Gold↔Oil correlation в risk-off

**Mitigation:**
- `CORRELATION_HAIRCUT = 0.7` на 2-ю позицию в одну сторону.
- Same-direction concentration check блокирует 3-ю позицию.
- LLM-промпт явно предупреждает: «moderately correlated during risk-off
  (both up on geopolitical, both down on USD strength)».

**Source:**
- finaur «Asset Correlation in Times of Crisis» 2026.
- Janus Henderson «Building smarter commodity exposure» 2026.

### Тесты

`tests/test_fx_ai_trader.py` — **34 теста, все зелёные**.
Polный pytest репо: **513 / 513 passed**.

Покрытие:
- `TestParseActionSchema` (15 тестов): hold + sentiment, open XAUUSD,
  open BRENT, high-uncertainty gate, custom threshold, invalid side,
  unknown symbol, negative lots, lots > 10, close, review-mode open
  reject, markdown fence, extra commentary, no JSON.
- `TestKillSwitch` (8): correlated set, empty store, max positions,
  per-symbol cap, correlation-haircut, same-dir concentration,
  opposite-dir allowed, daily loss block.
- `TestPaperReconcile` (6): long SL/TP, long no-touch, short SL/TP,
  gap-day prefer-SL.
- `TestVolumeRounding` (1): round-down к step + clamp [min,max].
- `TestTokenLockRecheck` (2): fresh-token no refresh, concurrent
  re-check (другой процесс перезаписал → refresh не вызван).
- `TestSettings` (2): defaults, db_path.

### Deploy (Phase 1 paper)

`Dockerfile.fx-ai-trader` (Python 3.12-slim, `pip install .`, entry
`python -m fx_ai_trader`) + сервис `fx-ai-trader` в `docker-compose.yml`
с bind-mount `./data:/data` (общий с Advisor для token-store).

Дефолты в compose:
- `AI_FX_TRADER_TRADING_ENABLED=false` (paper)
- `AI_FX_TRADER_SYMBOLS=XAUUSD,BZ=F`
- `AI_FX_TRADER_POLL_INTERVAL_SEC=900` / `REVIEW=300`

Шарит `DEEPSEEK_API_KEY` с `ai-trader` (один аккаунт-level rate-limit).

**Файлы:**
- new: `src/fx_ai_trader/` (полное дерево, 13 файлов)
- new: `tests/test_fx_ai_trader.py` (34 теста)
- new: `Dockerfile.fx-ai-trader`
- modified: `docker-compose.yml` (добавлен сервис `fx-ai-trader`)
- modified: `.env.example` (документация env'ов FX AI Trader)
- modified: `pyproject.toml` (`fx-ai-trader` entry-point + hatch packages)
