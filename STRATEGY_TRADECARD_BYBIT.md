# STRATEGY — tradecard (канон для детерминированных Bybit-ботов)

**Канон (единственный источник правды):** Chart Fanatics × SMB Capital, Jeff
Holden (Head of Trader Development) — «Inside One of the World's Top Prop Trading
Desks (The 5-Step Process)» / **Momentum Model** —
<https://youtu.be/WDdvnd9vLbM>

Этот документ — **отдельный канон** процесс-фреймворка SMB Momentum Model в
адаптации под **детерминированные rule-based боты** проекта: **`scalp_bot`**
(Bybit, orderflow sweep/density) и **`flowzone_bot`** (Bybit, auction/volume-
profile). Универсальный канон фреймворка (для LLM-агентов) живёт в
`STRATEGY_TRADECARD.md` и **остаётся без изменений** — здесь не дубликат, а
**deterministic-чтение того же ролика**: что в модели меняется, когда у «трейдера»
нет дискреции.

> ⚠️ Это **не торговая стратегия** входов/выходов (она своя у каждого бота —
> `STRATEGY_*` / `STRATEGY_FLOWZONE.md`). Это **процесс развития** торговой
> системы: диагностика повторяющихся убыточных паттернов → гипотеза-решение →
> валидированная маленькая победа → momentum. `tradecard` — **аналитический
> ревьюер** над уже совершёнными сделками ботов, а не генератор сигналов и **не**
> автотюнер конфига (см. `TASKSPEC_TRADECARD_BYBIT.md`).

---

## 0. Суть в одном абзаце

Рынок — **машина по генерации возможностей**; задача — не идеальная система, а
**системное устранение её повторяющихся ошибок**. Для детерминированного бота
«ошибка» — это **не психология** (бот не паникует и не жадничает), а
**воспроизводимый убыточный паттерн правил**: страта/сетап, который системно
теряет в конкретном режиме/сессии/символе, или скоринг, который не отделяет
винов от лузов. Процесс: каждый период **фиксировать такие паттерны** в report
card (без подгонки выводов) → выделить **1 главный повторяющийся** →
диагностировать его методом **5 Why** до настоящей причины (часто она «про
рынок», а не «про бота» — нужен фильтр режима/новый playbook) → получить
**гипотезу-решение** → внедрять её **только после одобрения человеком и OOS-
валидации**, пока не родится **маленькая победа (small win)** = статистически
подтверждённое снижение паттерна. Маленькие победы стакаются в **momentum**.
Поверх — **грейдинг сделок** (наш `score` ↔ A+/A/B/C) и **playbook'и** (наш
`strategy`): baseline + A+.

---

## 1. Философия (process > outcome) и место детерминированной системы

- **Рынок — opportunity generating machine.** Фокус — на возможностях, которые
  рынок генерирует, а не на P&L и не на «красоте» конфига.
- **Growth mindset, а не outcome focus.** Маленькая победа = «ок, дальше к
  следующей». Достигли таргета P&L → не «расслабились и перестали улучшать
  систему».
- **Ошибки — топливо роста.** Кто прячет убыточный паттерн (или «замазывает» его
  подгонкой порогов под последние сделки) — стагнирует; кто вскрывает причину и
  валидирует фикс — растёт.
- **Ключевой мост к деттерминизму:** спикер прямо говорит — топ-трейдеры
  *«extremely systematic in their process and mindset, NOT systematic in every
  exit and every entry»*. Детерминированный бот **уже** систематичен в каждом
  входе/выходе (это его сила). Значит, фреймворк SMB ложится **не на сами
  входы**, а **на слой развития системы**: процесс ревью, диагностики и
  валидированных улучшений правил. Это делает модель применимой к боту даже
  чище, чем к человеку.

> Цитаты канона: *«the market is an opportunity generating machine»*; *«you have
> to have a growth mindset»*; *«they're extremely systematic in their process
> and… mindset… not systematic in every exit and every entry».*

---

## 2. Кто «трейдер» в детерминированной системе (главная адаптация)

В ролике «трейдер» — человек, который ошибается и учится. У `scalp_bot` /
`flowzone_bot` исполнитель — **детерминированный движок без дискреции**. Поэтому
роль «трейдера, которого развивают по модели» распадается на два уровня:

1. **Стратегия + её конфиг** — это «playbook», который развивается. Его «ошибки»
   — не намерения, а **свойства правил**: где сетап системно проигрывает, где
   скоринг не работает, где выход оставляет деньги на столе.
2. **Человек-оператор** (ты) — единственный, кто проходит цикл SMB: читает report
   card, гоняет 5 Why на причину паттерна, формулирует гипотезу и **одобряет**
   изменение конфига (правило `strategy-guard.mdc`: правки стратегии — только с
   одобрения).

> Следствие: `tradecard` для Bybit-ботов — инструмент **оператора**, а не
> «самообучение бота». Бот ничего не «усваивает» сам (в отличие от LLM-агента
> `fx_ai_trader` с таблицей `lessons`). Все находки → человеку.

---

## 3. Элемент 1 — Goals → Friction

- Ставим цель системе (напр. «поднять WR sweep_fade», «снять просадку на
  density_break в флете») → при первом же контакте с рынком получаем **friction**:
  паттерн не уходит, появляется новый, метрика проседает на другом срезе.
- **Анти-паттерн (особо опасен для детерминированных ботов):** «взорвать и начать
  заново» = массово перетюнить пороги/выключить страту под свежую просадку. Это
  цикл, который **не строит momentum** и прямо нарушает `no-data-fitting.mdc` и
  `sample-size.mdc`.
- Цель полезна, но momentum создаёт только процесс ниже (диагностика → валидация).

> Цитата канона: *«everybody has a plan until they get punched in the face…
> friction… the market slaps you in the face… blowing things up, starting over…
> that process doesn't build momentum».*

---

## 4. Элемент 2 — Mistakes (daily/weekly report card, deterministic-чтение)

- Секция report card — **только повторяющиеся убыточные паттерны системы**,
  наблюдаемые **из данных** (`trades`: `score`, `reasons`, `strategy`, `mode`,
  `close_reason`, `pnl_usd`). НЕ психология, НЕ единичный лосс.
- Сначала просто **фиксировать** паттерны за период. **Не подгонять под желаемый
  P&L**, **не тюнить сразу** (deterministic-аналог «don't judge / don't solve
  right away» из канона — здесь это совпадает с `no-data-fitting.mdc`).
- За период паттерны сворачиваются в **2-3 повторяющиеся темы** (напр.
  «sweep_fade теряет на round-уровнях», «density_break дает SL-кластеры в
  азиатскую сессию», «высокий score не отделяет винов»).
- Признать, что **система ошибается** — это нормально и обязательно; цель — найти
  и устранить, а не защитить конфиг.

> Цитаты канона: *«one section of your daily report card should just be
> mistakes»*; *«write them out. Don't judge them. Don't try and solve them right
> away»*; *«you probably have them down into a pattern of maybe two or three big
> mistakes».*

**Что НЕ является «ошибкой» детерминированного бота:** запланированный лосс по
правилам (SL сработал как задумано) — это **не** ошибка, это стоимость эджа.
Ошибка — когда **повторяющийся** срез данных показывает, что правило системно
неоптимально (а не один неудачный исход).

---

## 5. Элемент 3 — Diagnosis (5 Why, метод Toyota)

- К приоритетной теме задаётся **«почему?» пять раз**. Каждое «почему» —
  опирается на срез данных (не на интуицию).
- **Настоящая причина обычно на 4-5-м «почему»**. Большинство останавливается на
  1-2 («просто рынок плохой») и не доходит до причины, которую можно устранить
  фильтром/правилом.
- Это **диагностика**, а не сразу фикс. Нельзя от убыточного среза прыгать к
  «подкрутим порог» — это подгонка.
- LLM (DeepSeek) используется как **аналитический помощник** 5 Why над агрегатами
  — read-only, ничего не меняет. (У Bybit-ботов своего LLM нет; DeepSeek-клиент
  инлайнится копией внутри отдельного пакета `src/tradecard_bybit/` — см. TASKSPEC.)

> Цитаты канона: *«from Toyota… the five W's… you ask why five times»*; *«the
> solution isn't presented until the fourth or fifth why… most people stop at one
> or two»*; *«you can't look at a mistake and come up with a solution right away
> without diagnosing it».*

**Важнейший нюанс канона для детерминированных ботов:** решение часто **не про
систему, а про рынок** — *«a lot of times the solutions aren't about us at all…
they're about opportunities generated by the market»*. Для бота это переводится
почти буквально: причина паттерна — **не «баг в правиле»**, а то, что **сетап
работает только в определённом режиме/сессии/ликвидности**, которого у страты
сейчас нет в фильтрах. Решение = **новый playbook или фильтр-условие**, а не
«сделай стоп туже».

> Цитата канона: *«you don't actually know the way the trade is supposed to play
> out… how can I expect myself to hold it to target?»* — для бота: «у страты не
> определён сценарий/режим, в котором сетап валиден».

---

## 6. Элемент 4 — Solution → Friction → Small Win (с жёстким OOS-гейтом)

- После 5 Why возникает **чёткая гипотеза-решение** (напр. «добавить session-
  фильтр», «гейт round-уровня», «перекалибровать score-веса»).
- Гипотеза **не даёт результат сразу**: при внедрении — снова friction (новые
  срезы, side-effects). Это ожидаемо.
- **Small win** засчитывается ТОЛЬКО когда: (а) изменение **одобрено человеком** и
  внедрено как обычная правка стратегии (`strategy-guard.mdc`); (б) на **OOS /
  forward**-выборке подтверждено **статистически значимое** снижение паттерна
  (`sample-size.mdc`: ≥100 сделок связки, ≥2 недели, p<0.05, разница WR≥10% или
  R:R≥0.3). До этого — статус **НАБЛЮДЕНИЕ/ГИПОТЕЗА**, не победа.
- **Компаундинг:** валидированные маленькие победы стакаются → momentum; число
  small wins за период **предсказывает** прогресс системы.

> Цитаты канона: *«to get to a much more effective solution. And even then you're
> going to run into friction»*; *«if you stay consistent… you generate a small
> win»*; *«how quickly these small wins compound and build momentum»*; *«the
> number of small wins… will dictate the success you have as a trader».*

**Growth vs outcome:** outcome-оператор увидел одну прибыльную неделю и
«успокоился»; growth-оператор фиксирует small win только после OOS-подтверждения
и идёт к следующей теме. Подгонка под красивый бэктест = **outcome-фокус** и
прямое нарушение `no-data-fitting.mdc`.

> Цитата канона: *«if you're outcome focused, you get a small win and feel
> great… growth mindset… on to the next small win».*

---

## 7. Грейдинг сделок (канон ↔ поле `score`)

У обоих ботов **уже есть** числовой `score` на каждом входе — это наш
естественный аналог грейдов A+/A/B/C:

- `scalp_bot`: `score` = число сработавших факторов сетапа (sweep/cvd_div/
  reclaim/mom + бонусы) — см. `analysis/signals.py`.
- `flowzone_bot`: `score` = confluence-score зоны (число VP-факторов) — см.
  `analysis/strategy.py`.

| Грейд канона | Доля дневного стопа (канон) | Наш референс |
|---|---|---|
| **A+** | до **80%** | верхний бакет `score` |
| **A / B** | **30%** | сильный бакет |
| **B** | **15%** | средний бакет |
| **C** | **5%** | нижний бакет |

- Задача `tradecard` — **аналитика, не управление риском**: проверить гипотезу
  *«выше `score` → лучше перформанс (WR/EXP/R)»*. Если score **не** отделяет
  винов — это сама по себе «ошибка №1» (грейдинг сломан → тема для 5 Why).
- Риск-аллокация канона (80/30/15/5%) **не применяется автоматически**: риск-
  модель ботов фиксирована (`risk_per_trade_usd`), меняется только с одобрения.
  Грейд-таблица канона — **референс в отчёте**.

> Цитаты канона: *«An A+ you get up to 80% of your daily stop… a B and A, 30%… a
> B trade 15%… a C trade 5%»*; *«once you can start to scale by effectively
> allocating your capital… everything else opens up».*

---

## 8. Playbooks (канон ↔ поле `strategy`)

«Playbook» канона = наша **страта** (`trades.strategy`):

- `scalp_bot` — **мультистратегийный** (`sweep_fade`, `sweep_fade_canon`,
  `density_break`, `density_bounce`). Это уже «несколько playbook'ов».
- `flowzone_bot` — пока **один** playbook (`flowzone`).

Маппинг канона:

- **Один playbook → ≈4 → 18-25.** Развивающейся системе нужно ≈4 рабочих
  playbook'а под разные режимы; `flowzone` (один) — по канону «нужно быть в нём
  лучшим в мире», и он хрупок к смене режима.
- **A + B = C (Career):** карьера = **A+ возможности** ПЛЮС **baseline**. Это
  **разные playbook'и/бакеты**. Baseline строят **первым**.
- **Анти-паттерн «big-game hunting»:** гнаться только за редкими high-`score`
  (A+) сетапами и буксовать, забросив baseline-страту, которая и генерит
  маленькие победы. Возврат к baseline восстанавливает momentum. Для нас:
  выключать «скучную, но плюсовую» страту ради редкого high-score — это
  big-game-hunting.
- **Не «изобретать колесо»:** взять каноничную модель (наши страты так и
  построены — CAP/Connors-Raschke/Steidlmayer), адаптировать, валидировать.

> Цитаты канона: *«start with the smallest win… one playbook… do the best job
> with one single playbook»*; *«get to at least about four… experienced traders
> have 18, 20, 25»*; *«A plus baseline equals career. So A plus B equals C»*; *«a
> lot of our traders that pulled out their baseline playbooks are doing quite
> well. It's the traders only looking for A+ that have shifted».*

---

## 9. Адаптация к рынку и новым активам (режим/сессия/вселенная)

- Задача — **«делать то, что делает рынок»**, а не повторять «то, что страта
  делала». Меняются участники/режим — меняются и валидные возможности.
- У каждого символа/режима **свой характер**; нельзя слепо переносить пороги
  страты на новый символ. Оба бота используют **авто-вселенную** (`momentum_
  universe` / `rvol`) — это и есть «выбор площадки по характеру рынка»; есть
  кривая обучения на новом наборе участников.
- Сессии (для flowzone — London/NY ликвидность критична) — часть «режима»: тот же
  сетап в тонкой сессии = другой DGP.

> Цитаты канона: *«our job is never to say I'm going to do exactly what I did.
> It's to do what the market is doing»*; *«this is a different market with
> different participants… there is a learning curve… you have to make
> adjustments».*

---

## 10. Чего фреймворк НЕ делает (анти-канон, deterministic-усиление)

- **Не прыгает от убыточного среза сразу к тюнингу порога** (без 5 Why и без OOS).
- **Не работает над двумя целями одновременно** (начинать с самой важной; риск/
  killswitch — всегда первым).
- **Не гонится за P&L / красивым бэктестом** в ущерб валидированным маленьким
  победам.
- **Не строит A+ playbook первым** — сначала baseline.
- **Не считает решением общие фразы** («сделать стоп туже», «убрать плохой
  символ») без диагностики и выборки.
- **Не подгоняет конфиг под последние N сделок** — это classic overfitting и
  прямое нарушение `no-data-fitting.mdc` + `sample-size.mdc`.
- **Не считает запланированный SL «ошибкой»** — ошибка только в повторяющемся
  паттерне на достаточной выборке.

> Цитаты канона: *«work on two goals at the same time… not get any very far…
> start with… stop-loss»*; *«saying you need to be tighter with your stop… those
> are really short of what the actual solution is».*

---

## 11. Сверка с первоисточником (verification)

| Пункт документа | Подтверждение в ролике | ✓ |
|---|---|---|
| Рынок = opportunity machine; growth mindset | «the market is an opportunity generating machine»; «growth mindset» | ✓ |
| Систематичность в процессе/мышлении, не в каждом входе → ложится на слой развития системы | «systematic in their process and… mindset… not in every exit and every entry» | ✓ |
| Goals → Friction; анти-паттерн «взорвал/заново» | «friction… slaps you in the face… blowing things up, starting over… doesn't build momentum» | ✓ |
| Report card = секция «ошибки», без осуждения/мгновенного фикса | «one section… should just be mistakes… Don't judge them. Don't solve right away» | ✓ |
| Период ошибок → 2-3 паттерна | «down into a pattern of maybe two or three big mistakes» | ✓ |
| 5 Why (Toyota), решение на 4-5-м | «from Toyota… ask why five times»; «solution isn't presented until the fourth or fifth why» | ✓ |
| Диагностика до решения | «can't look at a mistake and come up with a solution without diagnosing it» | ✓ |
| Решение часто про рынок, а не про нас → фильтр режима/новый playbook | «the solutions aren't about us at all… opportunities generated by the market» | ✓ |
| Знать, как сделка должна развиваться | «you don't actually know the way the trade is supposed to play out» | ✓ |
| Solution → friction → small win; компаундинг | «more effective solution… and even then friction»; «small wins compound and build momentum» | ✓ |
| Число small wins предсказывает успех | «the number of small wins… will dictate the success you have» | ✓ |
| Growth vs outcome на маленькой победе | «outcome focused… feel great… growth mindset… on to the next small win» | ✓ |
| Грейдинг A+/A/B/C = 80/30/15/5% (↔ наш `score`) | «A+ up to 80%… B and A 30%… B 15%… C 5%» | ✓ |
| Один playbook → ≈4 → 18-25; baseline первым (↔ наш `strategy`) | «one playbook… get to about four… experienced have 18, 20, 25» | ✓ |
| A + B = C (A+ + baseline = career) | «A plus baseline equals career. So A plus B equals C» | ✓ |
| Big-game-hunting вредит; вернуться к baseline | «only looking for A+… have shifted… baseline playbooks doing quite well» | ✓ |
| Не работать над 2 целями; риск первым | «two goals at the same time… start with… stop-loss» | ✓ |
| Делать то, что делает рынок; новые активы/режимы | «do what the market is doing»; «different participants… learning curve» | ✓ |

Все пункты опираются на ролик. Адаптация (deterministic-чтение) не добавляет
ничего сверх канона — только переводит роли «трейдер/ошибка/playbook/победа» на
язык rule-based системы.

---

## 12. Связь с правилами проекта (для детерминированных ботов — критично)

- `no-data-fitting.mdc` — **центральное** правило здесь: у детерминированной
  системы соблазн подогнать пороги под просадку максимален. Каждая «ошибка»,
  «тема», «гипотеза», «победа» обязана опираться на **артефакт анализа** (срез
  БД / backtest / OOS), не на интуицию.
- `sample-size.mdc` — «тема»/«победа» только на ≥100 сделок связки, ≥2 недели,
  p<0.05. Иначе НАБЛЮДЕНИЕ.
- `strategy-guard.mdc` — `tradecard` **advisory-only**; никаких автоправок порогов
  страт. Любая гипотеза → человеку, внедрение — отдельным одобренным коммитом с
  обновлением `STRATEGY_*`/тестов.
- `stats-collection.mdc` — P&L ground truth = Bybit `closedPnl` (net, через
  reconcile / `scripts/collect_bybit_3bots_stats.py`), не «расчётный» БД-PnL;
  paper и live раздельно (`mode`).
- `buildlog.mdc` — записи в `BUILDLOG_TRADECARD_BYBIT.md`.

---

## 13. Реализация → TASKSPEC

Адаптация канона в код (детекторы паттернов из `score`/`reasons`/`strategy`,
грейд-vs-перформанс, 5 Why через LLM, OOS-гейт small wins, отчёты) описана в:

- `TASKSPEC_TRADECARD_BYBIT.md` — ревьюер над данными `scalp_bot` и
  `flowzone_bot` (Bybit, детерминированные).

Ключевой инвариант: `tradecard` **наблюдает и предлагает**, но **не меняет**
торговую логику/пороги ботов. «Маленькая победа» = OOS-подтверждённое снижение
конкретного паттерна после **одобренной человеком** правки, а не подгонка
конфига под последние сделки.
