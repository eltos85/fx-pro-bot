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
    sl_cooldown_sec: float = Field(default=300.0)

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
    # ADX(14) Wilder 1978: <20 диапазон, ≥25 established trend. A/B 15д (n=6220→
    # 3104, data/scalp_adx_gate.txt): ema+adx@25 gross +0.140R/сделку vs +0.122R у
    # одного EMA (+15%), net −0.088 vs −0.100; пороги 30/35 выгоды не дают.
    htf_adx_gate: bool = Field(default=True)
    htf_adx_len: int = Field(default=14)       # ADX(14) — Wilder canonical
    htf_adx_max: float = Field(default=25.0)   # ≥25 = трендовый день → фейд стоп

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
    def strategy_list(self) -> list[str]:
        return [s.strip() for s in self.enabled_strategies.split(",") if s.strip()]

    @property
    def active_hours(self) -> set[int]:
        return {int(h) for h in self.active_hours_utc.split(",") if h.strip()}


def load_settings() -> ScalpSettings:
    return ScalpSettings()
