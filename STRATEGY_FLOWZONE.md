# STRATEGY — flowzone_bot

**Канон (единственный источник правды):** Fabervaale ENG — «How To Find The
BEST Entry Zones» — <https://youtu.be/06R-ebyOhDI>

Этот документ описывает стратегию **полностью и автономно**, строго по
первоисточнику (ролику). Он НЕ сравнивает методику с другими ботами проекта и
НЕ содержит наших готовых решений — только канон. Любая будущая правка логики
flowzone_bot сверяется с этим документом и с роликом, а не с интуицией.

---

## 0. Суть в одном абзаце

Цену двигает **только объём** (исполненные сделки). Мы определяем **контекст
аукциона** (тренд или баланс), находим **зоны высокой вероятности** через
объёмный профиль + поток ордеров (delta print, крупные сделки), и входим **по
направлению аукциона** (reversal area following the direction of the trend —
зона отката для перезарядки в сторону тренда), когда в зоне приходит
**подтверждение потоком** — поглощение (absorption) противоположной стороны.
Стоп — сразу за зоной, цель — ближайшая swing-точка, и перезарядка (re-entry)
на следующей сильной зоне.

---

## 1. Философия (Auction Market Theory)

- **Рынок — это аукцион.** Цена ищет баланс (где идёт двусторонняя торговля) и
  совершает направленные движения, когда баланс нарушается. Источник правды о
  происходящем — **исполненный объём**, а не свечи и не классические уровни
  поддержки/сопротивления.
- **Торгуем ПО направлению аукциона.** Канон называет это «sensitive high
  probability **reversal area** following the direction of the trend» — зона
  отката, где мы перезаряжаем позицию в сторону уже установленного тренда
  (continuation), а не разворачиваемся против него. Если рынок в трендовом
  сценарии вниз — ищем точки для **перезарядки шорта**; если вверх — для
  перезарядки лонга.
- **Никаких догадок.** Каждый вход подтверждается реальным потоком ордеров
  (delta / крупные сделки / поглощение), наблюдаемым **в реальном времени**, а
  не постфактум.

> Цитата канона: *«Sensitive high probability reversal area following the
> direction of the trend… the only thing that move price is volume… no
> conspiracy theory, just visualization of order entering the market.»*

---

## 2. Шаг 1 — Контекст рынка (profile shape)

Прежде чем искать вход, классифицируем рыночный сценарий по **форме объёмного
профиля**:

- **Трендовый сценарий (trade THIS):** есть **чистый пробой** предыдущего уровня
  с выраженной агрессией, и после пробоя цена **акцептируется (acceptance)** за
  пределами value area — то есть торгуется и принимается **ниже Value Area Low**
  (для шорта) или **выше Value Area High** (для лонга). Это значит: рынок ищет
  **новый баланс**, и мы ожидаем **направленное продолжение**.
- **Балансовый сценарий:** цена ходит внутри value area, акцепта за её границами
  нет — направленного ожидания нет, входов по этой методике **не берём**.

> Цитата канона: *«London session start with clear breakout of the previous
> level, a lot of aggression… they created a condition of trend scenario…
> because they accepted after the breakout below the value area low… we are
> seeking new balance of price and what we can expect here is direction.»*

**Важно:** **не берём первое движение** (часто оно до открытия сессии / без
подтверждённого контекста). Ждём **второе, ясное** движение в установленном
направлении.

> Цитата канона: *«I didn't took the first movement because it was before the
> opening of the London session, but the second movement was so clear.»*

---

## 3. Шаг 2 — Определение торговой зоны (где может быть вход)

Зона — это **место максимального давления в прошлом**, куда цена вернётся для
перезарядки. Строится по объёмному профилю предыдущей swing-точки. Компоненты:

### 3.1 Объёмный профиль (Volume Profile)
- **Value Area High / Low (VAH / VAL)** — границы value area, зоны, где
  сосредоточено основное принятие объёма. В ролике автор оперирует понятием
  «value area low/high» без числового определения; каноничная константа ширины
  value area **≈70%** общего объёма и термины POC/HVN/LVN — из Market Profile
  (Steidlmayer / Dalton), это research-источник, не произнесённый в ролике.
- **POC (Point of Control)** — цена с максимальным объёмом (термин Dalton).
- **Volume ledge** — место, где объём **резко** переходит от high-volume node
  (пик) к low-volume node (провал). Это сильный ориентир зоны.

> Цитата канона: *«the most relevant area… is this clear volume ledge. Volume
> ledge is where the volume goes from really peak point to really low point…
> from high volume node to low volume node really fast.»*

### 3.2 Delta print
- «Delta print» — в ролике это **название конкретного индикатора** платформы
  deep charts («one indicator that show you the exact area called delta
  print»), а не универсальный термин литературы. На swing-точке он показывает,
  **сколько было давления** через дельту (агрессивный buy − sell), исполненную
  ИМЕННО на этом уровне. Это зона, основанная на фактически исполненном потоке,
  а не на свечах или S/R.

> Цитата канона: *«you can measure how much pressure there was in the market
> during this swing point using the delta profile… this is not based on some
> support and resistance… is based on actual orderflow data that got executed
> previously.»*

### 3.3 Крупные сделки (big trades)
- Уровни, где объём был **поддержан крупными исполненными сделками** —
  важнейшие ценовые уровни.

> Цитата канона: *«this area is based on the profile of the previous swing
> point… you can see is when the volume got support by these big trades and
> this is the reason you see there is a lot of delta pressure on this area.»*

### 3.4 Конфлюэнс (confluence) = «super strong area»
- Самая сильная зона — там, где **совпадают несколько факторов**: например
  **Value Area High + big trades + delta-уровень**. Чем больше совпадений — тем
  выше вероятность отработки.

> Цитата канона: *«it's nice to notice also that the value area high it's here.
> So you have a confluence of value area high, big trades and delta level. This
> one is a super strong area.»*

**На зону ставим алерт** и ждём, когда цена к ней подойдёт.

---

## 4. Шаг 3 — Триггер входа (подтверждение потоком в зоне)

Сама по себе зона — **не сигнал**. Вход берём **только** при подтверждении
ордерфлоу, когда цена доходит до зоны:

1. **Реакция агрессоров в направлении сделки.** При подходе к зоне видим
   **сильных агрессивных продавцов** (для шорта) / покупателей (для лонга).
2. **Поглощение (absorption) противоположной стороны.** Ключевой триггер:
   контр-сторона пытается перехватить контроль, но её **поглощают**. Видно по
   **агрессивным deep-trades в ТЕЛЕ свечи**: «битва» за уровень выиграна
   доминирующей стороной.
   - Для **шорта**: агрессивные покупатели поглощены продавцами → «failed
     buyers».
   - Для **лонга** (зеркально): агрессивные продавцы поглощены покупателями.
3. **Подтверждение в реальном времени.** Серия «control of buyers… failed
   buyers» (или зеркально) на потоке = подтверждение. Данные не задержанные.

> Цитата канона: *«this battle of the sellers got won by sellers. Why?
> Aggressive orders, deep trades in the body of the candle… absorption of the
> aggressive buyers… buyers try to take control again but they got absorbed…
> control of the buyers… failed buyers… you can use them as a confirmation to
> execute your trades.»*

Только после подтверждения — **ставим лимитный ордер** в зоне (или исполняем,
защищаясь за зоной).

> Цитата канона: *«after this candle the situation is super clear, so you can
> already put a limit order here… from here the market collapse.»*

---

## 5. Шаг 4 — Управление сделкой

### 5.1 Вход
- **Лимитный ордер** в зоне после подтверждения потоком.

### 5.2 Стоп-лосс
- **Сразу ЗА зоной**: для шорта — **выше** идентифицированной area; для лонга —
  ниже. Стоп защищает «область, которую мы уже определили».
- **Масштаб стопа 1-2-3 / 1-2-4 / 1-2-5** — насколько консервативно ставить
  стоп (дальше за зону = безопаснее, но хуже R). Выбор зависит от желаемого
  запаса прочности.

> Цитата канона: *«execution that you can make protecting yourself above the
> area that we already identified… this is a 1-2-3, 1-2-4, 1-2-5, it depends on
> how much you want to be safe on the stop-loss placement.»*

### 5.3 Цели и перезарядка (re-entry)
- **Цель — ближайшая swing-точка** («targeting for a swing point»). Канон не
  уточняет долю снимаемого объёма; любой partial/масштаб фиксации — решение
  реализации (см. §10), не часть канона.
- После взятия цели на первой сделке — рынок создаёт **новый dealing range** и
  **новую super strong swing point**; входим **re-entry** в ту же сторону,
  защищаясь за новой зоной.

> Цитата канона: *«you can take also a re-entry here, covering above the area,
> targeting for a swing point… after you go to take profit on the first one,
> you have a condition again of super strong swing point.»*

### 5.4 Направление
- **Только по направлению аукциона/тренда.** Канон буквально: «sensitive high
  probability **reversal area following the direction of the trend**» — зона
  отката для входа в сторону уже установленного тренда. Контртренд по этой
  методике **не торгуем**.

### 5.5 BE-lock — вынос стопа в break-even (канон Trade Management)
- **После пробоя уровня поглощения** — стоп в **break-even** (risk-free), затем
  трейл по order-flow-агрессии. Реализовано в `executor._maybe_be_lock`.
- **Триггер**: цена прошла в сторону сделки на масштаб зоны поглощения
  (`favourable ≥ be_lock_zone_mult × (zone_high − zone_low)`). Это канон-точно
  («after breaking out of complete absorption»), не 1R-прокси. Зона-границы
  persist-ятся в БД (`zone_low`, `zone_high`).
- **BE-уровень** = entry ± `sl_buffer_bps` (anti-flicker буфер, покрывает
  round-trip fees — чтобы BE не стал микро-убытком).
- **Idempotent**: persisted `tr.sl` — ключ cross-tick idempotency (executor
  rebuilds `tr` from DB each cycle; in-memory `_be_locked` не нужен). Если SL
  уже в BE (round to tick == `tr.sl`) → silent no-op.
- Выключаемо через env `FLOWZONE_BE_LOCK_ENABLED`. Trail по order-flow —
  стадия 2 (канон «this print a new one, you bring your stop loss here»).

> Цитата канона (видео «The Only Orderflow Guide You'll Ever Need», 39:00 Trade
> Management): *«you can decide to go from 1 to 2 to 1 to 5 and put your stop
> loss to break even… after breaking out of the sellers of complete absorption
> and you have an amazing explosion where you can trail your position following
> the aggression of the market… this print a new one, you bring your stop loss
> here and you continue.»* — <https://youtu.be/Pz8f0wWW12M>

---

## 6. Сессии и тайм-фреймы (масштаб)

### 6.1 Сессии
- Примеры канона — **London** и **New York** сессии, входы привязаны к их
  динамике/открытию. Высокая ликвидность сессий нужна, чтобы absorption и big
  trades были читаемы. Вне активных сессий поток разрежен → методика не
  применяется.

> Цитата канона: *«one in London session and one in New York session.»*

### 6.2 Масштаб профиля и входа
Канон задаёт масштаб НЕ числом баров, а структурно:

- **Контекст / профиль — на уровне СЕССИИ.** Анализ начинается с формы профиля
  сессии (London/NY). Это «рамка» аукциона.
- **Fixed profile (фиксированный профиль).** Объёмный профиль **прибит** к
  диапазону — сессии / swing-точке / **dealing range**, а НЕ скользящее окно.
  Зона строится от **профиля предыдущей swing-точки**.
- **Вход — на уровне СВЕЧИ.** Подтверждение читается по потоку **внутри свечи**
  («deep trades в теле свечи»), решение — «после этой свечи».
- **Фрактальность.** Один и тот же паттерн (зона → поглощение → продолжение)
  повторяется на разных масштабах — метод **scale-agnostic**, после взятия цели
  ищем следующую зону в новом dealing range.

> Цитаты канона: *«start with the profile shape… London session»; «fixed profile
> volume analysis»; «area based on the profile of the previous swing point… this
> is dynamic»; «aggressive orders, deep trades in the body of the candle… after
> this candle the situation is super clear»; «in the fractal analysis we have
> another strong area»; «the market create again a new dealing range».*

### 6.3 Конкретика из ролика (скриншот графика)
По кадру платформы в ролике видны фактические настройки. **Важно:** тайм-фрейм
и инструмент ниже — это **визуальный вывод из кадра** экрана автора, в речи
ролика они **не произносятся**; на случай переинтерпретации кадра orientируемся
именно на скриншот.

- **График / тайм-фрейм входа = `5 Minuti` (M5, 5 минут).**
- **Инструмент канона = `NQ` (фьючерс Nasdaq-100)** — глубоко-ликвидный рынок
  (подтверждает требование ликвидности §6.1).
- **Профили — order-flow (footprint), а не из баров.** В панели включены
  `Order Flow - Vol. Profile`, `Order Flow - Bid/Ask`, `Dly Vol./Delta Profile`,
  `Wkly Vol./Delta Profile`, `Comp. Vol. Profile` — то есть профили объёма/дельты
  строятся из **исполненного потока (tick/footprint)** и привязываются к
  **дню / неделе / композиту / сессии**, отображаясь на 5m-графике.

**Вывод:** канон-вход — **M5**; профиль объёма/дельты — **tick/order-flow**
(footprint), привязанный к сессии/дню/неделе. Это снимает неопределённость по
числовому ТФ и по источнику профиля.

---

## 7. Пошаговый чеклист входа (детерминированный)

1. **Контекст:** есть ли трендовый сценарий? (чистый пробой + акцепт за VAH/VAL).
   Нет → не торгуем.
2. **Не первое движение:** ждём второе, ясное движение в направлении тренда.
3. **Зона:** построить объёмный профиль предыдущей swing-точки → найти VAH/VAL /
   POC / volume ledge + delta-уровень + big trades. Конфлюэнс ≥2 факторов = зона.
4. **Алерт** на зону, ждём подхода цены.
5. **Подтверждение потоком в зоне:** агрессия в сторону сделки + поглощение
   контр-стороны (deep trades в теле свечи, «failed» контр-сторона).
6. **Вход:** лимитка в зоне.
7. **Стоп:** за зоной (1-2-3/4/5 по консервативности).
8. **Цель:** ближайший swing-point; фиксация на цели.
9. **Reload:** следующая сильная зона по тренду → повтор с шага 3.

---

## 8. Чего стратегия НЕ делает (анти-канон)

- **Не входит против направления аукциона** (никакого контртренда).
- **Не входит по первому движению** / без подтверждённого контекста.
- **Не входит «по уровню» без подтверждения потоком** — зона без absorption/делта
  подтверждения сигналом не является.
- **Не опирается на классические индикаторы / свечные паттерны** как на источник
  правды — только исполненный объём и поток.
- **Не торгует в балансе** (внутри value area, без акцепта за границами).

---

## 9. Сверка с первоисточником (verification)

| Пункт документа | Подтверждение в ролике | ✓ |
|---|---|---|
| Только объём двигает цену; auction theory | «the only thing that move price is volume… auction market theory» | ✓ |
| Контекст: пробой + acceptance за value area = тренд | «clear breakout… accepted below the value area low… we can expect direction» | ✓ |
| Не брать первое движение, ждать второе | «I didn't took the first movement… the second movement was so clear» | ✓ |
| Зона = профиль swing-точки (VAH/VAL, volume ledge) | «area based on the profile of the previous swing point… volume ledge» | ✓ |
| POC, ≈70% VA-ширина — research (Steidlmayer/Dalton), не в ролике | не произносится; research-канон Market Profile | ⚠ атрибуция |
| Delta print = индикатор deep charts, исполненный поток на уровне | «one indicator… called delta print… actual orderflow data that got executed previously» | ✓ |
| Big trades маркируют уровень | «volume got support by these big trades» | ✓ |
| Confluence (VAH + big trades + delta) = сильная зона | «confluence of value area high, big trades and delta level… super strong area» | ✓ |
| Триггер = absorption контр-стороны (deep trades в теле) | «absorption of the aggressive buyers… deep trades in the body of the candle» | ✓ |
| Подтверждение в реальном времени | «this information in real time… as a confirmation to execute» | ✓ |
| Вход лимиткой после подтверждения | «you can already put a limit order here» | ✓ |
| Стоп за зоной, масштаб 1-2-3/4/5 | «protecting yourself above the area… 1-2-3, 1-2-4, 1-2-5» | ✓ |
| Цель swing-point, re-entry на след. super strong swing point | «targeting for a swing point… re-entry… super strong swing point» | ✓ |
| Частичная фиксация — не в ролике (решение реализации, §10) | не упоминается; только «take profit on the first one» | ⚠ не канон |
| Сессии London/NY | «one in London session and one in New York session» | ✓ |
| Масштаб: сессионный fixed-профиль + свечной вход + фрактальность | «start with the profile shape»; «fixed profile volume analysis»; «deep trades in the body of the candle»; «fractal analysis»; «new dealing range» | ✓ |
| ТФ входа = M5; инструмент NQ — визуальный вывод из кадра | скриншот графика: `5 Minuti`, `NQ-202512` (в речи не произносятся) | ✓ (кадр) |
| Профили — order-flow/footprint (tick), привязка день/неделя/композит | скриншот: `Order Flow - Vol. Profile`, `Dly/Wkly/Comp. Vol./Delta Profile` | ✓ |
| Только по тренду: «reversal area following the direction of the trend» | «sensitive high probability reversal area following the direction of the trend» | ✓ |

Все пункты документа имеют прямое подтверждение в ролике. Пункты, помеченные
`⚠`, — это research-канон Market Profile (Steidlmayer/Dalton) или решение
реализации, явно отделённые от первоисточника; расхождений по торговой логике
канона при сверке не выявлено.

---

## 10. Открытые вопросы к реализации (решаются в ТЗ, не в стратегии)

Эти вопросы НЕ относятся к канону стратегии, но нужны для автономной реализации
(см. `TASKSPEC_FLOWZONE.md`):

- На каком инструменте(ах) торговать (ликвидность критична для VP/absorption;
  канон демонстрировался на NQ — глубокий рынок, §6.3).
- Формализация «big trades», «volume ledge», «acceptance» в числовые пороги
  (каждый порог — со ссылкой на канон Market Profile: Steidlmayer / Dalton).

**Снято скриншотом (§6.3) — больше не открыто:**
- ТФ входа = **M5**; профиль = **tick/order-flow footprint**, привязка
  день/неделя/композит/сессия. Реализация должна строить VP из исполненного
  потока (а не из kline-volume), вход — на 5m.

**Решено (2026-06-22, приведение реализации строго к канону):**
- **Инструменты** — глубочайшие перпы Bybit (**BTC/ETH/SOL**) как аналог
  NQ-глубины (§6.1/§6.3): footprint/absorption читаемы только на ликвидности.
  Авто-ротация range/RVOL-альтов отключена (тянула тонкие памп-альты, где
  order-flow шумит). Опционально включается как форвард-эксперимент.
- **Контекст (§2)** = направленный **acceptance вне value area** по ФОРМЕ
  профиля (Steidlmayer/Dalton elongated vs balanced): из объёма в хвостах
  профиля доля ≥ **0.70** (каноничная VA-константа) на одной стороне → тренд
  (`context.classify` — МГНОВЕННЫЙ режим).
- **Зона** = **«super strong»**, конфлюэнс **≥3** факторов (§3.4 называет три:
  value area high + big trades + delta level).
- **Absorption (§4)** читается на окне = **тело M5-свечи (300с)** («deep trades
  in the body of the candle» + ТФ входа M5).

**Решено (2026-06-25, sticky-направление аукциона — фикс ложных переворотов):**
- Мгновенный `classify` по ФОРМЕ ДНЕВНОГО профиля **флапает**: при внутридневном
  откате value area мигрирует, встречный хвост перевешивает → ложный
  `trend_up`/`trend_down` → continuation в контртренд. Forward-стат (22–25.06,
  n=84): лонги-перевороты −$147 (WR 16%), шорты по тренду +$62 (WR 30%).
- Канон (00:33–06:00) направление **держит** (перезаряжает шорт по новым dealing
  range, не переворачивается) и меняет лишь по **«clear breakout of the previous
  level»**. Реализовано в `auction.AuctionTracker`: направление ЛАТЧИТСЯ на символ
  (якорь UTC-день) и адоптируется/переворачивается ТОЛЬКО когда ОБА: (1)
  мгновенный `classify` = тренд в эту сторону (acceptance вне VA) И (2) цена
  пробила последний подтверждённый **swing-экстремум**. Каноничный «previous
  level» в ролике не детализируется методом; в реализации он определён как
  Williams-фрактал (§5.3) — это наша конкретизация, не тождество с текстом
  ролика. Откат/баланс/неподтверждённый встречный хвост направление НЕ
  сбрасывают — это и есть «не первое, а второе ясное движение» (§2). Без новых
  числовых порогов (используются существующие swing left/right и accept_frac).

---

## 11. Аудит кода vs первоисточник (2026-06-25)

Сверка реализации `src/flowzone_bot/` с роликом-первоисточником. Цель — найти
где мы **доработали / подогнали / сломали** логику и математику торговли бота
относительно канона. Разметка: **[КАНОН]** = есть в ролике; **[RESEARCH]** =
каноничная литература Market Profile (Steidlmayer/Dalton и т.п.), в ролике не
звучит; **[НАШЕ]** = наше решение/конкретизация, в ролике нет.

### 11.1 Точная стратегия канона (терминология автора + мировые термины)

- **Auction Market Theory**: цену двигает только исполненный объём; рынок —
  аукцион, ищущий balance; направленное движение = нарушение balance. Источник
  правды — **order flow** (delta, big trades, absorption), не свечи/классические
  S/R. [КАНОН]
- **Шаг 1 — Profile shape.** Трендовый сценарий = **clear breakout of the
  previous level** + **aggression** + **acceptance** вне value area (ниже
  **Value Area Low** для шорта / выше **Value Area High** для лонга). Balanced =
  цена внутри value area, акцепта за границами нет → входы НЕ берём. Термины
  Value Area / VAH / VAL / acceptance / breakout — [RESEARCH] Market Profile.
- **Шаг 2 — Не первое движение**: *«I didn't took the first movement… the second
  movement was so clear»*. [КАНОН]
- **Шаг 3 — Зона = профиль ПРЕДЫДУЩЕЙ swing-точки** (**fixed profile**, не
  скользящее окно). Факторы: value area high/low [КАНОН/RESEARCH], **delta
  print** (индикатор платформы deep charts — исполненный поток на уровне)
  [КАНОН], **big trades** (крупные принты, поддержавшие объём) [КАНОН],
  **volume ledge** (HVN→LVN обрыв) [КАНОН, термины HVN/LVN — RESEARCH].
  **Confluence** факторов = «super strong area» [КАНОН]. На зону — alert.
- **Шаг 4 — Триггер в зоне** (real-time): агрессоры в сторону сделки + **absorption**
  контр-стороны (**deep trades in the body of the candle**, «control of buyers →
  failed buyers»). Только после — **limit order** в зоне. [КАНОН]
- **Шаг 5 — Стоп сразу за зоной** (above the area для шорта / below для лонга),
  масштаб **1-2-3 / 1-2-4 / 1-2-5** = кратные единицы (selectable
  консервативность, *«how much you want to be safe»*). **Цель — ближайший swing
  point**. **Re-entry** на следующей «super strong swing point» / новом **dealing
  range** (фрактальность). **Только continuation** по направлению тренда
  («sensitive high probability reversal area following the direction of the
  trend»). [КАНОН]
- **Шаг 6 — Сессии London/NY**, ТФ входа **M5**, инструмент **NQ** (глубокая
  ликвидность), профиль — **order-flow/footprint** (tick), привязка
  день/неделя/композит/сессия. [КАНОН — сессии/M5/NQ/footprint; привязка
  день/неделя/композит — со скриншота платформы, не из речи]

В ролике **НЕТ**: частичной фиксации, POC, ≈70%, Williams-фрактал, confluence ≥3,
структурной цели из POC/VAL. Любая реализация этих пунктов — [НАШЕ] и должна
быть размечена как таковая.

### 11.2 Расхождения (торговая логика и математика)

| ID | Модуль | Что делает код | Канон | Тип |
|---|---|---|---|---|
| **A1** | `strategy.py:99-104`, `settings.py:163-166` | Стоп = граница зоны + фиксированный буфер `sl_buffer_bps=8` (+пол `min_sl_bps=10`) | Стоп = **зона × N (1-2-3/4/5)**, кратные единицы | **сломана математика** |
| **A2** | `aggregates.py:127-130`, `main.py:267`, `context.py` | Профиль — один кумулятивный **ДНЕВНОЙ (UTC-день)** профиль; используется и для контекста, и для зоны | Зона = профиль **предыдущей swing-точки** (fixed profile по swing/dealing range); контекст = форма **сессионного** профиля | **подгонка инфры** |
| **A6** | `executor.py:477-507`, `strategy.py:40,109`, `settings.py:185` | Частичная фиксация 50% на цели 1, остаток на цели 2 со стопом в БУ | Полный выход на swing point, затем **re-entry** на новой зоне | **доработка логики** |
| A5 | `strategy.py:43-55,108` | Фолбэк-цель из POC/противоположной VA-границы при отсутствии swing | Цель = **только swing point** | доработка |
| B1 | `settings.py:153`, `zone.py` | `min_confluence=3` жёстко | Канон называет 3 фактора как **пример** «super strong area», не инвариант; §7 чеклист — «≥2» | подгонка порога |
| A3 | `context.py:89-105` | `classify` = тренд по хвостам вне VA ≥0.70, **без проверки breakout предыдущего уровня** | Тренд = breakout previous level + acceptance | доработка (частично закрыто в `auction.py`) |
| A4 | `context.py:71`, `settings.py:124` | `accept_frac=0.70` для acceptance | Acceptance описан качественно | [НАШЕ] порог |
| B2 | `orderflow.py:38-44`, `settings.py:132` | `big_trade_pct=0.90` (90-й перцентиль) | «big trades» качественно | [НАШЕ] порог |
| B3 | `orderflow.py:80,114`, `settings.py:145` | `absorption_min_counter_frac=0.5` | absorption = «failed buyers» качественно | [НАШЕ] порог |
| B5 | `settings.py:160,156,115` | `zone_delta_min_frac=0.6`, `ledge_drop_frac=0.5`, `cluster_ticks=5`, `vp_bucket_ticks=10` | Не в ролике | [НАШЕ] техпороги |

### 11.3 Главное (требует правки для «как в первоисточнике»)

- **A1 — сломана математика стопа.** Канон масштабирует стоп от ширины зоны
  (1×/2×/3×/4×/5× зоны → selectable R), код — плоский 8 б.п. + пол 10 б.п. На
  узкой зоне канон даёт tighter стоп (лучший R), код раздувает до `min_sl_bps`;
  на широкой — код ставит ближе «1-2-5». Влияние: R:R и частота стопов.
- **A2 — подгонка инфры.** Канон строит зону от **профиля предыдущей
  swing-точки** и контекст от **сессионного** профиля; код использует один
  дневной профиль для обоих. Это нарушает «fixed profile by swing» — суть
  методики. Влияние: зоны и контекст считаются не от того объекта.
- **A6 — доработка логики.** Канон: полный выход на swing point + re-entry на
  новой зоне. Код: удержание 50% позиции с стопом в БУ. Это **другой trade
  management**, не «take profit on the first one» + «condition again».

### 11.4 Что соответствует канону

- `orderflow.detect_absorption` (контр-агрессия + deep trade + цена не идёт в
  сторону контр-стороны = «failed») — [КАНОН].
- `volume_profile.find_ledges` (HVN→LVN) — [КАНОН] (термины — [RESEARCH]).
- `auction.AuctionTracker` (sticky-направление, переворот по breakout+
  acceptance, «не первое движение») — [КАНОН]-поведение.
- `session` London/NY gate — [КАНОН].
- `executor` LIMIT в зоне, стоп за зоной, риск per trade (Tharp) — [КАНОН]
  (кроме A1, A6).
- Профиль из исполненного потока (tick), M5 swings — [КАНОН] (кроме A2 —
  привязка дня вместо swing/сессии).
- `absorption_window_sec=300` = тело M5-свечи — [КАНОН].

### 11.5 План приведения к канону (решается отдельно, не в этом коммите)

A1, A2, A6 — изменения торговой логики/инфры, по `strategy-guard.mdc` требуют
согласования и обоснования. A2 — инфраструктурное (per-swing重建 профиля),
нетривиальное. A5, B1, A3 — вторичные. Числовые пороги A4/B2/B3/B5 — [НАШЕ]
конкретизации, требуют обоснования данными, не «чтобы было».

### 11.6 Приведено в исполнение (2026-06-25)

В этом коммите исполнены A1, A5, A6, A2, A3 (согласовано с пользователем):

- **A1 — стоп = зона × N (`sl_zone_mult`).** Стоп = far edge зоны + N × ширина
  зоны (канон «1-2-3/1-2-4/1-2-5»), N configurable (`sl_zone_mult=1.0` по
  умолчанию). Убраны `sl_buffer_bps` (как торговый множитель) и `min_sl_bps`.
  `sl_buffer_bps` сохранён как технический анти-фильтр-буфер. Файлы:
  `strategy.py`, `settings.py`.
- **A5 — цель только swing point.** Удалён `_structural_target` (фолбэк на
  POC/VAL/VAH). Без swing-цели сделка НЕ берётся (канон §5.3). Убран `tp2_level`.
  Файлы: `strategy.py`.
- **A6 — полный выход, без частичной фиксации.** Удалена `partial_exchange_tp`,
  `_maybe_partial`, поле `_partial`. Выход = полный на `tp_level` (swing point);
  re-entry — отдельной сделкой на следующей зоне (§8). Файлы: `executor.py`,
  `strategy.py`, `settings.py` (убран `partial_fraction`).
- **A2 — per-SESSION контекст + per-SWING зона.** Дневной `vp_buckets` удалён.
  Контекст (`classify`) — по форме **per-session** профиля (якорь = старт
  London/NY окна, `session.session_start_ts`). Зона — профиль **предыдущей
  swing-точки**: исполненный поток (footprint) в окне `[ts prev swing, now]`,
  собранный из persist-таблицы `prints` (SQLite). Принты persist-ятся через
  background `PrintStore` (batched flush из daemon-потока, чтобы не блокировать
  WS-callback; retention 6ч). `Swing.ts` добавлен для якоря окна. Файлы:
  `state/db.py` (таблица `prints`), `data/print_store.py` (новый),
  `data/aggregates.py`, `analysis/session.py`, `analysis/volume_profile.py`
  (`build_profile_from_prints`), `analysis/swings.py`, `app/main.py`,
  `config/settings.py`.
- **A3 — breakout-гейт зафиксирован в docstring.** `classify` остаётся чистой
  функцией формы профиля; breakout-гейт «clear breakout of the previous level»
  (канон §2) выполняется в `auction.AuctionTracker.update` (swings-пробой).
  Торговый путь всегда `auction.update(classify(...))` → вход требует
  breakout+acceptance. Docstring `context.py` явно это фиксирует.
- **R:R-фильтр ≥ 1:2 (канон Fabervaale, шаг 5.1).** После расчёта стопа/цели
  бот отбрасывает сделку, если `reward/risk < min_rr` (reward = |tp−last|,
  risk = |sl−last|). Канон: ролик cUTsoU-15Tc «The Simplest Orderflow Trading
  Model» — «our real risk-to-reward… maybe it's 1 to 2, 1 to 2.5»;
  chartfanatics AMT-strategy (Fabio) — «Reward-to-Risk 1:2.5 to 1:5». Источник:
  research, не data-fitting. Кейс-мотивация live #468: tp_hit с убытком (swing
  0.47 от entry, стоп 6.35 → rr 0.07; gross 0.24 < fees 1.83 → net −1.59).
  **2026-06-29: `min_rr` 2.5 → 2.0** (канон-флор «1 to 2» первоисточника
  Fabervaale). Причина: на крипто BTC/ETH/SOL (тоньше NQ, 24/7 без cash-session)
  zone-stop широкий → R:R≥2.5 почти недостижимо, бот встал (0 входов с 06-28).
  Возврат к канон-флору 1:2 возобновляет входы, не нарушая канон. Файлы:
  `strategy.py`, `settings.py` (`min_rr=2.0`).
- **BE-lock (канон Trade Management, видео 39:00).** 2026-06-29: добавлен
  вынос SL в break-even после пробоя зоны поглощения (`executor._maybe_be_lock`,
  §5.5). Прямо бьёт по 72% SL-hit (n=116, WR 28%, −$239 за 7д): лузеры, что
  вернулись → scratch на BE. Источник: <https://youtu.be/Pz8f0wWW12M> (39:00).
  Файлы: `executor.py`, `db.py` (`zone_low`/`zone_high`), `settings.py`
  (`be_lock_enabled`, `be_lock_zone_mult`), `strategy.py` (research-блок).

Числовые пороги A4/B2/B3/B5/B1 — оставлены как [НАШЕ] (B1 `min_confluence=3` —
согласовано с пользователем); изменение требует обоснования данными
(`no-data-fitting.mdc`, `sample-size.mdc`), не выполнено в этом коммите.


