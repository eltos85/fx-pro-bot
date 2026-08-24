# TASKSPEC — flowzone_bot (тех-задание для реализации)

Это **тех-задание и промпты для агента** на реализацию нового бота `flowzone_bot`
в **ОТДЕЛЬНОМ чате** (чтобы контекст прошлых правок не протёк). Канон стратегии —
`STRATEGY_FLOWZONE.md` (строго по ролику <https://youtu.be/06R-ebyOhDI>).

> ⚠️ Реализацию НЕ начинать в этом чате. Здесь только ТЗ. В новом чате — следовать
> разделам ниже и **сверять каждый компонент с каноном** (ролик + STRATEGY_FLOWZONE.md).

---

## 1. Цель и принципы

- Создать **полностью автономного** бота `flowzone_bot`, реализующего стратегию
  из `STRATEGY_FLOWZONE.md` (Auction Market Theory + Volume Profile + Order Flow,
  continuation-вход из зон высокой вероятности).
- **Изоляция кодовой базы:** новый модуль `src/flowzone_bot/`, свой Dockerfile,
  свой сервис в `docker-compose.yml`, свой SQLite-volume, свой BUILDLOG. Не
  тащить стратегические параметры других ботов (strategy-guard.mdc «изоляция»).
- **Канон — источник правды.** Любой числовой порог обосновывается каноном
  (ролик) или канонической литературой Market Profile (**Steidlmayer**, **Jim
  Dalton «Mind Over Markets»**) — НЕ интуицией и НЕ подгонкой (no-data-fitting.mdc).
- **Демо сначала.** Запуск на demo-счёте, trading-enabled включать только после
  проверки первых циклов.

---

## 2. Идентичность бота

- **Имя:** `flowzone_bot` (рабочее; переименование тривиально — согласовать с
  пользователем в начале нового чата).
- **Биржа:** Bybit (linear perp), через креды `ai_trader`.
- **Тип решений:** детерминированный, по микроструктуре в реальном времени. БЕЗ
  LLM (в отличие от ai_trader — переиспользуем только инфраструктуру, не DeepSeek).

---

## 3. Креды и интеграции (взять с VPS)

Всё уже есть в `/root/fx-pro-bot/.env` на VPS (хост `204.168.149.140`). Имена
переменных (значения НЕ хардкодить — читать из env):

**Bybit (аккаунт ai_trader, demo):**
- `AI_TRADER_BYBIT_API_KEY`
- `AI_TRADER_BYBIT_API_SECRET`
- `AI_TRADER_BYBIT_DEMO=true`
- `AI_TRADER_BYBIT_CATEGORY=linear`

> Для flowzone_bot завести **собственные env-имена** (`FLOWZONE_BYBIT_API_KEY`/
> `FLOWZONE_BYBIT_API_SECRET`/…), которым по умолчанию присваиваются значения
> ai_trader-ключей в docker-compose (`${FLOWZONE_BYBIT_API_KEY:-${AI_TRADER_BYBIT_API_KEY}}`),
> чтобы аудит ключей был раздельным (как сделано для scalp/bybit-bot).

**Telegram (бот и чат ai_trader):**
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- (репорты flowzone_bot идут в чат AI-Trader; помечать сообщения префиксом
  `[flowzone]`).

---

## 4. Инструмент(ы) торговли — РЕШЕНО

- **Символы берём из готового авто-селектора scalp_bot** (`src/scalp_bot/data/
  universe.py`): отбор по 24h turnover/range/spread + intraday RVOL (5m),
  композитный скор (vol/liq/spread). Переиспользуем как есть (Bybit `get_tickers`
  + `get_kline`).
- **Калибровочная заметка (канон, не блокер):** канон демонстрировался на NQ —
  глубоко-ликвидном рынке; absorption/footprint читаемы только на ликвидности.
  Селектор scalp уже имеет стражи ликвидности (turnover-floor + spread-cap), но
  взвешивает волатильность наравне с ликвидностью. Если на форвард-тесте
  footprint/absorption «шумят» на тонких монетах — сместить отбор в сторону
  ликвидности (через env-пороги селектора, не новой логикой). Решение по факту
  данных, не заранее (no-data-fitting).

---

## 5. Переиспользуемые компоненты (с ОБЯЗАТЕЛЬНОЙ сверкой с каноном)

Эти готовые куски можно переиспользовать как **инфраструктуру**, но каждый —
**перепроверить на соответствие канону ролика** перед использованием. Если
поведение не совпадает с каноном — НЕ подгонять канон под код, а адаптировать код.

| Компонент | Где | Переиспользовать как | Сверка с каноном |
|---|---|---|---|
| **Delta / CVD** | `scalp_bot/data/aggregates.py` (`SymbolState.on_trade`, `CvdSample`) | дельта-поток | Канон требует **delta НА УРОВНЕ/в зоне** (delta print), а у нас CVD по времени. Нужно агрегировать дельту **по цене** (delta-at-price), не только cumulative по времени. ✅ переиспользовать сбор сделок, ❗дописать delta-by-price. |
| **Absorption / ob_imbalance** | `aggregates.py` (`on_orderbook`, `ob_imbalance`) | дисбаланс книги | Канон absorption = **агрессивная сторона поглощается** (deep trades в теле свечи), это про **исполнения**, не только resting book. ✅ ob_imbalance как доп-фактор, ❗главный триггер — absorption по исполненной дельте vs движению цены. |
| **Confluence-скоринг** | паттерн `score`/`reasons` в `scalp_bot/analysis/signals.py` | агрегатор факторов зоны | ✅ паттерн годится (несколько факторов → score). Факторы должны быть КАНОН-овые: VAH/VAL, POC, ledge, delta, big trades. |
| **WS-плумбинг** | `scalp_bot/data/market_stream.py`, `exec_stream.py` | подписки Bybit public WS | ✅ переиспользовать (publicTrade, orderbook.50). Сверить с Bybit docs (api-docs.mdc). |
| **Telegram notifier** | `scalp_bot/telegram/notifier.py` | отправка репортов | ✅ переиспользовать, слать в `TELEGRAM_CHAT_ID` с префиксом `[flowzone]`. |
| **DB-слой** | `scalp_bot/state/db.py` | хранение сделок/PnL | ✅ переиспользовать паттерн, своя БД-файл/volume. |
| **Killswitch/safety** | `scalp_bot/safety/killswitch.py` | дневной/совокупный лимит | ✅ переиспользовать паттерн. |
| **Klines/HTF** | `scalp_bot/data/htf.py`, `levels.py` | REST get_kline | ✅ для контекста/свинг-структуры/сессионных границ (M5). VP строим из tick-потока, НЕ из kline-volume (§6 п.1). |
| **Universe-селектор** | `scalp_bot/data/universe.py` | авто-подбор символов | ✅ переиспользовать (turnover/range/spread + RVOL). Канон-калибровка ликвидности — §4. |
| **Risk/sizing** | `scalp_bot` модель риска (`virtual_capital`, `risk_per_trade_usd`, `max_open_positions`, `max_trades_per_hour`) | сайзинг и лимиты | ✅ те же значения (§6 п.8): demo, поэтому не критично. |

---

## 6. НОВЫЕ компоненты (строить с нуля, по канону)

1. **Volume Profile engine** — POC / VAH / VAL / HVN / LVN / volume ledge.
   - Источник данных: **tick/order-flow footprint** (исполненный поток через
     `publicTrade`), НЕ kline-volume — так в каноне (скриншот, STRATEGY §6.3:
     `Order Flow - Vol. Profile`). Профиль агрегируется по корзинам цен.
   - Привязка профиля: **сессия / день / неделя / композит** (как `Dly/Wkly/Comp.
     Vol. Profile` в каноне) + профиль предыдущей swing-точки. ТФ графика входа M5.
   - Пороги Value Area (≈70% объёма) — канон Market Profile (Steidlmayer/Dalton),
     цитировать в docstring.
2. **Auction context classifier** — тренд vs баланс: детект «чистый пробой +
   acceptance за VAH/VAL». Формализовать «acceptance» (например N баров/время
   принятия за границей) со ссылкой на Dalton.
3. **Big-trades detector** — крупные исполненные принты. Требует хранить
   **размер каждой сделки** (расширить `on_trade`, сейчас он схлопывает в CVD).
   Порог «крупной» — относительный (например percentile размера за окно), не
   magic-number.
4. **Zone builder (confluence)** — собирает зону из VP + delta-at-price + big
   trades; score по числу совпадений.
5. **Entry trigger (absorption-at-zone)** — подтверждение поглощения контр-стороны
   по исполненной дельте в теле свечи при подходе к зоне; «failed» контр-сторона.
6. **Trade manager** — лимит-вход в зоне; стоп за зоной (масштаб 1-2-3/4/5);
   цель = ближайший swing-point; частичная фиксация + reload на следующей зоне.
7. **Session gate** — торговать только в активные сессии (London/NY), окна в UTC.
8. **Risk/sizing** — модель **как у scalp_bot** (demo, поэтому не критично):
   `virtual_capital=1000`, фиксированный риск `risk_per_trade_usd=10`
   (qty = risk ÷ |entry−SL|), `max_open_positions=2`, `max_trades_per_hour=5`,
   killswitch по дневному/совокупному убытку. Значения — env, дефолты scalp.

---

## 7. Данные и API (api-docs.mdc — только офиц. доки Bybit)

Перед правками подключения/подписок — читать офиц. доку Bybit v5 и ссылаться:
- `publicTrade` WS — поток исполненных сделок (delta, big trades):
  <https://bybit-exchange.github.io/docs/v5/websocket/public/trade>
- `orderbook.50` WS — стакан (ob_imbalance/absorption):
  <https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook>
- `get_kline` REST — для VP из баров:
  <https://bybit-exchange.github.io/docs/v5/market/kline>
- Rate limits / order placement / tpslMode:
  <https://bybit-exchange.github.io/docs/v5/intro>
- pybit: <https://github.com/bybit-exchange/pybit>

---

## 8. Тесты (strategy-guard.mdc — тесты обязательны)

Минимум unit-тестов:
- Volume Profile: POC/VAH/VAL/ledge на синтетике с известным распределением.
- Auction classifier: тренд vs баланс на фикстурах (пробой+acceptance vs внутри VA).
- Big-trades detector: крупный принт детектится, мелочь — нет.
- Zone builder: confluence ≥2 факторов даёт зону, 0-1 — нет.
- Absorption trigger: поглощение контр-стороны → сигнал; чистый пробой без
  поглощения → нет.
- Trade manager: стоп за зоной, цель swing, частичная фиксация/reload.
- Все позитивные сценарии на **реальных барах/потоке** или честной синтетике
  (no-data-fitting: НЕ рисовать данные «под результат»).

---

## 9. Документация и деплой

- **BUILDLOG_FLOWZONE.md** — новый лог (по правилу buildlog.mdc).
- **STRATEGY_FLOWZONE.md** — уже создан; обновлять при любых уточнениях канона.
- `docker-compose.yml` — новый сервис `flowzone-bot` (по образцу `scalp-bot`):
  свой образ, env-блок, volume `flowzone_data`.
- Деплой: git push → селективный rebuild `flowzone-bot` (deploy-vps.mdc),
  проверка контейнера и логов после старта.

---

## 10. Применимые правила проекта (соблюдать)

- `strategy-guard.mdc` — research как источник правды, тесты, изоляция кодовых баз.
- `no-data-fitting.mdc` — не подгонять пороги под желаемый результат.
- `sample-size.mdc` — не делать выводов о прибыльности на <100 сделок.
- `api-docs.mdc` — параметры подключения только из офиц. доки Bybit.
- `stats-collection.mdc` — full-pagination при сборе статы, явный источник/период.
- `buildlog.mdc` — запись в BUILDLOG_FLOWZONE.md в том же коммите.
- `deploy-vps.mdc` — деплой только через git, проверка контейнера.

---

## 11. Фазы реализации (milestones)

1. **Каркас**: модуль `src/flowzone_bot/`, конфиг (env), Bybit-клиент (demo),
   WS-подписки, БД, Telegram, killswitch. Бот запускается, читает поток, ничего
   не торгует (paper/observe).
2. **Volume Profile + контекст**: VP engine + auction classifier. Логирует
   контекст и зоны (без входов).
3. **Поток**: delta-at-price + big-trades detector + absorption trigger.
4. **Зоны + вход**: zone builder (confluence) → лимит-вход в зоне на demo.
5. **Управление**: стоп за зоной, цели, частичная фиксация, reload.
6. **Сессии + риск**: session gate, sizing, лимиты.
7. **Тесты, BUILDLOG, деплой на demo**, наблюдение первых циклов.
8. **Форвард-тест** до накопления выборки (≥100 сделок) перед любыми выводами.

После КАЖДОЙ фазы — **сверка с каноном** (ролик + STRATEGY_FLOWZONE.md): не
появилось ли отсебятины, все ли пороги обоснованы каноном/литературой.

---

## 12. ГОТОВЫЙ СТАРТОВЫЙ ПРОМПТ для нового чата

> Скопировать в новый чат как первое сообщение.

```
Реализуем нового автономного бота flowzone_bot по тех-заданию TASKSPEC_FLOWZONE.md
и канону STRATEGY_FLOWZONE.md (строго по ролику https://youtu.be/06R-ebyOhDI).

Контекст:
- Биржа: Bybit linear, demo, креды ai_trader (env AI_TRADER_BYBIT_API_KEY/SECRET);
  для flowzone завести свои FLOWZONE_BYBIT_* с дефолтом на ai_trader-ключи.
- Telegram: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (чат AI-Trader), префикс [flowzone].
- Изоляция: новый модуль src/flowzone_bot/, свой Dockerfile, сервис в compose,
  свой volume, свой BUILDLOG_FLOWZONE.md. Без LLM.

Прежде чем писать код:
1. Прочитай STRATEGY_FLOWZONE.md и TASKSPEC_FLOWZONE.md целиком.
2. Прочитай ролик-сверку (раздел 9 стратегии) — это инвариант канона.
3. Все решения зафиксированы (§13 ТЗ): имя flowzone_bot, символы = авто-селектор
   scalp (universe.py), риск = модель scalp ($1000 / $10 на сделку), ТФ = M5,
   профиль = tick/footprint, Bybit demo (ключи ai_trader), TG = чат ai_trader.
   Открытых вопросов нет — уточняй только если что-то противоречит канону.
4. Работай по фазам §11 ТЗ. После каждой фазы — сверка с каноном.
5. Соблюдай правила: strategy-guard, no-data-fitting, sample-size, api-docs,
   stats-collection, buildlog, deploy-vps.

Начни с фазы 1 (каркас) ПОСЛЕ ответов на открытые вопросы. Не подгоняй пороги
под результат — каждый порог обосновывай каноном (ролик) или Market Profile
(Steidlmayer / Dalton).
```

---

## 13. Решения (все вопросы закрыты — можно стартовать)

Открытых вопросов НЕТ. Зафиксировано:

- **Имя бота** = `flowzone_bot` (подтверждено 2026-06-15).
- **ТФ входа** = **M5**; профиль объёма/дельты = **tick/order-flow footprint**,
  привязка день/неделя/композит/сессия (канон, STRATEGY §6.3; строить VP из
  исполненного потока, не из kline-volume).
- **Символы** = авто-селектор scalp_bot (`universe.py`), §4.
- **Риск/капитал** = модель scalp_bot (`virtual_capital=1000`,
  `risk_per_trade_usd=10`, `max_open_positions=2`, `max_trades_per_hour=5`), §6 п.8.
- **Биржа/креды** = Bybit demo, ключи ai_trader; Telegram — чат ai_trader (§3).
