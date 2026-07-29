# План правок fx_momentum_bot — 2026-07-24

Рабочий документ для поддержания контекста. Обновляется по мере выполнения.
Не деплоится автоматически; по готовности — коммит + selective rebuild `fx-momentum-bot`
по SSH (deploy-vps.mdc), либо полный деплой через GH Actions.

## Контекст решения

- **Окно анализа**: с последней правки ЛОГИКИ `83f8a2a` (13.07 07:08 UTC, per-symbol guard
  re-apply) по 24.07. Profit-protect (`4b32474`, 15.07) отключён через env 22.07 → не активен.
- **Метрики (34 сделки, cTrader deal-list ground truth)**: net −$141.85, WR 44%, avgR −0.30,
  PF 0.32. avg win +$4.49 / +0.48R vs avg loss −$11.01 / −0.92R (ratio 2.45×). EXP −0.30R.
- **Решающий тест H9 (Hurst на H1)**: H≈0.535 у EURUSD/GBPUSD/USDJPY/AUDUSD → слабо trending,
  НЕ mean-reverting. Edge есть, но тонкий → проблема в логике выхода/входа, не в фундаменте.
- **Что НЕ причина**: транзакционные издержки (4% net-лосса, gross уже −$201), Asia-сессия
  (уже отфильтрована), фундаментальная неверность momentum на H1 (Hurst опровергает).
- **Исследование источников**: research-агент ef9dc34c (Menkhoff/Sarno 2012, Moskowitz 2012,
  Daniel&Moskowitz 2016, Chan, AQR, López de Prado, r/algotrading, Wikibit, FX Foundations).
- **Решение пользователя (24.07 11:05)**: правило `sample-size.mdc` (≥100 сделок) снимается
  до набора выборки (месяцы); исполнить все пункты A–E. `strategy-guard.mdc` всё равно
  требует research-цитаты в docstring + обновление `STRATEGIES.md` + тесты + запись в
  `BUILDLOG.md` (не в `BUILDLOG_TRADECARD_MOMENTUM.md` — тот только для ревьюера).

## Артефакты анализа (источник правды, no-data-fitting.mdc)

- `scripts/momentum_loss_audit.py` — read-only аудит (cTrader deal-list + ctx входа).
- `/tmp/momentum_trades.json` — дамп 34 сделок (выгружен из named volume tradecard_momentum_data).
- `/tmp/hurst_h1.py` — Hurst R/S на H1 (yfinance, 730d).
- `/tmp/analyze_momentum.py` — локальный deep-analysis (R-distribution, hold, asymmetry).

## Срезы-доказательства

| Срез | Значение | Вердикт |
|---|---|---|
| avg win +0.48R / avg loss −0.92R | R:R ~0.5:1 при WR 44% | главная причина (H4) |
| exit: sl_hit n=13 (−$146), profit_exit n=10 avgR **+0.64** | победители режутся коротко | (A) |
| hold: losers med 3.5h, winners med 4.5h; 6/15 win <3h | hold короткий для momentum | (A)/(H3) |
| NY-open 14-16h UTC: WR 0-20%, net −$109 | London-open 08h WR 62% ~0 | (B) |
| ADX<20 (range): 19/34 сделок, PF 0.24, −$119 | ADX 20-30 ~ноль | (C) |
| with_htf PF 0.21 (−$131) vs counter PF 0.75 | HTF EMA200 вредит | (D) |
| 2 beyond_sl (07-14 12:30, обе в одну минуту) R −1.39/−2.45 = −$51.6 = 36% | gap через SL | (E) |
| costs = 4% net-лосса, gross −$201 | НЕ причина | — |

---

## Чек-лист правок

### (A) Exit: sign-decay на −threshold вместо zero-cross (гистерезис)
- [x] Спроектировать: позиция живёт, пока momentum не пересёк **−threshold** против
      направления (а не голый 0). Вход остаётся на +threshold → гистерезис полный.
      Победители получают room до реального разворота; проигрыши выходят раньше SL.
- [x] Реализовать в `src/fx_momentum_bot/app/main.py` (`_momentum_sign_direction` →
      новая функция с порогом; `sign-decay` блок ~859-908). Порог = `signal_threshold`.
- [x] Research-цитата в docstring: Chan (momentum требует persistence, не noise-exit);
      Moskowitz 2012 sign-rule как база, hysteresis — реализационная адаптация для тонкого
      H1-trend (Hurst H≈0.535). Сослаться на artifacts.
- [x] Тест: sign-decay НЕ закрывает при momentum в (−threshold, 0) против позиции;
      закрывает при < −threshold; существующий `test_decay_close_selection_via_sign`
      обновить.
- [x] Env-флаг `MOMENTUM_BOT_DECAY_EXIT_THRESHOLD_MULT` (1.0 = −threshold; 0.0 = старый
      zero-cross) для обратимости (strategy-guard «Обратимо»).
- [x] Прогон pytest: 60 passed.

### (B) Фильтр NY-open: блокировка входов 14-16h UTC (configurable)
- [x] Расширить `session_filter.py` или новый `ny_open_block`: список часов UTC для
      блокировки входов (по часу закрытого бара, как session_filter).
- [x] Настройки `MOMENTUM_BOT_NY_OPEN_BLOCK_ENABLED`, `MOMENTUM_BOT_NY_OPEN_BLOCK_HOURS`
      (default "14,15,16"). Обратимо.
- [x] Research-цитата: TheTradersLegacy (first 90 min NY = liquidity trap/stop-hunt);
      Andersen et al. 2003 (NY volatility). Чётко пометить: порог 14-16h data-driven
      из 34 сделок (n=5,3,2) → переоценить на 100 сделках.
- [x] Тест: вход в 14h UTC блокируется, 08h/12h — нет.
- [x] Прогон pytest: 63 passed.

### (C) ADX-фильтр входа: блокировка при ADX<20
- [ ] В точке входа использовать `ctx.adx` (уже считается `compute_entry_context`,
      observability → теперь становится блокирующим фильтром — снять «never blocks»
      инвариант в docstring, обновить).
- [ ] Настройки `MOMENTUM_BOT_ADX_FILTER_ENABLED`, `MOMENTUM_BOT_ADX_MIN` (default 20).
      Обратимо. ctx=None (мало данных) → НЕ блокировать (не ломать холодный старт).
- [ ] Research-цитата: Wilder 1978 (ADX<20 = range); Chan/AQR (momentum needs trend);
      artifact: ADX<20 PF 0.24.
- [ ] Тест: ctx.adx=15 + long-сигнал → skip:low_adx; ctx.adx=25 → вход; ctx=None → вход.

### (D) HTF EMA200: пересмотреть роль (консервативно)
- [x] Анализ: with-trend PF 0.21 vs counter 0.75 на 11/23 сделках — мало, инверсия = overfit.
      Решение: НЕ инвертировать, НЕ вводить блокировку по with_htf.
- [x] Проверить код: есть ли где блокировка по with_htf/ema_dist? grep main.py →
      НЕТ, только log/persist (строки 1100-1120). → (D) = «оставить observability-only,
      не вводить HTF-фильтр»; зафиксировать в BUILDLOG, что инверсия отвергнута как
      overfit на малой выборке. Код НЕ трогать.
- [x] Research-цитата: Lyons 2001, Asness 2013; отказ от инверсии — López de Prado DSR
      (34 сделки недостаточно для смены знака фильтра).

### (E) Gap-защита: закрытие открытых позиций перед high-impact новостями
- [x] Расширить `event_guard.py`: `high_impact_event_upcoming(before_min)` — HIGH-событие
      в следующие before_min минут (окно строго [now, now+before], без пост-релизного хвоста).
- [x] В цикле (main.py, рядом с friday_flat): если HIGH-событие в следующие
      `news_close_before_min` (default 5) мин → закрыть открытые позиции символа
      (scope: US — все; ECB — EUR-пары; BoJ — JPY-пары), как friday_flat.
- [x] Настройки `MOMENTUM_BOT_NEWS_CLOSE_ENABLED`, `MOMENTUM_BOT_NEWS_CLOSE_BEFORE_MIN`
      (default 5). Обратимо. before_min=0 → выключено.
- [x] Research-цитата: Andersen/Bollerslev/Diebold/Vega 2003 (news overreaction + gap);
      FX Foundations (slippage on fill); artifact: 2 beyond_sl = 36% убытка.
- [x] Тест: HIGH-событие через 3 мин, before_min=5 → upcoming; через 10 мин / после → нет;
      scoping ECB/BoJ; before_min=0 → выключено.
- [x] Прогон pytest: 71 (momentum) / 1266 (весь suite) passed.

### Общее
- [ ] `STRATEGIES.md` — добавить раздел fx_momentum_bot с research-блоками (параметры
      exit/session/ADX/news-close + источники). Обновить при изменении параметров.
- [ ] `BUILDLOG.md` (НЕ TRADECARD) — запись `fix(momentum): exit-hysteresis + NY-open
      + ADX + news-close` с симптом→причина→решение, ссылка на artifacts, метрики.
- [ ] `tests/test_fx_momentum_bot.py` — прогон `python3 -m pytest tests/test_fx_momentum_bot.py -v`.
- [ ] Коммит (без деплоя) — деплой отдельным решением пользователя.

## Статус
- [x] Анализ и метрики собраны
- [x] Research источников завершён
- [x] Hurst H9 проверен
- [x] План создан (этот документ)
- [x] (A) exit-hysteresis — `decay_exit_threshold_mult` (1.0)
- [x] (B) NY-open block — `ny_open_block_hours` (14,15,16)
- [x] (C) ADX filter — `adx_min` (20), `adx_block_reason`
- [x] (D) HTF решение — observability-only, инверсия отвергнута (overfit)
- [x] (E) news-close — `news_close_before_min` (5), `high_impact_event_upcoming`
- [x] Тесты — 71 (momentum) / 1266 (весь suite) passed
- [x] STRATEGIES.md §8 + BUILDLOG.md (2026-07-24) обновлены
- [x] Коммит + push (ветка feat/ai-trader-v0.30-institutional): 60f386c → a523e6f (ctx fix) → 02b4a68 (docs)
- [x] Деплой — selective rebuild fx-momentum-bot на VPS, контейнер Up, ошибок нет

## Итоговые env-флаги (все обратимы, дефолты = правки включены)

| Флаг | Default | Выключение (старое поведение) |
|---|---|---|
| `MOMENTUM_BOT_DECAY_EXIT_THRESHOLD_MULT` | 1.0 | 0.0 = zero-cross exit |
| `MOMENTUM_BOT_NY_OPEN_BLOCK_ENABLED` | true | false |
| `MOMENTUM_BOT_NY_OPEN_BLOCK_HOURS` | "14,15,16" | "" |
| `MOMENTUM_BOT_ADX_FILTER_ENABLED` | true | false |
| `MOMENTUM_BOT_ADX_MIN` | 20.0 | — |
| `MOMENTUM_BOT_NEWS_CLOSE_ENABLED` | true | false |
| `MOMENTUM_BOT_NEWS_CLOSE_BEFORE_MIN` | 5 | 0 |
