# Proposal: AI-Trader → Senior Discretionary Trader (LLM-led)

> **Статус:** DRAFT / PENDING APPROVAL. Не реализовано. Реализация планируется
> в отдельной ветке (например `feat/discretionary-trader`) после подтверждения
> пользователя и наблюдения 24+ часов за текущей v0.12-логикой.
>
> **Дата создания:** 2026-05-12
> **Автор контекста:** запрос пользователя «я хочу создать опытного трейдера
> из LLM, а не бота который у него советуется».

---

## 1. Цель

Сейчас промпт `SYSTEM_PROMPT` представляет собой длинный чек-лист правил
(«Counter-trend ONLY when ALL THREE…», «RSI MUST be ≤25», «need 2+
confirmations», «HOLD is always safe»). LLM по факту играет роль
**бухгалтера**, валидирующего условия — а не **трейдера**, который
взвешивает сетап на основе опыта.

Цель: переложить ответственность за **тактику** на LLM. Код оставляет за
собой только **безопасность капитала** (то, что нельзя нарушить даже на
самом сильном убеждении).

## 2. Принцип разделения: HARD vs SOFT

### HARD — остаётся в коде (capital safety)

| Что | Где сейчас | После изменений |
|---|---|---|
| Max 3 одновременных позиций | `KillSwitch.check_can_open_position` | без изменений |
| Max leverage 5x | `KillSwitch` | без изменений |
| Risk per trade ≤ $10 | `executor._apply_open` qty rounding + reject | без изменений |
| Daily loss limit $50 | `KillSwitch` | без изменений |
| Total loss limit $200 | `KillSwitch` | без изменений |
| Allowed symbols whitelist | `parse_action` | без изменений |
| Direction check (SL/TP) | `executor._apply_open` | без изменений |
| **R:R ≥ 1.5** | **только в промпте (soft)** | **перенести в `executor._apply_open` (hard)** |

R:R — единственное изменение в коде. Если LLM попытается открыть с R:R<1.5,
executor отклонит до отправки на биржу, в БД попадёт запись с error. Это
**математическая невозможность бить рынок с положительным edge** при низком
R:R на длинной выборке — это не «опыт трейдера», это арифметика expectancy.

### SOFT — отдаётся LLM (тактические решения)

| Что | Сейчас в промпте | Цель |
|---|---|---|
| Counter-trend rules | «ONLY when ALL THREE: RSI≤25 + BB touch + news catalyst» | guidance: «counter-trend takes stronger evidence than trend-aligned — your judgement» |
| RSI пороги для входа | «trend-aligned RSI<30/>70, counter-trend RSI≤25/≥75» | информационные лейблы `[OVERSOLD]`/`[EXTREME OVERSOLD]` остаются, но НЕ как rule |
| Need 2+ confirmations | mandatory rule | guidance: «articulate your edge in 1-2 lines; if you can't, hold» |
| Trend confirmation prefer | «prefer trades aligned with 4H trend» | guidance: «4H trend is your default direction; counter-trend needs more conviction» |
| Exit triggers 1/2/3/4 | формальный чек-лист, cite-list обязателен | guidance: «here's how experienced traders think about early exits» |
| Entry quality 2+ confirmations | mandatory | LLM decides |
| «HOLD is always safe» | категорично | оставить как «HOLD is a valid choice», не «is safe» |

## 3. Конкретное изменение в коде

### 3.1 `src/ai_trader/trading/executor.py` — добавить R:R hard-check

Внутри `_apply_open` после direction-check добавить:

```python
# R:R hard-check — capital protection invariant, не trader's discretion.
# При длинной выборке низкий R:R математически не даёт positive expectancy.
risk_dist = abs(price - sl_price)
reward_dist = abs(tp_price - price)
if risk_dist <= 0 or reward_dist / risk_dist < 1.5:
    rr = (reward_dist / risk_dist) if risk_dist > 0 else 0
    return ApplyResult(
        executed=False, summary="",
        error=f"R:R {rr:.2f} < 1.5 hard limit (TP {tp_price}, price {price}, SL {sl_price})",
    )
```

Тест:
```python
class TestRrHardCheck:
    def test_reject_rr_below_1_5(self): ...  # Buy, R:R=1.2 → reject
    def test_accept_rr_exactly_1_5(self): ... # Buy, R:R=1.5 → accept
    def test_reject_rr_below_1_5_sell(self): ... # Sell
```

### 3.2 `src/ai_trader/llm/prompts.py` — переписать `SYSTEM_PROMPT`

Новая структура (черновик):

```
You are a senior discretionary crypto perpetual-futures trader with 10+ years
of experience trading BTC, ETH, BNB, XRP, DOGE. You read price action,
multi-timeframe indicators, funding, and news flow — you don't follow checklists.

INFRASTRUCTURE LIMITS (the bot enforces these — you cannot bypass them):
- Maximum 3 simultaneous open positions.
- Maximum 5x leverage per position.
- Maximum $10 risk per trade (|entry-SL| * qty ≤ $10).
- Maximum $50 daily realised loss / $200 total realised loss.
- R:R must be ≥ 1.5 — if your idea has R:R < 1.5, the bot will reject it;
  return HOLD instead of forcing a bad geometry.
- Allowed pairs: BTCUSDT, ETHUSDT, BNBUSDT, XRPUSDT, DOGEUSDT.

DATA YOU SEE EACH CYCLE:
- 24h price change, funding rate (with band label).
- Last 12 hourly closes, 24h range.
- 1H & 4H indicators: RSI(14), MACD(12/26/9), ATR(14), EMA20/50, Bollinger(20,2).
- Crypto news headlines (when available, RSS, last 6h).
- Your open positions with pre-computed peak_pnl_r / current_pnl_r in R-units.

WHAT WE EXPECT FROM YOU:
- Use the data fully — multi-timeframe alignment, volatility regime, funding
  positioning, news context, R-units on open positions.
- Make decisions like an experienced trader, not by checklist. RSI 32 in a
  bear market isn't extreme — you know that. RSI 28 with a textbook lower BB
  touch and a bullish catalyst is a setup — you know that too. Use judgement.
- Counter-trend trades require more conviction than trend-aligned trades —
  the bar for «I'm fading this move» is higher than «I'm joining this move».
  How much higher is your call.
- Be honest about uncertainty. If you don't see an edge, HOLD. If you see
  one but it's borderline, smaller size or HOLD is fine.

ANALYSIS COMMENTARY (3-7 short lines max):
Briefly cite what you see (trend, volatility, sentiment, open positions
status, the specific setup or invalidation). Don't follow a fixed template —
just convey your read of the market in compact prose.

DECISION JSON (single, last in response):
{ "action": "open", ... } | { "action": "close", ... } | { "action": "hold", ... }
[same schema as before]

REMINDER: this is real money on a 14-day forward-test. Bad trades compound
quickly at 2% risk. Patience and edge come first; activity for activity's
sake is the enemy.
```

Размер: ~50 строк вместо текущих ~280.

### 3.3 `SYSTEM_PROMPT_REVIEW` — аналогично сжать

Сейчас review-prompt тоже жёстко-чек-лист'ный. Цель: «as an experienced
trader, briefly assess each open position — is the original idea still
working? If not, close. If yes, hold. Use peak/current R-units, 1H
indicators and funding as your inputs. No new opens this cycle.»

### 3.4 BUILDLOG запись

Полный BUILDLOG-блок с пунктом «изменение стратегии (не bug-fix)» и
обоснованием — переход к LLM-driven discretion.

## 4. Риски и митигации

| Риск | Мониторинг | Митигация |
|---|---|---|
| LLM торгует чаще и хуже без жёстких правил | Считать open-decisions / sutki, сравнивать WR/PF до и после | Если WR падает >10% при n≥30 — вернуть часть rule-based ограничений |
| LLM игнорирует R:R и спамит low-quality entries | Логировать reject'ы по R:R-hard в БД | hard-reject уже в коде — депозит защищён |
| LLM «галлюцинирует» данные (как было с VWAP/L-S) | Promp clean-up в v0.12 уже исключил это; явно сказано «use ONLY data shown» | + regex-тест на forbidden fragments сохраняется |
| LLM начинает counter-trend «по интуиции» в downtrend | Сравнить % counter-trend trades и их PnL до/после | Если counter-trend WR < trend-aligned WR на ≥15% при n≥30 — вернуть rule про trend-alignment |

## 5. Что **не должно** делаться в Шаге 1

- Не добавлять новые данные (VWAP / OI / L-S / F&G / orderflow) — это **Шаг 2**, отдельный проект.
- Не менять capital limits ($10/$50/$200/3 pos/5x lev).
- Не менять polling intervals (15min full / 5min review).
- Не менять список pairs.
- Не менять model (deepseek-v4-flash).

Цель Шага 1: измерить только эффект **смены стиля промпта** при одинаковом
data-окружении.

## 6. Шаг 2 (позже, отдельная PR)

После 1-2 недель Шага 1 и оценки результата — добавить в контекст реальные
2026-сигналы которых сейчас нет:

- **VWAP** — реализовать в `indicators.py` (rolling 20-bar or session VWAP).
- **L/S ratio** — через `bybit.get_long_short_ratio`.
- **OI delta** — через `bybit.get_open_interest` (24h % change).
- **Funding history** — через `get_funding_rate_history` (текущая ставка vs avg за 24h).
- **F&G index** — через alternative.me API (1 запрос/час).

Это даёт «опытному трейдеру» инструменты институционального уровня которые
он сейчас не имеет.

## 7. Реализация (когда даст добро)

Чек-лист порядка действий:

- [ ] Создать ветку `feat/discretionary-trader` от текущей main (post-v0.12).
- [ ] `executor.py`: добавить R:R hard-check + 3 теста.
- [ ] `prompts.py`: переписать `SYSTEM_PROMPT` (~50 строк) и
      `SYSTEM_PROMPT_REVIEW` (~30 строк).
- [ ] Обновить `build_user_prompt` и `build_user_prompt_review` если
      изменится структура commentary.
- [ ] Обновить существующие тесты которые проверяют конкретные фразы
      «MUST / ONLY / ALL THREE» — заменить на новые ожидания (что промпт
      содержит «infrastructure limits», «senior discretionary», «R:R must
      be ≥ 1.5», и т.д.).
- [ ] Удалить regex-тесты на жёсткие фразы; оставить regex-тест на
      forbidden fragments (VWAP/F&G/OI/etc — не должны вернуться).
- [ ] BUILDLOG_AI_TRADER.md: запись v0.13 + явное предупреждение о
      smene strategy.
- [ ] Локальный pytest зелёный.
- [ ] Деплой в ветку через `--no-deps --build ai-trader` НЕ на VPS-main,
      а только локально / в отдельный контейнер если есть. ИЛИ
      merge в main + наблюдение.
- [ ] Метрики до/после: open/cycle, close-early/cycle, WR, PF, средний R.

## 8. Как откатиться

Так как это изменение стратегии (не bug-fix), фиксируем baseline до v0.13
и держим возможность revert:

- Создать тег `pre-discretionary-v0.12` на текущем main коммите
  (`af8de1c` после v0.12).
- При плохих результатах: `git revert <merge-commit>` или
  `git reset --hard pre-discretionary-v0.12`.

---

## Приложение A. Текущая статистика для baseline

На момент создания этого документа (после v0.12 deploy 2026-05-12 ~16:00 UTC):

- **49 последних open-decisions** (период 2026-05-07 → 2026-05-12):
  - ~28 опирались на галлюцинированные signals (VWAP / retail L-S / OI /
    F&G / orderbook) — этих данных в контексте никогда не было.
  - ~6 trend-aligned (4H в направлении entry).
  - ~3 counter-trend с реальным RSI extreme + BB touch.
  - ~12 counter-trend с RSI 26-34 («near oversold») — слабый сетап.

- **Open positions @ deploy**: 1 (XRPUSDT id=новый, after id=58 −$4.51 closed).

Эти числа — точка отсчёта для оценки Шага 1.

## Приложение B. Цитаты пользователя (мотивация)

- «у меня есть ощущение что бот заходит в позицию с неактуальными данными».
- «нужно изучить нашего бота и всю его логику, у меня есть ощущение что где
  то есть противоречия которые вводят бота в заблуждения».
- «я хочу создать опытного трейдера из LLM а не бота который у него
  советуется».
- «давай посмотрим что будет делать с текущими правками 24 часа».
