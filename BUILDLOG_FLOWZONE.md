# BUILDLOG — flowzone_bot

Журнал сборки order-flow бота `flowzone_bot` (Bybit, Auction Market Theory +
Volume Profile + Order Flow). Канон стратегии — `STRATEGY_FLOWZONE.md` (ролик
<https://youtu.be/06R-ebyOhDI>), тех-задание — `TASKSPEC_FLOWZONE.md`.

Формат: записи группируются по дням (новые сверху). Для багов: симптом →
причина → решение. Для фич: что добавлено и на что влияет.

---

## 2026-06-17

### feat(universe): переключатель отбора монет rvol/momentum + тестово на momentum
`<pending commit>`

**Цель пользователя**: протестировать на flowzone метод подбора монет «как в
ролике» SerCrypto (<https://youtu.be/gCgYS-CsGWc>): ТОП по 24h росту/падению +
порог оборота, без анти-памп кэпа. Аналогично переключателю в scalp_bot
(sweep_fade). Сама стратегия flowzone (footprint/absorption/zone) НЕ меняется —
меняется только список символов (чистый A/B оси отбора).

**Что добавлено**:
- `src/flowzone_bot/data/momentum_universe.py` — момент-селектор (параллельная,
  изолированная от scalp_bot копия): ранг по МОДУЛЮ `price24hPcnt` (топ мувёров),
  hard-фильтр по `turnover24h`; опции `min_abs_change_pct`/`max_spread_bps`/
  `direction`. Анти-памп range-cap НЕТ (в отличие от RVOL-селектора).
- `config/settings.py`: `universe_method` ("rvol" default | "momentum"),
  `momentum_min_turnover_usd` (50M), `momentum_min_change_pct` (0),
  `momentum_max_spread_bps` (0=выкл), `momentum_direction` ("both").
- `app/main.py`: `_select_universe` ветвится по методу; лог пишет `метод=...`.
  RVOL-путь не тронут.
- `docker-compose.yml`: дефолт flowzone-bot переключён на `momentum`.

**Канон-оговорка (STRATEGY §6.1)**: footprint/absorption читаемы на ЛИКВИДНОСТИ
(канон на NQ). Momentum тянет «то что стреляет», в т.ч. тонкие памп-альты без
анти-памп кэпа — на них order-flow шумит. Осознанный риск форвард-теста; вывод
«лучше/хуже RVOL» — только n≥100 (`sample-size.mdc`), не по первым сделкам
(`no-data-fitting.mdc`). Поле `price24hPcnt` из Bybit get_tickers
(<https://bybit-exchange.github.io/docs/v5/market/tickers>, `api-docs.mdc`).

**Тесты**: +3 (`tests/test_flowzone_bot.py`): ранг по |24h| + фильтр оборота,
direction up/down + отсутствие анти-памп кэпа, дефолт `universe_method`. Всего
41, все зелёные.

**Файлы:** `src/flowzone_bot/data/momentum_universe.py`,
`src/flowzone_bot/config/settings.py`, `src/flowzone_bot/app/main.py`,
`docker-compose.yml`, `tests/test_flowzone_bot.py`

### fix(executor): сведение P&L на частичных закрытиях (DB == Bybit closedPnl)
`<pending commit>`

Симптом: при сверке статы локальная БД показывала net +$28.79, а Bybit
`closedPnl` (ground truth, `stats-collection.mdc`) — +$9.42 (+$19.38 завышения,
основной вклад — ZECUSDT). 6 сделок зависли `provisional` навсегда.

Причина (цепочка на партиалах, канон §5.3 частичная фиксация):
1. **REST-матч не ловит партиал.** `closed_pnl_detail` матчит ОДНУ запись по
   `closedSize ≈ qty`. Bybit же пишет ОТДЕЛЬНУЮ `closedPnl`-запись на каждое
   частичное закрытие (цель 1) + остаток (цель 2) — ни одна не равна полному
   объёму → матч `None` → REST-фолбэк не срабатывал → сделка вечно provisional.
2. **Оценка provisional завышала.** При закрытии позиции, если WS-филлы ещё не
   собрались, `_realized_or_estimate` считал `taker_pnl` на ПОЛНЫЙ объём по
   финальной (более выгодной, цель 2) цене — игнорируя, что половина уже закрылась
   на цели 1 (менее выгодной). Для лонга цель2>цель1 → завышение профита.

Решение:
- `client.closed_pnl_position()` — суммирует ВСЕ `closedPnl`-записи позиции в окне
  `[ts_open, ts_close+180с]`, фильтр по `avgEntryPrice`, и принимает сумму ТОЛЬКО
  если `Σ closedSize ≈ qty` (вся позиция собрана; иначе `None` — не выдумываем,
  `no-data-fitting.mdc`). Окно изолирует сделку: «один сетап на символ» + cooldown.
- `_rest_finalize`: точечный матч → фолбэк на `closed_pnl_position` (партиалы).
- `_realized_or_estimate`: оценка = реальный зафиксированный партиал (из филлов) +
  `taker_pnl` на ОСТАТОК объёма (не на полный) → транзиентная оценка не завышает.

closedPnl уже net (комиссии+funding) — приоритетнее расчётного `(exit−entry)×qty`
в БД (`stats-collection.mdc`). Тесты: +5 (`closed_pnl_position` sum/incomplete/
entry-filter, estimate-remaining, rest-finalize-fallback). Всего 38, все зелёные.

**Файлы:** `src/flowzone_bot/trading/client.py`,
`src/flowzone_bot/trading/executor.py`, `tests/test_flowzone_bot.py`

## 2026-06-16

### fix(killswitch): max_trades_per_hour ≤0 = ВЫКЛ (rate-limit не канон, режет reload)
`<pending commit>`

Симптом: на чистом даунтренде NEAR бот хотел перезаряжаться (reload, канон §5.3),
но упирался в `gate block: rate-limit ≥ 5/h` — generic анти-overtrading лимит из
модели scalp (TASKSPEC §6 п.8), которого НЕТ в каноне flowzone. Лимит резал
ключевую механику стратегии и занижал выборку форвард-теста.

Причина-2 (баг): `can_open` блокировал при `trades_since ≥ max_trades_per_hour`
без guard на ≤0 — постановка лимита в 0 заблокировала бы ВСЕ входы (0 ≥ 0).

Решение: `max_trades_per_hour ≤0 = выключен` (как у loss-лимитов в `is_killed`);
аналогичный guard на `max_open_positions`. На demo выставлен
`FLOWZONE_MAX_TRADES_PER_HOUR=0` — темп входов держат `max_open_positions=2` +
per-symbol cooldown'ы (signal 60с / reload 10с). Решение data/canon-driven (reload
— инвариант канона), не подгонка под P&L (выборка 17 сделок = шум, sample-size).

**Файлы:** `src/flowzone_bot/safety/killswitch.py`,
`src/flowzone_bot/config/settings.py`, `tests/test_flowzone_bot.py`

### Фаза 6 — session gate (London/NY) + sizing + лимиты
`<pending commit>`

Гейт активных сессий (канон §6.1). Sizing (риск-базированный, Tharp) и лимиты
(killswitch: дневной/совокупный + кэп позиций + rate-limit) уже реализованы в
фазах 1/4 — здесь добавлен только session gate.

- **analysis/session.py** — `parse_windows` ("HH:MM-HH:MM,…" → часы UTC),
  `in_session` (момент в активном окне; поддержка окна через полночь; пустые окна
  → круглосуточно). Окна — каноничные FX-сессии (BIS/Investopedia): London
  ≈07:00-16:00 UTC, NY ≈12:00-21:00 UTC.
- **config/settings.py** — `session_gate_enabled=true`,
  `session_windows_utc="07:00-16:00,12:00-21:00"` (London+NY).
- **app/main.py** — вне активной сессии входы не сканируются (§6.1, §8 «вне
  сессий методика не применяется»); статус сессии в heartbeat (`session=active/
  closed`).
- **tests** — +3: parse_windows, in_session (London/NY/перекрытие/вне/пустые
  окна), окно через полночь. Всего 32 flowzone — зелёные.

Сверка с каноном: торговля только в London/NY (§6.1), вне сессий — нет входов
(§8). Окна операционные (через env), не торговый эдж. Sizing/лимиты — Tharp 2007
+ mainstream risk-management (как в TASKSPEC §6 п.8).

**Файлы:** `src/flowzone_bot/analysis/session.py`,
`src/flowzone_bot/config/settings.py`, `src/flowzone_bot/app/main.py`,
`tests/test_flowzone_bot.py`

### Фаза 5 — trade manager (swing-цели, частичная фиксация, reload)
`<pending commit>`

Управление сделкой по канону §5.3, §8: цель = ближайший swing-point, частичная
фиксация на цели 1 + перевод стопа в безубыток, перезарядка (reload).

- **analysis/swings.py** — `find_swings` (фрактал Bill Williams «Trading Chaos»
  1995: бар-экстремум выше/ниже N баров с каждой стороны; 2 бара = канонический
  фрактал, инвариант), `nearest_swing_target` / `swing_targets` (ближайшая и
  список целей по тренду). Чистые функции.
- **analysis/strategy.py** — `evaluate` принимает `swings`: цель 1 = ближайший
  swing (канон §5.3), цель 2 = следующий (частичная фиксация). Фолбэк на VP-
  структуру (POC/VA) если swing-целей нет. `Signal.tp2_level` добавлен.
- **trading/executor.py** — `partial_exchange_tp`: биржевой TP = цель 2 (финал)
  при включённой частичной фиксации, иначе цель 1. Биржа ВСЕГДА держит SL+TP
  (безопасно при падении бота). `_maybe_partial`: на цели 1 закрывает долю
  reduce-only (`close_market`) + переводит стоп в безубыток, остаток едет на
  цель 2. Partial-филлы атрибутируются к сделке через `_open_trade_for_symbol`,
  net считается по сумме всех закрытий. `last_win_ts`/`_note_close` — учёт
  выигрышных закрытий для reload.
- **app/main.py** — `_swings_for` (klines M5 с TTL-кэшем `swing_cache_sec`, без
  клиента → пусто → VP-фолбэк), передача swings в `evaluate`. Reload: после
  недавнего выигрыша по символу — короткий `reload_cooldown_sec` вместо
  `signal_cooldown_sec` (перезарядка на следующей зоне по тренду).
- **config/settings.py** — `swing_left/right=2` (фрактал Уильямса, инвариант),
  `swing_kline_interval="5"` (M5, §6.3), `swing_kline_limit=200`,
  `swing_cache_sec=60`, `partial_fraction=0.5` (нейтральная доля),
  `reload_cooldown_sec=10`.
- **tests** — +5: фрактал Уильямса (пик/впадина), края не классифицируются,
  nearest/list swing-целей (шорт/лонг), evaluate берёт swing-цель поверх VP +
  цель 2, решение `partial_exchange_tp`. Всего 29 flowzone, 1303 по репо —
  зелёные.

Сверка с каноном: цель = ближайший swing (§5.3), частичная фиксация (§8), стоп
в безубыток после частичной, reload на след. зоне по тренду (§5.3) — всё
соответствует STRATEGY. Параметры: фрактал Уильямса 2 бара (канон), M5 (§6.3),
доля 0.5 нейтральная. Не подгонка под P&L.

**Файлы:** `src/flowzone_bot/analysis/{swings,strategy}.py`,
`src/flowzone_bot/trading/executor.py`, `src/flowzone_bot/app/main.py`,
`src/flowzone_bot/config/settings.py`, `tests/test_flowzone_bot.py`

### Фаза 4 — zone builder (confluence) + лимит-вход на demo
`<pending commit>`

Собран чеклист входа STRATEGY §7 целиком: контекст → зона confluence → подход
цены → absorption → лимитка в зоне со стопом ЗА зоной и структурной целью.

- **analysis/zone.py** — `build_zones`: кандидат-уровни VP (value_area VAH/VAL,
  POC, ledge, delta-печать, big_trades) кластеризуются по близости; зона = кластер
  со score = числом РАЗНЫХ факторов. Конфлюэнс ≥2 (STRATEGY §3.4 «confluence of
  value area high, big trades and delta level… super strong area»; §7 п.3). Зоны
  ТОЛЬКО по направлению аукциона (continuation): шорт reload-ит выше цены, лонг —
  ниже.
- **analysis/strategy.py** — `evaluate`: чистый пайплайн (snapshot + profile +
  context). Шаги: (1) трендовый контекст или None; (2) зоны конфлюэнса; (3) цена
  ДОШЛА до зоны; (4) absorption контр-стороны в окне-бёрсте; (5) Signal с лимиткой
  в зоне, стопом за зоной (+буфер) и структурной целью (ближайший POC / дальняя
  граница VA — swing будет в фазе 5). Геометрия сделки валидируется.
- **trading/executor.py** — `Executor`: риск-сайзинг (qty = risk_usd/|entry−SL|,
  Tharp 2007), PAPER (observe) и LIVE (Bybit demo) режимы. LIVE: LIMIT-вход в зоне
  с биржевыми SL/TP, write-ahead строка БД до ордера, ребракет по avg-fill,
  reduce-only выход, сверка net P&L из приватного WS execution с REST-фолбэком для
  restart-сирот.
- **analysis/context.py** — ИЗМЕНЕНИЕ модели: контекст теперь РЕЖИМ, а не
  мгновенная цена. Раньше требовалась «цена СЕЙЧАС за границей VA» — это ломало
  reload-сценарий (при откате к зоне цена возвращается внутрь VA, тренд «терялся»).
  Теперь тренд определяется по тому, ГДЕ торгуется объём окна (большинство ниже
  VAL → аукцион вниз), что сохраняет направление на откате. Существующие тесты не
  затронуты (в них acceptance совпадал с положением цены).
- **config/settings.py** — `zone_min_confluence=2` (инвариант §3.4),
  `zone_cluster_ticks=5`, `zone_delta_min_frac=0.6`, `sl_buffer_bps=8`,
  `min_sl_bps=10`, `signal_cooldown_sec=60`, `close_notify_fallback_sec=10`.
  `absorption_window_sec` 300→120 (бёрст агрессии у зоны = подмножество footprint-
  окна, короче окна контекста — отделяет триггер от режима).
- **app/main.py** — интеграция в loop: `ingest_executions` (приватный WS) →
  `manage` (сопровождение) → killswitch-гейт → `_scan_signals` (контекст → зона →
  absorption → `on_signal`) с cooldown и one-setup-per-symbol. exec_stream
  поднимается только в LIVE.
- **tests** — +5: confluence {poc,delta}, side-фильтр continuation, отсев <
  min_confluence, полный чеклист evaluate (trend_down + зона + absorption →
  Signal с валидной геометрией шорта), None в балансе. Всего 24 теста flowzone,
  весь репозиторий 1298 — зелёные.

Сверка с каноном: чеклист §7 (контекст→зона→подход→absorption→вход), confluence
≥2 (§3.4), стоп за зоной (§5.2), цель = структура (§5.3, swing в фазе 5),
continuation-only (§1, §5.4) — соответствует STRATEGY. Пороги: канон (≥2) или
нейтральные/технические (0.6, 5 тиков, 8/10 б.п.), не подгонка под P&L.

**Файлы:** `src/flowzone_bot/analysis/{zone,strategy,context}.py`,
`src/flowzone_bot/trading/executor.py`, `src/flowzone_bot/config/settings.py`,
`src/flowzone_bot/app/main.py`, `tests/test_flowzone_bot.py`

### Фаза 3 — delta-at-price + big-trades detector + absorption-триггер
`<pending commit>`

Order-flow примитивы (STRATEGY §3.2-3.4, §4) — фундамент триггера входа.

- **analysis/orderflow.py** — `size_percentile`, `big_trade_threshold`
  (ОТНОСИТЕЛЬНЫЙ порог = percentile размеров за окно, TASKSPEC §6.3 «не magic-
  number»; min_samples анти-шум по sample-size), `detect_big_trades`, `zone_delta`
  (Σ signed-delta принтов в ценовой полосе зоны — delta-at-price §3.2),
  `detect_absorption` — главный триггер (§4): контр-сторона ≥`min_counter_frac`
  объёма окна агрессировала, ≥1 крупная сделка контр-стороны (deep trade), и цена
  НЕ прошла в её сторону → «failed buyers/sellers» (поглощены).
- **config/settings.py** — `big_trade_pct=0.90` (верхний дециль, institutional-
  tail), `big_trade_min_samples=20`, `absorption_min_counter_frac=0.5`.
- **tests** — +7: percentile/порог, side-фильтр big-trades, zone_delta полоса,
  absorption confirmed (failed buyers/sellers), reject (цена пошла за контр-
  стороной / нет deep-trade).

Сверка с каноном: delta-at-price, big trades, absorption «много агрессии — нет
движения» — каноничные order-flow признаки (STRATEGY §3.2-3.4, §4). Пороги
относительные/нейтральные, не подгонка.

**Файлы:** `src/flowzone_bot/analysis/orderflow.py`,
`src/flowzone_bot/config/settings.py`, `tests/test_flowzone_bot.py`

### Фаза 2 — Volume Profile engine + классификатор контекста аукциона
`<pending commit>`

Добавлены движок объёмного профиля и классификатор контекста (STRATEGY §2-3),
без входов — только логирование контекста в heartbeat.

- **data/aggregates.py** — инкрементальная дневная аккумуляция footprint-профиля
  в `SymbolState` (`idx корзины → (buy, sell)`, якорь UTC-день — канон «Dly Vol.
  Profile», STRATEGY §6.3). Не храним миллионы тиков: профиль копится по
  корзинам. Размер корзины задаётся из `tick_size × vp_bucket_ticks`.
- **analysis/volume_profile.py** — `build_profile` (POC / VAH / VAL),
  `find_hvn_lvn`, `find_ledges`. Value Area = ≈70% объёма вокруг POC, каноничным
  ДВУХРЯДНЫМ расширением (Steidlmayer 1989 / Dalton «Mind Over Markets»: value
  area = 1 std ≈ 70%). Ledge = резкий обрыв HVN→LVN (drop_frac 0.5 — нейтральное
  «вдвое»). Все функции чистые.
- **analysis/context.py** — `classify` → trend_up / trend_down / balance по
  acceptance за границей VA (Dalton: value принят вне прошлой VA). Операционно:
  цена за границей + ≥`accept_frac` (0.5 = большинство) объёма окна за границей.
  В балансе continuation-входов не берём (STRATEGY §2).
- **config/settings.py** — `value_area_pct=0.70` (канон, инвариант),
  `vp_bucket_ticks=10` (разрешение, технический параметр), `context_accept_frac`
  =0.5, `context_accept_window_sec=300`.
- **app/main.py** — `_apply_vp_buckets` (tick_size×N по символам, на старте и
  ротации), `_context_for` + лог контекста в heartbeat (`ctx=…  VA=[..] acc↑↓`).
- **tests/test_flowzone_bot.py** — 12 тестов на честной синтетике: footprint-
  принты/eviction, дневной VP/смена дня, POC/VA на треугольном распределении,
  HVN/LVN, ledge, контекст (trend up/down/balance, фитиль без acceptance).

Сверка с каноном: POC/VAH/VAL/HVN/LVN/ledge, profile из tick-потока, Value Area
70%, acceptance за VA, «в балансе не торгуем» — всё соответствует STRATEGY §2-3,
§6.3 и таблице §9. Пороги — канон (70%) или нейтральные (0.5), не подгонка.

**Файлы:** `src/flowzone_bot/data/aggregates.py`,
`src/flowzone_bot/analysis/{volume_profile,context}.py`,
`src/flowzone_bot/config/settings.py`, `src/flowzone_bot/app/main.py`,
`tests/test_flowzone_bot.py`

### Фаза 1 — каркас (модуль, конфиг, подключение, observe-цикл)
`<pending commit>`

Создан изолированный модуль `src/flowzone_bot/` по образцу `scalp_bot`
(strategy-guard.mdc «изоляция кодовых баз»): свой env-namespace `FLOWZONE_*`,
свой SQLite (`flowzone_bot.sqlite`, volume `flowzone_data`), свой Dockerfile и
сервис в `docker-compose.yml`, свой BUILDLOG. Без LLM.

Что сделано (фаза 1, observe-режим — НИЧЕГО не торгует):

- **config/settings.py** — `FlowzoneSettings` (env `FLOWZONE_*`): инфраструктура,
  Bybit demo, авто-вселенная, риск/лимиты (модель scalp: $1000 / $10 риск на
  сделку, max 2 позиции, 5 сделок/час), Telegram с префиксом `[flowzone]`.
  `trading_enabled=false` по умолчанию (TASKSPEC §1 «Демо сначала»).
- **data/aggregates.py** — `SymbolState` хранит КАЖДЫЙ тиковый принт
  (`TradePrint`: цена/размер/сторона агрессора), а НЕ схлопывает в CVD как scalp.
  Это фундамент под delta-at-price (VP, фаза 2) и big-trades (фаза 3) — ключевая
  адаптация под канон (TASKSPEC §5: «❗дописать delta-by-price»).
- **data/market_stream.py** — публичный WS: `publicTrade` + `orderbook.50`
  (funding/ликвидации канону не нужны — не подписываемся).
- **data/exec_stream.py** — приватный WS execution (источник истины по net P&L).
- **data/universe.py** — авто-селектор scalp переиспользован (TASKSPEC §4):
  turnover/range/spread + intraday RVOL, композитный скор. Калибровка под канон
  (ликвидность критична для footprint) — через env, по факту форвард-теста.
- **trading/client.py** — `FlowzoneBybitClient` (REST: instrument, get_kline,
  get_tickers, place_entry limit/market, SL/TP, closed_pnl с pagination).
- **state/db.py** — `FlowzoneDB` (trades + killswitch-агрегаты, strategy='flowzone').
- **safety/killswitch.py** — дневной/совокупный лимит + кэп позиций + rate-limit
  (на demo выключен, лимиты ≤0).
- **telegram/notifier.py** — исходящие сообщения с префиксом `[flowzone]`.
- **app/main.py** — observe-цикл: авто-вселенная → WS-поток → heartbeat раз в 60с
  (px/число тиков/ob_imbalance по символам). Ротация вселенной раз в 5 мин.
- **Dockerfile.flowzone-bot** + сервис `flowzone-bot` в `docker-compose.yml`
  (volume `flowzone_data`, ключи Bybit с дефолтом на ai_trader, Telegram в чат
  ai_trader). **pyproject.toml**: пакет + CLI-скрипт `flowzone-bot`.

Сверка с каноном: фаза инфраструктурная, торговой логики/порогов нет —
расхождений с STRATEGY_FLOWZONE.md быть не может. VP/контекст/зоны/триггер —
следующие фазы, каждый порог будет обоснован каноном или Steidlmayer/Dalton.

**Файлы:** `src/flowzone_bot/**`, `Dockerfile.flowzone-bot`, `docker-compose.yml`,
`pyproject.toml`, `BUILDLOG_FLOWZONE.md`
