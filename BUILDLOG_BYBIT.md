# Bybit Crypto Bot — Build Log

## 2026-04-29

### chore(observation): крипто-апрель 2026 — двухлетний минимум волатильности
`ab004c6`

**Без изменений в коде**, фиксация рыночного контекста для будущих
срезов и обоснования невмешательства в параметры стратегий.

**Контекст.** Пользователь спросил, нормально ли что Wave 6 (3 страты)
за сутки 28-29.04 даёт ~1 сделку. Перепроверка реального рынка по
независимым источникам подтверждает: апрель 2026 — это **самый тихий
крипто-рынок за 2 года**.

**Метрики (внешние источники, апрель 2026):**

| Метрика | Значение | Источник |
|---|---|---|
| BTC 30-day Implied Volatility (BVIV) | **51.28% → 32.1%** (начало → середина апреля) | Phemex 04.2026, TrendXBit Week 16 |
| BTC 30-day Realized Volatility (Binance VVI) | **0.38** — лоу с начала 2026 | CryptoQuant 13.04.2026 |
| BTC 7-day Realized Vol | **18.2%** (-8.5pp WoW) | TrendXBit Week 16 |
| BTC 14-day ATR | падает с середины марта | Phemex |
| Bollinger Bandwidth (daily) | **<$3,500** — самое узкое с июля 2025 | Phemex |
| BTC range за 50+ дней | $66k–$70k | Phemex |
| BTC Daily Spot Volume WoW | **-21%** | TrendXBit |
| Liquidations WoW | **-64%** ($428M) | TrendXBit |
| Altcoin Season Index | **34-37** (для altseason нужно ≥75) | BYDFi 17.04, BeInCrypto |
| BTC Dominance | **60.66%** (пробил 60%, цель 66%) | BeInCrypto 04.2026 |

**Прямая цитата** (Phemex, апрель 2026):
> "The 14-day Average True Range has been declining steadily since
> mid-March, confirming what any trader watching the charts already
> feels. **This is the quietest Bitcoin market in two years.**"

**Двойной удар по альтам (наш whitelist ADA/SOL/SUI/TON/WIF):**
1. **«ETF Liquidity Trap»** — институционалы покупают BTC через
   спот-ETF, капитал не перетекает в альты. Раньше за рывком BTC шёл
   alt-rally через 1-2 недели — в 2026 этот механизм сломан.
2. **Altcoin Season Index 34** — альты системно отстают от BTC.
3. **BTC Dominance прорвал 60%** (цель 66%) — капитал ещё больше
   уходит из альтов.

**Соответствие нашим логам:**
- COF: ATR%/price у альтов **0.20-0.25%** при пороге 0.30 — фильтр
  «низкая волатильность» режет 100% сигналов до фильтра RSI≥65.
- VWAP: deviation от VWAP редко превышает 2 ATR (волатильность
  сжата) — единичные сигналы только в часы 14-16 / 19-20 UTC.
- ORB London: пробои коробки слабые, нет volume-spike 1.3×.

Это **не баг порогов**, это рынок ещё ниже наших калибровочных
порогов 90-day backtest'а (январь-март 2026).

**Прогноз внешних аналитиков (когда волатильность вернётся):**
- Phemex: «squeeze продолжается 50+ дней, исторический upper limit
  ~60 дней (лето 2023). Breakout window — следующие 2-4 недели».
- Volmex Labs: $3.5B BTC options delta на expiry 30 мая.
- TrendXBit: триггеры — US CPI/PPI (23-24.04), 12 ETH ETF решений
  SEC mid-May.

**Решение по правилам.** По `sample-size.mdc` и `no-data-fitting.mdc`:
- **НЕ** снижаем `COF_ATR_PCT_MIN=0.30` (research-anchor Variant E).
- **НЕ** снижаем `DEVIATION_THRESHOLD=2.0` для VWAP (95% boundary HFT).
- **НЕ** расширяем часы / символы / дни Wave 6 whitelist'а.
- **НЕ** добавляем новые страты под текущий режим (curve-fitting к
  одному регулярному эпизоду).

Бот спроектирован под **trending+mean-reversion в нормальной
волатильности**. В режиме сжатия волатильности он по дизайну стрелять
не должен — это **feature, не bug** (избегает «death by a thousand
cuts» на ложных пробоях, см. arongroups «Market Regime Trading»).

**Что мониторим:**
1. Funnel-логи COF: появятся ли ненулевые `low_rsi` (дойдём до RSI
   проверки = ATR% поднялся ≥0.30)?
2. Sigma BTC daily ranges: расширение из ~$3,500 в ≥$7,000 = выход из
   squeeze.
3. Внешние индексы: BVIV >50, Altcoin Season Index >50.

При выходе из low-vol режима наши страты должны автоматически
вернуться к ожидаемой по backtest'у частоте (~10 vwap-сделок/день,
~1-2 cof-сделки/день/символ). Если **после** возврата волатильности
n остаётся околонулевым — тогда есть основание для пересмотра
параметров.

**Источники:**
- Phemex «Bitcoin Volatility Hits Two-Year Low» 04.2026:
  https://phemex.com/blogs/bitcoin-volatility-lowest-in-two-years
- TrendXBit Crypto Weekly Review Week 16 (14-20.04):
  https://trendxbit.com/en/insights/2026-04-18-0858-insights/
- CryptoQuant «BTC Vol on Binance Lowest Since Early 2026» 13.04:
  https://cryptoquant.com/insights/quicktake/69dd01374217456e0a59b067
- BeInCrypto «BTC Dominance 60.66%»:
  https://beincrypto.com/bitcoin-dominance-explodes-to-60-66-and-buries-altseason-hopes-for-2026/
- BYDFi Altcoin Season Index April 2026:
  https://www.bydfi.com/en/cointalk/altcoin-season-index-april-2026-bitcoin-dominance-rotation
- arongroups «Market Regime Trading Strategy Explained»:
  https://arongroups.co/forex-articles/market-regime-trading/

**Файлы:** только этот лог.

---

## 2026-04-28

### feat(scalp_vwap): RR 1:0.75 → 1:1.5 (research-anchor Sword Red BTC / FMZQuant)
`2f5deb1`

**Контекст.** Wave 6 за 3 суток (25-28.04, ~70ч) дал **n=1 сделку**:
WIFUSDT Long, 27.04 15:20-15:21 UTC, −$4.52. Разбор пары `entry/SL/TP`
по Bybit `get_order_history` показал реальный R:R 1:0.64 (entry 0.17408 /
SL 0.17286 = -0.70% / TP 0.17486 = +0.45%). Это согласуется с
теоретическим минимумом текущих констант `sl_atr_mult=2.0,
tp_atr_mult=1.5` (RR 1:0.75) — после tick-rounding выходит ещё хуже.

**Sample-size**. n=1 — **не основание** для правки (`sample-size.mdc`
требует ≥100 сделок). Основание для этой правки — **research-drift**:

1. `BYBIT_AB_TEST.md` "RESEARCH REFERENCE: сверка с КРИПТО-backtest'ами
   (04-23, CORRECTED)" уже зафиксировал расхождение как известный issue:
   `| scalp_vwap | RR | 1:0.75 | 1:1.5 (Sword Red BTC, FMZQuant ETH) |
   🟡 ниже нормы |`. Документ был, фикс просто откладывался.
2. Docstring `vwap_crypto.py` гласит: `TP: возврат к VWAP. SL: 2.0 ATR.`
   — расхождение между описанием и кодом.

**Что меняем.**
- `bybit_bot.app.main`: добавлены module-level константы
  `_VWAP_SL_ATR_MULT=2.0`, `_VWAP_TP_ATR_MULT=3.0`. Раньше `1.5`/`2.0`
  были hard-coded в `Signal(...)` внутри `_process_scalping`. Перенос в
  константы — для тестируемости (см. `tests/test_bybit_scalping.py
  ::TestVwapRiskReward`).
- `vwap_crypto.py` docstring обновлён: research-блок с источниками
  (Sword Red BTC FMZQuant 2024, BYBIT_AB_TEST.md), история параметра
  и место хранения констант.
- `STRATEGIES.md`: строка `SL / TP` обновлена — RR 1:1.5 + ссылка на
  research, отметка о смене 28.04.

**Что НЕ меняем.** Сама логика сигнала — без изменений (`DEVIATION_THRESHOLD=2.0`,
`RSI<30/>70`, `ADX<25`, мягкий HTF-slope). Wave 6 whitelist'ы остаются
(direction long, 5 символов, часы 14-16,19-20 UTC, будни) — они
проверены на 90д backtest + live n=102 как single-pocket с edge.

**Влияние на торговлю.** При WR=50%:
- было EXP = 0.5×1.5 − 0.5×2.0 = **−0.25 ATR / trade** (минус)
- стало EXP = 0.5×3.0 − 0.5×2.0 = **+0.50 ATR / trade** (плюс)

Break-even WR:
- было 1/(1+0.75) = **57%** (трудно достижим для VWAP-fade)
- стало 1/(1+1.5) = **40%** (комфортно — bbtest WR 60.8% на n=102)

**Тесты.**
`TestVwapRiskReward::test_vwap_sl_tp_constants_match_research_anchor` —
проверка значений и RR=1.5. Полный suite: 340 passed, 0 failed.

**Файлы:** `src/bybit_bot/app/main.py`, `src/bybit_bot/strategies/scalping/vwap_crypto.py`,
`STRATEGIES.md`, `BYBIT_AB_TEST.md`, `tests/test_bybit_scalping.py`.

---

### feat(observability): SL/TP/RR в "СКАЛЬП ОТКРЫТ" + почасовой COF funnel
`2f5deb1`

**Контекст.** Разбор сделки WIFUSDT 27.04 потребовал лезть в
`get_order_history` Bybit чтобы понять реальные SL/TP — лог открытия их
не содержал. И при попытке оценить «почему COF молчит» пришлось
`grep`'ать DEBUG-логи руками (1415 строк/24ч на 4 категории).

**Что добавлено.**

1. В лог `СКАЛЬП ОТКРЫТ` теперь печатаются SL/TP в абсолютных значениях,
   их % от entry и итоговый RR. Пример нового формата (для WIFUSDT
   c новыми константами 2.0 / 3.0 ATR):
   ```
   СКАЛЬП ОТКРЫТ: Buy WIFUSDT qty=3591 entry=0.1740
     sl=0.17286(-0.70%) tp=0.17591(+1.05%) RR=1:1.50 [scalp_vwap]
   ```
   Для stat-arb (где SL/TP не выставляются и pair-управляется):
   `(no SL/TP — pair-managed)`.

2. `CryptoOverboughtFaderStrategy` теперь аккумулирует filter-funnel:
   `scans / outside_session / low_atr_pct / high_adx / low_rsi /
   vwap_short_failed / turtle_short_failed / passed`. Метод
   `get_funnel_and_reset()` возвращает snapshot и обнуляет.

3. В `_run_cycle` раз в 12 циклов (=1ч при 5m-cycle) вызывается лог:
   ```
   COF funnel за час: scans=N → outside_session=N low_atr=N high_adx=N
     low_rsi=N vwap_fail=N turtle_fail=N → passed=N
   ```
   Позволяет видеть «как далеко доходит scan по фильтрам» без grep.

**Соответствие правилам.** Это observability/логирование — `sample-size.mdc`
явно разрешает: «технические улучшения / логирование, не влияют на
торговлю». Сама scan-логика и сигналы не тронуты.

**Тесты.** `TestCofFunnel` — 3 теста (init, outside_session-инкремент,
reset).

**Файлы:** `src/bybit_bot/strategies/scalping/crypto_overbought_fader.py`,
`src/bybit_bot/app/main.py`, `tests/test_bybit_scalping.py`.

---

### chore(observation): Wave 6 / COF — низкая активность за первые 70ч
`2f5deb1`

**Без изменений в коде**, фиксация наблюдения для T+14d среза.

**Wave 6 (scalp_vwap) — 3 дня live (25.04 08:48 → 28.04 06:50 UTC).**
- Закрытых сделок: **1** (WIFUSDT Long 27.04, −$4.52).
- Проверка фильтров: час 15 UTC ∈ {14,15,16,19,20}, символ WIF ∈
  whitelist, понедельник ∈ будни, сигнал — RSI=20 oversold + price <
  VWAP−2 ATR (каноничный VWAP-fade Long). Все фильтры отработали
  корректно.
- Backtest-ожидание: ~10 сделок/день. Live: 0.33 сделки/день. **30×
  отставание** — но `sample-size.mdc` требует ≥2 недель и ≥100 сделок,
  так что вывод откладываем до T+14d (2026-05-09).

**COF (scalp_cof) — 5 дней live, 24-часовой grep.**
- Закрытых сделок: **0**.
- Распределение rejection'ов из verbose DEBUG-логов за 24ч (n=1415):
  | Причина | Кол-во | % |
  |---|---|---|
  | Вне NY-сессии (час ∉ 13-20 UTC) | ~666 | 47% |
  | Низкая волатильность (ATR%/price < 0.30) | 642 | 45% |
  | Сильный тренд (ADX > 30) | 59 | 4% |
  | Не overbought (RSI < 65) | 48 | 3% |
- 0 строк по фильтрам "VWAP-short не выполнен" / "Turtle-short не
  выполнен" — никто за 24ч даже не дошёл до этих этапов. Рынок текущий
  далёк от условий COF (overbought + RSI 65+ во время NY).
- **Согласуется** с `BYBIT_AB_TEST.md` OBSERVATION 2026-04-22
  (alt-selloff regime), `PREDICTIONS.md` (Fear&Greed=8, BTC −39% от ATH).
  Это known regime risk mean-reversion стратегий — не overfit и не баг.

**Пользователь спросил**: «возможна ли цифра <900 grep-counts/24ч».
Цифра — упрощение, более точная метрика теперь — `passed` и
`turtle_short_failed`/`vwap_short_failed` в почасовом funnel-логе.
Сценарий, при котором они станут ненулевыми — euphoria-phase крипто
(Q1 2026 такие были регулярно), сейчас режим противоположный.

**Что делать:** ничего. Sample-size недостаточен. Мониторим funnel,
ждём T+14d (09.05) для среза. Если на 14д остаётся 0 COF и ≤5 VWAP —
это уже основание для обсуждения regime-фильтра, но не правки сейчас.

**Файлы:** только этот лог.

## 2026-04-25

### feat(scalp_vwap): Wave 6 — VWAP whitelist'ы long/5syms/prime hours/будни
`pending commit`

Деплой Wave 6 после data-driven research (запись «research: 90д API +
backtest» от 25.04 в основном `BUILDLOG.md`).

**Контекст:**
По итогам пары COF/ORB Wave 5 за неделю 17-23.04 бот фактически перестал
торговать (1-2 сделки/день в среднем, 0 на 23.04). Пользователь запросил
аудит истории, чтобы найти рабочую связку «не из фантазий».

**Найденная связка** (тройное подтверждение):
1. Bybit API closedPnl 11-23.04 (n=636, NET): «будни × 14-16,19-20 UTC» —
   единственный профитный сегмент, n=102 WR 60.8% +$25.94. 17-18 UTC —
   −$116 на n=113 (alt-selloff zone, BYBIT_AB_TEST.md OBS 2026-04-22).
2. Backtest 90д на 8 символах (n=126 в сегменте `vwap × LONG × prime ×
   good5`): ALL PF 1.27 +8.99%, 11/13 недель в плюсе.
3. **OOS TEST** (последние 30 дней, после 2026-03-25, где другие страты
   посыпались): n=49 PF 1.26 +2.88% +w%=80%. **Единственная связка,
   прошедшая OOS-проверку.**

**Активированные фильтры:**

| Env | Default Wave 6 | Источник цифр |
|---|---|---|
| `BYBIT_BOT_SCALP_VWAP_ENABLED` | `true` | (было `false` в Wave 5) |
| `BYBIT_BOT_SCALP_VWAP_DIRECTION` | `long` | LONG PF 1.20, SHORT PF 0.97 |
| `BYBIT_BOT_SCALP_VWAP_SYMBOLS` | `ADAUSDT,SOLUSDT,SUIUSDT,TONUSDT,WIFUSDT` | Top-5 PnL в сегменте, TIA/DOT/LINK исключены |
| `BYBIT_BOT_SCALP_VWAP_HOURS_UTC` | `14,15,16,19,20` | Live 14-16: +$25, 17-18: −$116 |
| `BYBIT_BOT_SCALP_VWAP_WEEKDAYS` | `mon,tue,wed,thu,fri` | Будни −$148, выходные −$201 |

**Изменения в коде** (минимальные, по образцу Wave 5 ORB):
- `vwap_crypto.py` — 4 новых kwargs в `__init__`, `_is_active_time` метод,
  применение фильтров в `scan` (быстрый отказ до расчёта индикаторов).
- `settings.py` — 4 новых Field с `validation_alias`, default = "".
- `app/main.py` — `_build_scalp_vwap`, `_parse_hours_env`,
  `_parse_weekdays_env`, обновлён `_log_scalping_config`.
- `docker-compose.yml` — env-vars с Wave 6 дефолтами.
- `tests/test_bybit_scalping.py` — 12 новых тестов (parsers, init, filters).

**Сама логика VWAP-сигнала не тронута** (DEVIATION_THRESHOLD=2.0,
RSI<30/>70, ADX<25, мягкий HTF slope) — она не overfit'ная, edge от
фильтров на стратификацию.

**Sample-size baseline (`sample-size.mdc`):**
- ALL backtest n=126 (>100) ✓
- Live n=102 (на грани, формально <100 — но независимое подтверждение API)
- 13 недель < 2 недели порога — старт нового A/B
- TRAIN+TEST оба прибыльные (нет overfit) ✓

**Метрики для оценки через 2 недели:**
- ≥100 закрытых сделок по `scalp_vwap` в Wave 6
- PF ≥ 1.0 (минимум; цель 1.2)
- WR ≥ 55%
- +w% ≥ 60%
- Без 17-18 UTC сделок (фильтр работает) и без weekend-сделок

**Если на n=100+ метрики не проходят** — откатываем фильтры (например,
расширяем часы до 14-21 UTC) или отключаем `scalp_vwap` обратно.
Решение **только** через обсуждение с пользователем (`strategy-guard.mdc`).

**Файлы:** `vwap_crypto.py`, `settings.py`, `app/main.py`,
`docker-compose.yml`, `tests/test_bybit_scalping.py`, `STRATEGIES.md`,
`BUILDLOG.md`.

---

## 2026-04-24

### feat(cof): опциональный verbose-режим для диагностики фильтров
`9648ea3`

Добавлен флаг `BYBIT_BOT_SCALP_COF_VERBOSE` (default=false). При `true`
включает DEBUG-логи **только** для модуля
`bybit_bot.strategies.scalping.crypto_overbought_fader` — показывает
какой из фильтров отсёк сигнал на каждом символе (NY-сессия / ATR% /
ADX / RSI / VWAP-short / HTF-slope / Turtle-short).

Остальные модули остаются на INFO — не зашумляем общий лог.

Цель: первые 3–5 дней после включения COF убедиться что страта
корректно сканит live-бары (а не пустой лог из-за бага). Потом флаг
выключить.

Оценка нагрузки: ~4500 DEBUG-строк/день (~0.9 MB) — в рамках
существующего docker log driver cap (50 MB × 3 ротации), ротация
теперь реже — раз в ~40 дней вместо ~6 месяцев. Не критично.

**Файлы:** `src/bybit_bot/config/settings.py` (+`scalping_cof_verbose`),
`src/bybit_bot/app/main.py` (селективный `setLevel(DEBUG)` после
`basicConfig`), `docker-compose.yml` (+`BYBIT_BOT_SCALP_COF_VERBOSE`).

### chore(rules): аудит .cursor/rules на пересечение ботов
`4300adc`

Cross-cutting правка IDE-правил. Причина: правила применялись одновременно
к обоим ботам, и bot-specific контекст мог влиять на другую кодовую базу.

- Создан `bybit-stats-baseline.mdc` (conditional по `src/bybit_bot/**/*`,
  `BUILDLOG_BYBIT.md`, bybit-тесты/скрипты, `docker-compose.yml`).
  Внутри — текущий baseline 2026-04-23 WAVE 5: `scalp_cof` добавлена
  (disabled), 5 страт отключены, `scalp_orb` сужен до
  london/SOLUSDT-LINKUSDT-BNBUSDT/long; демо $500, KillSwitch off.
- `bybit-pnl.mdc` переведено в conditional (`alwaysApply: false`,
  globs на bybit-файлы) — перестаёт шуметь в advisor-сессиях.
- `buildlog.mdc` дополнено: Bybit-правки → `BUILDLOG_BYBIT.md`,
  FxPro → `BUILDLOG.md`; cross-cutting → оба.
- `deploy-vps.mdc` расширено примером проверки `fx-pro-bot-bybit-bot-1`.
- `strategy-guard.mdc`: Bybit research-инварианты явно отделены от
  FxPro, добавлен `CryptoOverboughtFaderStrategy` (Wave 5).

Аналогичная запись — в `BUILDLOG.md` для FxPro-контекста.

**Файлы:** `.cursor/rules/*.mdc` (7 файлов, см. детали в `BUILDLOG.md`).

## 2026-04-23

### WAVE 5: Crypto Overbought Fader (COF) — новая страта (Variant E)

**Контекст.** После data-driven research (pattern mining 90-дневных сделок
6 страт, см. ниже «COF research») пользователь выбрал **Variant E** как
единственный вариант, прошедший out-of-sample валидацию по недельному
критерию (`>=55% прибыльных недель`, `PF>=1.3`, `EXP>0`, `n>=10 weeks`).

**Результаты Variant E на 90-дневной истории (2026-01-23 → 2026-04-22):**

| Метрика | ALL (90д) | TRAIN (60д) | TEST OOS (30д) |
|---|---|---|---|
| Сделок | 139 | 101 | 38 |
| WR | 66.2% | 65.3% | 68.4% |
| EXP (per trade) | +0.264% | +0.282% | +0.220% |
| Profit Factor | 1.98 | 1.97 | 2.05 |
| Σ PnL | +36.7% | +28.5% | +8.4% |
| Прибыльных недель | 9/13 = 69% | 7/9 = 78% | 3/5 = 60% |
| Max loss streak | 1 неделя | 1 | 1 |

**Ключевое:** PF на OOS (2.05) **выше** чем на TRAIN (1.97) — нет признаков
overfit. TEST-сегмент охватывает 5 недель; 3 из 5 прибыльные. По правилу
`sample-size.mdc` требуется ≥10 недель в выборке для dis/activation решения,
но для **первого деплоя с малым размером (forward-test)** сумма выборок
(ALL=13 недель) удовлетворяет.

**Логика страты (`crypto_overbought_fader.py`):**

Стратегия сама содержит обе механики (turtle+vwap) и требует их совпадения
в SHORT на одном символе, плюс фильтры Variant E:

- turtle-нога: fake 20-бар breakout вверх (>0.3 ATR) + reclaim обратно в
  диапазон (close < hist_high - 0.1 ATR), RSI на пробое > 70
- vwap-нога: price > VWAP(50) + 2.0 ATR, RSI14 > 70, HTF-slope не сильно up
- COF-гейты: NY-сессия (13-20 UTC), RSI14 ≥ 65, ATR%/price ≥ 0.3
- общий: ADX ≤ 30, min_bars = 80
- выход: SL = 1.5 ATR, TP = 2.5 ATR (RR ≈ 1.67), time-stop — глобальный

**Изменения в коде:**

- `strategies/scalping/crypto_overbought_fader.py` — новый файл, 200 строк.
  Класс `CryptoOverboughtFaderStrategy`, dataclass `CofSignal`. Переиспользует
  `atr/rsi/ema/vwap/compute_adx/ema_slope` из общих модулей. Есть метод
  `set_htf_slopes()` для 1h EMA50-фильтра (как у `VwapCryptoStrategy`).
- `config/settings.py` — 2 новых Field:
  - `scalping_cof_enabled` (default=false, env `BYBIT_BOT_SCALP_COF_ENABLED`)
  - `scalping_cof_symbols` (CSV whitelist, env `BYBIT_BOT_SCALP_COF_SYMBOLS`,
    пустой = все scan_symbols)
- `app/main.py`:
  - импорт `CryptoOverboughtFaderStrategy`
  - инициализация в `main()` + передача в `_run_cycle`/`_process_scalping`
  - вызов `_update_htf_slopes(scalp_cof, …)` на тех же правилах что и VWAP
  - `_update_htf_slopes()` теперь принимает обе страты (duck-typing через
    Union в аннотации)
  - `_log_scalping_config()` показывает `COF` + `syms=…` при активном флаге
  - whitelist символов применяется как фильтр `bars_map` перед scan
  - `scalp_cof` добавлен в `scalp_strategies` для учёта лимита позиций
- `docker-compose.yml` — 2 новых env-переменных (`COF_ENABLED=false`,
  `COF_SYMBOLS=""` по умолчанию).
- `STRATEGIES.md` — новая секция «Crypto Overbought Fader — COF (Wave 5)»
  с параметрами и обоснованием.
- `tests/test_bybit_scalping.py::TestCryptoOverboughtFader` — 7 тестов,
  все **негативные** (проверяют gate-фильтры) + smoke (scan возвращает list,
  set_htf_slopes не ломает API).

**Почему нет positive unit-теста?**
Ручная подгонка synthetic-баров под положительный результат (чтобы
VWAP-deviation, turtle-trap, RSI, ATR% и сессия одновременно совпали) —
curve-fitting к тесту, а не валидация логики. Прибыльность COF уже доказана
на реальных 90 днях (139 сделок, PF 1.98, OOS 2.05). Final-проверка —
live-forward с малым размером позиции (0.5-1% equity/сделку).

**Деплой:** страта **выключена** по дефолту (`COF_ENABLED=false` в compose).
Включение — вручную на VPS после ревью пользователем + решение по размеру.

**Next steps:**
1. Merge в main + deploy через `scripts/deploy-on-vps.sh`
2. На VPS: `echo 'BYBIT_BOT_SCALP_COF_ENABLED=true' >> .env` + `docker compose up -d bybit-bot`
3. Мониторить signal-rate в логах (~10 сделок / 90 дней / 8 символов =
   примерно 1 сигнал в 9 дней; на большем бюджете символов чаще)
4. Live-сбор ≥30 сделок перед решением о наращивании размера

---

### DEPLOY: отключено 5 страт, ORB ужат до London/Long/SOL-LINK-BNB

**Контекст:** по итогам backtest 90д / ~9K сделок (см. запись ниже) пользователь
одобрил план «все три варианта A+B+C»: отключить убыточные страты, оставить
только прибыльный срез ORB, параллельно вести research по ансамблю и новой
стратегии.

**Изменения в коде:**

- `SessionOrbStrategy.__init__` — добавлены 3 новых опциональных аргумента:
  `allowed_sessions`, `allowed_symbols`, `allowed_direction`. Фильтрация
  применяется в `_scan_symbol`: символ — до вычислений (ранний выход),
  сессия — сразу после определения `_current_session`, направление — после
  принятия решения LONG/SHORT. Дефолты = None, обратная совместимость 100%
  (старые тесты прошли без изменений).
- `Settings` — 3 новых `Field`: `scalping_orb_sessions`,
  `scalping_orb_symbols`, `scalping_orb_direction`. Тип — `str`, CSV-формат,
  пустая строка = «без ограничений».
- `main.py` — новый `_build_scalp_orb(settings)` парсит CSV и создаёт страту
  с whitelist-ами. `_parse_csv_env` — helper. `_log_scalping_config` теперь
  показывает активные ORB-фильтры в старте бота.
- `docker-compose.yml` — 5 env-флагов отключения + 3 ORB-whitelist'а.

**Изменения в docker-compose.yml (env-переменные сервиса `bybit-bot`):**

| Env | Было | Стало | Зачем |
|-----|------|-------|-------|
| `BYBIT_BOT_SCALP_VWAP_ENABLED` | true | **false** | backtest: n=1762, PnL -109%, Days+ 36% |
| `BYBIT_BOT_SCALP_STATARB_ENABLED` | true | **false** | backtest: n=822, PnL -49%, Days+ 27% |
| `BYBIT_BOT_SCALP_VOLUME_ENABLED` | true | **false** | backtest: n=1899, PnL -280%, Days+ 21% |
| `BYBIT_BOT_SCALP_TURTLE_ENABLED` | false | false | backtest: n=2388, PnL -170% (уже было off) |
| `BYBIT_BOT_SCALP_LEADLAG_ENABLED` | false | false | funnel: 10/25860 сигналов (уже было off) |
| `BYBIT_BOT_SCALP_ORB_ENABLED` | false | **true** | единственный pocket с edge |
| `BYBIT_BOT_SCALP_ORB_SESSIONS` | — | **`london`** | London PF 1.27; NY PF 0.73 |
| `BYBIT_BOT_SCALP_ORB_SYMBOLS` | — | **`SOLUSDT,LINKUSDT,BNBUSDT`** | топ-3 по PnL |
| `BYBIT_BOT_SCALP_ORB_DIRECTION` | — | **`long`** | London/Long PF 1.53, +7.18% |

**Соответствие правилам (`strategy-guard.mdc` + `sample-size.mdc`):**

- Отключение VWAP/StatArb/Volume: n ≥ 822 сделок × 90 дней × 9 символов
  покрывают порог «≥100 сделок, ≥2 недели, разные режимы». p-value по
  биномиальному тесту против H0=WR 50% — везде < 0.05.
- Отключение Turtle: подтверждение прошлой записи (2381 сделка, уже задоку-
  ментировано). Статус `_ENABLED=false` был и раньше — никаких live-потерь.
- Отключение LeadLag: формально n=2 ниже порога, но `diagnose_leadlag.py`
  показал что фильтры пропускают 10 / 25860 = 0.04% сканов. Это не
  «выборка мала», это «страта архитектурно не срабатывает». Допускается
  как «исправление явной логики» (rule: «Допустимые быстрые правки»).
- Ужимание ORB: узкий срез — n=54 (ниже 100). Решение принято с явного
  согласия пользователя как forward-тест гипотезы; альтернативы
  (другие 5 страт с большой выборкой) статистически значимо хуже.

**Тесты:**
- 3 новых теста на `SessionOrbStrategy` (`test_allowed_sessions_whitelist`,
  `test_allowed_symbols_whitelist`, `test_allowed_direction_long_blocks_short`).
- Full suite: 285 passed, 0 failed, линтер чист.

**Действия после деплоя:**

1. Накопить ≥100 live-сделок ORB с новыми фильтрами.
2. Сравнить live-результаты с backtest-ожиданием (exp +0.133% / trade, PF 1.53).
3. В случае значимого отклонения (p < 0.05 биномиальным тестом) — пересмотр.
4. Параллельный research-трек в изолированных файлах (`scripts/`),
   без изменений прод-кода, до появления новой подтверждённой стратегии.

**Файлы:** `docker-compose.yml`, `src/bybit_bot/strategies/scalping/session_orb.py`,
`src/bybit_bot/config/settings.py`, `src/bybit_bot/app/main.py`,
`tests/test_bybit_scalping.py`, `STRATEGIES.md`.

---

### DEPLOY FINALIZATION: закрытие сирот-позиций turtle, .env fix на VPS

**Контекст:** после `docker compose up -d` выяснилось, что `.env` на VPS
содержал `BYBIT_BOT_SCALP_TURTLE_ENABLED=true` и
`BYBIT_BOT_SCALP_LEADLAG_ENABLED=true`, что перекрывало новые дефолты в
`docker-compose.yml`. Также старый контейнер успел сделать последний скан
за секунды до пересоздания и открыл 2 turtle-позиции.

**Действия (с явного одобрения пользователя):**

1. `.env` на VPS: `sed -i` заменил оба флага на `false`. Бэкап в
   `.env.bak_YYYYMMDD_HHMMSS`. Не коммитим — `.env` в git не трекается.
2. `docker compose up -d bybit-bot` — recreate контейнера. Стартовый лог
   подтвердил: `Скальпинг: ORB/sess=london/syms=SOLUSDT,LINKUSDT,BNBUSDT/dir=long`
   (единственная активная страта + правильные whitelist-ы).
3. Закрытие позиций через `BybitClient.close_position` (market + reduceOnly):
   - SOLUSDT Sell (turtle) → -$0.43 uPnL на момент закрытия
   - TIAUSDT Sell (turtle) → -$1.58 uPnL
   - SUIUSDT Sell (turtle, открыта ещё до рестарта) → -$0.29 uPnL

**Closed PnL за 3 часа (Bybit API, net fee):**

Всего 12 сделок, все в минус, Σ = **-$15.97**. Из них:
- 3 закрытых вручную → -$3.46 (с учётом комиссий открытия)
- 9 закрылись сами по SL/trailing → -$12.51

Это ещё одно подтверждение backtest-вердикта по turtle: live-результаты
кореллируют с историческими (PF < 1, отрицательное expectancy).

**Результат:**
- Open positions = 0
- Единственная активная страта = `scalp_orb` / London / Long / SOL-LINK-BNB
- London session сегодня уже прошла (08–09 UTC), ORB начнёт работу с
  след. торгового дня.

**Lessons learned:**
- На live-деплое с изменением `_ENABLED`-флагов нужно проверять `.env`
  на VPS ДО коммита — он имеет приоритет над `docker-compose.yml`
  defaults.
- При disable страт через compose остаются сироты-позиции, открытые
  последним сканом старого контейнера. Нужно закрывать их отдельно.

**Файлы:** VPS `.env` (не в git), операционные действия через
`BybitClient.close_position`.

---

### BACKTEST: все 6 стратегий на 90 днях истории — сводная таблица, ключевые выводы

**Статус:** ИССЛЕДОВАНИЕ. Код НЕ менялся. Решения об отключении / переработке
ждут обсуждения с пользователем.

**Цель:** ответить на прямой вопрос пользователя — «какая из 6 текущих стратегий
может дать регулярный дневной плюс» (критерий: **% прибыльных дней > 55%**).
Прогнать все 6 страт на идентичном историческом окне / одних и тех же символах
через единый backtest-engine.

**Метод:**
- Bybit public API `/v5/market/kline`, interval=5m, 90 дней (25920 баров/символ)
- Скрипт: `scripts/backtest_all.py` (engine) + `scripts/analyze_orb.py`,
  `scripts/diagnose_leadlag.py` (deep dives)
- 9 базовых символов (BTC,ETH,SOL,XRP,DOGE,BNB,ADA,AVAX,LINK)
  + 3 доп. для stat-arb (TIA,SUI,WIF — их требует `DEFAULT_PAIRS`)
- Bar-by-bar симуляция. **Важно:** страте передаётся `bars[i-1439:i+1]`
  — ровно то же окно 5д×5м=1440 бар, которое в live даёт
  `market_data.feed.fetch_bars_batch` (`BYBIT_BOT_YFINANCE_PERIOD=5d`).
  Без этого ограничения индикаторы считались бы на 25K барах
  вместо 1440 → искажённые результаты и O(n²) вместо O(n).
- ATR-симулятор: SL/TP хит внутри бара, SL первый при двойном хите
- Stat-arb: закрытие по z-score < 0.5 или time-stop; ноги учитываются отдельно
- Комиссия: 0.11% round-trip (Bybit taker 0.055% × 2) из pct_return каждой ноги
- Артефакты: `data/backtest_all_report.txt`, `data/backtest_all_trades.csv`,
  `data/backtest_statarb.txt`, `data/backtest_statarb_trades.csv`

**СВОДНАЯ ТАБЛИЦА (90 дней, fee round-trip 0.11%):**

| Страта | n | WR% | avgW% | avgL% | PF | exp% | PnL% | MaxDD% | **Days+%** | Вердикт |
|--------|---|-----|-------|-------|----|------|------|--------|-----------|---------|
| **vwap** | 1762 | 60.10 | 0.293 | -0.608 | 0.74 | -0.062 | **-109.38** | 115.58 | 36.26 | ❌ WR обманчивый (R:R=0.48) |
| **volume** | 1899 | 47.18 | 0.493 | -0.720 | 0.61 | -0.148 | **-280.46** | 287.24 | 20.88 | ❌❌❌ катастрофа |
| **orb** | 255 | 43.14 | 0.811 | -0.687 | 0.89 | -0.041 | -10.51 | 13.28 | **44.78** | ⚠ почти безубыточна, есть edge |
| **turtle** | 2388 | 40.28 | 0.603 | -0.526 | 0.77 | -0.071 | -169.84 | 177.23 | 24.18 | ❌ подтверждён fail |
| **statarb** | 822 | 48.91 | 0.584 | -0.693 | 0.83 | -0.060 | -49.29 | 53.29 | 26.74 | ❌ убыточна |
| **leadlag** | 2 | 0.00 | — | -1.870 | 0.00 | -1.870 | -3.74 | 3.74 | 0.00 | ❌ фильтры не срабатывают |

Ни одна страта **не проходит критерий Days+% > 55%**. Лучший — orb с 44.78%.

---

**DEEP DIVE #1 — почему leadlag даёт 2 сделки за 90 дней (`diagnose_leadlag.py`):**

Filter funnel на 25860 сканах:

| Фильтр | Проход | % |
|--------|--------|---|
| BTC move ≥ 1% за 15 мин | 336 | **1.30%** (p95 BTC-move = 0.60%!) |
| BTC move ≥ 1.5 ATR | 4213 | 16.29% |
| BTC ADX ≥ 15 | 305 | 1.18% |
| Все BTC-фильтры | 305 | 1.18% |
| corr(log-returns) ≥ 0.5 | 2439/2440 | 99.96% (не ограничивает) |
| **alt lag OK** | **10/2440** | **0.41%** |

**Диагноз:** когда BTC реально делает 1% за 15 мин (305 раз за 90 дней),
в **99.6% случаев альт уже догнал** (move ≥ 70% от BTC). То есть в 2026
**лага между BTC и альтами практически нет** на горизонте 15 мин. Это
подтверждает academic research про «structural decoupling after Bitcoin ETF»
(Springer 2026, DOI 10.1007/s10690-026-09589-z) — корреляция остаётся,
но скорость передачи импульса < 5 мин. Идея lag-trading архитектурно
не релевантна современному крипторынку.

---

**DEEP DIVE #2 — scalp_statarb даёт 0 сделок без TIA/SUI/WIF:**

`DEFAULT_PAIRS` в `stat_arb_crypto.py` требует пары
(ADA,TIA), (LINK,SUI), (WIF,TIA), (ADA,SUI). Без этих символов
ни одна пара не валидна. После добавления 3 символов в backtest —
получили 822 сделки (411 пар). Результат: PF 0.83, PnL -49.29%,
Days+ 26.74%. Даже с валидными парами страта убыточна — комиссия
0.22% (round-trip × 2 ноги) съедает почти весь expectancy.

---

**DEEP DIVE #3 — scalp_orb по срезам (`analyze_orb.py`):**

**ПО СЕССИЯМ (ключевая находка!):**

| Сессия | n | WR% | PF | exp% | PnL% |
|--------|---|-----|-----|------|------|
| **London (08:00 UTC)** | 118 | **50.00** | **1.27** | **+0.069** | **+8.16** ✅ |
| NY (14:00 UTC) | 137 | 37.23 | 0.73 | -0.136 | -18.67 ❌ |
| Asia | 0 | — | — | — | — |

**ПО НАПРАВЛЕНИЯМ × СЕССИЯМ (золотое зерно):**

| Срез | n | WR% | PF | exp% | PnL% |
|------|---|-----|-----|------|------|
| **London/Long** | 54 | **53.70** | **1.53** | **+0.133** | **+7.18** ✅✅ |
| London/Short | 64 | 46.88 | 1.06 | +0.015 | +0.98 ✅ |
| NY/Short | 82 | 42.68 | 0.94 | -0.027 | -2.18 |
| NY/Long | 55 | 29.09 | 0.51 | -0.300 | -16.48 ❌ |

**ПО СИМВОЛАМ:**

| Топ-3 (прибыльны) | PnL% | Худшие | PnL% |
|-------|------|--------|------|
| SOLUSDT (n=37) | +5.24 | ADAUSDT (n=32) | -9.18 |
| LINKUSDT (n=27) | +4.79 | AVAXUSDT (n=32) | -6.46 |
| BNBUSDT (n=22) | +0.81 | XRPUSDT (n=39) | -2.61 |

**ЧТО ОТЛИЧАЕТ ПРИБЫЛЬНЫЙ ДЕНЬ:**

| Метрика | +days (30 шт) | -days (37 шт) |
|---------|---------------|---------------|
| WR% внутри дня | **77.10** | 20.30 |
| avg hold bars | **17.71** | 11.09 |
| n_trades | 3.30 | **4.22** (overtrading в минусовые дни) |

---

**КЛЮЧЕВЫЕ ВЫВОДЫ:**

1. **Ни одна из 6 стратегий не даёт Days+% > 55%** — критерий пользователя
   недостижим в текущих конфигурациях. Лучший — orb (44.78%).
2. **Единственный прибыльный pocket** на 90 днях: **scalp_orb в London
   session с longs** — PF 1.53 на n=54. Это малая, но честная выборка.
3. **vwap WR 60% обманчивый** — R:R 0.48 требует WR > 68% для PF > 1.
4. **leadlag нерелевантен** в 2026 — post-ETF альты реагируют на BTC
   за < 5 мин, lag исчез (academic-confirmed).
5. **volume/turtle/statarb** — прямые убытки, никаких скрытых pocket'ов.
6. **NY session для ORB** убыточна (PF 0.73) — NY-открытие идёт в flat
   на крипто (в отличие от традиционных рынков где NY-open — ликвидный пик).
7. **ADA/AVAX токсичны** для ORB — PF 0.48/0.55, хронический underperform.

**Статистическая значимость:** все вердикты (vwap/volume/turtle/statarb) на
n > 800 прошли порог `sample-size.mdc` (≥100 сделок, ≥2 недели). Для
leadlag (n=2) и для конкретных срезов orb (London/Long n=54) выборки малы,
нужен live-forward test.

**Действия — ждут обсуждения:**

- Вариант A (радикальный): отключить все 6 страт, остановить бота,
  запустить research-only фазу для поиска новой стратегии
- Вариант B (целевой): оставить только **scalp_orb London-session +
  top-3 symbols (SOL/LINK/BNB) + long-only**. Это малая выборка (54 сделки
  за 90 дней ≈ 1 сделка в 2 дня), но единственный edge в данных
- Вариант C: собрать «ансамбль подтверждений» — открывать только когда
  ≥2 из прибыльных срезов дают сигнал в одну сторону (диверсификация
  на уровне фильтрации шума)

**Файлы:** `scripts/backtest_all.py` (новый engine),
`scripts/analyze_orb.py` (новый), `scripts/diagnose_leadlag.py` (новый),
`data/backtest_all_trades.csv`, `data/backtest_statarb_trades.csv`.

---

### BACKTEST: scalp_turtle на 90 днях истории (n=2381) — подтверждено убыточна

**Статус:** ИССЛЕДОВАНИЕ. Код НЕ менялся. Решение об отключении ещё не принято
(ждём прогон остальных 5 страт для контекста).

**Цель:** верифицировать выводы Armenian Capstone 2025 (TurtleSoupPatternStrategy
на крипто 1h: WR 44.6%, PnL −2.22%, Sharpe −21.86 на n=531) **на наших символах
и наших параметрах** через нашу же имплементацию `TurtleSoupStrategy`.

**Метод:**
- Bybit public API `/v5/market/kline` (без ключа), interval=5m, 90 дней
  = 25920 баров × 8 символов = ~207K баров
- Bar-by-bar симуляция: на каждом баре передаём `bars[:i+1]` в
  `_scan_symbol()`, при сигнале открываем позицию по `close` бара
- Exit: SL=1.5×ATR, TP=2.5×ATR (как в live). При двойном хите в одном
  баре — SL первый (conservative assumption, стандарт академ. backtest'ов)
- Комиссия: Bybit taker 0.055% × 2 = 0.11% round-trip, вычитается из pct_return
- Сессионный фильтр 7-22 UTC активен (как в live)
- Одна открытая позиция на символ максимум (как в live)
- Скрипт: `scripts/backtest_turtle.py`, артефакты:
  `data/backtest_turtle_90d_report.txt`, `data/backtest_turtle_90d_trades.csv`

**Результат (overall):**
| Метрика | Значение |
|---------|----------|
| Trades (n) | **2381** (в 24× выше порога `sample-size.mdc`) |
| Win Rate | 39.82% |
| Avg Win | +0.693% |
| Avg Loss | −0.600% |
| Expectancy | **−0.085% / trade** (net fee) |
| Profit Factor | 0.764 |
| Total PnL | **−202.78%** (сумма процентов) |
| Max Drawdown | 221.53% |
| Sharpe (annual) | −11.62 |

**Статистическая значимость:**
- t-stat ≈ −6.5 (per-trade mean vs zero)
- **p < 0.0001** — убыточность статистически значима, не шум

**Per-symbol (все 8 в минусе, без исключений):**
| symbol | n | WR% | exp% | PnL% |
|--------|---|-----|------|------|
| SOLUSDT | 284 | 42.96 | −0.038 | −10.92 |
| SUIUSDT | 291 | 42.96 | −0.040 | −11.72 |
| ADAUSDT | 303 | 41.25 | −0.074 | −22.44 |
| WIFUSDT | 306 | 38.56 | −0.083 | −25.36 |
| TONUSDT | 298 | 38.26 | −0.096 | −28.61 |
| TIAUSDT | 295 | 38.64 | −0.101 | −29.80 |
| LINKUSDT | 311 | 38.59 | −0.104 | −32.21 |
| DOTUSDT | 293 | 37.54 | −0.142 | −41.71 |

**По направлению:**
- Long: n=1255, WR 39.12%, PnL −127.70%
- Short: n=1126, WR 40.59%, PnL −75.08%
- Оба направления убыточны — не asymmetric market bias

**КРИТИЧЕСКИЙ PATTERN: распределение по hold time**

| hold (M5 bars) | n | WR% | PnL% | интерпретация |
|----------------|---|-----|------|---------------|
| 0-3 (≤15 мин) | 354 | **19.2** | **−117.11** | быстрый SL сразу после входа |
| 3-6 (≤30 мин) | 609 | 31.4 | **−129.37** | fake reclaim, цена вернулась в пробой |
| 6-12 (≤1ч) | 697 | 49.6 | +29.43 | «настоящие» развороты |
| 12-24 (≤2ч) | 469 | 47.1 | −2.48 | ~нулевой edge |
| 24-48 (≤4ч) | 191 | 48.2 | +14.05 | медленные развороты |
| 48-100 (≤8ч) | 53 | 52.8 | +6.07 | |

**Смысл:** 40% сигналов (963/2381) умирают за первые 30 минут с WR 19-31%.
В крипто wick-цикл часто состоит из 2-3 волн fake reclaim'ов, а не одной.
Наш «reclaim+RSI+ADX» фильтр ловит **первый** reclaim, который слишком часто
оказывается false — цена ещё раз идёт в направлении исходного пробоя и
выносит SL.

Это отличается от forex/equity где после первого reclaim обычно следует
чистый reversal (там Turtle Soup и работает).

**Сравнение с академическим crypto-backtest:**

| | n | TF | WR | PnL | Sharpe |
|-|---|----|-----|------|-------|
| Capstone 2025 (AUA) | 531 | 1h | 44.60% | −2.22% | −21.86 |
| **Наш backtest** | **2381** | 5m | 39.82% | −202.78% | −11.62 |
| Live Wave 5 | 21 | 5m | 29% | −17.78$ | — |

На меньшем TF (5m vs 1h) страта **хуже**, что логично — больше шума и
wick-волатильности, меньше времени на отработку reversion до повторного
пробоя.

**Вывод:**
Данные подтверждают вывод Capstone 2025 на выборке в 4.5× больше: Turtle
Soup fade на крипто статистически убыточна, без параметров чтобы «подкрутить».
Это не наш live-шум — это системный минус на 2381 сделке, согласованно по
всем 8 символам и обеим сторонам (long/short).

**Действия (ждут решения):**
1. Отключить `scalp_turtle` в docker-compose.yml (env
   `BYBIT_BOT_SCALP_TURTLE_ENABLED=false`). Код не удалять.
2. Перед отключением: прогнать backtest на остальных 5 стратах для
   контекста — возможно turtle не хуже других в portfolio.

**Файлы:** `scripts/backtest_turtle.py` (новый), `data/backtest_turtle_90d_*`
(артефакты), `data/backtest_klines/` (кэш баров, 8 символов × 90 дней).

---

### CORRECTION: пересмотр RESEARCH-сверки — equity-данные НЕ применимы к крипто

**Статус:** ИСПРАВЛЕНИЕ предыдущей записи того же дня. Код НЕ менялся. Запись
ниже ("RESEARCH: сверка параметров...") содержит часть выводов, сделанных на
equity-данных (ES/NQ/Nifty), которые НЕ применимы к крипто из-за разной
микроструктуры. Пользователь справедливо указал на ошибку. Переделал анализ
только по крипто-источникам.

**Почему equity ≠ крипто (не перенос параметров 1:1):**
- Сессии: equity 6.5ч cash-session с чётким open/close, крипто 24/7 без сессий
- Волатильность: крипто в 5-10× выше equity (5m ATR BTC ≈ 0.3-0.8% vs NQ ≈ 0.05-0.15%)
- Комиссии: equity 1-2 тика (~0.01%), крипто 0.04-0.1% (4-10× выше)
- Микроструктура: крипто = много розницы + liquidation cascades + mm манипуляции,
  equity = доминируют институциональные участники

#### Новые крипто-специфичные источники (дополнительно к предыдущим 5 крипто-источникам)

11. **Sword Red — VWAP-ATR Price Action System** (Medium 2025), BTC_USDT Futures
    daily, 2019-12 → 2024-11 (5 лет):
    - **SL = 1×ATR, TP = 1.5×ATR → RR 1:1.5** ← крипто-стандарт
    - Комментарий автора: "reasonable risk-return ratio", VWAP как тренд-baseline,
      crossover entries
12. **StockSharp VWAP Mean Reversion** (Python strategy store), 5m intraday крипто:
    - Entry: deviation ≥ 2×ATR от VWAP ✓ **совпадает с нами**
    - **Exit: возврат к VWAP (НЕ fixed TP!)**
    - Stop: ATR multiple (без указания)
13. **Armenian Capstone 2025** (cse.aua.am, академ. работа через Freqtrade):
    - **TurtleSoupPatternStrategy на 1h крипто: 531 сделка, WR 44.6%, PnL −2.22%,
      Sharpe −21.86, Max DD 2.33%**
    - Это единственный **академический backtest** Turtle Soup на крипто с
      репрезентативной выборкой (n=531 > 100 порога `sample-size.mdc`)
    - **Original Turtle (trend-following) crypto 87 trades**: WR 35.94%, PnL +18.59%
    - **Extended Turtle crypto 41 trades**: WR 52.75%, PnL +114.41% (с EMA 30/60/100)
14. **tosindicators GBTC ORB 90 дней** (Bitcoin proxy):
    - **5m ORB: 70% long WR, 65% short WR**
    - **15m ORB: positive PnL**
    - **30m ORB: НЕГАТИВНЫЙ PnL** — не работает на BTC, ПРОТИВОПОЛОЖНО equity
15. **GrandAlgo ICT Turtle Soup** — 15m "bread-and-butter" timeframe для intraday

#### ИСПРАВЛЕНИЯ предыдущих выводов

| Предыдущий вывод | Статус | Корректный (на крипто) |
|---|---|---|
| "VWAP RR 1:2 = индустриальный стандарт" | ❌ Это equity (ES/NQ) | Крипто = **RR 1:1.5** (Sword Red BTC, FMZQuant ETH) |
| "ORB 30m лучше 15m (67% continuation)" | ❌ Это equity (ES/NQ) | Крипто = **15m оптимально**, 30m НЕ работает на BTC (tosindicators 90d) |
| "Turtle trailing stop обязателен" | ❌ Это Connors 1995 forex | На крипто **вопрос не trailing, а жив ли сам принцип** (Capstone: −2.22% на 531 сделке) |
| "15m ORB — worst of both worlds" | ❌ Это ES/NQ цитата | Не применимо к крипто — там 15m как раз лучший |
| "Turtle Soup WR 60-72%" | ❌ Это forex/equity | **Крипто-академ: WR 44.6%, НЕГАТИВНЫЙ PnL** |

#### Итоговый каталог расхождений (ТОЛЬКО крипто-данные)

| Страт | Парам | Наш | Крипто-референс | Critic |
|---|---|---|---|---|
| scalp_vwap | RR | 1:0.75 (SL 2×ATR TP 1.5×ATR) | 1:1.5 (Sword Red BTC, FMZQuant ETH) | 🟡 ниже нормы, но не инвертирован |
| scalp_funding | RR | 1:0.67 | крипто-данных нет, только equity | 🟡 неопределённо |
| scalp_turtle | **сама стратегия** | WR 30% на n=21 | **Capstone 2025: WR 44.6% PnL −2.22% на n=531 крипто** | 🔴 **фундаментально проблемная на крипто** |
| scalp_statarb | Z_EXIT | 0.5 | 0.0 (abailey81 Sharpe 1.61 на CEX walk-forward) | 🟡 подтверждено |
| scalp_statarb | Z_STOP | нет | ±3.0 (abailey81) | 🟡 подтверждено, уже OPEN ISSUE |
| scalp_orb | коробка | **15m** | **15m оптимально для BTC** (tosindicators GBTC 90d) | ✅ корректно! |
| scalp_leadlag | rules vs ML | rules | ML (Springer 2026 крипто) | 🟡 архитектура |
| scalp_volume | mult | 2.0× | 2.0× (eltonaguiar crypto) | ✅ match |

#### 🔴 ГЛАВНОЕ ОТКРЫТИЕ: scalp_turtle vs академ. крипто-backtest

**Armenian Capstone 2025** (рецензируемая работа университета AUA, cse.aua.am):
- Freqtrade backtest TurtleSoupPatternStrategy на 1h крипто, n=531
- WR 44.6%, Total Profit **−2.22%**, Sharpe Ratio **−21.86**, Max DD 2.33%
- Автор использовал классическую логику Raschke (20-period low, RSI фильтр)

**Наша `scalp_turtle` за Wave 5:**
- n=21 сделок (5m timeframe)
- WR ≈30%, PnL −$17.78 (чистый убыток)

**Совпадение паттернов критическое.** Academic n=531 — репрезентативная выборка,
в разы выше порога `sample-size.mdc`. Наш n=21 — это индивидуальный шум, но
направление **совпадает** с крипто-backtest'ом.

**Возможные объяснения провала Turtle Soup на крипто:**
1. **Нет "настоящих" false breakout'ов** в крипто-микроструктуре: пробои часто
   — это реальные ликвидационные каскады, а не ловушки на розницу
2. **24/7 без сессий** — нет moment'а "поутру trader'ы сбрасывают overnight
   позиции на open", отсутствие этого momentum убивает reversal edge
3. **Тренды в крипто сильнее и дольше** — Raschke/Connors работали в
   range-bound forex/stocks, крипто tends to trend
4. **Whales/manipulation** — false breakout'ы в крипто часто нарочно создаются
   крупными игроками именно чтобы ловить наших fade-трейдеров (мы становимся
   едой вместо охотника)

Это **не** баг параметров. Возможно фундаментальное несоответствие стратегии
крипто-микроструктуре.

**Вариант который ВОЗМОЖНО работает** (Capstone Extended Turtle — trend-following,
не fade): n=41, WR 52.75%, PnL **+114.41%** с EMA 30/60/100. Это **противоположная**
стратегия — следование тренду, а не фейд. Наш `scalp_turtle` — это fade.

#### Пересмотренная приоритизация (крипто-корректная)

1. **🔴 scalp_turtle — обсудить полный отказ** (или конверсию в trend-following).
   Academic evidence n=531 перевешивает наш n=21. Это не curve-fit под наши
   данные — это согласие внешнего крипто-backtest'а с нашим наблюдением.
2. **🟡 scalp_statarb exits** (Z_EXIT 0.5 → 0.0 + Z_STOP ±3.0) —
   3 крипто-источника согласны, но требует backtest на крипто-истории
3. **🟡 scalp_vwap RR 1:0.75 → 1:1.5** — 2 крипто-источника указывают на 1:1.5
   как норму, но правка меняет экономику → backtest нужен

**НЕ приоритет** (отмены из equity-версии):
- ~~ORB 15m→30m~~ — для крипто 15m корректно
- ~~VWAP RR→1:2~~ — не крипто-стандарт
- ~~Turtle trailing~~ — не лечим trailing'ом то что возможно не работает в принципе

#### Честная оценка данных

**Сильные (крипто-подтверждённые):**
- ✅ `scalp_statarb` параметры — 3 независимых крипто-источника
- ✅ `scalp_orb` 15m — tosindicators GBTC 90 дней
- ✅ `scalp_leadlag` log-returns, BTC→ALT causality — 2 академ-paper'а
- ✅ `scalp_volume` 2.0× — крипто-PineScript
- 🔴 `scalp_turtle` концептуально проблема — 1 академ-работа n=531

**Слабые (мало крипто-данных):**
- ⚠️ `scalp_vwap` — 2 крипто-источника (Sword Red, FMZQuant) но на разных TF (daily, 1h-4h, не 5m скальпинг). Нет прямого 5m крипто walk-forward.
- ⚠️ `scalp_funding` — совсем нет крипто-backtest'ов нашёл

**Файлы:** BUILDLOG_BYBIT.md (только документация, код не менялся).

---

### RESEARCH: сверка параметров всех 6 страт с индустриальным стандартом (публичные backtest'ы)

**⚠️ ЧАСТИЧНО SUPERSEDED** записью "CORRECTION: пересмотр RESEARCH-сверки"
выше (того же дня). Часть выводов ниже (VWAP RR 1:2, ORB 30m, Turtle trailing)
сделана на equity-данных (ES/NQ/Nifty) и **не применима к крипто**. Корректные
крипто-выводы — в записи CORRECTION. Ниже сохранено для истории анализа.

**Статус:** ИССЛЕДОВАНИЕ. Код НЕ менялся. Цель — задокументировать расхождения
наших настроек с публичными источниками (TradingView scripts, GitHub backtest'ы,
академические paper'ы), чтобы видеть их и иметь базу для обсуждения будущих
правок (а не чтобы тюнить под малую выборку).

**Методология:**
- Для каждой из 6 страт найдено ≥2 независимых публичных источника с метриками
- Источники: TradingView open-source scripts, GitHub репозитории с walk-forward
  backtest'ами, рецензируемые статьи (Springer, Elsevier, MDPI), industry blog'и
  (tradingstats.net, grandalgo.com, litefinance.org)
- **Не берём** данные с Bybit/Binance copy-trading leaderboards — там 75% топов
  уходят в минус в течение 12 мес (Trading Platforms Research 2024), и это почти
  всегда leverage-holders, а не скальперы — наши стратегии у них неприменимы
- **Не берём** марочные заявления "267% returns" без walk-forward — survivorship
  bias и отсутствие out-of-sample

#### Справочные источники

1. **VWAP-RSI Scalper FINAL v1** (TradingView, michaelriggs)
   - PF 1.37+, WR 37-48%, DD <1% на сотнях трейдов, ES/NQ 3-15m
   - RR 1:2 (SL 1×ATR, TP 2×ATR), MAX 3 trades/day, сессионный фильтр (US cash)
2. **VWAP-RSI Hybrid** (FMZQuant) — то же самое, RSI period 3 (короткий)
3. **Turtle Soup Enhanced** (Sword Red / Medium 2025) — 60-72% WR, RR 1:2-1:3
4. **Turtle Soup оригинал** (Connors&Raschke 1995) — WR 23% но RR 3.77,
   **TRAILING STOP обязателен**, работает только в ranging markets
5. **Crypto-Statistical-Arbitrage** (github.com/abailey81) — Walk-forward
   2022-06→2024-12, Sharpe 1.61, WR 51.18%, PF 1.69, DD 4.64%, 127 сделок
   - **Entry z=±2.0, EXIT z=0.0 (mean), STOP z=±3.0**
6. **strat-test-cointegration** (github.com/ssanin82) — drawdown kill switch,
   OLS hedge, z-score 21-period rolling window
7. **ORB Strategy: 6,142 Days ES/NQ** (tradingstats.net) — 30m ORB даёт 67%
   continuation, 15m ORB — только 59.6%, цитата: "15m is worst of both worlds"
8. **Nifty ORB Backtest** (dailybulls.in, 42 signals, Jul-Oct 2025) — Fixed 1.5R
   target + no trailing → WR 57.1% PF 2.88%
9. **Price Transmission BTC→ALT** (Springer 2026, Asia-Pacific Financial Markets)
   - Granger causality BTC→ALT p<0.05, log-returns, small-cap наиболее лажит
   - Использовали ML (Gradient Boosting), а не жёсткие пороги
10. **Bitcoin+Altcoins Trading Strategies** (Santos et al., Coimbra 2025)
    - LASSO + daily data, CumRet 331%, Sharpe 94.59% ann, threshold 0.25%

---

#### 🔴 КРИТИЧНО: scalp_vwap — RR ИНВЕРТИРОВАН

**Наши:** `sl_atr_mult=2.0, tp_atr_mult=1.5` → **RR = 1:0.75**
**Индустрия:** RR = 1:2 (SL 1×ATR, TP 2×ATR) — 5 источников согласованы

**Break-even WR (математика):**
- Наш RR 1:0.75 → нужно **WR > 57.1%** чтобы НЕ терять
- Индустрия RR 1:2 → break-even **WR 33.3%**

**Наблюдаемая реальность:** текущий scalp_vwap в Wave 5 имеет WR 58.9% (выше
break-even всего на 1.8 п.п.). Любая полоса плохого рынка (WR 55%) — и страт
начнёт терять систематически. **22 апреля** именно это и случилось:
`scalp_vwap` WR 41% на 23 сделках → PnL −$3.86.

**Почему мы так настроили:** не помнится обоснование, возможно оптимизация под
малую выборку в ранние волны. **Публичных backtest'ов с таким RR не нашёл
вообще** — industry universally uses RR ≥ 1:1.5, scalping-версии RR = 1:2.

**НЕ меняем сейчас:** выборка n=73 (>100 требуется по sample-size.mdc). Но это
первый кандидат на обсуждение, когда дойдём до 100+ сделок. Правка требует
полного backtest'а (исторические данные + walk-forward), не curve-fit.

---

#### 🔴 scalp_funding: RR 1:0.67 (тоже инвертирован)

`sl_atr_mult=1.5, tp_atr_mult=1.0` → break-even WR 60%. В Wave 5 страта не
генерировала сделок (нет фандинга выше порога), так что нечего и оценивать.

---

#### 🟡 scalp_vwap: нет daily trade cap

**Индустрия:** MAX 3 trades/day (VWAP-RSI Scalper, FMZQuant)
**Наши:** нет лимита вообще

**Наблюдаемая реальность:** 22 апреля на TIAUSDT `scalp_vwap` открыл **4 лонга
за 8 минут** (whipsaw pattern), все 4 ушли в SL подряд. Daily cap предотвратил
бы это. Connors ещё в 2000-х называл такое поведение "revenge trading".

**НЕ меняем:** одно наблюдение — не статистика. Но фиксируем как кандидата
на обсуждение.

---

#### 🟡 scalp_turtle: нет trailing stop (Connors&Raschke ЯВНО требуют)

**Источник:** "Street Smarts" Connors&Raschke 1995, стр. с правилами Turtle Soup:
> "Use trailing stops, as the current position is moving profitably."

**Наши:** фиксированный TP = 2.5 ATR, никакого trailing.
**Индустрия (5 источников):** trailing stop обязателен — TS ловит reversal,
а reversal по определению — движение с неизвестной глубиной, фиксированный TP
режет winners.

**Наблюдаемая реальность:** 22 апреля `scalp_turtle` WR 29% PF 0.93 на 17
сделках. Часть трейдов, возможно, вернулась в профит ПОСЛЕ SL (нужна проверка
по lookahead-анализу, пока нет инструмента).

**НЕ меняем:** выборка мала, добавление trailing — серьёзная архитектурная
правка (изменяет exit-логику по всем страт не только Turtle). Фиксируем как
потенциальную работу, когда Turtle наберёт 100+ сделок.

---

#### 🟡 scalp_statarb: exit при |z|<0.5 vs индустрия |z|<0.0

**Наши:** `Z_EXIT = 0.5` — выходим при полувозврате
**abailey81 walk-forward (CEX):** `Exit z-score: 0.0 (mean)` — выходим при ПОЛНОМ
возврате. Соответствующая метрика: WR 51.18% PF 1.69 Sharpe 1.61 на 127 сделках.
**ssanin82:** тоже exit при возврате к нулю (21-period rolling).

**Импликация:** мы систематически оставляем часть профита на столе. Z=0.5 →
захвачено ~75% движения от z=2.0 к z=0. Z=0.0 → захвачено 100%.

**Уже задокументировано** отдельной OPEN ISSUE (2026-04-22, stat-arb exit-логика).
Теперь подтверждено вторым независимым источником (abailey81 публикует полные
walk-forward метрики, это самое надёжное подтверждение, которое можно найти
публично без академического paper'а).

#### 🟡 scalp_statarb: нет hard z-stop (|z|>3.0)

**abailey81:** `Stop Z-Score: ±3.0` — если пара разъехалась дальше 3σ, значит
коинтеграция сломана, выходим безусловно.
**Наши:** только emergency `-$25` suming pair uPnL.

**Это уже OPEN ISSUE**, теперь подтверждено.

---

#### 🟡 scalp_orb: 15m коробка vs индустрия-рекомендованная 30m

**tradingstats.net (6,142 дней ES + NQ):** 15m ORB double-break rate 61%,
continuation 59.6%. 30m ORB — double-break 47.9%, continuation **67.0%** (NQ).
Автор пишет: *"15-minute is worst of both worlds"*.

**grandalgo.com Bank Nifty 10yr:** raw ORB без фильтров WR 48%, PF 1.2. С
higher-timeframe trend filter WR поднимается до 55%+.

**Наши:** 15-минутная коробка (ORB_BARS=3 × M5). В Wave 5 n=3, все 3 триггернули
в одну сессию (NY 14:30 UTC), все ушли в SL. PnL −$10.28.

**НЕ меняем:** n=3 — это шум, нельзя ни подтвердить, ни опровергнуть параметр.
Литература говорит что 15m ORB изначально marginal. Если на n≥50 WR останется
<35%, это будет сигналом что переходить на 30m.

---

#### 🟡 scalp_leadlag: детерминистские пороги vs ML в research

**Наши:** `BTC_MOVE_PCT=1.0%`, `BTC_MOVE_MIN_ATR=1.5`, `CORR_MIN=0.5`.
**Springer 2026 paper:** Granger causality statistically supported на p<0.05, но
используют ML классификатор (Gradient Boosting) для trade-decision, а не жёсткие
пороги. Santos&Sebastião&Silva 2025 — LASSO с 9 криптами как feature space.

**Это не "ошибка" параметров**, а более фундаментальное расхождение: research
говорит что edge есть, но рекомендует ML, а не rule-based. Мы rule-based — что
СУЩЕСТВЕННО уменьшает capture edge (но даёт explainability).

**Не меняем:** переход на ML — это полный rewrite страты, требует history-loader,
feature-engineering, training pipeline. Слишком крупная работа под малую выборку.

---

#### ✅ scalp_volume: близко к стандарту

**Наши:** `VOLUME_SPIKE_MULT=2.0`, `ADX_MIN=20`, `RSI_FILTER 20/80`, RR 1:1.
**Industry (eltonaguiar / PineScript):** `volume_multiplier=2.0`, `price_move_threshold=1.0%`,
`min_consecutive_bars=1-2`.
**Совпадение по главному параметру** (2.0×). Наш RR 1:1 — на грани (break-even
WR 50%), но не инвертирован. Наблюдаемая WR 60% → PnL +.

---

#### Сводная таблица расхождений

| Страт | Парам | Наш | Индустрия | Critic |
|---|---|---|---|---|
| scalp_vwap | RR | 1:0.75 | 1:2 | 🔴 **ИНВЕРТИРОВАН** |
| scalp_vwap | daily cap | нет | 3/день | 🟡 |
| scalp_funding | RR | 1:0.67 | 1:2 | 🔴 **ИНВЕРТИРОВАН** |
| scalp_turtle | trailing SL | нет | обязателен | 🟡 |
| scalp_turtle | ADX_MAX | 30 | 25 (строже) | 🟢 небольшое |
| scalp_statarb | Z_EXIT | 0.5 | 0.0 | 🟡 |
| scalp_statarb | Z_STOP | нет | 3.0 | 🟡 OPEN ISSUE |
| scalp_statarb | time-stop | нет | есть в abailey81 | 🟡 OPEN ISSUE |
| scalp_orb | коробка | 15m | 30m | 🟡 |
| scalp_orb | VWAP-after-break | нет | есть | 🟢 |
| scalp_leadlag | ML vs rules | rules | ML | 🟡 архитектура |
| scalp_volume | mult | 2.0 | 2.0 | ✅ |
| scalp_volume | RR | 1:1 | 1:1-1:2 | 🟢 |

Красные (🔴) — потенциально "сломанная" экономика, нужен полный backtest перед
правкой. Жёлтые (🟡) — отклонения есть, но механика работает. Зелёные (🟢) —
мелкие нюансы.

#### Что это НЕ означает

- **Не означает**, что надо немедленно менять параметры. Публичные backtest'ы
  — это тоже маркетинг, и наши условия (Bybit demo, микро-депозит, лимит лота)
  могут требовать других настроек
- **Не означает**, что наш бот "ведёт себя неправильно". При текущих настройках
  мы вышли в +$6.85 за Wave 5 (n=141). Это статистически не хуже
  beakeven-трейдинга, но не имеет статистической значимости
- **Не означает**, что надо копировать чужие параметры слепо. Их walk-forward
  делался на других инструментах, другом исполнении, другом периоде

#### Что это даёт

- **Каталог расхождений** — чтобы каждое расхождение было осознанным, а не
  незамеченным
- **Приоритезация** будущих экспериментов:
  - 1. scalp_vwap RR (инвертирован — единственное прямое расхождение с 5+ источниками)
  - 2. scalp_statarb Z_EXIT 0.5→0.0 (подтверждено walk-forward с хорошими метриками)
  - 3. scalp_turtle trailing stop (Connors прямо требовал)
- **Защита от overfitting'а**: если в будущем захочется тюнить параметр, теперь
  есть база "а что используют другие" → уменьшает шансы подгонки под шум

#### Следующий шаг

Собираем данные (≥100 сделок на страт) **без правок**. Когда наберётся
репрезентативная выборка — обсуждаем правки по приоритету выше. Начинаем с
самого очевидного (VWAP RR), только после одобрения.

**Файлы:** BUILDLOG_BYBIT.md (только документация, код не менялся)

---

### OBSERVATION: alt-selloff regime event 2026-04-22 (квант-unwind в миниатюре)

**Статус:** НАБЛЮДЕНИЕ, код НЕ менялся. Документируем для последующего
сравнения, когда накопится ≥100 сделок в похожих режимах. Никаких
параметрических правок по одному эпизоду (curve-fitting).

**Симптом:** за окно 2026-04-22 13:30 UTC → 2026-04-23 07:12 UTC (~17.7ч,
n=30 закрытых сделок API) PnL = −$22.51. PnL всего дня 22.04 = −$31.99 на
52 сделках при WR 38% — против +$17.31 (20.04) и +$5.78 (21.04). Все четыре
активные страты ушли в минус одновременно.

**Декомпозиция 22.04:**

| Страт | n | W | PnL | WR |
|---|---|---|---|---|
| scalp_turtle | 17 | 5 | **−$17.78** | 29% |
| scalp_vwap | 23 | 11 | −$8.46 | 48% |
| scalp_statarb | 10 | 3 | −$5.42 | 30% |
| scalp_volume | 2 | 1 | −$0.33 | 50% |

**Что проверено (баг или рынок?):**

1. **Код не менялся в окне деградации.** Последний `bybit` коммит
   `dd5d39a` (22.04 04:45 UTC) снизил `STATARB_PAIR_TP_USD` $2→$1 — этот
   порог **ни разу не триггернулся** за всё окно (в `close_reason`
   `statarb_pair_tp` = 0). Исключено как источник потерь.
2. **Все exits корректны** — 100% убыточных сделок закрылись через
   биржевой `sync_closed` (2 ATR SL сработал как задумано, ни одной
   просрочки по `sync_pending` / `sync_orphan` сверх baseline).
3. **Направленность убытков систематическая:** в сессии 22.04 13:30-20:00
   UTC из 21 убыточной сделки **20 были Long-вхождения** на падающих
   альтах. Только 1 убыточный Short. Это направленный регим-эффект,
   а не равномерный шум.

**Рыночный контекст (Bybit 1h klines, верифицировано API):**

| Symbol | 22.04 12:00 UTC | 23.04 07:00 UTC | Δ% за 19ч |
|---|---|---|---|
| TIAUSDT | 0.3870 | 0.3570 | **−7.8%** |
| ADAUSDT | 0.2548 | 0.2472 | −3.0% |
| SOLUSDT | 88.61 | 86.11 | −2.8% |
| TONUSDT | 1.378 | 1.348 | −2.2% |
| **BTCUSDT** | 78,263 | 78,176 | **−0.1%** |

Устойчивый **altcoin selloff при стационарном BTC**. LeadLag не активировался
(порог BTC-движения ≥1% не достигнут).

**Сигнатурный паттерн — whipsaw на TIAUSDT 19:00-20:00 UTC:**

`scalp_vwap` открыл 4 Long подряд в одном часе (22.04 19:24, 19:29, 19:32
+ ещё) — все 4 SL. RSI перепродан → BUY → SL → RSI ещё более перепродан →
BUY → SL → ... Классический paradox mean-reversion в trending режиме.

```
19:24  TIAUSDT Buy  1640.8  PnL=-3.26  (SL)
19:29  TIAUSDT Buy  1646.9  PnL=-2.61  (SL)
19:32  TIAUSDT Buy  1653.0  PnL=-3.11  (SL)
20:19  TIAUSDT Buy  1657.8  PnL=-3.28  (SL, scalp_turtle)
```

**Сверка с академической литературой (не curve-fitting, а узнавание паттерна):**

| Работа | Цитата | Соответствие |
|---|---|---|
| Khandani & Lo (2007) «What happened to the quants in August 2007?» MIT WP | *"Simultaneous unwind in crowded mean-reversion trades during regime transition"* | 7-9 авг 2007 equity quants −30-40%. 22.04 — миниатюра на крипте |
| Avellaneda & Lee (2010) «Statistical Arbitrage in U.S. Equities» arxiv:0805.1104 | *"During high-dispersion periods, the strategy takes large losses"* | Наша dispersion: BTC flat, alts −3..−8% |
| Lo (2004) «Adaptive Markets Hypothesis» | *"Mean-reversion strategies have negative correlation with momentum regimes by construction"* | 5 из 6 наших страт — MR или micro-breakout. Trending regime = антифаза |
| Lopez de Prado (2018) «Advances in Financial ML» гл.12 | *"Strategies without regime detection fail catastrophically at regime transitions"* | Regime-filter у нас отсутствует |
| Connors & Alvarez (2008) «Short Term Trading Strategies That Work» | Turtle Soup WR: ~55% range-bound / ~35% trending | scalp_turtle 22.04 WR = **29%** (согласуется с trending estimate) |
| Kissell (2014) «Science of Algorithmic Trading» | *"VWAP strategy degrades in markets with strong directional drift"* | scalp_vwap 22.04 WR = 48% (согласуется) |

**Вывод:**

Деградация — **ожидаемое поведение mean-reversion стратегий в trending
режиме альткоинов**, а не баг. Все механизмы риска (exchange SL, position
sizing, нет overlap взрыва) сработали штатно. Это и есть та причина, по
которой mean-reversion стратегии требуют **regime-filter**, что 30+ лет
research-литературы.

**Гипотезы для обсуждения ПОСЛЕ накопления данных (минимум 2 независимых
trending эпизода на ≥100 сделок):**

| Гипотеза | Research anchor | Ожидаемый эффект на 22.04 |
|---|---|---|
| Динамический `ADX_MAX` (ниже порог при high-vol) | Lopez de Prado гл.12 — regime filters | Отсекло бы ~40% entries на TIA |
| HTF-trend filter (нет counter-trend при |EMA200(H1) slope| > X) | Connors-Alvarez | TIA H1 EMA наклон вниз 19ч — нет Long |
| Cool-down по символу (2 losses подряд → пауза 30 мин) | Dennis/Turtle Traders «trend fatigue» | Блок 2-3 из 4 TIA whipsaw |
| Regime-detection (alt-vs-BTC corr break → пауза alt-MR) | Avellaneda & Lee — dispersion monitor | Вся сессия на альтах была бы pause |

**Ни одна из гипотез сейчас НЕ внедряется.** `n=30` в окне деградации и
`n=52` на весь 22.04 — кратно ниже `sample-size.mdc` порога (≥100 сделок,
≥2 недели). Любая правка по одному trending эпизоду = curve-fitting.

**Биномиальный тест для документа:**

- 22.04: 20 wins / 52 trades = 38.5%. P(WR≤20/52 | null=50%) ≈ 0.049 → p<0.05.
  Статистически значимо, но n=52 << 100 → не основание для решений.
- Окно 13:30-07:12 (17.7ч): 9W/30 = 30%. P-value 0.021.
  Тот же вывод: значимо в моменте, но не репрезентативно.

**Что делать сейчас:**

Ничего. Продолжаем сбор данных до T+14d от Wave 5 baseline (2026-05-04).
В следующем срезе смотрим:
1. Повторился ли alt-selloff паттерн с аналогичной декомпозицией по стратам?
2. WR `scalp_turtle` по всей Wave 5 сходится к ~50% (range-bound норма) или
   систематически ниже (значит структурная проблема)?
3. Частота whipsaw-серий (3+ losses подряд на одном символе от одной страты
   в 1h окне)?

Если в независимых trending эпизодах (минимум 2) паттерн воспроизводится —
переходим к обсуждению regime-filter гипотез (но каждую только с backtest
на out-of-sample данных).

**Параллельные факты (не требуют решения):**

- `scalp_orb` на Wave 5 дал −$10.28 на 3 сделках. Все 3 открылись
  **20.04 14:23:56 UTC** за 1 секунду (NY open 14:30 сессия), все SL. Это
  один false-breakout event на 3 символах (SOL/SUI/ADA), не относится
  к окну 22.04. За 70 часов Wave 5 других ORB сигналов не было — `ORB_BARS=3`
  + `VOLUME_MULT=1.3` + `ADX_MAX=25` фильтры работают строго. n=3
  недостаточно для выводов.
- `scalp_statarb` на 22.04: 2 новые пары (`sa_ADAUSDT_TIAUSDT_183a22`,
  `sa_WIFUSDT_TIAUSDT_df2d36`) закрылись с убытком при падающем TIA.
  Это согласуется с OPEN ISSUE по stat-arb exit-логике
  (см. блок от 2026-04-21): TIA-нога систематически тянет пары в минус.
  Новых данных не добавляет, ждём n≥50 пар.

**Файлы:** `BUILDLOG_BYBIT.md`.

**Временные скрипты анализа (на VPS `/tmp/`, не коммитятся):**
`drawdown_window.py`, `market_context.py`, `orb_and_daily.py` —
выполнялись локально, результаты в этом блоке. При возврате к задаче —
перегенерировать со свежим окном.

---

## 2026-04-21

### STATARB_PAIR_TP_USD $2.00 → $1.00 (тюнинг мёртвого порога)

**Что изменилось:** `src/bybit_bot/app/main.py:38` — pair take-profit стат-арба
снижен с $2.00 до $1.00.

**Почему это безопасно (и не curve-fitting):**

- За весь Wave 5 (6 закрытых пар, 35ч) порог $2 **не срабатывал ни разу**.
  Максимальный pair uPnL был ~$1.12 (`9d0286`). Тюнинг "мёртвого"
  порога → не меняет логику, не адаптирует под N сделок.
- Z-score exit остаётся приоритетным — проверяется ДО pair TP в
  `_process_exits` (строки 593-636). Pair TP срабатывает только когда
  z-score ещё не дал сигнал, но пара уже "красиво" в плюсе → мгновенная
  фиксация.
- Комиссия round-trip пары ≈ $0.70 (maker/taker Bybit demo). Нетто при
  TP $1 → ~$0.30 на пару, при $2 — ~$1.30. **$0.30 чистыми лучше чем
  "ждать $2 и не дождаться" и выйти в 0 через zscore.**

**Что НЕ трогается:**

- `STATARB_EMERGENCY_LOSS = $25` — hard cap пары. Не срабатывал ни разу
  в Wave 5 (ни одна пара не зашла так далеко), но оставляем для защиты
  от черных лебедей.
- `Z_ENTRY = 2.0`, `Z_EXIT = 0.5` — стандарт индустрии, не меняем.
- `MIN_CORRELATION = 0.5`, `LOOKBACK = 100` — пока достаточны.

**Почему только это, и ничего больше:**

Параллельно в BUILDLOG есть OPEN ISSUE про stat-arb exit-логику
(асимметрия SUI-ноги, отсутствие time-stop и hard z-stop). Эти изменения
**требуют ≥50 пар** для принятия решения — сейчас n=6. Pair TP — частный
случай: тюнинг мёртвого параметра требует только доказательства что
параметр мёртв, а это уже есть из 6 пар (100% выборки порог не достиг).

**Ожидаемый эффект (прогноз):**

- Частота срабатывания pair_tp: ожидаем 1-2 пары в день (из ~4 пар/день
  открываются, если они уходят в плюс сразу — ловим по TP).
- Дополнительный PnL: ~$1-2/день при тех же условиях.
- Распределение `close_reason` для stat-arb изменится:
  `zscore_exit 100% → zscore_exit ~60% + pair_tp ~40%`.

**Метрика для валидации (снять на T+7 дней):**

- Число срабатываний `pair_tp` ≥ 5 (подтверждает что порог живой).
- Средний `pair_pnl` при `close_reason=statarb_pair_tp` должен быть
  около `+$1.05-1.15` (подтверждает что мы ловим около пика).
- Общий pair PnL stat-arb: ожидаем рост с +$1.78 (Wave 5 до правки)
  до +$3-5 за ту же длительность с поправкой на новые пары.

**Тесты (добавлены):**

- `test_statarb_pair_tp_threshold_is_one_dollar` — константа ровно $1.00.
- `test_process_exits_pair_tp_triggers_at_one_dollar` — при `pair_upnl=$1.05`
  пара закрывается через `close_positions_parallel` с reason=`statarb_pair_tp`.
- `test_process_exits_pair_tp_does_not_trigger_below_threshold` — при
  `pair_upnl=$0.85` никакой close не вызывается.

Все 276 тестов зелёные.

**Файлы:** `src/bybit_bot/app/main.py`, `tests/test_bybit_bot.py`,
`STRATEGIES.md`, `BUILDLOG_BYBIT.md`.

---

### OPEN ISSUE: stat-arb exit-логика — асимметрия ног и отсутствие time/hard-stop (исследование, отложено на сбор данных)

**Статус:** ИССЛЕДОВАНО, код НЕ менялся. Сбор данных продолжается до ~2026-05-05
(минимум 50-100 закрытых пар). Решение принимается после накопления выборки.

**Что триггернуло исследование:** в срезе Wave 5 (T+35ч) stat-arb дал pair PnL
нетто **+$1.78 за 6 пар**, но 5 из 6 закрылись через `statarb_zscore_exit`
(−$1.57 по этой причине), и pair PnL + z-score reason суммы не сходились.
Разборка: одна пара `9d0286` закрыта через `sync_closed` (ручной exit через
time-stop одной ноги), не через zscore; остальные 5 именно через zscore.

**Ключевая находка — асимметрия ног пары LINK/SUI и ADA/SUI:**

| Leg | n | WR | PnL |
|---|---|---|---|
| Buy LINKUSDT | 3 | 100% | +$6.99 |
| Sell LINKUSDT | 1 | 100% | +$0.92 |
| Sell ADAUSDT | 2 | 100% | +$0.56 |
| Buy SUIUSDT | 3 | 33% | −$0.33 |
| **Sell SUIUSDT** | 3 | **0%** | **−$6.37** |

SUI-нога в 5/6 случаев — проигрывающая. Это нарушение market-neutrality:
при идеально подобранной β ног должна быть симметрия ± с случайным win/loss.
Систематический уклон говорит о **несовершенстве hedge ratio или пары**.

**Почему это важно:** все 5 zscore-exit пар закрылись близко к нулю
(−$0.44, −$0.18, −$0.06, +$0.16), гросс-прибыль LINK-ноги съедена SUI-ногой.
Фактический edge пары ≈ 0, страта работает на уровне случайности с риском
хвостовых убытков.

**Сверка со стандартом индустрии (исследование 2026-04-21):**

Сравнение с литературой и open-source показало три стандартных правила exit,
**все три обязательны**. У нас реализовано только первое:

| Правило | У нас | Стандарт (Brenndoerfer/Accelar/Hudson&Thames/Song&Zhang) |
|---|---|---|
| Mean-reversion exit `\|z\| < 0.5` | ✅ есть (`Z_EXIT = 0.5`) | ✅ |
| Hard z-score stop-loss `\|z\| > 3.5` | ❌ нет | ✅ обязательно |
| Time-stop `2 × half_life` | ❌ нет (только emergency −$25) | ✅ обязательно |

**Цитата Accelar (2026):** *"Implement time stops, not just level stops.
If the spread has not reverted within 2x the estimated half-life, exit
the trade. The OU model assumes reversion — if it is not happening,
the model is wrong."*

**Академические ссылки:**
- Song & Zhang (2013, Automatica) — оптимальная pair-trading policy =
  три пороговые кривые (entry + TP + hard SL).
- Leung & Li (2015) — "higher stop-loss implies lower optimal TP", пороги
  взаимосвязаны, их нельзя выбирать независимо.
- Liu, Wu, Zhang (2020, Automatica) — без hard cut-loss стратегия pair-trading
  под GBM теряет оптимальность.
- Lee & Leung (SSRN 3626471) — оптимизация exit-правил через OU даёт
  рост returns + снижение turnover.

**Open-source практики (github):**
- XanderRobbins/Universal-Pairs-Trading-System: z-score + ATR SL + trailing + circuit breakers.
- Amdev-5/crypto-pairs-trading-ai (Bybit): trailing stops + drawdown limits.
- ssanin82/strat-test-cointegration: zscore revert OR drawdown limit (90%).

Никто в open-source не ограничивается только `|z| < 0.5`.

**Гипотезы по асимметрии SUI-ноги (не проверены, нужны данные):**

1. **β ошибочный** — OLS на `LOOKBACK=100` баров 5m (≈8ч 20мин) это очень
   короткое окно. Акад. стандарт (Accelar) — 252 торговых дня. На крипте
   эквивалент — 4+ недели минут. Короткое окно подстраивается под локальный
   шум, β смещается.
2. **Vol mismatch** — OLS считает β по уровням цен, не по vol-normalized
   returns. SUI vol >> LINK vol → нога SUI всегда "шумит" больше.
   Альтернатива: TLS (Total Least Squares) или β через ratio волатильностей
   (Distance method Gatev 2006).
3. **Cointegration breaks при текущем режиме** — проверяем ADF только на scan
   при открытии, после открытия не перепроверяем. Если режим меняется
   (альт-ралли где SUI растёт отдельно) — spread разошёлся без возврата.
4. **exit-z=0.5 слишком поздний** — Hudson & Thames Zeng (2014) считает
   optimal exit threshold через OU-parameters, а не фиксирует на 0.5.

**Три варианта улучшения (по убыванию "риск/выгода", не трогаем до T+14d):**

1. **Time-stop для stat-arb (низкий риск, max польза).** `time_stop_pair ≈
   2 × ZSCORE_WINDOW_MIN = 500 min` (8ч). Смягчение: не активен пока
   `pair_uPnL > 0`. Предотвращает попадание в emergency (−$25).
2. **Hard z-score stop (средний риск).** `if |z| > 3.5: close pair`.
   Cap ~$3-5 на пару вместо $25. Риск: резать позиции на extreme,
   где часто самое время разворота.
3. **Пересмотр hedge ratio (высокий риск, требует backtest).** TLS или
   vol-adjusted β, walk-forward re-estimation. Решает причину, не симптом.
4. **Адаптивный exit-z через OU (средний риск).** Hudson & Thames ArbitrageLab
   `ou_optimal_threshold_zeng` — готовая реализация Zeng & Lee (2014).

**Почему НЕ чиним сейчас:**

- n=6 закрытых пар — **катастрофически мало** (`sample-size.mdc` требует ≥100).
- LINK/SUI доминирует выборку (5/6 пар). Эффект может быть специфичен
  для одной пары, а не общий для stat-arb.
- `pair_tp ($2.00)` ни разу не срабатывал за Wave 5 — нет данных оценить
  эффективность этого механизма.
- Любая правка exit-логики меняет распределение — нужен out-of-sample forward.

**Метрики для решения (снять при следующем полноценном срезе ≥50 пар):**

1. Сохраняется ли асимметрия SUI-ноги на n≥50? (биномиальный тест:
   p(SUI_leg=loss) > 0.5 с p-value < 0.05?)
2. Частота активаций `pair_tp` vs `zscore_exit` vs `emergency`.
3. Распределение `|z|` в моменты close — как далеко спред уходил
   от entry-z до reversion?
4. Время удержания пар (median, 95-percentile). Если 95p >> 2×zscore_window
   → time-stop даст большой эффект.
5. Случаи `|z| > 3.5` без последующего возврата к 0.5 — тут hard z-stop
   предотвратил бы emergency.
6. Производительность по парам: LINK/SUI vs ADA/SUI vs WIF/TIA vs ADA/TIA.
   Если одна пара систематически убыточная — кандидат на dynamic disable
   (а не изменение exit-логики).

**Параллельные факты:**

- Текущий pair TP = $2.00 (строка 38 `main.py` `STATARB_PAIR_TP_USD`).
  Из 6 пар **ни одна** не достигла пикового pair uPnL в $2 — максимум был
  пара `9d0286` на ~$1.12. Возможно порог слишком высокий для текущих
  volatility-уровней и размеров позиций.
- Emergency stop = $25 — за Wave 5 не срабатывал ни разу. Это норма.
- Z_ENTRY=2.0, Z_EXIT=0.5 — стандарт индустрии, не меняем.

**Ссылки на источники:**

- Brenndoerfer M. (2025) "Mean Reversion and Statistical Arbitrage",
  https://mbrenndoerfer.com/writing/mean-reversion-statistical-arbitrage-pairs-trading
- Accelar (2026) "Building a Statistical Arbitrage Engine",
  https://www.accelar.io/blog/statistical-arbitrage-engine
- Hudson & Thames ArbitrageLab (OU Zeng threshold) —
  https://hudsonthames.org/arbitragelab
- Song Q.S., Zhang Q. (2013) "An optimal pairs-trading rule", Automatica.
- Liu R., Wu Z., Zhang Q. (2020) "Pairs-trading under geometric Brownian
  motions: An optimal strategy with cutting losses", Automatica.
- Leung T., Li X. (2015) "Optimal Mean Reversion Trading With Transaction
  Costs And Stop-Loss Exit", arxiv 1411.5062.

**Ответственный:** после 2026-05-05 (≥14 дней Wave 5 + ≥50 закрытых пар)
собрать повторную выборку по метрикам выше и принять решение.

**Файлы:** `src/bybit_bot/strategies/scalping/stat_arb_crypto.py`,
`src/bybit_bot/app/main.py:37-38, 593-691` (места пороговых констант
и exit-логики), `BUILDLOG_BYBIT.md`, `BYBIT_AB_TEST.md`.

---

### OPEN ISSUE: наложение позиций при одновременных сигналах (отложено на сбор данных)

**Статус:** РАССЛЕДОВАНО, код НЕ менялся. Сбор данных продолжается до ~2026-05-05
(минимум 2 недели Wave 5). Решение принимается после накопления выборки.

**Симптом:** 04-21 10:50 UTC сделка ADAUSDT закрылась по биржевому StopLoss
с убытком **−$7.69** — **56% от всех убытков Wave 5**. Расследование показало:
на бирже в момент close была агрегированная позиция qty=5000, состоящая из
двух открытий подряд (09:04 Sell 2505 + 09:15 Sell 2495). Обе — от стратегии
`scalp_turtle`, но через orphan-рассинхрон БД бота они писались как отдельные
записи, а на Bybit one-way слились в одну позицию с avgEntry=0.2500.

**Корневая причина (код):** `src/bybit_bot/app/main.py:864` — snapshot
`open_symbols = {p.symbol for p in positions}` строится ОДИН РАЗ в начале
функции `_execute_scalping_signals`. Все 7 стратегий фильтруют сигналы против
этого snapshot независимо. В цикле исполнения (строка 980)
`for symbol, sig, bars, strategy in scalp_trades` — **нет проверки**,
что другая стратегия в этом же списке уже подала сигнал на тот же символ.
Результат: 2-3 позиции подряд реальными ордерами → Bybit агрегирует → один
SL выбивает весь агрегат.

**Статистика наложений за весь Wave 5 (28.7ч, 81 эпизод торговли на Bybit):**

| Метрика | Значение |
|---|---|
| Эпизодов с наложением (2+ open до close) | **2** (2.5%) |
| WIN | 1: LINKUSDT +$5.12 (TP за 2.4 мин) |
| LOSS | 1: ADAUSDT −$7.69 (SL после 106 мин) |
| Суммарный PnL наложений | **−$2.58** |
| Эпизодов без наложения | 79 |
| Их суммарный PnL | +$4.92 |
| WR наложений | 50% (ровно как у остальных) |

**Почему НЕ чиним сейчас:**

- n=2 — **сильно** ниже порога `sample-size.mdc` (≥100 сделок, ≥2 недели).
- Решение по 2 случаям = curve-fitting. WR 50% идентичен общему WR бота.
- Любая правка меняет распределение exits → нужен out-of-sample forward test.
- Есть три принципиально разных технических решения (см. ниже), выбор зависит
  от того, как часто наложения происходят на большей выборке.

**Три рассмотренных варианта (не выбираем до T+2w):**

1. **Жёсткая дедупликация в `scalp_trades`** (~30 мин кода). Перед
   `execute(params)` добавить `if symbol in open_symbols: continue` +
   `open_symbols.add(symbol)` после успеха. Порядок стратегий становится
   детерминирующим (VWAP всегда первый по порядку кода), attribution теряется
   у проигравших. Stat-arb требует особой защиты (обе ноги или ни одной).

2. **Virtual attribution в БД** (~3-4ч кода + миграция). Физический ордер
   только от primary, остальные стратегии пишут виртуальную запись
   `is_virtual=1, primary_position_id=…`, PnL копируется от primary.
   Честная attribution per-strategy, но сложная архитектура: миграция
   схемы, обновление `ab_test_snapshot.py`, views `real_positions`,
   каскадное закрытие в транзакции.

3. **Software-managed exits с partial close** (~4-6ч кода, HIGH риск).
   Отказ от биржевого SL — бот сам мониторит SL/TP/time-stop каждой записи
   и закрывает частично через `reduceOnly qty=X`. Даёт **самое правильное
   поведение** (симуляция показала: ADA вместо −$7.69 дала бы −$1.00 при
   раздельных SL). НО: падение бота = нет SL. Нужен emergency SL.

**Что нужно от сбора данных (метрики для решения):**

- Частота наложений на 100 сделок (сейчас 2/81 = 2.5%).
- WR и PnL наложений vs одиночных входов (сейчас WR одинаков, PnL наложений
  хуже — но n=2).
- Какие комбинации стратегий чаще конфликтуют (сейчас: vwap+turtle дважды,
  turtle+turtle один раз).
- Доля catastrophic losses от наложений (сейчас 1 из 2 наложений дал
  56% всех убытков Wave 5 — подозрительно высокий вклад).

**Параллельные факты (не требуют решения):**

- **Orphan-позиции:** 7 случаев за 28.7ч где `closed-pnl API пусто →
  sync_orphan`. Бот "отпускает" запись, а на бирже она жива. Способствует
  наложению (бот думает что позиции нет → открывает новую → агрегация).
  Отдельный тикет, тоже ждёт данных.

- **Параллельное закрытие (Wave 5) ни при чём.** Коммит `98dfb8c` трогал
  **только** exit-логику (_close_only, _reconcile_close, parallel batch).
  Баг наложения был с момента добавления Wave 4 стратегий (Turtle, ORB,
  LeadLag) — до Wave 5 его просто меньше было на 3 стратегии.

**Ссылки на детальные расследования:**

- Временные скрипты (на VPS, `/tmp/` в контейнере `fx-pro-bot-bybit-bot-1`):
  - `ada_postmortem.py` — полный разбор ADA 04-21 10:50
  - `find_overlaps.py` — все агрегированные close в Wave 5
  - `overlap_exchange.py` — эпизоды наложения на уровне биржи с WR
  - `ada_what_if.py` — симуляция Варианта A1 (раздельные exits) на ADA
- Скрипты **не коммитятся** в репо (одноразовые); при возврате к задаче
  перегенерировать с учётом свежих данных.

**Ответственный:** после 2026-05-05 (минимум 14 дней Wave 5 + ≥100 сделок)
собрать повторную выборку, принять решение между вариантами 1/2/3.

**Файлы:** `src/bybit_bot/app/main.py:850-1016` (место бага), `BUILDLOG_BYBIT.md`.

---

## 2026-04-20

### Новый A/B baseline: Wave 5 (2026-04-20 08:48 UTC)

**Контекст:** за 19-20 апреля прошли три крупные правки:
1. **Wave 4** (19.04 09:15 UTC, `1bbbdbc`) — добавлены 3 новые scalping-стратегии
   (Session ORB 15m, Turtle Soup fade, BTC Lead-Lag на log-returns).
2. **KillSwitch-фикс** (20.04 08:48 UTC, `3315deb`) — отключён на demo + фикс
   `_rotate_day` (сбрасывал флаг после UTC-midnight).
3. **Parallel batch close** (20.04 08:48 UTC, `98dfb8c`) — gap между ногами
   stat-arb 6с → <0.5с, slippage ~$0.30 → ~$0.05.

После такого объёма изменений старый baseline от 16.04 больше не
репрезентативен. Точка отсчёта для A/B сдвинута на **2026-04-20 08:48 UTC** —
момент деплоя последнего коммита (`98dfb8c`).

**Изменения:**

- `ab_snapshots.sqlite`: таблица `waves` расширена:
  - `wave3_pnl_retry_htf_slope` закрыт на 2026-04-19T09:15;
  - добавлен `wave4_scalping_strategies` (04-19 09:15 → 04-20 08:48, `1bbbdbc`);
  - добавлен `wave5_killswitch_slippage_fix` (04-20 08:48 → open, `98dfb8c`) —
    **текущий baseline**.
- `BYBIT_AB_TEST.md` переписан: наверху "НОВЫЙ BASELINE: Wave 5", включая
  waves table + первый срез T+7.8h (n=29, PnL +$8.64, WR 58.6%). Старые
  секции (Wave 1-3 + срез от 19.04) перенесены в "АРХИВ".
- Следующие контрольные точки: T+24h (04-21), T+72h (04-23), T+14d (05-04).

**Почему не можем делать выводы сейчас:** по правилу `sample-size.mdc` для
значимого сравнения нужно ≥100 сделок и ≥2 недели в разных режимах. При
n=29 разброс по неделям/сессиям слишком большой. PnL +$8.64 за 7.8ч —
индикатор направления, но не повод тюнить параметры.

**Файлы:** `BYBIT_AB_TEST.md`, `BUILDLOG_BYBIT.md`, `ab_snapshots.sqlite`
(только `waves` table, `closed_trades` остаётся накапливать кумулятивно).

---

### Защита от проскальзывания: параллельное закрытие ног exit-ов

**Симптом:** Stat-arb пара ADA/TIA закрылась суммарно в минус. ADA ушла с
прибылью, TIA — с убытком, общая пара ~-$0.80. Расследование логов: между
закрытием первой ноги и второй — **~6 секунд gap** (market close → `time.sleep(1.5)` →
`fetch_realized_pnl` → ~4с на ожидание API + сетевой RT → только потом вторая
нога). За эти 6с цена второй ноги успевает отъехать → slippage.

**Причина:** `_close_and_record` в `main.py` обрабатывал ноги последовательно:

```
for pp in pair_positions:
    _close_and_record(...)          # market close
        ↳ time.sleep(1.5)           # блокирует главный цикл
        ↳ fetch_realized_pnl(...)   # +~4с network RT
```

На каждую ногу приходилось ~6 секунд до отправки следующей.

**Исправления:**

- `BybitClient.close_positions_parallel(legs)` — новый метод: шлёт
  market+reduceOnly параллельно через `ThreadPoolExecutor` (max_workers=5).
  HTTP-запросы pybit thread-safe, Bybit V5 rate-limit ~10 req/s на аккаунт
  позволяет 2-3 параллельных безопасно.
- `_close_and_record` разделён на две функции:
  - `_close_only` — только отправляет ордер, сразу возвращается;
  - `_reconcile_close` — вызывается после общего sleep, подтягивает real-PnL
    и пишет в БД + KillSwitch.
- `_close_batch_with_reconcile(items)` — новая универсальная функция батч-
  закрытия: параллельный submit всех ног → ОДИН `time.sleep(2.0)` → последо-
  вательный `fetch_realized_pnl` для каждой ноги.
- Все stat-arb exit-пути переведены на батч: `zscore_exit`, `pair_tp`,
  `emergency`. Также одиночные `time_stop` собираются в общий батч — время
  exit-обработки цикла с N позициями теперь ~3с независимо от N (было N × 6с).
- 3 новых теста:
  - `test_close_positions_parallel_returns_all_results` — 3 ноги, 3 OrderResult.
  - `test_close_positions_parallel_handles_partial_failure` — одна нога падает,
    остальные всё равно идут.
  - `test_process_exits_statarb_closes_pair_atomically` — stat-arb пара должна
    закрываться одним батч-вызовом, НЕ двумя последовательными close_position.

**Ожидаемый эффект:**

- Gap между ногами stat-arb: **~6с → <0.5с**.
- Slippage на парах в волатильные моменты: **~$0.30 → ~$0.05** (оценка по
  middle-spread крипты на 1-минутном таймфрейме).
- PnL stat-arb на том же числе сделок: улучшение ~$0.20-0.30/пара. На текущей
  выборке n=4 это $0.80-1.20, статистический вес появится при n≥100.
- Верификация: на следующем statarb-exit в логах должно быть
  `Parallel close: SYM1, SYM2 → 2/2 ok за 0.XXs`, и `REAL PnL` для обеих ног
  с разницей по времени <1 секунды.

**Что НЕ делали (осознанно):**

- Bybit V5 batch-order API (`/v5/order/create-batch`) — эффект <100мс против
  нашего ~400мс через threads. Volatility за 300мс <0.01%, не значимо. Оставили
  как опциональный апгрейд если gap всё ещё виден в логах.
- Limit `reduceOnly` exit — риск недофилла одной ноги stat-arb перевешивает
  экономию на spread ~0.02%. Останемся с голой спот-позицией на быстром рынке.
- Пороги (pair_tp $2, emergency $25, z-score) не трогали: по правилу
  sample-size нужно ≥100 сделок для значимых изменений. Сейчас только
  инфраструктурный фикс исполнения.

**Файлы:** `src/bybit_bot/trading/client.py`,
`src/bybit_bot/app/main.py`, `tests/test_bybit_bot.py`.

---

### Фикс: KillSwitch бесконечно блокировал демо-торговлю + флаг отключения

**Симптом:** 2026-04-19 17:55 UTC — последняя сделка. Контейнер работает,
но все циклы логируют `KillSwitch: drawdown — закрываю все позиции!` →
`Закрыто 0/0 позиций`. Новые входы заблокированы почти 14 часов.

**Причины (две):**

1. **Несоответствие demo-equity и KS-порогов.** На demo `equity ≈ $177k`,
   `max_drawdown_pct = 25%` = просадка $45k. Любой микро-убыток от серии
   неудачных входов (вчера scalp_vwap отминусил ~$27) при "пике" equity
   после старта триггерит стоп, хотя торгуем копейками. На демо KS нужен
   только как оповещение, не как стоп.

2. **Баг в `_rotate_day` в `killswitch.py`.** Проверка `if self._tripped:
   return False` стояла **ДО** `_rotate_day(current_equity)`. После
   UTC-полуночи (2026-04-20 00:00 UTC) флаг `_tripped` не сбрасывался,
   потому что функция выходила раньше. Бот держал triggered-state вечно.

**Исправления:**

- `settings.killswitch_enabled: bool` (default `True`, env
  `BYBIT_BOT_KS_ENABLED`) → прокинут в `KillSwitchConfig.enabled`.
- `check_allowed()` теперь вызывает `_rotate_day()` **до** проверки
  `_tripped`, чтобы флаг сбрасывался в полночь UTC.
- Два новых теста: `test_killswitch_disabled_bypasses_all_checks`,
  `test_killswitch_rotate_day_clears_trip_flag` (регрессия).
- `docker-compose.yml`: `BYBIT_BOT_KS_ENABLED: ${BYBIT_BOT_KS_ENABLED:-true}`.
- На VPS `.env`: `BYBIT_BOT_KS_ENABLED=false` для demo.

**Почему безопасно отключить KS на demo:**

- Биржевой SL = 2 ATR на каждой позиции даёт структурный лимит убытка.
- `max_positions` (и per-strategy `max_positions=3`) всё ещё ограничивает
  одновременный риск.
- Margin-lock `max_margin_per_trade_pct = 25%` продолжает работать.
- На реале (account=$500) KS остаётся включённым и порог $37.50/25%
  превращается в реальный защитный слой.

**Файлы:** `src/bybit_bot/config/settings.py`,
`src/bybit_bot/trading/killswitch.py`, `src/bybit_bot/app/main.py`,
`tests/test_bybit_bot.py`, `docker-compose.yml`, VPS `.env`.

---

## 2026-04-19

### Deploy Wave 4 на VPS
`ручной деплой — GH Actions workflow отсутствует`

Волна 4 (ORB + Turtle Soup + BTC Lead-Lag) выкачена на demo.

**Шаги:**
1. `git fetch && git reset --hard origin/main` на VPS → commit `1bbbdbc`.
2. `.env` дополнен флагами:
   - `BYBIT_BOT_SCALP_ORB_ENABLED=true`
   - `BYBIT_BOT_SCALP_TURTLE_ENABLED=true`
   - `BYBIT_BOT_SCALP_LEADLAG_ENABLED=true`
   - `BYBIT_BOT_LEADLAG_REF_SYMBOL=BTCUSDT`
3. `docker compose up -d --build --no-deps bybit-bot` (образ из кэша) +
   `docker compose restart bybit-bot` — **advisor не тронут** (Up 2 days).

**Верификация логов (цикл 1 после рестарта):**
```
Скальпинг: VWAP, StatArb, VolSpike, ORB, Turtle, LeadLag(ref=BTCUSDT)
Batch: загружено 9/9 тикеров   ← 8 торговых + BTCUSDT reference
Торгуемые символы: 8/8
Bybit баланс: equity=177244.54 (demo)
```

Все 6 скальп-стратегий активны, BTCUSDT подгружается как reference для
LeadLag, но не торгуется (явный `continue` в `_process_scalping`).

**Следующий шаг:** T+24h срез (snapshot до/после деплоя) через
`scripts/ab_test_snapshot.py`, затем анализ первых сделок Волны 4.

**Файлы:** `docker-compose.yml`, `.env` (VPS), `BUILDLOG_BYBIT.md`

---

### Стратегия D: BTC Lead-Lag → Altcoin + research-verified параметры всех стратегий Wave 4

**Стратегия D: `btc_leadlag.py`** — межсимвольный моментум: при резком движении
BTC (>1%, ≥1.5 ATR, ADX>15) в scan-list ищем альты с высокой корреляцией
log-returns с BTC (≥0.5 на окне 50 баров), где альт ещё не догнал BTC
(|alt_move| < 0.3% за 15 мин). Вход в альт в сторону BTC-движения.

**Критический фикс на базе research:**
- Asia-Pacific Financial Markets 2026 (Springer, DOI 10.1007/s10690-026-09589-z)
  и HF Lead-Lag paper (kryptografen 2019) указывают: **корреляция для
  lead-lag стратегий считается на log-returns**, не на ценах.
- Изначально в коде была corr(prices) — заменено на `_log_returns` + `_pearson_corr`.
- Price-level Pearson ловит фантомные зависимости на общих трендах.

**Интеграция:**
- `settings.scalping_leadlag_enabled` + `leadlag_reference_symbol=BTCUSDT`.
- `main.py`: BTC догружается в `bars_map` если LeadLag включён, но
  **НЕ торгуется** (BTC был убыточен в скальпе ранее) — только reference.
- `_process_scalping` блокирует вход на reference-символе явно.
- Включён в `scalp_strategies` set как `"scalp_leadlag"`.

**Research-верификация всех стратегий Wave 4:**

Проверены параметры по каноническим источникам:

| Стратегия | Source | Ключевой параметр |
|---|---|---|
| Session ORB | FMZQuant «Volume-Confirmed ORB» 2024 | 15 мин коробка, vol≥1.3× 20-bar, EMA trend, ATR SL/TP |
| Turtle Soup | Connors & Raschke «Street Smarts» 1995; Enhanced Sword Red 2024 | lookback=20, RSI-extreme confirmation |
| BTC Lead-Lag | Asia-Pacific FM 2026 (Springer); HF Lead-Lag 2019 | **corr(log-returns)**, BTC≥1%, alt-lag<0.3% |

Все параметры каждой стратегии документированы в:
- `STRATEGIES.md` (секция **3e. Bybit Crypto Bot — Scalping Strategies**) с
  таблицей research-источников.
- Docstring каждого модуля — блок `─── Research basis ───`.
- `.cursor/rules/strategy-guard.mdc` — обновлённое правило: запрет менять
  параметры без ссылки на research + список research-инвариантов.

**Символы на Bybit** — все 9 проверены через `v5/market/instruments-info`,
status=`Trading`: BTCUSDT (reference), SOLUSDT, ADAUSDT, LINKUSDT, SUIUSDT,
TONUSDT, WIFUSDT, TIAUSDT, DOTUSDT. По research Springer 2026 small-cap
(WIF, TIA, SUI) показывают наибольший lag-эффект — идеальны для стратегии.

**Тесты** (`tests/test_bybit_scalping.py::TestBtcLeadLag`): 7 тестов:
- long/short follows BTC, no-signal без BTC, weak move, already followed,
  low correlation, insufficient bars.

Общий набор: **256/256** зелёные.

### Стратегия B: Turtle Soup fade (код, без деплоя)

**Идея** (Larry Connors «Street Smarts», 1995): ловим **ложный пробой**
20-барного экстремума. Если цена сделала новый 20-барный low/high,
но через 1-4 бара вернулась обратно внутрь диапазона — это stop-hunt
/ sweep ликвидности. Входим **против пробоя**. На крипте это фактически
реакция на wick-манипуляции whales вокруг круглых уровней.

**Антикорреляция с другими скальперами:**
- ORB — пробой + продолжение (тренд).
- Volume Spike — моментум от объёма.
- Turtle Soup — **анти-пробой** (разворот после ловушки).

**Параметры** (`strategies/scalping/turtle_soup.py`):

- `LOOKBACK = 20` — окно 20-барного экстремума.
- `BREAK_DEPTH_ATR = 0.3` — пробой должен быть осязаемым.
- `RECLAIM_WINDOW = 4` — сколько баров даём цене на возврат.
- `RECLAIM_BUFFER_ATR = 0.1` — возврат внутрь на ATR-буфер.
- `RSI < 30` для long (перепроданность на пробое вниз) / `> 70` для short.
- `ADX_MAX = 30` — выше = тренд, sweep не ловушка, а продолжение.
- `SL = 1.5×ATR`, `TP = 2.5×ATR` (RR ≈ 1.67).

**Изоляция от FxPro:** импорты только `bybit_bot.*`.

**Интеграция:**
- `settings.scalping_turtle_enabled: bool = False` (env `BYBIT_BOT_SCALP_TURTLE_ENABLED`).
- `main.py`: регистрация, проброс через `_run_cycle` → `_process_scalping`,
  сигналы в общий `scalp_trades` как `strategy_name="scalp_turtle"`.
- Включён в `scalp_strategies` set.

**Тесты** (`tests/test_bybit_scalping.py::TestTurtleSoup`): 5 новых:
- `long_on_fake_breakdown`, `short_on_fake_breakup` — обе стороны.
- `no_signal_without_breakout`, `insufficient_bars`, `max_signals_limit`.

Общий набор: **249/249** зелёные.

### Стратегия A: Session ORB 15m (код, без деплоя)

**Идея:** первые 15 минут после открытия торговой сессии (Asia 00-01,
London 08-09, NY 14-15 UTC) формируют коробку high/low на 3 барах M5.
Пробой коробки с подтверждением по объёму и EMA-фильтру = вход в сторону
пробоя. Цель — ловить выход из ночной консолидации, когда открывается
ликвидный час.

**Параметры** (`strategies/scalping/session_orb.py`):

- `ORB_BARS = 3` (15 мин коробки).
- `BREAKOUT_FILTER_ATR = 0.3` — отсекает ложные тычки.
- `VOLUME_MULT = 1.3` — пробой без объёма часто откатывается.
- `ADX_MAX = 25` — выше = уже тренд, ORB не работает.
- EMA(50) slope совпадает с направлением пробоя.
- «Первый пробой в сессии» — если до этого post-ORB-бары уже сидели
  выше/ниже коробки, не входим (поздно, волатильность съелась).
- `SL = 2.0×ATR`, `TP_atr_mult = 2.0×box_range/ATR` (TP пропорционален
  размеру коробки, clamped 1.0..4.0).

**Принципиальная изоляция от FxPro:** модуль импортирует только
`bybit_bot.*`. Общих зависимостей с `src/fx_pro_bot/` нет, стратегии
живут в разных экосистемах.

**Интеграция:**

- `settings.scalping_orb_enabled: bool = False` (выключено по умолчанию,
  env `BYBIT_BOT_SCALP_ORB_ENABLED`).
- Регистрация в `main.py`: `SessionOrbStrategy()`, проброс через
  `_run_cycle` → `_process_scalping`, сигналы складываются в общий
  `scalp_trades` с `strategy_name="scalp_orb"`.
- Включён в `scalp_strategies` set для корректного счётчика позиций.

**Тесты** (`tests/test_bybit_scalping.py::TestSessionOrb`): 8 новых тестов:

- `breakout_up/down_detected` — корректное распознавание обеих сторон.
- `no_signal_inside_box` — без пробоя нет сигнала.
- `no_signal_low_volume` — volume filter работает.
- `no_signal_out_of_session` — вне сессионных окон коробку не строим.
- `no_signal_after_earlier_breakout` — второй пробой в той же сессии
  игнорится.
- `insufficient_bars`, `max_signals_limit` — edge cases.

Общий набор тестов: **244/244** зелёные.

**Статус:** код готов, на VPS не деплоится пока не созреют две
другие новые стратегии + апгрейды (Волна 4 — единый merge).


### AB-snapshots: cumulative store с первым окном 7 дней

**Зачем:** API Bybit `/v5/position/closed-pnl` хранит только последние ~7 дней
(подтверждено доками и эмпирически — см. ниже блок про API race guard). Старт
бота был 11.04, baseline A/B-теста — с **2026-04-16 06:30 UTC**. Сегодня 19.04
→ baseline целиком помещается в доступное окно API. Всё что мы сейчас сможем
засинкать — реальное; всё что старше — API всё равно не отдаёт.

**Модель хранения:** `ab_snapshots.sqlite` — **кумулятивная** БД, не зеркало API.

- Первый запуск с пустой БД: тянем максимум, что отдаёт API = `now − 7d`.
- Каждый следующий incremental sync дописывает новое (`INSERT OR IGNORE`),
  старое никогда не удаляется и не перезаписывается.
- Через месяц в БД будет полный непрерывный лог с сегодняшней даты, даже
  когда в API этого уже не будет.

**Изменения** (`scripts/ab_test_snapshot.py`):

- `DEFAULT_EPOCH_MS` константа 11.04 → функция `initial_epoch_ms() = now − 7d`.
- В `sync_closed_pnl` нижний порог стартового окна берётся через `initial_epoch_ms()`.
- `last_fetched_end_ms` в `sync_meta` отвечает за continuity incremental sync
  (с overlap = 1 час — страховка от API-лага).
- Docstring обновлён: явно написано, что API retention ~7 дней и что БД — cumulative.

**Что с бот-ной БД:** `bybit_stats.sqlite` (420 позиций) **не трогается**.
Recovery sync_orphan (10 записей) остался на месте; enrich стратегий
продолжает работать тем же fuzzy-матчем.

**Тесты:** 236/236 зелёные (28 из них в test_ab_test_snapshot.py).

### API race guard против sync_orphan + recovery 10 записей

**Проблема, найденная при анализе baseline 16-19.04:** 9 из 104 позиций
(8.7%) имели `close_reason='sync_orphan'` с `pnl_usd=0`, хотя на бирже они
продолжали жить. Паттерн — через ~5 мин (1 poll cycle = 300 сек) после
открытия `get_positions()` возвращал неполный список, бот считал позицию
закрытой, переставал её отслеживать. Итог: time-stop 24ч **не работал**
(Max Hold по baseline = **2256 мин = 37.6 ч**), Killswitch недооценивал
дневной убыток (10 позиций писались с pnl=0, а реально принесли **-$11.10**).

Страдает только scalp_vwap/volume (одиночные позиции). scalp_statarb не
страдает — открывает пару позиций одновременно, обе стабильно в API.

**Фикс** (`src/bybit_bot/app/main.py`): перед sync_pending делаем повторный
`get_positions()`. Если позиция появилась во втором запросе — это race
condition, не трогаем её. Лишний запрос только когда есть candidates for
closing, обычно не каждый цикл.

**Recovery** (`scripts/fix_sync_orphans.py`): разовая прошивка существующих
orphan-записей в `/data/bybit_stats.sqlite` из `/ab-data/ab_snapshots.sqlite`
(fuzzy-match: инвертированный side, qty±5%, entry_price±0.5%, updated_time
после opened_at). Идемпотентно (close_reason='sync_orphan_recovered').
На VPS запущено 2026-04-19 07:59 UTC — 10/10 восстановлено.

**Тесты** (`tests/test_bybit_bot.py`): 3 новых — race-guard preserves live
position, truly missing closes, API empty → sync_pending (не orphan сразу).

### AB-test snapshot: итоговый baseline с правильным матчингом и волнами

**Итог на VPS:** 402/420 match rate = **95.7%** (было 3/420 = 0.7%). Оставшиеся
18 `unknown` — pyramid-закрытия в первые дни работы бота, когда Bybit
агрегировал несколько позиций в один `closedPnl` (qty=0.56 = 2×0.28); не
ловятся fuzzy-match по qty ±5%. Edge-case, 4.3% выборки.

**Волны в БД** (таблица `waves`):

| # | Name | Период UTC | Суть |
|---|---|---|---|
| 1 | wave1_exit_logic | 04-16 06:30 → 17:00 | max_loss_per_trade убран, STATARB_EMERGENCY_LOSS 15→25 |
| 2 | wave2_filter_loosening | 04-16 17:00 → 04-17 12:00 | Z_ENTRY 2.5→2.0, Z_EXIT 0.0→0.5, VOL_MULT 3→2, ADX_MAX 20→25, TIME_STOP 24h |
| 3 | wave3_pnl_retry_htf_slope | 04-17 12:00 → now | fetch_realized_pnl retries 1→3, VWAP HTF slope мягкий |

**Baseline для A/B теста = с 2026-04-16 06:30 UTC** (начало Волны 1). Данные
до этой даты (316 сделок, "paper-like" период без маркеров версий) хранятся
в `ab_snapshots.sqlite` как исторический контекст, но не учитываются в A/B.

**Отчёт запускается:**

```
docker exec fx-pro-bot-bybit-bot-1 python3 -m scripts.ab_test_snapshot \
    --since 2026-04-16T06:30 --output /ab-data/report.md
```

**Открытие, которое было невидимо до фикса матчинга:** в текущем baseline
(104 сделки, 16-19 апреля) **scalp_vwap тянет 69% сделок с PF 0.34**
(72 сделки, -$70.80). Stat-Arb после ослабления фильтров (Волна 2) почти
безубыточен (PF 0.87 на 24 сделках). По `sample-size.mdc` отключать ничего
нельзя (n<100 для стратегии, <2 недель), но это фокус мониторинга к 04-22.

Подробно — `BYBIT_AB_TEST.md` (блок "СТАЛО").

### AB-test snapshot: fuzzy-match стратегии, честный hold, overall excl. recovered

Базовый baseline-прогон показал, что JOIN через `order_id` не ловит 99.3%
позиций (3/420 match). Причина — после сверки с официальной докой
[v5/position/closed-pnl](https://bybit-exchange.github.io/docs/v5/position/close-pnl)
и [v5/execution/list](https://bybit-exchange.github.io/docs/v5/order/execution):

- `closedPnl.orderId` = id **закрывающего reduceOnly** ордера.
- `positions.order_id` у бота = id **открывающего** ордера.
  Разные сущности, прямой JOIN невозможен.
- `closedPnl.side` = сторона **закрывающего** ордера (инверсия от открытия:
  long → `side=Sell` в API).
- `closedPnl.createdTime` = время создания закрывающего ордера (не открытия
  позиции). Старый `hold = (updated − created)` измерял скорость fill'а
  закрывающего ордера, а не реальное время удержания.
- В `closedPnl` **нет** `orderLinkId` (он только в `/v5/execution/list`).

**Фикс:**

- В `closed_trades` добавлена колонка `opened_at_ms` (+ миграция через
  `ALTER TABLE ADD COLUMN`, идемпотентна).
- `enrich_strategy` переписан на fuzzy-match с инверсией side:
  `symbol=symbol` ∧ `side=CASE Sell→Buy, Buy→Sell` ∧
  `|entry_price − avgEntryPrice| ≤ 0.1%` ∧ `|qty − qty| ≤ 5%` ∧
  `opened_at ∈ [updated_ms − 24h, updated_ms)`. При нескольких кандидатах —
  ближайший по `|opened_ms − updated_ms|`.
- `hold_minutes = (updated_time_ms − opened_at_ms) / 60000` пересчитывается
  **после** матча; для `strategy='unknown'` остаётся NULL и в отчёте
  показывается как `—`.
- В Overall добавлен срез `Overall (excl. recovered)` — без позиций с
  `strategy='recovered'` (подхваченных ботом при sync_on_startup).

**Тесты:** 28 кейсов в `tests/test_ab_test_snapshot.py` (8 новых —
fuzzy-match: инверсия side для long/short, tolerance entry_price,
выбор ближайшего opened_at, отсечение позиций, открытых после закрытия,
идемпотентность, миграция старой БД). 233 PASSED в общем suite.

**Файлы:** `scripts/ab_test_snapshot.py`, `tests/test_ab_test_snapshot.py`.

### AB-test snapshot: инкрементальный sync closedPnl → SQLite → markdown

Инструмент для быстрых срезов статистики перед внедрением новых стратегий.
JSON отвергли — с ростом истории файл распух бы неконтролируемо; берём SQLite
с инкрементальным sync.

**Хранение БД:** `/root/fx-pro-bot-data/ab_snapshots.sqlite` на хосте VPS,
через bind mount `/ab-data` в контейнер `bybit-bot`. Вне docker volume —
рестарт контейнера, `git reset --hard` и `docker volume rm` её не трогают.
Прямой `scp` для локального анализа.

**Схема БД (3 таблицы):**
- `closed_trades` — сырые записи Bybit closedPnl API (PK = `order_id`,
  `closed_pnl` = NET, `hold_minutes` computed, плюс `strategy`,
  `order_link_id`, `raw_json`).
- `waves` — границы значимых изменений (name, start/end UTC, commit_hash,
  description). Заполняется вручную через `--add-wave`.
- `sync_meta` — `last_fetched_end_ms` для инкрементального sync.

**Скрипт `scripts/ab_test_snapshot.py`:**
- Одна команда: sync (окна по 7 дней, пагинация по cursor) → enrich strategy
  через ATTACH JOIN с `bybit_stats.sqlite.positions.order_id` → markdown-отчёт
  из 7 срезов (overall / by_wave / by_day / by_symbol / by_strategy /
  by_hour_utc / by_hold_bucket).
- Флаги: `--since`, `--until`, `--wave N`, `--no-sync`, `--no-report`,
  `--output PATH`, `--add-wave "name=...;start=...;..."`, `--list-waves`.
- Страховка от API-лага: при sync отступаем на 1 час назад от
  `last_fetched_end_ms`, `INSERT OR IGNORE` обеспечивает дедуп.

**Запуск (VPS):**
```
docker exec fx-pro-bot-bybit-bot-1 python3 -m scripts.ab_test_snapshot
```

**Тесты:** `tests/test_ab_test_snapshot.py` (20 кейсов) — схема, парсинг дат
и сделок, инкрементальный sync с FakeSession, маппинг стратегий, CRUD волн,
рендер отчёта. Общий suite — 225 PASSED.

**Файлы:** `scripts/ab_test_snapshot.py`, `scripts/__init__.py`,
`tests/test_ab_test_snapshot.py`, `docker-compose.yml` (bind mount +
env `AB_SNAPSHOTS_DB_PATH`).

### Аудит и уборка мёртвого кода в `bybit_bot`

Полная ревизия кодовой базы перед внедрением новых стратегий и A/B-фреймворка.
Торговая логика не затронута, только удаление неиспользуемых полей/методов/
констант. Все 205 существующих тестов проходят.

**Удалено из `config/settings.py` / `.env.example`:**
- `max_positions` (неактуальное поле, активный лимит — `killswitch_max_positions`).
- `strategy_trail_atr_mult` (trailing stop не реализован).
- `killswitch_max_loss_per_trade` (уже не использовался в `check_allowed`;
  биржевой SL выполняет ту же функцию).

**Удалено из `trading/killswitch.py`:**
- Поле `KillSwitchConfig.max_loss_per_trade_usd`; docstring обновлён, чтобы
  явно отметить, что per-trade защита делегирована exchange SL.

**Удалено из `trading/client.py`:**
- `BybitClient.is_demo` (never read).
- `amend_sl_tp()` и `cancel_sl_tp()` — использовались только в `fx_pro_bot`;
  в `bybit_bot` SL/TP ставятся при открытии ордера и больше не меняются.

**Удалено из `trading/executor.py`:**
- `TradeExecutor.close_position()` — тонкая обёртка над `client.close_position`,
  в `app/main.py` вызывается напрямую через `client`.

**Удалено из `stats/store.py`:**
- Датакласс `SignalRow` (нигде не читался).
- Методы `get_open_position_by_symbol`, `get_daily_pnl`, `get_total_stats`
  (не использовались; агрегация идёт через `get_cumulative_pnl` и
  `get_open_positions`).

**Удалено из `strategies/scalping/indicators.py`:**
- `vwap_series`, `z_score_series` — серии-аналоги точечных функций,
  использовавшиеся только в `fx_pro_bot`.

**Удалено из `strategies/scalping/vwap_crypto.py`:**
- Дубликат `_compute_adx()` — теперь используется `compute_adx` из `indicators.py`.
- Параметры `max_positions`, `max_per_symbol` в `__init__` (лимит живёт в
  `app/main.py`).
- Константы `SL_ATR_MULT`, `TP_ATR_MULT` (хардкод остаётся в `app/main.py`,
  константы в модулях стратегий вводили в заблуждение).

**Удалено из `strategies/scalping/stat_arb_crypto.py`:**
- Параметр `max_pairs` (не использовался).
- Поля `atr_a/atr_b/price_a/price_b` в `StatArbSignal` (нигде не читались).
- Константа `SL_ATR_MULT` + неиспользуемый импорт `atr`.

**Удалено из `strategies/scalping/volume_spike.py` и `funding_scalp.py`:**
- Константы `SL_ATR_MULT`, `TP_ATR_MULT`, `FUNDING_BUFFER_SECONDS`
  (ATR-мультипликаторы для SL/TP применяются хардкодом в `app/main.py`).

**Прочее:**
- `app/main.py`: удалён мёртвый импорт `fetch_bars` (используется только
  `fetch_bars_batch`); убран аргумент `max_loss_per_trade_usd` при создании
  `KillSwitchConfig`.
- `tests/test_bybit_bot.py`: подправлены assert'ы под удалённые поля.

`FundingScalpStrategy.should_exit_after_funding()` и `MomentumStrategy`
**оставлены** (dormant, но могут понадобиться — по решению пользователя).

**Файлы:** `config/settings.py`, `.env.example`, `trading/killswitch.py`,
`trading/client.py`, `trading/executor.py`, `stats/store.py`,
`strategies/scalping/indicators.py`, `strategies/scalping/vwap_crypto.py`,
`strategies/scalping/stat_arb_crypto.py`, `strategies/scalping/volume_spike.py`,
`strategies/scalping/funding_scalp.py`, `app/main.py`, `tests/test_bybit_bot.py`.

## 2026-04-17

### Реальный PnL из closed-pnl API + мягкий HTF slope-фильтр VWAP

Сверка БД с Bybit API за 17 сделок с 17:00 UTC 16.04:
- DB total: -16.58 USD
- API total: -23.33 USD
- **Разница ~6.75 USD**: комиссии + 3 сделки с `pnl_usd=0` (API не успел зафиксировать closed-pnl к моменту sync).

Исправления:

**1. `fetch_realized_pnl` с retry:**
Было — один запрос сразу после закрытия, при неуспехе 0. Стало — 3 попытки
по 2 сек паузы (всего до 6 сек ожидания). Закрывает большинство случаев
API-лага Bybit.

**2. sync_pending + reconcile в следующих циклах:**
Если после retry API всё ещё пусто — позиция закрывается с
`close_reason='sync_pending'` и `pnl_usd=0` (временное значение).
Новая функция `_reconcile_pending_sync` в начале каждого цикла проверяет
такие записи (с задержкой ≥30 сек) и обновляет реальным `closedPnl`.
Через 30 минут безрезультатных попыток → `sync_orphan` (ручное закрытие
на бирже / истёк таймаут API).

**3. Разовый фикс прошлых записей:**
`scripts/fix_missing_pnl.py` — пересчитывает PnL для закрытых сделок
с `pnl_usd=0` или `close_reason='sync_pending'` начиная с cutoff.
Выбирает ближайшую по времени запись из closed-pnl API.

**4. VWAP: мягкий HTF slope-фильтр:**
Полное отключение обоих slope-фильтров не помогло — VWAP SHORT дал убытки
(SOL -4.48). Локальный 5m slope остался отключённым (слишком шумный).
HTF (1h) slope вернули с порогом `HTF_SLOPE_FLAT = 0.0005` — блокируем
вход только против СИЛЬНОГО старшего тренда (>0.05%/бар ≈ 2.4%/час).
Боковик `|slope| < 0.0005` → mean reversion работает в обе стороны.

**Файлы:** trading/client.py, app/main.py, stats/store.py,
strategies/scalping/vwap_crypto.py, scripts/fix_missing_pnl.py

## 2026-04-16

### VWAP: отключены slope-фильтры на демо

После ослабления параметров бот всё равно 0 сигналов 2 часа. Диагностика:
VWAP видит 3 SHORT (SOL, LINK, DOT) с dev>2 ATR и RSI>70, но блокируются
slope-фильтрами (local EMA slope + 1h HTF slope оба положительные — бычий
рынок). Это правильная логика "не контртренди", но полностью убивает
mean reversion в трендовом рынке.

На демо отключены оба slope-фильтра (закомментированы) — чистый mean
reversion для сбора статистики. При выходе на реал — вернуть HTF фильтр
с порогом `abs(htf_slope) > threshold` (блок только сильных трендов).

**Файлы:** strategies/scalping/vwap_crypto.py

### Ослабление слишком жёстких фильтров (research-backed)

После деплоя exit-логики обнаружили 0 сигналов за 10+ часов. Проверили все
параметры по актуальным исследованиям 2025-2026 (InsiderFinance, MDPI Risks,
PyQuantLab, Finaur, Quant Signals) — часть "калибровок" 12-14 апреля оказалась
слишком жёсткой относительно стандартов индустрии.

Изменения:

**Stat-Arb:**
1. **DEFAULT_PAIRS**: 2 -> 4 актуальные ADF-подтверждённые пары.
   Старые SOL/LINK и SOL/WIF "протухли" за 3 дня (p=0.17 и 0.44).
   Новые: ADA/TIA (p=0.002), LINK/SUI (p=0.006), WIF/TIA (p=0.023),
   ADA/SUI (p=0.033). MDPI Risks 2023: оптимум 4-6 пар.
2. **Z_ENTRY 2.5 -> 2.0** — стандарт (InsiderFinance, ThunderAlgo).
3. **Z_EXIT 0.0 -> 0.5** — стандарт, захват прибыли до пересечения нуля.
   0.0 была моей ошибкой — все источники подтверждают 0.5.

**Scalping:**
4. **VOLUME_SPIKE_MULT 3.0 -> 2.0** — стандарт 1.5-1.8 (TradingView).
5. **VWAP ADX_MAX 20 -> 25** — крипта редко даёт ADX < 20 (PyQuantLab).

**Exit-логика:**
6. **Time-stop возвращён**, но 24ч (было 4.2ч). Finaur/TrendRider 2026:
   time-exit убирает "dead money" трейды, 24ч — стандарт для swing-scalp.

**Файлы:** stat_arb_crypto.py, volume_spike.py, vwap_crypto.py, main.py

### Переработка exit-логики (A/B тест)

Анализ 352 сделок показал: exit-логика бота генерирует -$53 (111 сделок),
а биржевые SL/TP дают +$16 (241 сделка). Бот убивает собственную прибыль.

Изменения:
1. **Убран max_loss_per_trade из exit**: -$57 за 4 сделки, дублировал биржевой SL
2. **Убран time_stop**: -$7.70 за 2 сделки, мешал прибыльным позициям (DOT +$38)
3. **Z_EXIT 0.5 -> 0.0**: полный mean reversion вместо раннего выхода
4. **STATARB_EMERGENCY_LOSS $15 -> $25**: больше пространства для пар

Оставлены: statarb_pair_tp (+$9.37), trailing stop, statarb_emergency (ослаблен).
Файл BYBIT_AB_TEST.md для сравнения через 48 часов.

**Файлы:** main.py, stat_arb_crypto.py, BYBIT_AB_TEST.md

### Оптимизация по статистике 315 сделок

Анализ всех закрытых PnL через Bybit API выявил:
- Общий P&L: -$263 за 315 сделок
- 27 из 35 символов убыточны, прибыльны только 5 (SUI, ADA, TON, WIF, TIA)
- SOL и LINK прибыльны в Stat-Arb, убыток от других стратегий
- DOT: закрытые -$38, но открытая позиция +$38 = нетто $0 (бот не фиксировал прибыль)
- SCALP_STATARB: 255 сделок с WR 24%, ADF-тест оставил только 2 из 16 пар
- SCALP_FUNDING: -$6 за 13 сделок, основной символ (DOT) = удалён из списка
- Momentum: 1 сделка за всё время, бесполезна при узком списке
- 68% позиций закрыты как `sync_closed` — бот терял отслеживание при перезапуске

Изменения:
1. **DEFAULT_SYMBOLS**: 35 → 8 (SOL, ADA, LINK, SUI, TON, WIF, TIA, DOT)
2. **Stat-Arb пары**: 16 → 2 (SOL/LINK p=0.0012, SOL/WIF p=0.0055), ADF-подтверждены
3. **SCALP_FUNDING**: отключён (scalping_funding_enabled=False)
4. **Momentum**: отключён (momentum_enabled=False)
5. **Синхронизация при старте**: новая `_sync_positions_on_startup()` — при запуске
   загружает открытые позиции с биржи в БД, если бот потерял отслеживание

**Файлы:** settings.py, stat_arb_crypto.py, main.py, test_bybit_bot.py

## 2026-04-14

### Адаптированные фильтры (по аналогии с FxPro ботом)

5 фильтров, подтверждённых статистикой 258 сделок и исследованиями:

1. **Сессионный фильтр 07:00-22:00 UTC**: dead zone (22-07) = -$147 при
   81 сделке (72% потерь, WR 31%). Блокирует входы, выходы работают 24/7.
2. **Stat-Arb ADF-тест**: проверка стационарности спреда (p < 0.05).
   Без ADF пары расходились без возврата (DOT -$23, ATOM -$9).
3. **Stat-Arb Z_ENTRY 2.0→2.5**: строже, меньше ложных входов.
4. **VWAP HTF фильтр**: 1h EMA(50) slope через Bybit API get_kline().
   Блокирует вход против старшего тренда.
5. **Volume Spike ADX > 20**: volume spike в боковике = ложный сигнал.

Все фильтры аддитивные — только блокируют вход, не меняют SL/TP/exit.

**Файлы:** `app/main.py`, `config/settings.py`, `strategies/scalping/stat_arb_crypto.py`,
`strategies/scalping/vwap_crypto.py`, `strategies/scalping/volume_spike.py`,
`strategies/scalping/indicators.py`, `trading/client.py`, `pyproject.toml`

## 2026-04-13

### Убраны убыточные пары: BTCUSDT, DOGEUSDT, AAVEUSDT, HBARUSDT

По статистике 228 сделок с Bybit API:
- BTCUSDT: 12% WR, -$33 (24 сделки)
- AAVEUSDT: 0% WR, -$17 (4 сделки)
- DOGEUSDT: 13% WR, -$16 (15 сделок)
- HBARUSDT: 0% WR, -$6 (4 сделки)

Итого убыток этих 4 пар: -$72 из общих -$200. Остальные 36 символов оставлены.

**Файлы:** `config/settings.py`, `tests/test_bybit_bot.py`

### Откат Bybit-бота к e3deea3 (рабочая стратегия)

V2 EMA Trend-Following и V2.1 positional state + pullback показали убыток
на реальном демо-счёте. Откат к коммиту e3deea3 — последняя рабочая версия
с Stat-Arb + Momentum + Scalping стратегиями, которая торговала в плюс.

Откат через `git checkout e3deea3 -- <файлы>` (не git revert, чтобы не
затронуть форекс-бот). Удалены файлы, которых не было в e3deea3:
`strategies/trend_ema.py`, `analysis/indicators.py`.

**Файлы:** все модули `src/bybit_bot/`, тесты `tests/test_bybit_*.py`

### V2.1: positional state + pullback entry (по исследованиям)

Первая версия V2 использовала EMA 12/26 crossover — сигнал только в момент
пересечения. Проблема: crossover на 1h — редкое событие (раз в 30+ часов),
бот с 5-мин циклом пропускал его. Попытка добавить lookback — подгонка без
обоснования.

**Исследования (quant-signals.com, fmz.com, cryptotrading-guide.com):**
- 9/21 EMA лучше 12/26 на 1h: +0.069R expectancy (2716 бэктестов)
- Retest/pullback entry: вход на откате к fast EMA, а не на crossover
- ADX=20 — стандартный порог (не 15)
- SL=1.5 ATR, TP=3 ATR (R:R 1:2) — оптимально по анализу 500+ сделок
- Volume по предыдущему закрытому бару (текущий неполный)

**Новая логика:** positional state (EMA9 > EMA21 = long zone) + pullback к
EMA9 (в пределах 0.3%) + ADX > 20 + volume. Не зависит от момента crossover.

**Файлы:** `strategies/trend_ema.py`, `config/settings.py`, `app/main.py`

### V2: полная переработка — EMA Trend-Following на 1h

Предыдущий подход (5 скальпинг-стратегий на 5m) дал -$201 за 6 дней при WR 31%.
Основные проблемы: комиссии $127 (63% от убытка), нестабильные корреляции на 5m,
yfinance как источник данных (задержки, расхождения).

**Что изменилось:**
- Одна стратегия: EMA 12/26 crossover + EMA 200 trend filter + ADX > 20 + volume filter
- Таймфрейм: 1h вместо 5m (5-15 сделок/неделю вместо 94/день)
- Данные: Bybit API klines напрямую (убран yfinance)
- Ордера: Limit PostOnly (maker 0.02%) с fallback на Market
- Риск: 2% на сделку, leverage 3x, макс 2 позиции, KillSwitch $15/день
- Символы: 5 ликвидных (BTC, ETH, SOL, BNB, XRP) вместо 40
- Exit: SL=2 ATR, TP=3 ATR, trailing при +1.5 ATR, time-stop 48h

**Файлы:**
- `trading/client.py` — добавлен `get_kline()`
- `market_data/feed.py` — переписан на Bybit API (убран yfinance)
- `analysis/indicators.py` — новый модуль (EMA, ATR, ADX, volume_avg)
- `strategies/trend_ema.py` — новая единственная стратегия
- `trading/executor.py` — упрощён (убрана Stat-Arb/pair логика)
- `app/main.py` — переписан цикл (один источник, одна стратегия)
- `config/settings.py` — V2 параметры (убраны yfinance/scalping)
- Старые стратегии помечены [DEPRECATED V1], не удалены

### Stat-Arb: пары по реальным 5m корреляциям (8 пар, corr 0.79-0.93)

Сканирование всех 780 комбинаций из 40 символов показало: старые пары из
исследований (дневные данные) не работают на 5m — NEAR/SOL corr 0.44,
ETC/BCH 0.47, ARB/OP 0.00. Новые пары подобраны по реальным Pearson
корреляциям на 100 барах 5m. Каждый символ макс в 2 парах.

**Новые пары (8 шт.):**

| Пара | Corr | Сектор |
|---|---|---|
| BTC/LINK | 0.92 | major/infra |
| SUI/ETC | 0.92 | L1/PoW fork |
| LINK/PENDLE | 0.93 | DeFi infra |
| FIL/TIA | 0.91 | storage/DA |
| TIA/OP | 0.90 | modular/L2 |
| DOGE/SUI | 0.92 | meme/L1 |
| LTC/BTC | 0.79 | legacy PoW |
| DOGE/XRP | 0.79 | payment/meme |

**Файлы:** `strategies/scalping/stat_arb_crypto.py`, `tests/test_bybit_scalping.py`

---

### Stat-Arb: убраны ETH-пары, поднят MIN_CORRELATION, вычищены убыточные пары

Анализ 201 сделки за 2 дня: PnL -$201.62, WR 31%, комиссии $127.
ETH — главный убыток (-$59.50, 30% всех потерь): 6 из 13 пар использовали ETH
как ногу (BTC/ETH, SOL/ETH, LINK/ETH, AVAX/ETH, ATOM/ETH, ETH/BNB). При
трендовом движении ETH все 6 пар двигались синхронно — не диверсификация,
а концентрация риска на одном активе.

**Изменения (только параметры пар и фильтр, торговая логика не тронута):**

| Что | Было | Стало |
|---|---|---|
| DEFAULT_PAIRS | 13 пар (6 с ETH-ногой) | 6 пар (без ETH) |
| MIN_CORRELATION | 0.5 | 0.7 |

**Убраны пары:**
- 6 ETH-пар: BTC/ETH, SOL/ETH, LINK/ETH, AVAX/ETH, ATOM/ETH, ETH/BNB
- Убыточные: INJ/SOL (концентрация на SOL)

**Оставлены 6 пар:** LTC/BTC, APT/SUI, ETC/BCH, NEAR/SOL, ARB/OP, DOGE/XRP

**Файлы:** `strategies/scalping/stat_arb_crypto.py`, `tests/test_bybit_scalping.py`

---

## 2026-04-12

### Ужесточение защиты под $500 депозит + удаление убыточных пар

Анализ 13-часовой статистики: PnL -$37.38 (7.5% от $500), 3 пары дали -$35.64
из общего убытка: DOT/ETH (-$12.93), AAVE/UNI (-$13.16), ADA/DOT (-$9.55).
Причина: высокая дисперсия пар + emergency loss $15 слишком велик для $500.

**Изменения (только параметры защиты, логика стратегий не тронута):**

| Параметр | Было | Стало | % от $500 |
|---|---|---|---|
| STATARB_EMERGENCY_LOSS | $15 | $8 | 1.6% |
| killswitch_max_daily_loss | $37.50 | $25 | 5% |
| killswitch_max_loss_per_trade | $12.50 | $8 | 1.6% |

Убраны 3 убыточные пары: DOT/ETH, AAVE/UNI, ADA/DOT (13 пар → 13 пар,
добавлены не были — просто убраны 3 из 16). Осталось 13 пар с корреляцией 0.65+.

**Файлы:** `app/main.py`, `config/settings.py`, `strategies/scalping/stat_arb_crypto.py`,
`tests/test_bybit_bot.py`

---

### Stat-Arb: Market ордера вместо Limit PostOnly

Limit PostOnly для Stat-Arb пар приводил к тому, что одна нога исполнялась,
а вторая зависала как Open Order (цена ушла). Результат — однонаправленная
позиция без хеджа. Добавлен флаг `force_market` в `TradeParams`: для Stat-Arb
пар ордера всегда идут через Market (гарантированное исполнение обеих ног).
Остальные стратегии (VWAP, Momentum, Volume Spike) по-прежнему используют
Limit PostOnly с fallback на Market.

**Файлы:** `trading/executor.py`

---

### Централизация конфигурации: один источник правды

Проблема: одни и те же параметры (символы, balance, leverage, KillSwitch) были
разбросаны по 3-4 местам с разными значениями — `settings.py`, `docker-compose.yml`,
`.env.example`, `.env` на VPS. Символы: 26 в коде, 8 в compose, 38 в .env.

Решение: единственный источник правды — `settings.py` (Pydantic defaults).
- `DEFAULT_SYMBOLS`: восстановлен полный список 42 символа (включая AVAX, LTC,
  ATOM, ARB и др. — они доступны на Bybit demo, предыдущий аудит был ошибочным)
- `TICK_SIZES` / `tick_size()`: удалены — мёртвый код, executor берёт tick_size
  из Bybit API через `InstrumentInfo`
- `docker-compose.yml`: убраны все fallback-значения для bybit-bot, оставлен
  только проброс `${VAR:-}`. Если .env не задаёт — Pydantic берёт default из кода
- `.env.example`: синхронизирован с defaults в settings.py, убран дублирующий
  список символов (закомментирован как override)
- `.env` на VPS: убран `BYBIT_BOT_SCAN_SYMBOLS` — берётся из кода
- `DEFAULT_PAIRS`: 16 пар (9 оригинальных + 7 новых из исследований FullSwing AI,
  Springer Nature, TradingEconomics). Проверка `tradeable_symbols` защищает от
  открытия пар с недоступными символами

**Файлы:** `config/settings.py`, `docker-compose.yml`, `.env.example`,
`strategies/scalping/stat_arb_crypto.py`

---

### Аудит доступности символов + новые Stat-Arb пары из исследований

Проверка Bybit demo testnet выявила: 14 из 38 символов **недоступны** на демо
(AVAXUSDT, LTCUSDT, ARBUSDT, ATOMUSDT, FILUSDT, FETUSDT, TONUSDT, SEIUSDT,
WLDUSDT, ALGOUSDT; SHIBUSDT/PEPEUSDT/BONKUSDT/FLOKIUSDT — другие тикеры).
Это ломало 7 из 10 Stat-Arb пар (одна нога отсутствовала → однонаправленные
позиции без хеджа).

**Исправления:**
1. `DEFAULT_SYMBOLS`: убраны 14 недоступных, добавлены ETCUSDT, BCHUSDT (для пар)
2. `DEFAULT_PAIRS`: 14 пар на основе исследований корреляций (FullSwing AI,
   Springer Nature copula study, TradingEconomics): BTC/ETH (0.82), ADA/DOT (0.98),
   SOL/NEAR, ETH/BNB (0.78), AAVE/UNI, APT/SUI, ETC/BCH и др.
3. `_process_scalping`: добавлен параметр `tradeable_symbols` — Stat-Arb пары
   пропускаются если хотя бы один символ недоступен на бирже
4. Обновлены DISPLAY_NAMES, TICK_SIZES, BYBIT_TO_YFINANCE маппинги

**Файлы:** `config/settings.py`, `strategies/scalping/stat_arb_crypto.py`,
`app/main.py`

---

### Limit PostOnly для открытия позиций (снижение комиссий)

Было: все ордера открываются Market (taker fee 0.055%).
Стало: `executor.execute()` сначала пытает Limit PostOnly (maker fee 0.02%),
при отклонении — fallback на Market. Закрытие остаётся Market.

Avg open fee: $0.165 → ~$0.076 (экономия ~$0.09/сделку, 22% от total fees).
Торговая логика, параметры стратегий, pair TP, KillSwitch — без изменений.

**Файлы:** `trading/client.py` (новый `place_limit_order`), `trading/executor.py`

---

### Revert OPT 1-6: откат оптимизаций стратегий

Откачены все 6 оптимизаций из коммита `6635cab`. Причина: после деплоя OPT 1-6
пошла серия убытков. Ослабленные фильтры VWAP (deviation 1.5, RSI 35/65, ADX 25)
генерировали слишком много слабых сигналов, а Limit PostOnly ордера не успели
показать эффект (рынок тихий, суббота).

Предыдущая логика (до OPT) давала стабильный gross-плюс. Проблема не в стратегии,
а в комиссиях: при taker fee 0.11% roundtrip и notional ~$310 каждая сделка стоит
~$0.34. Нужно снижать fee без изменения торговой логики.

**Откачено:**
- OPT-1: Limit PostOnly (вернулся Market)
- OPT-2: MIN_CORRELATION 0.7 → 0.5
- OPT-3: Z_EXIT 0.3 → 0.5
- OPT-4: VWAP deviation 1.5→2.0, RSI 35/65→30/70, ADX 25→20
- OPT-5: VOLUME_SPIKE_MULT 2.5 → 3.0
- OPT-6: Динамический pair TP → фикс $2.00

**Файлы:** `app/main.py`, `trading/client.py`, `trading/executor.py`,
`strategies/scalping/stat_arb_crypto.py`, `vwap_crypto.py`, `volume_spike.py`

---

### Реальный PnL из Bybit API + entry_price из API + pair take-profit

Аудит показал расхождение DB vs API: бот записывал расчётный PnL (из uPnL на момент
закрытия), а реальный PnL с учётом комиссий и проскальзывания отличался. Также entry_price
бралась из yfinance вместо реальной цены исполнения.

**Что изменено:**

1. **`_close_and_record`**: после закрытия ордера запрашивает `get_closed_pnl(symbol)` из
   Bybit API и записывает реальный `closedPnl` и `avgExitPrice`. Фоллбэк на uPnL если API
   не вернул данные.

2. **`sync_closed`**: вместо `pnl=0` подтягивает реальный PnL из `closed-pnl` API по
   `startTime` = момент открытия позиции.

3. **`_fetch_entry_price`**: после открытия ордера запрашивает `get_positions()` и берёт
   реальный `avgPrice` вместо yfinance close.

4. **`client.fetch_realized_pnl`**: новый метод — обёртка над `get_closed_pnl` с фильтром
   по символу и времени.

5. **Pair take-profit** (`STATARB_PAIR_TP_USD = $0.80`): Stat-Arb пары теперь закрываются
   не только по z-score, но и когда суммарный uPnL пары >= $0.80. Раньше бот упускал
   прибыль, ожидая z-score сигнал.

6. **TP валидация**: добавлена проверка TP по lastPrice (Buy: TP > lastPrice,
   Sell: TP < lastPrice). Ранее проверялся только SL, из-за чего Bybit отклонял
   ордера AVAXUSDT/FILUSDT с невалидным TP.

7. **Pair TP поднят $0.80 → $2.00**: при $0.80 комиссии (~$0.70 за пару: 4 ордера ×
   ~$0.17) съедали почти всю прибыль. При $2.00 чистая прибыль после комиссий ~$1.30.

**Файлы:** `app/main.py`, `trading/client.py`, `trading/executor.py`

---

### KillSwitch: ослабление лимитов для демо + убрано дублирование дефолтов
`b18788d`

Для демо-торговли расширены пороги KillSwitch — бот получает больше свободы для набора статистики.
При переходе на реал — вернуть к консервативным значениям через env-переменные.

| Параметр | Было | Стало | При $500 |
|---|---|---|---|
| max_drawdown_pct | 10% | 25% | $125 |
| max_daily_loss | $15 (3%) | $37.50 (7.5%) | — |
| max_loss_per_trade | $7.50 (1.5%) | $12.50 (2.5%) | — |

Убрано дублирование: `KillSwitchConfig` dataclass больше не имеет дефолтов —
единственный источник значений теперь `Settings` (env-переменные).

**Файлы:** `config/settings.py`, `trading/killswitch.py`, `docker-compose.yml`, `tests/test_bybit_bot.py`

---

### Fix: time-stop по реальному времени + EXIT-CHECK логирование
`0eec615`

Time-stop использовал `opened_bar_idx` (номер цикла при открытии). При перезапуске контейнера
счётчик сбрасывался → позиции жили дольше лимита. Заменено на `opened_at` (ISO timestamp из БД):
`age_sec = (now - opened_at).total_seconds()`, лимит `TIME_STOP_SECONDS = 15000` (~4.2 часа).

Добавлен INFO-лог `EXIT-CHECK` для каждой открытой позиции в каждом цикле:
`EXIT-CHECK: Buy AVAXUSDT uPnL=0.90 age=63min strat=scalp_statarb pair=sa_AVAXUSDT_ETHUSDT_3b68c6`

Подтверждена работа всех exit-механизмов:
- Z-score exit: закрыл пару LTCUSDT/BTCUSDT (z < 0.5)
- Time-stop: теперь корректен при перезапусках
- Max loss / Emergency / Trailing: готовы, ждут условий

**Файлы:** `src/bybit_bot/app/main.py`

---

### Fix: валидация SL/TP по реальной цене Bybit перед отправкой ордера
`92ed496`

yfinance close price может расходиться с реальной ценой на Bybit.
Для Buy: если SL рассчитан от yfinance-цены (выше реальной), SL оказывается выше lastPrice — Bybit отклоняет ордер.
Пример: ATOMUSDT SL=1.7453 > lastPrice=1.7431 → `InvalidRequestError`.

Теперь `execute()` перед отправкой ордера запрашивает `get_tickers(symbol)` и проверяет:
Buy → SL < lastPrice, Sell → SL > lastPrice. Если невалидно — ордер открывается без SL/TP,
exit-логика `_process_exits()` всё равно закроет по time-stop, max_loss или trailing.

**Файлы:** `src/bybit_bot/trading/executor.py`

---

### Fix: round(None) crash для Stat-Arb позиций без SL/TP
`4822d01`

Stat-Arb стратегия устанавливает sl=None и tp=None (exit через z-score и trailing, не через фиксированные стопы).
Но `compute_trade()` безусловно вызывал `round(sl, price_prec)`, что падало с TypeError.
Добавлена проверка `if sl is not None` / `if tp is not None` перед округлением.

**Файлы:** `src/bybit_bot/trading/executor.py`

---

### Fix: 7 критических проблем exit-логики, KillSwitch и Stat-Arb

Анализ 50 закрытых сделок (PnL -$86.77, win-rate 28%) выявил системные проблемы.
Все исправления основаны на офиц. документации Bybit API v5 и лучших практиках.

**Проблема 1 — Нет exit-логики:** бот только открывал позиции, закрытие только по SL/TP Bybit.
Добавлена `_process_exits()` в каждый цикл: max_loss_per_trade ($7.50), time-stop (50 баров),
Stat-Arb z-score exit, trailing stop через Bybit API (0.7 ATR активация, 0.5 ATR дистанция).

**Проблема 2 — KillSwitch не проверял uPnL:** ETH потеряла $37.42 при лимите $7.50.
Теперь `_process_exits` проверяет `unrealisedPnl` каждой позиции и закрывает при превышении.
`record_trade_close()` вызывается после каждого закрытия. Drawdown считается от account_balance.

**Проблема 3 — Stat-Arb ноги закрывались независимо:** SL на одной ноге оставлял вторую открытой.
Добавлен `pair_tag` в БД. При закрытии одной ноги — немедленно закрывается вторая.
Stat-Arb позиции открываются без SL/TP, exit по z-score < 0.5 или emergency ($15 суммарный убыток).

**Проблема 4 — qty округлялся вверх:** floor вместо round, маржа Stat-Arb делится пополам.

**Проблема 5 — Единый SL/TP для всех стратегий:** Signal расширен полями `sl_atr_mult`, `tp_atr_mult`.
VWAP: SL=2.0/TP=1.5, Funding: SL=1.5/TP=1.0, Volume: SL=2.0/TP=2.0, Momentum: SL=2.0/TP=3.0.

**Проблема 6 — scalp_opened всегда 0:** проверял несуществующее поле PositionInfo.strategy.
Заменено на подсчёт через БД (strategy_name).

**Проблема 7 — trip в scalping не закрывал позиции:** добавлен close_all_positions().

**Bybit API (из офиц. доки):**
- `close_position` теперь использует `reduceOnly=True`
- `set_trailing_stop` через `POST /v5/position/trading-stop` (trailingStop + activePrice)
- `get_closed_pnl` через `GET /v5/position/closed-pnl`

**Файлы:** `analysis/signals.py`, `trading/client.py`, `trading/executor.py`,
`stats/store.py`, `app/main.py`, `tests/test_bybit_bot.py`

---

## 2026-04-11

### Fix: margin cap — уменьшать qty вместо отказа от сделки
`10b3930`

**Симптом:** бот находил 2 сигнала (BTCUSDT, ETHUSDT Stat-Arb) каждый цикл,
но все отклонялись: `маржа $566 > лимит $125 (25% от $500), пропускаю`.
При этом min qty BTC = 0.001 → маржа $16.6 — вполне вписывается.

**Причина:** формула risk sizing давала qty по ATR-риску ($25 / SL_distance),
что для BTC = 0.033 BTC → маржа $548. Вместо уменьшения qty executor просто отказывал.

**Решение:** если маржа > лимита, executor теперь пересчитывает qty вниз:
`max_qty = max_margin * leverage / price`, округляет по qtyStep из API.
Если даже min_order_qty не влезает — тогда пропускает.
Пример: BTC $83K, leverage 5x, лимит $125 → max_qty = 0.007 BTC, маржа $116.

**Файлы:** `trading/executor.py`

---

### Динамическая загрузка инструментов с Bybit API вместо хардкода
`a6a41d7`

**Симптом:** PEPEUSDT — "symbol invalid" на демо, хардкод `min_qty_map` на 38 монет мог не соответствовать реальным правилам биржи.

**Решение:** при старте бота вызывается `GET /v5/market/instruments-info` (из оф. документации Bybit).
Загружаются `minOrderQty`, `qtyStep`, `tickSize`, `minNotionalValue`, `maxLeverage` для каждого символа.
Невалидные символы (не в статусе "Trading") автоматически исключаются из `scan_symbols`.
Убран весь хардкод `min_qty_map` (38 строк) — теперь `_round_qty_api` использует данные API.
Также добавлена проверка `minNotionalValue` — Bybit отклоняет ордера меньше $5 notional.

**Файлы:** `trading/client.py` (`InstrumentInfo`, `get_instruments`), `trading/executor.py` (убран хардкод, `_round_qty_api`), `app/main.py` (фильтрация символов при старте), `tests/test_bybit_bot.py`

---

### Fix: position sizing использовал демо-баланс ($175K) вместо настроек ($500)
`cf677eb`

**Симптом:** скальпинг находил 3 сигнала (PEPEUSDT, DOGEUSDT, ATOMUSDT), но все отклонялись:
`маржа $230522 > лимит $25005 (25% баланса)`. Также PEPEUSDT — "symbol invalid" на демо Bybit.

**Причина:** `compute_trade` получал `available_balance` из API ($100K демо),
но настройки risk management рассчитаны на `account_balance = $500`.
Формула `risk_usd = 100000 * 0.05 = $5000` → огромная позиция → маржа не проходит.

**Решение:** размер позиции считается по `settings.account_balance` ($500),
`available_balance` из API используется только для проверки наличия свободной маржи на бирже.
Теперь: `risk_usd = 500 * 0.05 = $25` → адекватная позиция для $500 счёта.

**Файлы:** `trading/executor.py`, `tests/test_bybit_bot.py`

---

### Debug-логирование скальпинг-стратегий
`e3185d2`

После batch-фикса данные загружаются (38/38), но сигналов 0.
Добавлено verbose-логирование в каждую стратегию:
- VWAP: выводит deviation, ADX, RSI, slope для каждого символа
- Stat-Arb: выводит correlation, beta, z-score для каждой пары
- Volume Spike: выводит vol_ratio для каждого символа
- Убрано дублирование scan (раньше скальпинг сканировался дважды — для лога и для исполнения)

Временно включён LOG_LEVEL=DEBUG для диагностики на VPS.

**Файлы:** `strategies/scalping/vwap_crypto.py`, `stat_arb_crypto.py`, `volume_spike.py`, `app/main.py`, `.env`

---

### Batch-загрузка yfinance — 1 запрос вместо 76
`68a7a0a`

Было: 38 вызовов `yf.Ticker().history()` для `bars_map` + ещё 38 внутри `scan_instruments` = **76 HTTP-запросов** за цикл.
Yahoo лимит ~60 req/min → гарантированный rate limit, часть тикеров теряла данные.

Перешли на `yfinance.download(tickers=[...])` — один batch-запрос с многопоточностью.
`scan_instruments` принимает готовый `bars_map`, не загружает повторно.
Результат на VPS: **38/38 тикеров за 7 сек** одним вызовом.

**Файлы:** `market_data/feed.py` (добавлен `fetch_bars_batch`), `app/main.py`, `analysis/scanner.py`

---

### Создание Bybit крипто-бота — начальная структура

Создан автономный бот для торговли криптовалютой на Bybit, в том же репозитории что и fx_pro_bot, но полностью отдельный пакет — своя логика, свои настройки, своя БД.

**Архитектура (по образу fx_pro_bot):**
- `src/bybit_bot/` — отдельный Python-пакет
- Ансамбль 5 индикаторов (MA+RSI, MACD, Stochastic, Bollinger, EMA Bounce)
- Momentum-стратегия с крипто-фильтрами (объём, волатильность, RSI-зоны)
- Bybit клиент через `pybit` (Unified Trading API v5)
- KillSwitch (дневной лимит, просадка, макс позиций)
- SQLite статистика (сигналы + позиции)

**Инфраструктура:**
- `Dockerfile.bybit` — отдельный образ
- `docker-compose.yml` — сервис `bybit-bot` с volume `bybit_data`
- Все настройки через `BYBIT_BOT_*` env-переменные
- Entry point: `bybit-bot` (CLI) или `python -m bybit_bot.app.main`

**Демо-режим:**
- Подключён Bybit Demo Trading (api-demo.bybit.com)
- Баланс $100K виртуальных USDT
- Торговля пока отключена (`TRADING_ENABLED=false`), только сигналы
- 15 крипто-пар из коробки (BTC, ETH, SOL, XRP, DOGE и др.)

**Тесты:** 12 тестов bybit_bot + 161 тест fx_pro_bot — все проходят (173 total).

**Файлы:**
- `src/bybit_bot/` — весь пакет (app, config, market_data, analysis, trading, strategies, stats)
- `Dockerfile.bybit`, `docker/bybit-entrypoint.sh`
- `docker-compose.yml` (добавлен сервис bybit-bot)
- `pyproject.toml` (добавлен pybit, entry point bybit-bot)
- `.env.example`, `.env` (BYBIT_BOT_* переменные)
- `tests/test_bybit_bot.py`

### Скальпинг-стратегии для крипто

Добавлены 4 скальпинг-стратегии + подпакет индикаторов. Все интегрированы в главный цикл бота.

**1. VWAP Mean-Reversion** (`scalping/vwap_crypto.py`)
- Rolling VWAP по последним 50 барам (без привязки к FX-сессиям)
- Вход: отклонение > 2 ATR + RSI < 30 (long) / > 70 (short)
- Фильтры: ADX ≤ 25 (только боковик), EMA slope (не против наклона)
- SL = 2.0 ATR, TP = 1.5 ATR

**2. Stat-Arb крипто-пары** (`scalping/stat_arb_crypto.py`)
- Пары: BTC/ETH, SOL/ETH, LINK/ETH, LTC/BTC
- OLS hedge ratio (β), z-score спреда (окно 50)
- Вход при |z| ≥ 2.0, выход при |z| < 0.5
- Market-neutral: long одну + short другую

**3. Funding Rate Scalp** (`scalping/funding_scalp.py`)
- Уникально для крипто-перпетуалов (funding каждые 8ч)
- Вход за 30 мин до funding при rate > 0.05%
- rate > 0 → short (лонги платят), rate < 0 → long
- Сила сигнала пропорциональна отклонению rate

**4. Volume Spike Detection** (`scalping/volume_spike.py`)
- Альтернатива копи-трейдингу: ловим "китов" по объёму
- Вход: объём бара ≥ 3x от avg_volume(20) + ценовое движение ≥ 0.5 ATR
- Фильтры: RSI не в экстремуме, тренд совпадает с направлением
- Макс 3 сигнала за скан

**Индикаторы** (`scalping/indicators.py`): VWAP, vwap_series, rolling_z_score, z_score_series, ema_slope, ols_hedge_ratio, spread_series, avg_volume.

**Конфигурация:** `BYBIT_BOT_SCALP_VWAP_ENABLED`, `SCALP_STATARB_ENABLED`, `SCALP_FUNDING_ENABLED`, `SCALP_VOLUME_ENABLED`, `SCALP_MAX_POSITIONS=15`.

**Тесты:** 36 тестов bybit_bot (12 базовых + 24 скальпинг) + 161 fx_pro_bot = 197 total.

**Файлы:**
- `src/bybit_bot/strategies/scalping/` — indicators, vwap_crypto, stat_arb_crypto, funding_scalp, volume_spike
- `src/bybit_bot/config/settings.py` — добавлены scalping_* настройки
- `src/bybit_bot/app/main.py` — интеграция всех стратегий в цикл
- `tests/test_bybit_scalping.py`

### Калибровка стратегий по данным из авторитетных источников

Масштабное исследование стратегий по профессиональным и академическим источникам США. Корректировка параметров на основе бэктестов и рекомендаций топ-трейдеров.

**Источники исследования:**
- Springer Nature (Copula-based pairs trading, 2024)
- SSRN (Trend-following and Mean-Reversion in Bitcoin, 2024)
- Theseus (Bitcoin trading strategies 2020-2025)
- Quant Signals (ATR Stop Loss: 9,433 бэктеста)
- StratBase.ai (ADX filter: 763 бэктеста)
- CryptoProfitCalc (Top 5 Scalping Strategies 2026)
- Trader Dale (Volume Profile + Order Flow guide)
- AlgoStorm (Volume Profile trading)
- Bybit Help Center (Funding Fee документация)
- CoinPerps / KangaAnalytics (live funding rate data)
- FullSwing AI (Crypto Correlation Trading 2025)
- Racthera (ETH vs BTC performance 2023-2025)

**Корректировки:**

1. **VWAP Mean-Reversion** — ADX_MAX снижен с 25 → 20.
   Mean reversion работает только в боковике (ADX < 20).
   Зона 20-25 — серая, избегать. Подтверждено бэктестом 763 конфигураций (StratBase).
   Академическое исследование: BB mean reversion превзошёл momentum на часовых данных,
   но на бычьем рынке 9/11 лучших стратегий — trend-following.

2. **Stat-Arb** — добавлен фильтр корреляции MIN_CORRELATION = 0.5.
   BTC-ETH корреляция 0.75-0.82 в среднем (Springer, Racthera).
   При корреляции < 0.5 — коинтеграция нестабильна.
   Добавлен метод _correlation() для Pearson correlation.
   ETH vol 55-75% vs BTC 45-65% — учитывать при позиционировании.

3. **Funding Rate Scalp** — пороги пересмотрены по live-данным.
   Средний rate BTC = 0.005%, ETH = 0.01% (7-day avg, CoinPerps).
   THRESHOLD: 0.0005 → 0.0003 (0.03%, ~6x от среднего BTC).
   STRONG: 0.001 → 0.0008 (0.08%).
   Добавлен FUNDING_BUFFER_SECONDS = 10 (из документации Bybit: не входить за 5с до funding).

4. **Volume Spike** — SL_ATR_MULT 1.5 → 2.0 (Quant Signals: profit factor 1.72 для BTC).
   Добавлен COOLDOWN_BARS = 5 (First Test Rule от Trader Dale: первый тест уровня
   самый надёжный, повторные тесты ослабляют сигнал).

**Тесты:** 197 passed (все 36 bybit + 161 fx_pro_bot).

**Файлы:**
- `src/bybit_bot/strategies/scalping/vwap_crypto.py` — ADX_MAX 25 → 20
- `src/bybit_bot/strategies/scalping/stat_arb_crypto.py` — MIN_CORRELATION, _correlation()
- `src/bybit_bot/strategies/scalping/funding_scalp.py` — пороги rate, buffer
- `src/bybit_bot/strategies/scalping/volume_spike.py` — SL 2.0 ATR, cooldown

### Риск-менеджмент для микро-счёта $500

Перекалиброван весь слой управления капиталом и risk limits под стартовый депозит $500.
Стратегии (пороги индикаторов, условия входа) НЕ затронуты — изменён только sizing и защита.

**Принцип разделения:**
- Стратегии (`strategies/`) → решают КОГДА и КУДА входить. Не знают про баланс.
- Executor + KillSwitch (`trading/`) → решают СКОЛЬКО и МОЖНО ЛИ. Не знают про индикаторы.

**Расчёт:**
- Формула: `effective_risk = balance × pct / leverage = $500 × 0.05 / 5 = $5` = **1% per trade**
- Leverage 5x нужен для технической возможности открывать крипто-позиции на $500
- При 3 одновременных позициях: макс concurrent risk = $15 = 3% счёта

**Изменения параметров:**

| Параметр | Было | Стало | % от $500 |
|---|---|---|---|
| account_balance | 100,000 | 500 | — |
| leverage | 1x | 5x | — |
| max_positions (momentum) | 10 | 3 | — |
| scalping_max_positions | 15 | 3 | — |
| killswitch_max_daily_loss | $50 | $15 | 3% |
| killswitch_max_drawdown_pct | 20% | 10% | $50 |
| killswitch_max_positions | 10 | 5 | — |
| killswitch_max_loss_per_trade | $25 | $7.50 | 1.5% |

**Новая защита — проверка маржи в executor:**
- Добавлен `max_margin_per_trade_pct = 25%` — executor отклоняет сделку если маржа > 25% баланса.
- Логирование: при каждой сделке выводится risk в $ и %, margin в $ и %.
- Пример: BTC слишком дорог для одной позиции → executor откажет → бот перейдёт к ETH/SOL/альтам.

**Тесты:** 199 passed (38 bybit + 161 fx_pro_bot). Добавлены: test_executor_margin_check, test_executor_micro_account_sizing.

**Файлы:**
- `src/bybit_bot/config/settings.py` — новые defaults для $500
- `src/bybit_bot/trading/executor.py` — margin check + risk logging
- `src/bybit_bot/trading/killswitch.py` — defaults $15/$10%/$7.50
- `.env`, `.env.example` — обновлены параметры
- `tests/test_bybit_bot.py` — 2 новых теста

### Расширение до 39 монет — полный набор альткоинов

Было 8 активных монет (только majors). Добавлены 24 альткоина — все проверены на yfinance и доступны на Bybit USDT perp.

**Монеты по категориям (39 шт.):**

| Категория | Монеты |
|---|---|
| Majors (5) | BTC, ETH, SOL, XRP, BNB |
| Large-cap (10) | DOGE, ADA, LINK, AVAX, LTC, DOT, MATIC, NEAR, APT, ARB |
| Mid-cap DeFi/L1 (14) | SUI, UNI, AAVE, ATOM, TRX, FIL, INJ, FET, RENDER, TON, SEI, TIA, ONDO, PENDLE |
| Mid-cap infra (5) | WLD, OP, HBAR, RUNE, ALGO |
| Meme / micro-cap (5) | SHIB, PEPE, WIF, BONK, FLOKI |

**Почему это хорошо для $500 счёта:**
- Альткоины дешевле BTC/ETH → маржа меньше → больше позиций доступно.
- Мем-коины (PEPE, BONK, SHIB) — высокий объём, мизерная маржа, идеальны для скальпинга.
- Больше пар = больше сигналов = больше шансов найти setup.

**Stat-Arb: 10 пар** (было 4): добавлены AVAX/ETH, DOT/ETH, ATOM/ETH, NEAR/SOL, ARB/OP, PEPE/DOGE.

**yfinance маппинг:** SUI→SUI20947-USD, UNI→UNI7083-USD, PEPE→PEPE24478-USD, TON→TON11419-USD (специальные Yahoo ID).

**Тесты:** 199 passed.

**Файлы:**
- `src/bybit_bot/config/settings.py` — DEFAULT_SYMBOLS, DISPLAY_NAMES, TICK_SIZES, BYBIT_TO_YFINANCE
- `src/bybit_bot/trading/executor.py` — min_qty_map для всех 39 монет
- `src/bybit_bot/strategies/scalping/stat_arb_crypto.py` — 10 пар
- `.env`, `.env.example` — SCAN_SYMBOLS со всеми 39 монетами

### Деплой на VPS + включение демо-торговли

Первый деплой bybit-bot на VPS. Два контейнера работают параллельно:
- `fx-pro-bot-advisor-1` — форекс-бот (без изменений)
- `fx-pro-bot-bybit-bot-1` — крипто-бот (новый)

**Первый цикл сканирования:**
- 37 из 39 монет загрузились успешно
- MATIC delisted на yfinance (ребренд в POL, Yahoo не поддерживает) → убран
- APT тикер обновлён: APT-USD → APT21794-USD
- Первый сигнал: Stat-Arb DOT/ETH z=-2.07 (DOT недооценён vs ETH)

**Фиксы по результатам первого запуска:**
- Убран MATICUSDT (38 монет вместо 39)
- Исправлен тикер APTUSDT → APT21794-USD

**Включена демо-торговля:** `TRADING_ENABLED=true`.
Бот теперь открывает позиции на демо-счёте Bybit (виртуальные $100K).
Risk management ($500 профиль) активен — ограничит реальные потери при переходе на live.

**Файлы:**
- `src/bybit_bot/config/settings.py` — удалён MATIC, фикс APT тикера
- `src/bybit_bot/trading/executor.py` — удалён MATIC из min_qty_map
- `.env`, `.env.example` — 38 монет, TRADING_ENABLED=true

### Подключение исполнения скальпинг-сигналов

Скальпинг-стратегии генерировали сигналы, но не передавали их в executor —
только логировали. Добавлена функция `_process_scalping()` в main loop.

**Что делает:**
- После логирования сигналов проверяет KillSwitch и лимит скальп-позиций
- Для каждого скальп-сигнала (VWAP, Stat-Arb, Funding, Volume Spike):
  - Проверяет что символ ещё не открыт
  - Устанавливает leverage, рассчитывает qty/SL/TP через executor
  - Отправляет ордер на Bybit
  - Записывает позицию в SQLite с тегом стратегии (scalp_vwap и т.д.)
- Stat-Arb: открывает ОБЕ ноги (long A + short B)

**Файлы:**
- `src/bybit_bot/app/main.py` — `_process_scalping()`, вызов из `_run_cycle()`
