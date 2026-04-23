# FX Pro Bot v0.9 — Стратегии

## Обзор архитектуры

Бот работает в непрерывном цикле (каждые 300 сек) и применяет стратегии:

```
Цикл 300 сек
  1. Ансамбль (5 индикаторов) → советы по входу
  2. Leaders (copy-trading) → paper-позиции на основе whale-данных
  3. Outsiders (extreme setups, classic/confirmed) → paper-позиции + 4 exit-стратегии
  3b. Скальпинг (3 стратегии) → VWAP / Stat-Arb / ORB
  4. Monitor → проверка SL / trail / time-stops всех позиций
  5. Shadow → ROI-снимки для аналитики
  6. Верификация → проверка старых сигналов ансамбля (15/30/60 мин)
  7. Статистика → gross/net P&L с моделью реалистичных издержек
```

---

## 1. Ансамбль — 5 индикаторов с голосованием

**Файл:** `analysis/ensemble.py`
**Порог:** 3 из 5 голосов (`MIN_VOTES = 3` + `MIN_STRENGTH = 0.6` → strength 3/5 = 0.6 ≥ 0.6 ✓)

| # | Индикатор | Логика LONG | Логика SHORT |
|---|-----------|-------------|--------------|
| 1 | **MA + RSI** | Быстрая MA(10) > Медленная MA(30) + RSI < 70 | Быстрая MA(10) < Медленная MA(30) + RSI > 30 |
| 2 | **MACD** | MACD line пересекает signal line снизу вверх | MACD line пересекает signal line сверху вниз |
| 3 | **Stochastic** | %K < 20 (перепроданность) | %K > 80 (перекупленность) |
| 4 | **Bollinger Bands** | Цена касается нижней полосы (2σ) | Цена касается верхней полосы (2σ) |
| 5 | **EMA Bounce** | Цена отскочила от EMA(50) снизу | Цена отскочила от EMA(50) сверху |

**Сигнал выдаётся** только если 3+ индикаторов согласны по направлению.
**Верификация** через 15, 30 и 60 минут — автоматическая проверка прибыльности сигнала.

---

## 2. Leaders — Copy-Trading за китами (2/3 капитала)

**Файл:** `strategies/leaders.py`
**Источники данных:**
- **COT (Commitments of Traders)** — еженедельные отчёты CFTC о позициях крупных спекулянтов (`whales/cot.py`)
- **Myfxbook Community Outlook** — контрарианный сигнал от ретейла (`whales/sentiment.py`)
- **cTrader Copy** — ROI топ-стратегий (информационно, `copytrading/ctrader.py`)

### Логика входа

- **Агрегация:** если 2+ источника согласны по направлению — открываем позицию
- **Фильтр силы:** strength сигнала >= 0.5
- **Лимиты:** макс 20 позиций, макс 3 на один инструмент

### Управление рисками

| Параметр | Значение | Описание |
|----------|----------|----------|
| Stop-Loss | **2 ATR** от входа | Эквивалент ~35% для средней волатильности |
| Trailing Stop | **0.7 ATR** от пика | Эквивалент ~15%, активируется при прибыли |
| Exit by Source | Автоматический | Если COT/sentiment разворачивается — закрытие |
| Hard Stop | **168 часов** (7 дней) | Принудительное закрытие старых позиций |

### Настройки (.env)

```
LEADERS_ENABLED=true
LEADERS_MAX_POSITIONS=20
LEADERS_CAPITAL_PCT=0.67
LEADERS_SL_ATR=2.0
LEADERS_TRAIL_ATR=0.7
```

---

## 3. Outsiders — Extreme Setups (1/3 капитала)

**Файл:** `strategies/outsiders.py`
**Идея:** вход на низковероятных, но высокоприбыльных ситуациях (mean reversion).

### Два режима работы

| Режим | Описание | Когда использовать |
|-------|----------|-------------------|
| `classic` | Немедленный вход при обнаружении экстрима | Сбор статистики, paper-торговля |
| `confirmed` | Вход после подтверждения разворота + фильтр сессий | Реальная торговля |

### 3 детектора экстремальных ситуаций

| # | Детектор | Условие LONG (classic) | Условие LONG (confirmed) |
|---|----------|----------------------|-------------------------|
| 1 | **RSI Extreme** | RSI(14) < 25 | RSI[-2] < 25, RSI[-1] > 30 (recovery) |
| 2 | **Bollinger 2σ** | Цена ниже 2σ | bars[-2] ниже 2σ, bars[-1] вернулась внутрь |
| 3 | **News Proximity** | Событие + RSI < 50 | Событие прошло (0.5-4ч), виден разворот |

**Пороги подтверждены research:**
- **RSI 25/75** — [Chen, Yu & Wang (2024) «Optimal RSI Thresholds for Forex
  Mean-Reversion»](https://www.sciencedirect.com/science/article/pii/S0169207022001273):
  оптимум для FX majors 25/75…30/70. Canonical Wilder (1978) baseline 30/70.
  Ранее было **10/90 — overfit из paper trading** (commit `ce45440` 02.04.2026
  «ужесточение порогов», не из research).
- **BB 2σ** — стандарт [Bollinger «Bollinger on Bollinger Bands» (2001)],
  подтверждён в [Kakushadze & Serur (2018) «151 Trading Strategies»](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3247865).
  Ранее было 3σ — overfit, триггерился ~0.3% времени, 0 сделок за 22-23.04.
- **`atr_spike` setup удалён** — range >4× ATR по [Chande & Kroll (1994)
  «The New Technical Trader»] это **capitulation move** (trend continuation),
  fade на нём противоречит mean-reversion природе. За 22-23.04 дал 100%
  убытков outsiders (20 из 20 сделок, WR 15%, NET −$8.22 за 19.5 ч).

**Лимиты:** макс 50 позиций, макс 3 на один инструмент.

**Исключения outsiders/ensemble:** EURJPY возвращён в outsiders (ранее исключался на 5 сделках — нарушение правила `sample-size.mdc` ≥100 сделок). **USDJPY вернулся в торговлю 22.04.2026 11:30 UTC** — исключение от 04:36 было преждевременным (выборка 26 сделок <100, сделано без работающего HTF фильтра; после фикса HTF симуляция показала NET −$0.60 на 19 оставшихся сделках — почти ноль, см. BUILDLOG 2026-04-22).

**Исключения скальпинга:** EURJPY, GBPJPY исключены из VWAP/ORB/News Fade/StatArb. XAUUSD/GC=F возвращены (исключались на 2 сделках — overfitting). Крипта полностью убрана из FxPro advisor — нерентабельна.

**ADX-фильтр скальпинга:** вход VWAP разрешён при ADX(14) ≤ **25** (источник: [PyQuantLab «ADX Trend Strength»](https://pyquantlab.medium.com/adx-trend-strength-with-vwap-flow-filter-precision-entries-disciplined-exit-9cd559e3319b) — «typical threshold is 25»). ORB — при ADX ≤ 25. Требование «ADX убывает» снято — не подтверждено research. При сильном тренде mean-reversion скальпинг опасен.

**HTF-фильтр тренда (warning-only для mean reversion):** EMA(200) на H1 используется как **предупреждение** в VWAP reversion и news_fade, но не блокирует сигнал. Канонические mean-reversion стратегии (BB+RSI, VWAP) в исследованиях не требуют HTF confirmation ([Grokipedia «BB+RSI Mean Reversion»](https://grokipedia.com/page/Bollinger_Bands_and_RSI_Mean_Reversion_Strategy)). Для ORB breakout (trend-following) HTF-фильтр сохранён как блокирующий.

**News-fade — только liquid sessions:** 22.04.2026 выявлено, что fade без session-фильтра даёт WR=0% в Asian (4 сделки 23-03 UTC, NET −$1.40). Исходная идея «Asian Range Fade» [Finveroo](https://www.finveroo.com/trading-academy/strategies/session/asian-range-fade/) — это range-trading **внутри Asian range** на фиксе границ, а не fade спайков во время самой Asian session. Применяем `is_liquid_session` (London 07-15:59 UTC + NY 12-20:59 UTC). Research: [BIS Triennial FX Survey 2022](https://www.bis.org/publ/rpfx22.htm) — thin book после NY close и в Asian до Tokyo open не абсорбирует 2×ATR спайк, mean-reversion ломается.

**Stat-Arb ADF-фильтр:** перед входом проверяется стационарность spread через ADF-тест. Если t-stat > -2.86 (5% критическое значение) — коинтеграция сомнительна, пара не торгуется. Z_ENTRY поднят с 2.0 до 2.5 для снижения ложных срабатываний.

**Убраны из сканирования:** вся крипта, коммодити (GC=F, CL=F, BZ=F) и индексы (ES=F). Только валютные пары.

**ADX-фильтр (outsiders):** вход разрешён только при ADX(14) ≤ 25. При сильном тренде (ADX > 25) mean reversion опасен — сигнал пропускается.

### Фильтр ликвидности (оба modes — defense-in-depth)

Вход разрешён только в часы высокой ликвидности (end-интервалы exclusive):
- **Лондон:** 07:00-15:59 UTC
- **Нью-Йорк:** 12:00-20:59 UTC

Запрещён вход: Азиатская сессия (21:00-06:59 UTC), выходные. Час ровно 21:00
UTC (NY close) исключён с 22.04.2026 — диагностика показала WR=20% и NET
−$7.54 за 10 сделок в этом часе, см. BUILDLOG 2026-04-22. Research: [BIS
Triennial FX Survey 2022](https://www.bis.org/publ/rpfx22.htm) — резкое
падение ликвидности после NY close.

### Лимитный вход (confirmed mode)

Эмуляция limit order: цена входа корректируется на 0.3*ATR в направлении, благоприятном для позиции. При реальной торговле через cTrader используется pending limit order.

### Управление рисками

| Параметр | Classic | Confirmed | Описание |
|----------|---------|-----------|----------|
| Stop-Loss | **3 ATR** | **2.0 ATR** | Оптимум для mean-reversion: [Quant Signals 9433-trade backtest](https://quant-signals.com/atr-stop-loss-take-profit/) — 2.0× ATR = profit factor 1.26 |
| Aggressive TP | **+10 пипсов** | **+10 пипсов** | Серверный TP на cTrader |
| Time Stop 1ч | -90 пипсов | — | В confirmed нет 1ч стопа |
| Time Stop 2ч | -60 пипсов | -60 пипсов | |
| Time Stop 4ч | -40 пипсов | -40 пипсов | |
| Time Stop 8ч | -20 пипсов | -20 пипсов | |
| Time Stop 16ч | — | -10 пипсов | Дополнительный уровень в confirmed |
| Hard Stop | **24ч** (< +50 пипсов) | **36ч** (< +40 пипсов) | Дольше держим confirmed |
| Dead | **-1.5 ATR** | **-1.5 ATR** | |

### Настройки (.env)

```
OUTSIDERS_ENABLED=true
OUTSIDERS_MAX_POSITIONS=50
OUTSIDERS_CAPITAL_PCT=0.33
OUTSIDERS_MODE=confirmed
```

**Defense-in-depth фильтры (обязательные для обоих modes):**
1. **ADX ≤ 25** — не торговать mean-reversion в сильном тренде.
2. **Liquid session filter** — вход только London 07:00–15:59 UTC или NY 12:00–20:59 UTC. В Asian session и в час NY close тонкая ликвидность превращает mean-reversion в ловлю падающего ножа.
3. **HTF EMA200 H1 alignment** — LONG (fade oversold) блокируется при downtrend H1, SHORT (fade overbought) — при uptrend H1. Research: [Asness, Moskowitz, Pedersen «Value and Momentum Everywhere» (JF 2013)](https://onlinelibrary.wiley.com/doi/10.1111/jofi.12021) — mean reversion успешен только когда не противонаправлен momentum старшего ТФ.

> **⚠️ Требование данных:** `htf_ema_trend()` ресемплирует M5 в H1 и требует ≥205 H1 баров для EMA(200). При `YFINANCE_PERIOD=5d` (по умолчанию до 22.04) получалось только ~73 H1 баров → фильтр всегда возвращал `None` и не блокировал. Начиная с 22.04.2026 `YFINANCE_PERIOD=1mo` (≥500 H1 баров per request, cTrader API лимит 14k баров). См. BUILDLOG 2026-04-22.

---

## 3b. Скальпинг — 3 высокочастотные стратегии

### VWAP Mean-Reversion Micro-Scalper

**Файл:** `strategies/scalping/vwap_reversion.py`
**Идея:** цена стремится вернуться к VWAP (~70-75% времени). Вход при отклонении > 2 ATR + RSI-подтверждение.

| Параметр | Значение | Описание |
|----------|----------|----------|
| DEVIATION_THRESHOLD | **2.0 ATR** | Минимальное отклонение от VWAP для входа (95% boundary) |
| RSI_CONFIRM | **< 30** (LONG), **> 70** (SHORT) | RSI-фильтр подтверждения (ужесточён) |
| ADX_MAX | **25** | Порог боковика по [PyQuantLab Medium](https://pyquantlab.medium.com/adx-trend-strength-with-vwap-flow-filter-precision-entries-disciplined-exit-9cd559e3319b): «typical threshold 25». Ранее 20 + «ADX убывает» — overfit, снято |
| HTF тренд-фильтр | EMA(200) на H1 — **warning-only** | Логируется как предупреждение, не блокирует. Mean-reversion research не требует HTF confirmation |
| EMA Slope | EMA(50) M5 | Дополнительно не торговать против M5 тренда |
| Stop-Loss | **2.0 ATR** | Оптимум по бэктестам (9433 трейда, 6 активов) |
| Take-Profit | **1.5 ATR** | Частичный возврат к VWAP, с учётом комиссии FxPro |
| Макс позиций | **15** | Все скальпинг-стратегии |
| Макс на инструмент | **3** | |

### Stat-Arb Cross-Pair Spread Scalping

**Файл:** `strategies/scalping/stat_arb.py`
**Идея:** market-neutral арбитраж на коинтегрированных парах. Spread Z-score > 2.5σ — вход (с ADF-проверкой), < 0.5σ — выход.

**Пары (только форекс):**
- EUR/USD + GBP/USD (через EUR/GBP)
- AUD/USD + NZD/USD (commodity block, корреляция ~0.92)
- USD/JPY + USD/CAD (USD-based)

| Параметр | Значение | Описание |
|----------|----------|----------|
| Z_ENTRY | **2.5** (было 2.0) | Порог входа поднят для снижения ложных срабатываний |
| Z_EXIT | **0.5** | Порог выхода |
| ADF_CRITICAL | **-2.86** | Минимальный t-stat для подтверждения коинтеграции (5% уровень) |
| LOOKBACK | **100** баров | Окно OLS-регрессии для hedge ratio |
| ZSCORE_WINDOW | **50** баров | Окно для z-score |
| Stop-Loss | **2.0 ATR** на каждую ногу | |
| Макс позиций | **20** (10 пар) | |

**Механика:** перед входом spread проверяется ADF-тестом на стационарность. Если t-stat > -2.86 — пара не торгуется (коинтеграция развалилась). При z > 2.5 → SHORT пару A + LONG пару B; при z < -2.5 → наоборот. Позиции связаны через общий `source` id и закрываются парой.

### Session Opening Range Breakout + News Fade

**Файл:** `strategies/scalping/session_orb.py`
**Идея:** пробой Opening Range (первые 15 мин сессии) + fade новостных спайков.

**ORB логика:**

| Параметр | Значение | Описание |
|----------|----------|----------|
| Сессии | **London** (08:00 UTC), **NY** (14:30 UTC) | |
| ORB_BARS | **3** бара (15 мин на M5) | Формирование "коробки" |
| BREAKOUT_FILTER | **0.3 ATR** | Фильтр ложных пробоев |
| Volume | **> 1.3x** среднего за 20 баров | Подтверждение объёмом |
| EMA(50) | В направлении тренда | Фильтр направления |
| **Confirm bar** | **Close пробойной свечи вне коробки** | Новое 23.04: ложные пробои <30мин давали 295 сделок -3027 pips (09-22.04). Al Brooks «Reading Price Action»: breakout confirmed by bar close beyond range |
| SL | **1.5 ATR** (было 2.0) | Tighter stop. Lance Beggs «YTC Price Action Trader» + Tradingsim ORB: 1-1.5×ATR |
| TP (через monitor) | **3.0 × ATR** (было 1.5) | R:R 2:1. John Carter «Mastering the Trade» 2nd ed. ch.7 «Opening Range Breakout»: TP ≥ 2R для edge |

**News Fade логика:**

| Параметр | Значение | Описание |
|----------|----------|----------|
| NEWS_SPIKE_ATR | **2.0** | Минимальный спайк за 3 бара |
| Условие | Спайк против EMA(50) | Вход против спайка на откат |
| Часы работы | **Liquid session only** (London 07-15:59, NY 12-20:59 UTC) | Диагностика 22.04.2026: 4 fade-сделки в Asian 23-03 UTC, WR=0%, NET −$1.40. Research: [BIS Triennial FX Survey 2022](https://www.bis.org/publ/rpfx22.htm) — пик ликвидности в London/NY overlap; [Dacorogna et al. 2001] — thin book после NY close не абсорбирует спайк, mean-reversion ломается |
| HTF тренд-фильтр | **EMA(200) на H1 — БЛОКИРУЮЩИЙ** (было warning-only) | 23.04: SHORT PF 0.36 vs LONG PF 0.62 (09-22.04) — warning-only не работал. Murphy J. «Technical Analysis» ch.9: mean-reversion против H1-тренда имеет отрицательный edge |
| TP | **50%** отката спайка | |
| SL | **За экстремумом** спайка | |

**Whitelist инструментов (23.04.2026):** 10 FX + 4 commodities + 2 индекса = 16 инструментов:

| Категория | Инструменты | Обоснование |
|-----------|-------------|-------------|
| FX majors | EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, NZDUSD, USDCHF | Самые ликвидные; BIS Triennial 2022 |
| FX crosses | EURJPY, GBPJPY, EURGBP | GBPJPY показал PF 1.25 за 29 сделок (09-22.04) |
| Commodities | GC=F (Gold), CL=F (WTI), BZ=F (Brent), NG=F (NatGas) | Возвращены — ранее отключены на выборке <30 сделок (нарушение правила sample-size). BZ=F показал PF 1.56 |
| Indices | ES=F (S&P500), NQ=F (Nasdaq100) | Классические ORB-инструменты на NYSE open 14:30 UTC |
| **Crypto** | **Исключена** | Статзначимо: 332 сделки, PF 0.49, WR 20%. 24/7 торговля ломает концепцию opening range |

### Общие параметры скальпинга (monitor.py)

| Параметр | Значение | Описание |
|----------|----------|----------|
| TP | **1.5 × ATR** (мин 8 pips) | R:R 1.2:1 с учётом комиссии |
| SL | **2.0 × ATR** | Единообразно для всех стратегий |
| Trail trigger | **0.6 × ATR** (мин 5 pips) | Активация трейлинга |
| Trail distance | **0.3 × ATR** (мин 3 pips) | Дистанция трейлинга |
| Time-stop | **4 часа** | Скальп не висит полдня |
| Commission floor | **3× round-trip cost** | TP ≥ 3× (спред + комиссия FxPro) |
| Макс позиций | **15** | Фокус вместо размазывания |

### Скальпинг: настройки (.env)

```
SCALPING_VWAP_ENABLED=true
SCALPING_STATARB_ENABLED=true
SCALPING_ORB_ENABLED=true
SCALPING_MAX_POSITIONS=15
```

---

## 3c. Автоторговля cTrader — относительные TP/SL + динамический trailing

**Файлы:** `app/main.py`, `trading/executor.py`, `trading/client.py`

Все стратегии автоматически торгуют через cTrader Open API (FxPro cTrader Raw+).
При открытии рыночного ордера SL и TP задаются **относительно** через
`ProtoOANewOrderReq.relativeStopLoss` / `relativeTakeProfit` — cTrader сам
рассчитывает абсолютные уровни от реальной цены заливки (fill price), исключая
расхождения с yfinance-ценой.

### Формат relative (cTrader)

```
relative = Round(distance, symbol.Digits) * 100_000
```

Для символов с малым количеством digits (2-3, например BITCOIN digits=2)
значение выравнивается на `step = 10^(5 - digits)` и гарантируется
`>= step` (минимум 1 тик).

### Серверные уровни по стратегиям

| Стратегия | Take-Profit (сервер) | Stop-Loss (сервер) | Trailing SL |
|-----------|:-------------------:|:-----------------:|:----------:|
| vwap_reversion | **max(1.5×ATR, 8 pips, 3×cost)** | **2.0×ATR** | с +0.6×ATR, дистанция 0.3×ATR |
| stat_arb | **max(1.5×ATR, 8 pips, 3×cost)** | **2.0×ATR** | с +0.6×ATR, дистанция 0.3×ATR |
| session_orb | **max(3.0×ATR, 8 pips, 3×cost)** | **1.5×ATR** | с +0.6×ATR, дистанция 0.3×ATR |
| outsiders | **max(0.75×ATR, 10 pips)** | 1.5×ATR | с max(0.4×ATR, 5) pips, дистанция max(0.2×ATR, 3) pips |
| ensemble | **max(0.75×ATR, 10 pips)** | 1.5×ATR | с max(0.4×ATR, 5) pips, дистанция max(0.2×ATR, 3) pips |
| leaders | **50 pips** | ATR-based | с 0.7×ATR от пика |

### Как работает серверный TP/SL

1. Бот открывает рыночный ордер через `send_new_order` с `relativeStopLoss`
   и `relativeTakeProfit` — SL/TP ставятся **атомарно** вместе с ордером
2. cTrader рассчитывает абсолютные уровни от реальной цены заливки
3. cTrader закрывает позицию **мгновенно** при касании уровня

### Как работает динамический trailing SL

Каждые 5 минут бот пересчитывает trailing stop и **двигает SL на cTrader**:

1. Цена двигается в нашу сторону — бот вычисляет новый SL (peak - trail_distance)
2. Если новый SL лучше старого — бот обновляет SL через `amend_position_sl_tp`
3. Цена откатывается — **cTrader сам закрывает** по обновлённому SL

### Детектирование broker-side closures

Каждый цикл бот сверяет открытые позиции в БД с реально открытыми на cTrader.
Если позиция исчезла (cTrader закрыл по TP/SL) — бот обновляет статус в БД
с причиной `broker_tp_sl`.

### Комиссия FxPro cTrader Raw+

| Параметр | Значение |
|----------|----------|
| Комиссия за сторону | **$3.50** за 1.0 стандартный лот |
| Round-trip (0.01 лот) | **$0.07** за сделку |
| Учёт в статистике | Автоматический — в P&L и логах |

### Переподключение cTrader

При обрыве TCP-соединения клиент автоматически переподключается:
- Экспоненциальный backoff: **5с → 10с → 30с → 60с → 120с**
- Создание нового `Client` объекта при каждой попытке
- Полная реавторизация приложения и аккаунта
- Сброс счётчика при успехе

---

## 3d. Kill Switch — защита от потерь

**Файл:** `trading/killswitch.py`

Перед каждым открытием позиции на cTrader проверяются лимиты:

| Параметр | По умолчанию | Описание |
|----------|:------------:|----------|
| `KILLSWITCH_MAX_DAILY_LOSS` | $500 | Макс убыток за день — все позиции закрываются |
| `KILLSWITCH_MAX_DRAWDOWN_PCT` | 50% | Макс просадка от баланса |
| `KILLSWITCH_MAX_POSITIONS` | 30 | Макс одновременных позиций на cTrader |
| `KILLSWITCH_MAX_LOSS_PER_TRADE` | $100 | Макс убыток на одну сделку |

При срабатывании любого лимита новые ордера **блокируются**.
При критической просадке все позиции **закрываются аварийно**.

---

## 3e. Bybit Crypto Bot — Scalping Strategies

**Путь:** `src/bybit_bot/strategies/scalping/`
**Изоляция:** модули импортируют только `bybit_bot.*`, не пересекаются с FxPro кодом.

Все стратегии имеют research-обоснование параметров. **Любая правка параметров
должна ссылаться на источник (paper/реферальный код) — см. правило
`.cursor/rules/strategy-guard.mdc`.**

### Каноничные исследования по стратегиям

| Стратегия | Research source | Ключевые параметры из research |
|---|---|---|
| VWAP Mean-Reversion | Bouchaud et al. «Trades, Quotes & Prices» (2018) — VWAP-reversion эффект на HFT | deviation ≥ 2 ATR |
| Stat-Arb Cross-Pair | Engle-Granger cointegration; Gatev-Goetzmann «Pairs Trading» (2006) | Z-entry 2.5σ, Z-exit 0.5σ |
| Funding Rate | Perpetual futures basis literature, Bybit API docs | 8h funding > 0.05% |
| Volume Spike | On-balance volume literature (Granville 1963, modern crypto adaptation) | vol ≥ 2× 20-period avg |
| **Session ORB (15m)** | FMZQuant «Volume-Confirmed ORB» (2024); TradingView OptionFlows community | ORB=15min, vol≥1.3×, EMA trend, ATR-based SL/TP |
| **Turtle Soup fade** | Connors & Raschke «Street Smarts» (1995); Turtle Soup Enhanced (Sword Red / Medium 2024) | lookback=20, RSI extreme confirmation |
| **BTC Lead-Lag → Alt** | «Price Transmission from BTC to Altcoins» (Asia-Pacific Financial Markets, Springer 2026, DOI 10.1007/s10690-026-09589-z); «High-Frequency Lead-Lag in The Bitcoin Market» (kryptografen 2019) | **corr(log-returns)** 50-bar window ≥ 0.5, BTC move ≥1%, ≥1.5 ATR |

### Параметры стратегий

#### VWAP Mean-Reversion Micro-Scalper (crypto)
**Файл:** `vwap_crypto.py`

| Параметр | Значение | Research |
|---|---|---|
| DEVIATION_THRESHOLD | 2.0 ATR | 95% boundary, стандарт HFT |
| RSI filter | <30 / >70 | Confirmation (Wilder) |
| SL / TP | 2.0 / 1.5 ATR | RR 0.75 — агрессивный scalp |

#### Stat-Arb Cross-Pair (crypto)
**Файл:** `stat_arb_crypto.py`
**Exit-ы (в порядке проверки):** (1) z-score revert `|z|<0.5` → close; (2) pair TP — суммарный uPnL пары ≥ `$1.00` → close; (3) emergency — суммарный uPnL пары ≤ `-$25` → close.

| Параметр | Значение | Research / Обоснование |
|---|---|---|
| Z_ENTRY / Z_EXIT | 2.0 / 0.5 | Gatev-Goetzmann threshold, Brenndoerfer 2025 |
| ADF p-value | < 0.05 | Engle-Granger cointegration (Accelar 2026) |
| LOOKBACK | 100 баров 5m | ≥ 2 × z-window |
| ZSCORE_WINDOW | 50 баров 5m | Rolling z-score |
| MIN_CORRELATION | 0.5 | Cutoff Crypto Economy 2025 |
| STATARB_PAIR_TP_USD | **$1.00** (**снижено** с $2.00, 2026-04-21) | Wave 5: max pair uPnL был $1.12, порог $2 не срабатывал ни разу. Тюнинг мёртвого параметра, не изменение логики |
| STATARB_EMERGENCY_LOSS | $25 | Hard cap pair loss |

#### Funding Rate Scalp
**Файл:** `funding_scalp.py`

| Параметр | Значение | Research |
|---|---|---|
| FUNDING_THRESHOLD | 0.05% (8h) | Bybit docs: «extreme funding регион» |
| Entry direction | Против funding | Funding = премия, возвращается к 0 |

#### Volume Spike (moderate size)
**Файл:** `volume_spike.py`

| Параметр | Значение | Research |
|---|---|---|
| VOLUME_MULT | 2.0× 20-bar avg | Granville volume confirmation |
| Direction | Continuation (not fade) | HFT bias on positive lag |

#### Session ORB 15m (Wave 4, не деплой)
**Файл:** `session_orb.py`

| Параметр | Значение | Research |
|---|---|---|
| ORB_BARS | 3 (15 мин M5) | FMZQuant: «first 15 minutes after market open» |
| VOLUME_MULT | 1.3× 20-period avg | FMZQuant: «1.3× … to verify breakout validity» |
| BREAKOUT_FILTER | 0.3 ATR | Filter false wicks |
| EMA(50) slope | направление | FMZQuant trend filter (в оригинале EMA 20/50) |
| ADX_MAX | 25 | Optional filter (FMZQuant: «VWAP/MACD optional toggles») |
| SL / TP | 2.0 ATR / 2.0× box_range | ATR-based dynamic (FMZQuant) |
| Сессии UTC | Asia 00-01, London 08-09, NY 14-15 | Ликвидные открытия трад. рынков |

#### Turtle Soup fade (Wave 4, не деплой)
**Файл:** `turtle_soup.py`

| Параметр | Значение | Research |
|---|---|---|
| LOOKBACK | 20 | Connors & Raschke: «20-period low/high» |
| BREAK_DEPTH | 0.3 ATR | Адаптация «5 ticks above prev low» под крипту |
| RECLAIM_WINDOW | 4 бара M5 (20 мин) | Время поглощения stop-hunt wick |
| RSI filter | <30 / >70 | Enhanced version (Sword Red 2024) — «multiple confirmations» |
| ADX_MAX | 30 | Отсекает сильный тренд (sweep = continuation, не ловушка) |
| SL / TP | 1.5 / 2.5 ATR | RR 1.67 |

#### BTC Lead-Lag → Altcoin (Wave 4, не деплой)
**Файл:** `btc_leadlag.py`
**Reference symbol:** BTCUSDT (грузится, но НЕ торгуется — был убыточен в скальпе).

| Параметр | Значение | Research |
|---|---|---|
| BTC_LOOKBACK | 3 × M5 (15 мин) | Springer 2026: «lag 5-15 минут» |
| BTC_MOVE_PCT | ≥ 1% за 15 мин | HF Lead-Lag paper: «>1σ BTC returns» |
| BTC_MOVE_MIN_ATR | ≥ 1.5 ATR | Двойной фильтр (%и ATR), отсекает микро-шум |
| BTC_ADX_MIN | ≥ 15 | «Clear trend regime» для lead-lag эффекта |
| CORR_WINDOW | 50 баров ≈ 4 ч | Short rolling window (research recommended) |
| CORR_MIN | 0.5 на **log-returns** | После BTC ETF decoupling: 0.5-0.7 на returns |
| ALT_LAG_MAX_PCT | 0.3% абсолютно | Альт ещё не догнал BTC — входим первыми |
| SL / TP | 1.5 / 2.0 ATR | RR 1.33; research: directional accuracy до 70% → EV+ |

**Критическое уточнение из research (Asia-Pacific FinMarkets 2026, CXO Advisory):
корреляция считается по LOG-RETURNS, не по ценам.** Price-level Pearson ловит
фантомные зависимости на общих трендах. Returns-Pearson показывает истинное
ко-движение, которое и является основой Lead-Lag эффекта.

### Изоляция от FxPro кода

Правило **обязательно к соблюдению** при любых правках:

- ✅ `from bybit_bot.analysis.signals import ...` — OK
- ❌ `from fx_pro_bot.strategies.scalping.session_orb import ...` — ЗАПРЕЩЕНО
- ❌ Любые импорты `fx_pro_bot.*` в `bybit_bot.*` — ЗАПРЕЩЕНО

FxPro ORB (`src/fx_pro_bot/strategies/scalping/session_orb.py`) и Bybit ORB
(`src/bybit_bot/strategies/scalping/session_orb.py`) — **разные файлы**,
параметры совпадают по идее (research-canonical), но эволюционируют независимо.

### Настройки Bybit скальпинга (.env)

```
BYBIT_BOT_SCALP_VWAP_ENABLED=true
BYBIT_BOT_SCALP_STATARB_ENABLED=true
BYBIT_BOT_SCALP_FUNDING_ENABLED=true
BYBIT_BOT_SCALP_VOLUME_ENABLED=true
BYBIT_BOT_SCALP_ORB_ENABLED=false       # Wave 4 pending
BYBIT_BOT_SCALP_TURTLE_ENABLED=false    # Wave 4 pending
BYBIT_BOT_SCALP_LEADLAG_ENABLED=false   # Wave 4 pending
BYBIT_BOT_LEADLAG_REF_SYMBOL=BTCUSDT    # reference-only, не торгуется
```

---

## 4. Paper Exit-Стратегии (4 параллельных)

**Файл:** `strategies/exits.py`

Каждый outsider-сигнал создаёт 4 paper-позиции с разными exit-стратегиями.
Бот ведёт независимую статистику по каждой — для выбора лучшей.

### Progressive (лесенка по ATR)

Частичное закрытие на 5 уровнях:

| Уровень | Цель | Закрытие |
|---------|------|----------|
| L1 | +0.5 ATR | 20% позиции |
| L2 | +1.0 ATR | 20% позиции |
| L3 | +1.5 ATR | 20% позиции |
| L4 | +2.0 ATR | 20% позиции |
| L5 | +3.0 ATR | 20% позиции |

SL: -2 ATR.

### Grid (фиксированные пипсы)

Частичное закрытие на 4 уровнях:

| Уровень | Цель | Закрытие |
|---------|------|----------|
| G1 | +10 пипсов | 25% позиции |
| G2 | +25 пипсов | 25% позиции |
| G3 | +50 пипсов | 25% позиции |
| G4 | +100 пипсов | 25% позиции |

SL: -50 пипсов.

### Hold90 (trailing с порогами)

| Фаза | Порог | Действие |
|------|-------|----------|
| Ожидание | < 1.5 ATR | Ничего, держим |
| Активация | >= 1.5 ATR | Trail 30% от пика |
| Tight mode | >= 3.0 ATR | Trail 15% от пика (ужесточённый) |

SL: -2 ATR.

### Scalp (быстрый вход/выход)

| Параметр | Значение |
|----------|----------|
| Take Profit | +1 ATR |
| Stop Loss | -1.5 ATR |
| Time Stop | 4 часа |

---

## 5. Shadow Analytics

**Файл:** `strategies/shadow.py`

Фоновый трекинг для каждой открытой позиции (leaders + outsiders + paper):
- Каждый цикл (300 сек) записывает: цена, профит, пик, просадка
- Накапливает данные для анализа оптимальных SL/TP по стратегиям
- Выводит: лучший пик, худшая просадка, средний пик по стратегиям

```
SHADOW_ENABLED=true
```

---

## 6. Модель реалистичных издержек

**Файл:** `stats/cost_model.py`

При каждом открытии позиции оценивается реалистичная стоимость входа с учётом расширения спреда и проскальзывания в зависимости от рыночных условий.

### Множители спреда по источнику сигнала

| Источник | Множитель спреда | Slippage (% от ATR) | Обоснование |
|----------|:----------------:|:-------------------:|-------------|
| `extreme_rsi` | 2.5x | 5% | RSI 25/75 = повышенная волатильность |
| `extreme_bb` | 2.0x | 4% | Выход за 2σ = умеренный экстрим |
| `atr_spike` | 4.0x | 8% | legacy (setup удалён 23.04), множители оставлены для исторических позиций в БД |
| `news` | 3.5x | 7% | High-impact события = ликвидность исчезает |
| `cot`/`sentiment` | 1.0x | 2% | Спокойный вход по whale-данным |
| `vwap_deviation` | 1.2x | 3% | Скальпинг, умеренная волатильность |
| `stat_arb` | 1.2x | 3% | Market-neutral, малые ордера |
| `orb_breakout` | 1.2x | 3% | Пробой на открытии сессии |
| `news_fade` | 1.5x | 5% | Fade после новости |

### Формула

```
spread_cost = base_spread[symbol] × multiplier[source]
slippage = ATR_pips × slippage_pct[source]
total_entry_cost = spread_cost + slippage
round_trip_cost = total_entry_cost × 2
```

### Статистика

В логах отображается gross (чистый P&L) и net (за вычетом издержек):
```
Outsiders: 287 всего, win-rate 38%, +2702 gross, -1522 издержки, +1180 net, ~$+11.80
Leaders: 20 всего, win-rate 60%, +125 gross, -15 издержки, +110 net, ~$+11.00
```

---

## 7. Защитные фильтры

**Файл:** `strategies/filters.py`

Перед каждым входом проверяются:

| Фильтр | Условие |
|--------|---------|
| Лимит позиций | Leaders: max 20, Outsiders: max 50 |
| Лимит на инструмент | Max 3 позиции на один символ |
| Price Drift | Цена не ушла > 2 ATR от сигнала |
| Spread | Текущий спред < 2x нормального |
| Цена | > 0 (валидация) |

---

## Инструменты

### Forex (7 пар, + NZD для скальпинга stat_arb)

| yfinance | cTrader | Пипс | Pip Value (0.01) | Спред (pips) |
|----------|---------|------|:----------------:|:------------:|
| EURUSD=X | EURUSD | 0.0001 | $0.10 | 1.5 |
| GBPUSD=X | GBPUSD | 0.0001 | $0.10 | 1.8 |
| USDJPY=X | USDJPY | 0.01 | $0.07 | 1.5 |
| AUDUSD=X | AUDUSD | 0.0001 | $0.10 | 1.8 |
| USDCAD=X | USDCAD | 0.0001 | $0.07 | 2.2 |
| EURGBP=X | EURGBP | 0.0001 | $0.13 | 1.8 |
| USDCHF=X | USDCHF | 0.0001 | $0.10 | 1.8 |
| EURJPY=X | EURJPY | 0.01 | $0.07 | 2.0 |
| GBPJPY=X | GBPJPY | 0.01 | $0.07 | 2.5 |
| NZDUSD=X | — (скальпинг) | 0.0001 | $0.10 | 2.0 |

> **Примечание:** Коммодити (золото, нефть, газ), индексы (S&P500, Nasdaq) и крипта убраны из FxPro advisor.
> Причины: коммодити/индексы — высокие спреды, трудно ловить движения, тянут P&L в минус.
> Крипта — проблемы с SL на cTrader, отрицательный P&L. Крипта торгуется через Bybit-бот.

---

## Пример вывода в логах

```
── Сканирование (ансамбль 5 стратегий) ──
— EUR/USD LONG @ 1.08500 (сила 80%, стратегии: MA+RSI, MACD, Stochastic, Bollinger) —

── Leaders (copy-trading) ──
  LEADERS OPEN: EUR/USD LONG @ 1.08500 (SL=1.08300, trail=70.0 пипсов, src=cot+sentiment)
  Leaders: +1 открыто, -0 закрыто по развороту

── Outsiders (extreme setups) ──
  OUTSIDERS [CLASSIC] OPEN: USD/JPY LONG @ 149.500 (RSI=12.3, oversold < 15) + 4 paper, cost ~12.3 пипсов
  Outsiders: 1 extreme-сигнал, 1 открыто

── Мониторинг позиций ──
  CLOSE OUTSIDERS: Золото (XAU) SHORT → +45.2 пипсов (aggressive_tp)
  Позиций: 15 открыто, обновлено 16, закрыто: SL=0 trail=0 TP=1 time=0

── Shadow Analytics ──
  leaders: 5 позиций, пик +89.0 пунктов, просадка -12.0, средний пик +45.2
  outsiders: 10 позиций, пик +67.0 пунктов, просадка -25.0, средний пик +22.1

── Статистика ансамбля ──
  Горизонт 15м: 16 проверок, win-rate 56%, средний +5.9 пунктов

── Позиции по стратегиям ──
  Leaders: 20 всего (5 откр, 15 закр), win-rate 60%, +125.0 gross, -15.0 издержки, +110.0 net пипсов, ~$+11.00
  Outsiders: 50 всего (10 откр, 40 закр), win-rate 45%, +85.0 gross, -48.0 издержки, +37.0 net пипсов, ~$+3.70

── Paper exit-стратегии ──
  progressive: 40 всего, 30 закрыто, win-rate 58%, +4.20 пипсов, ~$+0.42
  grid: 40 всего, 30 закрыто, win-rate 50%, +2.80 пипсов, ~$+0.28
  hold90: 40 всего, 30 закрыто, win-rate 42%, +5.10 пипсов, ~$+0.51
  scalp: 40 всего, 30 закрыто, win-rate 67%, +3.50 пипсов, ~$+0.35
```

---

## Структура файлов

```
src/fx_pro_bot/
  analysis/
    ensemble.py        # Ансамбль 5 индикаторов
    signals.py         # MA, RSI, MACD, Stochastic, Bollinger, EMA
    scanner.py         # Сканирование инструментов
  strategies/
    leaders.py         # Copy-trading за китами
    outsiders.py       # Extreme setups (RSI/BB/ATR/news)
    exits.py           # 4 paper exit-стратегии
    monitor.py         # Мониторинг SL/trail/time-stops
    shadow.py          # ROI-аналитика
    filters.py         # Защитные фильтры входа
    scalping/
      indicators.py    # VWAP, z-score, OLS hedge, session range
      vwap_reversion.py # VWAP mean-reversion micro-scalper
      stat_arb.py      # Stat-arb cross-pair spread
      session_orb.py   # Opening Range Breakout + News Fade
  trading/
    client.py          # Низкоуровневый cTrader Open API (protobuf/TCP/Twisted)
    executor.py        # Высокоуровневый исполнитель сделок
    symbols.py         # Маппинг yfinance ↔ cTrader, SymbolCache
    killswitch.py      # Kill Switch — защита от потерь
    auth.py            # OAuth2 авторизация cTrader
  whales/
    cot.py             # CFTC COT reports
    sentiment.py       # Myfxbook sentiment
    tracker.py         # Whale tracker
  copytrading/
    ctrader.py         # cTrader Copy top strategies
  stats/
    store.py           # SQLite: suggestions + positions + paper + shadow
    cost_model.py      # Модель реалистичных торговых издержек
    verifier.py        # Автопроверка сигналов
    cleanup.py         # Очистка старых данных
  events/
    calendar_loader.py # Экономический календарь
  config/
    settings.py        # Все настройки (.env), PIP_SIZES, SPREAD_PIPS, комиссии FxPro
  app/
    main.py            # Главный цикл, координация всех стратегий
    auth_cli.py        # CLI для OAuth2 авторизации cTrader
    stats_cli.py       # CLI для просмотра статистики
```
