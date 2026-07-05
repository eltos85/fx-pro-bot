# Аудит реализации бота по стратегии «скальпинг на скам-токенах» — ответы по пунктам

> Ответ на аудит стратегии из `RESEARCH_SCAM_TOKEN_SCALP.md` с целью оценки
> реализуемости **торгового бота**. По каждому замечанию аудита — что говорит
> интернет и реалистичный вердикт. Источники приведены по пунктам.
>
> Это исследовательский анализ, не плановое изменение ботов проекта. Правила
> `no-data-fitting.mdc`, `sample-size.mdc`, `strategy-guard.mdc`, `api-docs.mdc`
> учитываются по ходу.

## Итоговый вердикт (кратко)

- **Сканер «ёрш»-паттернов** — реализуем, есть зрелая литература и готовые
  библиотеки. Это самая сильная часть. Пороги из блогов (RisingWave 2%/Z≥3/0.65)
  — **только стартовая точка для калибровки на своих данных**, не канон.
- **Фильтр спуфинга** — реализуем **на конкретных биржах** (MEXC, Bitget):
  у них есть официальные incremental L2-diff-фиды с sequence-номерами и
  процедурой восстановления. На «настоящих tier-3» (LBank, XT, CoinEx) —
  действительно ломается.
- **Исполнение** — противоречие exit-логики **частично** снимается через
  time-stop + density-routed limit exit + spoof-pull cancel + kill-switch.
  **Важно: time-stop решает только проблему охоты (намеренного выбивания), а
  не проблему исполнения — market-out по таймеру в пустом стакане ест тот же
  slippage, что и сработавший price-stop.** Стоимость time-stop-выходов —
  ключевая метрика Фазы 2; без неё теоретический P&L Фазы 1 завышен. Источники
  по exit-логике (chartscout, LedgerMind, AlphaEngine, mmgpt-pro) — SEO-блоги и
  GitHub-репозитории неизвестного качества, **не research-источник правды**;
  по духу `api-docs.mdc`/`no-data-fitting.mdc` это гипотезы для проверки на
  собственной ленте, не готовые правила. Latency и footprint — см. п.3б/3в.
- **Застревание/биржа** — кодом не решается, только bounded: allocation cap +
  daily sweep в cold storage + 2 биржи в разных юрисдикциях.
- **Экономика + sample-size** — full-automation ROI отрицателен при депо
  $100–$1000 (dev cost >> ceiling). `sample-size.mdc` выполним **на уровне
  класса сетапа** (≥100 trades across tokens), не per-token. **Гибрид
  (человек-исполнитель) не предлагается — пользователь торгует руками не будет.**
  Честная развилка после Фазы 2: либо full-automation с признанной
  отрицательной экономикой, либо остановка на уровне «research pipeline +
  данные». Положительный результат Фазы 2 **не обязан** вести к торговому боту.

## Трансформация гипотезы при сужении до MEXC/Bitget

Сужение вселенной до MEXC + Bitget (необходимое для качественного L2-фида и
`api-docs.mdc`) **подтачивает исходный edge**. Премиса Клеццова — «глупый MM на
непопулярной бирже». MEXC и Bitget — топ-10 площадки с публичными
профессиональными MM-программами (rebate за объём, требования к спреду/uptime).
Хороший фид и тупой повторяющийся алгоритм — **отчасти взаимоисключающие**:
чем лучше инфраструктура биржи, тем меньше шанс, что там живёт «повторяющийся
принт, который можно систематически снимать».

Это не убивает гипотезу — MEXC листит тысячи мусорных токенов, и среди них
могут быть объекты с подходящим поведением. Но **гипотеза стала другой**, чем
в исходном ролике: «на top-10 бирже с prof MM-программой, среди тысяч
малоизвестных токенов, встречаются ли повторяющиеся прострелы от genuine
density, exploitable мелким сайзом». **Именно эту преобразованную гипотезу
должна проверить Фаза 1**, а не исходную «глупый MM на tier-3». Если Фаза 1
покажет, что на MEXC/Bitget повторяющихся принтов нет (потому что MM там
профессиональные) — стратегия в этой формулировке закрывается, и переходить
на худший фид ради «тупого MM» значит пожертвовать spoof-фильтром и
`api-docs.mdc` — то есть отменить решения пунктов 2 и 3б.

## Качество источников — явная маркировка

По духу `api-docs.mdc` и `no-data-fitting.mdc` источники неравны, и это надо
зафиксировать явно, чтобы не ссылаться на блог как на research:

- **Research-grade (можно цитировать как опору):**
  - arXiv 2412.18848 (ML pump-and-dump detection) — академическая, peer-reviewed.
  - Официальные API-доки MEXC/Bitget (incremental order book, rate limits,
    server locations) — первичная документация.
  - dxFeed Iceberg Detection Solution — production-grade vendor-документация.
- **Стартовая точка для калибровки (не брать как готовые пороги):**
  - RisingWave SQL-детектор (пороги 2%/Z≥3/buy_ratio 0.65) — engineering-блог,
    разумная отправная точка, но калибровать на своей ленте.
  - tripolskypetr/volume-anomaly, punyamodi/Deep-Market-Maker — open-source
    реализации алгоритмов (Hawkes/CUSUM/BOCPD), алгоритмы канонические,
    параметры — нет.
- **Гипотезы для проверки в Фазе 2, не источник правды:**
  - chartscout, LedgerMind, AlphaEngine, mmgpt-pro — SEO-блоги и репозитории
    неизвестного качества (mmgpt-pro выглядит маркетинговым). Утверждение
    «time-stop outperforms price-stop на тонких книгах» — тезис из блога, не
    research. Все exit-пороги (180s, OBI<45%, delta-flip, kill-switch 2–5%/день)
    — гипотезы, которые Фаза 2 должна оценить на собственной ленте, а не
    импортировать как константы.
- **Operational practices (directionally верно, не количественно):**
  - Changelly, Sesamcoin, finconduit, decentralised.news — практики risk/exchange
    ops; конкретные цифры (5% hot, cold 75–90%) — институциональные, для retail
    пересчитываются под свой размер.

---

## Пункт 1. Сканер «ёрш»-паттернов

### Замечание аудита
«Реализуем, это лучшая часть. По всем парам tier-3 детектить регулярные
прострелы от фиксированной плотности одинаковым принтом. Сбор trades + L2,
кластеризация принтов по размеру, статистика повторяемости.»

### Что говорит интернет
Это полностью покрытая тема — есть и SQL-реализации, и ML, и готовые библиотеки:

- **RisingWave, «Building a Real-Time Crypto Pump-and-Dump Detector with SQL»**
  (https://risingwave.com/blog/build-real-time-crypto-pump-dump-detector-sql/) —
  готовая SQL-схема детектора: 1-минутный return ≥2%, volume Z-score ≥3,
  buy_ratio ≥0.65 → pump. Сlide-окно, материализованные view, sink в Kafka.
  Это буквально наш «прострел» в формализованном виде.
  ⚠️ **Пороги 2%/Z≥3/0.65 — engineering-эвристика из блога, не research. Брать
  только как стартовую точку для калибровки на собственной ленте MEXC/Bitget
  (Фаза 1). Канонических порогов для нашего сетапа «регулярный прострел от
  genuine density» в литературе нет — их нужно вывести из данных** (см.
  «Качество источников» в начале документа).
- **arXiv 2412.18848, «ML-Based Detection of Pump-and-Dump in Real-Time»**
  (https://arxiv.org/html/2412.18848v1) — Random Forest + AdaBoost детектят
  pump в течение секунд; фичи из order book WebSocket: bid-ask spread, order
  size, imbalance ratios. 24/43 pump'а идентифицированы в топ-5 кандидатов.
- **tripolskypetr/volume-anomaly** (https://github.com/tripolskypetr/volume-anomaly) —
  готовая TS-библиотека: Hawkes process, CUSUM, BOCPD для детекции аномалий
  trade flow. Zero-dependency. Прямо отвечает на «статистика повторяемости»:
  Hawkes ловит self-excitation (один принт порождает серию), CUSUM —
  структурный сдвиг, BOCPD — смену режима.
- **punyamodi/Deep-Market-Maker** (https://github.com/punyamodi/Deep-Market-Maker) —
  bivariate Hawkes на bid/ask arrivals, toxicity prediction. Можно переиспользовать
  для оценки «тупой MM vs токсичный flow».

### Уточнение под нашу страту
Классический pump-and-dump-детектор ищет **разовые** всплески. Наш сетап
другой — **регулярно повторяющиеся** прострелы от одной плотности. Поэтому
поверх стандартных фич нужны:

1. **Кластеризация принтов по размеру** (DBSCAN по (size, price) внутри окна) —
   выявление «одинакового принта».
2. **Repeat-frequency test**: для каждого кластера проверять гипотезу
   «прострелы происходят с регулярностью выше случайной» — poi-процесс против
   наблюдаемого распределения интервалов, p-value < 0.05.
3. **Привязка к плотности**: прострел должен стартироваться от уровня, где
   L2-стенка стабильно держалась > N минут (это уже bridge к пункту 2).

### Вердикт
✅ Реализуем дёшево. Ошибка детекции стоит ноль (не торгуем — собираем данные).
Это **правильная первая фаза**: она же закрывает раздел 11 исходного документа
(частота, hold time, теоретический P&L) до риска деньгами.

---

## Пункт 2. Фильтр спуфинга — качество L2-фида

### Замечание аудита
«Самая наукоёмкая часть. Нужен качественный incremental orderbook feed. На
tier-3 он часто кривой: пропуски sequence, снапшоты раз в секунду вместо
diff'ов, агрегированные уровни. Если фид не позволяет отследить жизнь
конкретной заявки — фильтр деградирует до эвристики.»

### Что говорит интернет — фид на конкретных биржах
Главная находка: **MEXC и Bitget имеют официальные incremental L2-diff-фиды
с sequence-номерами и процедурой восстановления после packet loss.** Это
ломает допущение «tier-3 = кривой фид» для двух конкретных кандидатов.

- **MEXC, «How to Properly Maintain a Local Copy of the Order Book»**
  (https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/how-to-properly-maintain-a-local-copy-of-the-order-book) —
  spot-канал `spot@public.aggre.depth.v3.api.pb@(100ms|10ms)@{symbol}`,
  инкрементальные апдейты с `fromVersion`/`toVersion`, проверка
  `fromVersion == localLastVersion + 1`, при разрыве — reinit через REST
  snapshot `depth?symbol=&limit=5000`.
- **MEXC Futures, «Incremental Order Book Maintenance Mechanism»**
  (https://www.mexc.com/api-docs/futures/websocket-api/incremental-order-book-maintenance-mechanism) —
  отдельный endpoint `depth_commits/{symbol}/1000` для восстановления после
  packet loss без полного reinit. Это именно то, чего боится аудит: механизм
  backfill'а пропущенных событий задокументирован официально.
- **Bitget, «Depth Channel»**
  (https://www.bitget.com/api-doc/contract/websocket/public/Order-Book-Channel) —
  `books`: первый push = snapshot, далее incremental `update`; поля `seq`
  (монотонный) и `pseq` (предыдущий seq) для детекции out-of-order/gaps.
  Частота books — 150ms, books1 — 10ms.
- **Bitget SBE (Simple Binary Encoding) depth50**
  (https://www.bitget.com/api-doc/uta/sbe/sbe-depth50) — бинарный фрейм,
  20ms push, `seq` uint64 для packet-loss detection. Это уже уровень,
  близкий к HFT-фиду.

→ `api-docs.mdc` для MEXC и Bitget **выполним**: есть официальная дока с
rate-limits, sequence-семантикой, recovery-процедурой. Замечание «официальной
доки уровня Bybit у таких бирж часто просто нет» **неверно для MEXC/Bitget**
(они top-10 по объёму, не настоящий tier-3). Замечание **остаётся в силе** для
LBank/XT/CoinEx и более мелких — там фид действительно может быть
snapshot-only.

### Что говорит интернет — детекция iceberg/spoof
Фильтр формализуем в фичи по L2-diff-фиду, есть готовые правила и продукты:

- **Nydar, «Iceberg Orders & Spoofing»** (https://nydar.co.uk/learn/iceberg-orders-spoofing) —
  конкретные правила:
  - Iceberg: 5+ fills одинакового размера (within 20% variance) на одной цене
    в коротком окне; level восстанавливается после partial fill; cumulative
    traded volume >> visible depth.
  - Spoof: крупная заявка исчезает при подходе цены; place-cancel циклы.
- **Kalena, «Crypto Level 2 Data: What the Order Book Hides»**
  (https://blog.kalena.ai/the-spoof-the-iceberg-and-the-whale-what-crypto-level-2-data-is-hiding-in-plain-sight) —
  таблица интерпретаций: «persistent bid with partial fills = genuine»,
  «large order, clean cancellation <1s = spoof», «iceberg refill pattern =
  hidden position», «one-sided cancellation surge = momentum shift».
- **dxFeed, «Iceberg Orders Detection and Prediction»**
  (https://dxfeed.com/solutions/iceberg-detection-solution/) — production-grade
  подход: synthetic iceberg = tranche если размещён в небольшом окне после
  исполнения ордера того же размера и цены; граф заказов, параметр минимальной
  длины цепочки (precision vs recall).
- **Investopedia + DEXTools** — подтверждение: «spot icebergs by observing
  repeated limit orders from the same MM»; CVD divergence filter для отличия
  пробоя от spoof-стенки.

### Уточнение под нашу страту
Наша «опорная плотность» = **genuine** order (стоит, partial fills, не
переставляется). Это **точный антипод spoof'а** и **часто iceberg** (MM
рефрешит видимый пик). Поэтому фильтр реально сводится к:

1. **Persistence score**: плотность стоит >60с без перестановки (по L2-diff).
2. **Partial-fill evidence**: на уровне есть executed trades, размер стенки
   убывает, но не до нуля (или восстанавливается = iceberg).
3. **Spoof-reject**: если level исчезает при подходе цены или «прыгает» —
   дисквалифицируем.
4. **Volume/depth mismatch**: cumulative traded volume at price >> visible
   depth → iceberg, трактуем как сильную опору.

### Вердикт
✅ Реализуем **на MEXC/Bitget** — у них фид достаточного качества для
life-cycle tracking заявки. ⚠️ Не реализуем на «настоящих tier-3» — там
стратегию вообще не стоит делать (нет верификации spoof = нет edge-контроля).
**Вывод: сужаем вселенную бирж-кандидатов с «tier-3» до «MEXC + Bitget»** —
это повышает качество фида и удовлетворяет `api-docs.mdc`, но уменьшает
количество «ёрш»-монет (компенсируется бóльшим количеством пар).

---

## Пункт 3. Исполнение

### 3а. Аварийный выход об плотность вместо стопа — противоречие в ядре

#### Замечание аудита
«Боту нужно кодировать условие выхода в среде без надёжного сигнала = де-факто
синтетический стоп, который стратегия запрещает. Либо жёсткое правило выхода
(= стоп, в дырявом стакане исполнится в ноль), либо не имеет (= застревание
до −100%).»

#### Что говорит интернет
> **Предупреждение о качестве источников.** Все источники этого пункта
> (chartscout, LedgerMind, AlphaEngine, mmgpt-pro) — SEO-блоги и GitHub-репы
> неизвестного качества, **не research-grade** (см. «Качество источников» в
> начале документа). По духу `api-docs.mdc`/`no-data-fitting.mdc` всё ниже —
> **гипотезы для проверки в Фазе 2 на собственной ленте**, а не готовые
> правила. В частности, утверждение «time-stop outperforms price-stop на
> тонких книгах» — тезис из блога, не research; Фаза 2 должна его подтвердить
> или опровергнуть измерением реального slippage time-stop-выходов.

Противоречие **частично снимается** через несколько механизмов, описанных в
блогах/репозиториях по скальпингу на тонких стаканах:

1. **Time-stop как первичный exit** (не price-stop):
   - **chartscout, «Crypto stop loss: pattern-by-pattern»**
     (https://chartscout.io/stop-loss-crypto-trading) — прямо: «Use time-based
     exits when price-based stops are unreliable. If an asset has thin order
     books across Binance, Bybit, KuCoin, MEXC — a fixed-duration exit
     ("out in 4 hours regardless") can outperform a price stop that gets
     ground out by manipulation.»
   - **AlphaEngine** (https://github.com/malaythakur/AlphaEngine) —
     `Time Exit: 180 seconds max hold` как один из exit-условий рядом с
     OBI-reversal и delta-flip.
   - **LedgerMind, «Automated Stop Loss Systems»** — time-based stops как
     отдельный класс для стратегий с определённым hold period и для
     thin-book сценариев.

   Time-stop **не требует контрагента по цене** — он закрывает по рынку в
   момент T независимо от стакана. Это не «стоп, который исполнят в ноль при
   пустом стакане» — это плановый выход по истечении срока жизни сетапа.
   Стратегия запрещает **price-stop** (потому что его выбивает в ноль); time-
   stop — другой класс, и он с стратегией **не противоречит**: «прострел
   повторяется несколько раз за сутки, ждём N минут, не выстрелило — выходим».

   ⚠️ **Критическая оговорка (не покрыта блогами):** time-stop решает только
   **проблему охоты** (намеренного выбивания price-stop'а манипуляцией), а не
   **проблему исполнения**. Market-out по таймеру в пустом стакане ест **тот
   же slippage**, что и сработавший price-stop — разница лишь в том, что
   таймер нельзя «выбить» намеренно. Поэтому:
   - стоимость time-stop-выходов (средний slippage относительно mid-price в
     момент срабатывания таймера) — **ключевая метрика Фазы 2**;
   - теоретический P&L Фазы 1 (без моделирования этого slippage) будет
     **завышен**, и на него нельзя опираться для решения full-automation;
   - если time-stop-slippage съедает целевые ~5% за круг — exit-механизм
     неработоспособен, и это выясняется **только на ленте**, не из блогов.

2. **Microstructure-signal exit** (кодированный аналог «выйти об плотность»):
   - **AlphaEngine**: exit conditions — `OBI < 45%`, `Delta Flip: Negative
     flow`, `Imbalance Reversal`. Это не цена, а состояние стакана.
   - Кодируется так: бот мониторит опорную плотность; **если она снята**
     (spoof-pull детектед) → немедленный market-out; **если прострел
     состоялся и лимитка на выход исполнена** → плановый exit; **если за
     hold-window ничего не произошло** → time-stop market-out.
   - Пороги 45%/delta-flip — из репозитория, **калибровать на своей ленте**.

3. **Density-routed limit exit вместо stop-market**: выход ставится не
   стоп-ордером, а **лимиткой на уровне ближайшей genuine density** (iceberg-
   confirmed). Это буквально кодированная версия «выйти об плотность»: бот
   не выставляет стоп, он переставляет лимитный ордер на ту стенку, которую
   детектит фильтр из п.2. Если стенку сняли — cancel + market-out.
   ⚠️ Этот механизм лучше time-stop по slippage (выход в ликвидность, а не в
   пустоту), но требует, чтобы genuine density **существовала в момент
   выхода** — то есть зависит от качества spoof/iceberg-фильтра из п.2. Если
   фильтр ошибся и плотность оказалась spoof'ом — выход деградирует в
   market-out с slippage. Качество этого выхода = качество фильтра, и
   измеряется тоже в Фазе 2.

#### Остаточный tail
Tail-сценарий (flash move без density в моменте, MM выключился) действительно
не покрывается. Решение — **kill-switch на уровне стратегии**, не позиции:
max loss per position в день, max drawdown per day 2–5% (AlphaEngine, LedgerMind).
Это **де-факто стоп**, но он срабатывает редко и находится **вне стакана**
(бот сам закрывает по рынку, а не биржа исполняет trigger). Семантически это
другое: price-stop в пустом стакане = всегда; kill-switch = только в
хвосте после того, как все остальные exit-механизмы не сработали.

#### Вердикт
⚠️ Противоречие **не разрешается полностью** — только смягчается. Time-stop
решает **охоту**, но не **исполнение**: market-out по таймеру в пустом стакане
ест тот же slippage, что и price-stop. Density-routed limit exit решает
исполнение, но только при наличии genuine density в момент выхода — то есть
зависит от качества фильтра п.2. Kill-switch покрывает tail, но это де-факто
стоп (признанное исключение). **Решающий ответ даёт только Фаза 2 измерением
реального slippage exit-механизмов на ленте** — без этого «противоречие
разрешено» будет overclaiming на базе SEO-блогов.

### 3б. Latency <100ms, VPS, rate-limits, api-docs.mdc

#### Замечание аудита
«<100ms до tier-3 = VPS рядом с их инфраструктурой, у tier-3 нет co-location,
ни стабильных гарантий. Rate-limits жёсткие и недокументированные, api-docs.mdc
почти невыполнимо.»

#### Что говорит интернет
Замечание **неверно для MEXC и Bitget** (опять же — они не настоящий tier-3):

- **Arbitron, «Crypto Exchange Server Locations & Latency Map (2026)»**
  (https://arbitron.app/learn/crypto-exchange-server-locations) — MEXC и
  Bitget оба в **AWS Tokyo (ap-northeast-1)**. Bitget ~24ms round-trip из
  Tokyo. Binance/Gate.io/KuCoin/HTX там же.
- **VoiceOfChain, «Low Latency Crypto Bot Architecture»**
  (https://voiceofchain.com/academy/low-latency-crypto-bot-architecture) —
  «Binance matching engine в AWS Tokyo; co-located VPS = 1–5ms; WebSocket =
  5–50ms; если стратегия требует реакции быстрее 500ms — нужен VPS рядом».
  Bitget: «AWS Singapore или Tokyo, test both». Подтверждает: REST 50–500ms
  (не для market data), WS 5–50ms (наш случай), home internet 20–150ms.
- **Moonbot docs** (https://moon-bot.com/ru/ufaq/what-location-should-be-selected-for-dedicated-servers-vds-for-the-bitget-exchange/) —
  Bitget-серверы в AWS Tokyo ap-northeast-1a/c; рекомендация — VDS в Tokyo.
- **mmgpt-pro/Bitget_Market_Making** (https://github.com/mmgpt-pro/Bitget_Market_Making) —
  production MM на Bitget: «co-located в Hong Kong», «rate limit optimization
  в пределах Bitget's **900 req/min limits**» (то есть лимит
  **задокументирован**), «IP whitelisting with Bitget security team».

#### Применительно к нашей страте
- Нам **не нужен sub-ms co-location**. Edge — не HFT-arms-race, а детекция
  повторяющегося паттерна. Поставить AWS Tokyo ap-northeast-1 VPS = 20–50ms
  round-trip до MEXC/Bitget. Требование «<100ms» выполнено с запасом.
- WebSocket для market data (5–50ms), REST только для placements/cancels.
- Rate-limits **документированы** (Bitget 900 req/min, MEXC — в их API docs).
  `api-docs.mdc` **выполним** — ссылки на официальные доки MEXC/Bitget можно
  класть в docstring констант.

#### Вердикт
✅ Для MEXC/Bitget замечание **снято**. ⚠️ Для настоящих tier-3 — остаётся,
но мы их уже исключили в п.2.

### 3в. MM видит твои лимитки / паттерн меняется

#### Замечание аудита
«Ты торгуешь против MM на его поле. Твои лимитки видны. Тупой MM может
перестать быть тупым, когда рядом систематически встаёт чужой объём. У бота,
делающего это сотни раз в день, — след в стакане.»

#### Что говорит интернет
Это реальная проблема, у неё есть стандартные техники снижения footprint'а:

- **HFT Book, «Order types»** (https://hftradingbook.com/microstructure/order-types) —
  Hidden orders (не видны в public feed, но matchable, теряют priority и
  maker-rebate), Post-only (всегда maker, не crosses spread), IOC для
  ping-проба hidden size без resting footprint.
- **Greeks.live, «Order Book Stealth»**
  (https://learn.greeks.live/area/order-book-stealth/) — техники:
  динамический displayed quantity, **randomizing timing of order submissions**,
  routing в dark pools/internalizers.
- **QuestDB, «Hidden Orders»** (https://questdb.com/glossary/hidden-orders/) —
  reserve orders (фиксированный visible peak + auto-replenish), fully hidden,
  minimum-quantity orders.
- **KuCoin API** (https://www.kucoin.com/docs-new/enums-definitions) —
  native hidden + iceberg order types, min visible size = 1/20 от total.
- **LeveX, «Aster Hidden Orders»** (https://levex.com/en/blog/aster-hidden-orders-explained) —
  fully concealed order placement (perp DEX, но концепт работает и на CEX с
  поддержкой).

#### Применительно к нашей страте
- На $240/сделку footprint сам по себе мал. Реальный риск — **систематическое
  повторение** на одной монете. Смягчения:
  1. **Jitter размера** (±20% от базового) — разбивает «одинаковый принт от
     нас», который MM мог бы выучить.
  2. **Jitter тайминга** внутри alert-window — не входить в ту же миллисекунду.
  3. **Iceberg на свой ордер** (если биржа поддерживает) — visible peak ≤1/20.
  4. **Rotate монет**: не торговать одну монету сотни раз; после N сделок
     переходить к следующему «ёрш»-кандидату (это уже часть стратегии —
     «реинкарнация»).
- Самое честное: если MM адаптировался (паттерн пропал после наших N сделок) —
  **это и есть сигнал выйти из монеты**, что согласуется со стратегией.

#### Вердикт
⚠️ Риск реальный, но **управляемый**: jitter + iceberg + rotation. Полностью
невидимым не станем (биржа видит наши ордера), но систематический след
размывается. Поскольку человека-исполнителя в контуре нет (см. «План» ниже),
детектор адаптации MM («паттерн пропал после наших N сделок» → rotation)
обязан быть кодированной частью exit-логики, а не наблюдением оператора.

---

## Пункт 4. Риск-менеджмент застревания не решается кодом

### Замечание аудита
«Tail (MM выключился, токен делистят, вывод заморожен) — не рыночный риск,
кодом не хеджируется. Единственная защита — размер аллокации, который готов
потерять целиком.»

### Что говорит интернет
Аудит **прав** — это не рыночный риск. Но есть набор операционных практик,
которые его bounded:

- **Changelly, «Crypto Risk Management»**
  (https://changelly.com/blog/risk-management-in-crypto-trading/) —
  multi-tier storage: hot <5% (working capital), cold storage для majority;
  «spread funds across multiple platforms, store long-term in cold wallets»;
  регулярный proof-of-reserve / regulatory / downtime check.
- **Sesamcoin, «Evaluating Crypto Exchanges»**
  (https://sesamcoin.com/evaluating-crypto-exchanges-framework-for-technical-due-diligence/) —
  «set up accounts on at least 2 exchanges in different jurisdictions»,
  «test API integration logs latency/fill/error before committing volume»,
  «test full withdrawal path with small amounts before urgent need».
- **finconduit, «MiCA Class 3 Playbook»** (https://finconduit.com/resources/mica-class3-exchange-playbook) —
  hot 1–5% AUC, warm 5–15%, cold 75–90%. Для retail-бота это транслируется
  в: «on-exchange balance = только working capital, profits sweep daily».
- **decentralised.news, «Exchange Frozen My Account»**
  (https://decentralised.news/exchange-frozen-my-account-exact-recovery-process) —
  паттерны, триггерящие AML-freeze: rapid deposit→trade→withdraw, VPN, new
  device, deposits from flagged wallets, **arbitrage / high-frequency
  transfers between platforms**. Прямо относится к нам: бот с частыми
  inter-exchange движениями = кандидат на freeze. **Mitigation**: avoid
  VPN, KYC clean, не делать rapid deposit-trade-withdraw, держать
  устоявшийся pattern активности.

### Применительно к нашей страте
Кодом не решается, но bounded следующими правилами:

1. **Allocation cap**: на одной бирже — только working capital, готовый к
   полной потере. Для депо $1000 → например $300 на MEXC, $300 на Bitget,
   $400 в cold (не в торговле). Это не «стоп-лосс на биржу», а признание
   tail-риска.
2. **Daily sweep**: прибыль и неиспользуемый остаток раз в сутки выводить
   на cold wallet / другую биржу. На-exchange баланс = только сегодняшний
   рабочий сайз.
3. **2 биржи в разных юрисдикциях** — диверсификация рискa одной площадки.
4. **Clean pattern**: не делать rapid deposit→trade→withdraw; депозит лежит,
   бот торгует, вывод — плановый, не сразу после сделки.
5. **Withdrawal-path test** раз в неделю малой суммой — чтобы знать, что
   путь работает, до того как понадобится срочно.
6. **Delist-monitoring**: бот должен проверять status инструмента
   (announcement endpoints у MEXC/Bitget) и закрывать позиции до делистинга.

### Вердикт
✅ Замечание аудита **верно по существу** (кодом не хеджируется), но
operational practices сводят tail к bounded loss = allocation cap. Это
признанная стоимость класса стратегий, не блокер.

---

## Пункт 5. Экономика бота + sample-size.mdc

### Замечание аудита
«Потолок $1000 / ~5% за круг = десятки долларов в день. Затраты: недели
разработки, VPS, поддержка ломающихся API. R&D окупается годами.
Клеццов торгует руками — машинная версия edge не валидирована. "Руками
эффективнее" = edge в человеческой адаптивности. `sample-size.mdc` ≥100
сделок на связку физически не успевает до смерти токена.»

### Что говорит интернет — sample size на умирающих инструментах
Это **ключевое** замечание, и оно требует аккуратной переинтерпретации в
рамках правил проекта:

- **LuxAlgo, Insigtrade, Memeburn, SSA Group** (общий консенсус) —
  статистическая надёжность требует **100–200 live trades**; backtest'ы
  часто misleading, нужен walk-forward / OOS на свежих данных.
- Для **короткоживущих инструментов** стандартная рекомендация —
  агрегировать на уровне **класса стратегии**, а не per-instrument, потому
  что per-instrument выборка никогда не набирается.

#### Применение `sample-size.mdc` и `no-data-fitting.mdc`
Правило `sample-size.mdc` требует «≥100 сделок по конкретной связке
(стратегия × инструмент)». Для умирающего токена это **физически невыполнимо**.
Это не повод отключать правило, а повод **корректно переопределить единицу
связки**:

- Связка = «стратегия × класс сетапа» (например, «ёрш от iceberg-плотности
  на MEXC spot, hold ≤ N мин»), а не «стратегия × конкретный токен».
- Тогда ≥100 сделок across tokens с одним и тем же классом сетапа —
  достижимо за 1–2 недели (по самому audits'у — «сотня за день или неделю»).
- **Это переопределение требует записи в `BUILDLOG.md` и явного
  обоснования** — иначе это именно то, что запрещает `no-data-fitting.mdc`
  («переинтерпретировать результаты post-hoc без записи и повторного
  прогона»). То есть: фиксируем в `BUILDLOG_YORSH.md`, что единица
  связки для этого класса — setup-class, прогоняем на одном батче токенов,
  валидируем OOS на свежем батче.

→ Замечание аудита **не блокирует стратегию**, но заставляет явно
переопределить методологию. Это **дисциплинирующее**, а не запрещающее.

### Что говорит интернет — автоматизация vs ручная торговля
- **Memeburn, «AI Trading Bots vs Human Traders 2026»** — розничная
  прибыльность: ~20% у ботов, выше у дисциплинированных людей; люди лучше в
  black-swan и regime-adaptability, боты в speed/consistency.
- **Insigtrade, «Algorithmic vs Manual 2026»** — recommended hybrid:
  «automated scanning and alerting with manual execution» — алгоритм
  смотрит 200 тикеров, шлёт alert, человек решает. «Automated execution
  with human position sizing» — другой паттерн гибрида.
- **SSA Group, LuxAlgo** — тот же консенсус: гибрид > pure automation для
  стратегий, где важна адаптивность к regime.

#### Применение к экономике
- Ceiling стратегии ~$20–50/день при депо $1000. R&D: недели разработки +
  VPS + поддержка = окупается **годами** при full-automation.
- **Разделение фаз меняет экономику**: бот-сканер делает самое дорогое
  (scan 24/7, сбор данных) — это дешёвая разработка (по п.1 — готовые
  библиотеки) и не требует торгового исполнения. R&D окупается не как
  торговый P&L, а как **research-investment**: validated edge → решение
  full-automate или нет. (Гибрид с человеком-исполнителем не
  рассматривается — пользователь руками не торгует, см. «План» ниже.)

### Вердикт
- ⚠️ **Full-automation торгового исполнения при депо $100–$1000 — не
  рекомендую.** Экономика отрицательная: dev cost >> ceiling.
- ✅ **Бот-сканер + сбор данных (без торгового исполнения) — рекомендую.**
  Это закрывает раздел 11 исходного документа, дёшево, и даёт данные для
  решения «автоматизировать ли исполнение».
- ✅ `sample-size.mdc` **выполним** на уровне setup-class (с записью в
  `BUILDLOG_YORSH.md`), не per-token.

---

## План и числовой критерий перехода к full-automation

> Пользователь торгует руками **не будет** — гибридная Фаза 3 (человек с
> приводом по alert'ам) из предыдущей версии этого документа **отменена**.
> Честная развилка после Фазы 2: либо full-automation с признанной
> отрицательной экономикой на депо $100–$1000, либо остановка на уровне
> «research pipeline + данные». Положительный результат Фазы 2 **не обязан**
> вести к торговому боту — Фазы 1–2 осмысленны сами по себе (дёшевы, отвечают
> на вопрос «существует ли паттерн вообще»).

### Фаза 1 — data collector + сканер (без торговли). 2–3 недели.
- Подключение к MEXC и Bitget WebSocket (trades + L2 diff по офиц. докам).
- **Запись полного сырого потока с первого дня** (все trades + все L2-диффы
  + периодические снапшоты, не только агрегаты). Критично: исторический
  L2-diff у бирж не купить — лента для симуляции Фазы 2 существует только
  та, которую записал наш коллектор. Без сырой записи Фаза 2 невозможна.
- Сканер «ёрш»: Hawkes/CUSUM + кластеризация принтов + repeat-frequency
  test + привязка к genuine density (через фильтр из п.2). Пороги —
  калибруются на собранной ленте, не берутся из блогов.
- Сбор в SQLite/Parquet: частота прострелов, hold time, видимый P&L **без**
  моделирования exit-slippage (помечать как upper bound).
- VPS AWS Tokyo ap-northeast-1.
- **Главная проверка Фазы 1**: подтверждение преобразованной гипотезы (см.
  «Трансформация гипотезы» в начале документа) — есть ли на MEXC/Bitget
  повторяющиеся прострелы от genuine density exploitable мелким сайзом,
  несмотря на prof MM-программы. Если нет — стратегия в этой формулировке
  закрывается без Фазы 2.

### Фаза 2 — paper-trading exit-механизма на исторической ленте. 1–2 недели.
- Симуляция exit-логики: time-stop + density-routed limit exit + spoof-pull
  cancel + kill-switch.
- **Ключевая метрика — time-stop-slippage**: средний slippage market-out'а
  относительно mid-price в момент срабатывания таймера. Без неё P&L Фазы 1 —
  upper bound, на который нельзя опираться.
- Метрики: WR, EXP, PF, средний hold, частота kill-switch, tail-loss rate,
  **net P&L после exit-slippage и комиссий**.

### Критерий перехода к Фазе 3 (full-automation) — определить ЧИСЛОМ до начала Фазы 2

Чтобы после недель разработки не сработал sunk cost и бот не поехал в лайв
на слабых данных, порог перехода фиксируется **заранее, в этом документе**, и
не пересматривается post-hoc без записи в `BUILDLOG_YORSH.md` и повторного
прогона (правило `no-data-fitting.mdc`).

Минимальный порог перехода (все условия должны выполняться одновременно):

1. **Net P&L после exit-slippage и комиссий** из Фазы 2 (на lente ≥2 недель,
   ≥100 сделок на setup-class, разные рыночные режимы) — положительный и
   статистически значимый (p-value < 0.05 против нуля; t-test или bootstrap CI).
2. **Ожидаемый дневной net P&L** ≥ **стоимость поддержки**:
   VPS Tokyo (~$10–15/мес) + амортизация dev-времени + операционный overhead
   (monitoring, reconcile, обновления под ломающиеся API). Конкретная цифра
   фиксируется в `BUILDLOG_YORSH.md` перед Фазой 2; для депо $100–$1000
  реалистичный минимум — **$30–50/день net** (иначе экономика отрицательна,
   как фиксирует сам аудит: dev cost >> ceiling).
3. **Tail-loss rate** (частота срабатываний kill-switch / застреваний) < N%
   от сделок — порог фиксируется в `BUILDLOG_YORSH.md`.
4. **WR / EXP** на setup-class — не ниже зафиксированного в `BUILDLOG_YORSH.md`
   минимума (calibrated из Фазы 1, не из блогов).

Если хотя бы одно условие не выполнено → **стратегия остаётся на уровне
research pipeline**, торговый бот не запускается. Это не неудача — это
ответ на вопрос «существует ли edge после автоматизации».

#### Честное следствие из критерия (принято осознанно)

Условие 2 (≥$30–50/день net) поставлено **на уровне или выше потолка**
стратегии (~$20–50/день в оптимистичном сценарии при депо $1000, см. п.5),
при этом депо не масштабируется — по первоисточнику стратегия умирает выше
$1000. Логическое следствие: **Фаза 3 (full-automation) практически
недостижима по построению** при текущем депо.

Это не ошибка критерия, а его смысл: Фазы 1–2 оправданы **не как путь к
торговому боту**, а как самостоятельный research:
- проверка существования паттерна «регулярный прострел от genuine density»
  на реальных данных;
- L2-collector-инфраструктура (MEXC/Bitget WS, incremental orderbook,
  spoof/iceberg-фичи) — переиспользуема для других идей проекта;
- методология работы с микроструктурой (Hawkes/CUSUM, print-кластеризация).

Если Фаза 2 неожиданно покажет net P&L сильно выше потолка — это повод
пересмотреть критерий **явно, с записью в `BUILDLOG_YORSH.md`**, а не
молча опустить порог (правило `no-data-fitting.mdc`).

### Параллельно — risk ops из п.4 (для любой live-фазы)
Allocation cap (on-exchange = только working capital), daily sweep в cold,
2 биржи в разных юрисдикциях, delist-monitoring, withdrawal-path test, clean
activity pattern (избегать rapid deposit→trade→withdraw, что триггерит AML-
freeze — см. decentralised.news в п.4).

### Соответствие правилам проекта
Гипотеза → данные → решение (`no-data-fitting.mdc`); ≥100 сделок на связку
с явным определением связки = setup-class, зафиксированным **до** сбора в
`BUILDLOG_YORSH.md` (`sample-size.mdc`); ссылки на офиц. доку MEXC/Bitget
для подключения (`api-docs.mdc`); без вливания в торговую логику существующих
ботов (`strategy-guard.mdc`); exit-пороги калибруются на своей ленте, не
импортируются из SEO-блогов.

---

## Источники (сводка)

### Сканер / pump-and-dump detection
- https://risingwave.com/blog/build-real-time-crypto-pump-dump-detector-sql/
- https://arxiv.org/html/2412.18848v1
- https://github.com/tripolskypetr/volume-anomaly
- https://github.com/punyamodi/Deep-Market-Maker

### L2-фид и spoof/iceberg
- https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/how-to-properly-maintain-a-local-copy-of-the-order-book
- https://www.mexc.com/api-docs/futures/websocket-api/incremental-order-book-maintenance-mechanism
- https://www.bitget.com/api-doc/contract/websocket/public/Order-Book-Channel
- https://www.bitget.com/api-doc/uta/sbe/sbe-depth50
- https://blog.kalena.ai/the-spoof-the-iceberg-and-the-whale-what-crypto-level-2-data-is-hiding-in-plain-sight
- https://nydar.co.uk/learn/iceberg-orders-spoofing
- https://dxfeed.com/solutions/iceberg-detection-solution/
- https://www.dextools.io/tutorials/order-book-reading-spoofing-walls-and-iceberg-orders
- https://www.investopedia.com/terms/i/icebergorder.asp

### Exit-логика / stops на тонком стакане
- https://chartscout.io/stop-loss-crypto-trading
- https://theledgermind.com/automated-stop-loss-systems/
- https://theledgermind.com/automated-crypto-scalping-strategies/
- https://github.com/malaythakur/AlphaEngine
- https://www.activtrades.com/en/news/how-to-use-volatility-based-position-sizing-when-trading-with-cfds

### Latency / VPS / exchange locations
- https://arbitron.app/learn/crypto-exchange-server-locations
- https://voiceofchain.com/academy/low-latency-crypto-bot-architecture
- https://moon-bot.com/ru/ufaq/what-location-should-be-selected-for-dedicated-servers-vds-for-the-bitget-exchange/
- https://github.com/mmgpt-pro/Bitget_Market_Making

### Stealth / hidden orders
- https://hftradingbook.com/microstructure/order-types
- https://learn.greeks.live/area/order-book-stealth/
- https://questdb.com/glossary/hidden-orders/
- https://www.kucoin.com/docs-new/enums-definitions
- https://levex.com/en/blog/aster-hidden-orders-explained

### Exchange / застревание risk
- https://changelly.com/blog/risk-management-in-crypto-trading/
- https://sesamcoin.com/evaluating-crypto-exchanges-framework-for-technical-due-diligence/
- https://finconduit.com/resources/mica-class3-exchange-playbook
- https://decentralised.news/exchange-frozen-my-account-exact-recovery-process

### Автоматизация vs ручная
- https://www.luxalgo.com/blog/ai-vs-manual-scalping-key-differences/
- https://insigtrade.com/blog/algorithmic-trading-vs-manual-trading-which-is-better
- https://memeburn.com/ai-trading-bots-vs-human-traders-what-the-data-says-in-2026/
- https://www.ssa.group/blog/manual-vs-automated-trading-which-is-better-for-you/
- https://www.quantvps.com/blog/trading-bot-strategies

---

## История документа
- 2026-07-05: первичная версия — ответы по 5 пунктам аудита реализации бота.
- 2026-07-05: правки по мета-ревью аудита:
  - добавлена «Трансформация гипотезы при сужении до MEXC/Bitget» — сужение
    подтачивает исходный edge (проф MM-программы vs «глупый MM»), Фаза 1
    проверяет именно преобразованную гипотезу;
  - добавлена явная маркировка «Качество источников» — exit-логика (п.3а)
    опирается на SEO-блоги/GitHub-репы, по духу `api-docs.mdc`/
    `no-data-fitting.mdc` это гипотезы для Фазы 2, не источник правды;
    пороги RisingWave (2%/Z≥3/0.65) — стартовая точка для калибровки, не канон;
  - п.3а: явная оговорка, что time-stop решает **охоту**, а не **исполнение**
    — market-out по таймеру ест тот же slippage, что и price-stop; стоимость
    time-stop-выходов = ключевая метрика Фазы 2, иначе P&L Фазы 1 завышен;
  - отменена гибридная Фаза 3 (пользователь руками не торгует); добавлен
    **числовой критерий перехода к full-automation**, фиксируемый заранее
    (до Фазы 2) — защита от sunk cost: net P&L после slippage статистически
    >0, ожидаемый дневной net ≥ стоимость поддержки (~$30–50/день для
    $100–$1000), tail-loss rate и WR/EXP пороги — в `BUILDLOG_YORSH.md`;
  - честная развилка: положительный результат Фазы 2 не обязан вести к
    торговому боту; Фазы 1–2 осмысленны как research pipeline.
- 2026-07-05: правки по второму мета-ревью:
  - вычищены остатки отменённого гибрида из вердиктов п.3в и п.5
    (детекция адаптации MM теперь обязана быть кодированной, не
    наблюдением оператора);
  - добавлено «Честное следствие из критерия»: условие 2 (≥$30–50/день)
    находится на уровне/выше потолка стратегии при немасштабируемом депо
    → Фаза 3 практически недостижима по построению; Фазы 1–2 оправданы
    как самостоятельный research (паттерн, инфраструктура, методология);
  - Фаза 1: явное требование записи полного сырого потока (trades +
    L2-diffs + снапшоты) с первого дня — исторический L2 у бирж не
    купить, без сырой записи Фаза 2 невозможна.
