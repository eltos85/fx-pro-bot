"""Настройки scalp_bot (env-namespace ``SCALP_*``).

Параметры стратегии вынесены в env, но имеют research-обоснованные
дефолты (см. docstring каждого поля и BUILDLOG_SCALP.md). Изменение
торговых порогов = правка стратегии (strategy-guard.mdc): только с
обоснованием.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScalpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCALP_", extra="ignore")

    # ─── Инфраструктура ──────────────────────────────────────────────────
    data_dir: str = Field(default="/data")
    log_level: str = Field(default="INFO")

    # ─── Bybit ───────────────────────────────────────────────────────────
    bybit_api_key: str = Field(default="")
    bybit_api_secret: str = Field(default="")
    bybit_demo: bool = Field(default=True)
    bybit_category: str = Field(default="linear")
    bybit_testnet: bool = Field(default=False)  # public market-data сеть

    # Монеты: глубокая ликвидность + волатильность (SizeProp 2026,
    # stoic.ai 2026 — BTC/ETH/SOL industry-standard для скальпа).
    symbols: str = Field(default="BTCUSDT,ETHUSDT,SOLUSDT")

    # Активные стратегии (CSV). Каждая ищет/сопровождает входы независимо;
    # конфликт направлений по символу → тик пропускается (см. strategies.py).
    # sweep_fade + density_bounce (fade) + density_break (momentum-пробой, v0.8.0).
    enabled_strategies: str = Field(default="sweep_fade,density_bounce,density_break")

    # ─── Авто-селектор вселенной (data/universe.py) ──────────────────────────
    # Если включён — бот сам выбирает монеты под стратегию из get_tickers, а
    # ``symbols`` используется лишь как fallback при сбое API. Пороги привязаны
    # к математике fee-guard и live-границе (BUILDLOG_SCALP 2026-05-30), а НЕ
    # подгоняются под прошлый P&L (no-data-fitting.mdc).
    auto_universe_enabled: bool = Field(default=True)
    # «Качество, не количество»: берём ВСЕ монеты, прошедшие hard-фильтр; это —
    # лишь safety-кап на число WS-подписок (≤0 = без лимита). Подошло 5 — берём
    # 5, подошло 2 — берём 2 (запрос пользователя 2026-05-31).
    universe_top_n: int = Field(default=15)
    # Пересмотр раз в 5 мин. Ротация — no-op если состав не изменился (см.
    # _rotate_universe), а метрики 24-часовые (двигаются медленно) → частый
    # refresh почти всегда дешёвый get_tickers без WS-рестарта. Ниже ~5 мин на
    # 24h-метриках новой информации не даёт (нужны intraday/RVOL — future).
    universe_refresh_sec: float = Field(default=300.0)
    # 150M→100M (2026-05-31): рынок просел ~2× по обороту, и floor $150M стал
    # выкидывать ровно те рабочие монеты, ради которых ставился (NEAR $137M,
    # ZEC $125M) — а у них range 8–10% и спред 0.2–0.4bps (тоньше BNB). Turnover —
    # грубый прокси; реальный страж ликвидности для скальпа = spread cap (5bps).
    # Не подгонка под P&L: возврат floor его исходного смысла на сдвинувшемся рынке.
    universe_min_turnover_usd: float = Field(default=100_000_000.0)
    # Пины: force-include в ОБХОД фильтра (запрос пользователя 2026-05-31 —
    # вернуть ALLO, который отсекает range-cap 30% как памп 42% + turnover $76M).
    # Осознанный риск памп-н-дампа на КОНКРЕТНОЙ монете, не общее ослабление
    # фильтра. Риск-сайзинг (v0.8.1) частично страхует: широкий range ALLO →
    # большой R → малый лот (qty=$1/дистанция). Пусто = чистый авто-режим.
    universe_pin_symbols: str = Field(default="ALLOUSDT")
    universe_min_range_pct: float = Field(default=6.0)
    universe_max_range_pct: float = Field(default=30.0)
    universe_max_spread_bps: float = Field(default=5.0)

    # ─── Капитал / риск ──────────────────────────────────────────────────
    virtual_capital: float = Field(default=1000.0)
    # Размер сделки в USD (notional). Пользователь мыслит «лотами в $».
    # Минимум 10$ — мельче комиссия/спред съедают прибыль скальпа.
    position_usd: float = Field(default=100.0)
    min_position_usd: float = Field(default=10.0)
    max_leverage: int = Field(default=5)
    # Killswitch (demo): дневной убыток $500, совокупный $800 (буфер до
    # обнуления $1000 депо), max 2 позиции, 5 сделок/час (анти-overtrade).
    max_daily_loss_usd: float = Field(default=500.0)
    max_total_loss_usd: float = Field(default=800.0)
    max_open_positions: int = Field(default=2)
    # 20→5/час (v0.10.0): анализ 402 сделок/24ч показал переторговлю — ~17/ч у
    # rate-limit-кэпа, при этом gross edge ≈0 (+0.031R). Канон: жизнеспособная
    # частота скальпа 3–12 сигналов/день, 8–12 уже даёт net PF<1 (StratBase 2026);
    # «overtrading — главная причина слива» (fxroboteasy/Echo Zero 2026). 5/ч —
    # forcing function против шумовых входов на нулевом edge.
    max_trades_per_hour: int = Field(default=5)

    # ─── Исполнение ──────────────────────────────────────────────────────
    # LIVE на demo по умолчанию (демо-счёт, риска нет). False = PAPER-режим
    # (симуляция без ордеров) — опциональный, не дефолт.
    trading_enabled: bool = Field(default=True)
    # post_only_limit (maker, дёшево) | market (taker, дорого но надёжно).
    # Bybit linear: maker 0.02% / taker 0.055% — round-trip taker ≈0.11%
    # съедает 10-20% цели скальпа (rononcrypto 2026). По умолчанию maker.
    entry_order_type: str = Field(default="post_only_limit")
    entry_fill_timeout_sec: float = Field(default=8.0)
    # Funding settlements Bybit — раз в 8ч (00:00/08:00/16:00 UTC) списание/
    # начисление по открытой позиции. Для 90-сек скальпа почти не задевает,
    # но НЕ открываемся в окне перед списанием, чтобы исключить funding-cost
    # совсем (https://www.bybit.com/en/help-center/article/Funding-fee-Calculation).
    avoid_funding_window_sec: float = Field(default=120.0)

    # ─── Параметры микроструктуры (research-based) ───────────────────────
    # Цикл оценки сигналов: orderflow читается из WS-кэша, без REST.
    eval_interval_sec: float = Field(default=1.0)
    # CVD: окно сэмплов для дивергенции (сек).
    cvd_window_sec: float = Field(default=180.0)
    # Sweep: lookback (сек) для локального swing-хая/лоя.
    sweep_lookback_sec: float = Field(default=300.0)
    # Стакан: сколько уровней берём для imbalance.
    ob_levels: int = Field(default=25)
    # Imbalance, выше которого книга считается перекошенной (bid/(bid+ask)).
    ob_imbalance_min: float = Field(default=0.58)
    # Окно сбора ликвидаций (сек). Аудит v0.9.0: liq как ФАКТОР входа убран
    # (0.2% присутствия на 502 входах, не каноничен для 90–120с разворота).
    # liq_events продолжаем собирать только для heartbeat-наблюдаемости.
    liq_window_sec: float = Field(default=60.0)
    # Анти-шум между входами по одному символу.
    signal_cooldown_sec: float = Field(default=60.0)

    # ─── Подтверждение разворота (sweep-and-reclaim, CAP-протокол) ────────
    # «Не входи во время свипа — жди возврата за уровень + разворота ленты».
    # Источники: chartwhisperer CAP 5-rule protocol (Rule 2 reclaim, Rule 5
    # CHoCH), CrossTrade, Kalena (tape-shift), Quantum-Algo. Главный фикс
    # «ловли ножа»: detect_sweep ловит экстремум, но без reclaim бот мог
    # входить в реальный пробой.
    require_reclaim: bool = Field(default=True)
    # Стакан как подтверждение входа sweep_fade. СНОВА ОБЯЗАТЕЛЕН (v0.10.0,
    # реверс v0.7.0-бонуса). score = sweep+div+reclaim+mom (=4) +ob_imb (=5).
    # Анализ 402 сделок/24ч (2026-05-31): score=5 (ob есть, n=104) gross
    # +0.11R, score=4 (ob нет, n=294 = 73% объёма) gross РОВНО 0.00R — чистый
    # слив на комиссии. Канон «строгий quantifiable edge-фильтр против
    # переторговли» (fxroboteasy/Echo Zero 2026): торгуем ТОЛЬКО где edge
    # доказан. v0.7.0 боялся потерять «жирные вины» no-ob входов, но их net
    # по факту −$47 (294 шт) — асимметрия не спасает нулевой edge. Sample
    # n=104/1 день → форвард-тест, валидируем за 2 недели (sample-size.mdc).
    require_ob_imbalance: bool = Field(default=True)
    # Доля возврата цены от свип-экстремума к свипнутому уровню (0..1).
    reclaim_frac: float = Field(default=0.5)
    # Двухфазный детектор: сколько секунд держим «взвод» после свипа, ожидая
    # reclaim+разворот. Канон: разворот печатается в 1-3 свечах после свипа.
    # 60→120с (v0.11.0): с bar-close подтверждением (confirm_bar_sec) нужно дать
    # ≥2 закрытия 1м-бара, иначе reclaim не успеет подтвердиться.
    arm_timeout_sec: float = Field(default=120.0)
    # Подтверждение reclaim на ЗАКРЫТИИ бара (сек), а не на тиках (v0.11.0).
    # Анализ удержания (2026-06-01): после удаления time_stop медиана холда =
    # 198с (вины 251с, до 16мин), цель TP 3.5R≈1.55% — это 5-15м движение. А
    # триггер стрелял по 30с-momentum на ТИКАХ (детектор взводился ~212 раз/мин
    # на шуме). Рассинхрон ТФ: вход быстрее холда/цели. Канон скальпа — подтверждать
    # на ЗАКРЫТИИ бара таймфрейма сделки (Al Brooks 2012 «signal bar close»;
    # chartwhisperer CAP Rule 2/5; StratBase 2026 — тест на 1м-барах, confirm на
    # close). 60с = 1м-бар: reclaim+разворот должны держаться ДО закрытия бара,
    # мгновенный тиковый прокол не триггерит. 0 = старый тиковый режим (fallback).
    confirm_bar_sec: float = Field(default=60.0)
    # Как часто (сек) повторять «плейбук»-логи ожидания/удержания, чтобы видеть
    # ход стратегии простым языком, но не флудить (цикл крутится ~1с).
    narrate_interval_sec: float = Field(default=15.0)
    # Сколько ждать филлы выхода по приватному WS перед тем как послать
    # close-уведомление в Telegram с ОЦЕНКОЙ (пометка ≈). Обычно филлы
    # доезжают за ~1с и уведомление уходит с реальным net из reconcile.
    close_notify_fallback_sec: float = Field(default=10.0)
    # Окно (сек) для оценки разворота CVD (лента качнулась в сторону сделки).
    momentum_window_sec: float = Field(default=30.0)
    # Минимум сделок в поздней половине окна для валидной CVD-дивергенции
    # (анти «пустота»: дивергенция на 2-3 тиках = шум). В активном рынке
    # late-половина содержит сотни тиков — порог 4 блокирует только мёртвые окна.
    div_min_late_trades: int = Field(default=4)

    # ─── Анти fee-trap (комиссии съедают мелкую цель) ────────────────────
    # Round-trip издержки. v0.10.0: возврат на MAKER-вход (post_only_limit) —
    # вход 0.02% (maker) + выход 0.055% (taker market-close/bracket) = 0.075%.
    # Раньше market-вход давал 0.11% (taker обе ноги). Анализ 402 сделок/24ч:
    # market-исполнение давало drag ~0.35R/сделку (fee+slippage), обнуляя
    # gross edge. Канон: «тейкер съедает 30–67% gross на тонкой цели; профи
    # берут maker-рибейты» (OneKey/StratBase/Echo Zero 2026 — maker = главный
    # рычаг профитности скальпа). Maker-вход убирает и entry-слиппедж (филл по
    # своей цене), не только удешевляет комиссию. Цена компромисса — непролив
    # лимитки на волатильном reclaim → пропуск сделки (канон: 3–12/день, ОК).
    # Источники: liberatedstocktrader, 1minscalper, VT Markets (цель ≥3×).
    # Сигнал отбрасывается, если ход до TP < min_target_fee_mult × round_trip.
    round_trip_fee_frac: float = Field(default=0.00075)
    # Net-expectancy гейт (канон: net edge ≥1.5× round-trip кост, иначе «даришь
    # капитал брокеру» — fxroboteasy 2026). Реализуемая pre-trade форма = цель
    # TP ≥ 3× round-trip (reward-gate строже 1.5×; реальный realized-edge гейт
    # pre-trade невозможен — WR заранее неизвестен). Оставляем 3.0×.
    min_target_fee_mult: float = Field(default=3.0)
    # Мин-R пол: дистанция стопа должна быть достаточно широкой, чтобы комиссия
    # была МАЛОЙ долей риска. R ≥ min_risk_fee_mult × round_trip_fee →
    # fee ≤ 1/mult доля R. mult=4 → fee ≤ 0.25R (R≈0.44%, TP 3.5R≈1.55% — центр
    # проф-коридора цели скальпа 0.5–2%). Обоснование (research, не подгонка под
    # выборку): издержки съедают 50–80% профита скальпера при тугом стопе
    # (Echo Zero 2026); стоп = «структура + ATR-буфер», 0.8–1.5× ATR за свингом
    # (cryptotrading-guide 2026, VT Markets, Wilder «2 ATR»); цель 0.5–2%
    # (stoic.ai 2026). Анализ 31 flow_scratch (2026-05-31): при R≈0.13% комиссия
    # ≈0.4–0.8R и съедала асимметрию. SL отодвигаем ЗА структуру, если структурный
    # R меньше пола (canon «beyond swing + buffer»).
    min_risk_fee_mult: float = Field(default=4.0)
    # Сайзинг: риск-базированный (канон профи: «стоп с графика, размер —
    # следствие»: qty = risk_per_trade_usd ÷ |entry−SL|). Широкий стоп тогда НЕ
    # растит $-риск, а лишь уменьшает лот. Источники: TradeOlogy/DYOR/StockCharts
    # 2026 («size is the output, never the input»). False = старый фикс-notional.
    risk_based_sizing: bool = Field(default=True)
    # Фиксированный $-риск на сделку (1% депо $1000 — Tharp/Van Tharp; при R≈0.44%
    # notional≈$227, в пределах killswitch $500/день и 2 одновременных позиций).
    risk_per_trade_usd: float = Field(default=1.0)

    # ─── density_bounce (стратегия №2: отскок от плотности в стакане) ─────
    # Стена = крупная лимитка ≥ wall_mult × средний размер уровня на своей
    # стороне (top-N). Kalena 2026: «relative sizing», порог 5–8× среднего за
    # 10–15 мин. 8→5 (2026-05-31): на живых книгах Bybit (top-25, мгновенный
    # baseline) самый крупный уровень всего 2–4× среднего — 8× (консерв. край)
    # НЕДОСТИЖИМ, density_bounce/break не «взводились» (0 сделок за всю историю).
    # 5× — НИЖНИЙ край research-диапазона Kalena, не подгонка (остаёмся в каноне).
    # Известное ограничение: research меряет vs среднее за 10–15мин, мы — vs
    # мгновенный top-25 → ratio структурно занижен; rolling-baseline = future.
    density_wall_mult: float = Field(default=5.0)
    # Близость стены к круглому числу (доля цены). Данилов: плотности на круглых
    # уровнях надёжнее как S/R. 0.1→0.3% (2026-05-31): 0.1% было слишком жёстко
    # (near_round=False на всех живых книгах) — гейт глушил все стены.
    density_round_frac: float = Field(default=0.003)  # 0.3%
    # Анти-спуфинг: стена должна продержаться ≥ persist_sec до входа.
    density_persist_sec: float = Field(default=10.0)
    # Анти-абсорбция: если ≥ absorb_frac стены «съели» за absorb_window —
    # остаток скоро снимут (Kalena: 30% за <10с → выход/не вход).
    density_absorb_frac: float = Field(default=0.30)
    density_absorb_window_sec: float = Field(default=10.0)
    # Вход, когда цена подошла к стене ближе near_bps (б.п. от цены стены).
    density_near_bps: float = Field(default=8.0)
    # Опциональный абсолютный пол стены в USD (0 = выкл, только относительный).
    density_min_wall_usd: float = Field(default=0.0)
    # Rolling-baseline (аудит v0.9.0): стена сравнивается со СКОЛЬЗЯЩИМ средним
    # «типичного» размера уровня за окно, а НЕ с мгновенным top-25. Это и есть
    # каноничный Kalena «5–8× среднего за 10–15 мин» — мгновенный baseline давал
    # max-уровень всего 2–4× (стена недостижима, 0/502 входов). 900с = верх
    # research-окна. Пока не накоплено ≥min_samples — fallback на мгновенный.
    density_baseline_sec: float = Field(default=900.0)
    density_baseline_min_samples: int = Field(default=30)

    # ─── HTF-bias: трендовый фильтр старшего ТФ (аудит v0.9.3) ────────────
    # Канон CAP «без контекста CVD-дивергенция — шум» (gates 1–3); Murphy 1999
    # (EMA200 primary trend); Asness 2013 (mean-reversion в согласии с трендом).
    # Фейд берём ТОЛЬКО по тренду: long-fade при price>EMA200_1h, short — ниже.
    # Контртренд (ловля ножа) блокируем. Гейт в main после resolve, fail-open
    # при сбое свечей. Без фильтра sweep_fade фейдил «в вакууме» (WR 29–40%).
    require_htf_trend: bool = Field(default=True)
    htf_interval: str = Field(default="60")   # 1H (Bybit kline interval)
    htf_ema_len: int = Field(default=200)      # EMA200 — primary trend (Murphy)
    htf_refresh_sec: float = Field(default=300.0)

    # ─── Сессионный фильтр (опционально, default OFF) ─────────────────────
    # Канон: свипы доходят в London/NY open + overlap, «мёртвые» часы дают
    # ложные. Crypto 24/7 + строгий конфлюенс → по умолчанию ВЫКЛ, чтобы не
    # уморить частоту. Включать при достаточной статистике.
    session_filter_enabled: bool = Field(default=False)
    # Активные UTC-часы (London 07-10, NY 13-16 + overlap 12-16).
    active_hours_utc: str = Field(default="7,8,9,12,13,14,15,16")

    # ─── Управление позицией ─────────────────────────────────────────────
    # v0.9.5: time_stop УДАЛЁН. Был реликтом эпохи контроля убытка (v0.6.0, 86%
    # потерь шло от тайм-стопа). Противоречил Философии B «дай победителю бежать»:
    # force-закрывал прибыльную ещё валидную сделку по таймеру (подрезал медленных
    # грайндеров до 3.5R). Теперь выход ТОЛЬКО по: flow_exit (лок при флипе ленты
    # ≥1R), flow_scratch (срез убытка при флипе ≥0.7R), биржевой TP@3.5R / SL@−1R.
    # Стоячая сделка гарантированно закрывается биржевым кронштейном (одобрено
    # пользователем 2026-05-31; принят tradeoff «может висеть дольше 120с»).
    # TP/SL в единицах R; SL ставится за свипнутый уровень + буфер.
    # 2.0→3.5R (Философия B): «дай победителю бежать» — асимметричный payoff
    # (редкий крупный вин перекрывает серию мелких минусов). 3.5R в каноне
    # свип-разворота (CrossTrade 2:1–4:1, chartwhisperer T1≈2-3R, T2 дальше).
    # flow_exit (профит-лок по развороту ленты) НЕ тронут: если поток держит —
    # сделка бежит к 3.5R, если развернулся — фиксируем накопленное раньше.
    take_profit_r: float = Field(default=3.5)
    sl_buffer_bps: float = Field(default=8.0)  # буфер за свип-уровнем, б.п.
    # Активный выход (hard invalidation): закрыть раньше тайм-стопа, если
    # ордер-флоу (CVD) развернулся ПРОТИВ позиции. Все скальп-источники:
    # «exit immediately when order flow flips» (Kalena, tradezella, tradealgo).
    active_exit_enabled: bool = Field(default=True)
    active_exit_min_age_sec: float = Field(default=10.0)  # не дёргаться на шуме
    # Профит-лок (flow_exit) фиксирует по развороту ленты ТОЛЬКО когда набрана
    # осмысленная прибыль ≥ flow_exit_activate_r × R (R = |entry−sl|). Анти-клиппинг
    # (анализ 427 сделок 2026-05-31): при пороге «≥ round-trip комиссии» flow_exit
    # давал 79 вин с медианой ~$0.04 (клипал центы), тогда как добежавшие до TP
    # (tp_sl) вины были в 4× крупнее (avg +$0.39). Копеечный порог обнулял смысл
    # TP=3.5R (v0.7.0) — сделка не доживала до цели. 1R = «дай заработать ставку,
    # потом фиксируй по развороту» (асимметричный payoff, Философия B).
    flow_exit_activate_r: float = Field(default=1.0)
    # Scratch-при-ошибке (research «exit if wrong» + анализ 304 сделок
    # 2026-05-31): если сделка явно в МИНУСЕ (ход против ≥ round-trip комиссии)
    # И поток (CVD) развернулся против — режем убыток рано, не ждём SL/тайм-стоп.
    # Данные: убыточные тянулись до 91с (ср. −$0.167), а с разворотом ленты идут
    # к SL (−$0.467). Брать flat/мелкий минус НЕ скретчим (иначе −fee на шуме).
    scratch_on_flow_flip: bool = Field(default=True)
    # Даём сетапу «созреть» перед скретчем (research: ~30с shot-clock; берём 20с,
    # т.к. flow_invalidated сам требует разворота ленты — это уже сильный сигнал).
    scratch_min_age_sec: float = Field(default=20.0)
    # Порог ГЛУБИНЫ скретча (аудит v0.9.2): режем убыток только когда сделка
    # реально в минусе ≥ scratch_min_adverse_r × R, а не при «минус ≥ комиссии»
    # (hair-trigger). Данные (60 свежих сделок, risk≈$1): старый порог давал
    # flow_scratch на 40% входов, ВСЕ в минус (−$12.31); резал при ходе против
    # всего −0.29R (далеко от SL −1R), реализуя −0.56R (0.27R съедала комиссия).
    # 0.7R симметричен анти-клиппингу flow_exit (≥1R): мелкий минус на шумовом
    # флипе ДЕРЖИМ (даём развиться к TP или дойти до биржевого SL), режем лишь
    # реально ломающиеся сделки раньше полного SL. С min_risk_fee_mult=4 (fee≈0.25R)
    # порог 0.7R заведомо выше комиссии. Не подгонка под P&L: устранение
    # hair-trigger по механике (fee-gap) + симметрия с flow_exit.
    scratch_min_adverse_r: float = Field(default=0.7)

    # ─── Старт «с чистого листа» ──────────────────────────────────────────
    # При старте закрыть любые открытые позиции по нашим символам и
    # реконсилить «зависшие» open-сделки в БД (новая логика входа/выхода).
    flatten_on_start: bool = Field(default=True)

    # ─── Telegram (опционально, нотификации без поллинга команд) ─────────
    telegram_enabled: bool = Field(default=False)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]

    @property
    def universe_pin_list(self) -> list[str]:
        return [s.strip().upper() for s in self.universe_pin_symbols.split(",")
                if s.strip()]

    @property
    def strategy_list(self) -> list[str]:
        return [s.strip() for s in self.enabled_strategies.split(",") if s.strip()]

    @property
    def active_hours(self) -> set[int]:
        return {int(h) for h in self.active_hours_utc.split(",") if h.strip()}


def load_settings() -> ScalpSettings:
    return ScalpSettings()
