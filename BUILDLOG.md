# Build Log

Лог изменений FX Pro Bot с момента подключения демо-счёта cTrader (07.04.2026).

---

## 2026-04-20

### fix(ctrader_feed): декодинг trendbars для JPY-пар (цены × 100)
`339a30e`

**Симптом:** после перехода на cTrader все JPY-позиции закрывались с «дикими»
отрицательными числами в логах (`USDJPY SHORT → -1572720.8 pips (stop_loss)`),
а позиции, висевшие в плюсе, регулярно закрывались в минус.

**Причина:** в `_decode_trendbar` делили raw-цену на `10^digits`. Для EURUSD
(digits=5) совпадало со стандартом cTrader, но для USDJPY (digits=3) выдавало
цену в 100 раз больше реальной: raw=15_884_200 → 15884.2 вместо 158.842.
Стратегии строили SL/TP от этой псевдо-цены (~15885), а брокер принимал ордер
по настоящей рыночной цене (~158.8). На трейлинге монитор видел «падение»
с 15885 до 158 → считал это дичайшим движением → закрывал по SL.

**Факт с live-API (подтверждено диагностикой):** `ProtoOATrendbar.low`
и дельты приходят в **фиксированной точности 10⁻⁵ для любого символа**,
независимо от `digits`/`pipPosition`. Проверено на EURUSD, GBPUSD, USDJPY,
EURJPY, GBPJPY.

**Решение:** `TRENDBAR_SCALE = 100_000` как константа, параметр `digits` в
`_decode_trendbar` оставлен для обратной совместимости, но игнорируется.
Относительные SL/TP в executor уже используют `* 100_000` — consistent.

**Тесты:** +1 regression-тест `test_bars_from_ctrader_decodes_jpy_pair_correctly`
(raw 15_884_200 → 158.842). Всего 271 passed.

**Файлы:** `src/fx_pro_bot/market_data/ctrader_feed.py`,
`tests/test_ctrader_feed.py`.

### fix: переход с yfinance на cTrader Open API для OHLCV-баров

**Симптом:** с утра понедельника 20.04 не открывалась ни одна сделка, хотя
бот работал и на прошлой неделе исправно торговал.

**Причина:** yfinance (Yahoo) отдал форекс-бары с **дырой 200 минут**
(06:10–09:30 UTC) — ровно период открытия London session. Session ORB не мог
построить box (нужно минимум 4 M5-бара после 08:00 UTC), outsiders-индикаторы
считались по устаревшим данным, ensemble/VWAP/Stat-Arb возвращали 0 сигналов.
Все 4 пары (EURUSD, GBPUSD, USDJPY, AUDUSD) имели одинаковую дыру одновременно.

**Решение:** реализована миграция основного источника баров на cTrader
Open API (`ProtoOAGetTrendbarsReq`). На том же периоде cTrader отдаёт
**48 баров в окне 06:00–10:00 UTC** (у yfinance было 0), volume настоящий
(у yfinance для форекса = 0), ответ за 1.04 сек. Архитектура:

- `CTraderClient.get_trendbars(symbol_id, period_minutes, from_ts, to_ts)` — raw
  proto-trendbars через существующий `_send_and_wait` паттерн.
- `market_data/ctrader_feed.py` — декодинг low + deltaOpen/High/Close через
  `digits` символа, плюс `bars_with_fallback()` с автоматическим откатом на
  yfinance если cTrader недоступен или вернул < 51 бара. Для крипты, которой
  нет в каталоге cTrader, — сразу yfinance.
- `scan_instruments(bar_fetcher=...)` — опциональный параметр, обратная
  совместимость 100% (тесты используют дефолтный yfinance).
- `app/main.py` — `_make_bar_fetcher(executor)` создаёт cTrader-fetcher при
  активной торговле, иначе None → дефолт (yfinance).

Торговая логика (стратегии, пороги, SL/TP) не тронута — меняется только
источник данных.

**Тесты:** +9 новых (`tests/test_ctrader_feed.py`) — декодинг OHLC, маппинг
таймфреймов, 4 сценария fallback, интеграция с сканером. Итого 270 passed.

**Файлы:** `trading/client.py`, `trading/executor.py` (публичные `client`/
`symbols`), `market_data/ctrader_feed.py` (новый), `analysis/scanner.py`,
`app/main.py`, `tests/test_ctrader_feed.py`

---

## 2026-04-16

### revert: откат OUTSIDERS_ALLOW_SYMBOLS — защита от оверфита

Откат решения от 2026-04-16 ограничить outsiders/ensemble только `USDJPY=X`.

Причина: мета-анализ BUILDLOG показал системный риск оверфита — несколько последних
решений принимались на малых выборках (2 сделки XAUUSD, 25 сделок по скальпингу,
28 сделок outsiders на пару за 48ч в одном рыночном режиме). По мировым практикам
(Lopez de Prado, Bailey, Harvey) для статистически значимого отключения инструмента
нужно ≥100 сделок в разных рыночных режимах.

Возвращён прежний `OUTSIDERS_EXCLUDE_SYMBOLS` (крипта, GC=F, EURJPY). Собираем
данные 1–2 недели в разных режимах, затем пересматриваем при sample ≥100.

Добавлено правило `.cursor/rules/sample-size.mdc`: порог 100 сделок / 2 недели
перед изменением стратегий на основе P&L-статистики.

**Файлы:** `strategies/outsiders.py`, `app/main.py`, `STRATEGIES.md`,
`tests/test_outsiders_realism.py`, `.cursor/rules/sample-size.mdc`

### restrict: outsiders/ensemble — только USDJPY (откачено)

Анализ 281 сделки за 48ч (14–16.04) через cTrader API выявил:
- **outsiders** генерирует 63% общих убытков (-$30.46 из -$48.04)
- Убыточен на 4 из 5 пар: GBPUSD WR 16% (-$13.66), EURUSD WR 22% (-$10.22), AUDUSD WR 19% (-$7.12), GBPJPY WR 43% но R:R 0.75 (-$2.41)
- Единственный прибыльный символ — USDJPY: WR 64%, net +$2.95, LONG-нога +$4.87

Решение: заменить чёрный список `OUTSIDERS_EXCLUDE_SYMBOLS` на белый список
`OUTSIDERS_ALLOW_SYMBOLS = {"USDJPY=X"}`. Фильтр применяется и к outsiders, и к ensemble.

**Откачено** в рамках антиоверфит-ревизии — sample size (28 сделок/пару) не даёт
статистической значимости.

**Файлы:** `strategies/outsiders.py`, `app/main.py`, `STRATEGIES.md`

---

## 2026-04-13

### fix: ужесточение фильтров скальпинг-стратегий по результатам анализа API

Анализ 25 сделок за 10 часов (через cTrader API) выявил системные проблемы:
- **VWAP reversion**: 100% loss rate (4/4) — входы против тренда в трендовом рынке
- **Stat-arb**: обе ноги EUR/GBP летят в минус одновременно, корреляция разваливается
- **Session ORB news_fade**: ложные сигналы в тихие часы (00:00–02:00 UTC), нет реальных новостей

Применённые изменения (подтверждены исследованием литературы и best practices):
1. **VWAP**: ADX_MAX 25→20, добавлен ADX falling check, HTF EMA(200) H1 тренд-фильтр
2. **News fade**: ограничен часами London (08-12) и NY (14:30-17 UTC)
3. **Stat-arb**: ADF-тест коинтеграции (t-stat < -2.86), Z_ENTRY 2.0→2.5
4. **Общее**: HTF фильтр EMA(200) на H1 для всех скальпинг-стратегий (вместо шумного EMA50 M5)

**Файлы:** `strategies/scalping/vwap_reversion.py`, `strategies/scalping/session_orb.py`, `strategies/scalping/stat_arb.py`, `strategies/scalping/indicators.py`, `STRATEGIES.md`

---

## 2026-04-12

### remove: коммодити и индексы убраны — только валютные пары

Нефть (CL=F, BZ=F) тянет P&L в минус — высокие спреды, трудно ловить движения.
Золото (GC=F) и индекс S&P500 (ES=F) аналогично. Оставлены только 7 форекс-пар.

**Изменения:**
- `DEFAULT_SYMBOLS`: убраны GC=F, CL=F, BZ=F, ES=F
- `STRATEGIES.md`: обновлены таблицы инструментов, убраны секции Commodities/Indices

**Файлы:** `config/settings.py`, `STRATEGIES.md`

### remove: крипта полностью убрана из FxPro advisor

За 24ч (11-12.04): 96 сделок, суммарный P&L -1108 pips (-6.05%).
stat_arb BTC/ETH: -627 pips (34 сделки), ETH-нога систематически проигрывает.
session_orb крипто: -457 pips (56 сделок), 16 из них audit_sl_past (SL пробит при открытии).
Крипта нерентабельна на FxPro cTrader — высокие спреды, проблемы с SL на альткоинах.

**Изменения:**
- `DEFAULT_SYMBOLS`: убраны все 11 крипто-тикеров
- `SCALPING_CRYPTO_ALLOWED`: очищен (BTC/ETH больше не допущены)
- `stat_arb.DEFAULT_PAIRS`: убрана пара BTC-USD/ETH-USD
- `STRATEGIES.md`: обновлены описания, убрана секция Crypto из инструментов

**Файлы:** `config/settings.py`, `strategies/scalping/stat_arb.py`, `STRATEGIES.md`

---

## 2026-04-11

### calibrate: калибровка скальпинга — альткоины, ADX, news_fade

**Симптом:** все крипто-позиции SHORT на растущем рынке, 0% WR на альткоинах.
Бот считает +5 pips, cTrader показывает -$0.01 (спреды альткоинов съедают всё).
cTrader не может поставить SL на альткоины → позиции без защиты → orphans → FORCE CLOSE.

**Три изменения:**

1. **Альткоины исключены из скальпинга.** Только BTC и ETH допущены к VWAP/ORB.
   SOL, DOGE, ADA, LINK, AVAX, LTC, BNB, DOT, XRP — убраны.
   Причина: `TRADING_BAD_STOPS` для всех альткоинов, спреды 5+ pips, yfinance≠cTrader цены.

2. **ADX-фильтр (≤25) добавлен в VWAP и ORB.** Outsiders уже имели этот фильтр.
   При ADX>25 (тренд) mean-reversion стратегии убыточны — теперь пропускаются.

3. **News_fade порог для крипто 3.0 ATR** (было 2.0). Крипто более волатильна,
   спайк 2xATR — это нормальный шум, а не аномалия для fade.

**Файлы:** `settings.py`, `vwap_reversion.py`, `session_orb.py`, `STRATEGIES.md`

---

### fix: аудит SL — использовать db_pos.stop_loss_price вместо пересчёта

**Симптом:** альткоины (DOT, SOL, LINK, AVAX, LTC, DOGE, ADA) закрывались
FORCE CLOSE через 2-6 сек после открытия. 10 из 10 audit_sl_past — false positive.

**Причина:** `_ensure_broker_sl_tp()` при `has_sl=False` (cTrader не ставит SL
для альткоинов) **пересчитывал SL из текущего ATR** вместо использования
`db_pos.stop_loss_price` (который стратегия уже рассчитала с 0.5% floor).
Пересчитанный SL отличался от DB: для DOT даже оказывался НИЖЕ entry для SHORT.
Проверка `sl_past` срабатывала → мгновенный force close.

**Решение:** если `db_pos.stop_loss_price > 0` — брать SL из DB напрямую.
Пересчёт из ATR — только fallback когда в DB нет SL.

**Файлы:** `main.py`

---

### fix: крипто R:R — TP floor 0.2% → 0.6% (было 2.5:1 против нас)

**Проблема:** `CRYPTO_SCALP_TP_MIN_PCT = 0.002` (0.2%) при `SL_MIN_PCT = 0.005` (0.5%).
R:R = 0.4:1 — рискуем $364 ради $145 на BTC. Нужен WR >71% чтобы выйти в ноль.

**Исправление:** `CRYPTO_SCALP_TP_MIN_PCT = 0.006` (0.6%). Новый R:R = 1.2:1.
Для BTC: TP ~$437 vs SL ~$364, для ETH: TP ~$13.5 vs SL ~$11.2.
Прибыльно при WR >45%.

**Файлы:** `monitor.py`, `STRATEGIES.md`

---

### fix: TRADING_BAD_VOLUME при закрытии orphan-позиций + аварийный SL/TP

**Симптом:** 8 orphan-позиций (альткоины, commodities) висели без SL/TP бесконечно.
Каждые 5 минут в логах: `ORPHAN CLOSE → TRADING_BAD_VOLUME — closeVolume 1000.00 > position volume 0.01`.
За 16 часов -$63 (4.2% от депозита), 353 сделки с 20.7% win rate.

**Причина:** `executor.close_position()` при `volume=None` подставлял
`lots_to_volume(0.01)` = 1000 (forex 100k contract). Для CFD-инструментов
(альткоины, commodities) реальный volume = 1 → cTrader отклоняет.

**Решение:**
1. `close_position(volume=None)` → reconcile для точного volume с брокера
   (`_resolve_position_volume` — новый метод)
2. Orphan-позиции: если close не удался → аварийный SL/TP ±2% от entry
   (лучше аварийная защита чем открытый риск)
3. Передача `bp.tradeData.volume` при закрытии orphan в аудите

**Файлы:** `executor.py`, `main.py`

---

## 2026-04-10

### fix: крипто SL min floor + оптимизация скальпинга по 12 источникам

**Проблема крипто SL:** LTC, DOT, BNB — SL distance $0.035 (0.065%!) при ATR $0.05. Позиции проскакивали SL, аудит не мог поставить (TRADING_BAD_STOPS — SL уже выше BID).

**Исправления крипто:**
- `CRYPTO_SCALP_SL_MIN_PCT = 0.005` (0.5%) — минимальный SL floor для всех крипто
- Floor применяется: в стратегиях (vwap/orb/stat_arb), при открытии ордера, в аудите
- Аудит: если SL уже пройден — **принудительное закрытие** (`audit_sl_past`)

**Оптимизация скальпинга (12 источников, 9433+ трейдов в бэктестах):**

| Параметр | Было | Стало | Источник |
|----------|------|-------|----------|
| VWAP deviation | 1.0 ATR | **2.0 ATR** | TradingView, StockSharp (95% boundary) |
| RSI confirm | 35/65 | **30/70** | Более строгий фильтр |
| Scalping SL | 1.5 ATR (VWAP) | **2.0 ATR** единообразно | Quant-Signals (оптимум 9433 трейда) |
| Scalping TP | 0.3 ATR / 5 pips | **1.5 ATR / 8 pips** | R:R 0.75:1 vs 0.2:1 |
| Trail trigger | 0.2 ATR / 3 pips | **0.6 ATR / 5 pips** | StratBase.ai backtest |
| Trail distance | 0.1 ATR / 2 pips | **0.3 ATR / 3 pips** | StratBase.ai backtest |
| Time-stop | 12 часов | **4 часа** | Scalping best practices |
| NY ORB window | до 21:00 | до **17:00** | Edge пропадает после 2.5ч |
| Max positions | 50 | **15** | Concentration risk management |
| TP commission floor | нет | **3× round-trip cost** | Учёт комиссии FxPro ($3.50/lot/side) |

**Файлы:** vwap_reversion.py, monitor.py, session_orb.py, stat_arb.py, settings.py, main.py, STRATEGIES.md

---

### feat: крипто-скальпинг — процентная система TP/SL + 11 альткоинов

**Проблема:** BTC-USD давал -867 pips/9ч потому что TP=5 pips ($5) при SL=$200+. R:R 1:40 против нас.

**Решение — процентная система для крипто:**
- TP = `1.0 × ATR` (для BTC@72K, ATR=$300 → TP=$300, ~0.42%)
- SL = `0.75 × ATR` (→ SL=$225, ~0.31%)
- R:R = **1.33:1 в нашу пользу**
- Trail trigger: `0.6 × ATR`, trail distance: `0.3 × ATR`
- Time-stop: 4 часа (вместо 12 для форекса)
- Минимальный TP: 0.2% от цены (floor)

**Добавлено 11 крипто (все проверены в cTrader):**
BTC-USD, ETH-USD, SOL-USD, XRP-USD, DOGE-USD, ADA-USD, LINK-USD, AVAX-USD, LTC-USD, BNB-USD, DOT-USD

Крипто доступно **только в скальпинге** (VWAP/ORB). Исключено из outsiders/ensemble.

Автозакрытие работает на 3 уровнях:
1. **Серверный TP/SL** на cTrader (ATR-based, процентный)
2. **Клиентский мониторинг** — trail, time-stop (процентный для крипто)
3. **Аудит** — `_ensure_broker_sl_tp` с крипто-параметрами

---

### fix: убраны убыточные пары по 9-часовому анализу

**Анализ**: 65 сделок за 9 часов, общий -937 pips. BTC-USD = 92% всех потерь.

**Убраны из DEFAULT_SYMBOLS:**
- BTC-USD — 0% WR, -867 pips (pip=$1, скальпинг невозможен)
- ZN=F — 0% WR (бонды, нет ликвидности в скальпинге)
- NQ=F — не найден в cTrader

**SCALPING_EXCLUDE_SYMBOLS (VWAP/ORB/StatArb):**
- EURJPY — 0% WR в stat_arb (5 сделок)
- GBPJPY — 40% WR но -23 pips в stat_arb

**StatArb:** пары (EURJPY, GBPJPY) и (ES=F, NQ=F) удалены из DEFAULT_PAIRS.

**docker-compose.yml:** SCAN_SYMBOLS убран — единственный источник правды DEFAULT_SYMBOLS в settings.py.

---

### fix: SCAN_SYMBOLS в docker-compose перезатирал DEFAULT_SYMBOLS

Fallback `SCAN_SYMBOLS` в docker-compose.yml включал удалённые ранее символы (EURGBP, USDCHF, NG=F, ETH-USD).
Бот торговал парами, которые были убраны из settings.py. Строка удалена.

---

## 2026-04-09

### fix: safe amend SL/TP — reconcile перед каждым amend

**Проблема:** cTrader `AmendPositionSLTPReq` — protobuf шлёт `0.0` для
неуказанных полей, cTrader интерпретирует как «удалить». Три бага:

1. `open_position()` amend только TP → затирал SL
2. `_update_broker_trailing_sl()` amend только SL → **затирал TP каждый цикл**
3. `client.amend_position_sl_tp()` не защищён от частичного обновления

**Решение:** `executor.amend_sl_tp()` теперь **всегда** делает reconcile
перед amend, получает текущие SL/TP с брокера и мержит с новыми значениями.
Невозможно случайно затереть ни одно поле.

**Файлы:** `executor.py` — `amend_sl_tp()`, `_get_broker_sl_tp()`,
убран дублирующий `_get_position_from_reconcile()`

### feat: автоматический аудит SL/TP каждый цикл

**Проблема:** если amend при открытии упал (таймаут, сеть) — позиция
навсегда оставалась без TP. Ручная расстановка — не решение.

**Решение:** `_ensure_broker_sl_tp()` — новый шаг в каждом цикле:
1. Получает все позиции с брокера через reconcile
2. Проверяет наличие SL и TP на каждой
3. Если чего-то нет — рассчитывает по стратегии и доставляет
4. Логирует: какие позиции починил, что поставил, или «Все N позиций с SL и TP ✓»

**Три уровня защиты:**
- При открытии: SL в ордере + TP amend после fill
- Каждый цикл: аудит подхватывает упавшие amend
- При amend: reconcile + merge — не затирает существующие уровни

**Файлы:** `main.py` — `_ensure_broker_sl_tp()`, порядок шагов в `_run_cycle()`

### fix: TP через amend после fill (cTrader API workaround)

**Проблема:** cTrader API игнорирует `relativeTakeProfit` на MARKET ордерах —
известная проблема, подтверждена на форуме cTrader (март 2025). SL через
`relativeStopLoss` работает, а TP — нет.

**Вторая проблема:** Первый execution event — ORDER_ACCEPTED (fill_price=0),
ORDER_FILLED приходит асинхронно. Из-за fill_price=0 TP amend пропускался.

**Решение:**
1. TP ставится через `ProtoOAAmendPositionSLTPReq` (абсолютная цена)
2. Цепочка fallback для fill_price: deal → pos.price → reconcile → entry_price_hint
3. Существующий SL сохраняется при amend
4. Скрипт `scripts/set_tp_all.py` для одноразовой расстановки TP

**Файлы:** `executor.py`, `main.py`, `scripts/set_tp_all.py`

### fix: broker PNL — sanity check executionPrice + grossProfit fallback
`301c540`

cTrader иногда возвращает некорректный `executionPrice` в deal history
(например, цена GBPJPY вместо EURJPY → +2753 pips вместо +7).
Добавлен sanity check (>5% от entry → отклонение), fallback на
`closePositionDetail.grossProfit` для расчёта пипсов.
Используется `closePositionDetail.entryPrice` как реальная цена входа.

**Файлы:** `executor.py`, `main.py`

### fix: broker trailing для ensemble/outsiders теперь ATR-based
`f5087ae`

Брокерский trailing использовал фиксированные 3 pips — на волатильных
инструментах (XPTUSD, metals) cTrader закрывал позицию слишком рано.
Теперь логика совпадает с клиентской: `max(0.15×ATR, 3 pips)`.
STRATEGIES.md: ensemble добавлен в таблицу серверных TP/SL, порог входа
уточнён до 4/5 (MIN_STRENGTH=0.7 → strength 3/5=0.6 не проходит).

**Файлы:** `main.py`, `STRATEGIES.md`

### fix: atomic TP/SL in order + ensemble strategy TP + ATR-based exits
`21d2296`

Переход на атомарное выставление SL/TP через `relativeStopLoss`/`relativeTakeProfit`
в `ProtoOANewOrderReq` — TP/SL ставятся вместе с ордером, а не отдельным amend.
Ensemble теперь получает TP (`max(0.5×ATR, 10 pips)`). Outsiders/ensemble exit
использует ATR-мультипликаторы. Металлы (GC=F, HG=F, PL=F) возвращены.
Scalping accordion: TP=5, trigger=3, distance=2 pips.

**Файлы:** `main.py`, `executor.py`, `client.py`, `monitor.py`, `settings.py`, `cost_model.py`

### Auto-reconnect cTrader on connection drop
`1475fd6`

Автопереподключение к cTrader при обрыве TCP/TLS. Обработка
`AccountDisconnectEvent` и `TokenInvalidatedEvent` с автообновлением
access token через refresh token. Heartbeat каждые 10 сек.

**Файлы:** `client.py`

---

## 2026-04-08

### Document server-side TP/SL + dynamic trailing
`9b32fa6`

Документация серверного TP/SL в STRATEGIES.md: таблица уровней по стратегиям,
описание атомарного выставления через cTrader API.

### Leaders: server-side TP (50 pips) + dynamic trailing SL
`37db250`

Leaders получает серверный TP=50 pips. Динамический trailing SL на cTrader:
каждый цикл пересчитывается и обновляется через `amend_position_sl_tp`.

### Return metals, accordion 3/5, fix SL in sync
`8a9279d`

Возврат металлов (GC=F, HG=F, PL=F). Scalping: TP=5, trigger=3, distance=2.
Исправлен SL при синхронизации старых позиций.

### Server-side TP on cTrader + detect broker closures
`7645bf8`

Серверный TP для outsiders (10 pips), скальпинга (5 pips).
Детект закрытий на стороне брокера: каждый цикл сверка DB vs cTrader.

### Remove/add metals, scalping TP/trailing
`12d4774` `cf1da6c`

Итерации с металлами (убраны, потом возвращены) и настройка
take-profit/trailing для скальпинговых стратегий.

### Stats: P&L in USD, correct pip_value
`dda0a41` `18b8cc8` `b294c99` `88df80a`

Переход статистики с пипсов на USD. Использование реального `broker_volume`
и `contract_size` из cTrader для точного расчёта pip_value.
P&L берётся из cTrader API (unrealized PnL endpoint).

### Remove silver, prevent oversized positions
`b0cf74c` `515960b`

Серебро (SI=F) убрано. Защита от открытия слишком больших позиций
при подтягивании к `min_volume` cTrader.

---

## 2026-04-07

### Подключение cTrader демо-счёта

Начальный депозит $500 + пополнение $1000 = **$1500 итого**.

### feat: expand market coverage
`1097d2d`

Расширение списка инструментов: индексы (ES=F, NQ=F), крипто (BTC, ETH),
облигации (ZN=F), дополнительный форекс и commodities.

### cTrader API integration
`41578cb` → `0343a5e`

Полный цикл интеграции cTrader Open API:
- TLS handshake + service-identity
- Авторизация приложения и аккаунта
- Автообнаружение `ctidTraderAccountId`
- Маппинг yfinance → cTrader символов (статический + динамический prefix)
- Исполнение ордеров для всех стратегий
- Синхронизация бумажных позиций с брокером
- Reconciliation при старте + защита от зависших сделок
- Округление SL/TP до digits символа
- Закрытие с реальным volume из cTrader

### Broker reconciliation + stuck trade protection
`47cf771` `8c48149`

Сверка открытых позиций при старте. Если позиция есть в DB но нет
на cTrader — закрытие как `broker_closed`. Использование
`tradeData.volume` из `ProtoOAPosition`.

---

## 2026-04-09 (вечер) — Risk:Reward rebalance + XAUUSD exclusion

### XAUUSD исключён из outsiders/ensemble
Золото (GC=F) исключено из стратегий outsiders и ensemble из-за
неблагоприятного R:R на высоковолатильном инструменте (два крупных
стопа -$6.62 и -$10.68 за 2 часа при мелких TP). Золото остаётся
доступным для Leaders (copy-trading) и скальпинга.

Добавлен `OUTSIDERS_EXCLUDE_SYMBOLS` — frozenset фильтрации в
`outsiders.py` (process_signals) и `main.py` (ensemble-блок).

### Risk:Reward → 2:1 (Вариант A)
Прежний R:R ≈ 4:1 (SL 2×ATR vs TP 0.5×ATR) давал большие убытки
при стопе и маленькие выигрыши при TP. Параметры изменены:

| Параметр | Было | Стало |
|----------|------|-------|
| SL (confirmed) | 2.0×ATR | 1.5×ATR |
| TP | max(0.5×ATR, 10 pips) | max(0.75×ATR, 10 pips) |
| Trail trigger | max(0.3×ATR, 5) | max(0.4×ATR, 5) |
| Trail distance | max(0.15×ATR, 3) | max(0.2×ATR, 3) |

Итоговый R:R ≈ 2:1 — при win-rate >33% стратегия прибыльна.

Затронутые файлы: `outsiders.py`, `monitor.py`, `main.py`, `STRATEGIES.md`.
