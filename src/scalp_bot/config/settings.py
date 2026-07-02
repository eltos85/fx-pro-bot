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
    # sweep_fade_canon (v0.18.20) — параллельный КАНОН-вариант sweep_fade
    # (форвард-тест A/B, одобрено пользователем 2026-06-11): значимые уровни
    # (PDH/PDL + дневные экстремумы) + full reclaim + вселенная мейджоров.
    # sweep_fade_run (v0.18.27, 2026-06-26) — изолированная гипотеза «дай
    # winners бежать»: canon-вход + breakeven-lock@1.0R + TP 3.0R + scratch
    # только лузеров (A/B против canon, одобрено пользователем 2026-06-26).
    # sweep_fade_trend (v0.18.27, 2026-06-26) — canon + rolling-trend-day-gate
    # входа: не фейдить в активном тренде дня (A/B против canon, одобрено).
    enabled_strategies: str = Field(
        default="sweep_fade,density_bounce,density_break,sweep_fade_canon,"
                "sweep_fade_run,sweep_fade_trend")

    # ─── sweep_fade_canon (v0.18.20): канон-вариант параллельным форвард-тестом ──
    # Базовый sweep_fade живёт ниже канонного WR 60%+ (live 899 сделок: WR 35%,
    # лучшая неделя 52%). Разрыв с каноном CAP — три упрощения: (1) фейдим
    # 3-минутный микро-экстремум вместо ЗНАЧИМОГО уровня ликвидности (PDH/PDL,
    # session H/L — где реально стоят стопы: Osler 2003 NY Fed «stop orders
    # cluster»; CAP/chartwhisperer «sweep of liquidity pool»); (2) reclaim 50%
    # пути вместо полного возврата ЗА уровень (CAP Rule 2 буквально); (3) vol-
    # вселенная подобрана под пробой, а fade канонически живёт в ликвидных
    # рейнджах (live: ETH WR 55% vs ZEC 28%). Канон-вариант исправляет все три,
    # выходы/SL/TP оставлены ИДЕНТИЧНЫМИ базовому — A/B изолирует качество входа.
    # Обе версии копят выборку параллельно (атрибуция в БД по колонке strategy),
    # решение по n≥100 на каждую (sample-size.mdc).
    # Вселенная канона: ликвидные мейджоры (深 книги, рейнджи — Tradeify
    # «ES deep book → fade»; наш live ETH 55% WR). Торгуются ТОЛЬКО канон-стратой
    # (symbol_scope), авто-вселенная других страт не затронута.
    sweep_fade_canon_symbols: str = Field(
        default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT")
    # v0.18.24: тип входа канона — TAKER ПО КАНОНУ Turtle Soup (Connors/Raschke
    # 1995 «Street Smarts»): канон-вход — стоп НАД/ПОД уровнем, срабатывает на
    # возврате цены сквозь уровень = активный вход ПО reclaim (исполняется как
    # taker). Пассивная maker-лимитка ниже цены = вход «купить откат вниз»,
    # ПРОТИВОПОЛОЖНЫЙ канон-входу вверх — maker был fee-overlay (v0.10.0), не из
    # канона свипа. Taker = возврат к канону. Подтверждение на данных (не
    # причина): канон-maker наливался 0/4 за сутки. База sweep_fade НЕ тронута
    # (остаётся maker, копим стату). None/пусто → глобальный maker.
    sweep_fade_canon_entry_order_type: str = Field(default="market")
    # Full reclaim (CAP Rule 2): цена должна ВЕРНУТЬСЯ за свипнутый уровень
    # (1.0 = весь путь), а не 50% как у базового.
    sweep_fade_canon_reclaim_frac: float = Field(default=1.0)

    # ─── sweep_fade_run (v0.18.27, 2026-06-26): изолированная гипотеза ───────
    # «дай winners бежать». Параллельный форвард-тест A/B против canon
    # (одобрено пользователем 2026-06-26). Вход = canon (значимые уровни +
    # full reclaim + мейджоры + taker). НОВОЕ — exit-контракт:
    #   • breakeven-lock при favourable ≥ be_activate_r (1.0R) — перенос
    #     биржевого SL к entry+буфер. Артефакт scalp_sf_study n=169: 98%
    #     сделок на 1R — winners, 18% лузеров → чистая точка BE.
    #   • flow_exit@1.5R УБРАН (главный убийца winners: MFE winners 3.11R,
    #     а фикс на ~1.27R). Winners бегут к биржевому TP, защищённые BE-стопом.
    #   • TP = 3.0R (по медиане winner-MFE 3.11R — data-driven, не интуиция).
    #   • flow_scratch только на losing side (favorable<0 + разворот ленты).
    # Источник: scripts/scalp_sf_study.py (cutoff 2026-06-17, verified 100%),
    # Sweeney 1988 MFE, Schwager/Brooks «winners run». Изоляция: свой name,
    # атрибуция в БД по strategy, не трогает base/canon. Копит n≥100.
    # Вселенная run-страты (по умолчанию = canon-список → A/B чистый
    # «canon vs canon+exit»). env SCALP_SWEEP_FADE_RUN_SYMBOLS.
    sweep_fade_run_symbols: str = Field(default="")
    # TP winners (R). 3.0 = медиана winner-MFE (ловит 52% полностью).
    sweep_fade_run_take_profit_r: float = Field(default=3.0)
    # Порог breakeven-lock (R favourable). 1.0 = MFE-разделение winners/losers.
    sweep_fade_run_be_activate_r: float = Field(default=1.0)
    # Losing-side scratch при развороте ленты (winners НЕ режем).
    # 2026-07-02: ВЫКЛЮЧЕН (default False) по анатомии убытков live-форварда:
    # 23 flow_scratch у run = −$257 (31% всех потерь бота за период), реализация
    # −1.13R/скретч при пороге −0.7R (slippage+fees+хвост до фактического
    # закрытия) — ХУЖЕ чем дать дойти до биржевого SL (−1R). Согласуется с
    # контрфактуалом v0.13.0 базовой страты (data/scalp_sweep.txt: sa 0.7→OFF
    # WR 36→43%, avgR −0.238→−0.215) — «чем меньше режем, тем лучше».
    # Артефакт: scripts/scalp_loss_anatomy.py (snapshot scalp_bot.sqlite
    # 2026-07-02, сверен с Bybit closedPnl). env для форвард-A/B оставлен.
    sweep_fade_run_scratch_on_flow_flip: bool = Field(default=False)

    # ─── sweep_fade_trend (v0.18.27, 2026-06-26): canon + trend-day-gate ───
    # Изолированная гипотеза «не фейдить в активном тренде дня». A/B против
    # canon (одобрено 2026-06-26). Вход = canon (значимые уровни + full
    # reclaim + мейджоры + taker + canon-exit flow_exit@1.5R). НОВОЕ — gate
    # входа по rolling-режиму дня: |close−open| последних N 15m-баров / avgATR
    # > trend_max → пропуск (в активном тренде свип = продолжение, не разворот).
    # Источник: scripts/scalp_canon_study.py (n=105, cutoff 2026-06-14):
    #   fade ПО тренду +$1.40/сделку WR 55% (прибыльно), fade ПРОТИВ тренда
    #   −$2.56/сделку (весь минус). Направленный EMA-гейт снять нельзя
    #   (canon v0.18.22 — свип PDH ⇒ EMA всегда long ⇒ 100% блок), поэтому
    #   гейтим режим, не направление. Wilder 1978 ADX; Connors/Raschke.
    # Изоляция: свой name, атрибуция по strategy, не трогает base/canon/run.
    # Вселенная trend-страты: пусто → canon-список (чистый A/B). env
    # SCALP_SWEEP_FADE_TREND_SYMBOLS.
    sweep_fade_trend_symbols: str = Field(default="")
    # Порог трендовости rolling-regime (> = тренд, пропуск). 1.5 — консистентно
    # с day_regime в анализе (scripts/scalp_canon_study.py).
    sweep_fade_trend_max: float = Field(default=1.5)
    # Lookback rolling-regime (число закрытых 15m-баров). 8 = 2 часа.
    sweep_fade_trend_lookback_bars: int = Field(default=8)

    # Per-symbol LONG-блок (CSV): на этих символах входы в ЛОНГ запрещены ВСЕМ
    # стратегиям, шорты разрешены. v0.18.17 (C-07) ставил ZECUSDT; v0.18.19
    # СНЯТ (одобрено пользователем): полная выборка показала, что лонг-минус
    # ZEC на ~85% набран при ×2.0-инверсии payoff (A-1; до 06-05 long −15.68 ≈
    # short −18.95 — симметрично), а шорты ZEC тоже минусовые (sweep_fade −64,
    # break −43.59) — side-асимметрия была иллюзией окна замера. Контртренд-
    # лонг риск покрыт структурно (DMI long-gate v0.18.18 + HTF-гейты).
    # Механизм сохранён как reversible exposure-lever (вкл. через env без
    # деплоя кода). Пусто = выкл. env SCALP_NO_LONG_SYMBOLS.
    no_long_symbols: str = Field(default="")

    # ─── Авто-селектор вселенной (data/universe.py) ──────────────────────────
    # Если включён — бот сам выбирает монеты под стратегию из get_tickers, а
    # ``symbols`` используется лишь как fallback при сбое API. Пороги привязаны
    # к математике fee-guard и live-границе (BUILDLOG_SCALP 2026-05-30), а НЕ
    # подгоняются под прошлый P&L (no-data-fitting.mdc).
    auto_universe_enabled: bool = Field(default=True)
    # Метод авто-отбора монет (переключатель «старый/новый»):
    #   "rvol"     — штатный селектор data/universe.py: 24h ликвидность/спред +
    #                анти-памп range-cap + intraday RVOL-ранжирование (default).
    #   "momentum" — метод «как в ролике» (data/momentum_universe.py): ТОП по
    #                росту/падению за 24h + порог оборота, БЕЗ анти-памп кэпа
    #                (ролик SerCrypto https://youtu.be/gCgYS-CsGWc).
    # Меняет ТОЛЬКО список символов, подаваемый стратегиям; сама торговая логика
    # sweep_fade не трогается → чистый A/B отбора монет. Решение «что лучше» —
    # по форвард-выборке n≥100 (sample-size.mdc), не по первым сделкам.
    # env SCALP_UNIVERSE_METHOD=momentum.
    universe_method: str = Field(default="rvol")
    # ─── momentum-метод (используется при universe_method="momentum") ────────
    # Порог суточного оборота. Ролик: «от 50 млн уже можно рассматривать, в
    # идеале 100+ млн». Дефолт 50M = нижняя граница ролика (мягче RVOL-floor
    # 100M намеренно — momentum берёт «то что стреляет», даже на меньшем обороте).
    momentum_min_turnover_usd: float = Field(default=50_000_000.0)
    # Минимальный |24h change| (%) для попадания в кандидаты. 0 = без порога,
    # только ранжируем по модулю движения (берём топ мувёров каков бы рынок ни был).
    momentum_min_change_pct: float = Field(default=0.0)
    # Спред-cap (bps). В ролике спред-фильтра нет → дефолт 0 (выкл). Включить,
    # если на тонких мувёрах спред съедает цель (sweep_fade fee-guard).
    momentum_max_spread_bps: float = Field(default=0.0)
    # Сторона отбора: "both" — гейнеры+лузеры (по модулю движения; sweep_fade сам
    # выберет сторону по HTF-гейту), "up" — только рост, "down" — только падение.
    momentum_direction: str = Field(default="both")
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
    # Пины: force-include в ОБХОД фильтра. v0.12.0 (2026-06-01): УБРАН пин ALLO —
    # канон-ревизия подбора монет (Volity/stoic.ai/dev.to 2026): «избегать pump/
    # dump-монет». ALLO (range 42%, дамп −32%) — параболическая, пин нарушал канон
    # (был ручной override «верни ALLO»). Пусто = чистый авто-режим по фильтрам.
    universe_pin_symbols: str = Field(default="")
    universe_min_range_pct: float = Field(default=6.0)
    # Range-cap: канон «>20%/день = манипуляция, избегать» (stoic.ai 2026; Volity:
    # >5% ATR = hot, size down). 30→20 (v0.12.0): 30% пропускал манипулятивные
    # движения. Research-cited порог, НЕ тюнинг под наш P&L (no-data-fitting).
    universe_max_range_pct: float = Field(default=20.0)
    universe_max_spread_bps: float = Field(default=5.0)
    # v0.14.0: СВЕЖИЙ отбор по intraday-активности (RVOL по амплитуде), а не
    # только по лагающему 24h-снимку. RVOL = текущая часовая амплитуда (rolling
    # 1ч из 5м-баров) / медиана часовых амплитуд монеты за сутки. Канон отбора
    # «что в игре СЕЙЧАС»: RVOL≥2 — сильно в игре, ≥1.5 умеренно, <1 затихла
    # (TradingSim/Warrior/anomiq 2026). Гейтим монеты, затихшие в последний час
    # (RVOL < порога), и ранжируем по RVOL. 1.0 = «не тише обычного для себя» —
    # самый мягкий defensible-floor (self-нормировка, не абсолютный порог →
    # не подгонка). 0 = выключить свежий гейт (только 24h-фильтр). fail-open:
    # при сбое klines монету НЕ выкидываем (REST-хиккап не должен опустошать
    # вселенную). Канон рескан 15-60мин — у нас 5мин (universe_refresh_sec).
    universe_min_rvol: float = Field(default=1.0)
    # v0.18.19 (аудит A-4, P-4 одобрен пользователем): floor «минимум N монет».
    # На остывшем рынке range≥6% + RVOL≥1.0 вырождали вселенную в 1 монету
    # (NEARUSDT, 44/76 сделок за сутки) — концентрационный риск + sl_cooldown
    # запирает бота целиком. Если после фильтров+RVOL монет < N — ДОБИРАЕМ из
    # liquidity-pool (прошли turnover/spread/range-cap анти-памп — стражи
    # ликвидности НЕ ослабляются) самые волатильные по range24h. 3 = минимальная
    # диверсификация (idiosyncratic-движение одной монеты не доминирует);
    # reversible operational-lever, не валидированный эдж. 0 = выкл (старое
    # поведение «качество, не количество» без пола).
    universe_min_symbols: int = Field(default=3)

    # ─── Капитал / риск ──────────────────────────────────────────────────
    virtual_capital: float = Field(default=1000.0)
    # Размер сделки в USD (notional). Пользователь мыслит «лотами в $».
    # Минимум 10$ — мельче комиссия/спред съедают прибыль скальпа.
    position_usd: float = Field(default=100.0)
    min_position_usd: float = Field(default=10.0)
    max_leverage: int = Field(default=5)
    # Killswitch: дневной/совокупный лимит убытка. ≤0 = ВЫКЛЮЧЕН (v0.18.23,
    # запрос пользователя: на демо killswitch не нужен — total-лимит не
    # сбрасывается и навсегда блокирует форвард-тест; прод-значение 0 в
    # docker-compose). Для live вернуть через env (например 500/800).
    # Дефолты класса оставлены защитными — fail-safe вне compose.
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
    # v0.18.16: пер-стратегийный тип входа. ФЕЙДЫ (sweep_fade/density_bounce) —
    # maker (цена возвращается к лимитке, дёшево). ПРОБОЙ (density_break) — TAKER:
    # maker-лимитка ставится на СВОЮ сторону книги (long→best_bid, НИЖЕ цены), а
    # растущий пробой к ней не возвращается → 56% сигналов не наливались (audit C-06,
    # fill-rate 42.6%). Канон: «breakout strategies depend on speed; limit orders
    # often fail to fill during explosive breakouts» (QMMFX); momentum-вход требует
    # агрессии (Tradeify, daytrading.com «order joins a queue» = наш entry_timeout).
    # None → fallback на глобальный entry_order_type.
    density_break_entry_order_type: str = Field(default="market")
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
    # Пауза после стоп-аута перед повторным входом в ТУ ЖЕ сторону по символу.
    # v0.15.0: backtest 15д n=6325 (data/scalp_sl_cooldown.txt, filter=ema боевой):
    # повторный вход той же стороной сразу после SL — в среднем убыточен
    # («месть-перефейд» провалившегося уровня). Свип cooldown 0/60/180/300/600с:
    # gross/сделку растёт монотонно (+0.087→+0.116R), отсекаются именно
    # отрицательные re-entries (live-кейс XLMUSDT #816 SL→#817 SL за 3мин в ту же
    # сторону). 300с = колено кривой (net выходит на плато −0.121, не режет объём
    # как 600с). Канон: не перефейдить провалившийся уровень сразу
    # (Connors/Raschke 1995 «Street Smarts»). Противоположную сторону не трогаем
    # (реальный разворот ловим). 0 = выключить паузу.
    # Базовый дефолт для НЕ-fade страт (density_break/bounce). sweep_fade имеет
    # отдельное окно (см. sweep_fade_sl_cooldown_sec ниже).
    # v0.18.21: и окно, и сам ФАКТ стопа — пер-стратегийные (запрос пользователя
    # 2026-06-11): last_sl_close_ts фильтруется по strategy — SL фейда не глушит
    # пробой/баунс по тому же символу+стороне (раньше чужой стоп блокировал все
    # страты, density_break/bounce теряли сигналы, стата перемешивалась).
    sl_cooldown_sec: float = Field(default=300.0)
    # v0.18.14: для sweep_fade окно расширено 300с→3600с (60м). Исходная калибровка
    # v0.15.0 тестировала только ≤600с; диапазон 30–90м (канон по MR-фейду) не
    # проверялся. Sweep по реальной истории sweep_fade (n=829,
    # scripts/scalp_cooldown_sweep.py): Δnet vs выкл монотонно 5м +3.53 → 30м
    # +68.59 → 60м +103.77 → 90м +109.61 (после 60м прирост резко замедляется —
    # колено). Чистое окно n=93: 60м — пик (+40.01), 90м падает. Канон по
    # mean-reversion фейду: Fondeo (VWAP MR) «if stopped — done ≥60 min»;
    # quantfoundrylab kill-switch «2 SL → 45-min halt»; Connors/Raschke «Street
    # Smarts». По символу+стороне (как базовый кулдаун). Только sweep_fade:
    # density_break — пробой (момент), длинная пауза backwards.
    sweep_fade_sl_cooldown_sec: float = Field(default=3600.0)

    def sl_cooldown_for(self, strategy: str) -> float:
        """Пауза после SL по стратегии. Семейство sweep_fade* (fade, вкл. канон-
        вариант v0.18.20) — расширенное окно 60м (канон MR «if stopped — done
        ≥60 min» Fondeo + sweep n=829); остальные (density_break пробой,
        density_bounce — не валидировались на длинное окно) — базовый
        sl_cooldown_sec."""
        if strategy.startswith("sweep_fade"):
            return self.sweep_fade_sl_cooldown_sec
        return self.sl_cooldown_sec

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
    # v0.18.26 (шаг 2) — база sweep_fade: full reclaim 1.0 (CAP Rule 2, как canon).
    # Артефакт: scripts/scalp_backtest_regime.py --reclaim-frac sweep (06-10..15,
    # NEAR/ZEC/TAO/WLD, прод-фильтры, n≈950): 0.5→1.0 netR -128→-117 (эффект
    # слабый, в канон-сторону). ИЗОЛЯЦИЯ: только база (canon — свой
    # sweep_fade_canon_reclaim_frac; density reclaim не использует). Откат: =0.5.
    sweep_fade_reclaim_frac: float = Field(default=1.0)
    # v0.18.26 (B) — база sweep_fade пропускает фейд у round-уровня (round00/50).
    # Артефакт: scripts/scalp_backtest_regime.py --level-decomp (06-10..15, n=956,
    # 4 альта, прод-фильтры): round WR 35%/avgR -0.231 ХУЖЕ микро WR 42%/-0.063 —
    # инверсия канон-ожидания (в даунтренд-режиме round-уровни пробивают, а не
    # фейдятся; Connors/Raschke «не фейди сильный тренд»). Forward-test с откатом,
    # пользователь принял риск малой выборки (1 режим/5д, BUILDLOG v0.18.26).
    # ИЗОЛЯЦИЯ: только база (canon фейдит значимые уровни намеренно → round_gate
    # не ставится; density этот детектор не использует). Откат: =false.
    sweep_fade_skip_round: bool = Field(default=True)
    # Двухфазный детектор: сколько секунд держим «взвод» после свипа, ожидая
    # reclaim+разворот. Канон: разворот печатается в 1-3 свечах после свипа.
    # v0.14.0: 120→60с — bar-close убран (см. confirm_bar_sec), а 120с поднимали
    # ТОЛЬКО под него. Канон order-flow: shot-clock ~30с, нереализовавшийся сетап
    # бросаем (Kalena 2026; TradeAlgo). 60с = умеренный компромисс под наш
    # fade-темп (всё ещё 2× канона), не тянем протухший сетап.
    arm_timeout_sec: float = Field(default=60.0)
    # Подтверждение reclaim на ЗАКРЫТИИ бара (сек). v0.14.0: ВЫКЛЮЧЕНО (0).
    # v0.11.0 ждал закрытия 1м-бара чтобы убрать тиковый шум, НО канон order-flow
    # скальпа прямо против: «waiting for a candle close can price you out of the
    # move» (Kalena 2026, TradeAlgo) — подтверждать надо ЛЕНТОЙ, а не закрытием
    # бара. У нас лента уже есть: вход требует разворот CVD + ob_imbalance —
    # это и есть tape-подтверждение. Bar-close был ВТОРЫМ, избыточным тормозом,
    # который прайсил нас из движения (live-кейс BNB 2026-06-02: reclaim добивал
    # через ~1мин после истечения взвода в 70% случаев). 0 = вход по ленте.
    # >0 = вернуть ожидание закрытия N-сек бара (fallback).
    confirm_bar_sec: float = Field(default=0.0)
    # Как часто (сек) повторять «плейбук»-логи ожидания/удержания, чтобы видеть
    # ход стратегии простым языком, но не флудить (цикл крутится ~1с).
    narrate_interval_sec: float = Field(default=15.0)
    # Сколько ждать филлы выхода по приватному WS перед тем как послать
    # close-уведомление в Telegram с ОЦЕНКОЙ (пометка ≈). Обычно филлы
    # доезжают за ~1с и уведомление уходит с реальным net из reconcile.
    close_notify_fallback_sec: float = Field(default=10.0)
    # ─── REST-фолбэк реконсиляции provisional-PnL (v0.18.11) ──────────────
    # WS-леджер обнуляется рестартом контейнера → осиротевшие provisional-сделки
    # иначе зависают навсегда. Добиваем их через get_closed_pnl (REST) под
    # rate-limit (api-docs.mdc: historical 5/сек).
    # Горизонт назад, в пределах которого пробуем REST-досверку (< 7 дней —
    # лимит окна Bybit get_closed_pnl: endTime−startTime ≤ 7д).
    reconcile_rest_horizon_sec: float = Field(default=7 * 24 * 3600 - 3600)
    # Дать WS-пути шанс перед REST (свежие закрытия досверяются по стриму).
    reconcile_rest_grace_sec: float = Field(default=60.0)
    # Не ретраить одну и ту же сделку чаще, чем раз в N сек.
    reconcile_rest_retry_sec: float = Field(default=300.0)
    # Бюджет REST-запросов реконсиляции на цикл (под rate-limit).
    reconcile_rest_max_per_cycle: int = Field(default=3)
    # Полу-окно (сек) вокруг ts_close для запроса closed_pnl: матчим запись в
    # [ts_close−w, ts_close+w]. Узко = надёжный матч на 1-й странице + меньше
    # риск схватить чужую сделку того же qty (порча статы).
    reconcile_rest_window_sec: float = Field(default=180.0)
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
    # Фиксированный $-риск на сделку. v0.18.5: $1→$10 (запрос пользователя —
    # «более рискованные позиции»). При R≈0.3–0.44% notional≈$2.3–3.3k; killswitch
    # $500/день = ~50 SL до стопа торгов (запас под ~30–60 сделок/день при WR ~40%).
    # Откат: SCALP_RISK_PER_TRADE_USD=1.
    risk_per_trade_usd: float = Field(default=10.0)

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
    # БАЗОВЫЙ дефолт. density_break использует его как есть (выстоявшая→пробитая
    # стена). density_bounce имеет ОТДЕЛЬНОЕ окно (density_bounce_persist_sec).
    density_persist_sec: float = Field(default=10.0)
    # v0.18.15: пер-стратегийный persist для density_bounce. Канон density-фейда
    # (resting-стена держит цену) требует, чтобы плотность ВЫСТОЯЛА 20–30+ мин —
    # это и есть анти-спуфинг (Secret Terminal density-scalping «sitting 30+ min»;
    # Bookmap order-flow; QuantStrategy.io). Прежние 10с (наследие быстрого скальпа)
    # пропускали спуф-грейд стены. None → fallback на density_persist_sec (для
    # обратной совместимости тестов/конфигов). 1200с = 20м (нижняя граница канона,
    # наименее ограничительная; env override SCALP_DENSITY_BOUNCE_PERSIST_SEC).
    # ТОЛЬКО density_bounce: density_break (момент-пробой) остаётся на базовом окне.
    # [forward-test] на n=12 edge не валидирован — это канон-grounded форвард-тест.
    density_bounce_persist_sec: float = Field(default=1200.0)
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
    # v0.18.16: confirmation ложного пробоя для density_break (audit C-06).
    # Профи единогласно различают НАСТОЯЩИЙ пробой и liquidity-grab по
    # FOLLOW-THROUGH потоку: реальный = устойчивый объём/CVD в сторону пробоя;
    # grab = спайк объёма с затуханием и быстрый возврат (eplanetbrokers,
    # fntradinglab, GrandAlgo «stacked imbalances = institutional, иначе fakeout»;
    # «volume is the truth serum for breakouts»). Гейт: вход на пробое ТОЛЬКО если
    # CVD подтверждает направление (reversal_momentum за momentum_window_sec). Это
    # каноничный фильтр grab'ов, применимый КО ВСЕМ монетам (включая deep-мейджоры
    # BTC, чьи круглые пробои = grab чаще follow-through, cryptos.live) — без
    # отключения инструментов (≠ overfit под n=26 BTC). False = legacy (вход на
    # первом пересечении). [forward-test] edge на n→100. ТОЛЬКО density_break.
    # Окно follow-through = существующий momentum_window_sec (канон CVD-tape-shift,
    # тот же reversal_momentum, что sweep_fade) — НЕ вводим новое число.
    density_break_confirm_cvd: bool = Field(default=True)
    # v0.18.16 (C-06 #3): канон-гейт абсорбции для density_break. КАНОН (не наша
    # стата): пробой на ГЛУБОКОЙ/слоистой книге = liquidity-grab, т.к. resting-
    # ликвидность поглощает движение (Tradeify ES-deep→fade vs NQ-thin→breakout;
    # Bookmap «order-book imbalance precedes impulse / absorption fades breakouts»).
    # Структурный сигнал глубины — resting `ob_imbalance` (bid/(bid+ask) top-N):
    # вход разрешён ТОЛЬКО если книга НЕ застакана против пробоя (для long bids
    # доминируют ≥ob_imbalance_min; для short зеркально). На круглом уровне глубокого
    # мейджора (BTC) там жирная resting-ликвидность ПРОТИВ → гейт режет grab. Это
    # per-symbol и ЕДИНО для всех монет (BTC=ETH=alt) — НЕ инструмент-скип, НЕ P&L.
    # Порог = тот же research-grounded ob_imbalance_min (0.58), без нового числа.
    # ТОЛЬКО density_break (фейды свой ob-гейт имеют). False = legacy (без гейта).
    density_break_require_ob: bool = Field(default=True)
    # v0.18.25 (V1, close-confirmation): КАНОН-вход пробоя. Канон C-06 (GrandAlgo /
    # PriceActionNinja / Alpha Learning): «настоящий пробой = ЗАКРЫТИЕ свечи за
    # уровнем; avoid entering on the first touch — wait for confirmed close (and
    # retest)». Раньше входили на ПЕРВОМ касании уровня (broke = last>level на
    # тике) — это прямое нарушение канона: first-touch ловит фейкауты/grab'ы,
    # отсюда WR 13% при канон-ожидании ≥33%. Теперь пробой АРМИТСЯ и подтверждается
    # на ЗАКРЫТИИ бара: цена всё ещё за уровнем = настоящий пробой → вход; цена
    # вернулась = first-touch фейкаут → отбой. bar=0 → legacy (вход на тике).
    # 60с — прецедент v0.11.0 (bar-close для тикового бота, тот же confirm_bar_sec).
    # V1 = шаги 1-2 канон-входа (пробит уровень + закрытие за ним). V2 (следующий
    # шаг к канону, см. BUILDLOG): + ретест уровня лимиткой (шаг 3) — лучшая цена и
    # ещё жёстче фильтр фейкаутов. V1 — чистый ПРЕФИКС V2, без анти-канон логики.
    density_break_confirm_bar_sec: float = Field(default=60.0)
    # v0.18.29: per-strategy no-trade blacklist (CSV). Монеты, на которых
    # density_break НЕ генерит сигналы (обе стороны). Изолировано от вселенной —
    # другие страты (sweep_fade/density_bounce/…) эти символы торгуют как раньше.
    # Артефакт решения: deep-анализ 72 density_break-сделок (BUILDLOG_SCALP
    # 2026-06-28, /tmp/dbreak_timing.py). Timing доказал, что удлиннение confirm
    # НЕ поможет: SL-hit медиана 9.5мин ПОСЛЕ входа (вход уже после 60с confirm),
    # 91% ложных стопов бьётся ПОЗЖЕ confirm-окна → confirm их не ловит. Потеря
    # сконцентрирована: BTC 21 сделка 90% SL net −$192 (z=3.67, p<0.0003 vs
    # H0 WR=50%) = 85% всего минуса; ZEC 24 79% SL −$116; TAO 8 88% SL −$65.
    # Без них 19 сделок net +$147. Формально BTC/ZEC <100 → нарушение sample-size,
    # НО 90% SL статистически значимо и решение одобрено пользователем (правило
    # допускает disable <100 с обсуждения). Пусто = выкл. env
    # SCALP_DENSITY_BREAK_NO_TRADE_SYMBOLS. Reversible без деплоя кода.
    density_break_no_trade_symbols: str = Field(
        default="BTCUSDT,ZECUSDT,TAOUSDT")

    # ─── HTF-bias: трендовый фильтр старшего ТФ (аудит v0.9.3) ────────────
    # Канон CAP «без контекста CVD-дивергенция — шум» (gates 1–3); Murphy 1999
    # (EMA200 primary trend); Asness 2013 (mean-reversion в согласии с трендом).
    # Фейд берём ТОЛЬКО по тренду: long-fade при price>EMA200, short — ниже.
    # Контртренд (ловля ножа) блокируем. Гейт в main после resolve, fail-open
    # при сбое свечей. Без фильтра sweep_fade фейдил «в вакууме» (WR 29–40%).
    require_htf_trend: bool = Field(default=True)
    # v0.16.0: контекст-ТФ 1H→15m. Research: для СКАЛЬПА контекст ставят на 15m
    # (DYOR Academy «scalping: context 1h/15m», VWAP-pullback guide «EMA200 на
    # 15m для bias», ChartScout «scalping: 15m context / 5m setup / 1m entry»;
    # правило соотношения ТФ 1:4–1:6 — наш вход ~1м → контекст 5–15м, а 1H в ~60×
    # старше входа = слишком медленный). A/B на истории (15д, n=6220,
    # data/scalp_htf_ab.txt): EMA200-15m даёт gross +0.122R/сделку vs +0.087R у
    # 1H (~+40%) при том же числе сделок; 4/6 монет лучше (NEAR/ZEC сильнее всех).
    htf_interval: str = Field(default="15")   # 15m (Bybit kline interval)
    htf_ema_len: int = Field(default=200)      # EMA200 — primary trend (Murphy)
    # 15m-бар закрывается раз в 900с → refresh 120с быстро подхватывает новый бар
    # (Bybit get_kline rate-limit с запасом: ~13 символов/120с). Было 300с под 1H.
    htf_refresh_sec: float = Field(default=120.0)
    # v0.17.0: ADX режим-гейт ПОВЕРХ EMA (additive). EMA даёт направление, ADX —
    # СИЛУ тренда. Канон MR запрещает фейд в сильный тренд: «never fade a one-
    # timeframe trending market» (Connors/Raschke «Street Smarts» 1995; Dalton).
    # ADX(14) Wilder 1978: <20 диапазон, ≥25 established trend, ≥30 strong trend.
    # v0.18.9 (2026-06-05): порог 25→30 ПОСЛЕ перехода на SL ×2.0. ADX-корзинная
    # A/B (filter=ema, без гейта) на 2 окнах при ×2.0 (data/scalp_adx_buckets_x2_
    # jun.txt / _may.txt): корзина 25–30 прибыльна в ОБА окна (netR +18.3/+5.5),
    # а ≥30 нестабильна (+13.0/−18.1). С широким стопом фейд в умеренном тренде
    # (25–30) выживает → блокировать с 25 стало слишком строго; режем только ≥30
    # (Connors/Raschke: «never fade a STRONG trend», ≥30 = strong). Связано с ×2.0:
    # при ×1.0 25–30 была непостоянна (+7.6/−7.8). [ограничение] валидировано на
    # sweep_fade; density_bounce делит гейт, но почти не торгует — мониторить.
    htf_adx_gate: bool = Field(default=True)
    htf_adx_len: int = Field(default=14)       # ADX(14) — Wilder canonical
    htf_adx_max: float = Field(default=30.0)   # ≥30 = strong trend → фейд стоп
    # v0.18.4: АСИММЕТРИЧНЫЙ DMI-гейт направления только для ЛОНГОВ. Диагноз: live
    # sweep_fade-лонги катастрофа (20% WR), шорты прибыльны (54%) — EMA200-кросс
    # плохо ловит направление на даунтрендовых альтах (whipsaw на 15m, лаг на 1H),
    # пропускает контртренд-лонги в дип. Wilder DMI (+DI/−DI, 1978) — более быстрый
    # детектор доминирующей стороны, уже считается для ADX. Фикс: лонг разрешён,
    # только если EMA И +DI>−DI вверх; шорты остаются на чистом EMA (там EMA уже
    # хорош, −0.025R). A/B харнес (3 окна, data/scalp_di_long_gate.txt): лонги
    # avgR −0.092/−0.100/−0.098 (EMA) → +0.004/+0.023/−0.006 (C), шорты не тронуты;
    # net total на каждом окне лучше, на 1–3 июн даже +7.2R. ТОЛЬКО MR (htf_strats),
    # density_break не трогаем. Канон: комбинировать EMA+DMI для направления
    # (Wilder 1978; multi-confirmation trend filter).
    htf_di_long_gate: bool = Field(default=True)

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
    # density_break: ПЕР-СТРАТЕГИЙНЫЙ TP (v0.18.3). Глобальный 3.5R (выше) остаётся
    # для sweep_fade/density_bounce. Для density_break ставим 2.5R как LIVE
    # КАНОН (v0.18.10): = глобальному take_profit_r=3.5 (Философия B «winners run»;
    # асимметричный payoff, свип-разворот 2:1-4:1 CrossTrade, T1≈2-3R). density_break —
    # ЧИСТЕЙШАЯ Философия B (единственная страта с should_exit=None ради бега
    # победителей), поэтому канонический потолок для неё = 3.5R, как у глобального.
    # ИСТОРИЯ: в v0.18.3 ставили 2.5R по контрфактуалу n=25 (q_db_mfe) — это была
    # подгонка под ШУМ (no-data-fitting.mdc: n<100). Низкий кап 2.5R противоречит
    # самой философии «winners run» и не имеет канонического источника → ОТКАЧЕНО
    # к канону. Поле оставлено для конфиг-гибкости (env override).
    density_break_take_profit_r: float = Field(default=3.5)
    sl_buffer_bps: float = Field(default=8.0)  # буфер за свип-уровнем, б.п.
    # Research-ручка: множитель ИТОГОВОГО риска (ширины SL) после мин-R пола.
    # default 1.0 = no-op (алгебраически тождественно). Для A/B гипотезы «шире
    # стоп → выше WR» (буфер один не годится: мин-R пол ~0.44% его маскирует).
    # TP масштабируется вместе (tp_r × risk), $-риск постоянен (risk-sizing → лот).
    sl_risk_mult: float = Field(default=1.0)
    # ПЕР-СТРАТЕГИЙНЫЙ множитель ширины SL для sweep_fade. None = fallback на
    # глобальный sl_risk_mult (так харнес --sl-mult продолжает работать).
    # v0.18.8 ставил ×2.0 (MAE/Sweeney p85); v0.18.19 (аудит A-1, 2026-06-10):
    # ОТКАТ в прод на 1.0 — live-форвард n=134 (06-05→06-10, > порога 100)
    # опроверг харнес: сайзинг от ПОЛНОГО SL при ×2.0 сжимал win-сторону в $
    # вдвое (base_risk=$5 при риске $10) → фактический R:R 1.75:1, flow_exit
    # avg +$7.84 < sl_hit −$11.17, break-even WR 56% > live 52%. При ×1.0
    # R-единицы совпадают (flow_exit ≥+$15 > SL −$10). density_break и
    # density_bounce не задеты (идут по глобальному 1.0).
    sweep_fade_sl_risk_mult: float | None = Field(default=None)
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
    # 1.0→1.5 (v0.13.0): sweep 15д на истории (n до 9275, data/scalp_sweep.txt):
    # подъём порога монотонно укрупняет средний зафиксированный винер (flow_exit
    # avgR +1.05→+1.55→+2.02 при 1.0/1.5/2.0), avgR всей выборки маргинально
    # лучший на 1.5 (−0.219 vs −0.238). 1.5R = винер успевает добежать дальше к
    # TP=3.5R, флипы ниже 1.5R держим. Research-сторона «let winners run»
    # (Schwager/Brooks), НЕ пик-хантинг (кривая плоская по avgR — это направление,
    # форвард-валидация на live).
    flow_exit_activate_r: float = Field(default=1.5)
    # Scratch-при-ошибке (research «exit if wrong» + анализ 304 сделок
    # 2026-05-31): если сделка явно в МИНУСЕ (ход против ≥ round-trip комиссии)
    # И поток (CVD) развернулся против — режем убыток рано, не ждём SL/тайм-стоп.
    # Данные: убыточные тянулись до 91с (ср. −$0.167), а с разворотом ленты идут
    # к SL (−$0.467). Брать flat/мелкий минус НЕ скретчим (иначе −fee на шуме).
    # v0.13.0: ВЫКЛЮЧЕН (default False). Контрфактуал (n больших) + sweep 15д
    # (data/scalp_sweep.txt): чем меньше режем — тем выше WR и avgR (sa 0.7→0.85→
    # OFF: WR 36→41→43%, avgR −0.238→−0.227→−0.215; sa=1.0 ≡ OFF, т.к. порог=SL).
    # SL уже на −1R, scratch при −0.7R резал лишь 0.3R недохода до стопа, ловил
    # мало, но убивал ~12% сделок которые отскочили бы (противоречит MR-философии
    # «дождаться отскока»). Полагаемся на биржевой SL. Снижает число путей выхода
    # (Философия B: меньше дискреционных триггеров). Не подгонка под live n=43 —
    # OOS-история + контрфактуал; форвард-валидация на live.
    scratch_on_flow_flip: bool = Field(default=False)
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

    # ─── Старт ────────────────────────────────────────────────────────────
    # flatten_on_start=True (legacy): при старте ЗАКРЫТЬ по рынку все открытые
    # позиции и пометить open-сделки restart_flat. Минус: деплой/рестарт срезает
    # живую позицию посреди движения (кейс #926 BNBUSDT: шорт был +$1.05, но
    # рестарт закрыл его и записал pnl=0). False (v0.18.0, default): позиции НЕ
    # трогаем — биржевые SL/TP (вешаются на позицию в place_entry) защищают их и
    # дают дойти до TP/SL; manage() со след. цикла читает их из БД и продолжает
    # сопровождать (flow_exit/time-stop/bracket). Adopt-старт точечно снимает лишь
    # резящие НЕзаполненные maker-входы по сохранённому link (cancel_all НЕ
    # используем — аккаунт может быть общим).
    flatten_on_start: bool = Field(default=False)

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
    def no_long_list(self) -> list[str]:
        return [s.strip().upper() for s in self.no_long_symbols.split(",")
                if s.strip()]

    @property
    def density_break_no_trade_list(self) -> list[str]:
        return [s.strip().upper() for s in self.density_break_no_trade_symbols
                .split(",") if s.strip()]

    @property
    def sweep_fade_canon_symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.sweep_fade_canon_symbols.split(",")
                if s.strip()]

    @property
    def sweep_fade_run_symbol_list(self) -> list[str]:
        """Вселенная sweep_fade_run. Пусто → canon-список (A/B чистый:
        canon vs canon+exit). env SCALP_SWEEP_FADE_RUN_SYMBOLS."""
        if self.sweep_fade_run_symbols.strip():
            return [s.strip().upper() for s in self.sweep_fade_run_symbols.split(",")
                    if s.strip()]
        return self.sweep_fade_canon_symbol_list

    @property
    def sweep_fade_trend_symbol_list(self) -> list[str]:
        """Вселенная sweep_fade_trend. Пусто → canon-список (A/B чистый:
        canon vs canon+trend-gate). env SCALP_SWEEP_FADE_TREND_SYMBOLS."""
        if self.sweep_fade_trend_symbols.strip():
            return [s.strip().upper() for s in self.sweep_fade_trend_symbols.split(",")
                    if s.strip()]
        return self.sweep_fade_canon_symbol_list

    @property
    def strategy_list(self) -> list[str]:
        return [s.strip() for s in self.enabled_strategies.split(",") if s.strip()]

    @property
    def active_hours(self) -> set[int]:
        return {int(h) for h in self.active_hours_utc.split(",") if h.strip()}


def load_settings() -> ScalpSettings:
    return ScalpSettings()
