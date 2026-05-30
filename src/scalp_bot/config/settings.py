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
    # Funding |rate| порог «толпа перекошена» (Lambda Finance 2026: 0.05%
    # = лёгкий перекос; coinxsight 2026: 0.03% = over-leverage).
    funding_extreme: float = Field(default=0.0003)
    # Ликвидационный flush: суммарный размер ликвидаций (в USD) за окно.
    liq_flush_usd: float = Field(default=50000.0)
    liq_window_sec: float = Field(default=60.0)
    # Конфлюенс: сколько из 5 микро-правил должно совпасть для входа.
    min_confluence: int = Field(default=3)
    # Анти-шум между входами по одному символу.
    signal_cooldown_sec: float = Field(default=60.0)

    # ─── Управление позицией ─────────────────────────────────────────────
    # Тайм-стоп: скальп не должен «висеть» (tick-scalping 60-90с, b2broker).
    time_stop_sec: float = Field(default=90.0)
    # TP/SL в единицах R; SL ставится за свипнутый уровень + буфер.
    take_profit_r: float = Field(default=1.5)
    sl_buffer_bps: float = Field(default=8.0)  # буфер за свип-уровнем, б.п.

    # ─── Telegram (опционально, нотификации без поллинга команд) ─────────
    telegram_enabled: bool = Field(default=False)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]


def load_settings() -> ScalpSettings:
    return ScalpSettings()
