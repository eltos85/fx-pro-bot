"""Настройки tradecard_momentum (env-namespace ``TRADECARD_MOMENTUM_*``).

Advisory-ревьюер: НЕ торгует, НЕ тюнит конфиг momentum-бота. Все «пороги» здесь —
**пороги наблюдения/значимости** (квантили грейда, факторы кластеров, порог
монотонности), а не торговые параметры. Они нейтральные/структурные и **не**
подгоняются под желаемый P&L (no-data-fitting.mdc).

Креды cTrader берутся через свои ``TRADECARD_MOMENTUM_CTRADER_*`` с дефолтами на
``MOMENTUM_BOT_CTRADER_*`` (в docker-compose), чтобы аудит был раздельным
(TASKSPEC §10). Для конкурентного запуска (momentum + fx_ai_trader уже держат 2
коннекта на app) рекомендуется ОТДЕЛЬНОЕ cTrader-приложение (свой client_id),
иначе соединение упрётся в лимит «2 connections per application»
(https://help.ctrader.com/open-api/ — api-docs.mdc).
"""
from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradecardMomentumSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADECARD_MOMENTUM_",
                                      env_file=".env", extra="ignore")

    # ─── Инфраструктура ──────────────────────────────────────────────────
    # Свой volume tradecard (своя SQLite + markdown report card).
    data_dir: str = Field(default="/data")
    # БД momentum-бота монтируется read-only по отдельному пути (свой volume
    # momentum_bot_data, см. docker-compose): /bots/momentum.
    momentum_db_dir: str = Field(default="/bots/momentum")
    momentum_db_filename: str = Field(default="momentum_bot.sqlite")
    # Своя SQLite tradecard (темы/гипотезы/победы) — отдельный volume.
    db_filename: str = Field(default="tradecard_momentum.sqlite")
    log_level: str = Field(default="INFO")
    reports_dir: str = Field(default="/data/tradecard")

    # ─── DeepSeek (5 Why, read-only аналитика) ───────────────────────────
    deepseek_api_key: str = Field(default="")
    deepseek_model: str = Field(default="deepseek-v4-flash")
    deepseek_base_url: str = Field(default="https://api.deepseek.com/anthropic")
    deepseek_thinking: bool = Field(default=True)
    deepseek_max_tokens: int = Field(default=8192)
    five_why_enabled: bool = Field(default=True)

    # ─── cTrader (read-only: deal-list = ground truth по P&L) ────────────
    # Свои creds с дефолтами на momentum-бота (compose). Пусто = БД-сверка
    # отключена (broker_pnl_enabled игнорируется), отчёт без ground-truth P&L.
    broker_pnl_enabled: bool = Field(default=True)
    ctrader_host_type: str = Field(default="demo")
    ctrader_client_id: str = Field(default="")
    ctrader_client_secret: str = Field(default="")
    ctrader_account_id: int = Field(default=0)
    # Token-service (тот же централизованный OAuth refresh, что у ботов).
    token_service_url: str = Field(default="")
    token_service_secret: str = Field(default="")
    token_service_label: str = Field(default="tradecard_momentum")

    # ─── Атрибуция / реконструкция сделок ────────────────────────────────
    # Торговая вселенная momentum (его yfinance-символы) — для атрибуции
    # deal'ов на общем cTrader-счёте (deal не несёт label; делим по symbolId,
    # как scripts/momentum_pnl_audit.py). Дефолт = дефолт momentum-бота.
    momentum_symbols_raw: str = Field(
        default="EURUSD=X,GBPUSD=X,USDJPY=X,AUDUSD=X")
    # SL-множитель ATR для реконструкции планового риска (R-единицы): бот
    # считает sl_distance = atr × atr_stop_mult на входе (fx_momentum_bot/app/
    # main.py). momentum_position_state чистится при закрытии, поэтому риск
    # восстанавливаем из ATR закрытой сделки в momentum_decisions. Значение
    # = дефолт MOMENTUM_BOT_ATR_STOP_MULT (источник — конфиг бота, не подбор).
    atr_stop_mult: float = Field(default=2.5)
    # Окно match'а opening-deal ↔ executed momentum_decision (сек). Решение
    # логируется в том же цикле, что и открытие (add_decision сразу после
    # open). Берём щедрое окно (один poll-цикл бота ~300с).
    decision_match_window_sec: float = Field(default=900.0)

    # ─── Telegram ────────────────────────────────────────────────────────
    telegram_enabled: bool = Field(default=False)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")
    telegram_prefix: str = Field(default="[tradecard-momentum]")

    # ─── Baseline анализа (точка отсчёта = последняя правка логики) ───────
    # Сделки ДО baseline не анализируются (разные «стратегии» нельзя смешивать,
    # no-data-fitting + sample-size). Пусто = без нижней границы. Дата
    # обоснована артефактом (дата выката коммита, сменившего логику — BUILDLOG).
    # Формат: "YYYY-MM-DD" (полночь UTC) ИЛИ "YYYY-MM-DD HH:MM".
    baseline_date: str = Field(default="")

    # ─── Пороги наблюдения (НЕ торговые; нейтральные/относительные) ───────
    # sample-size.mdc: «тема»/«победа» только при выборке ≥ этих порогов.
    min_trades_for_theme: int = Field(default=100)
    min_days_for_theme: int = Field(default=14)
    significance_p: float = Field(default=0.05)

    # Грейдинг §5: число score-бакетов (квантильный маппинг A+/A/B/C).
    grade_buckets: int = Field(default=4)
    # Монотонность грейда: минимальный Spearman ρ кривой «грейд → EXP».
    grade_monotonic_min_rho: float = Field(default=0.5)

    # symbol_session_leak / general slice min trades.
    regime_leak_min_trades: int = Field(default=20)
    # loss_cluster: связка флагается, если её доля убытков ≥ factor × baseline.
    loss_cluster_factor: float = Field(default=1.5)
    loss_cluster_min_trades: int = Field(default=20)
    # overtrading: «горячий» час ≥ factor × медианы по активным часам.
    overtrading_min_trades: int = Field(default=20)
    overtrading_spike_factor: float = Field(default=2.0)
    # swap_drag: swap съедает ≥ этой доли |gross| на связке (overnight financing
    # на трендовых позициях, специфично для TSMOM hold-механики).
    swap_drag_min_trades: int = Field(default=20)
    swap_drag_min_frac: float = Field(default=0.2)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, self.db_filename)

    @property
    def momentum_db_path(self) -> str:
        return os.path.join(self.momentum_db_dir, self.momentum_db_filename)

    @property
    def momentum_symbols(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.momentum_symbols_raw.split(",")
                     if s.strip())

    @staticmethod
    def _parse_date(raw: str) -> float | None:
        from datetime import UTC, datetime
        raw = (raw or "").strip().replace("T", " ")
        if not raw:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=UTC).timestamp()
            except ValueError:
                continue
        return None

    def baseline_ts(self) -> float | None:
        return self._parse_date(self.baseline_date)

    def telegram(self) -> tuple[bool, str, str, str]:
        return (self.telegram_enabled, self.telegram_bot_token,
                self.telegram_chat_id, self.telegram_prefix)


def load_settings() -> TradecardMomentumSettings:
    return TradecardMomentumSettings()
