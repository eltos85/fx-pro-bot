# Аудит крипто-стратегий и ИИ-агента — май 2026

Документ объясняет простым языком, что у нас сейчас работает, что устарело,
и что менять. Без воды.

> Источники: academic research 2024–2026 (Springer, MDPI, arXiv, LSE),
> институциональные отчёты (Bybit/Block Scholes Q1-2 2026, Coinbase + Glassnode
> Q1-Q2 2026, Galaxy Asset Management 03-2026, Amberdata 2026 Outlook),
> r/algotrading стресс-тесты 2026, статьи практиков (Lambda Finance, Cryptowisser).
> Ссылки в конце документа.

---

## 1. Executive summary (для тех кто не хочет читать всё)

1. **Базовые подходы (mean-reversion, momentum, ORB, lead-lag, stat-arb) всё ещё работают**, но микроструктура крипты сильно изменилась к 2026 году. Подгоняться под Twitter-моды не надо — но есть конкретные расхождения между нашими доками и кодом, и есть **критичные баги в ИИ-агенте**.

2. **P0 (применить сейчас)**: исправить расхождения в документации (3 несоответствия), починить ИИ-агент (исполняется ~85% решений), добавить retry на пустой ответ DeepSeek.

3. **P1 (ИИ-агент v0.3, согласован сброс)**: понизить риск с 5% до 2% (research 2026 единодушен — 1–2%), добавить multi-agent debate структуру в промпт, расширить контекст funding+OI парой, добавить BTC dominance.

4. **P2 (требуют sample-size валидации, ≥100 сделок и p<0.05)**: пересмотреть `CORR_MIN=0.5` в lead-lag (после ETF корреляция снизилась), пересмотреть `Z_ENTRY` в stat-arb на основе наших закрытых сделок.

5. **P3 (long-term, через 1–2 квартала)**: on-chain метрики в news (NUPL, MVRV), FOMC-календарь, перевод stat-arb на копулы или GHE (research 2024–2025 показывает превосходство).

---

## 2. Что изменилось в крипте к 2026 году

Чтобы понять, актуальны ли наши стратегии, нужно знать, в каком рынке они теперь работают. Кратко — что произошло за 2024–2026:

### 2.1. Перпы стали доминировать

В 2025 году объём торгов перпами составил **$61.7 трлн** (+29% к 2024), при том
что спот-объём был всего **$18.6 трлн** [Reuters 04-2026]. То есть **77% всего
крипто-объёма — это деривативы**.

**Что это значит для нас**: наш фокус на perp-фьючерсах Bybit полностью совпадает
с трендом. Это правильное место.

### 2.2. ETF разорвал связь BTC↔альты

После одобрения spot Bitcoin ETF в январе 2024 (IBIT теперь >60% всего ETF AUM)
произошло **структурное разделение** BTC и альтов: корреляции снизились в
коротких и длинных окнах, BTC превращается в отдельный класс активов
[LSE 2026 paper, Coinbase + Glassnode Q1-2026: BTC dominance ~59%].

**Что это значит для нас**: стратегия `btc_leadlag` (мы торгуем альты в сторону
BTC) **всё ещё работает академически** [Springer Asia-Pacific Financial Markets 2026:
Granger-causality BTC→альты подтверждена], но **сила сигнала ниже** чем была
в 2022–2023. Наши пороги `CORR_MIN=0.5` могли стать слишком мягкими в новой
реальности — это P2 (требует валидации на наших сделках, не на чужих).

### 2.3. Macro теперь важнее 4-летнего цикла

Bybit Outlook 2026 и Galaxy Research пишут одно и то же: **роль ФРС, политики
и институциональных потоков** теперь больше, чем халвинг-цикла. 82% институционалов
в Q2 2026 видят рынок в bear/late-bear (vs 31% в декабре 2025) — это значит
рынок ходит за макро, а не за «4-летним циклом».

**Что это значит для нас**: у ИИ-агента **нет в новостной ленте FOMC-календаря
и макро-контекста**. Это не обязательно дыра, но ограничение — сейчас агент
видит только заголовки RSS. Добавить экономический календарь — P3.

### 2.4. Funding rate сам по себе слабее, чем funding + OI

Старый подход «высокий funding = шорт» — частичный сигнал. **Современная связка
2026** [Lambda Finance 04-2026, Coinglass]:

- **Funding bands**: <0.05% — нейтрально, 0.05–0.20% — лёгкий перекос,
  >0.20% — сильный перекос.
- **Funding + OI together**: «OI растёт + funding положительный» = breakout
  попытка через 1–2 дня. Funding **в одиночку реагирует медленно** и
  пропускает быстрые движения.

**Что это значит для нас**: `funding_scalp.py` смотрит **только на funding rate**,
без OI. Это базовый подход, не state-of-the-art. Не сломан, но недокручен. P3.
ИИ-агент видит funding rate, но **не видит OI delta** — добавить в контекст. P1.

### 2.5. Stat-arb: cointegration > correlation, copula > cointegration

Research 2024–2025 [Springer Financial Innovation 2024, Computational Economics 2025]:

- На крипто-парах **cointegration-based** подходы превосходят простую
  correlation-based.
- **Copula + cointegration** (Springer 2024) — ещё лучше.
- **Generalized Hurst Exponent (GHE)** — ещё лучше Sharpe/Sortino, чем
  classical pairs trading.

**Что это значит для нас**: `stat_arb_crypto.py` уже использует ADF p-value
(cointegration test) — это правильно. Z-порог входа в коде = **2.0** (что
соответствует research для CEX altcoins, [GitHub abailey81 2025: ±2.0 entry,
0.0 exit, ±3.0 stop]). **Но в `strategy-guard.mdc` написано Z=2.5σ — это
расхождение с кодом** (P0 doc fix). Переход на копулы / GHE — P3.

### 2.6. Liquidity → стейблкоины

В Q1 2026 крипто-маркет (без стейблов) упал на ~18%, но **supply стейблкоинов
вырос с $308B до $318B** [Coinbase + Glassnode]. То есть деньги не уходят,
а паркуются. Это behavioral signal — институционалы ждут, не выходят.

**Что это значит для нас**: для нашего trading-фокуса это полезный context-флаг,
а не сигнал. ИИ-агент мог бы видеть «stablecoin supply растёт» как индикатор
risk-on/risk-off настроя. P3.

### 2.7. AI/LLM трейдинг сильно продвинулся в 2025–2026

State-of-the-art подходы к LLM-агентам [arXiv FinDebate 09-2025, TradingAgents 2024,
ATLAS NeurIPS 2025, TrustTrade 2025]:

- **Multi-agent debate**: разные роли (Bull researcher, Bear researcher, Risk
  manager, Trader с разными профилями) дебатируют, прежде чем принять решение.
  Лучше single-agent, потому что заставляет противопоставлять аргументы.
- **Fine-grained task decomposition**: дробить «проанализируй рынок» на
  мелкие задачи (отдельно тренд, отдельно volatility, отдельно sentiment).
  Лучше coarse-grained (всё одним промптом).
- **TrustTrade**: cross-validation между независимыми LLM-агентами для борьбы
  с галлюцинациями (LLM может «придумать» что funding=0.5% когда он 0.05%).
- **Adaptive-OPRO**: динамическое улучшение промпта на основе обратной связи
  от рынка. Слишком сложно для нашего масштаба.

**Что это значит для нас**: наш ИИ — single-agent с одним промптом,
fine-grained декомпозиции нет. Это **базовый подход 2024 года**. Самый
доступный апгрейд — multi-step reasoning внутри одного промпта (chain-of-thought):
«сначала проанализируй тренд, потом volatility, потом news, потом прими
решение». Это P1.

### 2.8. Отдельная проблема — backtests vs реальное исполнение

r/algotrading стресс-тест 2026 [Kalena 2026]: **34–67% разрыва между бэктестом
и live результатом — это slippage**. ~92% retail algo-трейдеров теряют деньги.

**Что это значит для нас**: у нас есть `slippage_guard` в advisor (FX). У
Bybit-бота тоже есть execution layer. ИИ-агент **не учитывает slippage** в
своём position sizing вообще — он просто пишет SL/TP. Это не критично на
$500 demo, но станет проблемой на реальных деньгах. P3.

---

## 3. Аудит Bybit-стратегий

8 файлов в `src/bybit_bot/strategies/scalping/`. Для каждой — кратко: что делает, статус относительно 2026 research, что менять.

| Стратегия | Тип | Research-канон | Статус 2026 | Действие |
|---|---|---|---|---|
| `btc_leadlag.py` | Lead-lag (BTC→альты) | Springer 2026 | Актуальна, но post-ETF сила сигнала ниже | P2: проверить CORR_MIN=0.5 на наших сделках |
| `session_orb.py` | ORB пробой | FMZQuant 2024 | Актуальна (UTC sessions match research) | Оставить, мониторить |
| `turtle_soup.py` | Mean-reversion (fade ложного пробоя) | Connors/Raschke 1995, ICT 2024 | Актуальна, классика | Оставить, мониторить |
| `vwap_crypto.py` | Mean-reversion VWAP | FMZQuant 2024 | Актуальна | **P0: docstring пишет ADX≤20, код ADX_MAX=25** — расхождение |
| `stat_arb_crypto.py` | Pairs / Z-score | abailey81 2025, MDPI 2023 | Актуальна (Z=2.0 = research norm для CEX) | **P0: strategy-guard.mdc пишет Z=2.5, код Z=2.0** — расхождение |
| `crypto_overbought_fader.py` | Mean-reversion ensemble (SHORT) | Internal mining 90-day OOS | Актуальна (data-driven) | **P0: docstring «13:00–21:00 UTC», код range(13,21) = 13–20** — расхождение |
| `funding_scalp.py` | Funding rate перекос | inline ref 2025, нет research-блока | Базовый подход, без OI | **P0: добавить research-блок в docstring** |
| `volume_spike.py` | Объёмный спайк momentum | inline ref 2025, нет research-блока | Базовый подход, без orderflow | **P0: добавить research-блок в docstring** |

### 3.1. Подробности по каждой проблеме (P0)

**`vwap_crypto.py` — внутреннее противоречие docstring vs код:**
- Docstring (строки 63–69): «Фильтр: ADX ≤ 20»
- Код (строка 41): `ADX_MAX = 25.0`
- **Решение**: привести docstring к коду (25.0 — это то что реально работает).
  Не менять код без sample-size validation.

**`stat_arb_crypto.py` — расхождение правила и кода:**
- `.cursor/rules/strategy-guard.mdc`: «`StatArbCryptoStrategy`: Z_entry=2.5σ»
- Код (строка 42): `Z_ENTRY = 2.0`
- Research 2025 для CEX altcoins: **Z = ±2.0** [GitHub abailey81 2025]
- **Решение**: код актуален, обновить правило с 2.5 на 2.0 со ссылкой на
  research 2025. Backstory: 2.5 было в правиле как историческая запись,
  не из недавнего research.

**`crypto_overbought_fader.py` — off-by-one в часах:**
- Docstring (строка ~84): «NY: 13:00–21:00 UTC»
- Код: `COF_SESSION_HOURS = range(13, 21)` (часы 13–20 включительно, не 21)
- **Решение**: привести docstring к коду — «13:00–20:59 UTC» (NY-сессия по факту).

**`funding_scalp.py`, `volume_spike.py` — нет research-блока:**
- В обоих модулях есть inline-ссылки на 2025 источники (TradingView, KangaAnalytics),
  но нет канонического блока «─── Research basis ───» в docstring.
- В `strategy-guard.mdc` они **вообще не упомянуты**.
- **Решение**: добавить research-блок в docstring, добавить запись в правило.

### 3.2. P2 — требуют валидации (НЕ применять без sample-size)

**`btc_leadlag.py` — `CORR_MIN=0.5`**:
- Post-ETF корреляции BTC↔альт снизились (LSE 2026 paper).
- Возможно стоит поднять до 0.6 или 0.7, но **без валидации на наших закрытых
  сделках это подгонка**.
- Действие: собрать ≥100 сделок этой стратегии, посмотреть WR/EXP по бакетам
  корреляции (0.5–0.6, 0.6–0.7, 0.7+) и принять решение.

**`stat_arb_crypto.py` — корреляция по ценам vs returns**:
- В `btc_leadlag.py` корреляция считается по log-returns (правильно по канону).
- В `stat_arb_crypto.py` Pearson считается по closes (ценам). Это нормально для
  cointegration-test, но для отбора пар обычно используют returns.
- Действие: обсудить отдельно. Менять без валидации — подгонка.

### 3.3. P3 — long-term улучшения

- `funding_scalp.py`: добавить OI delta в условие входа («funding ≥ X **И**
  OI растёт»). Это сильно сократит false positives. Источник: Lambda Finance,
  Coinglass.
- `stat_arb_crypto.py`: переход на copula-based или GHE. Springer 2024 / Computational
  Economics 2025 показывают превосходство.
- `volume_spike.py`: добавить orderflow / DOM-проверку. Сейчас сигнал на голом
  OHLCV — это то, против чего предостерегает r/algotrading анализ 2026.

---

## 4. Аудит ИИ-агента

Анализ за 33 часа работы (133 решений → к моменту финального снапшота 187 решений).

### 4.1. Поведение

| Метрика | Значение |
|---|---|
| Всего решений | 187 |
| `hold` | 163 (87%) |
| `open` (попыток) | ~14 |
| `open` (успешно записано в БД) | 2 |
| Ошибок | 22 (12%) |
| Закрыто по SL | 1 |

**Hold-bias 87%** — ожидаемо для прописанного «patient discretionary trader» с
требованием 2+ confirmation. Это **не баг, а фича**. Но 12% error rate — баг.

### 4.2. Распределение ошибок

```
12× place_order returned None       — Bybit отказывает в ордере
 8× parse_error: empty response     — DeepSeek возвращает пустой ответ
 1× HTTP 402 Insufficient Balance   — historical (исправлено пополнением)
 1× parse_error: no JSON found      — модель вернула текст без JSON
```

**12 «place_order returned None»** — это критично. ИИ принимает решение
открыть позицию, executor вызывает `place_order`, и Bybit отвергает. Возможные
причины:
- min order size (Bybit требует минимум по каждому символу: BTC ~0.001, ETH ~0.01)
- leverage не выставлен перед ордером
- недостаточно margin
- неверный side / неверная цена SL/TP

Нужно **залогировать ответ Bybit при ошибке** (сейчас просто `None`).
Это P0 bug-fix.

**8 empty response от DeepSeek** — это retry-кейс. Сейчас цикл просто пропускается.
P0 — добавить retry (1 раз, sleep 5s).

### 4.3. Промпт (`SYSTEM_PROMPT` в `src/ai_trader/llm/prompts.py`)

**Что хорошо:**
- Чёткие capital rules (capital, leverage, max positions, daily limit).
- Allowed pairs whitelist.
- Multi-confirmation requirement (2+ сигналов для входа).
- JSON-only output (минимум парс-ошибок при правильном ответе).
- Patience-приоритет («HOLD is always safe»).

**Что слабо относительно state-of-the-art 2026:**
- **Нет chain-of-thought**. Промпт просит сразу принять решение. Research 2025
  (FinDebate, ATLAS) показывает, что fine-grained задача даёт лучшие
  risk-adjusted returns.
- **Нет явного risk:reward calculation**. Промпт говорит «put SL/TP», но не
  заставляет считать R:R и проверять, что reward ≥ 1.5× risk.
- **Нет macro-контекста**: BTC dominance, DXY, текущая phase цикла (bull/bear/late-bear).
- **Funding rate в промпте упомянут, но в контексте не выводится**.
  То есть LLM знает что такое funding, но не видит его значения каждый цикл.
- **OI delta не упомянут** — а это сильнее funding в одиночку.
- **Нет структуры «pre-decision checklist»**: research показывает, что заставить
  LLM явно отметить «trend OK / volatility OK / news OK» работает лучше чем
  свободная форма.

### 4.4. Индикаторы

Текущий набор (`src/ai_trader/analysis/indicators.py`):
- RSI(14), MACD(12/26/9), ATR(14), EMA20/50, BB(20,2)
- Каноны: Wilder 1978, Appel 2005, Bollinger 2001 — **базовая классика**, OK.
- 1H + 4H — **OK для среднесрочных решений**, но можно добавить D1 для macro-trend.

**Чего нет, что есть в community 2026:**
- VWAP-anchored (есть в Bybit-стратегии `vwap_crypto.py`, не выведено в ИИ).
- OI delta (нет вообще).
- Funding rate в формате «текущее значение + 24h-тренд + band-классификация».
- BTC dominance (для альтов как macro-trigger).
- On-chain (NUPL, MVRV) — сложно интегрировать, отдельный API.

**Минимум на P1:** добавить в контекст значение funding rate каждого символа
(уже есть в `Ticker`!) с band-классификацией, и значение BTC dominance из
тикера. OI delta — P3.

### 4.5. News

Текущие источники:
- CoinDesk, CoinTelegraph, Decrypt, The Block (RSS).
- Простой keyword-фильтр по символам.
- Кэш 10 минут, top-8 за 6h.

**Что хорошо:** базовый news-flow есть, и LLM может реагировать на заголовки.

**Что слабо:**
- **Нет FOMC-календаря**. В 2026 macro > 4-year cycle (Bybit, Galaxy) — а у
  нас даты Fed meetings не входят в контекст.
- **Нет Twitter/X sentiment** — это где первой появляется alpha-decay новость.
  (Не критично, но research-стандарт.)
- **Нет on-chain alerts** (Glassnode/Santiment) — крупные перемещения / NUPL
  смена режима.
- **Keyword-фильтры простые**: «bitcoin», «satoshi», «btc». Пропускают
  релевантные новости с упоминанием cap-классов («large-cap selloff») или
  ETF-флоу («IBIT outflows»).

P1 — расширить generic-keywords (etf, ibit, fomc, dxy, dominance). FOMC-календарь
и on-chain — P3.

### 4.6. Риск-параметры

Текущее:
- `risk_per_trade = 5%` от $500 = $25 max risk per trade
- `max_leverage = 5×`
- `max_daily_loss = $125` (25% от капитала)
- `max_total_loss = $500` (100% — фактически terminate)
- 3 одновременные позиции max

**Что говорит research 2026:**
- **Mainstream consensus — 1–2% per trade** [KuCoin Risk Management 2026,
  Atlas Peak Research, Hyper-Quant]. **Position sizing определяет 70–80%
  long-term returns.**
- **Full Kelly опасен** — 65% drawdown от 10-trade losing streak. Practitioners
  используют **fractional Kelly 0.25–0.5**.
- При 5% risk и 5× leverage эффективная позиция = **25% капитала**. Один трейд
  с -8% движением = -2% от капитала. Серия из 3 неудач подряд = -6%, и
  психологически и математически это много.

**Вывод:** наш 5% — **агрессивно даже для эксперимента**. На реальных деньгах
это путь к сливу. **Снизить до 2%** — P1. Это даст $10 max risk per trade,
что лучше для:
- статистики (больше сделок до достижения порога kill-switch),
- эмоций (меньше «бумажных» PnL-скачков),
- consistency с industry standard 2026.

При $10 risk и 1.5 ATR SL → minimum order size может стать барьером (BTC ~$0.001,
при $40k цене это $40, при $10 risk = SL должен быть 25% — слишком тесно).
Поэтому одновременно с понижением risk нужно проверить, что **min order size**
не блокирует ИИ (это и есть `place_order returned None` — высока вероятность).

---

## 5. Action items по приоритетам

### P0 — применить сейчас (bug-fix, не требует sample-size)

| # | Что | Где | Почему |
|---|---|---|---|
| 1 | Привести docstring к коду | `vwap_crypto.py` | ADX≤20 vs ADX_MAX=25 |
| 2 | Обновить правило в соответствии с research 2025 и кодом | `.cursor/rules/strategy-guard.mdc` | Z=2.5 → Z=2.0 для stat-arb |
| 3 | Привести docstring к коду | `crypto_overbought_fader.py` | 13:00–21:00 → 13:00–20:59 |
| 4 | Добавить research-блок в docstring | `funding_scalp.py`, `volume_spike.py` | Каноничный формат |
| 5 | Добавить эти стратегии в правило | `.cursor/rules/strategy-guard.mdc` | Сейчас не задокументированы |
| 6 | Логировать конкретную ошибку Bybit при `place_order is None` | `src/ai_trader/trading/executor.py` | Чтобы понять почему 12 ордеров не прошли |
| 7 | Добавить retry на пустой LLM-ответ | `src/ai_trader/llm/client.py` или `app/main.py` | 8 ошибок «empty response» |

### P1 — ИИ-агент v0.3 (применить с reset n=0, согласовано)

| # | Что | Где |
|---|---|---|
| 8 | Снизить `risk_per_trade` с 5% до 2% | `config/settings.py` + `prompts.py` |
| 9 | Соответственно поправить лимиты (`max_daily_loss=50`, `max_total_loss=200`) | `config/settings.py` + `docker-compose.yml` |
| 10 | Добавить chain-of-thought структуру в промпт (pre-decision checklist) | `prompts.py` |
| 11 | Добавить explicit R:R calculation в промпт (требовать reward ≥ 1.5× risk) | `prompts.py` |
| 12 | Вывести funding rate с band-классификацией в контекст | `trading/context.py` |
| 13 | Добавить BTC dominance в контекст (для альтов) | `trading/context.py` |
| 14 | Расширить generic-keywords (etf, ibit, fomc, dxy, dominance) | `news/rss.py` |
| 15 | Сбросить `ai_trader_data` volume, записать в `BUILDLOG_AI_TRADER.md` | deploy |

### P2 — гипотезы, требуют sample-size (НЕ применять сейчас)

| # | Гипотеза | Что собрать |
|---|---|---|
| 16 | `btc_leadlag.CORR_MIN=0.5` стал слабым после ETF-decoupling | ≥100 сделок, разбивка по бакетам корреляции |
| 17 | `stat_arb_crypto` использует Pearson по ценам, не по returns | Сравнить отбор пар: returns vs prices на исторических данных |
| 18 | `funding_scalp.FUNDING_RATE_THRESHOLD=0.0003` может быть устаревшим | Снять статистику funding на наших символах за 60 дней, посмотреть распределение |

### P3 — long-term, через 1–2 квартала

- On-chain метрики в news (NUPL, MVRV, exchange netflow) через Glassnode API
- FOMC-календарь в news flow
- `funding_scalp` + OI delta condition (Lambda Finance 2026 framework)
- `stat_arb` переход на copula или GHE (Springer 2024 / Computational Economics 2025)
- `volume_spike` + orderflow / DOM-проверка
- ИИ-агент: рассмотреть multi-agent debate (FinDebate / TradingAgents подход)

---

## 6. Что мы НЕ меняем (и почему)

- **Bybit-стратегии не трогаем без sample-size**. Исправления по P0 — это
  только документация (приведение в соответствие с реально работающим кодом).
  Пороги, формулы, фильтры остаются. Правило `sample-size.mdc`: ≥100 сделок,
  ≥2 недели, p<0.05.
- **fx_pro_bot/advisor (FX-стратегии) не трогаем** — отдельная экосистема,
  отдельный аудит, если потребуется.
- **Базовые индикаторы ИИ (RSI/MACD/BB/ATR/EMA) оставляем** — каноны 1978–2001
  всё ещё работают. 2026 community просто **дополняет** их (funding/OI/dominance),
  а не заменяет.

---

## 7. Источники (research-канон 2024–2026)

### Микроструктура и macro

- Reuters, «Crypto exchanges gear up to launch US perpetual futures», 04-2026 — perp objёмы $61.7T в 2025
- Amberdata, «2026 Outlook: The End of the Four-Year Cycle» — macro > halving
- Bybit Outlook 2026 (PRNewswire, 2026) — institutional flows, options sentiment
- LSE 2026: «Bitcoin ETFs and structural decoupling in the cryptocurrency market» — BTC↔alt decoupling
- Coinbase + Glassnode, «Charting Crypto Q1/Q2 2026» — BTC dominance, NUPL, stablecoin flows
- Galaxy Asset Management, March 2026 Commentary

### Funding / OI

- Lambda Finance, «Crypto Funding Rates and Open Interest: April 2026 Snapshot»
- Cryptowisser, «Using Perpetual Futures and Funding Rates to Gauge Market Sentiment», 04-2026
- Glassnode Insights, «Inferring Leveraged Positioning from Price and Open Interest»

### Lead-lag и stat-arb

- Springer Asia-Pacific Financial Markets 2026: «Price Transmission from Bitcoin to Altcoins: High-Frequency Evidence and Implications for Trading Strategy»
- Springer Financial Innovation 2024: «Copula-based trading of cointegrated cryptocurrency Pairs»
- Springer Computational Economics 2025: «Analysis Pairs Trading Strategy Applied to the Cryptocurrency Market»
- GitHub abailey81/Crypto-Statistical-Arbitrage 2025 — Z-score parameters реальных live стратегий

### ORB и сессии

- GrandAlgo 2025: «Opening Range Breakout (ORB) Strategy: Complete Day Trading Guide»
- TakeTrading 2025: «Opening Range Breakout: Rules-First Strategy for Forex and BTC» — UTC midnight ORB

### LLM trading agents

- arXiv 2412.20138 — TradingAgents (Multi-Agent LLM Financial Trading Framework)
- arXiv 2509.17395 — FinDebate (Multi-Agent Collaborative Intelligence)
- arXiv 2602.23330 — Toward Expert Investment Teams (fine-grained task decomposition)
- OpenReview 3NOQZgO29P — ATLAS (Adaptive-OPRO)
- arXiv 2603.22567 — TrustTrade (selective consensus, hallucination suppression)

### Risk management

- KuCoin, «Risk Management Techniques for Futures Trading Leverage in 2026»
- Atlas Peak Research, «The Kelly Criterion in Financial Markets»
- Hyper-Quant, «Kelly Criterion Position Sizing in Volatile Crypto Markets»

### Reality-check

- Kalena 2026: «Crypto Algo Trading Reddit: 7 Best Strategies Tested» — slippage analysis
