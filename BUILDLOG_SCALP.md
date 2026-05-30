# Build Log — scalp_bot (orderflow-скальпер Bybit)

Лог нового бота `src/scalp_bot/`. Изолирован от `ai_trader`/`bybit_bot`/
`fx_pro_bot`/`fx_ai_trader` (strategy-guard.mdc). Решения детерминированные,
по микроструктуре в реалтайме, БЕЗ LLM.

## 2026-05-30

### v0.3.3 — плейбук-логи: пошаговый нарратив торговли простым языком
`<hash>`

Запрос: видеть в логах каждый этап стратегии (поиск → взвод → ожидание →
выстрел → филл → удержание → закрытие) понятным комментарием, чтобы на пальцах
понимать, где бот идёт верно, а где буксует/недо-переоценивает.

Добавлен отдельный логгер `scalp_bot.play`. Нарратив на **переходах состояний**
(не каждый тик), повторяющиеся «жду/держу» троттлятся раз в
`narrate_interval_sec` (15с):
- 🎯 ВЗВОД: свип уровня X + дивергенция CVD → цель reclaim Y, таймаут.
- ⏳ ожидание: сколько не хватает до reclaim, развернулся ли CVD.
- 💤 взвод истёк / 🔫 ВЫСТРЕЛ (reclaim+разворот, бонусы, score, уровни).
- ⛔ fee-guard отбил почти-вход (цель не покрывает комиссии).
- 📤 ставлю maker-лимитку / 📥 маркет-вход / ✅ филл / 🚫 отмена / ⌛ таймаут.
- ⏱ держу #id Nс: цена, до TP/SL. 🏁 закрыл: причина простым языком + pnl.
- 📊 раз в минуту — вердикт где «затык» воронки (нет свипов / нет дивергенции /
  взводимся но не стреляем / N входов).

**Файлы:** `src/scalp_bot/analysis/signals.py` (нарратив детектора),
`src/scalp_bot/trading/executor.py` (нарратив исполнения/сопровождения),
`src/scalp_bot/app/main.py` (плейбук-вердикт воронки),
`src/scalp_bot/config/settings.py` (`narrate_interval_sec`).

### v0.3.2 — 🔴 фикс: post-only вход с ЧУЖОЙ стороны стакана → entry_Cancelled
`<hash>`

**Симптом.** Telegram слал «🟢 open #14/#15», но на бирже позиций нет, equity
не двигался. В БД у ВСЕХ live-сделок (#10–#15) `close_reason='entry_Cancelled'`,
pnl=0, время жизни ~0.6с.

**Причина.** `build_signal` брал цену входа с ПРОТИВОПОЛОЖНОЙ стороны книги:
для LONG — `best_ask`, для SHORT — `best_bid`. Но ордер ставится как
**PostOnly** (maker). PostOnly BUY по `best_ask` мгновенно пересекает спред →
Bybit по правилу post-only его **отменяет** (не исполняет как taker,
https://bybit-exchange.github.io/docs/v5/order/create-order). Итог: ни одна
позиция реально не открывалась. Плюс уведомление «open» слалось на МОМЕНТ
ОТПРАВКИ ордера, а не на филл — вводило в заблуждение.

**Фикс.**
1. `build_signal`: для `post_only_limit` цена входа берётся по СВОЕЙ стороне
   (LONG→`best_bid`, SHORT→`best_ask`) — лимитка стоит мейкером, не пересекает
   спред. Для `market` — тейкер-референс (LONG→`best_ask`, SHORT→`best_bid`).
2. `executor`: уведомление «🟢 open» теперь шлётся ТОЛЬКО после реального филла
   (`Filled`/`PartiallyFilled` в `_manage_live`). На отправке maker-ордера —
   лёгкое «⏳ выставлена, жду филл». Market-вход уведомляет сразу (filled).

Ключи ботов проверены — свап корректен (scalp и ai_trader на разных demo-
аккаунтах). Открытых позиций на счёте scalp нет (закрывать нечего).

**Файлы:** `src/scalp_bot/analysis/signals.py` (maker-сторона входа),
`src/scalp_bot/trading/executor.py` (open-уведомление после филла),
`tests/test_scalp_bot.py` (+1 тест стороны книги).

### v0.3.1 — двухфазный детектор свип-разворота (взвод → выстрел)
`<hash>`

**Симптом.** После v0.3.0 funnel показал `SIGNALS=0` при том, что sweep,
reclaim, momentum, ob проходили часто по отдельности. Причина — фундаментальный
дефект одношаговой оценки: `sweep`+`cvd_divergence` требуют свежий **минимум**
(цена внизу), а `reclaim` требует **возврат наверх** — эти условия истинны в
РАЗНЫЕ моменты и почти никогда не совпадают в одном снимке. Бот не мог войти
структурно.

**Решение (одобрено пользователем).** Канон CAP «sweep → reclaim → CHoCH»
разнесён во времени, поэтому ловим его как **состояние**, а не один снимок:
- Фаза **ВЗВОД** (`arm`): `sweep` + `cvd_divergence` у экстремума → запоминаем
  сторону, свипнутый уровень и амплитуду прокола (`exc`).
- Фаза **ВЫСТРЕЛ** (`fire`): в течение `arm_timeout_sec` (60с по умолчанию),
  если цена сделала `reclaim` (вернулась ≥ `reclaim_frac` пути за уровень) И
  CVD развернулся (`reversal_momentum`) → вход. `ob`/`liq`/`funding` — бонус в
  `reasons`, не блокируют (в спокойном рынке они почти не печатаются).

Реализован класс `SweepReclaimDetector` (per-symbol state) в
`analysis/signals.py`. Построение сигнала (entry по книге, SL за свипнутым
уровнем + буфер, TP = `take_profit_r`×R, fee-guard) вынесено в общую
`build_signal()` — её переиспользуют и одношаговый `evaluate` (для тестов
геометрии), и детектор. В главном цикле `evaluate` заменён на per-symbol
детекторы; при открытии позиции / в open-state детектор сбрасывается
(`reset()`), чтобы не взводиться поверх позиции. Funnel расширен счётчиками
`armed` (циклов во взводе) и `FIRED` (фактических входов) — сразу видно,
доходит ли воронка до выстрела.

**Файлы:** `src/scalp_bot/analysis/signals.py` (build_signal + детектор),
`src/scalp_bot/config/settings.py` (`arm_timeout_sec`),
`src/scalp_bot/app/main.py` (детекторы + funnel armed/fired),
`tests/test_scalp_bot.py` (+4 теста две фазы), `.env.example`.

### v0.3.0 — аудит по учебникам скальпинга + фиксы (sweep-and-reclaim, liq-side, qty)
`<hash>`

Прочитаны проф-источники (реальный fetch): Bob Volman «Forex Price Action
Scalping» (2011), Bookmap/Kalena/TradingView (order-flow & CVD), ChartWhisperer
CAP 5-rule sweep-and-reclaim protocol, CrossTrade, Quantum-Algo (liquidity
sweeps), TraderSpy/Altrady/MetaMask/Yellow.com (funding/ликвидации),
LiberatedStockTrader/1minscalper/VT Markets (комиссии/риск). Сверена логика
бота, выписаны расхождения, внедрены изменения (одобрено пользователем).

**🔴 Bug-fix (инверсия семантики ликвидаций).** Офиц. дока Bybit
`all-liquidation`: поле `S` = POSITION side, `S="Buy"` = ликвидирован ЛОНГ
(forced sell, капитуляция вниз). Правило `liq_flush` для long-fade считало
`"Sell"` — инвертировано. Срабатывало на неверную сторону. Исправлено
(`signals.liq_flush`, `aggregates.LiqEvent` docstring).
https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation

**🔴 Bug-fix (Qty invalid, ErrCode 10001).** `position_size` после
`floor(qty/step)*step` давал float-артефакт `1.2000000000000002`, `str()`
улетал на биржу → reject. Добавлена квантизация `round(..., qty_decimals(step))`
+ `client.fmt_qty()` форматирует qty ровно по точности шага (защитно в
`place_entry`/`close_market`).

**Изменения стратегии (research-based, одобрены):**
- **Reclaim + разворот CVD** (CAP Rule 2 + Rule 5 / tape-shift): вход только
  после возврата цены за свипнутый уровень (≥`reclaim_frac`=0.5 пути) И когда
  CVD качнулся в сторону сделки за `momentum_window_sec`=30с. Чинит главный
  изъян — «ловлю ножа» (бот мог входить в реальный пробой). Источники: все
  sweep-гайды единогласно «не входи во время свипа, жди подтверждения».
- **TP 1.5R → 2.0R**: канон свип-разворота (CrossTrade 2:1–4:1, ChartWhisperer
  T1≈2-3R). 1.5R после комиссий давал тонкий edge.
- **Fee-guard**: сигнал отбрасывается, если ход до TP < `min_target_fee_mult`
  (3.0) × `round_trip_fee_frac` (0.00075 = maker+taker). Анти fee-trap для
  мелких целей (liberatedstocktrader/1minscalper/VT Markets: цель ≥3× издержек).
- **Активный выход (hard invalidation)**: `flow_invalidated` закрывает позицию
  раньше тайм-стопа, если CVD развернулся против (после `active_exit_min_age`
  10с). Источники: Kalena/tradezella/tradealgo «exit immediately when flow flips».
- **Funding-порог АСИММЕТРИЧНЫЙ**: short-fade при funding ≥ +0.05%, long-fade
  при ≤ −0.03% (TraderSpy/Altrady — crowded long глубже crowded short).
- **Сессионный фильтр** (опц., default OFF): только London/NY+overlap; ВЫКЛ
  чтобы не уморить частоту при строгом конфлюенсе.
- **Flatten-on-start**: при старте закрываем открытые позиции по символам +
  реконсилим зависшие open-сделки (новая логика входа/выхода, чистый лист).

Совпало с каноном и оставлено: CVD-дивергенция обязательна, направление
funding-фейда, SL за свипнутым экстремумом, maker-вход/killswitch/rate-limit.

**Файлы:** `analysis/signals.py` (reclaim/reversal_momentum/flow_invalidated/
fee-guard/liq-side/funding asym), `trading/executor.py` (active-exit, qty
квантизация), `trading/client.py` (fmt_qty), `app/main.py` (flatten-on-start,
session filter), `config/settings.py` (новые параметры), `tests/test_scalp_bot.py`
(41 тест).

### v0.2.1 — Telegram-нотификатор + переиспользование аккаунта ai_arena
`<hash>`

От удалённого ai_arena на VPS остались в `.env` отдельный demo-аккаунт
Bybit (`AI_ARENA_BYBIT_*`) и отдельный Telegram-бот (`AI_ARENA_TELEGRAM_*`).
Переиспользуем для scalp-bot:
- отдельный Bybit-аккаунт → чистый аудит PnL, не мешается с ai-trader
  (stats-collection.mdc);
- свой Telegram-бот для алертов.

Добавлен лёгкий `telegram/notifier.py` (только sendMessage, без поллинга
команд — не конфликтует с другими ботами на токене). Алерты: старт,
открытие/закрытие (PAPER и LIVE), killswitch. No-op если выключен/нет
token. На VPS `SCALP_BYBIT_*` и `SCALP_TELEGRAM_*` маппятся на
`AI_ARENA_*` через `.env` (compose их прокидывает).

**Файлы:** `telegram/notifier.py` (new), `config/settings.py`
(telegram_bot_token/chat_id), `trading/executor.py` (notify open/close),
`app/main.py` (notify старт/killswitch), `docker-compose.yml`,
`.env.example`, `tests/test_scalp_bot.py`.

### v0.2.0 — LIVE на demo по умолчанию, депо $1000, лот $10+, funding-guard
`<hash>`

По требованию пользователя: запускаем сразу на биржу (демо-счёт, риска нет),
PAPER больше НЕ дефолт.

- `trading_enabled=true` по умолчанию (LIVE на Bybit demo). PAPER остаётся
  опциональным режимом (false), но не навязывается.
- Капитал $1000; killswitch дневной $500 / совокупный $800 (буфер до
  обнуления депо); max 2 позиции; 20 сделок/час.
- Сайзинг переведён с фикс-риска на **фикс-notional**: лот $100, **минимум
  $10** (мельче — комиссия/спред съедают прибыль скальпа; пользователь
  мыслит «лотами в $»). Биржевой `minOrderQty` уважается.
- **Учёт комиссий**: LIVE-PnL = Bybit `closedPnl` (net, уже после maker/taker
  fee). Вход post-only maker (0.02%) дешевле taker (0.055%).
- **Funding-guard**: Bybit списывает/начисляет funding раз в 8ч
  (00:00/08:00/16:00 UTC) по открытой позиции. Для 90-сек скальпа почти не
  задевает, но бот НЕ открывает позиции в окне `avoid_funding_window_sec`
  (120с) перед списанием — funding-cost исключён полностью.

**Файлы:** `config/settings.py` (position_usd/min_position_usd, trading_enabled
default true, kill $500/$800, avoid_funding_window_sec), `trading/executor.py`
(position_size по notional + min-floor), `app/main.py` (funding-окно,
sec_to_next_funding), `docker-compose.yml`, `.env.example`, `tests/test_scalp_bot.py`.

### v0.1.0 — каркас orderflow-скальпера
`<hash>`

Новый отдельный бот по скальпингу в собственном Docker-контейнере
(`scalp-bot`, volume `scalp_bot_data`, env-namespace `SCALP_*`). Причина:
бэктесты свечного подхода (1H/15m/5m) показали отсутствие edge на
скальп-таймфреймах — для скальпа нужна микроструктура, которой у
`ai_trader` нет (см. чат «тупик свечной страты», BUILDLOG_AI_TRADER).

**Архитектура (rule-based, без LLM):**
- Данные: Bybit public WS (`publicTrade`→CVD, `orderbook.50`→imbalance,
  `tickers`→funding/OI, `allLiquidation`→каскады). Все потоки бесплатны и
  официальны (api-docs.mdc). Coinglass heatmap отвергнут — платный $699/мес,
  бесплатный план heatmap не даёт; Bybit `allLiquidation` отдаёт реальные
  ликвидации бесплатно (push 500ms).
- Сигнал: «свип ликвидности + поглощение» (mean-reversion fade). 5 микро-
  правил, CVD-дивергенция обязательна + ≥3/5 конфлюенс:
  1) sweep (свежий экстремум), 2) cvd_div [обяз.], 3) liq_flush,
  4) funding-перекос толпы против сделки, 5) ob_imbalance.
- Risk: фикс-риск $5/сделка (1% от $500, Van Tharp), плечо 5x, killswitch
  (дневной $50 / совокупный $150 / max 2 позиции / 20 сделок/час).
- Исполнение: post-only LIMIT вход (maker 0.02% вместо taker 0.055% —
  round-trip taker съедает 10-20% цели скальпа), reduce-only MARKET выход
  по тайм-стопу 90с; TP 1.5R, SL за свипнутым уровнем + 8 б.п.
- Режимы: PAPER (default, ордера симулируются на live-цене с учётом
  модельных комиссий) / LIVE на demo (флаг `SCALP_TRADING_ENABLED`).

**Валидация:** orderflow почти не бэктестится (нет дешёвой истории L2),
поэтому edge проверяется forward-тестом на **demo-счёте** (риска нет).
Набор ≥100 сделок, анализ WR/expectancy с учётом комиссий (sample-size.mdc)
до любых выводов об отключении/тюнинге. На реальные деньги — отдельное
решение пользователя после подтверждённого положительного expectancy.

**Smoke:** живой WS-коннект к Bybit подтверждён — приходят сделки (CVD),
стакан (imbalance), funding по BTC/ETH/SOL. 29 юнит-тестов зелёные
(сигналы, агрегаты, sizing, killswitch).

**Файлы:** `src/scalp_bot/` (config/settings, data/aggregates+market_stream,
analysis/signals, trading/client+executor, safety/killswitch, state/db,
app/main), `Dockerfile.scalp-bot`, `docker-compose.yml` (сервис scalp-bot
+ volume scalp_bot_data), `.env.example`, `pyproject.toml` (пакет+скрипт),
`tests/test_scalp_bot.py`.
