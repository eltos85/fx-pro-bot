# BUILDLOG — FX AI Trader (DeepSeek-V4 на cTrader FxPro: gold + Brent oil + Natural Gas)

## 2026-05-18 (вечер) — feat: добавлен NG=F (Natural Gas, NAT.GAS / Henry Hub)

`коммит при deploy`

**Что.** Discretionary бот теперь следит и торгует gold + Brent +
**natural gas** (NG=F → cTrader NAT.GAS id=1118 на FxPro demo).
По правилу `no-data-fitting.mdc` это instrument-add, не стратегическое
изменение — экспериментальный n=0 счётчик прошлого forward-test'а
**не сбрасывается** (стратегические thresholds и правила оставлены
один к одному, см. prompts.py v1.1 docstring).

**Разведка.**
- Новый скрипт `scripts/fx_ai_scout_gas_symbols.py` — однократно
  запущен на VPS, дампит ProtoOASymbol для всех инструментов с gas-
  keywords (NAT, GAS, NG, TTF, HENRY).
- На FxPro demo доступен **только NAT.GAS** (NG / Henry Hub).
  TTF (европейский Dutch front-month) **отсутствует** — торгуем
  только US-bench.
- Ещё 8 инструментов `#NGas_*26` — это месячные futures, ненужны
  для CFD-стратегии.

**Pip-value research (правило `no-data-fitting.mdc`, ≥2 confirmation).**
1. CME NYMEX Henry Hub Natural Gas Futures canonical spec:
   contract size 10 000 MMBtu, minimum tick $0.001/MMBtu = **$10/tick**.
2. cTrader Open API ProtoOASymbol(id=1118, NAT.GAS, ctid=46883073):
   `digits=3`, `pipPosition=3`, `lotSize=1_000_000`,
   `swapLong=-$11.11/3d`, `swapShort=+$1.81/3d` (contango carry).
   pip-value = `(10^-pipPosition) × (lotSize/100)` = `0.001 × 10_000` =
   **$10/pip/lot**.
3. Sanity: на 0.01 lot pip-value = $0.10/pip — идентично BRENT.
   1-lot $0.10 movement = 100 pips × $10 = $1000 PnL.

**Код.**
- `src/fx_ai_trader/trading/executor.py`:
  - `_pip_size_for("NG=F") = 0.001` (digits=3).
  - `_PIP_VALUE_USD_PER_STD_LOT["NG=F"] = 10.0` (с research-блоком
    в комментариях).
- `src/fx_ai_trader/config/settings.py`:
  - `DEFAULT_AI_FX_SYMBOLS = ("XAUUSD", "BZ=F", "NG=F")`.
- `src/fx_ai_trader/llm/prompts.py` (v1.1):
  - Header: «You trade ONLY three instruments» + NAT.GAS contract spec.
  - Новая секция «NATURAL GAS — STORAGE / WEATHER / LNG FRAMEWORK»
    (5 драйверов: storage cycle, weather HDD/CDD, LNG exports,
    production / rig count, geopolitics; mistakes-to-avoid block).
  - Noise-band sizing: NG standard $0.10–0.20, EIA Thu $0.20–0.40,
    cold-snap $0.50–1.00+/MMBtu.
  - Worked sizing examples: NG entry 3.250 / SL 3.100 / 0.017 lot для
    risk $25; WARN на 50-pip stops (inside hourly noise).
  - Trading windows: добавлены Thu 14:30 UTC (EIA NG storage) + Fri
    16:00 UTC (Baker Hughes rigs).
  - JSON schema: `"symbol": "XAUUSD" | "BZ=F" | "NG=F"`.
  - Review prompt: добавлено NAT.GAS в шапку.
- `src/fx_ai_trader/news/rss.py`:
  - `GAS_KEYWORDS` (storage, EIA, NOAA, HDD/CDD, LNG terminals,
    rig count, Henry Hub, TTF, pipeline outages).
  - `SYMBOL_KEYWORDS["NG=F"] = GAS_KEYWORDS`.
- `src/fx_ai_trader/news/eia.py`:
  - `_SERIES_NG_STORAGE = "NG.NW2_EPG0_SWO_R48_BCF.W"` (Weekly Working
    Underground Storage, Lower 48, Bcf — headline EIA Thursday).
  - `EiaSnapshot.ng_storage_*` поля + format_eia_snapshot печатает
    отдельный «EIA Weekly Natural Gas (Thursday update)» блок.
- `docker-compose.yml`: default `AI_FX_TRADER_SYMBOLS` →
  `XAUUSD,BZ=F,NG=F`.

**Тесты (13 новых, все зелёные).**
- `TestPipValueTable`:
  - `test_ng_pip_value_is_10usd_per_lot`.
  - `test_ng_pip_size_is_0_001`.
  - `test_ng_pnl_canonical` (0.10 lot, $0.10 move = ~$100).
  - `test_ng_short_pnl` (0.05 lot SHORT, $0.10 move = ~$50).
- `TestRssGasClassification`:
  - `test_ng_storage_headline_matched` (EIA storage report → NG=F).
  - `test_lng_terminal_headline_matched` (Freeport LNG outage → NG=F).
  - `test_weather_forecast_headline_matched` (NOAA polar vortex → NG=F).
  - `test_oil_headline_not_classified_as_gas` (false-positive guard).
- `TestSettings.test_defaults`: ожидание обновлено на
  `("XAUUSD", "BZ=F", "NG=F")`.

Полная регрессия `tests/test_fx_ai_trader.py + test_ctrader_token_service.py`
74 passed.

**Что _не_ менялось** (важно для n-counter): R:R/risk-budget rules,
sentiment-uncertainty gate (0.7), max-positions (3), max-lot-size
(0.50), KillSwitch caps, paper/live mode flags — все одинаковы.

**Файлы.**
- `src/fx_ai_trader/trading/executor.py`
- `src/fx_ai_trader/config/settings.py`
- `src/fx_ai_trader/llm/prompts.py`
- `src/fx_ai_trader/news/rss.py`
- `src/fx_ai_trader/news/eia.py`
- `docker-compose.yml`
- `scripts/fx_ai_scout_gas_symbols.py` (new)
- `tests/test_fx_ai_trader.py`
- `BUILDLOG_AI_FX_TRADER.md`

---

## 2026-05-18 (ночь) — fix: max_tokens regression + truncation-guard

`коммит при deploy`

**Симптом.**
```
10:11:23 LLM tokens: in=1460 out=4096 cost=$0.00135
10:11:23 [ERROR] Parse error: JSON parse error: not a decision dict (missing 'action'): dict
```

**Причина.** `AI_FX_TRADER_DEEPSEEK_MAX_TOKENS` в `docker-compose.yml`
стоял default `4096` — регрессия с ранней эпохи бота. В коде
`settings.py` default `8000` с явным комментарием «С 4096 наблюдался
out=4096 и оборванный JSON». LLM упёрся в потолок, JSON-блок
с `"action"` не дописался. Парсер `_extract_last_json_object` идёт
с конца и нашёл валидный `{...}` обрубок (sentiment-блок или часть
рассуждения), но без ключа `"action"` → ошибка.

Full-cycle output состоит из: thinking-блок (DeepSeek-V4 reasoning)
+ commentary (4–8 строк) + JSON с multi-dim sentiment (5 measures ×
N items) + decision. С двумя инструментами это легко 5–7k токенов.

**Фикс.**
1. `docker-compose.yml`: default поднят `4096 → 8192` (hard cap у
   DeepSeek Anthropic-compat API).
2. `src/fx_ai_trader/app/main.py`: добавлен **truncation-guard** в
   `_run_full_cycle` и `_run_review_cycle`. Если `tokens_output >=
   max_tokens - 16` → бот логирует `WARNING` (видно регрессию), пишет
   `error=llm_truncated_at_max_tokens` в БД, **не парсит** broken
   payload (избегаем ложного `Parse error`). Цикл пропускается, LLM
   попробует снова на следующем тике.

Truncation-guard также защитит от будущих случаев: если LLM
по какой-то причине станет жадным до токенов, проблема будет
**сразу** видна в логах, а не маскироваться под parse-error.

**Файлы:** `docker-compose.yml`, `src/fx_ai_trader/app/main.py`.

---

## 2026-05-18 (вечер) — refactor: убран local-mirror, сервис = single source

`коммит при deploy`

После реализации token-service (см. предыдущую запись) обнаружилась
концептуальная ошибка: бот всё ещё писал token-копию в локальный
`ctrader_tokens_ai_fx.json` ради paranoid-fallback. Это **возвращало**
split-brain (две rotation chains на одном аккаунте) — именно то от
чего token-service спасает.

**Фикс.** `ensure_valid_token_race_safe()` и `save_refreshed_token()`
больше не пишут в локальный файл когда `CTRADER_TOKEN_SERVICE_URL`
задан — сервис единственный owner. Файл создаётся **только** как
fallback если push в сервис провалился (защита от потери single-use
refresh_token при downtime сервиса).

`AI_FX_TRADER_CTRADER_TOKEN_PATH` default переключён на
`/data/ctrader_tokens.json` (общий файл с Advisor) — используется
только в fallback-режиме.

**Файлы:** `src/fx_ai_trader/trading/token_lock.py`, `docker-compose.yml`,
тесты `test_save_refreshed_token_skips_file_when_service_accepts_push`
+ `test_save_refreshed_token_falls_back_to_file_when_service_down`.

---

## 2026-05-18 — feat: централизованный ctrader-token-service

`коммит при deploy`

**Симптом.** fx-ai-trader попадал в петлю
`ConnectionError: cTrader: token refreshed, reconnect required`
каждые ~15 минут (после 5 reconnect-failures клиент уходит в backoff
delays=[5,10,20,30,60]), при этом Advisor работал стабильно. Логи
показывали LLM-вызовы на **пустых** market-data (`get_trendbars(...)
failed: cTrader: нет подключения`), что приводило к тратe DeepSeek
tokens впустую.

**Причина.** Архитектурная: оба бота используют один cTrader demo-аккаунт
(`ctid=46883073`), но **разные** token-файлы (`/data/ctrader_tokens.json`
у Advisor vs `/data/ctrader_tokens_ai_fx.json` у fx-ai-trader,
управляется `AI_FX_TRADER_CTRADER_TOKEN_PATH`). cTrader OAuth
использует rotating refresh_tokens — каждый refresh инвалидирует
предыдущий. Два независимых rotation chain-а на одном аккаунте =
Spotware silent-rotation отстреливает сессию того, чей токен «отстал».

Существующий `fx_ai_trader.trading.token_lock.flock` защищал от
concurrent file-write **внутри одного файла**, но не от того, что
файлы **разные**. Это был не race condition — это был split-brain.

**Решение.** Новый микросервис `ctrader-token-service`
(см. `BUILDLOG.md 2026-05-18` для общих деталей). Для fx-ai-trader
конкретно:

- `ensure_valid_token_race_safe()` теперь сначала пробует HTTP-fetch
  у сервиса. flock-путь остаётся как fallback, если сервис недоступен.
- `save_refreshed_token()` (callback для `CTraderClient.on_token_refreshed`)
  сначала пушит токен в сервис, потом зеркалирует в локальный файл.
- `CTraderClient._try_refresh_token()` при silent rotation сначала
  `GET /token` у сервиса (другой бот мог уже обновить — берём готовый),
  затем `POST /refresh` с dedup-окном 5с (защита от burst-запросов
  обоих ботов в одну секунду). Локальный refresh — только fallback.

ENV-переменные (одинаковые для Advisor и fx-ai-trader, оба сервиса
ходят к общему контейнеру):
- `CTRADER_TOKEN_SERVICE_URL=http://ctrader-token-service:8080`
- `CTRADER_TOKEN_SERVICE_SECRET=...` (HTTP-Bearer)

Backward-compat: если URL/SECRET пустые — fx-ai-trader работает по
старому через flock-файл. Это позволяет постепенный rollout.

**Тесты.** Покрыты scenarios «service выдал более свежий токен —
клиент его взял без refresh», «service вернул тот же — клиент дёрнул
force_refresh с dedup», «service down — fallback на flock-путь»,
«race_safe → mirror в файл», см. `tests/test_ctrader_token_service.py`
(24 теста). Полный suite 897 passed, без регрессий.

**Файлы:**
- `src/fx_ai_trader/trading/token_lock.py` — service-first +
  `_push_to_service` helper
- `src/fx_pro_bot/trading/client.py` — `_try_refresh_via_service` (общий
  для обоих ботов, fx-ai-trader тоже использует `CTraderClient` через
  `client_adapter.py`)
- `docker-compose.yml` — `depends_on: ctrader-token-service:
  service_healthy`, env vars

---

## 2026-05-13 (вечер) — bug-fix: broker-side reconcile (stale live-позиции)

`коммит при deploy`

**Симптом.** Позиция id=3 (BRENT BUY 0.01 lot @ 105.031, opened 15:20 UTC)
в нашей БД до сих пор `closed_at=None, exit_price=None,
realized_pnl_usd=None`, при том что **cTrader давно её закрыл по
SL=104.7** (deal_id=331875628, exit $104.721, broker gross −$3.32,
balance после $423.12).

В период 16:02 → 16:44 UTC (9 циклов подряд) LLM правильно решал CLOSE
(setup invalidated: цена ниже SL, MACD bearish flip, EMA20 пробит),
но получал от cTrader:
```
err=broker close_failed: cTrader error POSITION_NOT_FOUND: Position
not found with id 150428404
```
→ бот не записывал close в БД → следующий цикл LLM снова видел
"открытую" позицию → опять CLOSE → опять 404. Бесконечный фантом.

**Root cause.** `_apply_close()` опирался **только на локальную БД**
(`store.get_open_positions()`), без проверки broker-side активности.
Когда cTrader сам закрывал позицию по SL/TP (нормальный механизм
broker-side execution для серверных SL/TP), бот не дёргал ни
`client.reconcile()`, ни `client.get_deal_list()` для синхронизации.

**Финансовое последствие.** `realized_pnl_usd` broker-закрытых позиций
(в т.ч. убыточных по SL) **НЕ попадал в `daily_pnl`** → KillSwitch
`max_daily_loss_usd` видел $0 вместо реальных потерь. На demo —
косметика, на live — финансовая дыра.

**Fix.** Two-pronged sync на ровне с paper-reconcile:

1. **`adapter.get_active_broker_position_ids()`** — обёртка над
   `client.reconcile()`, возвращает set активных broker-pid с нашим
   `label='ai-fx-trader'`. Returns `None` при API-error (не пустой
   set! правило `None != []` из Bybit-агента 2026-05-07).
2. **`adapter.get_closing_deal_for_position(broker_pid, lookback_h)`**
   — обёртка над `ProtoOADealListReq`, возвращает dict с broker-true
   `exit_price`, `gross_pnl_usd`, `swap_usd`, `commission_usd`
   из `ProtoOAClosePositionDetail`.
3. **Новый модуль `broker_reconcile.py`**: `reconcile_broker_positions()`.
   Для каждой live-позиции в БД, отсутствующей в active broker set →
   подтягивает closing deal → пишет в БД `closed_at, exit_price,
   realized_pnl_usd = gross + swap + commission, close_reason='broker_auto'`.
   Вызывается в начале каждого full + review цикла, **сразу после**
   `reconcile_paper_positions()`.
4. **POSITION_NOT_FOUND recovery в `_apply_close`**: если LLM
   решила CLOSE и broker вернул POSITION_NOT_FOUND — executor пытается
   достать closing deal и записать broker-true PnL вместо ошибки.
   Это страховка на случай если main-loop reconcile ещё не успел
   отработать между SL-fire и LLM-CLOSE-decision.

**Источники / docs.**
- cTrader Open API `ProtoOAReconcileReq` / `Res` — list open positions:
  https://help.ctrader.com/open-api/messages/#protooareconcilereq
- `ProtoOADealListReq` / `ProtoOAClosePositionDetail` —
  `grossProfit`, `swap`, `commission` per `moneyDigits` divisor:
  https://help.ctrader.com/open-api/model-messages/#protooaclosepositiondetail
- Reuse Advisor pattern: `src/fx_pro_bot/trading/client.py:494,505` —
  готовые методы `get_unrealized_pnl()` и `get_deal_list()`.
- Polling-based reconcile pattern (не realtime event-stream) —
  стандарт для async OCO/SL/TP cleanup в retail trading bots
  (см. Bybit Two-Phase Commit аналог в `BUILDLOG_BYBIT.md`).

**Файлы:**
- `src/fx_ai_trader/trading/client_adapter.py` — +2 метода.
- `src/fx_ai_trader/trading/broker_reconcile.py` — новый модуль.
- `src/fx_ai_trader/trading/executor.py` — POSITION_NOT_FOUND recovery.
- `src/fx_ai_trader/app/main.py` — `reconcile_broker_positions()` в
  full и review циклах.
- `tests/test_fx_ai_trader.py` — `TestBrokerReconcile` (6 кейсов:
  closes broker-closed pos, skips active, no-op on API down, no-op
  if deal not found, broker-net != our_calc, apply_close recovers
  from POSITION_NOT_FOUND).
- `scripts/fx_ai_inspect_position_3.py` — read-only diag.

**Тесты.** 44/44 fx_ai зелёные, 552/552 общих pass. Особенно важные:
- `test_no_op_when_broker_api_unreachable` — гарантирует что при
  сетевом сбое мы **НЕ закрываем все позиции как фантомные**.
- `test_uses_broker_net_pnl_not_local_calc` — broker gross +$92.82
  пишется в БД, не our_formula +$101.53 (см. предыдущий BACKLOG
  item «idealized PnL»).

**Эффект после deploy.**
- Текущая stale id=3 закроется автоматически в первом full-cycle
  после рестарта контейнера (broker reconcile → deal 331875628 →
  exit $104.721, PnL −$3.32, close_reason='broker_auto').
- `daily_pnl` обновится с реальным убытком.
- LLM перестанет видеть фантомную позицию в context → review-циклы
  перестанут спамить POSITION_NOT_FOUND.

---

## 2026-05-13 (день) — broker-side verification PnL формулы (XAUUSD + BRENT)

`коммит при deploy`

**Контекст.** После bug-fix BRENT pip-value (10×) пользователь попросил
прямое подтверждение что **обе** формулы (XAUUSD $1/pip/lot, BRENT
$10/pip/lot) совпадают с тем что считает сам брокер, а не косвенное
через сравнение с Advisor / RoboForex spec.

**Что сделано.** Создан read-only скрипт
`scripts/fx_ai_verify_pnl_from_history.py`, который:

1. Дёргает `ProtoOADealListReq` через `CTraderFxAdapter` за последние
   48 часов (read-only, побочных эффектов нет).
2. Для каждого закрытого XAUUSD / BRENT deal берёт `grossProfit` из
   `ProtoOAClosePositionDetail` — это **ground truth от cTrader-бэкенда**
   (целое × 10^moneyDigits, точный расчёт их движка).
3. Параллельно считает наш PnL через `_calc_pnl_usd(side, entry, exit,
   volume_lots, symbol)` с теми же entry/exit/volume что у брокера.
4. Сравнивает.

**Результат (5 реальных сделок за 48h, ctid 46883073, ALL deltas $0.0000):**

| Deal ID | Symbol | Side | Lots | Entry → Exit | Broker gross | Наш расчёт | Δ |
|---|---|---|---|---|---|---|---|
| 331862418 | XAUUSD | BUY | 0.07 | 4701.19→4696.34 | −$33.95 | −$33.95 | $0.0000 |
| 331862269 | BRENT | BUY | 0.13 | 104.864→105.578 | +$92.82 | +$92.82 | $0.0000 |
| 331861259 | XAUUSD | BUY | 0.07 | 4703.81→4703.96 | +$1.05 | +$1.05 | $0.0000 |
| 331797394 | XAUUSD | SELL | 0.06 | 4692.60→4701.52 | −$53.52 | −$53.52 | $0.0000 |
| 331796613 | XAUUSD | SELL | 0.06 | 4693.41→4690.80 | +$15.66 | +$15.66 | $0.0000 |

**Σ |Δ| = $0.0000** на 4 XAUUSD сделках (BUY и SELL) и 1 BRENT сделке.
Формула fx-ai-trader **бит-в-бит** воспроизводит то что считает cTrader
backend. pip_value математика верна для обоих инструментов.

**Бонус.** Помнишь "discrepancy" $101.53 vs $92.82 по BRENT
(BACKLOG-item ниже)? Бот в логах писал idealized PnL с `current_price`
($105.605), broker реально закрыл по `executionPrice` $105.578. На
broker'ской exit-цене ($105.578) наша формула даёт **точно** $92.82.
Это подтверждает что разница — чисто архитектурная (current_price ≠
fill_price), а не баг в pip_value.

**Файлы:**
- `scripts/fx_ai_verify_pnl_from_history.py` — read-only verification.

**Источники.** cTrader Open API:
- `ProtoOADealListReq` — официальный endpoint для истории deals.
- `ProtoOAClosePositionDetail.grossProfit` — broker-side точный PnL.
- `compare_stats.py` (Advisor) — эталонный pattern декодирования
  `grossProfit / 10^moneyDigits`.

**Категория.** Documentation / verification. Никаких изменений в торговую
логику — формула уже была корректной (bug-fix BRENT pip-value 13-May
закрыл единственный известный дефект).

---

## BACKLOG (отложено до релиза)

### Использовать broker-reported PnL / fill_price из ProtoOAExecutionEvent

**Проблема (обнаружено 2026-05-13).** На LIVE-close бот пишет в БД
**idealized** PnL: использует `current_price` из last M1 close как
exit_price и считает PnL формулой. Реальный fill на брокере хуже на
slippage (≈5-10 pip на BRENT для market sell). Пример:
позиция id=2 BRENT — наш расчёт `+$101.53` (exit $105.605), реально
у брокера `+$92.82` (fill ~$105.538). Дельта $8.71 = slippage 6.7 pip +
commission.

**Почему важно перед релизом.**
- Статистика бота врёт в нашу пользу (overestimate winners, underestimate
  losers). Forward-test метрики (Sharpe, max DD, expectancy) искажены.
- **KillSwitch daily/total_loss считает по нашему PnL, не broker's**.
  При накапливании потерь бот может не остановиться вовремя.
- На demo это косметика, на live — реальные деньги.

**Что нужно сделать.**
1. `adapter.close_position()` → ждать `ProtoOAExecutionEvent` с
   `executionType=ORDER_FILLED` для close, вернуть `OrderResult` с
   реальным `fill_price` и `realized_pnl_usd` от broker.
   В cTrader execution event есть `closePositionDetail.grossProfit`
   и `closePositionDetail.commission` — оба нужны.
2. `executor._apply_close()` → использовать broker-данные вместо
   `current_price` / `_calc_pnl_usd()`.
3. На paper-mode — оставить локальный расчёт (там нет broker'a).
4. Аналогично для open — сохранять реальный `fill_price` от execution
   event, не quote (сейчас уже частично делается, но нужно сверить).

**Источники / docs.**
- cTrader Open API ProtoOAExecutionEvent:
  https://help.ctrader.com/open-api/model-messages/#protooaexecutionevent
- ClosePositionDetail fields: grossProfit, commission, swap, balance.

**Эффорт.** ~2-3 часа: модификация adapter, тесты на mock execution
event, документация в коде.

**Категория.** Не блокирует Phase 1 (paper observation + demo live),
блокирует Phase 2 (production live).

---

## 2026-05-13 (день) — bug-fix: pip-value для BRENT занижен в 10×

`коммит при deploy`

**Симптом.** Пользователь видел в FxPro cTrader-приложении floating PnL
по позиции id=2 (BUY 0.13 lot BRENT @ 104.824) **$39**, в то время как
бот в reviews писал «profit ~0.2R» и не срабатывал locked-profit guard
(≥1.5R). При profit-per-pip $10 на 1 lot и 0.13 lot = $1.30/pip, move
30 pip = $39 floating. По старой формуле бот считал $1/pip/lot =
$0.13/pip × 30 = $3.9 — **в 10× меньше**.

**Root cause.** В `executor.py` функция `_pip_value_per_std_lot()`
возвращала hardcoded `$1.0` для всех символов, включая BRENT. Это
было сделано как «Phase 1 baseline» (паттерн docstring так и говорил
«уточняется при paper-observation»). Реальное значение для FxPro
BRENT — **$10/pip/lot**, потому что 1 std lot BRENT = 1000 barrels
(canonical ICE/RoboForex/FxPro spec), а pip = $0.01/barrel.

**Источники** (правило `no-data-fitting.mdc` — ≥2 confirmation):

1. **ICE Brent Crude Futures** (canonical worldwide):
   theice.com/products/219 — contract size **1000 barrels**, minimum
   fluctuation **$0.01/barrel = $10/contract**.
2. **RoboForex Spot Brent Pro spec page**:
   https://roboforex.com/forex-trading/trading/specifications/card/pro-stan/BRENT/
   — «1 Pip Size = 0.01, Size of 1 lot = 1000 barrels, term currency
   = USD». Подтверждает что retail-broker'ы тоже следуют ICE-стандарту.
3. **Эмпирическое подтверждение** на FxPro demo (ctid=46883073,
   2026-05-13): позиция id=2 BUY 0.13 lot @ 104.824, move ≈30 pip до
   ≈105.12. Floating PnL в cTrader-приложении = **$39**. Расчёт:
   30 × 0.13 × $10 = $39.0 ✓ (со старой формулой было бы $3.9).

**Влияние bug'а на торговую логику.**

LLM получал в context'е заниженные `risk_usd` / `R-multiple` / paper-PnL:

| Метрика | Что видел LLM (×$1) | Реально на FxPro (×$10) |
|---|---|---|
| risk на текущую позицию | $17 | $172 |
| profit floating при move 30 pip | $4 | $39 |
| R-multiple при +$39 floating | 0.23R | 0.23R (только знаменатель другой) |
| TP-hit profit | $26 | $258 |

R-multiple сам по себе **верный** (числитель и знаменатель множатся
на одинаковый фактор и сокращаются). Поэтому locked-profit guard
(≥1.5R) **формально** работал. Но:

- **Sizing был неправильный**. LLM думал что 0.13 lot = $17 risk
  (2.8% от $500 virtual_capital), реально — $172 risk (34%).
- **KillSwitch daily/total cap не учитывал реальный risk**. При
  daily_loss=$150 одна потеря по реальной формуле ($172) превышает
  cap одной сделкой.
- **Прогнозируемый TP-profit в логах** искажён в 10×.

**Что делает фикс.**

- `_pip_value_per_std_lot()` теперь словарь per-symbol:
  - `XAUUSD` = $1.0/pip/lot (canonical 100 oz × $0.01).
  - `BZ=F`   = $10.0/pip/lot (canonical 1000 barrels × $0.01).
  - fallback $1.0 для незнакомых символов (поведение pre-fix).
- В промпт добавлены явные worked sizing examples — LLM теперь видит
  что для BRENT pip-value в 10× больше, и подгоняет lots-formula.
- При старте бот логирует фактический pip-value по каждому символу
  (`pip_size=0.0100, pip_value=$10.00/pip/lot`) — для будущей
  верификации.

**Влияние на текущую открытую позицию id=2.**

- SL/TP в cTrader — абсолютные цены, **остаются на брокере как есть**.
- В нашей БД `volume_lots / entry_price` остаются как есть.
- Только пересчёт R-multiple для LLM-reviews изменится (теперь будет
  правильный). Текущие +$39 floating ≈ +0.23R — всё ещё ниже 1.5R
  trigger, бот продолжит держать.

**Категория.** Bug-fix критичный (не curve-fitting): корректируем
расчёт под реальную работу брокера, основано на canonical spec + 2
дополнительных confirmation source'ов. По правилу `no-data-fitting.mdc`
— допустимая правка, эксперимент n=0 не сбрасывается.

**XAUUSD pip-value тоже проверен** (без правки — формула была верной):
Advisor открыл XAUUSD 0.07 lot через стратегию `gold_orb`
(broker_position_id=150420246, entry $4702.33). Эта позиция стала
независимым test-vehicle для проверки нашей формулы $1/pip/lot. При
current_price $4695.71 наш расчёт даёт PnL = (4695.71-4702.33) ×
0.07 × $100 = -$46.34. Реально в cTrader-приложении floating PnL =
**-$48**. Дельта $1.66 объясняется spread'ом FxPro (~0.5 pip × 7 oz)
и lag'ом current_price в Advisor-БД (1-2 минуты от last poll).
Источники: RoboForex Pro spec для XAUUSD (1 lot=100 oz, pip=0.01,
USD), FxPro contract specs, LBMA canonical. Не правим — спецификация
универсальна для всех brokeров на spot gold.

**Открытый вопрос для пользователя.** При новой формуле KillSwitch
caps (daily=$150, total=$300) становятся слишком тесными:
- 1 потеря на 0.13 lot BRENT с SL distance 132 pip = -$172 → выходит
  за daily-cap одной сделкой.
- Нужно поднять daily_loss / total_loss пропорционально новому
  пониманию реального риска (обсуждается отдельно).

**Файлы:**
- `src/fx_ai_trader/trading/executor.py` — словарь
  `_PIP_VALUE_USD_PER_STD_LOT` с per-symbol значениями + 3-source citation.
- `src/fx_ai_trader/trading/client_adapter.py` — логирование
  pip-value при resolve symbols.
- `src/fx_ai_trader/llm/prompts.py` — обновлены worked sizing examples
  с правильным $10/pip/lot для BRENT.
- `tests/test_fx_ai_trader.py` — новый `TestPipValueTable` (5 тестов,
  включая эмпирический $39).
- `scripts/fx_ai_inspect_symbols.py` — diag-script для опроса cTrader
  ProtoOASymbol (read-only). Не используется при работе бота, только
  для ручной верификации.

---

## 2026-05-13 (утро) — bug-fix: clamp out-of-range sentiment vs hard-reject

`коммит при deploy`

**Симптом.** Cycle 03:50 UTC:
```
[ERROR] fx_ai_trader: Parse error: schema validation error:
  [{'type': 'greater_than_equal',
    'loc': ('sentiment', 'items', 2, 'forwardness'),
    'msg': 'Input should be greater than or equal to 0',
    'input': -0.3, 'ctx': {'ge': 0.0}}]
```

LLM прислал `forwardness=-0.3` для 3-й новости. Pydantic `Field(ge=0.0,
le=1.0)` отвергнул **всё** решение целиком — потеряли decision (включая
core: open / close / hold).

**Root cause.** LLM путает `forwardness` (∈ [0, 1], где 0 = backward-
looking) с `polarity` (∈ [-1, 1], единственное signed-измерение). Это
известный failure-mode structured outputs от LLM: out-of-range numeric
values при отсутствии provider-level grammar-constrained sampling.
DeepSeek через Anthropic-compat прокси такой gen-time enforcement не
гарантирует (в отличие от Claude `strict: true` или OpenAI strict).

**Решение (исследовано по тематическим ресурсам).** Многоуровневая
защита по best-practices (см. ниже). Применили L2 + усиление L0 (промпт):

- **L0 (prompt)** — уточнение в SYSTEM_PROMPT: явные ranges с inequality
  notation, специальная строка «DO NOT use negative values for
  relevance / intensity / uncertainty / forwardness — that is a
  frequent slip from polarity confusion».
- **L1 (provider strict)** — НЕ применимо: DeepSeek-V4 через Anthropic-
  compat прокси не поддерживает Claude `strict: true` reliably (по
  Anthropic docs strict даётся только в их собственных моделях с
  grammar-constrained sampling).
- **L2 (Pydantic coerce)** — **выбран**. Annotated pattern с
  `BeforeValidator` + `Field(ge/le)` constraint. Clamp out-of-range
  ДО валидации; Field constraint остаётся как формальная спецификация
  схемы. Безопасно к `None` / `NaN` / `inf` / нечисловым типам (всё →
  0.0 как neutral default).
- **L3 (retry-with-feedback)** — не применили (это бо́льший рефакторинг,
  Instructor-style auto-retry с error feedback в prompt). Отложено на
  потом если clamp недостаточен.
- **L4 (graceful degradation)** — частично уже было (parse_action
  возвращает string-error → log_decision записывает error, цикл не
  крашится).

**Источники.**

- [Pydantic ofic docs «Validators»](https://docs.pydantic.dev/latest/concepts/validators)
  — 4 типа валидаторов (After/Before/Plain/Wrap), annotated pattern,
  пример с `truncate` через `WrapValidator` (длинная строка обрезается,
  не реджектится).
- [Pydantic blog «Minimize LLM Hallucinations with Pydantic Validators»](https://blog.pydantic.dev/blog/2024/01/18/llm-validation/)
  — «Pydantic validators minimize LLM hallucinations by enforcing
  constraints on model outputs». Подтверждение что **defensive coerce
  vs hard reject** — это рекомендованный паттерн для LLM-payloads.
- [Instructor «Validation & Retry»](https://python.useinstructor.com/learning/validation/retry_mechanisms/)
  — auto-retry pattern: validation error feeds back to LLM как
  context, modelл регенерирует. Configuration: `max_retries`,
  `retry_if_parsing_fails`.
- [Anthropic «Strict tool use»](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use)
  — grammar-constrained sampling (`strict: true`) на provider-side.
  Идеальный L1, но доступен только в Anthropic native, не через
  DeepSeek-compat прокси.
- [tianpan.co «Structured Outputs Not a Solved Problem», 2026](https://tianpan.co/blog/2026-04-18-structured-output-json-mode-failure-modes)
  — three-tier recovery strategy: detect → log → retry/fallback/surface.
- [callsphere.ai «Handling Structured Output Failures»](https://callsphere.ai/blog/handling-structured-output-failures-retries-fallbacks-partial-parsing.md)
  — graceful degradation pattern (safe default vs crash).
- [The Neural Base «Validator functions»](https://theneuralbase.com/structured-outputs/learn/beginner/validator-functions/)
  — `@field_validator` баг-гарды для LLM JSON.

**Имплементация.**

```python
def _coerce_unit(value: Any) -> float:
    """Clamp к [0, 1]; defensive к None/NaN/inf/strings → 0.0."""
    if value is None: return 0.0
    try: v = float(value)
    except (TypeError, ValueError): return 0.0
    if math.isnan(v) or math.isinf(v): return 0.0
    return max(0.0, min(1.0, v))

UnitFloat = Annotated[float, BeforeValidator(_coerce_unit), Field(ge=0.0, le=1.0)]
SignedUnitFloat = Annotated[float, BeforeValidator(_coerce_signed_unit), Field(ge=-1.0, le=1.0)]

class SentimentItem(BaseModel):
    title_snippet: str = Field(default="", max_length=200)
    relevance: UnitFloat
    polarity: SignedUnitFloat
    intensity: UnitFloat
    uncertainty: UnitFloat
    forwardness: UnitFloat
```

**Тесты.** Добавлен `test_open_sentiment_out_of_range_clamped` — кейс
с тремя видами ошибок одной partition:
- `forwardness=-0.3` → 0.0 (исходный реальный bug)
- `polarity=-2`, `relevance=1.5`, `forwardness=2.0` → clamp к границам
- `intensity="N/A"` (string), `uncertainty=null` → 0.0 как safe default

33/33 в `test_fx_ai_trader.py`, 515/515 в полной панели зелёные.

**Категория.** **Bug-fix схемы**, не curve-fitting стратегии (правило
`no-data-fitting.mdc`): торговая логика, R:R, sentiment-uncertainty
gate, threshold'ы — НЕ менялись. Эксперимент n=0 НЕ перезапускается.
Аналог fix'у бага в коде, не tuning'у стратегии.

**Файлы:**
- `src/fx_ai_trader/trading/executor.py` — annotated pattern с
  BeforeValidator, type aliases `UnitFloat`/`SignedUnitFloat`,
  defensive coerce для None/NaN/inf/non-numeric.
- `src/fx_ai_trader/llm/prompts.py` — уточнение в sentiment-блоке:
  явные inequality ranges + строка про anti-polarity confusion.
- `tests/test_fx_ai_trader.py` — новый позитивный тест на clamp.

---

## 2026-05-12 (вечер) — prompt v1.0 «discretionary commodity trader» + KillSwitch redesign + experiment **n=0 reset**

`коммит при deploy`

**Контекст.** Пользователь явно указал: «из нашего кода по решениям и
стратегиям ничего брать не надо было... цель ии агента не повторять
нашего бота, а принимать решения самому». v0.1 / v0.2 промптов копировали
нашу advisor-математику (R:R ≥ 1.5, risk $25 hard, correlation haircut
0.7, same-direction concentration block) — LLM упирался в эти микро-
ограничения и за 13 decisions не выполнил ни одной сделки. Подход
был концептуально неправильным.

**Что переделано.** Полная переработка промпта и safety layer:

1. **Промпт v1.0 — discretionary commodity trader.** Содержание построено
   на реальных тематических ресурсах для gold/oil-трейдеров:

   - **KenMacro «How to Trade Gold (XAUUSD) 2026: Macro Trader's
     Institutional Guide»** (Ken Chigbo, 18+ years London FX, upd
     06-May-2026, https://kenmacro.com/how-to-trade-gold-xauusd-2026/):
     5-driver hierarchy (real yields → DXY → central banks → geopol →
     ETF/COT), noise-band sizing ($15–25 normal / $30–50 FOMC-NFP /
     $100–200 macro shocks), trading windows (London open / NY open /
     COMEX close), top-5 retail failure modes, Macro-Flow Confluence
     Pullback (MFP) setup.
   - **KenMacro «How to Trade Oil: The Macro Trader's Guide»**
     (https://kenmacro.com/how-to-trade-oil/): 4-channel framework
     (supply / demand / dollar / geopol), DXY correlation **flips**
     по режиму (supply-led = positive, demand-led = inverse), OPEC+
     quota-compliance-spare-capacity.
   - **FXMacroData «Gold vs. Real Yields»**: real-yields объясняют
     45–55% квартальной gold-return variance.
   - **Sprott Money / GetARC «Gold COT Report Analysis» May 2026**:
     managed money net long +94 254 contracts (down from +302 508 в
     Feb despite higher price — short-covering rally, exhaustion).
   - **Middle East Insider «OPEC+ Spare Capacity April 2026»** (22-Apr-
     2026): spare capacity ~5M b/d, highest since 2009, compresses
     risk premium / caps rallies.
   - **Middle East Insider «Brent Crude Q2 2026 Forecast»**: $72–88
     institutional band.
   - **East Daley / Investing.com «Brent-WTI Spread»**: May 2026 spread
     ~$8–12 vs historical $3.85 avg (Hormuz disruption + light/heavy
     mismatch).
   - **GlobalMarketRaiders «EIA Edge: WTI Crude Counter-Trend»**:
     EIA Wed 10:30 ET = single biggest scheduled vol event, fade-the-
     spike setup, API Tue evening preliminary.
   - **Mark Douglas «Trading in the Zone»** (2000, Penguin/Prentice
     Hall): probabilistic mindset, 5 fundamental truths, accept-risk-
     emotionally framework, casino-operator vs gambler distinction.
   - **Van K. Tharp «Definitive Guide to Position Sizing Strategies»**
     (2008, IITM Press) + R-multiple framework: P = C/R, position
     sizing accounts for ~91% of performance variation среди profi-
     managers.

   Промпт не содержит больше «v0.x» формул из нашего executor'а.
   LLM сам решает entry-confirmations, R:R, position size по Tharp
   R-multiple, когда close, когда hold. Hold — default; «patience is
   the edge».

2. **KillSwitch v1.0 — broker-safety only.** Сняты:
   - `correlation_haircut=0.7` (gold↔oil 2-я same-side → 0.7×).
   - `same-direction concentration block` (3-я same-side в correlated
     set отвергалась).
   - `R:R ≥ 1.5` hard cap в `executor.py`.
   - `risk_per_trade_usd ≤ $25` hard cap в `executor.py`.

   Оставлены ТРИ класса защиты:
   - **Catastrophic loss caps**: `max_daily_loss_usd=$150`,
     `max_total_loss_usd=$300` — полная остановка эксперимента, НЕ
     tuning-параметр.
   - **Broker margin safety**: `max_open_positions=3` (runaway-loop
     protection), `max_positions_per_symbol=3` (= общий, sanity),
     `max_lot_size=0.50` (clamp; на demo $1500 и XAUUSD margin
     ~$3000/lot = 0.5 лот ≈ весь капитал).
   - **Anti-hallucination gate**: `aggregate_uncertainty > 0.7` →
     reject open (LLM сам должен был вернуть hold; backstop в
     `parse_action`).

3. **Эксперимент перезапущен n=0** от **12-May-2026 ~11:30 UTC**
   (deploy v1.0). 13 предыдущих decisions с 0 executed не дают
   статистических данных — терять нечего. 14-day forward-test
   стартует заново. Эта правка эквивалентна **смене стратегии**, не
   bug-fix (правило `no-data-fitting.mdc`: «Если хотя бы одно условие
   не выполнено — не отключаем»; здесь же отключаем всю торговую
   логику, поэтому n=0 reset обязателен).

**Файлы:**
- `src/fx_ai_trader/llm/prompts.py` — полностью переписан SYSTEM_PROMPT
  + SYSTEM_PROMPT_REVIEW, docstring с реальными источниками.
- `src/fx_ai_trader/safety/killswitch.py` — убраны `_correlated_with`,
  same-direction block, correlation haircut. `KillSwitchConfig` без
  `correlation_haircut`.
- `src/fx_ai_trader/trading/executor.py` — убран R:R ≥ 1.5 hard check,
  убран `risk_usd > settings.risk_per_trade_usd` hard check;
  `risk_usd`/`r_r` остались для audit-логов.
- `src/fx_ai_trader/config/settings.py` — удалены `risk_per_trade_usd`,
  `correlation_haircut`; `max_positions_per_symbol` 2 → 3.
- `src/fx_ai_trader/app/main.py` — log строка адаптирована.
- `tests/test_fx_ai_trader.py` — переписаны KillSwitch и Settings
  тесты под v1.0 API; добавлены `test_v1_no_correlation_haircut` и
  `test_v1_no_same_direction_block`.
- `.env.example` — секция FX AI Trader обновлена с заметкой о
  снятых env vars.

**Тесты:** 32/32 в `test_fx_ai_trader.py` зелёные. Полный прогон
`tests/` — 514/514 проходят.

**Что ожидаем после deploy.** LLM прочитает promt v1.0 с 5 драйверами
gold и 4 каналами oil. Будут ли реальные open'ы или продолжение hold'ов
— решит сам LLM по реальным данным feed'а (price + DXY + EIA когда
есть + RSS news). Метрика успеха Phase 1: НЕ количество сделок (hold
ok), а **качество reasoning** (макро-driver упоминается? noise-band
sizing? real-yields/DXY check?) + отсутствие тех. ошибок parser/
направления SL.

---

## 2026-05-12 (утро) — prompt v0.2 bug-fix: LLM pip-confusion для XAUUSD/BRENT (ОТМЕНЁН)

> **Status:** эта версия отменена в тот же день вечером (см. v1.0 выше).
> Причина: подход «уточняем pip-math в промпте через формулы из нашего
> executor'а» — это копирование advisor-логики, чего пользователь явно
> просил не делать. Запись сохранена для исторического контекста.

## 2026-05-12 — prompt v0.2 bug-fix: LLM pip-confusion для XAUUSD/BRENT

`коммит при deploy`

**Симптом.** За 3 часа paper-mode (07:51 → 10:56 UTC, 13 decisions) — **0
executed**, 11 errors. Распределение:

| Ошибка | Кол-во | Что произошло |
|---|---|---|
| `risk_usd > $25` | 4 | LLM ставит SL distance 1050–5228 pips (50× больше разумного) |
| `R:R < 1.5` | 3 | LLM думает R:R=1.11–1.37 приемлемо |
| `parse_error` | 3 | id 1+2 до max_tokens-fix (4096), id 7 после (рецидив на 8000) |
| `SL direction` | 1 | BUY с SL=4690 выше price=4686.54 |
| `hold` | 2 | LLM сам пропустил — корректно |

Все 8 попыток открыться — **BUY XAUUSD**. Ни одной SELL, ни одного
BRENT. Все 8 — заблокированы executor'ом.

**Причина (root cause).** LLM путает **определение pip** для XAUUSD/BRENT
spot CFD:

- XAUUSD pip = **0.01 USD/oz** (corrected) — НЕ 0.0001 как для EUR/USD.
- BRENT pip = **0.01 USD/barrel** — то же самое.

При цене XAUUSD ~4690, LLM-генерируемые SL ~4670 → реальная distance в
*price* = $20, в *pips* = 2000. LLM, видимо, calculcates distance в
pips как `int(distance × 10000)` (привычка из EUR/USD), получая 2-3
порядка отклонения. В коде `executor.py:_pip_size_for("XAUUSD")=0.01`
правильный — executor рассчитывает risk_usd корректно (2000 pips ×
0.5 lots × $1/pip = $1000). KillSwitch блокирует. Не баг кода — баг
LLM understanding.

**Решение (prompt v0.2).** Целевая правка `SYSTEM_PROMPT` без изменения
стратегических порогов:

1. **Новый блок «PIP CALCULATION — CRITICAL»** перед DECISION FORMAT:
   - Явное «pip = 0.01 USD per ounce/barrel, NOT 0.0001»;
   - Два numerical примера (правильный + неправильный) для XAUUSD;
   - HARD CEILING: SL distance > 100 pips XAUUSD / > 80 pips BRENT —
     return "hold" не задумываясь;
   - Memorise-formula `risk_usd = SL_pips × $1 × lots`, ≤ 25;
   - 3 примера допустимых (0.5×50, 0.2×30, 0.1×80) + 2 ранее
     отвергнутых для контраста (cargo cult anti-pattern).

2. **MANDATORY SANITY-CHECK** перед JSON: 4 шага explicit compute
   (SL_distance_pips, direction inequalities, R:R, risk_usd) — LLM
   должен напечатать их в commentary до DECISION, иначе hold.

3. **Усилен HOLD-default**: «A rejected entry costs 0; a wrong entry
   costs up to $25. 0 trades for a day is fine. Never force a trade.»

**Что НЕ менялось (стратегические пороги под защитой `strategy-guard.mdc`):**

- `R:R ≥ 1.5` (BBX Research «Classic 1-2-3 Scaling»).
- `risk_per_trade_usd = $25` (1% от $500 виртуального капитала).
- `aggregate_uncertainty > 0.7` → hold (arxiv 2603.11408 sentiment gate).
- `max_lot_size = 0.50`.
- `MAX_POSITIONS_PER_SYMBOL = 2`, `MAX_OPEN_POSITIONS = 3`.
- `CORRELATION_HAIRCUT = 0.7` (finaur 2026 «correlations spike»).
- Multi-dim sentiment структура (5 dimensions per news).
- EXIT MANAGEMENT 4 trigger'а.

**Compliance с правилами репо:**

| Правило | Статус | Обоснование |
|---|---|---|
| `no-data-fitting.mdc` | OK | Это bug-fix LLM-понимания (pip definition), не curve-fitting под результаты бэктеста. Аналог Advisor `MIN_BARS=5→50` от 28.04 — clarification existing semantics, не optimization. |
| `strategy-guard.mdc` | OK | Stratagic thresholds (R:R, risk, sentiment-gate, lot caps) не тронуты. Меняется только **clarification** definition'а pip и **explicit sanity-check** перед entry — это user-prompt engineering, не торговая логика. |
| `sample-size.mdc` | OK | На момент правки n=0 executed trades. Решение не основано на P&L stats, а на 100% parse/validation error rate (8/8 attempts rejected на 1-уровневой semantic ошибке). |

**Эксперимент НЕ перезапущен.** 14-day counter продолжает идти от
исходного MVP deploy (12.05.2026). Если за 24-48 часов после prompt v0.2
LLM продолжит давать 100% rejection rate — это уже системная проблема
с LLM (или с симбиозом промпт+модель), потребуется другой подход
(например, переход на pure pip-based JSON schema без price-units, или
явная конвенция в executor'е принимать SL/TP в USD distance).

**Тесты.** 34/34 fx-ai-trader pass; полный suite не запускался —
изменение в одном файле prompts.py (string constant), не затрагивает
executor / killswitch / parser. Логику парсера НЕ меняли.

**Файлы:**
- `src/fx_ai_trader/llm/prompts.py` (новый раздел + sanity-check + docstring v0.2)
- `BUILDLOG_AI_FX_TRADER.md` (эта запись)

---

## 2026-05-12 — token rotation hardening (изоляция OAuth-токенов)

`коммит при deploy`

**Контекст.** В тот же день что и MVP deploy случился инцидент с OAuth:
`refresh_token` в shared `/data/ctrader_tokens.json` оказался spent
после single-use rotation. Advisor с 09.05 не торговал, fx-ai-trader не
смог стартануть. Детальный post-mortem — в `BUILDLOG.md` 12.05
«defensive token sync + startup token-status log».

После полного re-auth через `fx-pro-auth`-flow тот же риск остаётся
для будущего: если callback `_on_token_refreshed → token_store.save`
упадёт между OAuth-call и `save()` — refresh_token потеряется.

**Защита на 3 уровнях (см. BUILDLOG.md детали):**

**A. Defensive sync** в `CTraderClient._do_auth` — после каждого
успешного auth in-memory токены пишутся в shared store через
callback (идемпотентно). Закрывает race «refresh прошёл, callback
упал».

**B. Startup logging** через `auth.log_token_status` — INFO/WARN/ERROR
в `docker logs` обоих ботов про сколько дней до expiration. Видимость.

**C. Изоляция token-store** между Advisor и fx-ai-trader. Дефолтный
`AiFxTraderSettings.ctrader_token_path` сменён с
`/data/ctrader_tokens.json` (shared) на `/data/ctrader_tokens_ai_fx.json`
(отдельный grant). После этого refresh одного бота **не задевает** refresh
другого: они живут на двух независимых OAuth grant'ах одного приложения
client_id.

**Что НЕ изменилось.**

- `client_id` / `client_secret` остаются общие (это credentials самого
  приложения, не пары access/refresh).
- `CTRADER_ACCOUNT_ID=46883073` (demo-аккаунт) общий — оба бота
  торгуют на одном счёте, но с **разными labels** (`fx-pro-bot` для
  Advisor, `ai-fx-trader` для AI). Reconcile-логика отделяет позиции
  по label (см. `client_adapter.get_open_positions`).
- `token_lock.py` (file-flock advisory lock) остаётся, но теперь
  имеет academic смысл — два разных файла не конкурируют. Хранится
  как defence-in-depth: если кто-то в `.env` пропишет тот же path —
  flock спасёт от corrupted writes.

**OAuth-flow для fx-ai-trader (выполнено вручную перед push'ем).**

1. Сгенерирован тот же `grantingaccess/?client_id=...&redirect_uri=...`
   URL как для Advisor (один client_id = одно приложение cTrader).
2. Пользователь авторизовался в браузере **второй раз** — cTrader
   выдал **новый** authorization code (тот же приложение, тот же
   аккаунт, но второй независимый grant — refresh_token будет
   собственный).
3. На VPS внутри образа `fx-pro-bot:local` сделан
   `exchange_code_for_tokens(...)` → атомарная запись в
   `/data/ctrader_tokens_ai_fx.json`.
4. После этого запушен код-change (default path) и сделан selective
   rebuild fx-ai-trader.

**Verification после деплоя:**
- Логи fx-ai-trader при старте: `FX-AI-Trader cTrader OAuth: токен
  валиден до 2026-06-11..., осталось 30.0 дней` (INFO).
- Логи Advisor при старте: `Advisor cTrader OAuth: токен валиден до
  2026-06-11..., осталось 30.0 дней` (INFO).
- Файлы:
  - `/data/ctrader_tokens.json` — Advisor (refresh_token = `Slyfr...`)
  - `/data/ctrader_tokens_ai_fx.json` — fx-ai-trader (другой refresh_token)
- Оба бота подключаются к cTrader через свои OAuth grant'ы.

**Operational follow-up.** Раз в 2-3 недели смотреть `docker logs` обоих
ботов на наличие `WARNING ... токен истекает через X дней` — если
появилось, пробросить `fx-pro-auth` заранее на оба бота (отдельные
OAuth-flow для каждого token-файла).

**Файлы:** `BUILDLOG.md` 12.05 «defensive token sync + startup
token-status log» содержит полный список.

---

## 2026-05-12 — Phase 1 deploy + fix LLM max_tokens 4096→8000

`коммит при deploy`

**Контекст.** После коммита `871e67c` (MVP scaffold) — selective rebuild
fx-ai-trader на VPS. Контейнер изначально упал в restart-loop:

```
RuntimeError: cTrader refresh error: Access denied
```

**Причина (НЕ наш код).** Shared `/data/ctrader_tokens.json` содержал
`refresh_token`, который Spotware уже признал недействительным (cTrader
OAuth2: refresh_token rotation = single-use grant, RFC 6749 §6).
Последний успешный refresh-callback Advisor'а датируется ≈9 апреля
(вычислено по `expires_at = 1778349598` минус 30-дневное окно). С тех пор
файл не обновлялся, а в `client.py` proactive-refresh (`fb0ffd1`,
11.05.2026) использует тот же spent token.

На момент моей диагностики Advisor сам тоже шёл с `cTrader: торговля
отключена` (видно в его свежих логах) — то есть проблема общая, не у
fx-ai-trader специфическая.

**Решение.** Полный re-auth через `fx-pro-auth`-flow:
1. Сгенерировал auth URL, пользователь авторизовался в браузере
   ([id.ctrader.com](https://id.ctrader.com/my/settings/openapi/grantingaccess/)).
2. На VPS внутри образа `fx-pro-bot:local` (`docker run --rm
   --env-file .env`) сделал `exchange_code_for_tokens(...)` →
   `TokenStore.save(...)`. Новый `expires_at = 1781200153` (≈11 июня).
3. `docker compose restart advisor` + `docker compose up -d --no-deps
   fx-ai-trader` → оба контейнера подхватили свежие токены из
   `/data/ctrader_tokens.json` через свои `TokenStore.load()` / наш
   `ensure_valid_token_race_safe()`.

После рестарта:
- Advisor: `cTrader: аккаунт 46883073 авторизован, готов к торговле`.
- fx-ai-trader: `SymbolCache 252`, `XAUUSD → XAUUSD (id=41)`,
  `BZ=F → BRENT (id=1117)`, `Full cycle 1`, RSS 40 items (8–9 после
  gold+oil фильтра), DeepSeek 200 OK, цена цикла $0.00174.

**Урок и follow-up для будущего.** Когда оба бота держат `ctrader_tokens.json`
в общем volume — потеря синхронизации возможна (Advisor рефрешит in-memory,
но если callback `_on_token_refreshed → token_store.save` падает между
вызовом OAuth-endpoint и `json.dump` — token-store остаётся со spent refresh).
Наш `token_lock.py` решает concurrent refresh между процессами, но не
решает «callback failed mid-write». Phase 2: либо отдельный
`/data/ctrader_tokens_ai_fx.json` (полная изоляция OAuth у каждого бота),
либо health-check «is refresh-token still valid» в Advisor с alert через
RSS/Telegram. Сейчас зафиксировано как known-risk, без code-fix.

---

### fix(fx_ai_trader): LLM max_tokens 4096 → 8000 (JSON режется)

`коммит при deploy`

**Симптом.** После успешного старта в `07:50–08:08 UTC` 12.05 контейнер
работал, но 2 full-cycle подряд:

```
LLM tokens: in=4247 out=4096    ← упёрся в max_tokens
LLM response: ## Analysis Commentary 1. TREND: XAUUSD 4H EMA20 (4699)...
Parse error: no JSON object with 'action' found
Parse error: JSON parse error: not a decision dict (missing 'action'): dict
```

LLM выдавал полный analysis commentary (TREND / VOLATILITY / MACRO /
SENTIMENT для двух символов) и **обрезался по `max_tokens=4096`** до того
как добирался до финального JSON-блока. В результате `parse_action` не
находил decision-объект → `apply_action` не вызывался → бот не торговал.

**Причина (тех-параметр, не торговая логика).** Анти-Anthropic-compat
endpoint DeepSeek-V4 включает thinking-блок (внутренний reasoning) поверх
max_tokens. На bybit-варианте `ai_trader` 4096 достаточно (1 символ,
без EIA блока). У `fx_ai_trader` промпт ×2 длиннее:
- 2 символа × (current/1H × 24 / 4H × 30 / индикаторы)
- macro-block (DXY + EIA для oil)
- multi-dim sentiment блок per news × 5 items
- двойной EXIT MANAGEMENT блок (full vs review)

Соответственно ответ тоже ×2 длиннее. 4096 = thinking + commentary, на
JSON ничего не остаётся.

**Решение.** В `AiFxTraderSettings.deepseek_max_tokens` дефолт
`4096 → 8000`. Anthropic-compat API DeepSeek поддерживает до 8192
(см. [api-docs.deepseek.com/guides/anthropic_api](https://api-docs.deepseek.com/guides/anthropic_api)).
Стоимость full-cycle растёт с $0.00174 до ~$0.0028 (+60%) — для
paper-mode наблюдения за ~14 дней при 96 циклах/сутки = $3.9/мес вместо
$2.4, незначимо.

**Без изменений.** Промпт `SYSTEM_PROMPT` НЕ редактируется (он заморожен
правилом `no-data-fitting.mdc` на ≥14 дней paper-observation). Только
бюджет на output. KillSwitch, multi-dim sentiment, R:R 1.5 gate, paper
reconcile — без изменений.

**Compliance.** Не нарушает `strategy-guard.mdc` (тех-параметр клиента
LLM, не торговый порог), `no-data-fitting.mdc` (выход режется по
объективной причине: `out=4096` в логе, не подгонка под результаты
бэктеста), `sample-size.mdc` (не connection между сделками, n=0 на
момент фикса).

**Файлы:**
- `src/fx_ai_trader/config/settings.py` (default 4096 → 8000 + комментарий)
- `.env.example` (раздел AI_FX_TRADER, новый default)
- `BUILDLOG_AI_FX_TRADER.md` (эта запись)

**Тесты.** 34/34 fx-ai-trader pass; полный suite 513/513 pass (max_tokens
не влияет ни на parser, ни на бизнес-логику).

---

## 2026-05-12 — Phase 1 MVP scaffold (paper-mode)

**Запрос пользователя:** «создать AI-агента для FX (gold + oil), аналог
Bybit ai_trader». Источник: chat-thread `[fx_ai_oil_trader_mvp_44cb1e89]`
и план `/.cursor/plans/fx_ai_oil_trader_mvp_44cb1e89.plan.md`.

**Что сделано.** Создан изолированный пакет `src/fx_ai_trader/` — третий
бот в репо (после `fx_pro_bot/` advisor и `bybit_bot/`/`ai_trader/`).
Phase 1: paper-mode на XAUUSD (spot gold CFD) + BZ=F (Brent oil →
cTrader `BRENT`), dual-timer 15+5 мин, DeepSeek-V4 через
Anthropic-compatible endpoint.

### Структура пакета

```
src/fx_ai_trader/
├── __init__.py / __main__.py
├── app/main.py                  # dual-timer 15+5 cycle (full + review)
├── config/settings.py           # AI_FX_TRADER_* env-prefix, paper-by-default
├── llm/
│   ├── prompts.py              # SYSTEM_PROMPT FX + multi-dim sentiment блок
│   └── client.py               # shim над ai_trader.llm.client (DeepSeek-V4)
├── trading/
│   ├── client_adapter.py       # CTraderFxAdapter поверх fx_pro_bot.CTraderClient
│   ├── token_lock.py           # race-safe OAuth refresh (fcntl.flock + re-check)
│   ├── executor.py             # Pydantic schema + parse_action + apply_action
│   ├── paper_reconcile.py      # SL/TP touch detection через M1 свечи
│   └── context.py              # collect + format full/review
├── news/
│   ├── rss.py                  # ForexLive/Investing/OilPrice/Kitco + double filter
│   └── eia.py                  # EIA Open Data weekly petroleum
├── state/db.py                 # AiFxTraderStore (positions/decisions/daily_pnl)
├── safety/killswitch.py        # FX-параметры + correlation-aware checks
└── analysis/indicators.py      # shim над ai_trader.analysis.indicators
```

### Ключевые решения и source-of-truth

**Изоляция от существующего Advisor (cTrader Gold-ORB):**

- Advisor торгует **GC=F futures** (`scalping_gold_orb.py`, MAX_POSITIONS=10),
  AI-агент торгует **XAUUSD spot** + **BRENT** — это другие cTrader
  `symbolId` → полная broker-side изоляция, никаких пересечений на
  margin pool.
- Все наши ордера маркируются `label="ai-fx-trader"`. Advisor ставит
  `label="fx-pro-bot"`. Reconcile у обоих фильтрует по своему label —
  никто не закроет чужую позицию как «orphan». Спецификация cTrader OpenAPI
  forum 41177 + FAQ подтверждают что `label` — string ≤100 chars,
  устойчивый к рестартам и виден в `ProtoOAReconcileRes.position`.
- Отдельная БД: `/data/fx_ai_trader.sqlite` (vs `stats.sqlite` advisor'а).
- OAuth token-store шарится: `/data/ctrader_tokens.json` — общий demo-аккаунт
  FxPro. Concurrent refresh защищён через `fcntl.flock` advisory lock с
  re-check после acquire (см. ниже Risk 4).

**Multi-dim sentiment block (Risk 1 mitigation):**

- Промпт требует от LLM 5-мерный sentiment per news item:
  `relevance / polarity / intensity / uncertainty / forwardness ∈ [0,1]`
  + `aggregate_uncertainty` per cycle.
- `executor.parse_action(max_uncertainty=0.7)` — gate: при
  `aggregate_uncertainty > 0.7` open-decisions reject'ятся ДО broker
  call'а. Cost-savings + предотвращает entry на low-conviction LLM.
- Sentiment JSON пишется в `decisions.sentiment_json` для post-hoc
  валидации калибровки uncertainty.

**Pydantic schema validation:**

- `OpenAction / CloseAction / HoldAction` — все три варианта решения.
- Структурные ошибки (неверный тип, отсутствующее поле, side ≠
  BUY/SELL, lots ≤ 0, lots > 10, uncertainty out of [0,1]) ловятся
  ДО apply-стадии (Risk 1: schema at agent boundaries — Tauric Research
  PR #458).

**KillSwitch — FX-параметры + correlation-aware:**

| Параметр | Значение | Обоснование |
|---|---|---|
| `MAX_DAILY_LOSS_USD` | 150 | `$25 risk × 6 убыточных = $150` |
| `MAX_TOTAL_LOSS_USD` | 300 | halt эксперимента (60% капитала) |
| `MAX_POSITIONS` | 3 | для 2 инструментов |
| `MAX_POSITIONS_PER_SYMBOL` | 2 | защита от over-allocation (Janus Henderson 2026) |
| `RISK_PER_TRADE_USD` | 25 | от virtual capital $500 (5%) |
| `MAX_LOT_SIZE` | 0.50 | hard cap, защита от extreme-волатильности |
| `CORRELATION_HAIRCUT` | 0.7 | gold↔oil correlated в risk-off (finaur 2026) |

Дополнительные guards:
- **Same-direction concentration**: 3-я позиция в одну сторону по
  correlated assets (XAUUSD + BZ=F = correlated group) REJECT'ится.
- **Correlation-haircut**: 2-я позиция в ту же сторону по correlated set
  → `volume_lots × 0.7` (research: finaur «correlation spikes in crisis»).

**Paper-mode reconcile через M1 свечи:**

В paper-mode broker не отрабатывает SL/TP (мы не ставили реальный ордер).
`paper_reconcile.reconcile_paper_positions()` тянет M1 свечи с момента
`opened_at` и для каждого бара проверяет touch SL/TP. Gap-day
(одновременный пробой SL и TP в одном баре) → preferred SL (conservative,
worst execution assumption).

NB: используются M1 свечи **самого cTrader** (`get_trendbars` period=1),
а не yfinance — это уменьшает «фантомные» touch'и из-за wide-spread
wicks. Weekend gaps корректны (бары просто отсутствуют в этом промежутке).

### Risks & mitigations (community-verified)

#### Risk 1 — LLM hallucinations / invalid output

**Mitigation:** Pydantic schema at boundary + multi-dim sentiment +
uncertainty gate `> 0.7 → reject open`.

**Source:**
- Tauric Research/TradingAgents PR #458 «schema at agent boundaries».
- Medium 2026 «Hallucination prevention out of the prompt and into the
  schema».
- arxiv 2603.11408 «Beyond Polarity: Multi-Dimensional LLM Sentiment
  Signals for WTI Crude Oil Futures Return Prediction».

#### Risk 2 — RSS / news noise

**Mitigation:**
- Source weights (OilPrice/Kitco = 0.7, ForexLive/Investing = 1.0).
- 12h time-window filter.
- Dedupe by normalized title (lowercase + strip punctuation, не по URL).
- Двойной keyword filter (gold-set + oil-set, news может попасть в оба).

**Source:**
- stock-market.live 2026 «Build a News-Driven Trade Bot — selectivity > speed».
- stockalpha.ai sentiment guide — entity extraction + time-window.

#### Risk 3 — cTrader rate-limit (50 req/s non-historical, 5 req/s historical)

**Mitigation:**
- Реюз `fx_pro_bot.trading.client.CTraderClient` — он уже имеет
  heartbeat 8s, smart-reset backoff `(5,10,30,60,120,300,900)`,
  `STABLE_UPTIME_SEC=300` (см. `BUILDLOG.md` 06-11.05.2026).
- Один общий клиент на процесс через `CTraderFxAdapter`.
- Bars-запросы только в начале каждого full-cycle (15 мин) + review
  (5 мин) — нагрузка ~5–10 req/cycle, далеко от лимитов.

**Source:** cTrader Open API docs `help.ctrader.com/open-api/connection/`,
community forum 45954 (silent token rotation).

#### Risk 4 — OAuth refresh race condition (shared token-store с Advisor)

**Mitigation:** `fx_ai_trader/trading/token_lock.py` —
`fcntl.flock` advisory exclusive lock + re-read под локом + atomic
write через `os.rename`. Если другой процесс уже refresh'нул токен
пока мы ждали — используем on-disk значение, refresh НЕ вызывается
(single-use refresh_token cTrader защищён).

**Source:**
- Coder PR #22904 «singleflight + optimistic locking».
- Nango blog «How to handle concurrency with OAuth token refreshes».
- openai/codex issue #10332 «file lock + re-check pattern».

#### Risk 5 — Paper-mode статистическая значимость

**Mitigation:** Phase 1 = ≥14 дней paper-observation (≥30 трейдов,
p-value < 0.05). LIVE не включаем без подтверждённой стабильности
парсера, калибровки uncertainty-gate, в разных режимах рынка
(тренд/флет/новости). Правило `sample-size.mdc` соблюдено.

**Source:**
- NexusTrade 2026 «30–90 days paper trading before live».
- Kiploks «weeks, not hours» calibration guide.
- `.cursor/rules/sample-size.mdc` (this repo).

#### Risk 6 — Gold instrument overlap с Advisor (GC=F)

**Mitigation:** Advisor торгует **GC=F futures** (cTrader: `GOLD_*` —
front-month contract), AI-агент торгует **XAUUSD spot** — это разные
символы на cTrader, разные `symbolId`, **независимый margin pool на
аккаунте**. На FxPro demo они существуют параллельно как разные
instruments. Дополнительно `label`-isolation на уровне reconcile.

**Source:**
- cTrader OpenAPI symbols catalog (`get_symbols` + `get_symbol_details`).
- FxPro symbols specification.

#### Risk 7 — Gold↔Oil correlation в risk-off

**Mitigation:**
- `CORRELATION_HAIRCUT = 0.7` на 2-ю позицию в одну сторону.
- Same-direction concentration check блокирует 3-ю позицию.
- LLM-промпт явно предупреждает: «moderately correlated during risk-off
  (both up on geopolitical, both down on USD strength)».

**Source:**
- finaur «Asset Correlation in Times of Crisis» 2026.
- Janus Henderson «Building smarter commodity exposure» 2026.

### Тесты

`tests/test_fx_ai_trader.py` — **34 теста, все зелёные**.
Polный pytest репо: **513 / 513 passed**.

Покрытие:
- `TestParseActionSchema` (15 тестов): hold + sentiment, open XAUUSD,
  open BRENT, high-uncertainty gate, custom threshold, invalid side,
  unknown symbol, negative lots, lots > 10, close, review-mode open
  reject, markdown fence, extra commentary, no JSON.
- `TestKillSwitch` (8): correlated set, empty store, max positions,
  per-symbol cap, correlation-haircut, same-dir concentration,
  opposite-dir allowed, daily loss block.
- `TestPaperReconcile` (6): long SL/TP, long no-touch, short SL/TP,
  gap-day prefer-SL.
- `TestVolumeRounding` (1): round-down к step + clamp [min,max].
- `TestTokenLockRecheck` (2): fresh-token no refresh, concurrent
  re-check (другой процесс перезаписал → refresh не вызван).
- `TestSettings` (2): defaults, db_path.

### Deploy (Phase 1 paper)

`Dockerfile.fx-ai-trader` (Python 3.12-slim, `pip install .`, entry
`python -m fx_ai_trader`) + сервис `fx-ai-trader` в `docker-compose.yml`
с bind-mount `./data:/data` (общий с Advisor для token-store).

Дефолты в compose:
- `AI_FX_TRADER_TRADING_ENABLED=false` (paper)
- `AI_FX_TRADER_SYMBOLS=XAUUSD,BZ=F`
- `AI_FX_TRADER_POLL_INTERVAL_SEC=900` / `REVIEW=300`

Шарит `DEEPSEEK_API_KEY` с `ai-trader` (один аккаунт-level rate-limit).

**Файлы:**
- new: `src/fx_ai_trader/` (полное дерево, 13 файлов)
- new: `tests/test_fx_ai_trader.py` (34 теста)
- new: `Dockerfile.fx-ai-trader`
- modified: `docker-compose.yml` (добавлен сервис `fx-ai-trader`)
- modified: `.env.example` (документация env'ов FX AI Trader)
- modified: `pyproject.toml` (`fx-ai-trader` entry-point + hatch packages)
