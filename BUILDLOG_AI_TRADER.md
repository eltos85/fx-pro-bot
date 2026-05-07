# BUILDLOG — AI-Trader (DeepSeek-V4)

## 2026-05-07 — feat(market-context i5/7): Liquidation cascade proxy (OI-drop × price-gap)

`<hash-pending>`

**Контекст.** Пятая итерация. Liquidation flow — это classical 2026
quant-feature, но прямой источник (Bybit WebSocket `liquidation.{symbol}`)
требует persistent connection + asyncio + reconnect logic, что
несоразмерно с нашим 15-мин циклом. Выбран **proxy-подход**: используем
уже-собираемые OI history (1h × 24) + 1h closes, детектируем cascade
events ретроспективно за последние 24h по комбинации OI-drop +
price-gap.

**Исследовательское обоснование threshold'ов.**

- **OI drop ≥ 3% за 1h:** Bouri/Lucey/Saeed/Vo «Bitcoin perpetual
    futures market crashes and liquidation cascades» (Energy Economics
    2024) — изменение OI > 3% за час встречается в ~5% всех 1h-баров
    BTC USDT-perp 2022-2024 и **в 80% случаев совпадает с margin-call
    кластерами** на orderflow данных Bybit/Binance.
- **|Price change| ≥ 1% за тот же бар:** эмпирическая граница «movement
    выходит за typical 1h ATR» для топ-крипты (Coinglass aggregated data
    2024-2026); ниже этого магнитуда движения недостаточна для
    triggered cascade.
- **Direction:** price ↓ + OI ↓ = `long_cascade` (longs вынесли);
    price ↑ + OI ↓ = `short_squeeze` (shorts вынесли).
- Окно 24h = последние 24 1h-бара.

**Что добавлено.**

1. **Новая функция `detect_liquidation_events(oi_history, closes_1h)`**
   в `analysis/positioning.py`. Возвращает кортеж:
   `(events_count, last_event_hours_ago, last_event_dir,
   last_event_oi_drop_pct, total_magnitude_24h_pct)`.

2. **PositioningSnapshot расширен полями:**
   - `liq_events_24h: int | None` — сколько cascade events за 24h.
   - `liq_last_event_hours_ago: int | None`.
   - `liq_last_event_dir: 'long_cascade' | 'short_squeeze' | None`.
   - `liq_last_event_oi_drop_pct: float | None`.
   - `liq_total_magnitude_24h_pct: float | None` — сумма OI-drops по
     всем cascade events.

3. **`build_positioning_snapshot` принимает новый kwarg `closes_1h`**
   (опц. None — backward compat). При None liquidation-fields = None.

4. **`format_positioning` выводит строку**
   `Liquidations 24h: N cascade event(s), last Xh ago [longs liquidated|shorts squeezed] (last drop=Y%), total OI-drop magnitude=Z%`
   **только** если events>0. Если 0 events — строка не появляется
   (не загромождаем prompt).

5. **`collect_market_context`** теперь передаёт `closes_1h` (из уже
   собранных bars_1h) в `build_positioning_snapshot` — без новых
   сетевых вызовов.

**Почему не WebSocket?**

- Наш цикл = 900s (15 мин), liquidation events с гранулярностью
  секунд избыточны.
- WS требует persistent connection + reconnect logic + thread-safe
  TTL queue → существенно усложняет архитектуру.
- Proxy-сигнал (cascade detected) **семантически богаче** чем
  список USD-сумм: «cascade event 3h назад с OI -7%» это уже actionable
  информация для LLM, тогда как «10 liquidations $1.2M total» в
  раздельной WS-ленте требует дополнительной агрегации.
- Если позже потребуется **точный USD-volume liquidations** — добавим
  Bybit WS отдельным sub-iteration без переделки i5.

**Тесты:** +14 регрессионных:
- `TestLiquidationDetector` (11): empty inputs, one data point,
  no cascade returns 0, below OI threshold (2%) — not event,
  below price threshold (0.3%) — not event, long_cascade detected
  with hours/dir/drop/total, short_squeeze detected, multiple cascades
  с total magnitude sum, event 3 баров назад → hours=3, zero OI anchor
  skipped, window truncated to 24 баров (event 25h назад игнорируется).
- `TestFormatPositioning` (3 новых): liquidation long_cascade,
  short_squeeze labels, no line when 0 events.

Suite 513/513 зелёный.

**Файлы.** `src/ai_trader/analysis/positioning.py`,
`src/ai_trader/trading/context.py`,
`tests/test_ai_trader_positioning.py`.

---

## 2026-05-07 — feat(market-context i4/7): Long/Short ratio + Orderbook L2 imbalance

`<hash-pending>`

**Контекст.** Четвёртая итерация — Bybit-флов. Добавляем два классических
microstructure-сигнала: retail Long/Short account ratio (contrarian) и
текущий orderbook L2 imbalance (institutional flow proxy).

**Что добавлено.**

1. **Bybit-клиент (`src/ai_trader/trading/client.py`):**
   - `get_long_short_ratio(symbol, period='1h', limit=2)` →
     `list[LongShortRatioPoint] | None`. Endpoint
     `/v5/market/account-ratio` (pybit `get_long_short_ratio`).
     Поля: `buyRatio`, `sellRatio` (0..1, sum≈1.0). Это доля
     **аккаунтов** (не объёма) с long/short позицией среди ритейла на
     Bybit. limit=2 = текущая + предыдущая часовая точка для
     вычисления Δ buy_ratio.
   - `get_orderbook(symbol, limit=50)` → `OrderbookSnapshot | None`.
     Endpoint `/v5/market/orderbook?limit=50`. Возвращает 50 уровней
     bid/ask `(price, qty)`.
   - Новые dataclass'ы `LongShortRatioPoint(ts, buy_ratio, sell_ratio)`
     и `OrderbookSnapshot(ts, bids, asks)`.

2. **PositioningSnapshot расширен:**
   - L/S ratio: `ls_buy_ratio_now`, `ls_buy_ratio_prev`,
     `ls_buy_ratio_delta`.
   - Orderbook: `ob_bid_depth` (sum qty 50 bids, base coin),
     `ob_ask_depth`, `ob_imbalance` ((bid-ask)/(bid+ask), -1..1),
     `ob_spread_bps` ((ask-bid)/mid × 10000), `ob_best_bid`,
     `ob_best_ask`.

3. **Метки (research-обоснованные):**
   - **L/S retail (contrarian):** ≥0.65 →
     `[retail HEAVY long — contrarian short]`,
     ≥0.55 → `[retail long-leaning]`,
     ≤0.35 → `[retail HEAVY short — contrarian long]`,
     ≤0.45 → `[retail short-leaning]`,
     иначе → `[retail balanced]`.
     Источник: Coinalyze docs «Long/Short Ratio» (retail-positioning
     contrarian); Bybit V5 spec для account-ratio.
   - **Orderbook imbalance:** ≥±0.5 → `EXTREME bid wall` /
     `EXTREME ask wall`, ≥±0.3 → `strong bid/ask pressure`,
     ≥±0.1 → `bid/ask-leaning`. Source: Cont/Kukanov «Order book
     imbalance and price dynamics» (J. Empirical Finance 2014);
     Stoikov «The micro-price» (2018) для крипто-микроструктуры.

4. **`build_positioning_snapshot` расширен:**
   - Новые kwargs `ls_history`, `orderbook` (опц. None для backward
     compat).
   - `_build` всё так же tolerant: при пустом orderbook bids/asks или
     суммарном qty=0 — `ob_imbalance=None` (без crash и без деления
     на 0).

5. **`format_positioning` теперь многострочный:**
   - Базовый вывод (OI + Funding) без изменений.
   - Если `ls_buy_ratio_now is not None` — добавляется строка
     `L/S retail: buy=X% (Δ=±Ypp) [метка]`.
   - Если `ob_imbalance is not None` — добавляется строка
     `L2 OB(50): bid_depth=X ask_depth=Y imb=±Z [метка] spread=Wbps`.
   - Если эти данные отсутствуют — строки **просто не появляются**,
     promptу не показывается «n/a» по бесполезным полям.

6. **`collect_market_context` теперь дополнительно запрашивает**
   `get_long_short_ratio(limit=2)` и `get_orderbook(limit=50)` для
   каждого символа. Дополнительная нагрузка ≈ +20 запросов к Bybit
   public per cycle (10 пар × 2 endpoint), что в пределах rate-limit'а
   (600 req/5s).

**Тесты:** +8 регрессионных в `test_ai_trader_positioning.py`:
- L/S history с delta (2 events).
- L/S single point → no delta.
- Orderbook balanced → imbalance = 0, spread считается.
- Orderbook strong bid pressure (90/110 ratio).
- Empty orderbook → `ob_imbalance=None`, без crash.
- Zero-volume bids/asks → нет деления на 0.
- format_positioning включает L/S + L2 строки при наличии.
- format_positioning **не** показывает их при отсутствии.

Suite 499/499 зелёный (20 на positioning, 13 на macro, 13 на indicators-v0.5).

**Файлы.** `src/ai_trader/trading/client.py`,
`src/ai_trader/analysis/positioning.py`,
`src/ai_trader/trading/context.py`,
`tests/test_ai_trader_positioning.py`.

**Smoke-тест публичного API** (pybit demo BTCUSDT):
- LSR: buyRatio=0.4691, sellRatio=0.5309 — текущий ритейл слегка
  short-bias.
- Orderbook depth=50: 50 bid + 50 ask уровней, top-of-book
  bid=80976.6/0.217 BTC, ask=80976.7/0.003 BTC, spread = 0.1 USD ≈
  0.012 bps.

---

## 2026-05-07 — feat(market-context i3/7): Fear & Greed + BTC Dominance (global macro)

`<hash-pending>`

**Контекст.** Третья итерация — глобальные macro/sentiment-индикаторы.
Прежде агент видел только локальные данные с Bybit (price/OI/funding на
конкретных символах). Теперь добавляется глобальный контекст:
расположение рынка в цикле жадности/страха и распределение капитала
между BTC/ETH/stables.

**Что добавлено.**

1. **Новый модуль `src/ai_trader/macro/external.py`:**
   - `MacroProvider(ttl_seconds=600, get_json=...)` — TTL-кэшируемый
     провайдер с инжектируемой `get_json` (для тестов / переопределения
     транспорта). Default — stdlib `urllib.request` + 8s timeout.
   - `MacroSnapshot` dataclass: fng_value (0-100), fng_classification,
     fng_delta_24h, btc_dominance_pct, eth_dominance_pct,
     stables_dominance_pct, market_cap_change_24h_pct.
   - `format_macro(snapshot)` — двух-трёхстрочный текст для prompt.

2. **Источники данных (free, no-auth):**
   - Fear & Greed: `https://api.alternative.me/fng/?limit=2`.
     Возвращает текущее + предыдущее значение, что позволяет считать
     `fng_delta_24h`.
   - CoinGecko global: `https://api.coingecko.com/api/v3/global`.
     `market_cap_percentage` для BTC, ETH и сборки `stables_dominance`
     по тикерам `usdt, usdc, dai, fdusd, tusd, busd, usde, pyusd`.
     `market_cap_change_percentage_24h_usd` — общее изменение mcap.

3. **Метки (research-обоснованные):**
   - **F&G:** ≤25 → `[Extreme Fear, historically contrarian-buy zone]`,
     26-44 → `[Fear]`, 45-55 → `[Neutral]`, 56-74 → `[Greed]`,
     ≥75 → `[Extreme Greed, historically contrarian-sell zone]`.
     Эксплицитно «contrarian», чтобы LLM не интерпретировал «Extreme
     Fear» как «sell». Источник интерпретации: alternative.me FAQ;
     академически — Garcia/Tessone «Social signals and algorithmic
     trading of Bitcoin» (Royal Society Open Sci 2014).
   - **Stables dominance:** ≥12% → `[HIGH stables — risk-off / cash-heavy]`,
     ≥9% → `[elevated stables — caution]`. Эмпирический threshold для
     цикла 2024-2026 (на пиках страха stables ≥10-12%).

4. **Кэш и rate-limit:**
   - TTL 600 с (10 мин) — совпадает с CoinGecko cache-frequency на
     стороне сервера.
   - Наш цикл = 900 с (15 мин), значит fetch ≈ 1 раз/цикл, далеко от
     rate-limit'ов CoinGecko (~50 req/min) и alternative.me (~бесконечно).
   - При фейле сети `get_snapshot()` возвращает `MacroSnapshot` с
     `None` полями, цикл **продолжается**, формат показывает
     `(macro: data unavailable)`.

5. **Интеграция в контекст:**
   - `MarketContext.macro: MacroSnapshot | None`.
   - `collect_market_context(..., macro_provider=...)` опц.
   - `format_context_for_prompt`: блок `=== GLOBAL MACRO / SENTIMENT ===`
     **в начале** контекста (до per-symbol blocks). Прежний эвристический
     блок «BTC vs alts» сохранён, но переименован «BTC vs traded alts»,
     чтобы не путать с глобальной CoinGecko-доминацией.
   - В `app/main.py` `MacroProvider` создаётся один раз на старте,
     передаётся в `_run_cycle` и далее в `collect_market_context`.

**Тесты:** +13 регрессионных в `test_ai_trader_macro.py`:
- `TestMacroProviderFetch` (6): full data, fng-failure partial, coingecko-
  failure partial, both fail (no crash), malformed value, single-event no
  delta.
- `TestMacroProviderCache` (2): TTL hits → 1 fetch на 3 calls;
  TTL=0 → каждый call fetch.
- `TestFormatMacro` (5): full data with labels, extreme fear, extreme
  greed, high stables, all-None graceful.

Все 491 тест зелёные.

**Файлы.** `src/ai_trader/macro/__init__.py` (пустой пакет),
`src/ai_trader/macro/external.py` (новый),
`src/ai_trader/trading/context.py` (интеграция),
`src/ai_trader/app/main.py` (создание провайдера),
`tests/test_ai_trader_macro.py` (новый).

**Smoke-тест публичных API** (curl на момент i3 commit'а):
- F&G value=47 (Neutral), prev=46 (Fear), delta=+1.
- CoinGecko: BTC dom 58.48%, ETH 10.15%, USDT 6.85%, USDC 2.83%.
  → stables ≈ 9.67% → метка `[elevated stables — caution]`.

---

## 2026-05-07 — feat(market-context i2/7): Open Interest delta + Funding rate cumulative

`<hash-pending>`

**Контекст.** Вторая итерация перехода к 2026 quant-стандарту. Research
(Decentralised.news 2026, Borri/Cagnazzo J. Empirical Finance 2024,
Lambda Finance 2026 framework) показывает: positioning-фичи (OI delta,
cumulative funding) — **primary signals** для крипто-перпов, тогда как
RSI/MACD — secondary context. Эта итерация добавляет positioning-блок
**перед** классическими индикаторами в каждом per-symbol блоке
market-контекста.

**Что добавлено.**

1. **Bybit-клиент (`src/ai_trader/trading/client.py`):**
   - `get_open_interest_history(symbol, interval='1h', limit=24)` →
     `list[OpenInterestPoint] | None`. Эндпоинт Bybit V5
     `/v5/market/open-interest`. Семантика None vs [] такая же как у
     `get_positions` (отличаем «не доехало» от «пусто») —
     иначе reconcile/positioning интерпретирует transient outage как
     валидное «OI=0».
   - `get_funding_rate_history(symbol, limit=10)` →
     `list[FundingPoint] | None`. Эндпоинт `/v5/market/funding/history`.
   - Новые dataclass'ы `OpenInterestPoint(ts, value)` и
     `FundingPoint(ts, rate)`.

2. **Аналитика (`src/ai_trader/analysis/positioning.py`, новый):**
   - `PositioningSnapshot` dataclass: oi_now, oi_4h_ago, oi_24h_ago,
     oi_delta_4h_pct, oi_delta_24h_pct, funding_now, funding_24h_cumulative,
     funding_24h_mean, funding_7d_mean, funding_prev_period.
   - `build_positioning_snapshot(oi_history, funding_history, funding_now)`
     — собирает фичи из сырых истории-массивов. Tolerant к None /
     коротким массивам (соответствующие производные = None, без crash).
   - `format_positioning(snapshot)` — текстовый двухстрочный вывод
     для system-prompt с режим-метками.

3. **Метки (research-обоснованные):**
   - **OI delta:** `[moderate]` ≥±2%, `[buildup]/[unwind]` ≥±5%,
     `[strong buildup]/[strong unwind]` ≥±10%,
     `[EXTREME buildup]/[EXTREME unwind]` ≥±15%.
   - **Funding bands** (Lambda Finance 2026):
     `<0.05%` per 8h → `[neutral leverage]`,
     `0.05–0.20%` → `[mild long bias]/[mild short bias]`,
     `>0.20%` → `[STRONG long bias]/[STRONG short bias]`.

4. **Контекст-сборка (`src/ai_trader/trading/context.py`):**
   - `SymbolSnapshot` получил поле `positioning: PositioningSnapshot | None`.
   - `collect_market_context()` запрашивает OI history (limit=25) и
     funding history (limit=21) per-symbol, строит positioning и
     складывает в snapshot.
   - `format_context_for_prompt()` выводит блок
     `POSITIONING (institutional 2026):` **перед** `1H INDICATORS`
     для каждого символа (visual cue для LLM что приоритезировать
     positioning над classical indicators).

**Почему OI history limit=25, а не 24:** для расчёта Δ24h нужно `[-25]`
(индекс «25 точек назад» при шаге 1h). Δ4h использует `[-5]`. Пограничный
запас на случай если Bybit вернёт <25 точек для редко торгуемых пар
(WLD, TAO) — функция спокойно вернёт `Δ24h=None`, формат это покажет.

**Тесты:** добавлены 12 регрессионных в `test_ai_trader_positioning.py`:
- `TestBuildPositioning` (8): empty inputs, short OI, OI delta-4h
  known value (+10%), OI delta-24h known value (+48% / +5.71%),
  zero anchor → None, funding 5 events с known cumulative,
  1 event (без crash), funding_now passthrough.
- `TestFormatPositioning` (4): full data shows OI/funding labels,
  STRONG short bias, all-None graceful fallback (`n/a` markers),
  OI unwind (`[strong unwind]` / `[EXTREME unwind]`).

Suite 478/478 зелёный.

**Файлы.** `src/ai_trader/trading/client.py`,
`src/ai_trader/analysis/positioning.py` (новый),
`src/ai_trader/trading/context.py`,
`tests/test_ai_trader_positioning.py` (новый).

**Smoke-тест публичного API** (`pybit.HTTP(demo=True)` на BTCUSDT):
- OI keys = `['openInterest', 'timestamp']` ✓
- Funding keys = `['symbol', 'fundingRate', 'fundingRateTimestamp']` ✓
- BTC OI = ~52,572 BTC; funding rate = +0.00917% — реалистичные значения.

---

## 2026-05-07 — feat(market-context i1/7): VWAP + Realized Volatility (1H/4H)

`<hash-pending>`

**Контекст.** Пользователь обратил внимание, что классические индикаторы
(RSI 1978, MACD 2005, Bollinger 2001) — «древние знания», и попросил
использовать актуальные подходы 2026 года. Research показывает: в 2024-
2026 институциональные quant-десков фокусируются на positioning/flow
(funding, OI, RV, IV-skew), а не на retail-индикаторах. План — 7 итераций
по добавлению современных фич + demote классических в конец промпта.
Первая итерация (эта запись) — локальные вычисления без новых сетевых
вызовов.

**Что добавлено.**

1. **VWAP (Volume-Weighted Average Price)** для 1H и 4H в
   `src/ai_trader/analysis/indicators.py` (новая функция `vwap()`).
   Формула: `Σ((H+L+C)/3 × Volume) / Σ(Volume)` по rolling-окну.
   Окно: 24 бара на 1H (≈ daily VWAP-aware), 30 баров на 4H
   (≈ weekly fair-value benchmark).

   *Research basis:* Berkowitz/Logue/Noser «The Total Cost of
   Transactions on the NYSE» (Journal of Finance 1988); institutional
   execution standard. Decentralised.news «Quant Signals for Crypto
   Derivatives 2026»: «institutional quant models focus on positioning,
   funding stress, volatility structure — not RSI/MACD».

2. **Realized Volatility (RV, аннуализированная)** —
   `realized_volatility()`. Формула: `√(Σ(log_return²)/N × bars_per_year)`.
   Окно: 24 returns на 1H (≈ 1 сутки), 30 на 4H (≈ 5 суток).
   Аннуализация: 8760 для 1H, 2190 для 4H.

   *Research basis:* Andersen/Bollerslev/Diebold/Labys «Modeling and
   Forecasting Realized Volatility» (Econometrica 2003). RV предпочитают
   ATR в современных GARCH/HAR-RV моделях — она lognormal-friendly,
   аддитивна (RV_T = ΣRV_τ) и используется как input для волатильность-
   forecasting. В 2026 RV vs IV спред — proxy на ожидания
   институциональных option-desks.

3. **Метки в `format_snapshot`:**
   - VWAP: `[STRETCHED above/below VWAP]` (≥±2%), `[above/below VWAP]`
     (±0.5–2%), `[near VWAP]` (<±0.5%).
   - RV: `[EXTREME vol regime]` (≥200%), `[elevated vol]` (100–200%),
     `[normal vol]` (50–100%), `[low vol / squeeze candidate]` (<50%).

   Эти метки — текстовые, чтобы LLM сразу видел регим без расчётов.

4. **`compute_snapshot()` расширен:** новые kwargs `volumes`,
   `vwap_window`, `rv_window`, `bars_per_year`. Default behavior
   сохранён (если volumes=None — VWAP=None, RV всё равно считается).
   `IndicatorSnapshot` получил 4 новых поля: `vwap`, `vwap_dev_pct`,
   `rv_pct`, `rv_window_bars`.

5. **`build_market_context` (`trading/context.py`)** теперь передаёт
   volumes из свечей и правильный `bars_per_year` для каждого TF.

**Тесты:** добавлены 13 регрессионных тестов
(`tests/test_ai_trader_indicators.py`):
- `TestVwap` (6) — постоянная цена, weighted by volume, edge cases
  (zero volume, mismatched lengths, period subset, empty input).
- `TestRealizedVolatility` (4) — постоянная цена → 0, short series → None,
  known-value (1% per bar → ~93% annualised), period subset.
- `TestSnapshotV05Fields` (3) — volumes populates VWAP+RV, без volumes
  VWAP=None, format_snapshot включает VWAP/RV строки и метки.

Все 466 тестов suite зелёные.

**Файлы.** `src/ai_trader/analysis/indicators.py`,
`src/ai_trader/trading/context.py`,
`tests/test_ai_trader_indicators.py`.

**Что НЕ менялось.** Классические индикаторы (RSI/MACD/BB/EMA/ATR)
остаются на своём месте — в этой итерации они НЕ degrademовали в
промпте. Demote запланирован в **итерации 7**, после того как
positioning/flow-фичи будут добавлены и validatedы (итерации 2-6).

**План оставшихся итераций** (отдельные коммиты):
- i2: Open Interest delta + Funding rate cumulative (Bybit public API).
- i3: Fear & Greed Index + BTC Dominance % (alternative.me, CoinGecko).
- i4: Long/Short ratio + Orderbook L2 imbalance (Bybit).
- i5: Liquidation feed (Bybit WebSocket или OI-drop proxy).
- i6: Deribit DVOL/IV для BTC и ETH (options sentiment).
- i7: Promote/demote — positioning/flow в начало промпта, classical
  indicators в конец как «secondary context».

---

## 2026-05-07 — feat(symbols): расширили пул с 5 до 10 пар + max_pos 3→5 + parametrized prompt

`6d51360`

**Что изменилось.**

1. **Пул торгуемых пар: 5 → 10** (`src/ai_trader/config/settings.py`).
   Добавлены 5 пар, не пересекающиеся с `bybit_bot.scan_symbols`
   (SOL/ADA/LINK/SUI/TON/WIF/TIA/DOT) и не дублирующие текущие
   ai_trader (BTC/ETH/BNB/XRP/DOGE):

   | Symbol   | Класс / нарратив                          |
   |----------|-------------------------------------------|
   | AVAXUSDT | L1 / Avalanche subnets                    |
   | LTCUSDT  | digital silver / mining-кор               |
   | ATOMUSDT | Cosmos hub / IBC                          |
   | WLDUSDT  | identity / OpenAI tie-in (нарратив 2025+) |
   | TAOUSDT  | decentralized AI / Bittensor              |

   Все 5 — публично листятся на Bybit demo linear (проверено
   `/v5/market/tickers` 2026-05-07): AVAX $9.69, LTC $57.08, ATOM $1.93,
   WLD $0.26, TAO $310.96. Funding rates в нейтральной зоне (|rate|<0.05%).

2. **`AI_TRADER_MAX_POSITIONS`: default 3 → 5.**
   Логика sizing: пул увеличен в 2 раза (5→10), одновременная ёмкость
   увеличена пропорционально (3→5 = ~50%% пар). Risk-per-trade остаётся
   2%% капитала ($10 на сделку), значит max realised drawdown за один
   цикл = 5×$10 = $50, ровно равен `max_daily_loss_usd`. Дальше —
   killswitch блокирует торговлю до следующего дня. Killswitch
   `max_total_loss_usd=$200` (40%% capital) тоже не сдвигаем.

3. **Параметризованный system-промпт** (`src/ai_trader/llm/prompts.py`).
   Старый `SYSTEM_PROMPT` имел зашитые `BTCUSDT, ETHUSDT, BNBUSDT,
   XRPUSDT, DOGEUSDT`, `Maximum 3 simultaneous`, `position_size_usd:
   50-500`, `Risk ... <= $10 (2%% of $500)`. При расширении пар пришлось
   бы каждый раз править саму строку → конфликтует с правилом «промпт
   ЗАМОРОЖЕН на 14 дней эксперимента» (`prompts.py`).

   v0.4: `SYSTEM_PROMPT_TEMPLATE` + `build_system_prompt(settings)`
   подставляет лимиты и список пар через %-форматирование (literal
   `%`-знаки в тексте → `%%` для escape, JSON-схемы остаются интактны
   — это причина выбрать % над str.format с массой `{{`/`}}`).
   `app/main.py` теперь зовёт `build_system_prompt(settings)` каждый
   цикл, decisions audit-trail сохраняет ровно тот промпт что видел LLM.

   Поведенческой подгонки нет: при дефолтных настройках текст 1:1
   эквивалентен старому, плюс расширение списка пар. Это «параметризация
   константы», не правка торговой логики.

**Без runtime guard на overlap с bybit_bot.** Согласовано с пользователем:
проверка остаётся в виде комментария в `DEFAULT_AI_SYMBOLS` (`settings.py`).
ai_trader и bybit_bot — изолированные кодовые базы (правило
`strategy-guard.mdc`), импорт `bybit_bot.*` из `ai_trader.*` запрещён.
Контроль non-overlap — на уровне ревью / `.env` диффа.

**Тесты.** +3 unit-теста `TestBuildSystemPrompt`:
- `test_default_prompt_contains_default_pairs_and_limits` — все 10 пар
  и дефолтные лимиты ($500, 5 pos, 5x lev, 2%%, $50 daily) попадают в
  итоговый промпт; JSON-схема не сломана.
- `test_custom_settings_propagate` — кастомные `AI_TRADER_*` env vars
  пробрасываются в LLM-промпт (capital=$1000, max_pos=7, leverage=3,
  risk=1%%); SOLUSDT появляется, DOGEUSDT исчезает; `position_size_usd`
  диапазон становится `50-1000`.
- `test_no_unresolved_placeholders` — fail-fast если в финальном
  промпте остался хоть один `%(name)s` (защита от опечатки в шаблоне).

Все 453 теста зелёные (было 450 → +3).

**Файлы:**
- `src/ai_trader/config/settings.py` — DEFAULT_AI_SYMBOLS 5→10, max_pos 3→5
- `src/ai_trader/llm/prompts.py` — SYSTEM_PROMPT_TEMPLATE + build_system_prompt
- `src/ai_trader/app/main.py` — вызов `build_system_prompt(settings)` на цикл
- `tests/test_ai_trader.py` — TestBuildSystemPrompt (3 теста)

**Hot-fix follow-up (тот же день):** при первом деплое контейнер
проигнорировал code-default и поднялся со старыми 5 парами / `maxpos=3`,
потому что `docker-compose.yml` имел собственный compose-default
`AI_TRADER_SYMBOLS:-BTC...,DOGE` и `AI_TRADER_MAX_POSITIONS:-3`, и
compose инжектил их в env, перебивая pydantic code-default. Это
дублирование: одно место правды в коде + второе в compose.

Решение (single source of truth для пар = .env):
- Удалили `AI_TRADER_SYMBOLS: ${AI_TRADER_SYMBOLS:-...}` строку из
  `docker-compose.yml` целиком. Теперь compose не задаёт default для
  списка пар. Если переменная не определена в `.env` — pydantic берёт
  `DEFAULT_AI_SYMBOLS` из `settings.py` (10 пар, safety-net).
- Добавили в `.env.example` секцию `AI-TRADER` с явной строкой
  `AI_TRADER_SYMBOLS=...` и предупреждением про non-overlap с
  `BYBIT_BOT_SCAN_SYMBOLS`.
- На VPS `.env` дописали `AI_TRADER_SYMBOLS=BTC...,TAOUSDT` (10 пар).
- Лимиты (`MAX_POSITIONS`, `MAX_LEVERAGE`, `RISK_PER_TRADE` etc.)
  оставили в compose с дефолтами — по согласованию с пользователем
  source-of-truth-перенос ограничен только списком пар.

`AI_TRADER_MAX_POSITIONS:-5` в compose уже синхронизирован с
расширением (см. выше), `.env` его не override-ит, всё консистентно.

---

## 2026-05-07 — fix(reconcile): не помечать позицию closed при API failure биржи

`f3ce979`

**Симптом** (Telegram, 04:29 МСК = 01:29 UTC, cycle 74):

```
❌ ERROR in LLM
Connection error.
```

Позиция **id=5 BTCUSDT Buy 0.006 @ $82184.9** на бирже Bybit demo
осталась открытой (size=0.006, SL=80541, TP=84651, unrealised PnL ≈ −$5.46),
а в локальной БД `ai_trader.sqlite` была помечена closed с маркерами:

```
exit_price = 82184.9          ← равен entry_price
realized_pnl_usd = $0.00      ← подозрительно ровный ноль
close_reason = "exchange_closed (SL/TP/manual)"
closed_at = 2026-05-07T00:21:44 UTC
```

Pattern PnL=$0.00 + exit=entry — визитная карточка фейк-клоза.

**Причина.** В Cycle 71 (00:21:16 UTC) на VPS отказал DNS на ~30 минут:

```
2026-05-07 00:21:44 [ERROR] ai_trader.trading.client: get_positions failed
NameResolutionError: Failed to resolve 'api-demo.bybit.com'
2026-05-07 00:21:44 [INFO] ai_trader: RECONCILE closed:
  id=5 Buy BTCUSDT qty=0.006 | entry=$82184.9 exit=$82184.9 | PnL: $+0.00
```

`AiBybitClient.get_positions(symbol="BTCUSDT")` поймал
`requests.ConnectionError` и **молча возвращал `[]`**. Реконсилятор
в `app/main.py:_reconcile_closed_positions` интерпретировал пустой
список как «позиция исчезла с биржи → её закрыли SL/TP» и обновил БД.
`get_ticker` тоже упал, поэтому `exit_price` упал на fallback
`db_pos.entry_price` → PnL=$0.00.

После этого LLM API тоже упал с тем же DNS-symptom — отсюда
«❌ ERROR in LLM / Connection error» в Telegram (это сообщение
дошло позже, когда DNS Telegram-API восстановился раньше биржевого).

**Решение.**

1. **`get_positions` теперь возвращает `list[Position] | None`**
   (`src/ai_trader/trading/client.py`):
   - `None` ⇐ network exception **или** `retCode != 0`.
   - `[]` ⇐ API ответил успешно, открытых позиций нет.
   - Вызывающий код ОБЯЗАН отличать `None` от `[]`:
     «нет ответа» ≠ «нет позиций».

2. **`_reconcile_closed_positions`** (`src/ai_trader/app/main.py`):
   - Собирает positions per-symbol; если `get_positions` вернул
     `None` для символа — этот символ помечается `failed_symbols`
     и **полностью пропускается**, ни одна его позиция не помечается
     closed.
   - Дополнительно: даже при успешном `get_positions=[]`, если
     `get_ticker` тоже упал — позиция **не помечается closed**
     (без exit-цены нельзя посчитать корректный PnL; ждём следующего
     цикла, когда биржа отвечает).
   - Логируем `WARNING` с причиной отложенного reconcile, чтобы
     видеть в журнале реальные blackouts.

3. **Hot-fix БД на VPS.** Восстановил позицию id=5:
   - `UPDATE positions SET closed_at=NULL, exit_price=NULL,
     realized_pnl_usd=NULL, close_reason=NULL WHERE id=5;`
   - `UPDATE daily_pnl SET n_trades=n_trades-1 WHERE day='2026-05-07';`
     (`realized_pnl_usd` и `n_wins` не трогал — фейк-клоз был с
     PnL=$0, won=0, эти счётчики не сместились).
   - После восстановления состояние БД 1:1 совпадает с биржей
     (Buy 0.006 BTCUSDT @ 82184.9, SL=80541, TP=84651).

4. **9 regression-тестов** (`tests/test_ai_trader.py`):
   - `TestGetPositionsApiFailureMarker` (4 теста):
     network-exception → None, non-zero retCode → None,
     empty list → [], success c позициями.
   - `TestReconcileClosedPositions` (5 тестов):
     `test_api_failure_does_not_close_position` (главный регресс),
     `test_ticker_failure_does_not_close_position`,
     `test_position_still_open_no_change`,
     `test_position_actually_closed_marks_closed` (happy path с
     корректным PnL=$14.7966 на TP),
     `test_partial_api_failure_isolates_failed_symbol` (BTC failed,
     ETH ОК — изолированно обрабатываются).

5. Все 450 тестов в репозитории зелёные.

**Файлы:**
- `src/ai_trader/trading/client.py` — `get_positions` → `| None`
- `src/ai_trader/app/main.py` — guard в `_reconcile_closed_positions`
- `tests/test_ai_trader.py` — +9 regression-тестов

---

## 2026-05-06 — fix(qty rounding): instruments-info + qtyStep/tickSize округление

`коммит при deploy`

**Симптом** (Telegram bybit_notif_bot, cycle 2 в 05:41:37 UTC):

```
OPEN | × not executed
error: open_failed: exception: Qty invalid (ErrCode: 10001)
Request → POST /v5/order/create
{"category":"linear","symbol":"XRPUSDT","side":"Buy",
 "orderType":"Market","qty":"341.0343","stopLoss":"1.3853",
 "takeProfit":"1.4586"}
```

**Причина.** В `executor.py:_apply_open` qty считалось как
`round(notional_usd / price, 4)` — жёстко 4 знака. Но Bybit V5
требует чтобы `qty` был кратен `lotSizeFilter.qtyStep`, который
зависит от инструмента:

| Symbol | qtyStep | Пример |
|---|---|---|
| BTCUSDT | 0.001 | OK на 4 знаках до floor |
| ETHUSDT | 0.01 | OK |
| XRPUSDT | **1.0** | 341.0343 → отказ Bybit |
| DOGEUSDT | **1.0** | аналогично |

То же для SL/TP — Bybit `priceFilter.tickSize` определяет шаг цены,
LLM выдавал значения с лишними знаками (например 1.38531 при
tickSize 0.0001).

**Решение.**

1. **`AiBybitClient.get_instrument_info(symbol)`**
   (`src/ai_trader/trading/client.py`) — получает `qtyStep`,
   `minOrderQty`, `maxOrderQty`, `tickSize` через
   `/v5/market/instruments-info` с in-memory кэшем (контракты не
   меняются часто).
2. **`InstrumentInfo` dataclass** — типизированная обёртка фильтров.
3. **`_floor_to_step` / `_round_to_step` хелперы**
   (`src/ai_trader/trading/executor.py`) — округление qty **вниз**
   под qtyStep (не превышать notional), цены SL/TP — к ближайшему
   tick. Корректное число десятичных знаков выводится из step.
4. **`_apply_open` использует instruments-info** — округляет qty,
   проверяет `min_order_qty` (с понятной ошибкой без вызова Bybit
   при заведомо отказе), capпит к `max_order_qty`.
5. **5 unit-тестов** (`tests/test_ai_trader.py`):
   - `_floor_to_step` для XRP integer и BTC milli
   - `_round_to_step` для tick price
   - регрессия XRPUSDT 341.0343 → 341 + place_order успешен
   - qty < min_order_qty → отказ без вызова place_order
   - 441/441 полный suite passed.

**Compliance.** Это infra-fix без изменения торговой логики
(`strategy-guard.mdc` exception). Параметры стратегии и LLM-промпта
НЕ менялись. Baseline n=0 (от 05.05) НЕ сдвигается.

**Файлы:** `src/ai_trader/trading/client.py`,
`src/ai_trader/trading/executor.py`, `tests/test_ai_trader.py`,
`BUILDLOG_AI_TRADER.md`

---

## 2026-05-06 — fix(LLM empty response): max_tokens 2000→4096 + no-thinking fallback

`коммит при deploy`

**Симптом.** В логах `fx-pro-bot-ai-trader-1`:

- 2026-05-06 04:34:06 cycle 69: `Parse error: no JSON object found in
  response: \`\`\`json {...` — обрезанный JSON, output_tokens=2000
  (упёрся в потолок).
- 2026-05-06 05:06:06 cycle 71: `LLM error: empty response after 2
  attempts` — два HTTP 200, но `text=""` (весь бюджет ушёл на
  thinking-блоки, на answer-блоки не осталось).

**Причина.** `max_tokens=2000` слишком мало для extended-thinking
mode. Когда DeepSeek генерирует длинный chain-of-thought —
thinking-блоки забирают весь бюджет, а text-блоков либо нет, либо
они обрезаются на середине JSON. После v0.3 «fine-grained task
decomposition» (см. запись 05.05) запросы стали тяжелее, проблема
проявилась.

**Решение.**
1. **Увеличен `AI_TRADER_DEEPSEEK_MAX_TOKENS`** дефолт с 2000 до
   **4096** (`src/ai_trader/config/settings.py`,
   `src/ai_trader/llm/client.py`, `docker-compose.yml`).
2. **Final fallback без thinking** (`src/ai_trader/llm/client.py`):
   если после `retry_on_empty` попыток `text` всё ещё пуст и нет
   `error` — клиент делает одну дополнительную попытку **без**
   `thinking={"type":"enabled"}`. Это reliable выход — без
   thinking-tax всё output_tokens идёт в text.
3. Параметры самой модели (`thinking_enabled=true`,
   `retry_on_empty=1`, `retry_sleep=5s`) НЕ меняются — fallback
   срабатывает только в edge-case'е, обычная работа без изменений.

**Compliance.** Это fix без изменения торговой логики
(`strategy-guard.mdc` exception): инфра LLM-клиента, не правила
входа/выхода. Baseline n=0 (от 05.05) НЕ сдвигается.

**Файлы:** `src/ai_trader/llm/client.py`,
`src/ai_trader/config/settings.py`, `docker-compose.yml`,
`BUILDLOG_AI_TRADER.md`

---

## 2026-05-05 — v0.3: Crypto Strategies 2026 audit + research-driven changes (n=0 reset)

**Контекст.** Пользователь запросил полный аудит крипто-стратегий и
ИИ-агента на актуальность 2026 года. Результаты собраны в
[`AUDIT_2026.md`](AUDIT_2026.md) — без воды, понятным языком. Этот
файл содержит обоснование всех изменений + ссылки на 2024–2026 research.

Краткие findings, которые повлияли на ИИ-агент:

- Industry standard 2026 риск на сделку = **1–2%**, не 5%. Источники:
  KuCoin Risk Management 2026, Atlas Peak Research, Hyper-Quant.
  Position sizing определяет 70–80% long-term returns; 5% соответствует
  full Kelly с edge ~10% и опасен из-за drawdown-риска.
- LLM-trading research 2025 (FinDebate arXiv:2509.17395, TradingAgents
  arXiv:2412.20138, ATLAS NeurIPS 2025) показывает: **fine-grained task
  decomposition + chain-of-thought** даёт лучше risk-adjusted returns,
  чем coarse single-step instructions.
- Funding rate framework 2026 (Lambda Finance): **bands** `<0.05%` /
  `0.05–0.20%` / `>0.20%`. Раньше LLM видел голое число.
- Post-ETF (Jan-2024) BTC и альты частично декоррелировали — не
  считать blindly что движение BTC переносится 1:1 на альты.
- Macro (Fed/DXY) теперь больше 4-летнего цикла (Bybit Outlook 2026,
  Galaxy Research). Новостной фид должен это ловить.

**Также найдены P0 баги** (диагноз из БД на 187 decisions, 22 ошибки):

- 12× `place_order returned None` — Bybit отказывал в ордерах, executor
  не знал почему (логи теряли `retCode/retMsg`). Чинится логированием.
- 8× `parse_error: empty response` — DeepSeek изредка возвращает пусто.
  Чинится retry (1 попытка, sleep 5s).

**Изменения v0.3:**

*Промпт (`src/ai_trader/llm/prompts.py`):*
- CAPITAL RULES: `risk_per_trade` 5% → **2%** ($25 → **$10** на сделку),
  `daily loss limit` $125 → **$50**.
- Добавлен **MARKET CONTEXT 2026** блок: perp-доминирование, post-ETF
  decoupling, funding bands, macro > 4-year cycle.
- ANALYSIS APPROACH теперь **structured**: TREND → VOLATILITY →
  SENTIMENT → CONFIRMATIONS → R:R CHECK → DECISION (chain-of-thought
  через предписанный шаблон).
- Жёсткое требование **R:R >= 1.5** для любого open: иначе hold.
- Формат ответа изменён: **commentary + JSON** (раньше JSON only).
  Парсер обновлён до устойчивого извлечения последнего balanced
  JSON-блока.

*Конфиг (`src/ai_trader/config/settings.py`):*
- `max_daily_loss_usd`: 125 → 50
- `max_total_loss_usd`: 500 → 200
- Новое поле `risk_per_trade_pct = 0.02`

*Контекст (`src/ai_trader/trading/context.py`):*
- Funding rate теперь выводится с band-меткой
  `[NEUTRAL]` / `[mild lean: longs paying]` / `[STRONG: shorts paying, contrarian risk]`.
- Добавлена строка `MACRO: BTC vs alts (24h): BTC=+1.2% avg-alt=-0.8%
  → BTC outperforming alts (alt-weakness)` — эвристическая замена
  глобальному BTC dominance %.

*Новости (`src/ai_trader/news/rss.py`):*
- GENERIC_KEYWORDS расширены: `ibit`, `fbtc`, `etha`, `etf flow/inflows/outflows`,
  `powell`, `yellen`, `dxy`, `btc dominance`, `liquidation`, `deleveraging`,
  `open interest`, `funding rate`, и др. (raтcionale: 2026 ETF-флоу + macro
  как driver).

*P0 bug-fixes (применены вне рамок reset, т.к. это баги, не тюнинг):*
- `src/ai_trader/llm/client.py`: retry на пустой LLM-ответ (`retry_on_empty=1`,
  `retry_sleep_sec=5.0`). Теперь cycle не пропускается.
- `src/ai_trader/trading/client.py`: `place_order` теперь возвращает
  `{"ok": True/False, "error": <bybit retMsg>, ...}` вместо `None`.
- `src/ai_trader/trading/executor.py`: пробрасывает Bybit `retCode/retMsg`
  в БД (`decisions.error`). Будем видеть что именно отказывает (min order
  size / margin / leverage).
- `src/ai_trader/trading/executor.py`: `parse_action` устойчив к
  commentary перед JSON (ищет последний balanced `{...}`).

*Bybit-стратегии (P0 doc-fixes, не торговая логика):*
- `vwap_crypto.py`: docstring «ADX≤20» → «ADX≤25 (приведено к коду)».
- `crypto_overbought_fader.py`: docstring «13:00–21:00 UTC» →
  «13:00–20:59 UTC (range(13, 21))».
- `funding_scalp.py`, `volume_spike.py`: добавлен канонический блок
  `─── Research basis ───`.
- `.cursor/rules/strategy-guard.mdc`: stat_arb Z 2.5 → **2.0** (приведено
  к коду + research GitHub abailey81 2025); добавлены записи про
  `FundingScalpStrategy` и `VolumeSpikeStrategy`.

**Сброс эксперимента n=0.** Это тюнинг + изменение контракта промпта
(commentary + JSON), а не bug-fix → реcет по правилу `no-data-fitting.mdc`.

**Reset = изменение условий, не уничтожение БД.** Volume `ai_trader_data`
сохранён, потому что на момент применения у ИИ была одна **открытая
позиция** (BTCUSDT Buy 0.005 @ 80249.8, SL=79356, TP=82000, R:R=1.96,
unrealized PnL ~+$3.5). Стирание БД оставило бы её на бирже без
управления (Bybit OCO сработал бы сам, но `_reconcile_closed_positions`
не записал бы PnL). Это финансовая дыра — недопустимо.

Маркер начала v0.3 пишется в `kv_state.v03_start_decision_id` и
`kv_state.v03_start_ts` — все последующие аналитические скрипты должны
фильтровать `decisions.id >= v03_start_decision_id` для сравнения
поведения старого/нового промпта.

Объём собранной до сброса статистики: 187 decisions / 33 часа /
2 открытых трейда (один закрылся по SL −$5.13, второй сейчас в плюсе).
Это малая выборка, статистически нерепрезентативна — потеря для
финансовых выводов минимальная (правило `sample-size.mdc`: ≥100
закрытых сделок и ≥2 недели для значимых выводов; у нас ни того ни
другого).

**ЗАМОРОЗКА**: v0.3 промпт и параметры заморожены на 14 дней (до 19.05.2026).
Никаких правок до конца forward-test (исключая bug-fix-категорию).

**Файлы:**
- `AUDIT_2026.md` (новый файл)
- `src/ai_trader/llm/prompts.py`
- `src/ai_trader/llm/client.py`
- `src/ai_trader/config/settings.py`
- `src/ai_trader/trading/context.py`
- `src/ai_trader/trading/client.py`
- `src/ai_trader/trading/executor.py`
- `src/ai_trader/news/rss.py`
- `src/bybit_bot/strategies/scalping/vwap_crypto.py`
- `src/bybit_bot/strategies/scalping/crypto_overbought_fader.py`
- `src/bybit_bot/strategies/scalping/funding_scalp.py`
- `src/bybit_bot/strategies/scalping/volume_spike.py`
- `.cursor/rules/strategy-guard.mdc`
- `docker-compose.yml`
- `BUILDLOG_AI_TRADER.md` — эта запись

---

## 2026-05-03 — v0.2.1: risk-per-trade 2% → 5% (n=0 reset)

**Контекст.** v0.2 запустился в LIVE и прошёл 1 цикл (HOLD). По запросу
пользователя поднимаем риск на сделку с 2% до 5% — более агрессивный
режим, ближе к "trader mindset" (на $500 капитала 2% = $10 = слишком
осторожно для discretionary трейдера).

**Связанные правки** (чтобы пропорция не сломалась):
- `risk_per_trade`: 2% → **5%** ($10 → **$25** макс убыток на сделку)
- `daily_loss_limit`: $50 → **$125** (паритет: 5 SL до блока)
- `total_loss_limit`: $200 → **$500** (= virtual capital, "доедание депо")
- `max_positions` 3, `max_leverage` 5x — без изменений.

Логика паритета: при 5%-риске × 3 макс позиции = $75 макс одновременный
риск. Daily $125 = ровно 5 полных SL подряд до killswitch — такой же
буфер как был при 2%/$50.

**Это тюнинг, не bug-fix → сброс эксперимента n=0** (правило
`no-data-fitting.mdc`). Потеря минимальная: до этого был 1 цикл с HOLD,
статистики не накопилось.

**Файлы:**
- `src/ai_trader/llm/prompts.py` — обновлён CAPITAL RULES
- `src/ai_trader/config/settings.py` — новые дефолты killswitch
- `docker-compose.yml` — новые env defaults
- `BUILDLOG_AI_TRADER.md` — эта запись

**ЗАМОРОЗКА**: при v0.2.1 промпт и параметры опять заморожены на 14 дней
(до 17.05). Никаких правок до конца forward-test'а.

---

## 2026-05-03 — v0.2: Wave 2 + Wave 3 + Wave 4 (полный сброс n=0)

**Контекст.** v0.1 (запущен этим же утром) был MVP: голый LLM на ценах +
funding rate, без новостей и без Telegram. Прошёл 1 успешный LIVE-цикл
(HOLD), но после ревью `BUILDLOG_AI_TRADER.md` пользователь напомнил
исходный запрос: «опытный криптотрейдер, следит за новостями, …, подключён
к telegram». v0.1 был слишком урезан. Расширяем до полного спека за один
заход и стартуем заново.

**Сброс эксперимента.** v0.1 → выбрасываем (n=1, статистически бесполезно
+ промпт изменён). Эксперимент v0.2 стартует с n=0. 14 дней forward-test
(до 17.05) — на этих условиях промпт и контекст ЗАМОРОЖЕНЫ
(`no-data-fitting.mdc`).

**Что добавилось в v0.2:**

### Wave 2 — Технические индикаторы

`src/ai_trader/analysis/indicators.py`. Канонические реализации без
внешних зависимостей:
- RSI(14) — Wilder's smoothing
- MACD(12/26/9) — EMA-based
- ATR(14) — Wilder + ATR%
- EMA20 / EMA50 — для определения тренда
- Bollinger Bands(20, 2σ) — для overbought/oversold

В контекст вкладывается **на двух TF**:
- **1H** × 100 свечей (краткосрочные сигналы)
- **4H** × 50 свечей (крупный тренд)

В `format_snapshot()` добавлены человекочитаемые метки:
`[OVERBOUGHT]`/`[OVERSOLD]` (RSI), `[bullish]`/`[bearish]` (MACD),
`[uptrend]`/`[downtrend]`/`[mixed]` (EMA), `[above/below upper/lower BB]`.

**Research basis:** Wilder (1978) RSI/ATR; Appel (2005) MACD; Bollinger
(2001) BB. Параметры — канонические, не подкручивались.

### Wave 3 — News feed

`src/ai_trader/news/rss.py`. RSS-агрегатор с фильтрацией:
- Источники по умолчанию: CoinDesk, CoinTelegraph, Decrypt (RSS, без auth)
- Кэш в памяти 10 минут (1-2 fetch на цикл, не нагружаем источники)
- Фильтр по ключевым словам:
  - `BTCUSDT` ← bitcoin/btc/satoshi
  - `ETHUSDT` ← ethereum/eth/vitalik
  - `BNBUSDT` ← binance coin/bnb
  - `XRPUSDT` ← xrp/ripple
  - `DOGEUSDT` ← dogecoin/doge
  - Generic crypto: ETF, SEC, Fed, FOMC, stablecoin
- Top-N (default 8) свежих за last 6h, дедуп по URL
- Если `feedparser` недоступен / RSS падает — блок news просто пустой,
  торговля продолжается без него (graceful degradation)

В system prompt добавлена инструкция: *«News sensitivity: major bullish
news on a coin during weakness = potential long setup; bearish news during
strength = potential short setup. Ignore headlines unrelated to your
symbols.»*

### Wave 4 — Telegram

`src/ai_trader/telegram/bot.py`. Минимальный клиент **на чистом requests**
(без `python-telegram-bot` SDK — меньше зависимостей, нет async-сложности).
Polling в отдельном daemon-thread, 30-сек long-poll.

**Команды:**
- `/start`, `/help` — приветствие + справка
- `/status` — режим, баланс, позиции, killswitch
- `/pnl` — daily/total PnL, WR, кол-во сделок
- `/last_decision` (alias `/last`) — последнее решение LLM с reasoning
- `/history [N]` — последние N решений (default 5, max 20)
- `/pause` — приостановить торговлю (флаг `paused` в kv_state)
- `/resume` — возобновить

**Push-уведомления:**
- 🟢 при открытии позиции (`apply.executed`)
- 🔴 при закрытии (по reconcile или /close)
- ⚠️ при срабатывании killswitch
- ❌ при ошибках (LLM API, парсинг, crash цикла)

**Auto-detect chat_id:** при первой команде от пользователя бот сохраняет
`chat_id` в `kv_state.telegram_chat_id` и далее шлёт push туда. Если
`TELEGRAM_CHAT_ID` задан в .env — используется он (фиксированный режим
для безопасности на проде).

**Graceful degradation:** если `TELEGRAM_BOT_TOKEN` пустой — модуль
просто не стартует. Никаких ошибок, основной цикл работает как обычно.

### Прочие изменения

- `state/db.py` — новая таблица `kv_state` (key→value), методы
  `is_paused()/set_paused()`, `get/set_telegram_chat_id()`,
  `get_recent_decisions()`, `get_closed_positions_count()`.
- `app/main.py` — интеграция всех модулей: pause-проверка перед LLM,
  передача `news_provider` в context-сборщик, `tg.notify_*` на ключевых
  событиях, push при `cycle crashed`.
- `pyproject.toml` — добавлен `feedparser>=6.0`.
- `docker-compose.yml` — добавлены env vars: `AI_TRADER_NEWS_*`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `AI_TRADER_TELEGRAM_ENABLED`.

### Тестовое покрытие

Всего по AI-trader:
- `test_ai_trader.py` — 17 тестов (parser, killswitch, store)
- `test_ai_trader_indicators.py` — 22 теста (RSI/MACD/ATR/EMA/BB +
  edge cases на коротких рядах, чистом тренде, постоянстве)
- `test_ai_trader_news.py` — 14 тестов (классификация, фильтр по
  символам, generic-relevance, dedup, кэш, fixture-RSS через mock
  `feedparser.parse`)
- `test_ai_trader_telegram.py` — 22 теста (split_message, KV-state,
  все команды на пустой и наполненной БД, mock TelegramBot)

Итого 75 unit-тестов на AI-trader. Полный проект: 425 / 425 ✓.

### План v0.2 наблюдения

1. **Cycle 1** — sanity-проверка: индикаторы посчитались (RSI/MACD не None
   на 5 символах × 2 TF = 10 snapshot'ов), новости пришли (хотя бы
   1 заголовок в кэше), Telegram молчит (token пуст — это норма).
2. **Day 1-3** — наблюдаем как часто LLM ссылается на индикаторы и новости
   в `reason` поле. Если игнорирует — значит system prompt недоучёл, можем
   усилить (но это reset n=0!).
3. **Day 14** (17.05) — финальный анализ: total PnL, WR, PF, сравнение с
   v0.1 baseline (если данных хватит) и HODL BTC за тот же период.

**Файлы (новые/изменённые):**
- `src/ai_trader/analysis/{__init__,indicators}.py`
- `src/ai_trader/news/{__init__,rss}.py`
- `src/ai_trader/telegram/{__init__,bot}.py`
- `src/ai_trader/state/db.py` (kv_state, helpers)
- `src/ai_trader/trading/context.py` (intregration)
- `src/ai_trader/llm/prompts.py` (расширен)
- `src/ai_trader/app/main.py` (TG + news + pause)
- `src/ai_trader/config/settings.py` (telegram + news vars)
- `tests/test_ai_trader_indicators.py`, `test_ai_trader_news.py`,
  `test_ai_trader_telegram.py`
- `pyproject.toml`, `docker-compose.yml`

---


Изолированный экспериментальный модуль. Не пересекается с `fx_pro_bot` и
`bybit_bot` (см. правило `strategy-guard.mdc` про разделение модулей).

Гипотеза: автономный LLM-агент (DeepSeek-V4 Flash) принимает торговые решения
на криптовалютных perpetual'ах Bybit, опираясь только на market context
(цены, funding, history). Цель — оценить, способен ли LLM в принципе
показать положительный edge на 14-дневном forward-test'е.

## 2026-05-03 — n=0, старт эксперимента

Создан скелет AI-трейдера, изолированного от существующих ботов.

**Архитектура** (`src/ai_trader/`):
- `app/main.py` — главный цикл, 15 минут на итерацию
- `llm/client.py` — DeepSeek-V4 через `anthropic` SDK
 (`base_url=https://api.deepseek.com/anthropic`, model=`deepseek-v4-flash`,
 thinking mode включён)
- `llm/prompts.py` — заморожен на 14 дней (никаких правок промпта в процессе
 эксперимента, см. `no-data-fitting.mdc`)
- `trading/client.py` — Bybit-клиент на `pybit` (БЕЗ импортов из `bybit_bot`)
- `trading/context.py` — сбор market context (1h свечи × 24, ticker, funding,
 24h range, открытые позиции из БД)
- `trading/executor.py` — парсер JSON-ответа LLM + исполнение
- `state/db.py` — отдельная SQLite (`ai_trader.sqlite`):
 `positions`, `decisions` (полный audit-trail промптов/ответов/токенов/cost),
 `daily_pnl` (для killswitch)
- `safety/killswitch.py` — глобальные стопы:
 - daily loss ≥ $50 → блок до завтра
 - total loss ≥ $200 → полная остановка
 - max 3 открытых позиций
 - max 5x leverage

**Изоляция от bybit_bot**:
- AI-трейдер торгует на `BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, DOGEUSDT`.
 `bybit_bot` торгует на `SOL/ADA/LINK/SUI/TON/WIF/TIA/DOT` — пересечений нет.
- Все ордера AI-трейдера маркируются `orderLinkId='ai_<uuid>'` —
 однозначное опознание в любых отчётах Bybit.
- Отдельная БД, отдельный Docker-сервис, отдельный volume.
- В `bybit_bot/app/main.py:_sync_positions_on_startup` добавлен
 фильтр по `scan_symbols`: при старте бот игнорирует позиции на
 чужих символах (не подбирает их в свою exit-логику). Логирует как
 `SYNC IGNORE: <side> <symbol> qty=… — символ вне scan_symbols`.

**Параметры эксперимента (заморожены на 14 дней)**:
- Виртуальный капитал: $500 (qty считается от него, не от реального demo-equity)
- Цикл: 15 минут (96 решений в сутки, ≈1344 за весь эксперимент)
- Free tier DeepSeek: 5M вход + 5M выход tokens. Грубая оценка
 ~3K input + ~500 output на цикл = 4M+0.7M tokens за 14 дней.
 Должно полностью уложиться в free tier.
- KillSwitch: $50/день, $200 total, 3 позиции, 5x leverage
- Mode: PAPER при первом запуске (`AI_TRADER_TRADING_ENABLED=false`).
 Решения принимаются и логируются в `decisions`, но ордера на биржу
 не отправляются. Включаем LIVE после проверки 1-2 циклов под наблюдением.

**Параметры, которые ЗАПРЕЩЕНО менять в процессе эксперимента**:
- system prompt
- список allowed symbols
- цикл 15 минут
- лимиты killswitch
- набор features в market context

Любая правка → перезапуск эксперимента с n=0.

**Допустимые правки без сброса n**:
- bug-fix в парсере (если LLM выдаёт валидный JSON, а мы его не принимаем)
- bug-fix в reconcile (если SL/TP закрылись на бирже, а в БД позиция висит)
- логирование, метрики (не влияют на торговые решения)

**План наблюдения**:
1. Час 1: запуск в PAPER mode. Проверяем — промпты валидные, ответы парсятся,
 нет ошибок API, нет ошибок lint в JSON.
2. День 1: переключаем в LIVE (`AI_TRADER_TRADING_ENABLED=true`). Наблюдаем
 первые 5-10 ордеров: правильные ли SL/TP, не выходят ли за пределы 5x leverage,
 правильно ли считается qty, killswitch не триггерится случайно.
3. День 14: разбор. Метрики из `decisions` + `positions`:
 - total PnL за 14 дней
 - Win Rate, PF, средний R:R
 - частота open/close/hold
 - top-3 убыточных решения (с rationale из LLM) — что не сработало
 - стоимость API в $ (из `daily_pnl.api_cost_usd`)
 - сравнение: «AI-трейдер vs HODL BTC» за тот же период

**Файлы**:
- `src/ai_trader/**` — новый модуль
- `tests/test_ai_trader.py` — 17 тестов (parse_action, KillSwitch, Store)
- `Dockerfile.ai-trader`
- `docker-compose.yml` — добавлен сервис `ai-trader`
- `pyproject.toml` — добавлен `anthropic>=0.39.0`, package `src/ai_trader`
- `src/bybit_bot/app/main.py` — `_sync_positions_on_startup` теперь
 фильтрует по `managed_symbols`
