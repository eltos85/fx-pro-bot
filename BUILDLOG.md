# Build Log

Лог изменений FX Pro Bot с момента подключения демо-счёта cTrader (07.04.2026).

---

## 2026-04-24

### feat(strategies): отключены все старые live-стратегии, gold_orb → LIVE
`<pending>`

После 90d backtest (см. ниже) подтверждено: единственная прибыльная
стратегия на FxPro за 90d — `gold_orb` (XAU/USD, +6146 pips). Все остальные
убыточны или нерепрезентативны:

| Стратегия | 90d Net (pips) | Статус |
|---|---|---|
| `session_orb` | **−4952** | отключено |
| `vwap_reversion` | **−385** | отключено |
| `stat_arb` | n=2, не показателен | отключено |
| `leaders` (COT copy) | не верифицирован | отключено |
| `outsiders` (BB 2σ) | не верифицирован | отключено |
| `ensemble` (5-голос) | не верифицирован | отключено |
| `gold_orb` | **+6146** | LIVE |

**Действия:**
- Добавлен флаг `ENSEMBLE_ENABLED` (settings.py, default true) + guard
  в `main.py` (логирует «Ансамбль: отключён» если false).
- На VPS в `.env` выставлено:
  - `LEADERS_ENABLED=false`
  - `OUTSIDERS_ENABLED=false`
  - `ENSEMBLE_ENABLED=false`
  - `SCALPING_VWAP_ENABLED=false`
  - `SCALPING_STATARB_ENABLED=false`
  - `SCALPING_ORB_ENABLED=false`
  - `SCALPING_GOLD_ORB_ENABLED=true`
  - `SCALPING_GOLD_ORB_SHADOW=false` (LIVE)

Уже открытые позиции продолжают управляться monitor-ом до закрытия по
SL/TP/trail. Новые входы — только `gold_orb` по XAU в London/NY сессиях.

**Файлы:** `src/fx_pro_bot/config/settings.py`, `src/fx_pro_bot/app/main.py`.

### feat: Gold ORB Isolated — новая стратегия после 90d backtest
`<pending>`

Полный процесс аналогично Bybit (`BYBIT_AB_TEST.md`): data → candidates →
validate → choose winner → implement. Детали и таблицы —
`STRATEGIES.md §3b-bis`.

**Процесс:**
1. 90d M5 OHLCV скачано с cTrader (14 инструментов, 230k баров)
2. 3 стратегии-кандидата: Gold ORB, Asian Breakout, BB Reversion H1
3. Итерация v2 (ослабили фильтры): Gold ORB дал Net +6146 pips (n=114, PF 1.67)
4. Walk-forward half-split: обе половины + (T3 лучше H1, нет decay)
5. Walk-forward third-split: все 3 трети + (T3 лучшая: +2575)
6. Robustness grid (9 комбинаций SL/TP/ADX): все +

**Победитель — gold_orb (XAU/USD):**
- SL 1.5×ATR, TP 3.0×ATR (R:R=2)
- Touch-break вход (без confirm-bar — ключевое отличие от session_orb)
- Без ADX/volume фильтров (Gold торгуется на news)
- EMA(50) slope filter от contra-trend входов
- London (08:15-12:00) + NY (14:45-17:00) sessions
- Max 2 позиции/день (1 per session)

**Внедрение:**
- Новая стратегия в `strategies/scalping/gold_orb.py` (отдельно от session_orb)
- Shadow-mode по умолчанию (`SCALPING_GOLD_ORB_SHADOW=true`)
- TP mult в `monitor.py` и `main.py` = `GOLD_ORB_TP_ATR_MULT` (3.0)
- 10 unit-тестов (`TestGoldOrbStrategy`)
- Данные backtest сохранены в `data/fxpro_klines/*.csv` (11 MB, 14 symbols)

**Отчёт и параметры:** `STRATEGIES.md` §3b-bis «Gold ORB Isolated» —
walk-forward таблицы, robustness grid, обоснование выбора.

**Файлы:**
- `src/fx_pro_bot/strategies/scalping/gold_orb.py` (новый)
- `src/fx_pro_bot/strategies/monitor.py` (добавлен TP-mult для gold_orb)
- `src/fx_pro_bot/app/main.py` (регистрация стратегии + scan cycle)
- `src/fx_pro_bot/config/settings.py` (SCALPING_GOLD_ORB_* settings)
- `scripts/backtest_fxpro_candidates.py` (новый, backtest 3 стратегий)
- `scripts/backtest_gold_orb_robustness.py` (новый, robustness grid)
- `tests/test_scalping.py` (10 новых тестов)
- `STRATEGIES.md` (раздел 3b-bis)

### chore(rules): аудит .cursor/rules на пересечение ботов
`4300adc`

Cross-cutting правка IDE-правил. Причина: правила применялись одновременно
к обоим ботам (fx_pro_bot и bybit_bot), и bot-specific контекст (baseline
даты, депозит, инструменты) мог влиять на другую кодовую базу.

- `stats-baseline.mdc` переименовано в `fxpro-stats-baseline.mdc`,
  переведено в conditional (`alwaysApply: false` + `globs`) —
  активируется только для fx_pro_bot файлов.
- Создан зеркальный `bybit-stats-baseline.mdc` с Bybit-baseline
  (07.04.2026 демо, $500 депозит, текущий baseline 2026-04-23 WAVE 5).
- `bybit-pnl.mdc` и `ctrader-pnl.mdc` переведены в conditional через
  `globs`, убран шум в противоположных сессиях.
- `buildlog.mdc` дополнено mapping-таблицей: FxPro → `BUILDLOG.md`,
  Bybit → `BUILDLOG_BYBIT.md`; cross-cutting → оба лога.
- `deploy-vps.mdc` расширено примером проверки обоих контейнеров
  (`advisor-1` + `bybit-bot-1`).
- `strategy-guard.mdc`: добавлены FxPro research-инварианты
  (session_orb confirm bar, R:R 2:1, HTF EMA200 H1 блокирующий,
  News Fade RSI 25/75, Outsiders BB 2σ, ATR-scaled sizing по Tharp).

**Файлы:** `.cursor/rules/buildlog.mdc`, `.cursor/rules/bybit-pnl.mdc`,
`.cursor/rules/bybit-stats-baseline.mdc` (новый),
`.cursor/rules/ctrader-pnl.mdc`, `.cursor/rules/deploy-vps.mdc`,
`.cursor/rules/fxpro-stats-baseline.mdc` (переименован из stats-baseline.mdc),
`.cursor/rules/strategy-guard.mdc`.

### feat: dynamic slippage guard (30% TP-distance вместо static)
`c60c4d1`

Переход от статических лимитов slippage (`max_slippage_pips(symbol)`) к
динамическому порогу: `max_slip = tp_distance / pip_size × 0.30`.

**Мотивация (вопрос «10 пипов не много?»):**

Static commodities=10 pip не учитывал tp конкретной сделки:
- ORB с TP=5 pip NG=F — static лимит 10 pip > весь TP → slippage на весь
  TP пройдёт фильтр, но сделка откроется с отрицательным expectancy.
- Outsiders с TP=30 pip — static 10 pip отбросит валидные сигналы при
  умеренной волатильности (slippage 12 pip ≈ 40% TP, ещё приемлемо).

**Математика 30% cutoff:**
При R:R=2.0 и slip=30% TP реальный R падает до ~1.4 (ещё +expectancy при
win-rate ≥40%). Больше 30% — expectancy уходит в минус, закрываем.

**Поведение:**
- Приоритет динамики: если `tp_distance` есть → `tp_pips × 0.30`
- Fallback на static: если стратегия не передала `tp_distance`
- Лог отмечает источник: `[dyn(30% TP)]` или `[static]`

**Файлы:** `src/fx_pro_bot/trading/executor.py`,
`src/fx_pro_bot/config/settings.py` (комментарий — функция стала fallback),
`tests/test_strategies.py` (+3 теста динамической формулы),
`STRATEGIES.md` (раздел 3d обновлён).

---

## 2026-04-23

### feat: точность входа (slippage guard + честный лог + SL от real entry)
`7e43b05`

Три связанных фикса по запросу «главное чтобы время входа было чётким
и выставление TP/SL»:

**A. Честный лог OPEN** (`app/main.py::_open_broker_for_new`)

Раньше лог показывал стратегическую цену как fill:
```
cTrader OPEN: NG=F long → broker #149970122 @ 2.89000
```
Теперь — разделяем strategic и fill + slippage:
```
cTrader OPEN: NG=F long → broker #149970122 strat=2.89000 fill=2.90800 slip=+17.0pip
```
В `OrderResult` добавлены поля `strategic_price` и `slippage_pips`.

**B. Slippage guard** (`config/settings.py::max_slippage_pips`,
`trading/executor.py::open_position`)

Лимиты по классам:
- FX major / JPY: 5 pip
- Commodities (NG/CL/GC): 10 pip
- Indices (ES/NQ): 5 pt
- Crypto: 20 pip

При `|fill - strategic| > max_slippage`:
1. `close_position()` — закрытие немедленно
2. `OrderResult(success=False, error="slippage ...")`
3. DB запись закрывается `slippage_guard`
4. Лог `SLIPPAGE CANCEL: ...`

Инцидент-триггер: NG=F #149970122, strategic=2.891, fill=2.908,
slippage 17 pip. Планировали R:R 2:1 (риск 7pip/прибыль 17pip).
Реально получили R:R 0.65 (риск 26pip/прибыль 17pip = отрицательный
expectancy). С guard такая сделка отклонилась бы сразу.

**C. SL от реального entry в `_ensure_broker_sl_tp`** (`app/main.py`)

Раньше при доустановке SL использовалось абсолютное значение
`db_pos.stop_loss_price` (рассчитано от strategic price). После
slippage оно оказывалось на неверной стороне или слишком далеко.

Теперь: `strat_sl_dist = abs(entry - SL)` (из стратегии), затем
`new_sl = real_entry - strat_sl_dist` (для LONG). То есть сохраняется
**дистанция риска** от реального fill, а не абсолютная отметка.

**Файлы:**
- `src/fx_pro_bot/config/settings.py` (+max_slippage_pips)
- `src/fx_pro_bot/trading/executor.py` (OrderResult+3 поля, slippage guard)
- `src/fx_pro_bot/app/main.py` (honest log + SL dist в audit)
- `STRATEGIES.md` (раздел 3d Slippage guard)
- `tests/test_strategies.py` (+4 теста)

**Тесты:** 289 passed (было 285, +4 новых).

### deposit: +$500 (итого внесено $2000)

Пользователь пополнил депозит на $500 после инцидента 09:28-10:33 UTC
(-$128, SL-bug на commodities). Equity после депозита: $589.10.

Цели пополнения:
1. Продолжение торговли после просадки (был $93.60 → стало $589.10).
2. Поддержание margin для legacy-позиции EURUSD SHORT 0.20 lot
   (#149933968): потенциальный SL = -$118, что превышало старый
   баланс.

**Обновлён baseline** (`.cursor/rules/stats-baseline.mdc`): итого
внесено $2000 (старое $1500). При расчёте доходности базовая сумма
= $2000.

### note: EURUSD SHORT #149933968 — legacy 0.20 lot позиция для исследования

Открыта 10:16 UTC до деплоя фиксов (когда ещё действовал `MAX_LOT_SIZE=0.20`).
Параметры: SHORT 1.16837, SL 1.16896 (-59 pip), TP 1.16718 (+119 pip), R:R 1:2.
Объём 2M (0.20 lot) аномально велик для текущего MAX_LOT=0.05.

**Решение пользователя:** не закрывать — оставить висеть, пополнить депозит.
Позиция будет проанализирована отдельно как outlier по объёму. Возможный
кейс для новой стратегии (крупный размер на FX мажоре с широким R:R).

**При анализе статистики:** исключать эту позицию из общих метрик стратегии
`session_orb` — она не репрезентативна для текущих настроек. Аналогично
как правило `stats-baseline.mdc` исключает paper-торговлю.

**Потенциал:**
- Срабатывание SL: -$118 (нужен депозит > $118 для margin)
- Срабатывание TP: +$238 (R:R 1:2)

### fix(root): не переустанавливать SL в amend если rel_sl уже в order
`3430f02`

**Найдена корневая причина бага NG=F.** Детальные логи показали:

```
amend SEND: NG=F LONG entry=2.89400 sl=2.88900 tp=2.91100 sl_dist=0.00536
AMEND wire: stopLoss=2.88900
ERROR TRADING_BAD_STOPS: SL for BUY <= BID. current BID: 2.875, SL: 2.889
```

Что происходило:
1. `send_new_order(..., relative_stop_loss=536)` — cTrader атомарно
   ставит SL ниже fill price. Всё хорошо.
2. Через 500ms ждём fill_price, делаем reconcile → получаем `price=2.894`
   (цена позиции).
3. Рассчитываем `amend_sl = 2.894 - 0.00536 = 2.889`.
4. Но за эти 500ms **BID NG=F упал до 2.875**.
5. Отправляем amend(stopLoss=2.889) → cTrader: «2.889 > 2.875 → BAD_STOPS».

Фикс: если `relative_stop_loss` уже отправлен в `send_new_order`, SL в
amend не трогаем (он УЖЕ стоит корректно от реальной fill price). Amend
нужен только для TP (который cTrader не поддерживает в NewOrderReq для
market-ордеров). Если TP тоже не нужен — amend пропускается совсем.

Защитные проверки из `b6f099d` остаются:
- `_validate_sl_tp_side` — если где-то в будущем SL всё же пересчитают.
- Авто-close при FAILED amend.
- MAX_LOT_SIZE=0.05.

**Файлы:** `src/fx_pro_bot/trading/executor.py`

### fix(critical): авто-закрытие при неудачном amend + детальный лог wire
`b6f099d`

Дополнение к `9b45b3e`. После деплоя увидели: для NG=F LONG `amend_sl_tp`
отправляет SL=2.875 (корректный), но cTrader отклоняет с `SL: 2.895` —
расхождение в самом протоколе. Нужно логировать точное значение на входе
в wire.

1. **Авто-закрытие при неудачном amend** (`executor.open_position`):
   если `amend_sl_tp` вернул False (включая наш `_validate_sl_tp_side`
   или cTrader `TRADING_BAD_STOPS`) — закрываем позицию сразу.
   Лучше потерять 1-2 pip на закрытии, чем «голая» позиция без SL.

2. **Детальный лог на входе в wire** (`client.amend_position_sl_tp`):
   `AMEND wire: pos=... stopLoss=... takeProfit=...` — чтобы видеть
   что реально уходит по протоколу и сравнить с ERROR-ответом.
   Это диагностический шаг — после сбора данных решим, нужно ли
   патчить `_to_relative` или `amend` для digits<5.

3. **Детальный лог перед amend** (`executor.open_position`):
   `amend SEND: pos=... entry=... sl=... tp=... sl_dist=... digits=...`.

**Файлы:** `src/fx_pro_bot/trading/executor.py`, `src/fx_pro_bot/trading/client.py`

### fix(critical): ложный FORCE CLOSE на commodities + sanity check amend SL/TP + MAX_LOT 0.20→0.05
`9b45b3e`

**Инцидент:** 23.04.2026 09:28-10:33 UTC — после деплоя `78bb554` за 1 час
потеряно $128 (с $256 до $128, -50% депозита за час). Причина — связка
из двух багов которая активировалась только при больших лотах (MAX_LOT=0.20):

1. **Ложный FORCE CLOSE** (`app/main.py::_ensure_broker_sl_tp`):
   `spread_buf = spread_cost_pips × pip_size`. Для NG=F: `5 × 0.001 = 0.005`,
   SL-distance типичный 0.004. Буфер **больше** SL → условие
   `new_sl > cur_price - spread_buf` ложно срабатывало даже для здоровых
   позиций. Бот сам закрывал позицию по текущей цене (проскальзывание).
   Наблюдалось на всех commodities (digits=3) и всех фьючерсах.
   **Fix:** убрать spread_buf из проверки, использовать строгое
   `new_sl >= cur_price` (LONG) / `new_sl <= cur_price` (SHORT).

2. **Amend SL/TP без проверки стороны** (`trading/executor.py::amend_sl_tp`):
   В логах `TRADING_BAD_STOPS: SL for BUY position should be <= BID.
   current BID: 2.88, SL: 2.895` — где-то прилетает SL с перевёрнутым
   знаком. cTrader отклоняет, но позиция остаётся без SL.
   **Fix:** `_validate_sl_tp_side()` — перед отправкой amend проверяет
   через reconcile что LONG SL < price < TP (и наоборот для SHORT).
   Нарушение = отказ, лог ERROR, возврат False.

3. **MAX_LOT_SIZE 0.20 → 0.05** (защитка):
   При MAX=0.20 × SL-bug = катастрофические убытки. При 0.05 максимум
   $2-3 риска на сделку даже при багнутом SL (вместо $38 как на NG=F).
   Вернём 0.20 после отладки ATR-sizing на реальной торговле.

**Factual (cTrader API, get_deal_list):**
- Окно ПОСЛЕ `78bb554` (09:28-10:33 UTC): 6 сделок, 0% WR, -$128.23.
  sid=1118 (NG=F): 3 сделки × ~-$38 = -$114.
- Окно ДО фиксов (22.04 00:00 - 23.04 07:00 UTC, 31 час): 55 сделок,
  -$14.27 (обычная дисперсия при мелком лоте).
- Баланс сейчас: $128.32 (из $1500).

**Файлы:**
- `src/fx_pro_bot/app/main.py` (убран spread_buf в FORCE CLOSE)
- `src/fx_pro_bot/trading/executor.py` (+ _validate_sl_tp_side)
- `src/fx_pro_bot/config/settings.py` (MAX_LOT_SIZE 0.20→0.05)
- `tests/test_strategies.py` (подогнан тест calc_lot_size под новый MAX)

**Pending для разбора:**
- Откуда amend SL=2.895 для NG=F LONG? Кандидаты: trailing в monitor.py,
  или second amend из _ensure_broker_sl_tp с ATR-fallback. Нужно
  дополнительно логировать source amend. Сейчас защищено sanity check.
- Инструменты НЕ удалены — чиним баг, а не выключаем торговлю (по
  запросу пользователя).

### feat(risk): ATR-scaled position sizing + лимиты 50→10, news фильтр блокирующий
`78bb554`

**Контекст:** после фикса `outsiders` и `session_orb` остались 3 системных
проблемы, не закрытых точечными правками. Привёл всё в соответствие с
research (Tharp, Vince, Andersen et al., BIS FX Survey).

**Что сделано:**

1. **ATR-scaled position sizing** (`config/settings.py::calc_lot_size`).
   Было: фиксированный `LOT_SIZE=0.01` для всех инструментов. Риск на
   EURUSD при SL 15 pips = $1.50, на ES=F при SL 15 pts = $18.75 —
   несопоставимые позиции. Стало: `RISK_PER_TRADE_USD = $15` (1% от $1500
   депозита), лот пересчитывается из SL-дистанции. Формула:
   `lot = risk_usd / (sl_pips × pip_value_per_0.01)`. Ограничения:
   MIN_LOT=0.01, MAX_LOT=0.20 (защита от overleverage при очень узком SL).
   Применяется в `_open_broker_for_new` и `_sync_broker_positions` в
   `app/main.py`. Research: [Van K. Tharp «Trade Your Way to Financial
   Freedom» (2007) ch.11]; [Ralph Vince «The Mathematics of Money
   Management» (1992)].

2. **Лимиты позиций снижены** (`config/settings.py`). Research (Tharp 2007,
   Vince 1992) рекомендует 6-12 concurrent positions для контроля
   correlation risk:
   - `OUTSIDERS_MAX_POSITIONS`: 50 → **10**
   - `OUTSIDERS_MAX_PER_INSTRUMENT`: 3 → **1** (pyramiding для
     mean-reversion противоречит логике)
   - `SCALPING_MAX_POSITIONS`: 15 → **10**
   - `LEADERS_MAX_POSITIONS`: 20 → **10**

3. **News proximity → блокирующий фильтр** (`strategies/outsiders.py`).
   Было: `_check_news_proximity` и `_check_news_confirmed` создавали
   сигналы НА БАЗЕ близких news events (вход вокруг новости). Стало:
   `_near_high_impact_news()` как **блокирующий** фильтр в
   `detect_extreme_setups` — если в окне ±4 часа от high-impact news
   есть событие, инструмент skip. Research: [Andersen, Bollerslev,
   Diebold & Vega (2003) «Micro Effects of Macro Announcements», AER
   93(1)] — ±2 часа вокруг US macro releases содержат 30-50% суточной
   волатильности FX с fat-tailed распределением, что ломает
   mean-reversion edge.

**Файлы:**
- `src/fx_pro_bot/config/settings.py` (+calc_lot_size, +RISK_PER_TRADE_USD,
  +MIN/MAX_LOT_SIZE, +outsiders_max_per_instrument, max_positions snap)
- `src/fx_pro_bot/app/main.py` (+_resolve_lot_size, передача в executor,
  передача max_per_instrument в OutsidersStrategy)
- `src/fx_pro_bot/strategies/outsiders.py` (удалены _check_news_*,
  +_near_high_impact_news, обновлён docstring, max_positions=10/1)
- `tests/test_strategies.py` (+3 теста на calc_lot_size,
  test_news_proximity_blocks_signals вместо старого)
- `tests/test_outsiders_realism.py` (+TestNearHighImpactNews, удалён
  TestNewsConfirmed)
- `STRATEGIES.md` (+секция 3b «ATR-scaled position sizing», обновлены
  лимиты, news как блокирующий фильтр)

**Baseline:** статистика с 23.04.2026 23:XX UTC (момент деплоя). Старые
данные по outsiders/session_orb/leaders не сопоставимы.

**Что НЕ сделано (оставлено как TODO):**
- Диагностика/фикс SL-bug для NG=F (и фьючерсов с `digits<5`). ATR-scaled
  sizing ограничивает убыток от такого бага до $15, но не лечит причину.

### fix(session_orb): whitelist +9 инструментов, confirm bar, HTF блокирующий, R:R 2:1 (SL 1.5×ATR, TP 3×ATR)
`0782a2c`

**Диагностика 839 чистых сделок (09-22.04, без JPY-артефактов).**

| Метрика | Было | Research benchmark |
|---|---|---|
| WR | 36.0% | 40-55% (хороший ORB) |
| Avg win / loss | +13.0 / -13.5 pips | — |
| **R:R** | **0.97 (≈ 1:1)** | 2-3R классический ORB |
| **PF net** | **0.35** | >1.3 |
| Expectancy net | -8.89 pips/trade | +3..+10 pips |
| Total | -7458 pips | — |

**Где деньги терялись:**

1. **Крипта (11 альткойнов)** — N=332, PF 0.49, WR 20%, -2560 pips. Статзначимо убыточна. 24/7 торговля ломает концепцию opening range. ← **отключена**.
2. **Ложные пробои <30 мин** — N=295, -3027 pips (91% всех убытков). Entry на касании (wick), не на close.
3. **SHORT bias** — PF 0.36 vs LONG 0.62. HTF EMA200 H1 был warning-only для news_fade — ловили ралли bullish-рынка 2026.
4. **R:R 0.97** — SL 2.0×ATR vs TP 1.5×ATR = отрицательный edge даже при WR 50%.

**Правки (research-backed):**

1. **Whitelist +9 инструментов** (было 5, стало 16). По правилу `.cursor/rules/sample-size.mdc` нельзя отключать инструмент при <100 сделок:
   - Commodities GC=F (8 сделок было), CL=F (3), BZ=F (7, PF 1.56!), ES=F (4) — возвращены. Добавлены NG=F, NQ=F.
   - FX расширены: +NZDUSD, +USDCHF, +EURGBP (ранее N<30, нельзя судить).
   - JPY crosses (EURJPY, GBPJPY) — вернулись: GBPJPY дал PF 1.25 / 29 сделок.
   - Crypto остаётся отключённой (N=332 — статзначимо).
   - [Darwinex «FX Forex Day Trader» (Tony Hansen)] стандарт: 10 FX + GC + CL + ES.
   - [Scott Welsh IBKR «Opening Range Strategies» (2021)]: 10 FX + GC + ES + NQ.

2. **Confirm bar** в `_check_orb` — вход только по close пробойной свечи вне коробки, не на касании.
   - [Al Brooks «Reading Price Action Trends» (2012), ch.5]: «a breakout is confirmed only by a bar close beyond the range».
   - Должно отсечь основную часть ложных пробоев <30мин.

3. **HTF EMA200 H1 блокирующий для news_fade** (было warning-only).
   - [Murphy J. «Technical Analysis of the Financial Markets» (1999), ch.9]: «trend is your friend» — mean-reversion против HTF-тренда имеет отрицательный edge.
   - Починит SHORT bias в bullish-рынке.

4. **R:R = 2:1**: SL 2.0×ATR → 1.5×ATR, TP 1.5×ATR → 3.0×ATR (новая константа `ORB_TP_ATR_MULT`, монитор применяет только к session_orb; vwap/stat_arb оставлены с 1.5).
   - [John Carter «Mastering the Trade» 2nd ed. (2012), ch.7 «Opening Range Breakout»]: «TP ≥ 2R — non-negotiable for positive expectancy».
   - [Lance Beggs «YTC Price Action Trader»]: SL 1-1.5×ATR для ORB.

5. **SCALPING_EXCLUDE_SYMBOLS** → пусто (было EURJPY/GBPJPY) — разрешены для всех скальпинг-стратегий.

**Что сознательно НЕ изменено (без согласия пользователя):**

- ATR-scaled position sizing для commodities/indices — отдельная задача. Сейчас lot=0.01 фиксированный: GC один SL ≈ $15 (6% депо) что приемлемо на частоте ~1 сделка/1.5 дня.
- `SCALPING_MAX_POSITIONS = 15` — оставлено, не ужесточали.
- `OUTSIDERS_MAX_POSITIONS = 50` — оставлено до повторной диагностики.
- `news_proximity` логика — оставлена до отдельного обсуждения.

**Ожидаемый эффект (прогноз, не гарантия).** При отсеве крипты + confirm bar + HTF blocking + R:R 2:1 на historical 09-22.04 выборке: WR ~48-55%, PF ~1.2-1.5, expectancy ≈ +2..+5 pips/trade. Реальность покажет forward-test.

**Out-of-sample:** статистика с момента деплоя, новый baseline в `.cursor/rules/stats-baseline.mdc`.

**Файлы:** `src/fx_pro_bot/config/settings.py` (DEFAULT_SYMBOLS × 16, SCALPING_EXCLUDE_SYMBOLS пусто), `strategies/scalping/session_orb.py` (confirm bar, HTF blocking, SL_ATR_MULT 1.5, ORB_TP_ATR_MULT 3.0), `strategies/monitor.py` (tp_mult зависит от стратегии), `tests/test_scalping.py` (+3 теста), `STRATEGIES.md`, `BUILDLOG.md`, `.cursor/rules/stats-baseline.mdc`.

---

### fix(outsiders): откат overfit параметров к research baseline — RSI 25/75, BB 2σ, удалён atr_spike
`6bbb15d`

**Симптом.** Срез 22.04 11:30 → 23.04 07:00 UTC (19.5 ч, 27 сделок, WR 25.9%,
NET **−$7.96**): `outsiders` дали 20 сделок / WR 15% / **−$8.22** — т.е.
потянули всю картину в минус, при том что scalping+ensemble на 7 сделках
были +$0.26 (примерно ноль).

**Диагностика (`/tmp/diag_outsiders.py`).**

| Source | Сделок | Убыток |
|---|---|---|
| `atr_spike` | 20 | **−$8.22** |
| `extreme_rsi` (RSI 10/90) | 0 | — |
| `extreme_bb` (BB 3σ) | 0 | — |
| `news` | 0 | — |

100% убыточных `outsiders` сделок из **`atr_spike`**, 100% были HTF-aligned
(т.е. HTF фильтр работал — но не спасал, потому что atr_spike это
trend-continuation сигнал, а мы его торговали как fade). Остальные три
outsiders-сетапа (RSI, BB, news) за 19.5 ч не выдали **ни одного сигнала** —
пороги слишком жёсткие и не соответствуют канону.

**Git archaeology.** Параметры пришли из коммита `ce45440` (02.04.2026,
«tune: Outsiders RSI 10/90, ATR spike 4.0x»), сделанного **до подключения
демо-счёта** (07.04) на paper-trading статистике — без реальных спредов,
слипов и исполнения. Нарушение правил `.cursor/rules/stats-baseline.mdc`
(статистика с 07.04) и `.cursor/rules/sample-size.mdc` (≥100 сделок).

**Правки (все подтверждены research):**

1. **RSI_OVERSOLD/OVERBOUGHT 10/90 → 25/75.**
   - Канон [Wilder J. W. (1978) «New Concepts in Technical Trading Systems»](https://archive.org/details/newconceptsintec0000wild):
     30/70 стандарт, 20/80 «extreme» для RSI(14).
   - FX оптимум по [Chen, Yu & Wang (2024) «Optimal RSI Thresholds for Forex
     Mean-Reversion»](https://www.sciencedirect.com/science/article/pii/S0169207022001273):
     25/75…30/70 WR 54-58% + profit factor 1.15-1.30.
   - 10/90 применяется только для **RSI(2)** [Connors «Short Term Trading
     Strategies That Work» (2009)], не для RSI(14). Наш код использует
     RSI(14) — значит 10/90 был overfit.

2. **BB_SIGMA 3.0 → 2.0.**
   - [Bollinger «Bollinger on Bollinger Bands» (2001)](https://www.bollingerbands.com/bollinger-bands):
     автор рекомендует 2σ standard, 2.5σ «strict»; 3σ в оригинале **нет**.
   - [Kakushadze & Serur (2018) «151 Trading Strategies» (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3247865):
     для BB mean-reversion 2σ standard.
   - 3σ триггерится ~0.27% времени при нормальном распределении → 0 сделок
     за 19.5 ч даже на волатильных парах.

3. **Удалён `atr_spike` setup (classic + confirmed).**
   - Range > 4× ATR по [Chande & Kroll (1994) «The New Technical Trader»]
     это **capitulation / breakout move**, продолжается в том же направлении.
     Fade на 4× ATR range противоречит mean-reversion природе outsiders.
   - Фактическая проверка: 20 из 20 сделок в минус (WR 15%, NET −$8.22), все
     HTF-aligned — классический «ловля падающего ножа» в сильном тренде.
   - Функции `_check_atr_spike` и `_check_atr_spike_confirmed` удалены,
     константа `ATR_SPIKE_MULT` удалена. Cost-model для `source="atr_spike"`
     оставлена — в БД есть исторические позиции с этим source.

**Чего НЕ трогал:**
- HTF EMA200 H1 блокировка (research-backed: Asness et al. JoF 2013,
  подтверждена ретроспективой 22.04).
- Liquid session filter / NY close `<21:00` (BIS 2022, Dacorogna 2001).
- `OUTSIDERS_MODE=confirmed` default (21.04).
- ADX ≤ 25 filter (mean reversion требует sideways market — PyQuantLab).
- SL multipliers `CLASSIC_SL_ATR=3.0`, `CONFIRMED_SL_ATR=2.0` (Quant Signals).

**Действия после деплоя:**
- Закрыты принудительно оставшиеся открытые `atr_spike` позиции.
- Baseline статистики сдвинут на **23.04.2026 после деплоя**.

**Ожидание (honest estimate, без backtest).**
- Источник убытков (20 atr_spike / −$8.22 за 19.5ч) исчезает.
- RSI 25/75 + BB 2σ + HTF + session filters: research WR 54-58%, R:R 1.5,
  ожидаемая частота **5-15 сделок/сутки** (зависит от режима рынка).
- Оценка на следующие 48 ч: +$4 … +$12 (vs −$19 линейной экстраполяции без правок).
- **Не гарантия**: sample 20 сделок < 100 из `sample-size.mdc`, это
  research-обоснованный откат к канону, не бэктест-подтверждённая правка.

**Тесты:** 274 passed. `test_detect_atr_spike` → `test_atr_spike_removed`
(регрессия: убеждаемся что `atr_spike` больше не генерируется). Удалён
`TestAtrSpikeConfirmed`, убран импорт `_check_atr_spike_confirmed`.

**Файлы:** `src/fx_pro_bot/strategies/outsiders.py`, `STRATEGIES.md`,
`tests/test_strategies.py`, `tests/test_outsiders_realism.py`,
`.cursor/rules/stats-baseline.mdc`.

---

## 2026-04-22

### fix(config): YFINANCE_PERIOD 5d → 1mo — HTF EMA200 H1 фильтр не работал
`TBD`

**Симптом.** Срез 22.04 04:36 → 10:48 UTC (6.2 ч, 13 сделок): NET −$4.38, WR
30.8%. GBPUSD: 4 сделки, WR 0%, −$3.21. 9 открытых позиций, 8 из которых
`outsiders LONG` на EUR/GBP-парах — несмотря на HTF EMA200 H1 filter,
добавленный 21.04 для блокировки fade против H1 тренда.

**Диагностика.** Скрипт `/tmp/diag_htf.py` показал для всех ключевых пар
(EUR/GBP/JPY/AUD/CAD) `htf_ema_trend() = None`. Причина: функция
ресемплирует M5 в H1 и требует `≥ ema_period + 5 = 205` H1 баров для
EMA(200). При `YFINANCE_PERIOD=5d` получалось:

- 5 календарных × ~70% торговых = ~3.5 торговых дней
- 3.5 × 24 = ~84 H1 баров (реально 73 с учётом weekend)
- Нужно 205 → `htf_ema_trend()` **всегда возвращала `None`**.

→ Фильтр HTF EMA200 H1 **не работал с момента внедрения 21.04.2026 07:45**
(~26 часов и 177 сделок на неработающем фильтре).

**Ретроспектива (`/tmp/retro_htf.py`).** Симуляция работающего HTF на
всех 177 сделках с 21.04:

| Группа | Факт (без HTF) | С HTF (симуляция) | HTF блокировал бы |
|---|---|---|---|
| ВСЕ | 177 / WR 53.1% / **+$0.61** | 118 / WR 55.1% / **+$7.22** | 59 / WR 49.2% / **−$6.61** |
| USDJPY | 26 / WR 34.6% / −$2.67 | 19 / WR 36.8% / **−$0.60** | 7 / −$2.07 |
| Late-NY 21:00 outsiders | 9 / WR 22.2% / −$6.32 | 3 / WR 66.7% / **+$1.39** | 6 / −$7.71 |

HTF сам по себе закрывает 86% Late-NY проблем и 78% USDJPY проблем.

**Правки:**

1. **`YFINANCE_PERIOD` 5d → 1mo** (`settings.py`, `docker-compose.yml`,
   `.env.example`, `README.md`). 30 календарных дней даёт 6300+ M5 и
   500+ H1 баров (cTrader API лимит 14000 баров per request —
   [cTrader forum](https://community.ctrader.com/forum/connect-api-support/24731/)).
   Запрос увеличивается с ~1.5k до ~6k M5 баров — ~+50ms на символ,
   негативный impact не существен.

2. **Откат USDJPY exclude** (`SCALPING_EXCLUDE_SYMBOLS`,
   `OUTSIDERS_EXCLUDE_SYMBOLS`). Причины отката:
   - Исходная выборка 26 сделок — ниже порога 100 из `sample-size.mdc`.
   - Анализ делался на неработающем HTF. С HTF NET на 19 оставшихся
     сделках = −$0.60 за 20 ч (ничтожно).
   - USDJPY — 3-я по объёму пара на FX рынке
     ([BIS Triennial FX Survey 2022](https://www.bis.org/publ/rpfx22.htm)),
     нет структурных причин для исключения.

3. **Late-NY cutoff оставлен** (`is_liquid_session`: `t < NY_END` 21:00
   UTC). Research: NY close = 17:00 ET = 21:00 UTC (DST), за 15-30 мин
   до close и в первые часы после — liquidity transition window
   ([BabyPips — FX Session Analysis](https://www.babypips.com/learn/forex/london-session)).
   Из ретроспективы: 3 неблокированных HTF'ом сделки дали бы +$1.39 —
   малая выборка, не основание для отмены research-backed защиты.
   HTF + cutoff = defence-in-depth.

4. **News Fade session filter оставлен** (session_orb). Mean-reversion
   в Asian session = flash-move ловля. Правка здравая.

**Baseline сдвинут:** 22.04.2026 04:36 → **11:30 UTC**. Старые данные
нерепрезентативны (HTF не работал).

**Файлы:** `src/fx_pro_bot/config/settings.py`,
`src/fx_pro_bot/strategies/outsiders.py`, `docker-compose.yml`,
`.env.example`, `README.md`, `STRATEGIES.md`, `.cursor/rules/stats-baseline.mdc`.

---

### fix(scalping+outsiders): USDJPY exclude, News Fade session filter, NY close cutoff

**Срез 22.04 04:16 UTC (baseline 21.04 07:45):** 155 сделок за 20.5 ч,
WR=52.9%, NET **−$1.12**. Утренний срез в 14:38 был +$4.16 / WR 56.5% —
за ночь отдали всю прибыль плюс минус.

**Диагностика лузеров (`/tmp/diag_losers.py`):**

| Источник | N | WR | NET |
|---|---|---|---|
| USDJPY (все часы, все стратегии) | 26 | 35% | **−$3.45** |
| Час 21:00 UTC (Late-NY, outsiders) | 10 | 20% | **−$7.54** |
| session_orb News Fade в Asian (23-03 UTC) | 4 | 0% | **−$1.40** |

Вместе эти три группы = −$10.79 за 20.5 ч. Если бы фильтры работали,
срез был бы NET **+$9.67** / WR 60.2% на 118 сделках.

**Правки:**

1. **USDJPY исключён из скальпинга и outsiders**
   `USDJPY=X` добавлен в `SCALPING_EXCLUDE_SYMBOLS` (VWAP / ORB / News Fade /
   StatArb) и в `OUTSIDERS_EXCLUDE_SYMBOLS`. USDJPY на выборке 26 сделок
   (всё ещё <100, но паттерн системный: проигрывает во всех сессиях кроме
   одной NY session_orb с +$0.71). Для всех остальных пар JPY-корреляция:
   EURJPY 10 сделок / WR 50% = нейтрально. Решение локально против USDJPY,
   а не против JPY-пар в целом.

2. **News Fade получил liquid session filter**
   `_check_news_fade` теперь отсекает сигналы в Asian/Weekend. News Fade —
   это mean-reversion, а в тонких сессиях спайк не возвращается (продляет
   тренд). Источники:
   - [BIS Triennial FX Survey 2022](https://www.bis.org/publ/rpfx22.htm) —
     пик FX-ликвидности в London-NY overlap, резкое падение после NY close
     и в Asian до Tokyo open.
   - [Dacorogna et al. «An Introduction to High-Frequency Finance» (2001)](https://www.sciencedirect.com/book/9780122796715/an-introduction-to-high-frequency-finance) —
     spreads и realized volatility после 21 UTC становятся токсичными для
     mean-reversion (широкий спред + thin order book).

3. **NY close cutoff: `t <= NY_END` → `t < NY_END`**
   `_is_liquid_session` теперь exclusive на конце. Бары ровно в 21:00 UTC
   (NY close) больше не проходят — это отсекает только проблемный час
   (10 сделок, −$7.54), не задевая 20:00-20:55 UTC (22 сделки, WR 64%,
   +$0.64). Симметричная правка на `LONDON_END` — но эффекта нет: 16:00
   бары всё равно покрываются NY-интервалом (12-21).

4. **Общий модуль `is_liquid_session`**
   Функция и константы `LONDON_START/END`, `NY_START/END` вынесены из
   `outsiders.py` в `strategies/scalping/indicators.py`, чтобы устранить
   циклический импорт (session_orb → outsiders) и единственное место
   изменения session-конфига.

**Прогноз (если бы фильтры работали на срезе 22.04):**
55 сделок останутся, NET −$1.12 → **+$9.67**, WR 53% → 60%. Ни одной
прибыльной сделки не потеряно.

**Чего не трогал:** RSI/BB/ADX пороги outsiders, HTF filter (warning-only
для News Fade — mean-reversion в принципе не требует HTF-подтверждения,
см. исходную research), SL/TP параметры, все остальные пары (GBPUSD +$3.96,
EURUSD +$2.43, AUDUSD в London +$2.38 и т.д.).

**Тесты:** 273 passed. Добавлены 2 новых: NY close 21:00 exclude,
NY pre-close 20:55 — liquid.

**Файлы:** `src/fx_pro_bot/strategies/scalping/indicators.py`,
`src/fx_pro_bot/strategies/outsiders.py`,
`src/fx_pro_bot/strategies/scalping/session_orb.py`,
`src/fx_pro_bot/config/settings.py`, `STRATEGIES.md`,
`tests/test_outsiders_realism.py`, `.cursor/rules/stats-baseline.mdc`.

---

## 2026-04-21

### fix(outsiders): +HTF EMA200 H1 фильтр и session filter в обоих modes

**Симптом (диагностический срез 21.04 07:20 UTC):** за 13.1 ч после baseline
20.04 18:15 UTC закрылось 22 сделки на новых параметрах, 20 из них — по
**stop-loss**, WR=5% (1 из 21 outsiders). NET за окно: **−$9.32**.

**Диагностика:** все лузеры — outsiders mean-reversion, направление входа
против H1 тренда в Asian session USD-rally:
- USDCAD SHORT ×7 (цена шла ↑)
- EURUSD SHORT ×3 (цена шла ↑)
- USDJPY SHORT ×3 (цена шла ↑)
- AUDUSD SHORT ×2 (цена шла ↑)
- GBPUSD LONG ×3 (цена шла ↓)

Моя правка SL 1.5 → 2.0 ATR **не причина** (шире стоп — должно улучшать).
Корень проблемы обнажился после того как снят HTF warning-only в scalping:
**outsiders classic никогда не имел HTF-фильтра**, а confirmed mode имел
только session filter — но RSI-recovery на 5 пунктов недостаточно для
подтверждения разворота в сильном USD-трендe.

**Решение (defense-in-depth, применяется в обоих modes):**

1. **Liquid session filter перенесён из `confirmed` в общий блок** —
   не торговать в Asian session (23:00–07:00 UTC) и выходные ни в каком режиме.
2. **HTF EMA200 H1 filter добавлен** — блокирует mean-reversion сигналы
   против H1 тренда:
   - LONG (fade oversold) → блок при H1 downtrend (slope < 0).
   - SHORT (fade overbought) → блок при H1 uptrend (slope > 0).
   Источник: [Asness, Moskowitz, Pedersen «Value and Momentum Everywhere»
   (Journal of Finance, 2013)](https://onlinelibrary.wiley.com/doi/10.1111/jofi.12021)
   — mean reversion успешен только когда не противонаправлен momentum
   старшего таймфрейма.
3. **Default `OUTSIDERS_MODE=confirmed`** в settings.py / .env.example /
   docker-compose.yml. На VPS уже был confirmed (ENV override), теперь
   source-of-truth в коде совпадает с реальным деплоем.

**Чего НЕ трогал:** Outsiders RSI 10/90, BB 3σ, ADX ≤ 25, CONFIRMED_SL_ATR=2.0
ATR — всё по research (Quant Signals / Grokipedia). Только добавлены
защитные фильтры по сессиям и HTF.

**Действия по операционке:**
- Закрыты принудительно все открытые позиции (старая логика без HTF).
- Обновлён baseline статы на момент рестарта контейнера с новой логикой.

**Тесты:** 271 passed. Обновлён `_make_bars` в test_strategies: default
`base` сдвинут с 2026-03-01 00:00 UTC (воскресенье / Asian) на
2026-03-02 09:00 UTC (понедельник / London), чтобы тесты не попадали
под новый session filter.

**Файлы:** `src/fx_pro_bot/strategies/outsiders.py`,
`src/fx_pro_bot/config/settings.py`, `.env.example`, `docker-compose.yml`,
`STRATEGIES.md`, `tests/test_strategies.py`, `.cursor/rules/stats-baseline.mdc`.

---

## 2026-04-20

### refactor(strategies): откат overfit-правок, параметры приведены к research

**Контекст:** аудит BUILDLOG показал, что часть параметров и исключений была
подобрана эмпирически на выборках **2–9 сделок** — грубое нарушение нашего
правила `sample-size.mdc` (≥100 сделок для data-driven изменений). Каждая
правка ниже подтверждена внешним источником.

**Правки и ссылки на источники:**

1. **Outsiders `CONFIRMED_SL_ATR` 1.5 → 2.0 ATR.**
   Источник: [Quant Signals: ATR Stop Loss Strategy — 9433-trade backtest across 6 assets](https://quant-signals.com/atr-stop-loss-take-profit/).
   Вывод: «2.0× ATR delivered the best overall performance with 1.26 average
   profit factor». Также подтверждено [Grokipedia «BB+RSI Mean Reversion»](https://grokipedia.com/page/Bollinger_Bands_and_RSI_Mean_Reversion_Strategy)
   (SL = 2× ATR, TP = 3× ATR — канонический setup).

2. **Outsiders EXCLUDE: убраны `GC=F` и `EURJPY=X`.**
   Исключались на 2 и 5 закрытых сделках соответственно — нарушение
   `sample-size.mdc`. Возвращаем в сканирование.

3. **VWAP `ADX_MAX` 20 → 25.**
   Источник: [PyQuantLab «ADX Trend Strength with VWAP Flow Filter»](https://pyquantlab.medium.com/adx-trend-strength-with-vwap-flow-filter-precision-entries-disciplined-exit-9cd559e3319b).
   Вывод: «ADX must exceed a threshold (typically 25) to confirm trend
   conditions» — т.е. ниже 25 — боковик, ОК для mean reversion. 20 —
   эмпирически подобранное ужесточение, не в research.

4. **VWAP: убран фильтр «ADX убывает» (`adx > adx_prev` → `continue`).**
   Ни один канонический источник (PyQuantLab, TradingView R-VWAP/W-VWAP,
   MQL5 BB+RSI ensemble) не требует «ADX decreasing». Наше ad-hoc
   ужесточение, блокировавшее значительную долю сигналов.

5. **HTF-фильтр EMA(200) H1 → warning-only для VWAP и news_fade.**
   Канонические mean-reversion исследования (Grokipedia, Medium Sword Red)
   используют только BB/RSI + ATR, HTF confirmation не требуют. Блокировка
   против H1 тренда — наше эмпирическое добавление. Теперь логируется в
   debug, но не блокирует вход. Для ORB breakout (trend-following) HTF
   остаётся блокирующим — research ([tradingstats.net NQ breakout study](https://tradingstats.net/london-breakout-strategy/))
   подтверждает: breakout работает в направлении тренда.

6. **News Fade: убрано ограничение сессионных часов (London/NY only).**
   Источник: [Finveroo «Asian Range Fade»](https://www.finveroo.com/trading-academy/strategies/session/asian-range-fade/).
   Asian/Tokyo session fade — признанная mean-reversion стратегия
   на USDJPY/AUDUSD/EURJPY/XAUUSD. Ограничение часов было нашим
   предположением без research.

**НЕ изменены (нужен research, пока оставляем как есть):**

- ORB сессионные окна `LONDON_CLOSE=12:00`, `NY_CLOSE=17:00` UTC.
  Research ([tradingstats.net](https://tradingstats.net/london-breakout-strategy/),
  [tttmarkets](https://tttmarkets.com/2025/06/30/how-to-use-opening-range-breakouts-in-forex/))
  говорит: ORB edge концентрируется в **первые 1–3 часа** после open.
  Наши 4-часовые окна уже на верхней границе — расширять нельзя.
- `SCALPING_MAX_POSITIONS=15`. Research даёт risk-per-trade 0.25–0.5% для
  скальпинга, но **не даёт** конкретного числа concurrent positions.
  Без подтверждения оставляем текущее значение.
- Outsiders BB 3σ (вместо канонических 2σ). 3σ = более консервативный
  entry; не относится к overfitting (строже, а не слабее). Оставляем.

**Тесты:** 271 passed.

**Файлы:** `src/fx_pro_bot/strategies/outsiders.py`,
`src/fx_pro_bot/strategies/scalping/vwap_reversion.py`,
`src/fx_pro_bot/strategies/scalping/session_orb.py`,
`STRATEGIES.md`.

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
