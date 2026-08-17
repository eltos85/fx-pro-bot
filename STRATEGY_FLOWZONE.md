# STRATEGY — flowzone_bot

**Канон (единственный источник правды) — три ролика одного автора (Fabervaale),
методика согласована:** «How To Find The BEST Entry Zones» (зоны/триггеры) —
<https://youtu.be/06R-ebyOhDI>; «The Only Orderflow Guide You'll Ever Need»
(Trade Management BE/trail 39:00, Value Area 68% 28:50) —
<https://youtu.be/Pz8f0wWW12M>; «The Simplest Orderflow Trading Model»
(R:R 1:2) — <https://youtu.be/cUTsoU-15Tc>; «My Signature Orderflow Model»
(фиксация на exhaustion) — sozai.app/transcript/signature-orderflow-model.
Доп.: winkler.expert Fabervaale rulebook (68% VA); tradezella AMT-playbook
(Fabio, BE по CVD-pressure); forex.in.rs World-Cup strategy (trail to last
absorption, never re-widen).

**Тайм-коды и цитаты в этом документе — из полных транскриптов** (sozai.app,
канал Fabervaale ENG), полученных 2026-07-29. Прямой доступ к YouTube для
`yt-dlp`/fetch закрыт (`LOGIN_REQUIRED`), поэтому до этой даты сверка шла по
выдержкам — что и дало ошибки атрибуции, исправленные в §11.8. Тайм-коды
раздела §11.8 и далее соответствуют транскрипту «The Only Orderflow Guide»
(41:04) и могут отличаться на десятки секунд от прежних ссылок вида «28:50»,
проставленных по выдержкам.

Этот документ описывает стратегию **полностью и автономно**, строго по
первоисточникам (роликам автора). Он НЕ сравнивает методику с другими ботами
проекта и НЕ содержит наших готовых решений — только канон. Любая будущая
правка логики flowzone_bot сверяется с этим документом и с роликами, а не с
интуицией.

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

### 2.1 Форма профиля (profile shape) — канон-нюансы

Канон (Dalton/Steidlmayer «Mind Over Markets»; «The Only Orderflow Guide»)
различает паттерны формы профиля, помимо бинарного тренд/баланс:

- **P-shape** — профиль с тяжёлым хвостом в одну сторону (агрессивные
  участники принимали направление) → направленное продолжение на следующий
  период. *«P-shape profile… aggressive buyers… directional next day»*.
- **Double distribution** — два объёмных кластера, разделённых low-volume node
  (LVN-перешейком) → два dealing range, быстрый проход по LVN.
- **Balance / bell** — симметричный колокол вокруг POC → баланс, не торгуем.
- **Shift** — POC сместился относительно предыдущего профиля → миграция value.

**C3 (2026-07-29): форма ГЕЙТИТ направление — [КАНОН].** Одного acceptance вне
value area недостаточно. Разбирая профиль, который развернулся вверх, автор
говорит:

> *«So from a down profile, you go in an up profile. Is not a P shape. So it's
> still balance. You can use this as indecision. You can put blue instead of
> green.»* (34:32)

То есть отсутствие P-формы **отменяет** направленность, даже когда value
мигрировала. И наоборот, P-shape сам по себе даёт ожидание: *«This is what we
call the Pshapes where the buyers are really aggressive and you close the cash
session on the upside. So you can expect a directional move also on the day
after»* (26:49).

Правило в коде (`context.classify`, параметр `shape_gate`): тренд остаётся
только если форма подтверждает — P-shape в ту же сторону (тяжёлый хвост **плюс
направленная дельта в самом хвосте**: направление приняли агрессоры) либо
double distribution (канон 31:31 — направленный день с тонким LVN). Тяжёлый
хвост без подтверждающей дельты классифицируется как `normal` → баланс, входов
не берём.

Флаг `FLOWZONE_PROFILE_SHAPE_ENABLED` (по умолчанию **включено**); `false`
возвращает прежнее поведение «только acceptance вне VA» для A/B.

> **История атрибуции.** До 2026-07-29 `shape` был лишь обогащением
> (`ctx.shape` логировался, вход гейтил бинарный тренд/баланс). Это и было
> расхождение: канон использует форму как часть определения bias.

---

## 3. Шаг 2 — Определение торговой зоны (где может быть вход)

Зона — это **место максимального давления в прошлом**, куда цена вернётся для
перезарядки. Строится по объёмному профилю предыдущей swing-точки. Компоненты:

### 3.1 Объёмный профиль (Volume Profile)
- **Value Area High / Low (VAH / VAL)** — границы value area, зоны, где
  сосредоточено основное принятие объёма. Канон-автор называет ширину value area
  **≈68%** общего объёма: видео «The Only Orderflow Guide» (28:50) — *«value
  area… where the 68% of the volume of the distribution took place»*; то же в
  winkler-rulebook. 68% = одно стандартное отклонение (Gaussian). Термины
  POC/HVN/LVN — из Market Profile (Steidlmayer / Dalton), research-источник.
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

### 3.5 Composite / double-day profile (merge) — [КАНОН]

Автор сливает перекрывающиеся профили сессий/дней в **composite**, и это не
приём «на всякий случай», а способ уточнить границу value area. В ролике он
делает это четырежды подряд, разбирая двухмесячную историю NQ:

> *«So these two profile can be merged. You can merge them and you can see one
> piece of information because when they are overlapping on the same level they
> are just telling you that they stayed in this balance area for two days.»*
> (31:14)
>
> *«you can do from here merge and do a double day profile on a single level and
> you can have a really precise value area low point.»* (31:59)
>
> *«these two profile that you can see here are on the horizontal level so you
> can also merge both of them»* (32:33); *«you have three profile on an
> horizontal level also we can merge them»* (32:50).

Критерий слияния — **перекрытие по value area** («overlapping on the same
level»), а не соседство по времени: хвосты-отвержения уходят далеко за область
принятия и пересекались бы почти всегда.

Реализация: `volume_profile.merge_profiles` + `value_areas_overlap`, подключение
в live-путь — `_merged_session_profile` в `app/main.py`
(`FLOWZONE_PROFILE_MERGE_ENABLED`, по умолчанию **включено**, глубина
`FLOWZONE_PROFILE_MERGE_LOOKBACK=3`). Профили прошлых сессий не восстановимы из
`prints` (retention 6ч), поэтому сессионный профиль периодически сохраняется в
таблицу `session_profiles`.

> **История атрибуции.** До 2026-07-29 этот раздел помечал merge как «[НАШЕ]
> инфра-утилита», а флаг стоял в `false`. Это была ошибка разметки: в ролике
> merge звучит как базовая практика профильного фрейминга. Исправлено в рамках
> аудита C1-C5.

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

### 4.1 Initiative auction / exhaustion — [КАНОН]

Подводя итог исполнению, автор перечисляет **три** равноправных паттерна, а не
один: *«we saw the absorption, we saw the exhaustion, we saw the initiative
auction»* (37:03).

- **Initiative auction** — высокая агрессия И результат: *«constant aggression
  of the buyer… one side print that is also called imbalance… a strong delta and
  a candle that close on the upside… the delta that is leading the price»*
  (15:43-16:31), вывод: *«an amazing signal if you are a trend following
  trader»* (17:02). Это **второй триггер входа** рядом с absorption — берётся,
  когда рынок не даёт теста зоны с поглощением («The Simplest Orderflow Trading
  Model»: *«we can use this as a confirmation trigger to go long… you can take a
  momentum trade with a really tight stop-loss»*).
- **Exhaustion** — затухающий объём + contrarian imbalance на экстремуме:
  *«decreasing volume… a contrarian imbalance… the price snap back to the value…
  usually amazing opportunities to capitalize on a reversal trade»* (18:28).
  У нас это **не вход**: бот торгует только continuation, а разворотный сигнал
  канон в такой ситуации использует для **фиксации прибыли** — «My Signature
  Orderflow Model» (06:04): *«you can understand that now this selling pressure
  is almost exhausted… it's not worth to risk all this profit… So I take out my
  position, and what I do, I consider putting stop below. So if the market wants
  to continue, I'm into the position again»*. Повторный вход у нас —
  `reload_cooldown_sec`, отдельной сделкой.

Порядок триггеров входа: absorption приоритетнее (основной reload-сетап §4),
initiative — momentum-вариант. В `Signal.reasons` пишется `trigger=absorption`
или `trigger=initiative`.

Реализация: `orderflow.detect_initiative` / `detect_exhaustion`, вход —
`strategy._evaluate_reload`, фиксация — `executor._maybe_exhaustion_exit`
(стадия 3 сопровождения, закрытие с `close_reason=exhaustion_exit`).
Флаг `FLOWZONE_INITIATIVE_EXHAUSTION_ENABLED`, по умолчанию **включено**.

> **История атрибуции.** До 2026-07-29 раздел помечал оба паттерна как «[НАШЕ]
> детекторы» с флагом `false`, то есть работал только absorption. Исправлено в
> рамках аудита C1-C5.

### 4.2 Hook / failed auction — [КАНОН]

Отдельный сетап, который автор прямо называет самым надёжным по win rate. Цена
выходит за границу value area, **не принимается** там и возвращается внутрь:

> *«when we go back to the value area low and down, we do what we call the hook.
> So they do a failed auction, they try to break, they get rejected. So all this
> it's a rejection area. And when you go back inside, you have your continuation
> trade»* (26:17)
>
> *«it also hook the value area high from the downside. And this is what we like
> to call a fake out, a failed auction trap traders… usually the price slice
> through the value area to go to seek orders on the value area low. This is one
> really profitable setup with high win rate»* (27:20)

Правила:

- Неудачная вылазка идёт **против** направления аукциона: для лонга — ниже VAL,
  для шорта — выше VAH. Вход — по направлению аукциона (continuation, §5.4).
- «Failed» = за границей **не построили value**. Порог берём из канон-константы
  Value Area: доля объёма за границей должна быть меньше нейтральной вне-VA
  массы `1 − 68% = 32%`. Отдельного подобранного числа не вводим.
- Вход только после возврата цены **внутрь** value area («when you go back
  inside»).
- Стоп — за экстремумом неудачной вылазки: принятие цены снаружи опровергает сам
  тезис failed auction. Цель — как везде, ближайшая swing-точка (§5.3) с
  R:R-фильтром (§5.1).

Реализация: `analysis/hook.py` (`detect_hook`) + `strategy._evaluate_hook`.
Ищется по persisted-принтам (`prints`), а не по 5-минутному окну снапшота: hook
разворачивается дольше одной M5-свечи. Флаг `FLOWZONE_HOOK_ENABLED`, окно
поиска `FLOWZONE_HOOK_LOOKBACK_SEC=3600` (операционный лимит свежести и объёма
чтения из SQLite, не торговый порог).

> До 2026-07-29 сетапа в боте не было вообще.

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

### 5.5 Trade Management — BE-lock + trail (канон видео 39:00)

Канон (полный транскрипт 39:00 «The Only Orderflow Guide You'll Ever Need»):
после входа в зоне поглощения управление позицией — ДВЕ стадии.

**Стадия 1 — BE-lock** (`executor._maybe_be_lock`):
- **Триггер**: цена **пробила структурный swing-уровень, подтверждённый ПОСЛЕ
  входа**, в стороне сделки между entry и TP (long: `price > post-entry swing
  high`; short: `price < post-entry swing low`) — канон *«when you **break this
  level**, you can decide to put your stop loss to break even»* + *«this one
  print a new one»*. Пред-entry swing не используется: ближайший из них по
  направлению сделки — это сама TP-цель (тот же набор M5-фракталов, что у
  `nearest_swing_target`), т.е. его пробой = момент исполнения TP и BE
  вырождается в no-op (фикс 2026-07-02). **НЕ** «favourable ≥ N×zone_width»
  (то было [НАШЕ] изобретение `f6ef82a`, срабатывало слишком рано → обрезало
  wins на откате к entry, кейсы #488/#489/#492: wins +0.03/+0.25 вместо +21).
- **CVD-pressure gate** (tradezella playbook Fabio *«If CVD shows strong
  pressure, move the stop to break-even early»*): в `trade_window` доминирует
  сторона сделки (long: `buy_vol > sell_vol`; short: `sell_vol > buy_vol`).
  [НАШЕ] операционализация качественного канон-условия. Выкл через
  `FLOWZONE_BE_LOCK_CVD_GATE=false`.
- **BE-уровень** = entry ± `sl_buffer_bps` (anti-flicker буфер, покрывает
  round-trip fees — чтобы BE не стал микро-убытком).
- **Idempotent**: persisted `tr.sl` — ключ cross-tick idempotency. Если SL уже
  в BE → silent no-op. Выкл через `FLOWZONE_BE_LOCK_ENABLED`.

**Стадия 2 — trail** (`executor._maybe_trail`, только после BE):
- **Канон**: *«after breaking out of complete absorption and you have an
  amazing explosion where you can trail your position following the aggression
  of the market. **This one print a new one, you bring your stop loss here and
  you continue.**»* SL едет за **последним absorption-принтом контр-стороны** в
  стороне сделки: long — deep SELL print ниже цены = поддержка → SL сразу ПОД
  неё; short — deep BUY print выше цены = сопротивление → SL сразу НАД ней.
  SL ставится **ЗА уровнем** (та же конвенция, что стоп «за зоной» §5.2
  «protecting yourself above the area»): буфер внутрь уровня выбивал бы позицию
  на обычном ретесте ещё не сломанной поддержки/сопротивления (фикс 2026-07-02;
  до этого SL ставился между ценой и уровнем).
- **Never re-widen** (forex.in.rs World-Cup strategy *«Trail to the last
  absorption, never re-widen a stop»*): SL двигается ТОЛЬКО в сторону сделки
  (long → выше текущего SL, short → ниже). Idempotency: persisted `tr.sl`.
- Окно детекции absorption-принтов = `trail_window_sec` (тело M5, как
  `absorption_window_sec`). Порог big-trade = тот же `big_trade_pct`.
- Биржевой TP (swing-цель §5.3) сохраняется: либо TP-ордер исполнится на
  swing-цели, либо trail-SL закроет по пути в плюс. Выкл через
  `FLOWZONE_TRAIL_ENABLED`.

**close_reason** (`executor.bracket_exit_reason`): классифицируется по
пересечению `tr.tp`/`tr.sl`, НЕ по знаку (exit−entry) — после BE/trail SL стоит
в стороне прибыли, и закрытие по BE-SL (exit в прибыли) должно метиться
`sl_hit`, а не `tp_hit` (кейс #489: exit=SL, pnl +0.25, ошибочно `tp_hit`).

> Цитата канона (видео, 39:00): *«you can decide to go from 1 to 2 to 1 to 5 and
> put your stop loss to break even… after breaking out of the sellers of
> complete absorption and you have an amazing explosion where you can trail your
> position following the aggression of the market… this one print a new one, you
> bring your stop loss here and you continue.»* —
> <https://youtu.be/Pz8f0wWW12M>. Доп.: tradezella AMT-playbook (Fabio)
> «Break-even: If CVD shows strong pressure, move the stop to break-even early»;
> forex.in.rs «Trail to the last absorption, never re-widen a stop.»

---

## 6. Сессии и тайм-фреймы (масштаб)

### 6.1 Сессии
- Примеры канона — **London** и **New York** сессии, входы привязаны к их
  динамике/открытию. Высокая ликвидность сессий нужна, чтобы absorption и big
  trades были читаемы. Вне активных сессий поток разрежен → методика не
  применяется.

> Цитата канона: *«one in London session and one in New York session.»*

> **C4 (2026-07-29): одна сессия, выбранная по объёму — [КАНОН].** Автор
> формулирует правило явно: *«I only trade in the New York session for US
> indices because it's where the majority of the volume get traded and I find it
> from statistical validation the London session to be usually for US indices
> not so valuable to add to the profile. So I only use the cash session
> profile»* (28:54). То есть сессия **одна**, и выбрана она по доле объёма.
>
> Для крипты (24/7, cash-сессии нет) окно выбрано **измерением**, а не
> аналогией: `scripts/flowzone_session_volume.py`, 1000 часовых баров ≈41 день,
> среднее по BTC/ETH/SOL с нормировкой внутри символа — пик приходится на
> 13:00 (8.9%), 14:00 (8.3%), 15:00 (7.6%) UTC; окно **NY 12:00–21:00 держит
> 51.4%** оборота за 9ч против 46.8% у London 07:00–16:00. Объединение
> 07:00–21:00 даёт 67.2%, но за 14ч — то есть разбавляет профиль пятью часами
> низкой активности ради 15.8 п.п.
>
> Итог на 2026-07-29: `session_windows_utc = "12:00-21:00"`.
>
> **2026-08-17 изоляция ядра (форвард, не отмена канона).** После C1-C5 на
> тройке BTC/ETH/SOL WR 25%→17% и у absorption, и у hook/initiative (разница
> <1 п.п.). Чтобы не смешивать C4 (окно/профиль) с новыми сетапами, дефолт
> окна временно возвращён к London+NY `07:00-16:00,12:00-21:00`, hook и
> initiative выключены. C4-окно — `FLOWZONE_SESSION_WINDOWS_UTC=12:00-21:00`.

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
| POC, ≈68% VA-ширина — канон-автор (видео 28:50) + research (Dalton) | «68% of the volume» (видео Pz8f0wWW12M); research-канон Market Profile | ✓ |
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
  профиля доля ≥ **0.68** (канон-автор, Value Area 68%) на одной стороне →
  тренд (`context.classify` — МГНОВЕННЫЙ режим).
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

В ролике **НЕТ**: частичной фиксации, POC, Williams-фрактал, confluence ≥3,
структурной цели из POC/VAL. **68% VA-ширина** — канон-автор (видео
Pz8f0wWW12M 28:50 + winkler-rulebook). Любая реализация этих пунктов — [НАШЕ] и
должна быть размечена как таковая.

### 11.2 Расхождения (торговая логика и математика)

| ID | Модуль | Что делает код | Канон | Тип |
|---|---|---|---|---|
| **A1** | `strategy.py:99-104`, `settings.py:163-166` | Стоп = граница зоны + фиксированный буфер `sl_buffer_bps=8` (+пол `min_sl_bps=10`) | Стоп = **зона × N (1-2-3/4/5)**, кратные единицы | **сломана математика** |
| **A2** | `aggregates.py:127-130`, `main.py:267`, `context.py` | Профиль — один кумулятивный **ДНЕВНОЙ (UTC-день)** профиль; используется и для контекста, и для зоны | Зона = профиль **предыдущей swing-точки** (fixed profile по swing/dealing range); контекст = форма **сессионного** профиля | **подгонка инфры** |
| **A6** | `executor.py:477-507`, `strategy.py:40,109`, `settings.py:185` | Частичная фиксация 50% на цели 1, остаток на цели 2 со стопом в БУ | Полный выход на swing point, затем **re-entry** на новой зоне | **доработка логики** |
| A5 | `strategy.py:43-55,108` | Фолбэк-цель из POC/противоположной VA-границы при отсутствии swing | Цель = **только swing point** | доработка |
| B1 | `settings.py:153`, `zone.py` | `min_confluence=3` жёстко | Канон называет 3 фактора как **пример** «super strong area», не инвариант; §7 чеклист — «≥2» | подгонка порога |
| A3 | `context.py:89-105` | `classify` = тренд по хвостам вне VA ≥0.70, **без проверки breakout предыдущего уровня** | Тренд = breakout previous level + acceptance | доработка (частично закрыто в `auction.py`) |
| A4 | `context.py:80`, `settings.py:124` | `accept_frac=0.68` для acceptance | Acceptance описан качественно | [НАШЕ] порог (канон-автор 68% VA) |
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
- **2026-06-30: BE-lock + trail возвращены к канону 39:00 (E1/E2/E3).**
  Симптом: после `f6ef82a` (29 Jun) wins стали минимальными (+0.03/+0.25 вместо
  +21 до BE), losses существенные (−10…−18). Диагноз по полному транскрипту 39:00
  + tradezella/forex.in.rs:
  - **E1** — BE-триггер был [НАШЕ] `favourable ≥ be_lock_zone_mult × zone_width`
    (срабатывал слишком рано, до «amazing explosion»). Канон: *«when you break
    THIS LEVEL»* = пробой предыдущего swing-уровня. Триггер переписан на
    swing-пробой (`_last_swing_price`) + CVD-pressure gate (tradezella «If CVD
    shows strong pressure»). `be_lock_zone_mult` удалён, добавлены
    `be_lock_break_structure`, `be_lock_cvd_gate`.
  - **E2** — стадия 2 (trail) не была реализована → winning-сделки закрывались
    на откате к entry. Добавлен `executor._maybe_trail`: после BE SL едет за
    последним absorption-принтом контр-стороны в стороне сделки (канон «this
    print a new one, you bring your stop loss here»), только в сторону сделки
    (forex.in.rs «never re-widen»). `trail_enabled`, `trail_window_sec`.
  - **E3** — `bracket_exit_reason` классифицировал по знаку (exit−entry), после
    BE-SL в стороне прибыли метил закрытие как `tp_hit` (#489: exit=SL, +0.25,
    `tp_hit`). переписан по пересечению `tr.tp`/`tr.sl`.
  Обоснование: канон-несоответствие (bugfix-категория `no-data-fitting.mdc`), не
  P&L-подгонка; 3 сделки после `f6ef82a` — шум (`sample-size.mdc`). Файлы:
  `executor.py`, `config/settings.py`, `app/main.py` (swings → manage),
  `tests/test_flowzone_bot.py`, `STRATEGY_FLOWZONE.md` §5.5.

Числовые пороги A4/B2/B3/B5/B1 — оставлены как [НАШЕ] (B1 `min_confluence=3` —
согласовано с пользователем); изменение требует обоснования данными
(`no-data-fitting.mdc`, `sample-size.mdc`), не выполнено в этом коммите.

### 11.7 Приведено в исполнение (2026-06-30, аудит D1-D8)

Полный аудит бота против трёх канон-видео + winkler-rulebook выявил расхождения
D1-D8 (согласовано с пользователем — «все переделки»). Исполнено:

- **D1 — Value Area 68% (канон-автор), не 70%.** Канон-автор буквально называет
  68% (видео Pz8f0wWW12M 28:50 *«68% of the volume»*; winkler-rulebook). Было
  0.70 (Steidlmayer/Dalton literature). Изменено `value_area_pct` и
  `context_accept_frac` 0.70 → 0.68 в `settings.py`, дефолты в
  `volume_profile.py`/`context.py`, тесты. Влияние: VA уже на 2% → больше
  хвостов вне VA → `classify` чаще детектит тренд. Канон-фикс (не P&L-подгонка).
- **D2 — London+NY задокументированы как [НАШЕ] крипто-адаптация (§6.1).** Канон
  держит одно NY cash-окно; на крипто 24/7 cash-сессии нет → London+NY.
  Кода не меняли (адаптация оправдана), атрибуция в доке.
- **D3 — `merge_profiles` (composite/double-day) утилита (§3.5).** Канон:
  *«merge them… double day profile»*. Реализована утилита слияния профилей в
  `volume_profile.py` + тесты. В live-путь **не подключена** по умолчанию
  (`profile_merge_enabled=false`) — включение как торгового критерия требует
  OOS-валидации (`no-data-fitting.mdc`).
- **D4 — `classify_shape` (P-shape / double-distribution / balance / shift)
  (§2.1).** Канон различает паттерны формы. Реализовано как обогащение
  (`ctx.shape`), **не гейтит** вход (бинарный тренд/баланс `classify` гейтит
  как прежде). Тесты + док.
- **D7 — `detect_initiative` / `detect_exhaustion` (§4.1).** Канон описывает
  initiative (continuation) и exhaustion (reversal) паттерны. Реализованы
  детекторы в `orderflow.py` + тесты. В live-вход **не гейтят** по умолчанию
  (`initiative_exhaustion_enabled=false`) — основной канон-сетап absorption
  (§4); новые триггеры требуют OOS-валидации.
- **D8 — атрибуция трёх канон-видео (§0/header).** Doc указывал один канон
  (06R-ebyOhDI), но §5.5 — из Pz8f0wWW12M, min_rr — из cUTsoU-15Tc. Header
  обновлён: три ролика одного автора + winkler/tradezella/forex.in.rs доп.

Обоснование D3/D4/D7 как non-gating: `strategy-guard.mdc`/`no-data-fitting.mdc`
запрещают менять торговую логику без OOS-валидации; детекторы/утилиты готовы к
форвард-эксперименту, live-гейтинг — отдельной правкой по данным. Файлы:
`config/settings.py`, `analysis/context.py`, `analysis/volume_profile.py`,
`analysis/orderflow.py`, `tests/test_flowzone_bot.py`, `STRATEGY_FLOWZONE.md`.

### 11.8 Строгий канон по дословным транскриптам (2026-07-29, C1-C5)

Аудит D1-D8 (§11.7) опирался на выдержки. 2026-07-29 удалось получить **полные
транскрипты** роликов Fabervaale ENG (sozai.app; сам YouTube отдаёт
`LOGIN_REQUIRED` для yt-dlp и прямого fetch). Сверка по дословному тексту
показала, что часть решений §11.7 была основана на неверной атрибуции: три
механики, помеченные как «[НАШЕ]» и **выключенные**, в ролике звучат как
базовые практики автора, а один сетап отсутствовал целиком.

Пользователь подтвердил приведение к строгому канону со всеми пунктами.

- **C1 — merge профилей включён в live-путь (§3.5).** Канон 31:14-32:50, четыре
  повтора, критерий «overlapping on the same level». Добавлены
  `value_areas_overlap`, `_merged_session_profile`, таблица `session_profiles`
  (профили прошлых сессий не восстановимы из `prints` — retention 6ч).
  `profile_merge_enabled` false → **true**.
- **C2 — initiative и exhaustion включены (§4.1).** Канон 37:03 перечисляет три
  паттерна исполнения. Initiative стал вторым триггером входа рядом с
  absorption; exhaustion — стадией 3 сопровождения (фиксация прибыли, канон
  «Signature Model» 06:04), не входом: бот торгует только continuation.
  `initiative_exhaustion_enabled` false → **true**.
- **C3 — форма профиля гейтит направление (§2.1).** Канон 34:32: *«Is not a P
  shape. So it's still balance… indecision»*. `classify` получил `shape_gate`;
  тяжёлый хвост без направленной дельты в самом хвосте больше не даёт тренд.
- **C4 — одна сессия вместо склейки London+NY (§6.1).** Канон 28:54: одна
  сессия, выбранная по доле объёма. Окно для крипты измерено
  (`scripts/flowzone_session_volume.py`): NY 12:00-21:00 = 51.4% оборота за 9ч
  против 46.8% у London за те же 9ч; склейка 07:00-21:00 = 67.2%, но за 14ч.
  `session_windows_utc` `"07:00-16:00,12:00-21:00"` → **`"12:00-21:00"`**.
- **C5 — добавлен сетап hook / failed auction (§4.2).** Канон 26:17 и 27:20,
  *«one really profitable setup with high win rate»*; в боте отсутствовал.
  Новый модуль `analysis/hook.py`, ветка `strategy._evaluate_hook`, поиск по
  persisted-принтам. Порог «не приняли» выведен из VA 68%, новых
  magic-number-ов нет.

Телеметрия переведена в непрерывные скаляры (`init_prev` с долей дельты и
флагом подтверждения, `vratio` = плотность ленты / EMA): бинарные защёлки дали
2/16 покрытия за неделю, на таком темпе порог в 100 сделок (`sample-size.mdc`)
недостижим.

Тестовые фикстуры `_down_elongated_buckets` / `_short_reload_profile` держали
нисходящий день с **покупательской** дельтой в нижнем хвосте (весь профиль был
buy-only ради краткости). Под гейтом C3 канон справедливо считает такой день
indecision, поэтому хвосты приведены к sell-доминанте — это исправление
фикстуры под описанный ею же сценарий, а не подгонка под тест.

Файлы: `config/settings.py`, `analysis/{context,volume_profile,orderflow,hook,
strategy,session,telemetry}.py`, `app/main.py`, `trading/executor.py`,
`state/db.py`, `scripts/flowzone_session_volume.py`, `tests/test_flowzone_bot.py`.

### 11.9 Изоляция ядра + починка C3 (2026-08-17)

После двух недель C1-C5: 134 сделки, WR 17.2%, −$749. Hook 46 / initiative 32 /
absorption 56 — WR 17.4 / 15.6 / 17.9. C3 не резал: все 126 сделок после фикса
shape были `double_distribution` (любой локальный пик соседних корзин = HVN).

Согласовано с пользователем (форвард, не отключение по P&L-порогу sample-size —
разница WR между сетапами <1 п.п., n hook/initiative <100):

1. **Детектор DD** — HVN/LVN относительно средней плотности профиля; перешеек
   = обрыв ≤ 0.5 меньшего пика (конвенция volume ledge §3.1). Гейт C3 начинает
   отличать P-shape от пилы тиков.
2. **Hook и initiative выключены** (`hook_enabled=false`,
   `initiative_exhaustion_enabled=false`). Ядро = absorption. Код сетапов на
   месте, включаются env.
3. **Окно сессии** возвращено к London+NY `07:00-16:00,12:00-21:00` (как до C4),
   чтобы C4 не смешивался с ядром. C4 `12:00-21:00` — через env.
4. **C1 merge** остаётся включённым (не изолировался этим шагом).
5. Halt/cooldown на 33004 — отдельный коммит `36673d4`.



