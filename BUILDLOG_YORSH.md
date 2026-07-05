# Build Log — yorsh_bot

Изолированный сканер «ёрш»-паттернов MEXC/Bitget (data-only, Фаза 1).
ТЗ: `docs/TZ_YORSH_SCANNER.md`. Родительские: `docs/RESEARCH_SCAM_TOKEN_SCALP.md`,
`docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md`.

По правилу `buildlog.mdc` этот лог также **поглощает research-артефакты**
(отдельного `BUILDLOG_RESEARCH.md` нет): определение единицы связки = setup-class,
числовой критерий перехода к Фазе 3, результаты калибровки M6 — пишутся
отдельной секцией «Research» **до начала сбора статистики Фазы 2**.

---

## 2026-07-05

### M0: скелет пакета + Docker + compose
`pending-commit`

Скелет изолированного бота `src/yorsh_bot/` по паттерну `scalp_bot`:
пустые модули с docstring (exchanges/data/analysis/report/replay), рабочие
`config/settings.py` (все env из ТЗ раздел 4), `state/db.py` (полная SQLite-
схема: densities/spurt_events/candidates/universe_log/collector_health/meta
+ UNIQUE-индекс на active-кандидата), `app/main.py` (heartbeat-заглушка).
Модуля `trading/` нет вообще (Фаза 1 = data-only). Интеграция: `pyproject.toml`
(wheel-targets + CLI `yorsh-bot`), `Dockerfile.yorsh-bot`, сервис `yorsh-bot`
в `docker-compose.yml` (named volume `yorsh_data:/data`). Тесты:
`tests/test_yorsh_bot.py` — settings/defaults/env, схема БД, meta-хелперы,
UNIQUE-кандидат, изоляция импортов (subprocess, не тянет чужие пакеты),
отсутствие trading-модуля.

**Файлы:** `src/yorsh_bot/**`, `tests/test_yorsh_bot.py`, `pyproject.toml`,
`Dockerfile.yorsh-bot`, `docker-compose.yml`, `BUILDLOG_YORSH.md`

### M1: MEXC-коллектор + локальная книга + recorder
`pending-commit`

Реализован по официальной доке MEXC (api-docs.mdc — все константы со ссылками
в docstring): WS `wss://wbs-api.mexc.com/ws`, ≤30 подписок/соединение, PING
keepalive `{"method":"PING"}` (20с < 60с server-disconnect), каналы
`spot@public.aggre.deals.v3.api.pb@100ms@{sym}` (trades) + `…depth…`
(fromVersion/toVersion), REST snapshot
`api.mexc.com/api/v3/depth?symbol=&limit=5000` → lastUpdateId. Данные —
protobuf; `exchanges/mexc_pb.py` строит `PushDataV3ApiWrapper` и nested-сообщения
через runtime-descriptor (`descriptor_pb2`+`message_factory`, protobuf 3.20.1 —
без protoc/grpc_tools, схема из github.com/mexcdevelop/websocket-proto).
`data/orderbook.py`: MEXC range-процедура (snapshot → buffer pre-snapshot →
discard to_version≤last → from_version==last+1 иначе reinit; Bitget seq-mode
параметризован `version_mode`). `data/recorder.py`: партиции
`raw/mexc/{sym}/{date}/{HH}.jsonl.gz`, часовая ротация, retention по дате
партиции + cap по GB (удаляем старые). `app/main.py`: коллектор на
`YORSH_SYMBOLS_STATIC` (временная до M3), reconnect с capped exponential
backoff. Deps: `aiohttp>=3.9`, `protobuf>=3.20`.

Тесты (`tests/test_yorsh_orderbook.py`, 15 шт): MEXC range-процедура
(snapshot+diff, gap→reinit, stale skip, sequential, size=0 remove,
pre-snapshot buffer, zero-size в snapshot), Bitget seq-mode, recorder
(партиция, ротация по часу, retention по дате, cap по размеру — несжимаемые
данные), protobuf round-trip (depth/deals/empty-body). Live WS/REST в
sandbox не проверяются — сеть ограничена; логика покрыта офлайн-тестами.

**Файлы:** `src/yorsh_bot/exchanges/{mexc.py,mexc_pb.py}`,
`src/yorsh_bot/data/{orderbook.py,recorder.py}`, `src/yorsh_bot/app/main.py`,
`src/yorsh_bot/exchanges/base.py`, `tests/test_yorsh_orderbook.py`,
`pyproject.toml`

### M2: Bitget-коллектор + tests
`pending-commit`

Реализован по официальной доке Bitget (api-docs.mdc): WS
`wss://ws.bitget.com/v2/ws/public`, heartbeat строкой `"ping"` каждые 30с
(server disconnect через 2мин без ping), ≤10 msg/сек, ≤50 каналов/соединение
(рекомендация), 240 подписок/час. Каналы SPOT: `books` (200ms, первый push
`action:"snapshot"` → затем `action:"update"`; `seq`+`pseq` внутри `data[0]`,
gap = `pseq != last_seq`) + `trade` (`data:[{ts,price,size,side:"buy"|"sell",
tradeId}]`). Данные — JSON (проще MEXC protobuf). Reinit при gap — resubscribe
канала books (fresh snapshot), drainer-таск. REST orderbook
`api.bitget.com/api/v2/spot/market/orderbook` — только запись (его `ts` не в
WS seq-пространстве → книгу им не инициализируем, в отличие от MEXC REST).
`BookSnapshot.source` ("rest"|"ws_books") разделяет применение к живой книге.
`app/main` — per-exchange: MEXC range-mode + REST-snapshot=init; Bitget
seq-mode + WS-snapshot=init + REST=record-only. Changelog: `checksum` удалён
в мае 2026 — не используем, верим `seq`/`pseq`.

Тесты (`tests/test_yorsh_bitget.py`, 10 шт): constructor-лимит (25×2≤50),
pong/ack/non-json игнор, books snapshot (source=ws_books, seq из data[0]),
books update (DepthDiff seq/pseq), empty-data, trade snapshot+update
(side buy/sell, ts ms→s), unknown-side normalize, request_reinit. Live
WS/REST в sandbox не проверяются.

**Файлы:** `src/yorsh_bot/exchanges/bitget.py`, `src/yorsh_bot/exchanges/base.py`,
`src/yorsh_bot/app/main.py`, `tests/test_yorsh_bitget.py`

### M3: universe-менеджер + SubscriptionSupervisor + tests
`pending-commit`

Динамическая вселенная подписок (продакшн-режим; `YORSH_SYMBOLS_STATIC` пуст).
`data/universe.py`: REST-fetchers по доке — MEXC
(`api.mexc.com/api/v3/exchangeInfo` base/quote + `/ticker/24hr` quoteVolume,
null → fallback volume×lastPrice) и Bitget (`api/v2/spot/public/symbols`
baseCoin/quoteCoin + `/api/v2/spot/market/tickers` quoteVolume). Чистая логика:
`filter_universe` (quote=USDT, оборот в `YORSH_MIN/MAX_24H_VOLUME_USD`,
blacklist мейджоров по base-asset, **protected** = active-кандидаты БД —
добавляются без фильтра, но только если символ есть в REST-листе),
`diff_subscriptions`, `batch_by_conn` (MEXC 15/conn = 30 подписок/2 канала;
Bitget 25/conn = 50 каналов). `UniverseManager.refresh()` — diff против
current, лог add/remove в `universe_log` через callback; protected из
`get_protected` (БД).

`data/supervisor.py`: `SubscriptionSupervisor` — по одному коллектору-таску
на батч (соединение); на `reconcile(exchange)` стартует новые / перестраивает
изменившиеся батчи (cancel+новый клиент) / останавливает пропавшие индексы.
Ребилд на ротации допустим (reconnect ≤24ч всё равно случается, ротация раз
в 6ч). `run_loop` глушит ошибки reconcile (сетевые) с warning-логом.

`app/main.py` переведён на двухрежимный запуск: `YORSH_SYMBOLS_STATIC` →
статические подписки (тесты/отладка); пусто → `UniverseManager`+
`SubscriptionSupervisor` с real client-factory (MexcSpotClient/BitgetSpotClient
per батч, per-batch books/recorder/health-колбэки вынесены в `_build_client`).
Protected тянутся из `candidates WHERE status='active'`. Recorder — один на
биржу (shared по батчам, single event loop).

Тесты (`tests/test_yorsh_universe.py`, 17 шт): filter (quote/volume range,
blacklist мейджоров, base-extraction, protected kept despite volume+blacklist,
protected not-in-rows skipped, dedup), diff (add/remove/no-change), batching
(split/empty/invalid-per-conn), manager refresh (add+log, diff на ротации,
protected из БД, batches≤per-conn, refresh пробрасывает ошибку / run_loop
глушит), blacklist-contents. `tests/test_yorsh_supervisor.py` (5 шт): один
батч под лимитом, split на 2 батча, rebuild изменившегося батча (cancel+
новый), unchanged-батч не трогается, пропавшие батч-индексы останавливаются.
Mock-client-factory (блокирующий `run()` до cancel). Live REST в sandbox
не проверяется — fetcher инжектируется synthetic.

**Файлы:** `src/yorsh_bot/data/{universe.py,supervisor.py}`,
`src/yorsh_bot/app/main.py`, `tests/test_yorsh_universe.py`,
`tests/test_yorsh_supervisor.py`

### M4: density-tracker + spoof/iceberg-фильтр + tests
`pending-commit`

`analysis/density.py` — `DensityTracker` на потоке L2-диффов + трейдов для
одного (exchange, symbol). Детекция «плотностей»: уровень с размером ≥
`YORSH_DENSITY_KRATNOSTI` × медиана размеров стороны (поддерживает тонкую
копию `_sizes[side][price]` из диффов). Жизненный цикл: появление (`open`),
persistence (`last_seen - first_seen`), partial fills (трейд по цене
плотности → `partial_fill_vol += size`; buy-trade ест ask-плотность,
sell-trade — bid), pull (size→0 при `|best - price| ≤
density_approach_ticks × tick` → `pull_count++`), refill (размер вернулся в
окне `density_refill_window_sec` → `refilled++`), move (пропавшая
vanished-но-не-closed плотность всплыла на новой цене в `move_window` →
`moved++`, оригинал закрывается). `flush(now)` закрывает протухшие
(vanished > `density_gap_close_sec`) с финальным вердиктом.

Вердикты (правила — аудит п.2 «Уточнение под нашу страту», 4 признака;
пороги — стартовые точки, `no-data-fitting.mdc`):
- **spoof**: `pull_count > 0` (снятие при подходе цены) OR `moved > 0` (прыгает).
- **iceberg**: `refilled > 0` OR `partial_fill_vol ≥ mismatch_ratio × peak`
  (cumulative traded >> visible).
- **genuine**: `persistence ≥ MIN_PERSISTENCE_SEC` AND `partial_fill_vol > 0`
  AND `moved == 0` AND `pull_count == 0`.
- **unknown**: иначе.

`make_db_persistor(db)` — колбэк `DensityEvent → SQLite densities`
(insert на open с возвратом `db_id` через mutable-поле события, update на
update/close). В `state/db.py` добавлены `insert_density`/`update_density`.
В `config/settings.py` — новые пороги (refill/move windows, approach_ticks,
mismatch_ratio, gap_close) с пометкой «стартовая точка, калибровать».

Тесты (`tests/test_yorsh_density.py`, 7 шт): genuine (ask-стенка стоит
>60с, partial fill, цена откатилась далеко → cancel без pull → genuine),
spoof-pull (снятие при подходе best-price → pull_count → spoof), iceberg-
refill (размер восстановлен в окне → iceberg), iceberg-mismatch (cumulative
traded 5× peak без refill → iceberg), spoof-move (пропала + всплыла на новой
цене в move_window → moved → spoof), DB-персистор genuine (запись в
densities: verdict/peak/partial_fill), DB-персистор spoof (pull_count
записан). Synthetic-диффы, без сети.

**Файлы:** `src/yorsh_bot/analysis/density.py`,
`src/yorsh_bot/state/db.py`, `src/yorsh_bot/config/settings.py`,
`tests/test_yorsh_density.py`

### M5: ёрш-сканер + daily-отчёт + tests
`pending-commit`

`analysis/prints.py` — `SpurtDetector` (оконный, на потоке трейдов):
прострел = движение ≥ `YORSH_SPURT_MIN_AMPLITUDE_PCT` за окно (стартовое
60с, калибровать), направление up/down по знаку, триггер-принты =
доминирующего направления, cooldown на повторную эмиссию. Кластеризация
принтов по размеру `cluster_prints_by_size` — гистограммная (сортировка +
группировка с max/min ≤ 1.2 = 20% variance), НЕ DBSCAN: признак 1-D,
sklearn не тащим (ТЗ), детерминированно O(n log n) — эквивалент DBSCAN по
1-D size. «Одинаковый принт» = крупнейший кластер, медианный размер =
`trigger_cluster_size`.

`analysis/yorsh_scanner.py` — `YorshScanner.evaluate`: 3 проверки (аудит
п.1, признаки 1–3):
  (а) «одинаковый принт»: крупнейший size-кластер покрывает ≥50% триггеров;
  (б) repeat-frequency test: интервалы против Пуассон-нуля, p<0.05 =
       under-dispersion (регулярность выше случайной). Статистика
       `Q=(n-1)·s²/x̄ ~ chi²(n-1)` (Cox & Lewis 1966 ch.6, интервальный
       дисперсионный тест); p = `P(Q ≤ q)` (lower tail, регулярность → малое Q).
       chi2-CDF — stdlib через regularized lower incomplete gamma
       (`_gammp`, Numerical Recipes series + Lentz continued fraction), без scipy.
  (в) привязка к density: `densities_near` (genuine/iceberg, активная на
       момент старта, цена в пределах `price_tol_pct`%).
Прошедшие все три + ≥4 прострелов → `upsert_candidate` (UNIQUE active на
(exch,sym): старый → closed, новый → active). ВСЕ прострелы → `spurt_events`
(passed_filters 0/1) — для калибровки M6.

`report/daily.py` — CLI `python -m yorsh_bot.report.daily [--date]`:
active-кандидаты, прострелов/сутки, медианная амплитуда, медианное
`revert_ms`, theoretical P&L как **UPPER BOUND** (без exit-slippage/fees/
funding/impact — аудит п.3а, явный warning в выводе; notional — стартовая
точка, sizing Фаза 2).

В `state/db.py` добавлены `insert_spurt`/`upsert_candidate`/`spurts_for_day`/
`active_candidates`/`densities_near`.

Тесты (`tests/test_yorsh_scanner.py`, 13 шт): chi2-CDF (known values, edge
0/large), repeat-frequency (regular → p<0.05, пуассон-подобные → p>0.05,
<3 интервалов → None), cluster_prints (группировка same-size, empty),
SpurtDetector (emit on amplitude, no-emit below threshold, down-direction),
scanner (passed+candidate записан + все 5 spurts в БД с density_id, fail
без density, fail без regularity), daily-report (seeded — candidate в
выводе, UPPER BOUND $0.55, median amplitude/revert_ms). Synthetic, без сети.

**Файлы:** `src/yorsh_bot/analysis/prints.py`,
`src/yorsh_bot/analysis/yorsh_scanner.py`, `src/yorsh_bot/report/daily.py`,
`src/yorsh_bot/state/db.py`, `tests/test_yorsh_scanner.py`
