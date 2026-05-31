# Build Log — scalp_bot (orderflow-скальпер Bybit)

Лог нового бота `src/scalp_bot/`. Изолирован от `ai_trader`/`bybit_bot`/
`fx_pro_bot`/`fx_ai_trader` (strategy-guard.mdc). Решения детерминированные,
по микроструктуре в реалтайме, БЕЗ LLM.

## 2026-05-31

### v0.9.3 — HTF-bias: фейд только по тренду EMA200 1H (контекст, аудит №4)
`<hash>`

**Контекст (находка аудита №4).** Канон CAP: «без структурного контекста
CVD-дивергенция — шум» (gates 1–3: HTF-структура/сессия). sweep_fade фейдил
микроструктуру В ВАКУУМЕ — ни HTF-bias, ни сессии, ни VWAP-локации. Вероятная
причина низкого WR (29–40%): часть фейдов — ловля ножа против старшего тренда.

**Правка (одобрено пользователем 2026-05-31, вариант «htf», дизайн согласован).**
Трендовый фильтр старшего ТФ: EMA200 на 1H (Bybit `get_kline` interval=60,
refresh раз в `htf_refresh_sec`=300с, кэш per-symbol). Правило (trend-aligned
fade): long-fade только при `price > EMA200_1h`, short — только ниже; контртренд
блокируем. Гейт в main-loop ПОСЛЕ `resolve(sig)` (HTF — market-regime фильтр,
общий для всех стратегий, как killswitch/funding-window), детектор не трогаем.
**Fail-open**: нет HTF-данных (REST-сбой / новый листинг < ema_len свечей) →
НЕ блокируем (сбой свечей не должен глушить торговлю).

**Research:** Murphy 1999 (EMA200 = primary trend filter; уже канон в fx_pro_bot
strategy-guard как блокирующий H1-фильтр); Asness et al. 2013 «Value & Momentum
Everywhere» (mean-reversion в согласии с трендом, не против); chartwhisperer CAP
(структурный контекст до входа).

**Влияние:** режет частоту (контртренд-фейды ~половина) → должен поднять WR и
убрать худшие ловли ножа. Подтвердим live forward-тестом (≥100 сделок,
sample-size.mdc).

**Файлы:** `data/htf.py` (NEW — `HtfTrend`, `compute_ema`, fail-open),
`trading/client.py` (`get_kline`), `config/settings.py` (4 поля HTF),
`app/main.py` (инстанс+refresh+гейт после resolve), `docker-compose.yml` (4 env),
`tests/test_scalp_bot.py` (+5 тестов HTF; 111 passed, полный набор 1034 passed).

### v0.9.2 — flow_scratch: порог глубины 0.7R вместо hair-trigger «≥комиссии»
`<hash>`

**Проблема (данные, 60 свежих сделок, risk≈$1).** flow_scratch — главный
кровосос: **24 из 60 входов (40%), все в минус, −$12.31**. Резал при ходе против
медианно **−0.29R** (далеко от SL −1R), а реализовывал **−0.56R** — разницу 0.27R
съедала комиссия. Условие срабатывания было `минус ≥ round-trip комиссии`
(≈−0.25R) + любой флип ленты + 20с → hair-trigger: убивал сделки на шумовой
просадке, не дав развиться. 75% скретчей (по всей истории) — при ходе всего
0–0.5R против.

**Асимметрия (корень).** flow_exit мы подняли до ≥1R (анти-клиппинг вин), а
flow_scratch резал при ≥0.25R минуса — мелкий профит держим, мелкий минус рубим
на шуме + платим комиссию. Это душило «Философию B» (дай развиться).

**Правка (одобрено пользователем 2026-05-31, вариант «0.7R»).** Добавлен
`scratch_min_adverse_r` (default **0.7**): скретчим только когда сделка реально
в минусе ≥ 0.7R × R И лента развернулась И созрела. Симметрично анти-клиппингу
flow_exit. Мелкий минус на шумовом флипе ДЕРЖИМ (уйдёт в безубыточный time_stop
medR +0.04 или восстановится), режем лишь реально ломающиеся сделки до полного
SL. С min_risk_fee_mult=4 (fee≈0.25R) порог 0.7R заведомо выше комиссии. Не
подгонка под P&L: устранение hair-trigger по механике (fee-gap) + симметрия.

**Файлы:** `config/settings.py` (`scratch_min_adverse_r`),
`analysis/strategies.py` (порог глубины в should_exit + docstring,
убран неиспользуемый fee_px), `docker-compose.yml` (env),
`tests/test_scalp_bot.py` (+2 теста: держим −0.3R, режем −0.8R; обновлены 2;
106 passed).

**Sample-size disclaimer.** Окно ~3ч/60 сделок (один режим). Но проблема
МЕХАНИЧЕСКАЯ (hair-trigger + fee-gap 0.27R) и совпадает с подтверждённым выводом
v0.8.1 — не оптимизация под выборку (no-data-fitting.mdc, sample-size.mdc).

### v0.9.1 — density: rolling-baseline стены (Kalena 10–15мин вместо мгновенного)
`<hash>`

**Проблема (данные).** density_bounce/density_break = **0 из 502 входов** за всю
историю. Корень (диагностика на живых книгах, BUILDLOG v0.8.3): `_baseline_avg`
берёт знаменатель из **мгновенного** top-25 стакана, где самый крупный уровень
лишь 2–4× среднего → порог 5× недостижим. Research Kalena меряет стену против
среднего размера уровня **за 10–15 мин**, а не мгновенного — это и был расхождение
реализации с источником.

**Правка (одобрено пользователем 2026-05-31, вариант «fix»).** Добавлен
`RollingBaseline` — скользящее среднее per-snapshot baseline за
`density_baseline_sec` (900с = верх research-окна). `detect_wall`/`_wall_in_range`
получили опциональный параметр `baseline`: если передан скользящий — сравниваем
стену с ним; пока не накоплено `density_baseline_min_samples` (30) — fallback на
мгновенный (warmup-совместимость). Обе density-стратегии кормят baseline каждый
тик в `update`/`should_exit`. Это каноничный знаменатель Kalena, не подгонка порога.

**Честно про ожидания (no-data-fitting).** Глубину стакана исторически бэктестить
нельзя — частоту входов после правки подтвердим только live (forward-test). Не
обещаю, что страты «оживут»: time-windowed baseline даёт research-каноничный
знаменатель, но если на текущих монетах реально нет аномальных стен — входов будет
мало. Это первый корректный шаг, не гарантия частоты.

**Файлы:** `analysis/strategies.py` (`RollingBaseline`, baseline-параметр в
detect_wall/_wall_in_range, аккумуляторы в обеих density-стратегиях + research-
докстринги), `config/settings.py` (2 поля), `docker-compose.yml` (2 env),
`tests/test_scalp_bot.py` (+3 теста: RollingBaseline окно/warmup/non-positive,
detect_wall с явным baseline; 104 passed, полный набор пройдёт перед коммитом).

### v0.9.0 — аудит sweep_fade: удаление factor-noise и мёртвого пути
`<hash>`

**Контекст.** Пользователь попросил аудит основной стратегии против канонов
скальпинга — найти лишнюю/вредную логику, которую «натянули/перепридумали».
Перечитал весь код входа + освежил каноны (chartwhisperer CAP, Kalena DOM,
traderssecondbrain «confluence math»).

**Канон (источники).** CAP-сигнал = свип + CVD-дивергенция + reclaim (CHoCH);
funding/ликвидации в гейтах CAP ОТСУТСТВУЮТ. «HF-скальпинг выигрывает на 2
факторах; 5+ конфлюенсов систематически недобирают» (traderssecondbrain 2026).
Аудит-правило канона: «убери фактор по одному — если WR/прибыль не падает, он
был шумом».

**Данные (502 входа из БД, ground-truth по reasons).** Все 502 входа —
sweep_fade. Распределение reasons:
- `sweep+cvd_div+reclaim+mom` — 361 (72%, чистое ядро без бонусов);
- `+ob_imb` — 138 (27%);
- `+liq` — **1 (0.2%)**; `+funding` — **1 (0.2%)**.
→ funding и liq как факторы входа — **мёртвый декор** (присутствие 0.2%), не
гейтят и не каноничны для разворота на 90–120с (funding — 8ч-метрика).

**Находки и правки (рефактор, поведение ЖИВОГО входа не меняется):**
1. **Убраны funding/liq как факторы входа** — из фазы ВЫСТРЕЛ детектора, из
   funnel (`_FUNNEL_RULES`), из `diagnose`. Удалены функции `funding_supportive`,
   `liq_flush` и поля `funding_extreme_pos/neg`, `liq_flush_usd`. liq_events
   продолжаем собирать только для heartbeat-наблюдаемости.
2. **Удалён мёртвый legacy-путь** `evaluate()` / `_evaluate_side()` /
   `min_confluence`: живой путь — только `SweepReclaimDetector.update`, а
   evaluate жил лишь в `diagnose`+тестах. Docstring `signals.py`/`main.py`
   описывал «≥min_confluence из 5» — это поведение МЁРТВОГО пути (документация
   рассинхронилась с детектором). `diagnose` переписан на фазы детектора
   (sweep/div/reclaim/momentum/ob), без legacy-скоринга.

**Что НЕ тронуто (каноничное ядро).** Свип-детект, обязательная CVD-дивергенция,
reclaim, momentum-подтверждение, SL за свипом+буфер, fee-guard, мин-R пол,
риск-сайзинг, flow_exit, killswitch — всё совпадает с CAP/канон.

**Не сделано в этой правке (вынесено на отдельные решения):**
- density_bounce/density_break = 0/502 входов — кандидат на disable-до-фикса
  (rolling-baseline) либо фикс. Решение за пользователем.
- Отсутствие HTF-bias / сессии / VWAP-локации — канонический пробел КОНТЕКСТА
  («без контекста CVD-дивергенция — шум», CAP). Кандидат на эксперимент с данными.
- flow_scratch hair-trigger (отдельный разбор) — ждёт выбора порога.

**Файлы:** `analysis/signals.py` (−funding/liq/evaluate/_evaluate_side, docstring,
diagnose-рерайт), `config/settings.py` (−3 поля), `app/main.py` (docstring,
startup-лог, `_FUNNEL_RULES`), `docker-compose.yml` (−3 env),
`tests/test_scalp_bot.py` (−5 legacy-тестов, +2 build_signal/diagnose; 101 passed,
полный набор 1024 passed).

**Sample-size disclaimer.** Удаление factor-noise обосновано присутствием 0.2%
на n=502 + каноном (CAP не использует funding/liq), а не оптимизацией под P&L
(no-data-fitting.mdc). Поведение живых входов не меняется (factors не гейтили).

### v0.8.3 — density wall 8×→5× + round-band 0.1%→0.3% (страты не срабатывали)
`<hash>`

**Симптом.** Пользователь: «не вижу новую страту в логах». Проверка: density_bounce
и density_break — **0 сделок за всю историю** (488 — все sweep_fade), в логах лишь
стартовая регистрация. Страты крутятся, но молчат.

**Диагностика (живые данные).** Достал стаканы по ALLO/XLM/BNB/NEAR/ZEC (REST
`get_orderbook` limit=50, top-25 как в проде) и прогнал `detect_wall`:
самый крупный уровень = **2.1–3.6× среднего**, порог `density_wall_mult=8.0`
НЕДОСТИЖИМ → стена не находится → страты не «взводятся». Плюс `near_round=False`
везде (`density_round_frac=0.1%` — слишком жёсткий гейт).

**Причина.** Порог 8× — консервативный край research-диапазона Kalena (5–8×). На
живых книгах Bybit (top-25, мгновенный baseline) такие стены не встречаются. Плюс
несоответствие реализации источнику: Kalena меряет стену vs среднее за 10–15 мин,
у нас — vs мгновенный top-25 → ratio структурно занижен.

**Правка (одобрено 2026-05-31).**
- `density_wall_mult` 8.0→**5.0** — НИЖНИЙ край диапазона Kalena «5–8×» (остаёмся
  в research-каноне, не подгонка под 3×). Честно: на текущих книгах макс 3.6×, так
  что и при 5× срабатывания будут РЕДКИМИ — это первый шаг, не гарантия частоты.
- `density_round_frac` 0.001→**0.003** (0.1%→0.3%) — гейт «стена у круглого числа»
  смягчён (был блокирующе тугим, near_round=False везде). Остаётся гейтом.
- Rolling-baseline (10–15мин avg как в research) — будущий редизайн, отмечено.

**Файлы:** `config/settings.py` (2 порога + research-комменты),
`analysis/strategies.py` (research-блок DensityBounce docstring),
`docker-compose.yml` (2 env), тесты не трогали (density-хелпер передаёт пороги
явно, проверяет логику; 110 passed).

### v0.8.2 — recalibration вселенной: turnover 150→100M + пин-механизм (ALLO)
`<hash>`

**Симптом (диагностика на живых данных).** Селектор пропускал ВСЕГО 2 монеты
(XLM, BNB) — бот застрял на них. Запрос `get_tickers` (678 тикеров) + прогон
`rank_universe`: рабочие альты не проходили **turnover-floor $150M**:
- NEAR: range 9.8%, turnover **$137M**, spread 0.4bps → FAIL (turn);
- ZEC: range 8.3%, turnover **$125M**, spread 0.2bps → FAIL (turn);
- ALLO: range **42.1%** (>30 cap), turnover $76M → FAIL (pump + turn).

**Причина.** Floor $150M ставился (BUILDLOG 2026-05-30), чтобы держать рабочие
монеты, у которых тогда был оборот $248–799M. Рынок просел ~2× → тот же floor
стал выкидывать ровно эти монеты. Turnover — грубый прокси; реальный страж
ликвидности скальпа = **spread cap (5bps)**, а у NEAR/ZEC спред 0.2–0.4bps
(тоньше BNB 1.4).

**Правка (одобрено 2026-05-31).**
1. **`universe_min_turnover_usd` 150M→100M** — возвращает NEAR/ZEC-класс; ALLO
   и прочие пампы по-прежнему отсекает range-cap 30%. Не подгонка под P&L:
   возврат floor его исходного смысла на сдвинувшемся рынке (spread — главный
   страж). Range-cap 30% оставлен (анти-памп, выбор пользователя).
2. **Пин-механизм** (`universe_pin_symbols=ALLOUSDT`, `apply_pins`): force-include
   в ОБХОД фильтра — пользователь передумал и захотел ALLO обратно. Это
   осознанный риск памп-н-дампа на КОНКРЕТНОЙ монете, не общее ослабление
   фильтра. Риск-сайзинг (v0.8.1) частично страхует: широкий range ALLO →
   большой R → малый лот.

**Файлы:** `config/settings.py` (`universe_min_turnover_usd`,
`universe_pin_symbols` + `universe_pin_list`), `data/universe.py` (`apply_pins`
+ docstring), `app/main.py` (`_select_universe` применяет пины),
`docker-compose.yml` (2 env), `tests/test_scalp_bot.py` (+3 теста, 110 passed).

### v0.8.1 — мин-R пол (fee ≤ 0.25R) + риск-базированный сайзинг
`<hash>`

**Проблема (анализ, не интуиция).** Разбор 31 `flow_scratch` (БД, PnL по WS):
скретч режет РАНО (медиана 0.41R, только 3% у самого SL) и дешевле полного SL
(−0.27 vs −0.45), т.е. работает верно. НО R-нормированный лосс = **−0.74R при
adverse-ходе всего 0.41R** → разницу (~0.33R) съедает **комиссия**. Причина: SL
тугой (R≈0.13% цены), а round-trip fee ≈0.11% ≈ **0.4–0.8R**. Это убивало
асимметрию TP=3.5R (v0.7.0): чтобы 3.5R окупил серию, надо ещё перекрыть
fee-драг на каждой сделке.

**Проф-подтверждение (запрошено пользователем перед правкой):**
- Стоп скальпинга = «структура + ATR-буфер», 0.8–1.5× ATR за свингом; общий
  канон Уайлдера «2 ATR, чтобы не выбило шумом»
  ([cryptotrading-guide 2026](https://cryptotrading-guide.com/best-atr-stop-loss-for-crypto-2026-the-complete-guide-to-atr-multipliers-placement-risk/),
  [VT Markets](https://www.vtmarkets.com/discover/atr-for-traders-how-to-set-smarter-stops-targets-position-size/)).
- Цель скальпа 0.5–2% ([stoic.ai 2026](https://stoic.ai/blog/best-cryptocurrencies-for-day-trading-complete-guide-to-top-trading-opportunities/));
  наш TP 3.5R≈0.47% был НИЖЕ коридора.
- Издержки съедают 50–80% профита скальпера; при тугом стопе fee 3–5%/round-trip
  ([Echo Zero 2026](https://blog.echozero.app/article/scalping-strategy-performance-in-high-frequency-crypto-markets)).
- Канон: «стоп с графика, РАЗМЕР — следствие: size = risk$ ÷ stop distance»
  ([TradeOlogy](https://academy.tradeology.app/risk-psychology/position-sizing-deep-dive),
  DYOR, StockCharts 2026).

**Правка (одобрено 2026-05-31: k=4, риск-сайзинг, $1/сделку).**
1. **Мин-R пол** (`min_risk_fee_mult=4.0`): `R ≥ 4 × round_trip_fee` →
   fee ≤ 0.25R (R≈**0.44%**, TP 3.5R≈**1.55%** — центр коридора 0.5–2%). Если
   структурный R меньше пола — SL отодвигается ЗА свип-уровень (`build_signal`),
   TP пересчитывается от итогового R.
2. **Риск-сайзинг** (`risk_based_sizing=True`, `risk_per_trade_usd=1.0`):
   `qty = risk$ ÷ |entry−SL|` (`position_size_by_risk`). Широкий стоп НЕ растит
   $-риск, лишь уменьшает лот. При R≈0.44% и риске $1 notional≈$227 (в пределах
   killswitch $500/день и 2 одновременных позиций). Legacy фикс-notional —
   `risk_based_sizing=False`.

**Почему не подгонка под выборку (`no-data-fitting`/`sample-size`).** n=31/4ч мало
для P&L-вывода, и правка обоснована СТРУКТУРНО (fee-математика + проф-канон), а
не оптимизацией под прошлый P&L. P&L-эффект проверить forward-тестом на ≥100
сделок (ждём накопления).

**Файлы:** `config/settings.py` (`min_risk_fee_mult`, `risk_based_sizing`,
`risk_per_trade_usd`), `analysis/signals.py` (мин-R пол в `build_signal`),
`trading/executor.py` (`position_size_by_risk` + ветка в `on_signal` + docstring),
`docker-compose.yml` (3 env), `tests/test_scalp_bot.py` (+6 тестов, 107 passed).

### v0.7.1 — анти-клиппинг flow_exit: профит-лок только после ≥1R
`<hash>`

**Источник правды (анализ, не интуиция).** Разбор 427 live-сделок из БД
(`/data/scalp_bot.sqlite`, PnL сверен по приватному WS = ground truth; запрос:
group by `close_reason`, распределение плюсовых `flow_exit`). Запрос-скрипт
одноразовый через `docker exec` (как `/tmp/scalp_stats*.py` в v0.6.0).

**Что показали данные:**
- `flow_exit`: n=110, **WR 72%, единственный плюсовой (+$5.26)**, но **79 вин с
  медианой ~$0.04** — распределение: ~60 из 79 ≤ $0.07, и лишь 2 крупных (1.43,
  1.48). avgWin flow_exit = **+$0.101**.
- `tp_sl` (добежавшие до биржевого TP): avgWin **+$0.387 — в 4× крупнее**.
- `time_stop`: n=234, WR 15%, −$29.37 (мёртвые сделки).
- **Диагноз:** flow_exit фиксировал в тот же миг, как профит покрывал комиссию
  (`favorable ≥ round_trip_fee` ≈ 0.11%) → клипал центы. После v0.7.0 (TP→3.5R)
  это **обнуляло** правку: сделка не доживала до 3.5R, её срезал копеечный лок.

**Правка (одобрено пользователем 2026-05-31, вариант «≥1R»).** Порог профит-лока
`flow_exit` поднят с «≥ round-trip комиссии» до **≥ `flow_exit_activate_r` × R**
(R = |entry−sl|, default 1.0R). Логика `SweepFadeStrategy.should_exit`: по
развороту ленты фиксируем ТОЛЬКО когда набрано ≥1R прибыли; ниже — ДЕРЖИМ (даём
добежать к TP=3.5R), НЕ клипаем. `flow_scratch` (срез убытка) и hard SL — без
изменений. Это даёт правке v0.7.0 (3.5R) реально проявиться: победитель бежит,
а проверенный механизм лока по развороту остаётся (поймал 1.43/1.48).

**flow_exit НЕ удалён** (требование пользователя + данные: он единственный
плюсовой и спасает сделки от гниения в time_stop, WR 72% vs 15%). Поднят только
порог активации.

**Файлы:** `config/settings.py` (`flow_exit_activate_r`),
`analysis/strategies.py` (профит-лок по ≥1R + docstring),
`docker-compose.yml` (`SCALP_FLOW_EXIT_ACTIVATE_R`),
`tests/test_scalp_bot.py` (flow-тесты под 1R + анти-клип; 101 passed).

**Sample-size disclaimer.** Данные — почти целиком старый режим (TP 1.5R /
time_stop 90с, hold~91с); новые параметры крутятся ~30 мин. НО клиппинг — это
**артефакт порога** (его не подняли вместе с TP в v0.7.0), а не свойство режима;
выборка ≥100. Эффект пере-проверить forward-тестом на свежей выборке.

### v0.8.0 — стратегия №3 `density_break`: пробой на сносе плотности («прострел»)
`<hash>`

**Запрос пользователя.** По ролику Руслана Данилова
([YouTube «Разгон депозита» 2026](https://www.youtube.com/watch?v=YWLjzc0A3k4),
+ ранее [«Все рабочие стратегии»](https://www.youtube.com/watch?v=HOwqznGsX88)):
сделать ещё одну стратегию. Ключевая идея, которой у нас НЕ было: плотность,
которая ДЕРЖАЛА цену, при пробое даёт «прострел» — *«если его снимут, прострел
будет хороший»*, *«стопы за плотностью выбивают + крупный игрок → импульс»*.

**Что это.** Третья независимая стратегия поверх мультистратегийного каркаса —
**зеркало `density_bounce`** (momentum/breakout, ПРОТИВОПОЛОЖНА fade):
- `density_bounce` (есть): стена держит → fade В стену (отскок).
- `density_break` (новая): выстоявшая стена ПРОБИТА → вход ПО ХОДУ пробоя.

**Логика (на символ).**
1. Наблюдаем крупную стену у круглого числа (`detect_wall` + `near_round`).
2. Стена «выстояла» (`persisted`), если продержалась ≥ `density_persist_sec`
   (10с) — **анти-спуфинг**: мелькнувшая <persist стена = спуфинг, НЕ сигнал.
3. Стена исчезла с уровня И цена ПРОБИЛА его по ходу:
   ask-стена (сопротивление) пробита вверх → **LONG**; bid-стена (поддержка)
   пробита вниз → **SHORT**. SL за пробитым уровнем (`build_signal` swept=
   цена_стены: ложный пробой = возврат за уровень), TP по R + общий fee-guard.
4. Снос БЕЗ пробоя цены (спуфинг-пулл) — v1 НЕ торгуем (нет подтверждения
   пересечением уровня).
5. Выход (`should_exit`) v1 — только общие TP/SL/тайм-стоп (ложный пробой режет
   hard SL). Flow-based выход — отдельная итерация после валидации базового эджа
   (no-data-fitting.mdc: не наслаивать непроверенные эвристики выхода).

**Research basis** (strategy-guard.mdc, в docstring `DensityBreakStrategy`):
Данилов YouTube 2026 (снос плотности → прострел); Bookmap «liquidity void»;
Kalena 2026 wall-detection (removal/absorption); arXiv 2604.20949 (depth раньше
flow). Параметры ПЕРЕИСПОЛЬЗУЮТ `density_*` (wall_mult 8×, persist 10с, round
0.1%) — новых порогов нет, не подгонка.

**Конфликт со sweep_fade/density_bounce.** Стратегии независимы; разные
направления по символу в один тик → `resolve` пропускает тик (гард из Фазы 1).
density_break (пробой) и density_bounce (отскок) триггерятся РАЗНЫМИ событиями
(стена держит vs стена снесена) — одновременно по одной стене не сработают.

**Включение.** `enabled_strategies` (CSV). settings.py дефолт →
`sweep_fade,density_bounce,density_break`; compose `SCALP_ENABLED_STRATEGIES`
(дефолт те же три, реальное значение из VPS `.env`). На VPS `.env` дополнен
`density_break`.

**Файлы:** `analysis/strategies.py` (`DensityBreakStrategy` + регистрация в
`build_strategies` + docstring модуля), `config/settings.py` (enabled default),
`docker-compose.yml` (`SCALP_ENABLED_STRATEGIES`), `tests/test_scalp_bot.py`
(+5: fire long/short, спуфинг, нет пробоя, регистрация; 100 passed).

**Sample-size disclaimer.** Новая стратегия → выводы по WR/PnL ТОЛЬКО после ≥100
сделок по связке `density_break × инструмент` (sample-size.mdc). Сейчас — запуск
на demo для forward-валидации (orderflow не бэктестится). Постратегийная стата в
heartbeat (`stats_by_strategy`) уже разводит метрики по стратегиям.

### v0.7.0 — Философия B: дай победителю бежать (ob_imb→бонус, TP 3.5R, time_stop 120с)
`<hash>`

**Запрос/контекст.** Пользователь: первая итерация бота имела WR всего ~26–29%,
НО была прибыльной — за счёт редких КРУПНЫХ вин (~$19), перекрывавших серию
мелких минусов (≤$2). Наши последующие правки (особенно обязательный `ob_imb` в
v0.6.0) задушили именно этот асимметричный payoff: подняли WR 29%→40%, но отсекли
«жирные» вины (>$0.5). Вывод (анализ 430 сделок + research проф-скальперов):
для fade-входа с WR ~29% выживание идёт НЕ через рост WR (нужен ~58% — нереально),
а через **асимметрию** — дать победителю добежать. Явная директива пользователя:
**снести `ob_imb` (он мешает), `flow_exit` НЕ трогать** (это проверенный плюс).

**Сверка с первоначальной стратегией (перед правками).** Ядро ВХОДА —
двухфазный `SweepReclaimDetector` (ВЗВОД: свип+CVD-дивергенция → ВЫСТРЕЛ:
reclaim≥50%+разворот ленты). НЕ ТРОНУТО ни одной правкой. ob/liq/funding в
исходном дизайне v0.3.1 были БОНУСОМ — гейт `ob_imb` мы добавили лишь в v0.6.0,
так что его снятие = **возврат к оригиналу**, а не новая ломка.

**Правки (одобрено пользователем 2026-05-31):**
1. **`ob_imb` → снова бонус** (`require_ob_imbalance` True→False). Гейт в фазе
   ВЫСТРЕЛ снят: reclaim+разворот достаточно для входа, стакан лишь добавляет
   очко в reasons (как в v0.3.1). Больше «жирных» сетапов проходят.
2. **TP 2.0→3.5R** (`take_profit_r`). Дай победителю бежать; 3.5R в каноне
   свип-разворота (CrossTrade 2:1–4:1). Если поток держит — сделка идёт к 3.5R;
   если развернулся — `flow_exit` фиксирует накопленное (механизм НЕ тронут).
3. **time_stop 60→120с** (`time_stop_sec`). 60с резали ради ограничения
   time_stop-убытка (86% потерь, анализ 304 сделок v0.6.0), но ранний срез
   УБЫТОЧНЫХ теперь делает `flow_scratch` (≥20с при развороте ленты, v0.6.0) —
   тугой бэкстоп не нужен как защита от убытка, он лишь душил РЕДКИЕ крупные
   вины (медиана выхода победителя 55–67с, до 3.5R нужно больше времени).

**Что НЕ тронуто (по явному требованию + защита ядра):** вход (детектор,
CVD-дивергенция, reclaim, SL за свипом, fee-guard), `flow_exit` (профит-лок по
развороту ленты), `flow_scratch` (ранний срез убытка), hard SL, killswitch.

**🔴 Сопутствующий фикс (важно для трактовки прошлой статы).** В
`docker-compose.yml` дефолты `SCALP_TIME_STOP_SEC=90` и `SCALP_TAKE_PROFIT_R=1.5`
были УСТАРЕВШИЕ и (т.к. VPS `.env` их не задаёт) **перебивали settings.py через
env** — pydantic читает env поверх дефолта поля. Значит ЖИВОЙ бот реально крутил
**time_stop=90с и TP=1.5R**, а правки settings.py v0.3.0 (TP→2.0) и v0.6.0
(time_stop→60) на VPS НИКОГДА не применялись. Подтверждение: анализ 304 сделок
показал «висели до стенки 91с» (= 90с, не 60с). Сейчас синхронизировано:
compose 120/3.5 + добавлен `SCALP_REQUIRE_OB_IMBALANCE=false`. Т.е. фактический
переход на VPS: time_stop 90→120с, TP 1.5→3.5R, ob_imb on→off.

**Файлы:** `config/settings.py` (require_ob_imbalance False, take_profit_r 3.5,
time_stop_sec 120 + research-комментарии), `analysis/signals.py` (комментарий
ob-гейта: бонус по умолчанию), `docker-compose.yml` (синк дефолтов + ob-флаг),
`tests/test_scalp_bot.py` (_cfg дефолт require_ob_imbalance=False; 95 passed).

**Sample-size disclaimer (sample-size.mdc / no-data-fitting.mdc).** Правки —
изменение параметров выхода/фильтра, обоснованы анализом 430 сделок + research,
НЕ подгонка под последние сделки. time_stop/TP меняем по гипотезе асимметрии;
эффект пере-проверить forward-тестом на свежей выборке после деплоя.

### v0.6.1 — селектор вселенной: качество-не-количество + композит + 5 мин
`<hash>`

**Запрос пользователя.** (1) Брать ВСЕ монеты, прошедшие критерии, а не
фиксированные 5 (подошло 5 — берём 5, подошло 2 — берём 2). (2) Улучшить формулу
отбора как у проф-скальперов. (3) Искать чаще.

**Research (отбор монет у проф day-trade/scalp).** Volity «5-filter framework»,
stoic.ai, dev.to trendrider 2026: ликвидность (24h vol floor) и волатильность
(ATR% sweet-spot) — co-equal; спред — «скрытая комиссия», съедающая edge на
каждом round-trip; RVOL и корреляция к BTC — продвинутые фильтры (future,
нужны intraday-данные).

**Правки:**
1. **Качество, не количество.** `rank_universe` теперь возвращает ВСЕ прошедшие
   hard-фильтр символы; `universe_top_n` (5→15) — лишь safety-кап на число
   WS-подписок (`≤0` = без лимита). Количество определяют фильтры.
2. **Композитное ранжирование** вместо «sort by range%». Было: биас в самые
   «горячие» (рискованные), ликвидность — только tie-break. Стало:
   `score = 0.45·vol_n + 0.45·liq_n + 0.10·(1−spread_n)` (min-max нормировка
   внутри прошедшего фильтр пула). Эффект: ликвидная монета с хорошей (не макс.)
   волатильностью обходит «тонкую» гипер-волатильную → меньше слиппедж/стоп-аутов.
   Hard-фильтры (turnover≥$150M, range 6–30%, spread≤5bps) без изменений.
3. **Refresh 30→5 мин.** Ротация — no-op при неизменном составе
   (`_rotate_universe`), метрики 24-часовые (медленные) → частый refresh почти
   всегда дешёвый `get_tickers` без WS-рестарта. Ниже ~5 мин на 24h-метриках
   новой информации нет (для непрерывного поиска нужны intraday/RVOL — future).

**Файлы:** `data/universe.py` (композит-скор `W_VOL/W_LIQ/W_SPREAD`, `_norm`,
`top_n≤0`=без лимита), `config/settings.py` (top_n 15, refresh 300),
`docker-compose.yml` (дефолты), `tests/test_scalp_bot.py` (+2: композит,
top_n=0; 95 passed).

### v0.6.0 — починка выхода (scratch) + строгий вход (ob_imb обязателен)
`<hash>`

**Источник правды (анализ, не интуиция).** Полный разбор 304 закрытых сделок с
момента старта контейнера 2026-05-30 11:14 UTC (БД `scalp_bot.sqlite`, PnL
reconciled из приватного WS = ground truth). Скрипты разбора:
`/tmp/scalp_stats.py`, `/tmp/scalp_stats2.py` (одноразовые, через `docker exec`).

**Что показали данные (net −$32.57, WR 32%, avg −$0.107/сделку):**
- `time_stop` = **186 сделок (61%), net −$24.18 = 86% всего убытка**. Все висели
  до стенки 91с; 160 убыточных (ср. −$0.167), лишь 26 плюсовых.
- `flow_exit` = **единственный плюс: 69 сделок, WR 84%, +$3.16**, ср. хват 55с.
  → наш реальный эдж = выход по развороту ленты, когда он подтверждён.
- `tp_sl` net −$11.55: SL ловится 36× (−$0.467) против TP 13× (+$0.36) —
  R:R реализуется плохо.
- Победители решаются БЫСТРО (медиана хвата 67с), убытки ТЯНУТСЯ (медиана 91с).
- Вход с `ob_imb`: WR 40% (n=67, −$4.48); без него: WR 29% (n=240, −$29).
- Long/short льют одинаково (33%/30%) → дело не в направлении.

**Research-консенсус проф-скальперов крипты (order-flow школа 2026):**
- Kalena (DOM scalping, ×2 статьи), TradeZella, LedgerMind: правило ~30с
  shot-clock, *«exit if wrong immediately when order flow flips»*, «не давай
  скальпу стать свингом» (хват 10–90с).
- Order-book imbalance — ядро входа (Kalena: bid/ask ratio ≥ порога). Наш
  `ob_imbalance_min=0.58` ≈ 1.4:1.
- Quality > quantity: целевой WR профи 55–70%; мало качественных входов лучше
  массы слабых.

**Правки (одобрено пользователем 2026-05-31):**
1. **time_stop 90→60с** (`time_stop_sec`). Не 45с: убило бы flow_exit-
   победителей (медиана их выхода 56с). 60с — бэкстоп; ранний срез делает (2).
2. **scratch-при-ошибке** (`scratch_on_flow_flip=True`, `scratch_min_age_sec=20`).
   `SweepFadeStrategy.should_exit`: если ход в МИНУС ≥ round-trip И поток (CVD)
   развернулся против И сделка созрела (≥20с) → режем убыток рано
   (`flow_scratch`), не ждём SL/тайм-стоп. Симметрично профит-локу `flow_exit`.
   Флэт/мелкий ±|ход|<комиссии НЕ трогаем (иначе −fee на шуме).
3. **ob_imb обязателен для входа** (`require_ob_imbalance=True`). Был «бонус» в
   reasons; теперь гейт в фазе ВЫСТРЕЛ `SweepReclaimDetector`: reclaim+разворот
   без подтверждения стакана → вход придерживаем (взвод держится). Качество↑.

**Файлы:** `config/settings.py` (time_stop 60, scratch_*, require_ob_imbalance),
`analysis/signals.py` (ob-гейт в детекторе), `analysis/strategies.py`
(scratch-ветка + docstring), `trading/executor.py` (`flow_scratch` в _CLOSE_RU),
`tests/test_scalp_bot.py` (+5 тестов: scratch ×3, ob-гейт ×2; 93 passed).

**Sample-size disclaimer.** Пользователь решил действовать по объёму выборки
(304 сделки), приняв риск одного рыночного режима (~21ч, см. sample-size.mdc).
Правки опираются на данные+research, не на интуицию. Эффект пере-проверить на
свежей выборке (forward-test) — ждём накопления сделок после деплоя.

## 2026-05-30

### v0.5.0 — авто-селектор торговой вселенной (без хардкода монет)
`<hash>`

**Зачем.** Хардкод `SCALP_SYMBOLS` устаревает: волатильность монет дрейфует по
режимам. Теперь бот сам выбирает монеты под стратегию и пересматривает раз в
30 мин (`universe_refresh_sec=1800`; изначально был час, ускорено по запросу
2026-05-30 — рынок дрейфует быстрее часа).

**Как.** `data/universe.py::rank_universe` тянет `get_tickers` (24h snapshot,
офдок <https://bybit-exchange.github.io/docs/v5/market/tickers>) и фильтрует:
- `range% = (high24h−low24h)/last ∈ [6%, 30%]` — амплитуда. Floor 6% из
  математики fee-guard (нужен стоп `R≥0.22%` → round-trip taker 0.11% ×
  min_target_fee_mult / take_profit_r) + live-границы (2.5–5.4% режутся,
  9–16% проходят). Cap 30% — отсечь pump-and-dump (XLM 37%/ALLO 43%).
- `turnover24h ≥ $150M` — ликвидный тир (рабочие монеты были 248–799M$).
- спред ≤ 5 bps. Только `*USDT`-перпы, пре-маркет-листинги пропускаем.
Сортировка по range% убыв. (tie-break turnover) → топ-N (default 5).

**Ротация (часовая, безопасная).** `_rotate_universe`: символ с открытой
позицией НЕ выкидываем пока не закроется; `SymbolState` переиспользуем (CVD
переживает рестарт WS — теряется ~1с реконнекта, не всё окно); стратегии не
пересоздаём — лениво добавляем символы (`ensure_symbols`), чтобы executor
ссылался на те же объекты для дискреционного выхода. exec-стрим (account-wide)
ротации не требует.

**Пороги — конфиг (env), не подгонка** (no-data-fitting.mdc): привязаны к
fee-guard и live-границе, а НЕ оптимизированы под прошлый P&L. Параметры:
`SCALP_AUTO_UNIVERSE_ENABLED/_TOP_N/_REFRESH_SEC/_MIN_TURNOVER_USD/
_MIN_RANGE_PCT/_MAX_RANGE_PCT/_MAX_SPREAD_BPS`. `SCALP_SYMBOLS` — только
fallback при сбое API.

**Файлы:** `data/universe.py` (новый), `trading/client.py` (get_tickers),
`config/settings.py`, `app/main.py`, `analysis/strategies.py` (ensure_symbols),
`docker-compose.yml`, `tests/test_scalp_bot.py` (+6).

### v0.4.2 — точный net P&L из приватного WS execution (вместо REST)
`<hash>`

**Симптом.** БД/Telegram расходились с выпиской Bybit (#47 ZEC: бот $0.0721,
выписка closedPnl $0.0398). Причина: при закрытии бот СРАЗУ дёргал REST
`get_closed_pnl`, биржа ещё не успевала опубликовать запись → fallback на
оценку `taker_pnl` по `mark_price` (519.09 вместо реального филла 518.92).
Гонка по времени → недетерминированный результат (старый #39 совпал, свежие нет).

**Решение (api-docs.mdc — офдок Bybit v5).** Источник истины по P&L —
приватный WebSocket `execution`, а НЕ REST-опрос:
<https://bybit-exchange.github.io/docs/v5/websocket/private/execution>
Каждый филл несёт точные `execPnl` (realized = cashFlow), `execFee` (реальная
комиссия), `execPrice` (реальная цена), `orderLinkId` (наш тег). Матч к сделке
по `orderLinkId` (вход/выход тегаются), для биржевых TP/SL (пустой linkId) — по
символу к открытой сделке. **net = Σ execPnl − Σ execFee = Bybit closedPnl**
(закрытая формула close-pnl). Без гонок и без оценок.

**Поток.** `BybitExecStream` (приватный WS, demo-домен по флагу) кладёт филлы в
потокобезопасную очередь; главный цикл `drain()` → `executor.ingest_executions()`
(в своём треде) накапливает на сделку `{fee, pnl, close_val, close_qty}`.
`_realized_or_estimate` берёт net из леджера когда `close_qty≈qty`; если филлы
ещё в пути (WS обычно быстрее REST) — предв. оценка + флаг `pnl_provisional`,
`reconcile()` дотягивает реальный net из того же леджера на следующих циклах.

**БД.** Колонка `pnl_provisional` (+миграция), `finalize_pnl()`,
`provisional_closed_since()` — БД сходится с выпиской 1:1 (stats-collection.mdc).

**REST.** `get_closed_pnl` оставлен ТОЛЬКО в `_flatten_on_start` (разовый
стартовый реконсил, где WS-леджер ещё пуст). В hot-path REST убран.

**Telegram-уведомление о закрытии** теперь шлётся с РЕАЛЬНЫМ net (из
`reconcile`), а не с оценкой в момент закрытия. Раньше: TG показывал
`+$0.06`, а реальный net (по выписке = дельта Wallet Balance) был `−0.0026`
(NEAR #58 2026-05-30). При provisional-закрытии уведомление откладывается
(`_close_pending`), уходит когда филлы доедут по WS. Fallback
`close_notify_fallback_sec` (10с): если филлы не дошли — шлём оценку с
пометкой `≈`, чтобы уведомление не потерялось. Проверка по выписке: net
сделки = сумма двух `Change` (Open+Close) = дельта Wallet Balance, Bybit
не показывает это одной ячейкой → расхождение было только визуальным в TG.

**Файлы:** `data/exec_stream.py` (новый), `trading/executor.py`,
`app/main.py`, `state/db.py`, `trading/client.py`, `tests/test_scalp_bot.py`.

### v0.4.1 — density_bounce (Фаза 2): стратегия №2 «отскок от плотности»
`<hash>`

**Что.** Вторая независимая стратегия поверх каркаса v0.4.0: отскок от
плотности (крупной лимитки) в стакане. Reversion-философия, родственна
sweep_fade, но другой триггер (структура книги, а не CVD-свип).

**Логика (на символ).**
1. Стена = уровень с size ≥ `density_wall_mult`×baseline (baseline = средний
   размер уровня БЕЗ самой стены, иначе аномалия раздувает свой порог).
2. Стена должна быть близко к круглому числу (`density_round_frac`), шаг
   круглости масштабируется к величине цены (66→шаг1, 518→шаг10, 2.4→шаг0.1).
3. Анти-спуфинг: стена должна выстоять ≥ `density_persist_sec` (10с) до входа.
4. Анти-абсорбция: если ≥ `density_absorb_frac` (30%) стены съели за
   `density_absorb_window_sec` (10с) — снять наблюдение (остаток снимут).
5. Вход, когда цена подошла к стене ≤ `density_near_bps`. bid-стена → LONG,
   ask-стена → SHORT. SL сразу за стеной (`build_signal` swept=цена_стены),
   TP по R с общим fee-guard.
6. Выход (`should_exit`): стена возле SL исчезла → тезис снят → `density_gone`.

**Research basis** (strategy-guard.mdc): Kalena «Crypto Wall Detection» 2026
(стена = 5–8× среднего, относительный порог; >30% за <10с = спуфинг); arXiv
2604.20949 (depth-сигналы причинно раньше flow); Данилов YouTube 2025 (отскок
от плотности на круглом числе, короткий стоп за стеной). Параметры — в
docstring `DensityBounceStrategy` и в `settings.py`.

**Data-слой.** `SymbolSnapshot` теперь несёт top-N уровни стакана
(`bids`/`asks`, цена→объём) — раньше хранился только агрегат `ob_imbalance`.

**Конфликт со sweep_fade.** Если в один тик sweep_fade и density дают РАЗНЫЕ
направления по символу — `resolve` пропускает тик (гард из Фазы 1).

**Тесты.** +9 (near_round, detect_wall baseline-exclusion, arm→fire после
persist, no-fire при удалённой цене, absorption-drop, should_exit wall-gone +
min-age, фабрика двух стратегий). Итого 78 passed.

**Файлы:** `analysis/strategies.py` (DensityBounceStrategy + helpers),
`data/aggregates.py` (bids/asks в snapshot), `config/settings.py` (density_*),
`trading/executor.py` (close-reason density_gone), `tests/test_scalp_bot.py`,
`.env.example`.

### v0.4.0 — мультистратегийный каркас (Фаза 1) + фикс атрибуции PnL
`<hash>`

**Зачем.** Готовим бота к нескольким независимым стратегиям (обсуждение
архитектуры с пользователем): бот гоняет N стратегий поверх одного потока
данных, каждая сама ищет вход, СВОЯ стратегия сопровождает и закрывает свою
позицию; параллельно ищем другие входы по всем стратегиям. Текущий sweep-fade
становится стратегией №1 без изменения поведения. Density-bounce — Фаза 2.

**Каркас (поведение sweep_fade не меняется).**
- `analysis/strategies.py`: протокол `Strategy` (update/armed/reset/should_exit),
  `SweepFadeStrategy` (обёртка над `SweepReclaimDetector` + fee-aware выход
  перенесён сюда из executor), `build_strategies` (фабрика по
  `SCALP_ENABLED_STRATEGIES`), `resolve` (гард конфликта: разные направления по
  символу в один тик → пропуск тика; одна сторона → max score).
- `Signal.strategy` + колонка `trades.strategy` (миграция ALTER для БД на VPS,
  старые сделки → `sweep_fade`). Атрибуция: сделка помечается стратегией.
- Executor: дискреционный выход диспетчеризуется владельцу
  (`strategy.should_exit`); универсальные TP/SL/тайм-стоп/killswitch — общие.
- main: вместо одного детектора — прогон всех стратегий + `resolve`; 1 позиция
  на символ (как и было, через open_symbols).

**Постратегийная стата (мониторинг).** `db.stats_by_strategy(since)` →
сделки/wins/losses/net PnL по стратегиям (реконсил-закрытия исключены).
В heartbeat — строка `📈 [strategy] сегодня: сделок/WR/net`. ВАЖНО: решения об
отключении стратегии — только при ≥100 сделок по связке (sample-size.mdc),
здесь стата = наблюдаемость, не триггер.

**🔴 Фикс рассинхрона PnL (БД ↔ выписка Bybit).** Симптом (повторный репорт):
числа в Telegram (`#36 +0.12`) не сходятся с выпиской. Причина по офдоку
(https://bybit-exchange.github.io/docs/v5/position/close-pnl): ответ
`get_closed_pnl` **НЕ содержит `orderLinkId`**, поэтому прежний матч
`startswith("scalp_")` всегда промахивался и код падал в фолбэк `items[0]`
(самая свежая закрытая по символу) — при частых сделках по ZEC/HYPE это ЧУЖОЙ
цикл. Проверено по примеру доки: `closedPnl = cumExit − cumEntry − openFee −
closeFee` → уже net, т.е. при ПРАВИЛЬНОЙ записи БД корректна; чиним атрибуцию.
Решение `client.closed_pnl(symbol, order_id, qty, since_ms)`: матч по `orderId`
закрывающего ордера (наши reduce-only), для биржевых TP/SL — по `closedSize`≈qty
в окне `startTime`=ts_open. items[0]-фолбэк УБРАН (лучше None+оценка по цене,
чем чужой PnL).

**Тесты.** +11 (resolve-конфликт, фабрика, тег+стата+миграция БД, диспетч
выхода, order_id в closed_pnl). Итого 69 passed. Поведение sweep_fade прежнее.

**Файлы:** `analysis/strategies.py` (new), `analysis/signals.py` (Signal.strategy),
`state/db.py` (колонка+миграция+stats_by_strategy), `trading/client.py`
(closed_pnl), `trading/executor.py` (диспетч выхода+PnL), `app/main.py` (router+
HB-стата), `config/settings.py` (enabled_strategies), `tests/test_scalp_bot.py`.

### v0.3.4 — 🔴 fee-aware выходы: не скретчить флэт в комиссионный минус
`<hash>`

**Симптом (репорт пользователя + выписка Bybit).** Бот систематически закрывал
сделки в мелкий минус, хотя «угадывал» направление. Пример реальной сделки
(get_closed_pnl): SHORT HYPE вход 65.875 → выход 65.824, ход **+0.077$ в нашу
сторону**, но `openFee+closeFee = 0.109$` → `closedPnl = −0.032$`. Все 5 первых
маркет-сделок (#22–#26) закрылись по `flow_exit` за 15–25с в −$0.09…−$0.19.

**Диагноз.** `closedPnl` Bybit — УЖЕ чистый (= gross − openFee − closeFee,
проверено арифметикой), бот пишет его в БД, т.е. в УЧЁТЕ комиссия есть. Дыра в
ЛОГИКЕ ВЫХОДА: `fee-guard` гейтит только цель входа (TP ≥ 3× round-trip), а
активный выход (`flow_exit`) и тайм-стоп закрывали сделку НАМНОГО раньше TP, на
ходе ~0.07% < round-trip taker 0.11%. Итог — «угадал, но комиссия съела».
Вторая неточность: `round_trip_fee_frac=0.00075` (maker+taker) недооценивал
издержки на маркет-входе (обе ноги taker = 0.11%).

**Фикс.**
1. `Executor._flow_exit` стал **fee-aware**: активный выход срабатывает ТОЛЬКО
   когда ход в нашу пользу ≥ round-trip taker (`entry × 2 × 0.055%`) — т.е.
   фиксирует профит, покрывший комиссию (профит-лок по развороту ленты). Флэт/
   мелкий плюс < комиссии больше НЕ скретчим; убыточные ведёт SL (defined risk),
   а не активный выход.
2. `round_trip_fee_frac` 0.00075 → **0.0011** (taker обе ноги, подтверждено
   реальными openFee/closeFee ≈0.109$ на $100). fee-guard теперь требует цель
   ≥ 0.33%, реально бьющую комиссию.

Это правка логики выхода + порога издержек (одобрено пользователем). Выводы по
WR/PnL — после ≥100 сделок (sample-size.mdc); сейчас фиксируем устранение
структурной утечки на комиссии.

**Файлы:** `src/scalp_bot/trading/executor.py` (fee-aware `_flow_exit`),
`src/scalp_bot/config/settings.py` (`round_trip_fee_frac`),
`tests/test_scalp_bot.py` (+3 теста), `.env.example`.

### config (VPS .env) — маркет-вход + волатильные монеты (HYPE/NEAR/ZEC)
`<env-only, без правки кода>`

Симптом после v0.3.3: детектор взводится (armed 24–60/мин) и доходит до
reclaim+разворота, но `FIRED≈0` — 100% сигналов на BTC/ETH/SOL/XRP режет
`fee-guard`: микро-свипы дают стоп вплотную, цель 2R < 0.225% (3× round-trip).

**Артефакт (данные Bybit get_tickers, 2026-05-30 07:30 UTC), 24h range%:**
BTC 2.52 / ETH 3.50 / SOL 3.53 / XRP 5.39 / HYPE 9.49 / ZEC 11.07 / NEAR 16.11
(оборот: HYPE 799M$, SOL 609M$, XRP 381M$, ZEC 256M$, NEAR 248M$). Вывод:
порог fee-guard требует дистанцию стопа ≥0.11% цены; на «тихих» мейджорах
внутриминутные свипы мельче. Подтверждено live: на волатильных альтах HYPE
**дал реальный выстрел** (fee-guard пройден), XRP — 31 отказ/3мин (микро-свипы
как у BTC), SOL/XRP в том же низковолатильном ведре.

**Изменения (env, не код):**
- `SCALP_SYMBOLS=HYPEUSDT,NEARUSDT,ZECUSDT` — ликвидные волатильные альты
  (range 9–16%, оборот >240M$). Убраны BTC/ETH/SOL/XRP (range 2.5–5.4% →
  свипы не проходят fee-guard). XLM(37%)/ALLO(43%) не брали — событийные пампы,
  гэп/слиппедж опасны для fade.
- `SCALP_ENTRY_ORDER_TYPE=market` — мейкер post-only лимитка на быстрых reclaim
  отменялась (HYPE #20: выставлен → Cancelled, цена ушла). Маркет (тейкер) даёт
  гарантированный филл; fee-guard уже закладывает round-trip издержки (3×
  cushion над 0.11% taker round-trip).

Это смена набора монет и типа исполнения (одобрено пользователем), НЕ правка
сигнальной логики/порогов. Сбор статистики продолжается (sample-size.mdc):
выводы по WR/PnL — после ≥100 сделок / ≥2 недель.

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
