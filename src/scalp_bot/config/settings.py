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
    enabled_strategies: str = Field(default="sweep_fade")

    # ─── Авто-селектор вселенной (data/universe.py) ──────────────────────────
    # Если включён — бот сам выбирает монеты под стратегию из get_tickers, а
    # ``symbols`` используется лишь как fallback при сбое API. Пороги привязаны
    # к математике fee-guard и live-границе (BUILDLOG_SCALP 2026-05-30), а НЕ
    # подгоняются под прошлый P&L (no-data-fitting.mdc).
    auto_universe_enabled: bool = Field(default=True)
    universe_top_n: int = Field(default=5)
    universe_refresh_sec: float = Field(default=3600.0)  # пересмотр раз в час
    universe_min_turnover_usd: float = Field(default=150_000_000.0)
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
    # обнуления $1000 депо), max 2 позиции, 20 сделок/час (анти-overtrade).
    max_daily_loss_usd: float = Field(default=500.0)
    max_total_loss_usd: float = Field(default=800.0)
    max_open_positions: int = Field(default=2)
    max_trades_per_hour: int = Field(default=20)

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
    # Funding-перекос толпы (АСИММЕТРИЧНО, research 2026):
    # crowded LONG = funding ≥ +0.05% (TraderSpy/Altrady) → фейдим ШОРТОМ;
    # crowded SHORT = funding ≤ −0.03% → фейдим ЛОНГОМ.
    # https://blog.traderspy.app/en/blog/crypto-funding-rates-secret-weapon/
    funding_extreme_pos: float = Field(default=0.0005)  # порог для short-fade
    funding_extreme_neg: float = Field(default=0.0003)  # порог для long-fade
    # Ликвидационный flush: суммарный размер ликвидаций (в USD) за окно.
    liq_flush_usd: float = Field(default=50000.0)
    liq_window_sec: float = Field(default=60.0)
    # Конфлюенс: сколько из 5 микро-правил должно совпасть для входа.
    min_confluence: int = Field(default=3)
    # Анти-шум между входами по одному символу.
    signal_cooldown_sec: float = Field(default=60.0)

    # ─── Подтверждение разворота (sweep-and-reclaim, CAP-протокол) ────────
    # «Не входи во время свипа — жди возврата за уровень + разворота ленты».
    # Источники: chartwhisperer CAP 5-rule protocol (Rule 2 reclaim, Rule 5
    # CHoCH), CrossTrade, Kalena (tape-shift), Quantum-Algo. Главный фикс
    # «ловли ножа»: detect_sweep ловит экстремум, но без reclaim бот мог
    # входить в реальный пробой.
    require_reclaim: bool = Field(default=True)
    # Доля возврата цены от свип-экстремума к свипнутому уровню (0..1).
    reclaim_frac: float = Field(default=0.5)
    # Двухфазный детектор: сколько секунд держим «взвод» после свипа, ожидая
    # reclaim+разворот. Канон: разворот печатается в 1-3 свечах после свипа.
    arm_timeout_sec: float = Field(default=60.0)
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
    # Round-trip издержки. С маркет-входом (SCALP_ENTRY_ORDER_TYPE=market) обе
    # ноги — TAKER: 0.055% × 2 = 0.11%. Подтверждено реальными сделками бота
    # 2026-05-30 (openFee+closeFee ≈ 0.109$ на $100). Раньше стоял 0.075%
    # (maker-вход + taker-выход) — недооценивал издержки на маркете.
    # Источники: liberatedstocktrader, 1minscalper, VT Markets (цель ≥3×).
    # Сигнал отбрасывается, если ход до TP < min_target_fee_mult × round_trip.
    round_trip_fee_frac: float = Field(default=0.0011)
    min_target_fee_mult: float = Field(default=3.0)

    # ─── density_bounce (стратегия №2: отскок от плотности в стакане) ─────
    # Стена = крупная лимитка ≥ wall_mult × средний размер уровня на своей
    # стороне (top-N). Kalena 2026: «relative sizing», порог 5–8× среднего за
    # 10–15 мин; берём консервативный край 8×. arXiv 2604.20949: depth-сигналы
    # причинно раньше flow. https://blog.kalena.ai/crypto-wall-detection-...
    density_wall_mult: float = Field(default=8.0)
    # Близость стены к круглому числу (доля цены). Данилов: плотности на
    # круглых уровнях надёжнее как S/R.
    density_round_frac: float = Field(default=0.001)  # 0.1%
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

    # ─── Сессионный фильтр (опционально, default OFF) ─────────────────────
    # Канон: свипы доходят в London/NY open + overlap, «мёртвые» часы дают
    # ложные. Crypto 24/7 + строгий конфлюенс → по умолчанию ВЫКЛ, чтобы не
    # уморить частоту. Включать при достаточной статистике.
    session_filter_enabled: bool = Field(default=False)
    # Активные UTC-часы (London 07-10, NY 13-16 + overlap 12-16).
    active_hours_utc: str = Field(default="7,8,9,12,13,14,15,16")

    # ─── Управление позицией ─────────────────────────────────────────────
    # Тайм-стоп: скальп не должен «висеть» (tick-scalping 60-90с, b2broker).
    time_stop_sec: float = Field(default=90.0)
    # TP/SL в единицах R; SL ставится за свипнутый уровень + буфер.
    # 2.0R — канон для свип-разворота (CrossTrade 2:1–4:1, chartwhisperer
    # T1≈2-3R). Ранее 1.5R — после комиссий edge слишком тонкий.
    take_profit_r: float = Field(default=2.0)
    sl_buffer_bps: float = Field(default=8.0)  # буфер за свип-уровнем, б.п.
    # Активный выход (hard invalidation): закрыть раньше тайм-стопа, если
    # ордер-флоу (CVD) развернулся ПРОТИВ позиции. Все скальп-источники:
    # «exit immediately when order flow flips» (Kalena, tradezella, tradealgo).
    active_exit_enabled: bool = Field(default=True)
    active_exit_min_age_sec: float = Field(default=10.0)  # не дёргаться на шуме

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
    def strategy_list(self) -> list[str]:
        return [s.strip() for s in self.enabled_strategies.split(",") if s.strip()]

    @property
    def active_hours(self) -> set[int]:
        return {int(h) for h in self.active_hours_utc.split(",") if h.strip()}


def load_settings() -> ScalpSettings:
    return ScalpSettings()
