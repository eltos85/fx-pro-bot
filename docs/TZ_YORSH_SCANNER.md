# ТЗ: `yorsh_bot` — изолированный сканер «ёрш»-паттернов (MEXC/Bitget)

> Реализация Фазы 1 (и заготовка Фазы 2) из
> `docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md`. **Без торговли**: data collector +
> сканер повторяющихся прострелов от genuine density на низколиквидных
> спот-парах MEXC и Bitget. Торговое исполнение (Фаза 3) практически
> недостижимо по числовому критерию аудита и в это ТЗ не входит.
>
> Родительские документы:
> - `docs/RESEARCH_SCAM_TOKEN_SCALP.md` — исходная стратегия (Клевцов + уточнения)
> - `docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md` — аудит реализуемости, фазы, критерии

---

## 0. Архитектурное решение: отдельный бот, не стратегия

**Вопрос:** оформлять как стратегию внутри существующего бота или как
отдельный бот?

**Решение: отдельный изолированный пакет `src/yorsh_bot/`** (паттерн
`scalp_bot`/`flowzone_bot`). Обоснование:

1. **Нет подходящего хозяина.** cTrader-боты (`fx_pro_bot`, `fx_ai_trader`,
   `fx_momentum_bot`) — FX/CFD, их `strategies/` завязаны на cTrader-фид.
   Bybit-боты (`scalp_bot`, `flowzone_bot`) жёстко изолированы (см. их
   `__init__.py`) и весь их data-слой построен на `pybit`, который не
   работает с MEXC/Bitget. «Стратегия внутри» невозможна технически.
2. **`strategy-guard.mdc` (изоляция кодовых баз):** торговая логика между
   ботами не шарится; новый класс сетапа требует отдельного одобрения.
   Отдельный пакет исключает случайное влияние на работающие боты.
3. **Проект уже имеет шаблон изоляции:** свой пакет в `src/`, свой
   `Dockerfile.<name>`, свой compose-сервис с named volume, свой префикс
   env-переменных, свой BUILDLOG, тесты в общем `tests/`.
4. **Фаза 1 — вообще не торговля.** Это коллектор+сканер; модуля
   `trading/` в пакете не будет вовсе (не «выключен флагом», а отсутствует).

**Что переиспользуем из проекта:** только архитектурные паттерны
(`data/market_stream.py`, `state/db.py`, `config/settings.py` на
pydantic-settings) — **копируем структуру, не импортируем код** других ботов.

**Имя:** `yorsh_bot` (сетап в research-доке называется «ёрш»). Сервис —
`yorsh-bot`, env-префикс `YORSH_*`, лог — `BUILDLOG_YORSH.md`.

---

## 1. Скоуп

### Входит (Фаза 1)

- WS-коллектор MEXC spot: trades + incremental L2-diff, поддержка локальной
  книги по официальной процедуре (`fromVersion`/`toVersion`, reinit через
  REST snapshot).
- WS-коллектор Bitget spot: канал `books` (snapshot + update, `seq`/`pseq`),
  детекция gap → resubscribe.
- **Запись полного сырого потока с первого дня**: все trades, все L2-диффы,
  периодические снапшоты. Исторический L2 у бирж не купить — лента для
  симуляции Фазы 2 существует только та, которую записали мы.
- Выбор вселенной: REST-список спот-пар, фильтры (не топ-30 CMC, возраст,
  минимальный/максимальный оборот), менеджер подписок с ротацией.
- Density-tracker + spoof/iceberg-фильтр (persistence, partial fills,
  pull-detection, volume/depth mismatch) — по правилам из аудита, п.2.
- Сканер «ёрш»: кластеризация принтов, repeat-frequency test, привязка
  прострела к genuine density; кандидаты и события — в SQLite.
- Отчёт: ежедневная сводка кандидатов (частота прострелов, амплитуда,
  hold time, теоретический P&L **как upper bound** без exit-slippage).

### Входит (Фаза 2, отдельный milestone)

- Replay-симулятор на записанной ленте: exit-машина
  (time-stop / density-routed limit exit / spoof-pull cancel / kill-switch),
  метрики WR/EXP/PF/slippage/net P&L.

### НЕ входит

- Торговое исполнение, ордера, приватные API-ключи с правами trade.
- Гибрид с человеком-исполнителем (отменён — пользователь руками не торгует).
- Любые изменения в существующих ботах, их compose-сервисах и стратегиях.

---

## 2. Структура пакета

```
src/yorsh_bot/
├── __init__.py            # docstring: изоляция — НЕ импортирует другие боты
├── __main__.py            # python -m yorsh_bot
├── app/
│   └── main.py            # runtime loop: universe → collectors → scanner
├── config/
│   └── settings.py        # pydantic-settings, префикс YORSH_
├── exchanges/
│   ├── base.py            # общие типы: Trade, DepthDiff, BookSnapshot, dataclasses
│   ├── mexc.py            # WS+REST MEXC spot (ссылки на офиц. доку в docstring)
│   └── bitget.py          # WS+REST Bitget spot (ссылки на офиц. доку)
├── data/
│   ├── orderbook.py       # локальная книга: apply diff, gap detection, checksum
│   ├── recorder.py        # сырая запись: JSONL.gz, партиции exchange/symbol/date
│   └── universe.py        # выбор пар, фильтры, ротация подписок
├── analysis/
│   ├── density.py         # tracker плотностей: persistence, partial fills, pull
│   ├── prints.py          # кластеризация принтов (DBSCAN по size/price)
│   └── yorsh_scanner.py   # repeat-frequency test, привязка к density, кандидаты
├── state/
│   └── db.py              # SQLite: candidates, densities, spurt_events,
│                           #     universe_log, collector_health, meta
├── report/
│   └── daily.py           # сводка-отчёт (CLI + лог)
└── replay/                # Фаза 2
    └── simulator.py       # replay ленты + exit-машина + метрики
```

Интеграция в проект:

- `pyproject.toml`: пакет в `[tool.hatch.build.targets.wheel]`, CLI
  `yorsh-bot = "yorsh_bot.app.main:run"` в `[project.scripts]`.
- `Dockerfile.yorsh-bot` — по образцу `Dockerfile.scalp-bot`
  (`ENV YORSH_DATA_DIR=/data`, `CMD ["yorsh-bot"]`).
- `docker-compose.yml`: сервис `yorsh-bot`, named volume `yorsh_data:/data`,
  `env_file: .env`, `restart: unless-stopped`.
- `BUILDLOG_YORSH.md` — новый лог, записи по правилу `buildlog.mdc`.
- `tests/test_yorsh_bot.py` (+ `tests/test_yorsh_orderbook.py` и т.д.).

Зависимости: stdlib + `sqlite3` + `websockets` (или `aiohttp`) +
`pydantic-settings`. Без pandas в рантайме (анализ-скрипты могут использовать).
Новые зависимости добавлять в общий `pyproject.toml`.

## 3. Хранение данных

**Сырой поток** (recorder): `{YORSH_DATA_DIR}/raw/{exchange}/{symbol}/{YYYY-MM-DD}/{HH}.jsonl.gz`
— одна строка = одно событие `{ts_exch, ts_local, type: trade|diff|snapshot, payload}`.
Ротация файла по часу. Retention: `YORSH_RAW_RETENTION_DAYS` (default 30) +
жёсткий cap `YORSH_RAW_MAX_GB` (default 20) — при превышении удаляются самые
старые партиции, событие пишется в лог и в `meta`.

**Производные** (SQLite `{YORSH_DATA_DIR}/yorsh_bot.sqlite`):

- `densities(id, exchange, symbol, side, price, first_seen, last_seen,
  peak_size, persistence_sec, partial_fill_vol, pull_count, verdict)` —
  verdict: `genuine | iceberg | spoof | unknown`.
- `spurt_events(id, exchange, symbol, ts, direction, amplitude_pct,
  duration_ms, trigger_print_size, density_id, revert_ms)` — прострелы.
- `candidates(id, exchange, symbol, first_detected, spurts_per_day,
  regularity_pvalue, print_cluster_size, status)` — «ёрш»-кандидаты.
- `universe_log`, `collector_health` (gaps, reconnects, lag) — операционка.
- `meta(key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)` — key/value для
  retention/cap событий (последняя удалённая партиция, текущий размер raw) и
  runtime-мета (версия схемы, last_calibration_ts). Чтение/запись через
  хелперы в `state/db.py`.

## 4. Конфигурация (env, префикс `YORSH_`)

| Переменная | Default | Смысл |
|---|---|---|
| `YORSH_DATA_DIR` | `/data` | корень данных |
| `YORSH_EXCHANGES` | `mexc,bitget` | активные биржи |
| `YORSH_MAX_SYMBOLS_PER_EXCHANGE` | `50` | лимит подписок (сверяться с лимитами WS в офиц. доке) |
| `YORSH_UNIVERSE_REFRESH_HOURS` | `6` | пересбор вселенной |
| `YORSH_MIN_24H_VOLUME_USD` / `YORSH_MAX_24H_VOLUME_USD` | `10000` / `2000000` | фильтр оборота: живой, но низколиквидный |
| `YORSH_RAW_RETENTION_DAYS` | `30` | retention сырой ленты |
| `YORSH_RAW_MAX_GB` | `20` | cap диска |
| `YORSH_DENSITY_KRATNOSTI` | `5.0` | кратность размера плотности к соседним уровням (M4); **стартовая точка, калибруется** |
| `YORSH_DENSITY_MIN_PERSISTENCE_SEC` | `60` | порог genuine (из аудита п.2; калибруется на ленте) |
| `YORSH_SPURT_MIN_AMPLITUDE_PCT` | `2.0` | стартовый порог прострела (RisingWave; **калибровать**, см. `no-data-fitting.mdc`) |
| `YORSH_SYMBOLS_STATIC` | `(empty)` | **временная** — список символов для M1/M2 до готовности universe-менеджера (M3); в M3 удаляется |

Все пороги-эвристики помечаются в коде комментарием «стартовая точка,
калибруется на собранной ленте (RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md, раздел
"Качество источников")».

## 5. Требования из правил проекта

- **`api-docs.mdc`:** все параметры подключений (URL, лимиты подписок,
  ping/pong-интервалы, rate limits, процедура восстановления книги) — только
  из официальных док, ссылка в docstring рядом с константой:
  - MEXC spot WS: <https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/how-to-properly-maintain-a-local-copy-of-the-order-book>
  - MEXC futures incremental (если понадобится): <https://www.mexc.com/api-docs/futures/websocket-api/incremental-order-book-maintenance-mechanism>
  - Bitget depth: <https://www.bitget.com/api-doc/contract/websocket/public/Order-Book-Channel> (для spot — соответствующий spot-раздел той же доки)
- **`no-data-fitting.mdc`:** пороги сканера — стартовые точки, финальные
  значения только из калибровки на собранной ленте с записью в
  `BUILDLOG_YORSH.md`.
- **`sample-size.mdc`:** единица связки = setup-class (зафиксировать в
  `BUILDLOG_YORSH.md` до начала сбора статистики Фазы 2).
- **`strategy-guard.mdc`:** тесты обязательны (`python3 -m pytest tests/ -v`
  перед коммитом); никаких импортов из других ботов; никаких правок их кода.
- **`buildlog.mdc`:** каждый коммит — запись в `BUILDLOG_YORSH.md`. Этот же
  лог поглощает research-артефакты (отдельного `BUILDLOG_RESEARCH.md` нет):
  определение единицы связки = setup-class, числовой критерий перехода к
  Фазе 3, результаты калибровки M6 — всё пишется в `BUILDLOG_YORSH.md`
  отдельной секцией «Research» (до начала сбора Фазы 2).
- **`deploy-vps.mdc`:** деплой только через git + GH Actions; для изоляции —
  селективный rebuild `docker compose up -d --no-deps --build yorsh-bot`.
  Первое время коллектор живёт на существующем VPS (латентность для записи
  не критична — таймстемпы событий биржевые); VPS в AWS Tokyo — только если
  дойдёт до исполнения, чего критерий аудита практически исключает.

## 6. Milestones и критерии приёмки

| # | Milestone | Критерий приёмки |
|---|---|---|
| M0 | Скелет пакета + Docker + compose | контейнер стартует, healthcheck-лог, пустая БД создаётся; `pytest` зелёный |
| M1 | MEXC-коллектор + локальная книга + recorder | ≥24ч записи без дыр (gaps логируются и восстанавливаются reinit'ом); юнит-тесты применения diff'ов |
| M2 | Bitget-коллектор | то же для Bitget (`seq`/`pseq` gap-detection) |
| M3 | Universe-менеджер | вселенная собирается по REST, фильтры работают, ротация не рвёт запись активных символов |
| M4 | Density-tracker + spoof-фильтр | по записанной ленте: таблица `densities` с вердиктами; тесты на synthetic diff-последовательности (persistence/pull/refill) |
| M5 | «Ёрш»-сканер + отчёт | `candidates` наполняется; ежедневная сводка; p-value регулярности считается корректно (тест на Пуассон-нуле) |
| M6 | Калибровка порогов | отчёт по 2+ неделям ленты: распределения амплитуд/частот, выбранные пороги + запись в `BUILDLOG_YORSH.md` |
| M7 | (Фаза 2) Replay-симулятор | exit-машина на ленте, метрики net P&L / slippage / tail-loss rate; сверка с критерием перехода из аудита |

Тесты M4 на synthetic-данных допустимы: это **инфраструктурные** тесты
механики фильтра (не подгонка стратегии под результат) — negative/positive
сценарии жизненного цикла заявки, аналогично тестам orderbook-механики.

---

## 7. Промты для реализации

Каждый промт — самодостаточное задание для отдельной агент-сессии.
Выполнять по порядку; перед каждым коммитом — `python3 -m pytest tests/ -v`.

### Промт M0 — скелет

```text
Прочитай docs/TZ_YORSH_SCANNER.md (разделы 0–5) и создай скелет нового
изолированного бота yorsh_bot в этом репозитории по паттерну scalp_bot:

1. Пакет src/yorsh_bot/ со структурой из раздела 2 ТЗ (пустые модули с
   docstring; в __init__.py — явное заявление изоляции: НЕ импортирует
   fx_pro_bot, fx_ai_trader, scalp_bot, flowzone_bot).
2. config/settings.py — pydantic-settings, префикс YORSH_, все переменные
   из раздела 4 ТЗ с default'ами.
3. state/db.py — SQLite-схема из раздела 3 ТЗ (densities, spurt_events,
   candidates, universe_log, collector_health, meta), init + migrations
   по образцу src/scalp_bot/state/db.py (структуру смотри, код не импортируй).
4. app/main.py — заглушка runtime loop: читает settings, создаёт БД, пишет
   heartbeat-лог раз в 30с. __main__.py для python -m yorsh_bot.
5. pyproject.toml: пакет в wheel-targets, CLI yorsh-bot.
6. Dockerfile.yorsh-bot по образцу Dockerfile.scalp-bot.
7. Сервис yorsh-bot в docker-compose.yml: named volume yorsh_data:/data,
   env_file .env, restart unless-stopped. Существующие сервисы не трогать.
8. tests/test_yorsh_bot.py: smoke-тесты (settings из env, схема БД, изоляция
   импортов — проверить что yorsh_bot не тянет модули других ботов).
9. Создай BUILDLOG_YORSH.md с первой записью.

Не добавляй торговых модулей — их не будет вообще (Фаза 1 = data-only).
Прогони pytest, добейся зелёного.
```

### Промт M1 — MEXC-коллектор

```text
Прочитай docs/TZ_YORSH_SCANNER.md и реализуй в src/yorsh_bot/ коллектор
MEXC spot (Фаза 1, milestone M1):

1. exchanges/base.py: dataclasses Trade, DepthDiff, BookSnapshot c полями
   ts_exch, ts_local, exchange, symbol + payload-специфика.
2. exchanges/mexc.py: WS-клиент (websockets/aiohttp) на публичные каналы
   trades + incremental depth. ОБЯЗАТЕЛЬНО по официальной доке
   https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/how-to-properly-maintain-a-local-copy-of-the-order-book
   — прочитай её WebFetch'ем перед кодом: имена каналов, protobuf/json формат,
   семантика fromVersion/toVersion, процедура reinit через REST snapshot,
   лимиты подписок на соединение, ping/pong. Каждую константу подключения
   снабди ссылкой на доку в комментарии (правило api-docs.mdc).
3. data/orderbook.py: локальная книга — apply diff c проверкой
   version-последовательности; при gap — лог в collector_health + reinit.
4. data/recorder.py: сырая запись ВСЕХ событий (trades, diffs, периодические
   снапшоты раз в N минут) в {YORSH_DATA_DIR}/raw/mexc/{symbol}/{date}/{HH}.jsonl.gz,
   ротация по часу, retention по YORSH_RAW_RETENTION_DAYS и cap YORSH_RAW_MAX_GB.
5. app/main.py: подключить коллектор для символов из YORSH_SYMBOLS_STATIC
   (временная env-переменная до M3, например 2-3 низколиквидных пары),
   reconnect с exponential backoff (параметры — из доки MEXC, не выдуманные).
6. Тесты: применение diff-последовательности к книге (корректность, gap
   detection, reinit), recorder (партиционирование, ротация, retention).
   Формат тестовых diff'ов — по структуре из официальной доки.

Никаких приватных ключей и торговых вызовов. Прогони pytest.
Добавь запись в BUILDLOG_YORSH.md.
```

### Промт M2 — Bitget-коллектор

```text
Прочитай docs/TZ_YORSH_SCANNER.md, изучи реализованный exchanges/mexc.py и
сделай аналогичный коллектор Bitget spot в src/yorsh_bot/exchanges/bitget.py
(milestone M2):

1. Перед кодом прочитай WebFetch'ем официальную доку Bitget по spot depth
   websocket (канал books: snapshot + update, поля seq/pseq) и trades —
   начни с https://www.bitget.com/api-doc/ (spot websocket public channels).
   Все константы — со ссылками на доку (api-docs.mdc).
2. Gap-detection по seq/pseq; при разрыве — resubscribe + лог в
   collector_health.
3. Переиспользуй data/orderbook.py и data/recorder.py (raw/bitget/...).
   Если книга Bitget требует другой семантики применения diff'ов — расширь
   orderbook.py параметризацией, не копипастой.
4. app/main.py: параллельная работа обоих коллекторов (asyncio), деградация
   одного не роняет второй.
5. Тесты по образцу M1 (diff-последовательности в формате Bitget).

Прогони pytest, запись в BUILDLOG_YORSH.md.
```

### Промт M3 — universe-менеджер

```text
Прочитай docs/TZ_YORSH_SCANNER.md (разделы 1, 4) и реализуй
src/yorsh_bot/data/universe.py (milestone M3):

1. REST-получение списка спот-пар MEXC и Bitget (официальные endpoints из
   док, ссылки в комментариях). Учти rate limits из док.
2. Фильтры вселенной: quote = USDT; 24h-оборот в диапазоне
   [YORSH_MIN_24H_VOLUME_USD, YORSH_MAX_24H_VOLUME_USD]; исключить
   мейджоры/топ-монеты (статический blacklist BTC/ETH/SOL/... в конфиге).
3. Приоритизация: сортировка кандидатов по «интересности» (для старта —
   случайная выборка в пределах YORSH_MAX_SYMBOLS_PER_EXCHANGE; после M5
   сюда добавится score от сканера — оставь hook).
4. Ротация: пересбор раз в YORSH_UNIVERSE_REFRESH_HOURS; символы с активными
   «ёрш»-кандидатами (status='active' в candidates) не отписывать; изменения
   логировать в universe_log.
5. Менеджер подписок: добавление/удаление символов без разрыва соединения,
   если WS-протокол позволяет (проверь в доке; иначе — переподключение
   пачкой). Соблюдай лимит подписок на соединение из офиц. доки — при
   превышении открывай дополнительные соединения.
6. Тесты: фильтры, ротация с protected-символами, разбивка по соединениям.

Убери временную YORSH_SYMBOLS_STATIC из M1 (universe теперь источник).
Прогони pytest, запись в BUILDLOG_YORSH.md.
```

### Промт M4 — density-tracker + spoof/iceberg-фильтр

```text
Прочитай docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md (пункт 2 «Фильтр спуфинга»,
подраздел «Уточнение под нашу страту») и docs/TZ_YORSH_SCANNER.md, реализуй
src/yorsh_bot/analysis/density.py (milestone M4):

1. DensityTracker на потоке L2-диффов: детекция «плотностей» — уровней с
   размером >> соседних (параметр kratnosti — в settings, стартовое значение
   пометить как калибруемое). Жизненный цикл плотности: появление,
   persistence (сколько стоит без перестановки), partial fills (по трейдам
   на этой цене + уменьшению размера), pull (снятие при подходе цены),
   refill (iceberg-паттерн: размер восстанавливается после fills).
2. Вердикты: genuine (persistence > YORSH_DENSITY_MIN_PERSISTENCE_SEC +
   partial fills + не переставляется), iceberg (refill или cumulative traded
   volume >> visible), spoof (pull при подходе цены / прыжки по уровням),
   unknown. Правила — из аудита п.2 (4 признака), пороги — в settings с
   пометкой «стартовая точка, калибровать» (no-data-fitting.mdc).
3. Запись в таблицу densities (state/db.py), обновление по мере жизни уровня.
4. Тесты на synthetic diff-последовательностях: сценарии genuine (стоит,
   partial fills), spoof (снялась за 2с до касания), iceberg (3 refill'а),
   перестановка уровня. Это инфраструктурные тесты механики фильтра
   (positive/negative жизненные циклы), не подгонка стратегии — см. раздел 6 ТЗ.

Прогони pytest, запись в BUILDLOG_YORSH.md.
```

### Промт M5 — «ёрш»-сканер + отчёт

```text
Прочитай docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md (пункт 1 «Сканер») и
docs/TZ_YORSH_SCANNER.md, реализуй analysis/prints.py,
analysis/yorsh_scanner.py и report/daily.py (milestone M5):

1. prints.py: детекция «прострелов» (spurt) — быстрое движение цены
   ≥ YORSH_SPURT_MIN_AMPLITUDE_PCT за короткое окно, стартующее агрессивными
   маркет-принтами. Кластеризация принтов-триггеров по (size, price-offset)
   — DBSCAN или простая гистограммная кластеризация на stdlib (обоснуй выбор
   в docstring; sklearn в проект не тащить без необходимости).
2. yorsh_scanner.py: для каждого символа — серия прострелов; проверки:
   (а) триггер-принты из одного кластера размеров («одинаковый принт»);
   (б) repeat-frequency test: интервалы между прострелами против
   Пуассон-нуля, p-value < 0.05 (реализация теста — stdlib/statistics,
   формулу задокументируй);
   (в) прострел стартует от уровня genuine/iceberg density (join с таблицей
   densities по цене и времени).
   Прошедшие все три — запись в candidates.
3. spurt_events: писать ВСЕ прострелы (и не прошедшие фильтры) — они нужны
   для калибровки M6.
4. report/daily.py: CLI-сводка за день: кандидаты, прострелов/сутки,
   медианная амплитуда, медианное время до отката (revert_ms), теоретический
   P&L как UPPER BOUND (без exit-slippage — явно писать это в отчёте, см.
   аудит п.3а).
5. Тесты: repeat-frequency test на синтетике (регулярная серия → p<0.05,
   пуассоновская → p>0.05), кластеризация, привязка к density.

Прогони pytest, запись в BUILDLOG_YORSH.md.
```

### Промт M6 — калибровка (после ≥2 недель сбора)

```text
В yorsh_bot накоплено ≥2 недель сырой ленты и spurt_events. Прочитай
docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md (разделы «Качество источников»,
«Трансформация гипотезы», Фаза 1) и выполни калибровку порогов (milestone M6):

1. Напиши scripts/yorsh_calibrate.py: по spurt_events и densities построй
   распределения: амплитуды прострелов, интервалы, persistence плотностей,
   доля genuine/spoof/iceberg, частота кандидатов по биржам.
2. Ответь на главный вопрос Фазы 1 (преобразованная гипотеза из аудита):
   существуют ли на MEXC/Bitget повторяющиеся прострелы от genuine density?
   Сколько кандидатов, насколько стабильны во времени?
3. Предложи откалиброванные пороги (амплитуда, persistence, кратность
   плотности) ИЗ ДАННЫХ, с квантилями и обоснованием. Никаких изменений
   «чтобы кандидатов стало больше» (no-data-fitting.mdc).
4. Результат — отчёт в docs/YORSH_PHASE1_REPORT.md + запись в
   BUILDLOG_YORSH.md (выборка, период, метрики, пороги до/после).
5. Если кандидатов нет или паттерн нерегулярен — так и написать: стратегия
   в этой формулировке закрывается (это валидный результат Фазы 1, см.
   аудит, раздел «Трансформация гипотезы»).

Код стратегии/сканера в этом milestone не менять — только анализ и отчёт.
Изменение порогов — отдельным коммитом после согласования отчёта.
```

### Промт M7 — Фаза 2: replay-симулятор

```text
Фаза 1 yorsh_bot дала положительный результат (есть стабильные кандидаты —
см. docs/YORSH_PHASE1_REPORT.md). Прочитай
docs/RESEARCH_SCAM_TOKEN_SCALP_AUDIT.md (п.3а, Фаза 2, «Критерий перехода»,
«Честное следствие из критерия») и реализуй src/yorsh_bot/replay/simulator.py:

1. Replay сырой ленты (raw/*.jsonl.gz): восстановление книги и трейдов в
   хронологии, воспроизведение сигналов сканера point-in-time (без
   look-ahead: сканер видит только прошлое).
2. Симуляция виртуальной позиции по сетапу кандидата: вход лимиткой перед
   genuine density (fill-модель: консервативно — fill только если цена
   прошла сквозь уровень; задокументируй допущения).
3. Exit-машина из аудита п.3а: (а) плановый выход лимиткой на уровне
   прострела; (б) spoof-pull cancel → market-out по книге (slippage считать
   по реальной глубине в момент выхода); (в) time-stop → market-out по книге;
   (г) kill-switch. Все выходы моделируются съеданием реальной ликвидности
   из восстановленной книги.
4. Метрики (в SQLite + отчёт): WR, EXP, PF, средний hold, tail-loss rate,
   частота kill-switch, И ГЛАВНОЕ — средний slippage time-stop-выходов
   относительно mid (ключевая метрика по аудиту) и net P&L после slippage
   и комиссий (taker fee из офиц. доки биржи, ссылка в комментарии).
5. Прогон по всем кандидатам Фазы 1, отчёт docs/YORSH_PHASE2_REPORT.md:
   сверка с числовым критерием перехода из аудита (все 4 условия, по
   каждому: выполнено/нет). Ожидаемый по аудиту результат — критерий НЕ
   пройден и проект остаётся research pipeline; если пройден — НЕ начинать
   Фазу 3, а зафиксировать результат и обсудить с пользователем.
6. Тесты: fill-модель, отсутствие look-ahead (сигнал не видит будущие
   события), расчёт slippage на synthetic книге.

Прогони pytest, запись в BUILDLOG_YORSH.md.
```

---

## 8. Риски реализации

| Риск | Смягчение |
|---|---|
| Объём сырой ленты больше ожидаемого (50 симв. × 2 биржи) | cap `YORSH_RAW_MAX_GB`, мониторинг в `collector_health`, при нехватке — сократить вселенную, не качество записи |
| MEXC protobuf-формат каналов сложнее JSON | зафиксировать формат на этапе M1 по доке; если protobuf — добавить зависимость protobuf в pyproject осознанно |
| WS-лимиты подписок жёстче ожидаемых | лимиты читаются из доки на M1/M2, менеджер соединений в M3 масштабирует число соединений |
| Кандидатов на MEXC/Bitget не окажется (prof MM) | это валидный результат Фазы 1 — стратегия закрывается, инфраструктура коллектора остаётся проекту |
| Дрейф ТЗ в сторону торговли «раз уж всё готово» | числовой критерий перехода зафиксирован в аудите ДО разработки; Фаза 3 не входит в ТЗ |

## 9. История документа

- 2026-07-05: первичная версия. Решение «отдельный бот `yorsh_bot`, не
  стратегия» (раздел 0), milestones M0–M7, промты для агент-сессий.
- 2026-07-05: правки по ревью ТЗ — `BUILDLOG_YORSH.md` поглощает research
  (отдельного `BUILDLOG_RESEARCH.md` нет); в таблицу env добавлены
  `YORSH_SYMBOLS_STATIC` (временная, M3) и `YORSH_DENSITY_KRATNOSTI`
  (калибруется); зафиксирована схема `meta` (key/value); комментарий дерева
  пакета дополнен `universe_log`/`collector_health`; `YORSH_SYMBOLS_STATIC`
  default → `(empty)`.
