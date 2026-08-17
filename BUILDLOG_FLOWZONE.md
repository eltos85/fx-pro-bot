# BUILDLOG — flowzone_bot

Журнал сборки order-flow бота `flowzone_bot` (Bybit, Auction Market Theory +
Volume Profile + Order Flow). Канон стратегии — `STRATEGY_FLOWZONE.md` (ролик
<https://youtu.be/06R-ebyOhDI>), тех-задание — `TASKSPEC_FLOWZONE.md`.

Формат: записи группируются по дням (новые сверху). Для багов: симптом →
причина → решение. Для фич: что добавлено и на что влияет.

---

## 2026-08-17

### feat(flowzone): изоляция ядра — DD-детектор, hook/initiative выкл, окно London+NY
`1f559dd`

Пользователь подтвердил пакет из разбора 17.08: починить то, что врёт
относительно задумки, и развести C4/C1 vs новые сетапы на форварде.

**1. Детектор double distribution (баг C3).** Симптом: 126/126 сделок после
фикса `shape` были `double_distribution` — гейт формы не резал. Причина:
`find_hvn_lvn` считал HVN любой локальный пик соседних корзин; `classify_shape`
брал min/max HVN и любой LVN между ними. Dalton HVN = концентрация **выше
средней** плотности профиля; перешеек двух dealing range — тонкий LVN, та же
конвенция обрыва что volume ledge §3.1 (`drop_frac=0.5`). Не новый P&L-порог.
Регрессия: пила 10/11 по 30 корзинам больше не DD; два кластера 100 через
перешеек 5 — по-прежнему DD.

**2. Hook и initiative выключены.** C1-C5 n=134: hook WR 17.4% (n=46),
initiative 15.6% (n=32), absorption 17.9% (n=56). Разница <1 п.п. — по
sample-size отключать «худший сетап» нельзя, но смешивать их с ядром на
следующем форварде бессмысленно. Дефолт `hook_enabled=false`,
`initiative_exhaustion_enabled=false` (тот же флаг глушит exhaustion-exit,
который за две недели не сработал ни разу). Код на месте, env включает.

**3. Окно сессии** `07:00-16:00,12:00-21:00` (London+NY, как до C4). Absorption
на тройке тоже упал 25%→18% вместе с C1-C5, не только новые входы — C4 не был
разведён. Канон-окно 12:00-21:00 остаётся через env. C1 merge не трогаем.

Halt/cooldown 33004 — коммит `36673d4`, уже в проде. Ключ на VPS не менялся
(нет secret).

**Файлы:** `analysis/{context,volume_profile,session}.py`, `config/settings.py`,
`docker-compose.yml`, `STRATEGY_FLOWZONE.md` §6.1/§11.9, `tests/test_flowzone_bot.py`.

---

### fix(flowzone): cooldown на отказ входа + halt при Bybit 33004
`36673d4`

Проверка 17.08: demo-ключ истёк, бот 16.08 за 8 минут записал 55
`entry_Rejected` по одному шорту ETH (код 33004). Торговые пороги не трогаем.

**Симптом.** `on_signal` при отказе биржи возвращает `None`, а cooldown
ставился только если вернулся id. Eval-цикл ~1с → тот же сигнал долбил REST
каждую секунду. Write-ahead insert плодил мусорные строки в `trades`.

**Причина.** Анти-даблклик был привязан к успешному place, не к попытке.
Истёкший ключ (офдок Bybit V5: *«33004 (Derivatives) Your api key has expired»*
https://bybit-exchange.github.io/docs/v5/error) не останавливал сканер.

**Решение.**
1. `apply_signal_cooldown` — cooldown на попытку входа, до `on_signal`.
2. `is_expired_api_key` в клиенте (33004 / «api key has expired»); флаг
   `auth_expired` с `set_leverage` и `place_order`.
3. Executor: после 33004 на leverage — ордер не пишется; после 33004 на
   place — одна строка `entry_Rejected`, дальше skip до рестарта. Главный
   цикл не сканирует входы, пока флаг поднят.

Ключ в этом коммите не меняется (нужна пара key+secret на VPS `.env`, не в git).

**Файлы:** `trading/client.py`, `trading/executor.py`, `app/main.py`,
`tests/test_flowzone_bot.py`. Тесты: 1377 passed.

---

## Бэклог (аудит 2026-07-02, «мелкие наблюдения — не блокеры»)

Из аудита v0.2.0 (запись `473df5c` ниже). Пять наблюдений оценены по влиянию
на WR/экспектейшен; ничего не меняем без данных (no-data-fitting.mdc).
История сделок ДО фиксов `473df5c` загрязнена kline-багом — все замеры ниже
только на чистом форварде после деплоя фиксов.

1. **[замер, офлайн] Контрфактуал фактора `big_trades`** (`zone.py:87-89`).
   Сейчас фактор считается по 300-сек окну `snap.trades`, канон — историчный
   поток на зоне (принты per-swing профиля). Ошибка двусторонняя: свежий
   крупный принт дотягивает 2-факторный кластер до гейта confluence=3 (ложная
   зона) / исторические big trades на зоне невидимы (пропуск «super strong
   area»). Скрипт по `PrintStore`: на каждом входе пересчитать score зоны с
   big_trades из per-swing принтов и посчитать долю сделок, где менялся
   вердикт гейта. Если доля заметная (>10-15%) — выносить на обсуждение
   правку окна.
2. **[замер, SQL] Частота срабатывания `min_notional`-floor в сайзинге**
   (`executor.py:44-56`). Floor молча поднимает $-риск выше
   `risk_per_trade_usd` (WR не трогает, искажает $-стату); после floor по
   `qty_step` возможен провал ниже min_notional → reject. Запрос по БД:
   `qty×entry` vs расчётный `risk_usd/|entry−sl|×entry`, доля сделок с
   подтяжкой и фактический перебор риска. Если floor почти не срабатывает —
   закрыть как non-issue.
3. **[решение при n≥100] Интерпретация стопа «1-2-3/4/5» (`sl_zone_mult`)**
   (`strategy.py:106-116`, `settings.py:203-213`). Текущая интерпретация
   N=1 = far_edge + 1×ширина зоны даёт риск-дистанцию ≈2× ширины зоны против
   ≈1× при буквальном «сразу за зоной». Эффект тройной: WR выше (глубокие
   ретесты не выбивают), $-виннер вдвое мельче (риск-сайзинг), частота входов
   ниже (R:R-гейт делит на удвоенный риск — именно поэтому 2026-06-29
   min_rr опускали 2.5→2.0, бот вставал). Знак итога неизвестен → replay-
   сравнение двух интерпретаций на n≥100 чистых сделок. Правка стратегии —
   только после обсуждения (strategy-guard.mdc), с обновлением STRATEGY §11.5.
4. **[техдолг, низкий приоритет] `ledge_drop_frac` вынести в settings**
   (`zone.py:68`): док заявляет reversible через env, по факту хардкод-дефолт
   0.5. На торговлю не влияет (дефолт тот же), только тюнинг без деплоя.
5. **[наблюдение, без действий] `_rebracket`** (`executor.py:323`): сдвиг
   SL/TP на дельту филла концептуально неканоничен (уровни структурные, не
   entry-привязанные), но эффект ≈0 (порог 1 б.п., лимит-вход, дистанции
   сохранены). Пересмотреть только если появится маркет-вход.

---

## 2026-07-31

### fix(flowzone): сериализация SQLite между потоками + форма профиля не терялась в латче
`3be72e2`

Проверка бота через двое суток после выката канона (`f0813db`) нашла два бага,
оба внесены вместе с C1. Торговых порогов не касаемся — выборка 9 сделок,
это шум (sample-size.mdc).

**Баг 1 — гонка на общем SQLite-соединении.** Симптом: три падения за двое
суток — `cannot start a transaction within a transaction` в
`save_session_profile` (07-29 14:59), `cannot commit - no transaction is
active` (07-30 14:47) и `SystemError: error return without exception set` в
`insert_prints` (07-30 18:19, потерян батч принтов). Причина: соединение
открыто с `check_same_thread=False` и делится между main-циклом и фоновым
потоком флаша принтов, но записи **не сериализованы**. Офдок sqlite3:
*«If False, the connection may be accessed in multiple threads; write
operations may need to be serialized by the user to avoid data corruption»*
(<https://docs.python.org/3/library/sqlite3.html#module-functions>). Гонка
существовала и раньше (принты + сделки), но C1 добавил третьего писателя
(3 профиля в минуту) и перевёл её из редкой в регулярную. Решение: `RLock`
на весь метод — лочить отдельные `execute`/`commit` недостаточно, т.к.
sqlite3 в legacy-режиме открывает транзакцию неявно на первом DML, поэтому
атомарной должна быть пара (DML → commit). Регрессионный тест гоняет три
писателя параллельно; без лока воспроизводит ровно те же ошибки прода
(проверено: 10/10 прогонов).

**Баг 2 — `shape=unknown` во всех сделках.** Симптом: все 9 сделок после
выката канона логировали `ctx=trend_up`+`shape=unknown`, т.е. измерить связь
формы профиля с исходом было нельзя. Причина: `AuctionTracker.update`
пересобирал `Context` без поля `shape` → дефолт dataclass `UNKNOWN`. Гейт C3
при этом работал корректно (он внутри `classify`, до латча), пострадала
только наблюдаемость. Решение: пробрасывать `shape=inst.shape` — латчится
только `state`, остальные поля описывают текущий профиль.

**Файлы:** `state/db.py`, `analysis/auction.py`, `tests/test_flowzone_bot.py`
(+2 теста). Тесты: 1332 passed.

---

## 2026-07-29

### feat(flowzone): строгий канон C1-C5 по дословным транскриптам роликов
`f0813db`

Пользователь попросил пересмотреть первоисточник и привести стратегию к
строгому канону. Впервые удалось достать **полные транскрипты** роликов
Fabervaale ENG (sozai.app): прямой доступ к YouTube для `yt-dlp` и fetch закрыт
(`LOGIN_REQUIRED`, бот-чек; куки браузера и подстановка UA не помогли). До этого
вся сверка шла по выдержкам — отсюда и ошибки атрибуции ниже.

**Симптом.** Аудит D1-D8 (30.06) пометил три канон-механики как «[НАШЕ]» и
оставил их выключенными, ссылаясь на `no-data-fitting.mdc`. Дословный текст
показывает, что в ролике это базовые практики автора, а не наши надстройки.
Плюс один сетап, который автор называет самым надёжным, отсутствовал целиком.

- **C1 — merge профилей** (`profile_merge_enabled` false → **true**). Канон
  31:14 *«these two profile can be merged. You can merge them… when they are
  overlapping on the same level»*, 31:59 *«do a double day profile on a single
  level and you can have a really precise value area low point»*, далее ещё два
  повтора (32:33, 32:50). Критерий слияния — перекрытие value area. Добавлены
  `value_areas_overlap`, `_merged_session_profile`, таблица `session_profiles`
  (профили прошлых сессий не пересобрать из `prints` — retention 6ч).
- **C2 — initiative + exhaustion** (`initiative_exhaustion_enabled` false →
  **true**). Канон 37:03 *«we saw the absorption, we saw the exhaustion, we saw
  the initiative auction»*. Initiative стал вторым триггером входа рядом с
  absorption (absorption приоритетнее). Exhaustion — НЕ вход (бот только
  continuation), а стадия 3 сопровождения: фиксация прибыли по «My Signature
  Orderflow Model» 06:04 *«this selling pressure is almost exhausted… I take out
  my position»*. Новый `close_reason=exhaustion_exit`.
- **C3 — форма профиля гейтит направление.** Канон 34:32 *«Is not a P shape. So
  it's still balance. You can use this as indecision»*. `classify` получил
  `shape_gate`: тренд остаётся только при P-shape в сторону acceptance (хвост
  + направленная дельта **в хвосте**) или double distribution. Тяжёлый хвост
  без подтверждающей дельты → баланс. Раньше `shape` был лишь обогащением.
- **C4 — одна сессия вместо склейки London+NY.** Канон 28:54 *«I only trade in
  the New York session… where the majority of the volume get traded… So I only
  use the cash session profile»*. Окно для крипты **измерено**, а не взято по
  аналогии: `scripts/flowzone_session_volume.py`, 1000 часовых баров ≈41 день,
  среднее по BTC/ETH/SOL — пик 13:00 (8.91%), 14:00 (8.34%), 15:00 (7.61%) UTC;
  **NY 12-21 = 51.4%** оборота за 9ч против **London 07-16 = 46.8%**; склейка
  07-21 = 67.2%, но за 14ч. `session_windows_utc` → `"12:00-21:00"`. Это меняет
  и торговое окно, и базу VP: было 14ч, стало 9ч.
- **C5 — новый сетап hook / failed auction.** Канон 26:17 *«they do a failed
  auction, they try to break, they get rejected… when you go back inside, you
  have your continuation trade»* и 27:20 *«This is one really profitable setup
  with high win rate»*. Модуль `analysis/hook.py` + ветка
  `strategy._evaluate_hook`. Порог «не приняли» = доля объёма за границей
  меньше `1 − value_area_pct` (32%) — выведен из канон-константы VA, нового
  подобранного числа нет. Стоп за экстремумом неудачной вылазки, цель и
  R:R-фильтр общие (§5.1/§5.3). Ищется по persisted-принтам: hook длиннее одной
  M5-свечи.

**Телеметрия → непрерывные скаляры.** За неделю после v2-фикса `init_prev`
покрыл 2/16 сделок, а `shock` (порог ×4) не сработал ни разу — реплей по тикам
дал максимум ×2.1-3.0. Бинарные защёлки на таком темпе не наберут 100 сделок
(`sample-size.mdc`), поэтому пишем непрерывно: `init_prev` теперь всегда несёт
направление ноги, долю дельты и флаг `conf`, добавлен `vratio` = плотность
ленты / базовая EMA. Дискретный `shock` с TTL сохранён.

**Правка тестовых фикстур.** `_down_elongated_buckets` и `_short_reload_profile`
описывали нисходящий день, но весь профиль был buy-only, включая нижний хвост.
Под гейтом C3 канон справедливо считает такой день indecision (34:32), поэтому
хвосты приведены к sell-доминанте — исправление фикстуры под её собственный
сценарий, не подгонка под тест (`no-data-fitting.mdc`).

**Ожидаемое влияние на частоту сделок — вниз**, и это надо учитывать при сборе
статистики: окно сузилось 14ч → 9ч, гейт C3 отсекает дни без подтверждающей
дельты в хвосте. В противоход работают два новых входа (initiative, hook).
Нетто-эффект неизвестен — это и есть предмет замера. Правило `sample-size.mdc`
(≥100 сделок, ≥2 недели, p<0.05) до набора выборки не нарушаем: никаких
дальнейших правок порогов до данных.

**Файлы:** `config/settings.py`, `analysis/{context,volume_profile,orderflow,
hook,strategy,session,telemetry}.py`, `app/main.py`, `trading/executor.py`,
`state/db.py`, `scripts/flowzone_session_volume.py`, `tests/test_flowzone_bot.py`,
`STRATEGY_FLOWZONE.md`, `docker-compose.yml` (только env сервиса `flowzone-bot`:
окно сессии жёстко задавало старое `07:00-16:00,12:00-21:00` и перекрыло бы
дефолт из кода, плюс добавлены флаги C1/C2/C3/C5). Тесты: 1316 passed.

---

## 2026-07-24

### fix(flowzone): telemetry v2 — preceding initiative, structural dwell, shock TTL
`dd41adc`

Аудит direction-telemetry на 81 закрытой сделке (#534–614, 06–23.07,
17 календарных дней; 73/81 `pnl_verified`). Это **ниже порога n≥100**, поэтому
торговые гейты/AuctionTracker НЕ менялись. P&L из локальной SQLite использован
только сравнительно, не как биржевой ground truth.

Найдены три дефекта самой наблюдаемости:

1. **`initiative` смешивал импульс с absorption.** Детектор работал на текущем
   300-сек entry-окне, где стратегия специально ищет поглощаемую контр-агрессию.
   Поэтому 64/81 сделок получили `counter`, включая все top-10 wins; это не
   направление предшествующей ноги. Исправлено: `init_prev` считается только
   на persisted prints завершённого предыдущего M5-окна
   `[now−2×300s, now−300s)`. SQLite читается лишь на signal/auction-flip.
2. **`dwell` почти отсутствовал (aligned 5/81)** и считался за ближайшим
   M5-фракталом, который не представляет значимую структуру. Исправлено:
   `dwell_struct_up/down` использует max confirmed swing highs / min swing lows
   всего M5-lookback; дополнительно логируются `dStructHi/dStructLo` в bps.
3. **`shock` не истекал.** 50/81 сделок имели возраст >6ч, максимум ≈194 тыс.
   секунд — фича не описывала пост-шоковый режим. Добавлены TTL=3600с и reset
   при смене `vp_session_start`; устаревший shock исключается из reasons.

Также `DirectionTelemetry.update` перенесён из signal-scan до open/cooldown
гейтов: теперь все символы обновляются каждый eval-loop, поэтому открытая
позиция/cooldown не создают пропуски shock/dwell. Изменения строго non-gating:
входы, пороги и направление аукциона не меняются.

**Файлы:** `analysis/telemetry.py`, `app/main.py`,
`tests/test_flowzone_bot.py`.

---

## 2026-07-06

### feat(flowzone): direction-telemetry — наблюдаемость устойчивости аукциона (non-gating)

Контекст: разбор эталона #530 ETHUSDT short +$54.94 (06.07, tp_hit, rr=6.1,
44 мин) против флап-лузеров #531/#532 (−$18.96/−$12.43, переворот латча
short→long за 9 минут). По HB-логам найден механизм ложного переворота:
объёмный шок 12:00 UTC (44-93K тиков/мин против базы 2-5K) сдвинул value area
вниз → всё утреннее наторгованное оказалось «выше VAH» → мгновенный classify
дал acc↑100% trend_up (артефакт миграции VA, не принятие); одновременно
dead-cat bounce пробил ближайший M5-фрактал (структурный уровень 62900 не
тронут) → оба канон-условия переворота формально выполнены → латч перевернулся
в лонг против инициативной волны.

Решение: НЕ трогаем AuctionTracker (n=34 пост-деплой — шум, sample-size.mdc).
Добавлен `analysis/telemetry.py` (`DirectionTelemetry`) — non-gating
наблюдаемость, четыре фичи-кандидата в `reasons` сделки (persist в БД) и в лог
переворота латча (`auction flip SYM: down → up | tele=...`):

1. **init** — последний initiative-импульс (`detect_initiative`, D7):
   направление, возраст, alignment со стороной сделки (same/counter).
2. **dwell** — секунды непрерывного удержания цены за пробитым
   swing-экстремумом (канон «accepted after breakout» = процесс, не снимок).
3. **dHi/dLo** — дистанция (bps) до экстремумов дня (качество «пробитого
   уровня»: фрактал в 700 bps от day-high ≠ структурный пробой).
4. **shock** — возраст/направление последнего объёмного шока (тики ≥4× EMA
   базы, пол 1000): после шока classify ненадёжен 30-60 мин.

Формат в reasons: `tele=init:down:412s:same,dwell_dn:35s,dHi:-123bp,dLo:18bp,
shock:down:1520s`. Через ≥100 сделок — mining: какие фичи разделяют эталоны и
флапы → data-driven правка гейта переворота (отдельным решением).

Тех-пороги телеметрии (EMA halflife 600с, shock ×4/пол 1000, refresh 5с) —
параметры наблюдаемости (anti-noise), НЕ торговые: влияют только на содержимое
лог-строки. pytest 1217 passed (5 новых).

**Файлы:** `analysis/telemetry.py` (новый), `app/main.py` (интеграция в
scan-цикл + flip-лог), `tests/test_flowzone_bot.py`.

---

## 2026-07-02

### fix(flowzone): аудит v0.2.0-канона — 5 багов (kline DESC, BE≡TP, trail-буфер, шум-acceptance, session-якорь)
`473df5c`

Полный аудит реализации против канона (STRATEGY_FLOWZONE.md + winkler-rulebook
как независимый вторичный источник) + математическая проверка VA/classify на
синтетике. Ядро канона (session-профиль, per-swing зона, confluence, absorption,
R:R, полный выход) подтверждено корректным. Найдено и исправлено 5 багов:

1. **[КРИТИЧЕСКИЙ] kline DESC не разворачивался** (`main._swings_for`). Bybit
   get_kline отдаёт новые бары сверху (docstring клиента это фиксирует;
   `universe.hourly_range_rvol` разворачивает, `_swings_for` — нет) →
   `Swing.idx` инвертирован → «последний swing» (`max idx` в
   `_last_swing_price`/`auction._recent_extreme`) = самый СТАРЫЙ бар окна
   (~16.7ч, 200×M5). Следствия: BE-lock у шортов срабатывал сразу после филла
   (цена всегда ниже древнего swing low) → trail → выбивание в микро-плюс;
   у лонгов BE не срабатывал никогда (древний high недостижим) → полный стоп.
   Это и есть live-симптом «wins минимальны (+0.03/+0.25), losses существенны»
   (#489/#496), который 06-30 лечили переделкой E1 — симптом, не причину.
   Латч AuctionTracker сверял «clear breakout» с уровнем 16ч давности
   (установка тренда тривиальна по ходу движения, переворот невозможен).
   Фикс: `list(reversed(kl))`. Тесты были зелёные, т.к. кормили хронологию.
2. **BE-триггер вырождался в TP.** TP и BE-триггер — из одного набора
   M5-фракталов: ближайший пред-entry swing по направлению сделки = сама
   TP-цель → «пробой уровня» = момент исполнения TP (BE-no-op). Фикс:
   триггер = пробой swing-уровня, подтверждённого ПОСЛЕ входа (`s.ts >
   ts_open`), строго между entry и TP — канон «this one print a new one».
3. **Trail ставил SL внутри absorption-уровня** (long: `anchor+buf` ВЫШЕ
   поддержки) — обычный ретест ещё не сломанного уровня выбивал позицию.
   Фикс: SL ЗА уровнем (long: `anchor−buf`, short: `anchor+buf`) — та же
   конвенция, что стоп «за зоной» §5.2.
4. **`classify` давал тренд по шуму**: доли acceptance считались от объёма
   хвостов без требования материальности — пара случайных принтов за VA
   (<1% объёма) при колоколе давала trend. Фикс: доминирующий хвост ≥
   `(1−value_area_pct)/2` общего объёма (нейтральная одно-сторонняя вне-VA
   масса, при VA 68% → 16%); порог выведен из канон-константы, не magic-number.
5. **Session-якорь прыгал в 16:00 UTC** (`session_start_ts` брал ПЕРВОЕ
   совпавшее окно): при London 07-16 + NY 12-21 якорь в 16:00 менялся
   07:00→12:00 → профиль обнулялся (терялся объём 12-16), контекст ежедневно
   уходил в warming посреди NY. Фикс: якорь = старт НЕПРЕРЫВНОГО активного
   блока (union окон, `merged_segments`: 07-21 → якорь 07:00). Бонус:
   `SymbolState.seed_vp` + `main._seed_session_vp` — бэкфилл session-профиля
   из persisted `prints` после рестарта/ротации mid-session (раньше профиль
   часами копился с нуля при живых данных в БД).

Обоснование: №1 — bugfix проводки данных; №2/№3 — канон-несоответствия
(bugfix-категория `no-data-fitting.mdc`, цитаты 39:00/§5.2); №4/№5 —
исправление явной логики. Изменения согласованы с пользователем (аудит по его
запросу, фиксы одобрены). ВАЖНО для статы: live-выборка после `f6ef82a`
(29 Jun) собрана на сломанном BE/латче — baseline tradecard flowzone сдвигать
на дату деплоя этого фикса. pytest: 1165 passed (было 1158, +7 регрессионных:
DESC-kline, pre-entry/at-TP BE-swing, trail-буфер за уровнем, шум-acceptance,
session-блок 07-21, seed_vp ×2).

**Файлы:** `src/flowzone_bot/app/main.py` (_swings_for reverse,
_seed_session_vp), `src/flowzone_bot/trading/executor.py` (_maybe_be_lock
post-entry фильтр, _maybe_trail буфер за уровень),
`src/flowzone_bot/analysis/context.py` (материальность acceptance),
`src/flowzone_bot/analysis/session.py` (merged_segments, session_start_ts),
`src/flowzone_bot/data/aggregates.py` (seed_vp), `config/settings.py`
(докстринги BE/trail), `STRATEGY_FLOWZONE.md` (§5.5),
`tests/test_flowzone_bot.py`.

---

## 2026-07-01

### fix(flowzone): E3-reconcile — close_reason по sl/tp, не по знаку net

Симптом: live #496 BTCUSDT short, `exit=58503`, `sl=58502.6` (trailed в
прибыль), `tp=58275`, `close_reason=tp_hit` при `pnl=+0.31`. Закрытие по
traill-SL в малый плюс, должно быть `sl_hit`.

Причина: `executor.reconciled_bracket_reason` переопределял `close_reason` при
REST-сверке **по знаку net** (`net>=0 → tp_hit`, `net<0 → sl_hit`) — это тот же
E3-баг, что починили в `bracket_exit_reason` (WS-путь), но он остался в пути
REST-reconciliation. WS-закрытие корректно ставило `sl_hit` (через
`bracket_exit_reason` по пересечению sl/tp), а REST-сверка перебивала на
`tp_hit`, т.к. BE/trail-SL стоит В СТОРОНЕ ПРИБЫЛИ (long: SL>entry, short:
SL<entry) → малый положительный net. Кейсы #489, #496.

Решение: `reconciled_bracket_reason(tr, exit_price, net)` делегирует канон-логике
`bracket_exit_reason(side, entry, exit, sl, tp)` (по пересечению sl/tp, не по
знаку). Если sl/tp/exit неизвестны — НЕ переопределяет (держит WS-классификацию,
не возвращается к знаку). 3 call-site обновлены под новую сигнатуру
(`_realized_from_fills`, `_rest_finalize`, `_rest_verify`). Регрессионный тест
`test_reconcile_keeps_sl_hit_for_be_trail_close_in_small_profit`.

Обоснование: bugfix канон-несоответствия (no-data-fitting.mdc bugfix-категория),
не P&L-подгонка. pytest 1158 passed. Файлы: `executor.py`,
`tests/test_flowzone_bot.py`.

---

## 2026-06-30

### fix(flowzone): BE-lock+trail к канону 39:00 (E1/E2/E3) + канон-аудит D1-D8

Полный аудит бота против трёх канон-видео Fabervaale + winkler-rulebook +
tradezella/forex.in.rs. Симптом пользователя: «wins минимальны, losses
существенны, мы поломали логику / отошли от канона». Диагноз: ядро стратегии
(контекст, зона, absorption-триггер) интерпретировано верно; **Trade
Management (BE/trail) был сделан по половине цитаты** — починено (E1/E2/E3).
Остальные расхождения — adaptations/упрощения, исполнены как детекторы/утилиты
(non-gating, per `no-data-fitting.mdc`/`strategy-guard.mdc`).

**E1 — BE-триггер переписан на канон.** Было [НАШЕ] `favourable ≥
be_lock_zone_mult × zone_width` (коммит `f6ef82a`, 29 Jun) — срабатывало слишком
рано, до «amazing explosion», обрезало wins (+0.03/+0.25 вместо +21). Канон
(Pz8f0wWW12M 39:00): *«when you **break this level**, put your stop loss to
break even»*. Триггер = пробой предыдущего swing-уровня (`_last_swing_price`) +
CVD-pressure gate (tradezella «If CVD shows strong pressure»). `be_lock_zone_mult`
удалён; добавлены `be_lock_break_structure`, `be_lock_cvd_gate`. Файлы:
`executor.py`, `config/settings.py`, `app/main.py` (swings → manage).

**E2 — trail стадия 2 (канон).** Не была реализована → winning-сделки
закрывались на откате к entry. Канон: *«this print a new one, you bring your
stop loss here and you continue»*. `executor._maybe_trail`: после BE SL едет за
последним absorption-принтом контр-стороны в стороне сделки (окно
`trail_window_sec`=тело M5), только в сторону сделки (forex.in.rs «never
re-widen»). `trail_enabled`, `trail_window_sec`. Файлы: `executor.py`,
`config/settings.py`.

**E3 — `bracket_exit_reason` по tp/sl, не по знаку.** После BE-SL в стороне
прибыли метил закрытие как `tp_hit` (#489: exit=SL, +0.25, `tp_hit`). Переписан
по пересечению `tr.tp`/`tr.sl`. Файлы: `executor.py`.

**D1 — Value Area 68% (канон-автор), не 70%.** Канон-автор буквально называет
68% (Pz8f0wWW12M 28:50 *«68% of the volume»*; winkler-rulebook). Было 0.70
(Steidlmayer/Dalton literature). `value_area_pct` и `context_accept_frac`
0.70 → 0.68 в `settings.py`, дефолты в `volume_profile.py`/`context.py`, тесты.
VA уже на 2% → больше хвостов вне VA → `classify` чаще детектит тренд. Канон-фикс.

**D2 — London+NY задокументированы как [НАШЕ] крипто-адаптация.** Канон держит
одно NY cash-окно; на крипто 24/7 cash-сессии нет → London+NY. Кода не меняли
(адаптация оправдана), атрибуция в `STRATEGY_FLOWZONE.md` §6.1.

**D3 — `merge_profiles` (composite/double-day) утилита.** Канон: *«merge them…
double day profile»*. Утилита в `volume_profile.py` + тесты. В live-путь НЕ
подключена по умолчанию (`profile_merge_enabled=false`) — OOS-валидация.

**D4 — `classify_shape` (P-shape / double-distribution / balance / shift).**
Канон различает паттерны формы. Обогащение `ctx.shape` (non-gating), тесты + док
`STRATEGY_FLOWZONE.md` §2.1. `profile_shape_enabled=true` (обогащение, не
меняет торговое решение).

**D7 — `detect_initiative` / `detect_exhaustion` детекторы.** Канон описывает
initiative (continuation) и exhaustion (reversal) паттерны. Детекторы в
`orderflow.py` + тесты. В live-вход НЕ гейтят по умолчанию
(`initiative_exhaustion_enabled=false`) — основной канон-сетап absorption (§4).

**D8 — атрибуция трёх канон-видео.** Doc указывал один канон (06R-ebyOhDI), но
§5.5 — из Pz8f0wWW12M, min_rr — из cUTsoU-15Tc. Header `STRATEGY_FLOWZONE.md` +
`__init__.py` обновлены: три ролика + winkler/tradezella/forex.in.rs доп.

Обоснование non-gating D3/D7: `strategy-guard.mdc`/`no-data-fitting.mdc`
запрещают менять торговую логику без OOS-валидации; детекторы/утилиты готовы к
форвард-эксперименту, live-гейтинг — отдельной правкой по данным. E1/E2/E3 —
bugfix-категория (канон-несоответствие), не P&L-подгонка; 3 сделки после
`f6ef82a` — шум (`sample-size.mdc`).

**Файлы:** `src/flowzone_bot/analysis/{context,volume_profile,orderflow}.py`,
`src/flowzone_bot/config/settings.py`, `src/flowzone_bot/__init__.py`,
`src/flowzone_bot/trading/executor.py`, `src/flowzone_bot/app/main.py`,
`tests/test_flowzone_bot.py`, `STRATEGY_FLOWZONE.md`. pytest: 1157 passed.

---

## 2026-06-29

### feat(flowzone): BE-lock + R:R-флор 1:2 — канон Trade Management (стадия 1)
`f6ef82a`

Контекст: flowzone кровоточит ~2 недели. За 7д: 116 сделок, WR 28%, **−$239**
(pnl = net closedPnl Bybit, verified=115/116 — ground truth), z≈−4.7 vs 50%
(p<0.001), n=116 ≥ 100 (порог sample-size для решений достигнут). R:R-фильтр
≥2.5 (06-25) не починил причину — только порезал частоту: 24→3→1→0→0 сделок,
бот встал (0 входов с 06-28: на крипто BTC/ETH/SOL zone-stop широкий →
R:R≥2.5 недостижимо; канон калиброван под глубокий NQ cash-session).

Research (источники автора — Fabio Valentini / Fabervaale): сайт fabervaale.com,
бесплатный Telegram t.me/fabervaaleEng, видео «The Only Orderflow Guide You'll
Ever Need» (youtu.be/Pz8f0wWW12M, раздел **39:00 Trade Management**), письм.
rulebook winkler.expert/reports/fabervaale-rulebook.html, playbook
tradezella.com/strategies/auction-market-strategy (2 модели: continuation +
mean-reversion — у нас только первая).

Решение (канон-faithful, 2 расхождения с каноном):
1. **R:R 2.5 → 2.0** — канон-флор «1 to 2» первоисточника Fabervaale (ролик
   cUTsoU-15Tc: «maybe it's 1 to 2, 1 to 2.5»). 1:2 — нижняя граница канона,
   не data-fitting. Возобновляет входы на крипто.
2. **BE-lock по пробою края зоны** — канон Trade Management (видео 39:00):
   «after breaking out of complete absorption → put your stop loss to break
   even». `executor._maybe_be_lock`: favourable ≥ `be_lock_zone_mult` ×
   zone_width → SL в entry ± `sl_buffer_bps` (anti-flicker, покрыть fees).
   Прямо бьёт по 72% SL-hit: лузеры, что вернулись → scratch на BE. Зона-
   границы persist-ятся в БД (`zone_low`, `zone_high` — аддитивная миграция).
   Idempotent: persisted `tr.sl` = cross-tick idempotency key (executor
   rebuilds tr from DB each cycle; in-memory `_be_locked` не нужен). Если SL
   уже в BE → silent no-op. Выключаемо: `FLOWZONE_BE_LOCK_ENABLED`.

Стадии: 1 (R:R-флор + BE-lock) — этот коммит; 2 — трейл по order-flow (канон
«this print a new one, you bring your stop loss here»); 3 — ужесточение
`detect_absorption` (закрытие свечи против агрессора + повторные failed-ретесты).

**Файлы:** `config/settings.py` (`min_rr=2.0`, `be_lock_enabled`,
`be_lock_zone_mult`), `trading/executor.py` (`_maybe_be_lock`, `_be_sl`,
persist zone в `on_signal`), `state/db.py` (`zone_low`/`zone_high` + миграция),
`analysis/strategy.py` (research-блок), `STRATEGY_FLOWZONE.md` (§5.5, §11.6),
`tests/test_flowzone_bot.py` (BE-lock 6 тестов + R:R-флор + DB zone persist).

### анализ(flowzone): диагностика убыточности и стопа входов
`f6ef82a` (без кода — фиксация наблюдения per sample-size.mdc)

Симптом: «flowzone почти перестал ставить, всё в минус». Диагностика (БД
flowzone_bot.sqlite, net closedPnl Bybit): частота обвалилась после canon-
реврайта 06-25 (24→3→1→0→0; 0 входов 06-28/06-29); за 7д −$239, WR 28%,
n=116 (p<0.001). R:R-фильтр ≥2.5 на крипто делает входы недостижимыми (ctx=
warming 06-28; ctx=trend_*, session=active, open=0 06-29 — silent `return None`
в `evaluate`: нет строки генерации/отброса сигнала). Пост-filter сделки (rr≥2.5,
n=9) всё равно минус (3TP/6SL, реализованный payoff 1.47:1 < канон 2.5:1).
Бонус-баг: на части сделок sl≈entry (R:R считается мусором ~1e12) — до фильтра.

Файлы: `BUILDLOG_FLOWZONE.md` (запись), `scripts/scalp_perstrat_since.py`
(коротко: cutoff flowzone не в этом скрипте — БД отдельная)

### анализ(flowzone): выбор монет (канон-ликвидность vs эмпирика) — список оставлен
`<без кода>` (фиксация наблюдения per sample-size.mdc; решение пользователя 2026-06-29)

Канон (AMT/orderflow, STRATEGY §6.1/§6.3) требует глубокую ликвидность + тесный
спред — absorption/big-trades читаются только на ликвидности. Bybit tickers:
BTCUSDT $3.88B/0.02bps, ETHUSDT $1.63B/0.06bps (канонично-глубокие);
SOLUSDT $0.81B/1.38bps (в 23× шире spread, 1/5 оборота → НЕ каноничен, footprint
зашумлён). HYPEUSDT $0.21B/0.32bps — погранично.

Эмпирика (net closedPnl, last 7d, n=116):
- ETHUSDT: 24 tr, WR 42%, **+$82.29** (avgW $16.74 / avgL −$6.08) — единственный +
- BTCUSDT: 34 tr, WR **12%**, **−$156.26** (4W/30L, avgW $19.28 / avgL −$7.78)
- SOLUSDT: 51 tr, WR 33%, **−$110.74** (avgW $3.66 = TP на шуме / avgL −$5.09)
- since canon-rewr 06-25 (n=28): BTC 1W/10L WR 9% −$98; SOL WR 62% +$1; ETH 44% +$4

Парадокс BTC: глубочайший/каноничнейший, но худший WR 12% — НЕ ликвидность, а
strategy-fit (M5 choppy, зона-стоп прошибается ложными разворотами; avgL −$11.33
шире ETH −$8.16). ETH (тоже глубокая) работает → проблема в BTC-микроструктуре
под текущий канон. SOL: тонкий И минус (двойной провал).

Sample-size: BTC n=34<100 → формально отключать рано (p<0.001, z≈−4.4 vs 50%).
**Решение пользователя: список BTC/ETH/SOL оставить без изменений.** Наблюдаем
после стадии 1 (R:R 1:2 + BE-lock) — улучшит ли BE-lock WR на BTC/SOL. Если BTC
продолжит лить после стадий 2/3 и n≥100 — пересмотреть.

---

## 2026-06-25

### feat(flowzone): R:R-фильтр ≥ 1:2.5 — канон Fabervaale (возврат, не data-fitting)
`<pending commit>`

Контекст: live-сделка #468 ETHUSDT — `tp_hit` с убытком −1.59 (reward 0.47 /
risk 6.35 = rr 0.07; gross 0.24 < round-trip fees 1.83). swing-цель оказалась
ближе entry, чем зона/стоп → TP математически не окупался.

Канон: Fabervaale, ролик «The Simplest Orderflow Trading Model» (cUTsoU-15Tc) —
«our real risk-to-reward… maybe it's 1 to 2, 1 to 2.5»; chartfanatics AMT-strategy
(Fabio) — «Reward-to-Risk 1:2.5 to 1:5». Цель фильтра = не data-fitting, а
возврат к канону: сделка берётся только если reward/risk ≥ 2.5.

Реализация: в `evaluate` (после расчёта sl/tp) — `reward=|tp−last|`,
`risk=|sl−last|`; если `risk<=0 or reward/risk < cfg.min_rr` → `None`.
`min_rr=2.5` (settings.py, env-reversible). В `reasons` добавлен `rr=N.N`.
Синтетические positive-тесты сдвинуты (swing 110→95), чтобы rr ≥ 2.5 —
тест проверяет чеклист, не R:R (правило no-data-fitting.mdc: не подгонять
тестовые данные под ожидание, но positive-сценарий требует валидной геометрии
канона). Добавлен `test_evaluate_rr_filter_rejects_close_swing`.

Файлы: `analysis/strategy.py`, `config/settings.py`, `tests/test_flowzone_bot.py`,
`STRATEGY_FLOWZONE.md` (§11.6)

### fix(flowzone): close_notify_fallback 10→30с — убрать ложный знак provisional-уведомления
`<pending commit>`

Симптом: TG `🔴 close #462 SOLUSDT pnl=≈$-0.25 (tp_hit)` — закрытие по TP с
отрицательной оценкой. Реальный исход по REST closedPnl +$0.49 (pnl_verified=1).

Причина: биржевой bracket (TP/SL) детерминирован, но WS-исполнения атрибутируются
с задержкой → закрытие уходило как provisional (is_real=False) с taker_pnl-оценкой
по mark_price на остаток позиции. Через `close_notify_fallback_sec=10с` fallback
отправлял TG с provisional `tr.pnl_usd` (≈), раньше чем REST давал точное число.
Оценка по mark_price с taker-fee на остаток дала лжевой знак на малом ходе SOL.

Решение: дефолт `close_notify_fallback_sec` 10→30с (`settings.py`) + fallback
getattr-дефолт в `executor.py` синхронно. 30с дают REST-сверке время до
fallback-уведомления — TG приходит уже с точным closedPnl (без ≈, без лжезнака).
UX-тайминг, не торговая логика (`no-data-fitting.mdc` не применимо).

**Файлы:** `config/settings.py`, `trading/executor.py`

### feat(strategy): приведение кода к канону — A1/A5/A6/A2/A3 исполнены
`<pending commit>`

Исполнены расхождения из §11 аудита кода (согласовано с пользователем). Все тесты
зелёные (1071 passed). Канон = ролик Fabervaale (07:13); правки — возврат к
каноничной торговле, не новая логика.

- **A1 (стоп = зона × N).** Стоп = far edge зоны + `sl_zone_mult` × ширина зоны
  (канон «1-2-3/1-2-4/1-2-5»). Убраны `sl_buffer_bps` (как торговый множитель) и
  `min_sl_bps` — плоский буфер ломал R:R (на узкой зоне раздувал стоп, на широкой
  ставил ближе «1-2-5»). `sl_buffer_bps` сохранён как технический анти-фильтр.
- **A5 (цель только swing).** Удалён `_structural_target` — фолбэк TP на
  POC/VAL/VAH при отсутствии swing. Канон §5.3: цель = только swing point; без
  swing-цели сделка НЕ берётся. Убран `tp2_level`.
- **A6 (полный выход, без partial).** Удалены `partial_exchange_tp`,
  `_maybe_partial`, поле `_partial`, `partial_fraction`. Канон: полный выход на
  swing point + re-entry отдельной сделкой на следующей зоне (§8). Это другой
  trade management, не «take profit on the first one» + «condition again».
- **A2 (per-session контекст + per-swing зона).** Дневной `vp_buckets` удалён.
  Контекст `classify` — по форме **per-session** профиля (якорь = старт London/NY
  окна, `session.session_start_ts`). Зона — профиль **предыдущей swing-точки**:
  исполненный поток (footprint) в окне `[ts prev swing, now]` из новой таблицы
  SQLite `prints`. Принты persist-ятся background `PrintStore` (batched flush из
  daemon-потока, чтобы не блокировать WS-callback; retention 6ч). `Swing.ts`
  добавлен для якоря окна. Канон требует профиль из исполненного потока (footprint),
  не kline-volume (`no-data-fitting.mdc`).
- **A3 (breakout-гейт в docstring).** `classify` — чистая функция формы профиля;
  breakout-гейт «clear breakout of the previous level» (канон §2) выполняется в
  `AuctionTracker.update` (swings-пробой). Торговый путь всегда
  `auction.update(classify(...))` → вход требует breakout+acceptance. Docstring
  `context.py` явно фиксирует разделение.

Числовые пороги A4/B2/B3/B5/B1 оставлены как [НАШЕ] (B1 `min_confluence=3` —
согласовано с пользователем); изменение требует обоснования данными
(`no-data-fitting.mdc`, `sample-size.mdc`).

**Файлы:** `analysis/strategy.py`, `analysis/context.py`, `analysis/swings.py`,
`analysis/volume_profile.py`, `analysis/session.py`, `data/aggregates.py`,
`data/print_store.py` (новый), `state/db.py`, `trading/executor.py`,
`config/settings.py`, `app/main.py`, `tests/test_flowzone_bot.py`,
`STRATEGY_FLOWZONE.md` (§11.6), `BUILDLOG_FLOWZONE.md`

### docs(strategy): аудит КОДА flowzone vs первоисточник — разметка расхождений
`<pending commit>`

Повторный аудит — теперь **кода** `src/flowzone_bot/` против ролика-первоисточника
(Fabervaale ENG, 07:13). В `STRATEGY_FLOWZONE.md` добавлен §11 «Аудит кода vs
первоисточник»: точная стратегия канона с терминологией автора + мировыми
терминами, и таблица расхождений с разметкой [КАНОН]/[RESEARCH]/[НАШЕ].

Главные расхождения (где мы доработали/подогнали/сломали):
- **A1** `strategy.py:99-104` / `settings.py:163-166` — стоп **сломана
  математика**: канон масштабирует стоп от ширины зоны (1-2-3/4/5 = зона×N,
  selectable R), код — плоский `sl_buffer_bps=8` + пол `min_sl_bps=10`. Влияет на
  R:R и частоту стопов.
- **A2** `aggregates.py:127-130` / `main.py:267` / `context.py` — **подгонка
  инфры**: профиль один кумулятивный ДНЕВНОЙ (UTC), а канон строит зону от
  профиля **предыдущей swing-точки** (fixed profile по swing/dealing range) и
  контекст от **сессионного** профиля. Зоны/контекст считаются не от того объекта.
- **A6** `executor.py:477-507` / `strategy.py:40,109` / `settings.py:185` —
  **доработка логики**: частичная фиксация 50% + стоп в БУ вместо каноничного
  полного выхода на swing point + re-entry на новой зоне.

Вторичные: A5 (структурный TP-фолбэк на POC/VAL — не в ролике), B1
(`min_confluence=3` жёстко — канон назвал 3 как пример, не инвариант; §7 «≥2»),
A3 (`classify` без проверки breakout previous level — частично закрыто
`AuctionTracker`). Числовые пороги A4/B2/B3/B5 (0.70, 0.90, 0.5, 0.6, 0.5, 5,
10) — [НАШЕ] конкретизации, требуют обоснования данными.

Код НЕ правился — только фиксация аудита в doc. План приведения к канону (A1/A2/
A6 — изменения торговой логики/инфры, по `strategy-guard.mdc` требуют
согласования) — в §11.5, отдельный коммит.

**Файлы:** `STRATEGY_FLOWZONE.md` (§11)

### docs(strategy): аудит STRATEGY_FLOWZONE к первоисточнику — фикс атрибуции канон vs research
`<pending commit>`

Аудит `STRATEGY_FLOWZONE.md` против расшифровки ролика-первоисточника (Fabervaale
ENG, 07:13). Логика канона подтверждена полностью; исправлены места, где документ
приписывал ролику то, чего в нём нет (нарушение `strategy-guard.mdc` /
`no-data-fitting.mdc`):

- §0/§1/§5.4: восстановлен буквальный термин канона «reversal area following the
  direction of the trend» (в ролике слово «continuation» не звучит — это наша
  интерпретация, теперь явно помечена).
- §3.1: «≈70%» ширина value area и термины POC/HVN/LVN — это research Market
  Profile (Steidlmayer/Dalton), не произносятся в ролике; добавлена атрибуция.
- §3.2: «delta print» — название индикатора платформы deep charts, не
  универсальный термин; уточнено.
- §0/§5.3/§7/§9: убрана «частичная фиксация» (в ролике только «targeting for a
  swing point» + «take profit on the first one»); partial перенесён в разряд
  решения реализации (§10).
- §6.3: M5/NQ — визуальный вывод из кадра экрана, в речи не произносятся;
  добавлена оговорка.
- §10: «Williams-фрактал = previous level» переписано как «previous level
  реализован нами через Williams-фрактал» (метод не детализируется каноном).
- §9: добавлены строки-атрибуции `⚠` для research/решений, не являющихся каноном.

Торговая логика канона не менялась; правка — только корректность атрибуции
источников (канон vs research vs наше решение).

**Файлы:** `STRATEGY_FLOWZONE.md`

### feat(strategy): sticky-направление аукциона — фикс ложных переворотов (v0.3.0)
`<pending commit>`

Наблюдение (forward на канон-логике v0.2.0, baseline flowzone=2026-06-22 13:55,
n=84): net **−$84.96**, WR 23%, все 4 дня в минусе. Разрез по стороне —
ключевой: **long 44 сделки, WR 16%, −$147.36**; **short 40, WR 30%, +$62.39**.
Грейд анти-монотонен (tradecard Spearman ρ=−1.00): чем выше confluence-score, тем
хуже. Симптом → причина: мгновенный `context.classify` читает форму ДНЕВНОГО
footprint-профиля; при внутридневном откате value area мигрирует, встречный хвост
перевешивает → ложный `trend_up` после нисходящего аукциона → бот берёт
continuation-**лонг в контртренд/чоп** («первое движение», которое канон велит НЕ
брать).

Канон (ролик 00:33–06:00, <https://youtu.be/06R-ebyOhDI>): направление аукциона
задаётся **«clear breakout of the previous level» + acceptance** и **держится**
(автор перезаряжает шорт по новым dealing range, НЕ переворачивается в лонг внутри
down-аукциона); *«I didn't take the first movement… the second movement was so
clear»*.

Решение — новый `analysis/auction.py::AuctionTracker`: направление ЛАТЧИТСЯ на
символ (якорь UTC-день, как профиль §6.3) и адоптируется/переворачивается ТОЛЬКО
когда ОБА: (1) мгновенный `classify` = тренд в эту сторону (acceptance вне VA) И
(2) цена пробила последний подтверждённый **swing-экстремум** (Williams-фрактал
§5.3 = «previous level»). Откат/баланс/неподтверждённый встречный хвост НЕ
сбрасывают направление (sticky = «второе движение»). `classify` остаётся чистой
функцией; латч-логика поверх. **Без новых числовых порогов** (swing left/right и
accept_frac уже существуют) — research=источник правды (strategy-guard.mdc), не
подгонка под P&L (n=84 < 100 — правка обоснована расхождением с каноном, данные —
corroboration).

**Файлы:** `analysis/auction.py` (new), `app/main.py` (латч в `_scan_signals`/
`_context_for`/`_heartbeat`, swings до контекста), `analysis/context.py`
(докстринг: classify = мгновенный), `__init__.py` (0.2.0→0.3.0),
`STRATEGY_FLOWZONE.md` §10, `tests/test_flowzone_bot.py`. tradecard baseline
flowzone → 2026-06-25 (новая граница логики).

---

## 2026-06-22

### feat(strategy): приведение реализации СТРОГО к канону ролика (v0.2.0)
`<pending commit>`

Наблюдение (tradecard weekly 2026-26, baseline flowzone=2026-06-17): live n=266,
WR 35%, net **−$606.48**, EXP −0.53. Детекторы: `factor_noise` по `ctx:trend_down`
И `ctx:trend_up` одновременно (оба не предиктивны — контекст-фильтр = шум); грейд
монотонна (ρ=0.80), но низ score=2 (n=64, EXP −0.72) = ~75% убытка. Это симптомы
расхождения РЕАЛИЗАЦИИ с каноном ролика (<https://youtu.be/06R-ebyOhDI>), а не
шум выборки. По запросу пользователя — пересмотр ролика и приведение строго к
канону одной правкой (research = источник правды, strategy-guard.mdc).

Расхождения канон↔код и фиксы (каждый со ссылкой на ролик/Market Profile):
1. **Ликвидность (§6.1/§6.3, канон=NQ).** Авто-ротация range/RVOL тянула тонкие
   памп-альты, где footprint/absorption шумят. → `auto_universe_enabled` default
   **False**; торгуем глубочайшие перпы (BTC/ETH/SOL) как аналог NQ-глубины.
2. **Контекст (§2, Steidlmayer/Dalton).** Было: тренд = ≥50% объёма ОКНА за
   границей VA (шумит, флипает на откате, срабатывает в обе стороны). → `classify`
   v2: режим по ФОРМЕ дневного профиля — направленный acceptance ВНЕ value area
   (доля хвоста ≥ **0.70**, каноничная VA-константа). Устойчив к откату к зоне
   reload (канон «второе движение»). Убран `context_accept_window_sec`.
3. **Confluence (§3.4 «super strong area»).** Было `zone_min_confluence=2`. Канон
   называет три фактора (VAH + big trades + delta) → **3**.
4. **Absorption (§4 + §6.3).** Было окно 120с. Канон «deep trades in the body of
   the candle», ТФ входа M5 → окно = **тело M5-свечи (300с)**.

**Файлы:** `analysis/context.py` (classify по форме профиля), `config/settings.py`
(дефолты + докстринги), `app/main.py` (`_context_for`), `docker-compose.yml`
(`FLOWZONE_AUTO_UNIVERSE_ENABLED=false`), `__init__.py` (0.1.0→0.2.0),
`STRATEGY_FLOWZONE.md` §10, `tests/test_flowzone_bot.py`.

**Стата:** baseline tradecard flowzone сдвинут на 2026-06-22 (логика сменилась —
старые сделки не смешиваем; sample-size.mdc). Вывод «лучше/хуже» — по n≥100 OOS.

---

## 2026-06-17

### fix(stats): REST closedPnl — источник правды для ВСЕХ закрытых live (true-up)
`<pending commit>`

Симптом: после фикса партиалов оставался систематический дрейф ~+$1.29/сутки
(синхронный срез 08:35 UTC: БД +$11.77 vs Bybit closedPnl +$10.48), сконцентрир.
в мульти-филл сделках (JTO +0.80, XLM +0.59). Прошлые «расхождения» +$19/+$4.45
оказались артефактами НЕсинхронных снимков (БД и Bybit читались с лагом ~12 мин,
бот успевал наторговать) — пропавших строк/багов там не было.

Причина (исследование офдоки + BUILDLOG_SCALP v0.18.11/12/13): `reconcile`
досверял против биржи ТОЛЬКО `pnl_provisional=1`. Сделки, закрытые WS как
«complete» (qty≥98%), брали net из WS-леджера (`Σ execPnl − Σ execFee`) и больше
НЕ сверялись. В гонке «позиция закрыта (get_position) vs WS-филл» терялся fill с
комиссией → net занижал комиссию → дрейф копился и не чинился.

Канон (офдок Bybit):
- [closed-pnl](https://bybit-exchange.github.io/docs/v5/position/close-pnl):
  `closedPnl` уже net (gross − `openFee` − `closeFee`, оба поля в ответе); funding
  идёт отдельными записями (`execType` = `Settle`/`SessionSettlePnL`).
- [execution/list](https://bybit-exchange.github.io/docs/v5/order/execution):
  realized PnL ОТСУТСТВУЕТ (только `execFee`) → REST execution не источник pnl.
→ REST `closedPnl` — единственный авторитетный источник; WS первичен лишь для
быстрого provisional (низкая задержка для killswitch/уведомлений).

Решение: REST closedPnl досверяет ВСЕ закрытые live-сделки, не только provisional.
- БД: колонка `pnl_verified` (+аддитивная миграция ALTER для существующих БД),
  `verify_pnl()` (ставит net + provisional=0 + verified=1), селектор
  `unverified_closed_live_since()` (исключает paper и технические entry_*/
  restart_flat закрытия — у них нет closedPnl).
- `reconcile`: цикл 2 — универсальный true-up unverified закрытых live против
  closedPnl (общий бюджет 3/цикл + throttle 300с). WS-net снимает provisional,
  но НЕ verified → true-up досверяет и ловит дрейф комиссий. Неоднозначные
  (несколько сделок того же символа+entry, не делимы по closedSize) после 3
  неудачных REST-попыток принимаем как WS-net (verified) — не жжём бюджет.
- `closed_pnl_position`: фильтр `execType ∈ {Trade,BustTrade}` (funding/Settle не
  искажают матч по объёму) + допуск по entry ужесточён `1e-3 → 1e-5` (как в
  `closed_pnl_detail`): быстрые reload'ы по символу заходят на близких ценах
  (напр. JTO 0.7689 vs 0.7684 = 0.065%) — широкий допуск слипал их записи, Σ
  closedSize ≠ qty → REST сдавался. avgEntryPrice совпадает с нашим entry до 0%.
- REST-пути (`_rest_finalize`/`_rest_verify`) зовут `verify_pnl` (авторитетно) —
  одного запроса достаточно, без повторной сверки.

Существующие 86 закрытых сделок самобэкфилятся reconcile-ом после деплоя (по
бюджету за несколько минут). Чисто учётный слой: стратегии/сайзинг/входы/выходы
НЕ затронуты; killswitch и reload-тайминг станут точнее. Тесты: +8 (true-up
дрейфа, give-up после N, селектор, verify_pnl, execType-фильтр), всего 46
flowzone / 1334 общих — зелёные.

**Файлы:** `src/flowzone_bot/state/db.py` (колонка+миграция+verify_pnl+селектор),
`src/flowzone_bot/trading/executor.py` (reconcile цикл 2 + `_fetch_closed_pnl` +
`_rest_verify`), `src/flowzone_bot/trading/client.py` (execType-фильтр),
`tests/test_flowzone_bot.py`

### revert(universe): flowzone обратно на rvol (momentum-отбор — наблюдаемый регресс)
`<pending commit>`

Откат дефолта `FLOWZONE_UNIVERSE_METHOD` momentum→rvol по наблюдению
пользователя: на форвард-тесте momentum-отбора (топ-15 по 24h движению, без
анти-памп кэпа) виден заметный регресс flowzone vs штатный rvol-селектор.

Это согласуется с канон-оговоркой (STRATEGY §6.1): footprint/absorption читаемы
на ЛИКВИДНОСТИ; momentum тянул тонкие памп-альты (BEAT/LAB/H/WLD без анти-памп
кэпа) где order-flow шумит. Откат к ПРОВЕРЕННОМУ baseline (rvol) — консервативное
направление, НЕ отключение стратегии по малой выборке (`sample-size.mdc`).
Выборка за ~4ч < порога значимости — «регресс» статистически не доказан, поэтому
вывод фиксируется как НАБЛЮДЕНИЕ, не валидированное решение; код момент-селектора
и переключатель сохранены (env-реактивируемы для будущего A/B на n≥100).

Scalp_bot остаётся на momentum (отдельный эксперимент пользователя, не тронут).

**Файлы:** `docker-compose.yml`

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
