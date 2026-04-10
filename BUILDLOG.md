# Build Log

Лог изменений FX Pro Bot с момента подключения демо-счёта cTrader (07.04.2026).

---

## 2026-04-10

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
