# STRATEGY — tradecard (канон процесс-фреймворка)

**Канон (единственный источник правды):** Chart Fanatics × SMB Capital, Jeff
Holden (Head of Trader Development) — «Inside One of the World's Top Prop Trading
Desks (The 5-Step Process)» / **Momentum Model** —
<https://youtu.be/WDdvnd9vLbM>

Этот документ описывает фреймворк **полностью и автономно**, строго по
первоисточнику (ролику). Он НЕ описывает торговую стратегию входа/выхода и НЕ
содержит наших готовых решений — только канон. Любая будущая правка `tradecard`
сверяется с этим документом и с роликом, а не с интуицией.

> ⚠️ Важно: это **не торговая стратегия**, а **процесс развития трейдера**
> (диагностика ошибок → решение → маленькие победы → momentum). Сам спикер прямо
> говорит, что хорошие трейдеры систематичны **в процессе и мышлении**, а не в
> каждом входе/выходе. Поэтому `tradecard` — это **аналитический ревьюер** над
> уже совершёнными сделками, а не генератор сигналов (см. `TASKSPEC_TRADECARD_*`).

---

## 0. Суть в одном абзаце

Рынок — это **машина по генерации возможностей**; задача трейдера — не быть
идеальным, а **системно устранять свои ошибки**. Процесс: каждый день
**фиксировать ошибки** в daily report card (без осуждения) → за неделю выделить
**1 главную повторяющуюся ошибку** → диагностировать её методом **5 Why (Toyota)**
до настоящей причины (она вскрывается на 4-5-м «почему») → получить **чёткое
решение** → внедрять его через **трение (friction)**, пока не родится **маленькая
победа (small win)**. Стакая маленькие победы, трейдер набирает **momentum**, и
кривая P&L идёт вверх «без единого большого момента». Поверх этого — **грейдинг
сделок** (A+/A/B/C) с привязкой риска к грейду и **построение playbook'ов**
(сначала один идеальный, потом расширение; карьера = baseline + A+).

---

## 1. Философия (process > outcome)

- **Рынок — opportunity generating machine.** Фокус не на себе и не на P&L, а на
  возможностях, которые рынок генерирует каждый день.
- **Growth mindset, а не outcome focus.** Маленькая победа = «ок, дальше к
  следующей», а не «я молодец, можно расслабиться». Перфекционизм вреден: достиг
  числа P&L → перестал смотреть на рынок.
- **Ошибки — это топливо роста, а не повод для самобичевания.** Кто прячется от
  ошибок — стагнирует; кто их вскрывает и правит — растёт быстрее всех.
- **Систематичность — в процессе и мышлении, не обязательно в каждой сделке.** У
  каждой сделки свой план (свои критерии входа/выхода), но процесс ревью —
  одинаков и повторяем.

> Цитаты канона: *«the market is an opportunity generating machine»*; *«you have
> to have a growth mindset»*; *«the people that make the most mistakes, as long
> as they're doing the right things after that, tend to experience the most
> growth»*; *«they're extremely systematic in their process and… mindset… not
> systematic in every exit and every entry».*

---

## 2. Элемент 1 — Goals → Friction

- Трейдер ставит цель → при первом же действии получает **friction** (трение):
  рынок «бьёт по лицу», появляются ошибки и трудности.
- **Анти-паттерн:** взорвать всё и поставить новую цель («ничего не работает,
  начну заново»). Это цикл «взорвал → начал заново», который **не строит
  momentum**.
- Цели полезны, но сами по себе momentum не создают — нужен процесс ниже.

> Цитата канона: *«everybody has a plan until they get punched in the face…
> friction is you have this wonderful plan… and then the market slaps you in the
> face… blowing things up, starting over… that process doesn't build momentum».*

---

## 3. Элемент 2 — Mistakes (daily report card)

- В **daily report card** отдельная секция — **только ошибки** (Dr. Brett
  Steenbarger — автор практики report card).
- Одну неделю: **просто выписывать** ошибки в конце дня. **Не осуждать**, **не
  анализировать сразу**, **не решать сразу**. Просто перечислить
  («продал слишком рано», «не уважал стоп», «передержал», «зашёл оверсайзом»).
- За неделю ошибки сворачиваются в **2-3 повторяющихся темы (паттерна)**.
- Признать сам факт, что трейдер **ошибается** — это нормально и обязательно.

> Цитаты канона: *«one section of your daily report card should just be
> mistakes»*; *«write out the mistakes… Just write them out. Don't judge them.
> Don't try and solve them right away»*; *«you probably have them down into a
> pattern of maybe two or three actual big mistakes».*

---

## 4. Элемент 3 — Diagnosis (5 Why, метод Toyota)

- К приоритетной ошибке задаётся **«почему?» пять раз подряд** (метод Toyota
  Production System). Каждое «почему» записывается.
- **Настоящая причина/решение обычно вскрывается на 4-5-м «почему»**, иногда
  глубже. Большинство останавливается на 1-2 и **не доходит** до решения,
  которое реально устраняет проблему.
- Это **диагностика**, а не сразу «решение». Нельзя от ошибки прыгать к решению,
  минуя 5 Why.
- Вторую неделю процесса посвящают именно тренировке 5 Why (думать так глубоко —
  «неестественно»). LLM-промпт (есть в описании ролика) помогает дойти до
  решения быстрее.

> Цитаты канона: *«we adopted this process from… Toyota… the five W's… you ask
> why five times»*; *«the solution isn't presented until the fourth or fifth
> why… most people stop at one or two»*; *«that diagnosis part is really
> important because you can't look at a mistake and come up with a solution
> right away without diagnosing it».*

**Важный нюанс из примера канона:** иногда настоящее решение — **не про самого
трейдера**, а про **рыночные условия** («этот вход работает, только когда рынок
на твоей стороне» → нужен отдельный playbook с критерием). То есть 5 Why может
вывести не «будь дисциплинированнее», а «у тебя не определён сценарий, как сделка
должна развиваться».

> Цитата канона: *«a lot of times the solutions aren't about us at all. They're
> actually about opportunities generated by the market… you don't actually know
> the way the trade is supposed to play out… how can I expect myself to hold it
> to target?»*

---

## 5. Элемент 4 — Solution → Friction → Small Win

- После 5 Why возникает **чёткое решение** (clear solution) — потому что причина
  диагностирована правильно.
- Решение **не даёт результат сразу**: при внедрении снова **friction**, снова
  ошибки. Это ожидаемо — «там и происходит рост».
- Пройдя цикл 1-2 раза с дисциплиной, трейдер получает **small win** — реально
  решённую проблему №1. Не «хоумран», а маленькая победа, с которой можно
  двигаться дальше.
- **Компаундинг:** маленькие победы стакаются, momentum нарастает; победы
  приходят на всё более высоких уровнях → кривая P&L в итоге «загибается» вверх,
  **без единого большого момента**. Число small wins за год **предсказывает**
  успех трейдера.

> Цитаты канона: *«you have this process… to get to a much more effective
> solution. And even then you're going to run into friction»*; *«if you stay
> consistent… you generate a small win»*; *«how quickly these small wins
> compound and… build momentum»*; *«the number of small wins generated over the
> course of the year will dictate the success you have as a trader».*

**Growth vs outcome на small win:** outcome-трейдер получает маленькую победу и
радуется/расслабляется; growth-трейдер — «ок, дальше к следующей». По daily
report card сразу видно, какой это тип.

> Цитата канона: *«if you're outcome focused, you get a small win and feel
> great… if you have a growth mindset, you get a small win and it's just okay, on
> to the next small win».*

---

## 6. Грейдинг сделок и риск-аллокация

Каждая сделка грейдится по качеству возможности; риск привязан к грейду
(в терминах **доли дневного стопа**):

| Грейд | Доля дневного стопа | Смысл |
|---|---|---|
| **A+** | до **80%** | редчайшая высоковероятная возможность |
| **A / B** (в ролике названы вместе) | **30%** | сильная возможность |
| **B** | **15%** | обычная нормальная сделка |
| **C** | **5%** | слабая/маргинальная возможность |

- Грейд **не меняет** сетап/возможность — меняется только **аллокация риска** и
  «насколько сильно можно бить по мячу».
- Умение **масштабировать через грейд** (больше риска на A+, меньше на C) — это
  то, что открывает переход к следующим playbook'ам.

> Цитаты канона: *«An A+ you get up to 80% of your daily stop… a B and A, you get
> 30%… a B trade you can get 15%… a C trade you allocate 5%»*; *«once you can
> start to scale by effectively allocating your capital… everything else opens
> up».*

---

## 7. Playbooks (один → четыре; baseline + A+)

- **Развивающийся трейдер:** довести до идеала **ОДИН** playbook («положить один
  кирпич идеально»). Это даёт структуру для остальных.
- От 1 к 4 переход уже лёгкий (есть процесс). Развивающемуся нужно **≈4**
  playbook'а (рынок меняется, нужны разные возможности). Опытные имеют **18-25**.
- **Не «изобретать колесо»:** взять чужую модель, **сделать своей** под свою
  личность, и стартовать.
- **A + B = C (Career):** карьера = **A+ возможности** ПЛЮС **baseline**. Это
  **разные playbook'и**. Baseline строят **первым** (A+ редки и лежат «на хвосте
  распределения», под них нужен отдельный playbook, но появляются они нечасто).
- **Анти-паттерн «big-game hunting»:** в трудном рынке трейдеры гонятся только за
  A+ и буксуют, потому что **не фокусируются на baseline-победах**. Возвращение к
  baseline восстанавливает momentum.
- **Опасность одного playbook'а:** если он один — нужно быть в нём лучшим в мире;
  при смене рыночных условий один playbook перестаёт работать.

> Цитаты канона: *«start with the smallest win… usually one playbook… do the best
> job with one single playbook»*; *«you want to get to at least about four…
> experienced traders have 18, 20, 25»*; *«A+ plus baseline equals career. So A
> plus B equals C»*; *«a lot of our traders that pulled out their baseline
> playbooks are doing quite well. It's the traders only looking for A+ that have
> shifted».*

---

## 8. Адаптация к рынку и новым активам

- Задача трейдера — **«делать то, что делает рынок»**, а не повторять «то, что я
  делал». Когда меняются участники/условия — меняются и возможности.
- У каждого нового актива (пример канона: Bitcoin, металлы) — **свой характер,
  свои участники, свои приоритеты**; нельзя слепо переносить playbook. Есть
  кривая обучения, её надо уважать; обычно через пару недель набора участников
  актив торгуется чище.

> Цитаты канона: *«our job is never to say I'm going to do exactly what I did.
> It's to do what the market is doing»*; *«this is a different market with
> different participants… there is a learning curve… you have to make
> adjustments».*

---

## 9. Чего фреймворк НЕ делает (анти-канон)

- **Не прыгает от ошибки сразу к решению** (без 5 Why диагностики).
- **Не работает над двумя целями одновременно** (research: КПД падает — начинать
  с самой важной; риск/стоп — всегда первым).
- **Не гонится за P&L / хоумранами** в ущерб маленьким победам.
- **Не строит A+ playbook первым** для развивающегося трейдера (сначала
  baseline).
- **Не считает решением общие фразы** («буду дисциплинированнее», «поставлю стоп
  туже») — это «короче настоящего решения».
- **Не делает выводов на поверхностном анализе** (1-2 «почему») и не накапливает
  momentum через «взорвал → начал заново».

> Цитаты канона: *«if you try and work on two goals at the same time, you're
> probably not going to get any of them very far… start with the most important
> one… it would be stop-loss»*; *«saying you need to be tighter with your stop…
> those are really short of what the actual solution is».*

---

## 10. Сверка с первоисточником (verification)

| Пункт документа | Подтверждение в ролике | ✓ |
|---|---|---|
| Рынок = opportunity machine; growth mindset | «the market is an opportunity generating machine»; «you have to have a growth mindset» | ✓ |
| Goals → Friction; анти-паттерн «взорвал/заново» | «friction… the market slaps you in the face… blowing things up, starting over… doesn't build momentum» | ✓ |
| Daily report card, секция «ошибки», без осуждения | «one section of your daily report card should just be mistakes… write them out. Don't judge them» | ✓ |
| Неделя ошибок → 2-3 паттерна | «you probably have them down into a pattern of maybe two or three big mistakes» | ✓ |
| 5 Why (Toyota), решение на 4-5-м | «from Toyota… five W's… you ask why five times»; «solution isn't presented until the fourth or fifth why» | ✓ |
| Диагностика до решения, не наоборот | «can't look at a mistake and come up with a solution without diagnosing it» | ✓ |
| Решение часто про рынок, а не про себя | «the solutions aren't about us at all… opportunities generated by the market» | ✓ |
| Знать, как сделка должна развиваться (hold to target) | «you don't actually know the way the trade is supposed to play out… how can I hold it to target?» | ✓ |
| Solution → friction → small win; компаундинг | «to get to a much more effective solution… and even then friction»; «small wins compound and build momentum» | ✓ |
| Число small wins за год предсказывает успех | «the number of small wins… will dictate the success you have as a trader» | ✓ |
| Growth vs outcome на маленькой победе | «outcome focused… feel great… growth mindset… on to the next small win» | ✓ |
| Грейдинг A+/A/B/C = 80/30/15/5% дневного стопа | «A+ you get up to 80%… a B and A, 30%… a B trade 15%… a C trade 5%» | ✓ |
| Один playbook → ≈4 → 18-25; baseline первым | «start with… one playbook… get to about four… experienced have 18, 20, 25» | ✓ |
| A + B = C (A+ + baseline = career) | «A plus baseline equals career. So A plus B equals C» | ✓ |
| Big-game-hunting вредит; вернуться к baseline | «only looking for A+… have shifted… pulled out baseline playbooks are doing quite well» | ✓ |
| Не работать над 2 целями; риск первым | «work on two goals at the same time… not get any very far… start with… stop-loss» | ✓ |
| Делать то, что делает рынок; новые активы | «do what the market is doing»; «different market… different participants… learning curve» | ✓ |
| Систематичность в процессе/мышлении, не в каждом входе | «systematic in their process and… mindset… not in every exit and every entry» | ✓ |

Все пункты документа имеют прямое подтверждение в ролике. Расхождений с
первоисточником при сверке не выявлено.

---

## 11. Открытые вопросы к реализации (решаются в TASKSPEC, не в каноне)

Фреймворк канона описывает **человека-трейдера**. Наш «трейдер» —
**LLM-агент** `fx_ai_trader` (DeepSeek + cTrader). Адаптация (что считать
«ошибкой агента», как грейдить сделку из данных БД, как гонять 5 Why через LLM,
куда отдавать «решения») — это **реализация**, она в:

- `TASKSPEC_TRADECARD_FX.md` — ревьюер над данными `fx_ai_trader` (cTrader/Forex).

Ключевой инвариант адаптации (зафиксирован в ТЗ): `tradecard` —
**advisory-only**. Он **наблюдает и предлагает**, но **не меняет** торговую
логику/пороги/промпты автоматически (правила `no-data-fitting.mdc`,
`sample-size.mdc`, `strategy-guard.mdc` «изменения стратегий только с одобрения»).
«Маленькая победа» = снижение частоты конкретной ошибки агента на достаточной
выборке, а не правка стратегии под последние сделки.
