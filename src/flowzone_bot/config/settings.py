"""Настройки flowzone_bot (env-namespace ``FLOWZONE_*``).

Параметры вынесены в env, но имеют обоснованные дефолты. Любой числовой порог,
влияющий на торговлю, обоснован каноном (ролик STRATEGY_FLOWZONE.md) или
канонической литературой Market Profile (Steidlmayer / Dalton). Изменение
торговых порогов = правка стратегии (strategy-guard.mdc): только с обоснованием.

Фаза 1 (каркас): инфраструктура + подключение + вселенная + риск/лимиты +
Telegram. Параметры Volume Profile / контекста / зон / триггера добавляются в
последующих фазах вместе с соответствующими движками (no unused params).
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FlowzoneSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLOWZONE_", extra="ignore")

    # ─── Инфраструктура ──────────────────────────────────────────────────
    data_dir: str = Field(default="/data")
    log_level: str = Field(default="INFO")

    # ─── Bybit (demo, креды ai_trader по умолчанию — см. docker-compose) ──
    bybit_api_key: str = Field(default="")
    bybit_api_secret: str = Field(default="")
    bybit_demo: bool = Field(default=True)
    bybit_category: str = Field(default="linear")
    bybit_testnet: bool = Field(default=False)  # public market-data сеть

    # Fallback-список символов (используется только при сбое авто-селектора).
    symbols: str = Field(default="BTCUSDT,ETHUSDT,SOLUSDT")

    # ─── Авто-селектор вселенной (переиспользуем scalp_bot/data/universe.py) ─
    # Канон демонстрировался на NQ — глубоко-ликвидном рынке; absorption/delta
    # print/big-trades ЧИТАЕМЫ ТОЛЬКО на ликвидности (STRATEGY §6.1, §6.3). Метод
    # range/RVOL-ротации тянет тонкие памп-альты, где footprint шумит и сигнал
    # деградирует. Поэтому по умолчанию авто-ротация ВЫКЛЮЧЕНА — торгуем
    # фиксированный список глубочайших перпов (BTC/ETH/SOL, поле ``symbols``) как
    # ближайший аналог NQ-глубины. Включить ротацию: FLOWZONE_AUTO_UNIVERSE_ENABLED
    # =true (форвард-эксперимент отбора, не канон).
    auto_universe_enabled: bool = Field(default=False)
    # Метод авто-отбора монет: "rvol" (штатный selector data/universe.py) |
    # "momentum" (ТОП по 24h росту/падению + оборот, БЕЗ анти-памп кэпа — метод
    # из ролика SerCrypto https://youtu.be/gCgYS-CsGWc, data/momentum_universe.py).
    # 2026-06-17: тестово ПЕРЕКЛЮЧЕНО на momentum (форвард-A/B отбора монет).
    # Меняет ТОЛЬКО список символов; стратегия flowzone не трогается. Откат:
    # FLOWZONE_UNIVERSE_METHOD=rvol. Решение «что лучше» — n≥100 (sample-size.mdc).
    universe_method: str = Field(default="rvol")
    # momentum-параметры (действуют при universe_method="momentum").
    momentum_min_turnover_usd: float = Field(default=50_000_000.0)
    momentum_min_change_pct: float = Field(default=0.0)
    momentum_max_spread_bps: float = Field(default=0.0)
    momentum_direction: str = Field(default="both")
    universe_top_n: int = Field(default=15)
    universe_refresh_sec: float = Field(default=300.0)
    universe_min_turnover_usd: float = Field(default=100_000_000.0)
    universe_pin_symbols: str = Field(default="")
    universe_min_range_pct: float = Field(default=6.0)
    universe_max_range_pct: float = Field(default=20.0)
    universe_max_spread_bps: float = Field(default=5.0)
    universe_min_rvol: float = Field(default=1.0)
    universe_min_symbols: int = Field(default=3)

    # ─── Капитал / риск (модель scalp_bot, §6 п.8 TASKSPEC; demo) ─────────
    virtual_capital: float = Field(default=1000.0)
    # Фиксированный $-риск на сделку: qty = risk_per_trade_usd / |entry−SL|
    # (Van K. Tharp «Trade Your Way to Financial Freedom» 2007 ch.11 — размер
    # как следствие стопа, а не вход).
    risk_per_trade_usd: float = Field(default=10.0)
    min_position_usd: float = Field(default=10.0)
    max_leverage: int = Field(default=5)
    # Killswitch: ≤0 = ВЫКЛЮЧЕН (demo — деньги виртуальные, total-лимит иначе
    # навсегда заблокировал бы форвард-тест). Для live вернуть через env.
    max_daily_loss_usd: float = Field(default=0.0)
    max_total_loss_usd: float = Field(default=0.0)
    max_open_positions: int = Field(default=2)
    # НЕ канон (в STRATEGY лимита частоты нет; §5.3/§8 поощряют reload). Generic
    # анти-overtrading гард из модели scalp (TASKSPEC §6 п.8). ≤0 = ВЫКЛЮЧЕН —
    # тогда темп входов держат только max_open_positions + per-symbol cooldown'ы.
    max_trades_per_hour: int = Field(default=5)

    # ─── Исполнение ──────────────────────────────────────────────────────
    # Фаза 1: observe по умолчанию (trading_enabled=False). Канон §1 TASKSPEC:
    # «Демо сначала… trading-enabled включать только после проверки первых
    # циклов». Включается через env FLOWZONE_TRADING_ENABLED=true.
    trading_enabled: bool = Field(default=False)
    entry_fill_timeout_sec: float = Field(default=8.0)
    # Цикл оценки: микроструктура читается из WS-кэша, без REST.
    eval_interval_sec: float = Field(default=1.0)
    # Анти-даблклик: пауза между входами по одному символу (сек).
    signal_cooldown_sec: float = Field(default=60.0)
    # Сколько ждать филлы выхода по WS перед close-уведомлением с оценкой (≈).
    # Биржевой bracket (TP/SL) детерминирован, но WS-исполнения могут атрибути-
    # роваться с задержкой → provisional-оценка (taker_pnl по mark_price) иногда
    # расходится знаком с реальным closedPnl (был случай #462: ≈-0.25 при tp_hit
    # → REST true-up +0.49). 30с дают REST-сверке время до fallback-уведомления.
    close_notify_fallback_sec: float = Field(default=30.0)

    # ─── Окна агрегации микроструктуры (data/aggregates.py) ──────────────
    # Окно (сек) хранения тиковых принтов для триггера absorption и детекции
    # big-trades (percentile размера за окно). Не профиль сессии (тот строится
    # инкрементально), а короткая «лента» для подтверждения в зоне.
    trade_window_sec: float = Field(default=300.0)
    # Стакан: число уровней для ob_imbalance (доп-фактор, не главный триггер).
    ob_levels: int = Field(default=25)

    # ─── Persist тиков (A2, канон §3 — per-swing профиль) ────────────────
    # Принты persist-ятся в SQLite ``prints`` для построения per-swing профиля
    # (окно [ts prev swing, now]). Background batched-flush из daemon-потока,
    # чтобы не блокировать WS-callback. Технические параметры объёма/темпа,
    # не торговые эджи.
    print_flush_interval_sec: float = Field(default=2.0)
    # Retention: принты старше порога удаляются (per-swing окно — часы внутри
    # сессии; 6ч — с запасом). 0 = без pruning (рост БД).
    print_prune_older_sec: float = Field(default=6 * 3600.0)

    # ─── Volume Profile + контекст аукциона (фаза 2, канон STRATEGY §2-3) ─
    # Value Area = ≈70% объёма вокруг POC. КАНОН Market Profile (Steidlmayer
    # «Markets & Market Logic» 1989; Dalton «Mind Over Markets» — value area =
    # одно стандартное отклонение ≈ 70% TPO/объёма). Это инвариант, не тюним.
    value_area_pct: float = Field(default=0.70)
    # Разрешение профиля: ширина ценовой корзины = tick_size × N. ТЕХНИЧЕСКИЙ
    # параметр гранулярности footprint (не торговый порог): footprint-профиль
    # строится по корзинам цен из исполненного потока (STRATEGY §6.3). Слишком
    # мелко = шум по корзинам, слишком крупно = размытый POC. 10 тиков —
    # умеренное разрешение; не подгонка под результат.
    vp_bucket_ticks: int = Field(default=10)
    # Контекст аукциона по ФОРМЕ профиля (STRATEGY §2; Steidlmayer/Dalton): тренд
    # = направленный acceptance ВНЕ value area — из объёма в хвостах профиля (ниже
    # VAL / выше VAH) доля ≥ accept_frac на одной стороне → аукцион в эту сторону;
    # симметрия → баланс (не торгуем). Режим читается по самому профилю (дневной
    # footprint), поэтому СТАБИЛЕН на откате к зоне reload (канон «второе движение»).
    # 0.70 = каноничная Value-Area-константа (value area ≈70% принятого объёма;
    # acceptance вне VA = направленное принятие той же грейд-доли). Не тюнинг под
    # P&L; reversible через env (no-data-fitting.mdc).
    context_accept_frac: float = Field(default=0.70)

    # ─── Поток: big-trades + absorption-триггер (фаза 3, канон STRATEGY §3-4) ─
    # Big trade = крупный исполненный принт (STRATEGY §3.3 «volume got support by
    # these big trades»). Порог ОТНОСИТЕЛЬНЫЙ (TASKSPEC §6.3: не magic-number) —
    # percentile размера сделок за окно. 0.90 = верхний дециль (institutional-
    # tail распределения размеров, практика footprint/order-flow). Нейтральный
    # относительный порог, не подгонка под P&L.
    big_trade_pct: float = Field(default=0.90)
    # Минимум сделок в окне для валидного percentile (иначе «крупное» на 2-3
    # принтах = шум). Технический анти-шум, не торговый порог.
    big_trade_min_samples: int = Field(default=20)
    # Absorption (STRATEGY §4): контр-сторона агрессирует, но поглощается и НЕ
    # двигает цену в свою сторону («failed buyers/sellers», deep trades в теле
    # свечи). Триггер требует: (1) контр-сторона ≥ absorption_min_counter_frac
    # объёма окна (она реально давила), (2) ≥1 крупная сделка контр-стороны (deep
    # trade), (3) цена НЕ прошла в сторону контр-агрессии (поглощена). 0.5 =
    # «большинство» — нейтральный порог. Окно = ТЕЛО M5-свечи: канон §4 «deep
    # trades in the body of the candle» + §6.3 (ТФ входа = M5). M5-бар = 300с →
    # absorption читается на масштабе свечи входа (канон-привязка, не подгонка P&L).
    absorption_window_sec: float = Field(default=300.0)
    absorption_min_counter_frac: float = Field(default=0.5)

    # ─── Зоны (confluence) + вход (фаза 4, канон STRATEGY §3.4, §4-5, §7) ─
    # Confluence «super strong area» = совпадение НЕСКОЛЬКИХ факторов. Канон §3.4
    # называет ровно ТРИ: *«confluence of value area high, big trades and delta
    # level. This one is a super strong area»*. Берём только такие сильные зоны →
    # порог = 3. Факторы: value_area (VAH/VAL), POC, ledge, delta, big_trades.
    # Инвариант канона — не тюним вниз без обсуждения.
    zone_min_confluence: int = Field(default=3)
    # Кластеризация близких уровней в одну зону: tolerance = bucket_size × N
    # тиков-корзин. Технический параметр близости (не торговый порог).
    zone_cluster_ticks: int = Field(default=5)
    # «Сильная» дельта-печать на уровне: |delta| ≥ delta_min_frac × объём корзины
    # (одно-сторонний поток на уровне — STRATEGY §3.2). 0.6 = выраженный перекос
    # (нейтрально, не подгонка). Корзина с max |delta| ≥ порога → фактор delta.
    zone_delta_min_frac: float = Field(default=0.6)
    # Буфер за зоной для стопа (STRATEGY §5.2 «стоп сразу ЗА зоной») —
    # технический анти-фитиль буфер ПОВЕРХ канон-масштаба. Сам масштаб стопа =
    # far_edge зоны + N × ширина зоны (канон «1-2-3 / 1-2-4 / 1-2-5», §5.2) —
    # задаётся ``sl_zone_mult`` ниже. 8 б.п. — нейтральный микро-буфер, не
    # торговый эдж; не заменяет канон-масштаб.
    sl_buffer_bps: float = Field(default=8.0)

    # ─── Масштаб стопа 1-2-3 / 1-2-4 / 1-2-5 (канон §5.2) ─────────────────
    # Канон: *«1-2-3, 1-2-4, 1-2-5, it depends on how much you want to be safe on
    # the stop-loss placement»*. Нотация автором численно не расшифрована; в
    # order-flow/Al Brooks практике стоп = «just beyond the structural level»,
    # масштабируется с размером структуры. Реализованная интерпретация (см.
    # STRATEGY §11.5): стоп = far_edge зоны + N × ширина зоны, где N = кратное
    # «безопасности»: 1 = «1-2-3» (минимум за зоной), 2 = «1-2-4», 3 = «1-2-5»
    # (дальше за зоной = безопаснее, хуже R). Selectable через env. Default 1
    # (ближайший к «стоп сразу за зоной»). Изменение = правка стратегии
    # (strategy-guard.mdc).
    sl_zone_mult: float = Field(default=1.0)

    # ─── R:R-фильтр (канон Fabervaale, шаг 5.1) ──────────────────────────
    # Минимальный reward/risk до входа. Канон: ролик cUTsoU-15Tc «The Simplest
    # Orderflow Trading Model» — «our real risk-to-reward… maybe it's 1 to 2,
    # 1 to 2.5»; chartfanatics AMT-strategy (Fabio) — «Reward-to-Risk 1:2.5 to
    # 1:5». Если swing-цель ближе к entry чем risk × min_rr — TP не окупает
    # риск (и при малом ходе даже round-trip fees, кейс #468: reward 0.47 /
    # risk 6.35 = 0.07 → tp_hit с убытком −1.59). 2.5 = каноничная нижняя
    # граница. Источник: research, не data-fitting (strategy-guard.mdc).
    min_rr: float = Field(default=2.5)

    # ─── Цели / swing / re-entry (фаза 5, канон §5.3, §8) ─────────────────
    # Цель = ближайшая swing-точка (STRATEGY §5.3). Swing = фрактал Bill Williams
    # «Trading Chaos» 1995: бар-экстремум выше/ниже N баров с каждой стороны.
    # 2 бара (left=right=2) — канонический фрактал Уильямса (инвариант, не тюним).
    swing_left: int = Field(default=2)
    swing_right: int = Field(default=2)
    # ТФ структуры/входа = M5 (канон §6.3, скриншот «5 Minuti»). Bybit get_kline
    # interval "5". limit — глубина для поиска swing-структуры.
    swing_kline_interval: str = Field(default="5")
    swing_kline_limit: int = Field(default=200)
    # TTL кэша klines на символ (M5-бар обновляется раз в 5 мин — частый refetch
    # бессмыслен и жжёт rate-limit). Технический параметр, не торговый.
    swing_cache_sec: float = Field(default=60.0)
    # Re-entry (STRATEGY §5.3 «re-entry… super strong swing point»): после
    # ВЫИГРЫШНОГО закрытия — короткий cooldown, чтобы быстро перезарядиться на
    # следующей зоне по тренду (вместо полного signal_cooldown_sec). Это отдельная
    # новая сделка, а НЕ частичная фиксация (канон: полный выход на swing point,
    # затем re-entry). Технический параметр темпа, не торговый эдж.
    reload_cooldown_sec: float = Field(default=10.0)

    # ─── Session gate (фаза 6, канон STRATEGY §6.1) ──────────────────────
    # Торгуем только в активные сессии London/NY (ликвидность нужна для absorption/
    # big-trades; вне сессий поток разрежен — §6.1, §8). Окна UTC (Bybit — UTC):
    # London ≈07:00-16:00, NY ≈12:00-21:00 (каноничные FX-сессии, BIS/Investopedia).
    # Пустая строка/выкл → круглосуточно. Окна — операционные, не торговый порог.
    session_gate_enabled: bool = Field(default=True)
    session_windows_utc: str = Field(default="07:00-16:00,12:00-21:00")

    # ─── Telegram (репорты в чат ai_trader, префикс [flowzone]) ──────────
    telegram_enabled: bool = Field(default=False)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")
    telegram_prefix: str = Field(default="[flowzone]")

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]

    @property
    def universe_pin_list(self) -> list[str]:
        return [s.strip().upper() for s in self.universe_pin_symbols.split(",")
                if s.strip()]


def load_settings() -> FlowzoneSettings:
    return FlowzoneSettings()
