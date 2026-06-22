"""Настройки tradecard_bybit (env-namespace ``TRADECARD_BYBIT_*``).

Advisory-ревьюер: НЕ торгует, НЕ тюнит конфиг ботов. Все «пороги» здесь —
**пороги наблюдения/значимости** (квантили грейда, baseline-факторы кластеров,
порог монотонности), а не торговые параметры. Они нейтральные/структурные и
**не** подгоняются под желаемый P&L (no-data-fitting.mdc).

Креды ботов берутся через свои `TRADECARD_BYBIT_*` с дефолтами на ключи ботов
(в docker-compose), чтобы аудит был раздельным (TASKSPEC §10).
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradecardBybitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADECARD_BYBIT_", extra="ignore")

    # ─── Инфраструктура ──────────────────────────────────────────────────
    # Свой volume tradecard (своя SQLite + markdown report card).
    data_dir: str = Field(default="/data")
    # БД ботов лежат в РАЗДЕЛЬНЫХ volume'ах (scalp_bot_data / flowzone_data),
    # каждый бот монтирует свой как /data. tradecard монтирует их read-only по
    # отдельным путям (см. docker-compose): /bots/scalp и /bots/flowzone.
    scalp_db_dir: str = Field(default="/bots/scalp")
    flowzone_db_dir: str = Field(default="/bots/flowzone")
    # Своя SQLite tradecard (темы/гипотезы/победы) — отдельный volume.
    db_filename: str = Field(default="tradecard_bybit.sqlite")
    log_level: str = Field(default="INFO")
    # Каталог для markdown report card (data/tradecard/...).
    reports_dir: str = Field(default="/data/tradecard")

    # ─── DeepSeek (5 Why, read-only аналитика) ───────────────────────────
    deepseek_api_key: str = Field(default="")
    deepseek_model: str = Field(default="deepseek-v4-flash")
    deepseek_base_url: str = Field(default="https://api.deepseek.com/anthropic")
    deepseek_thinking: bool = Field(default=True)
    deepseek_max_tokens: int = Field(default=8192)
    # Включать 5 Why (LLM). False — отчёт без LLM-диагностики (только агрегаты).
    five_why_enabled: bool = Field(default=True)

    # ─── Bybit (read-only: closedPnl + klines) ───────────────────────────
    # Свои ключи на каждый бот; дефолты на ключи ботов задаются в compose.
    scalp_bybit_api_key: str = Field(default="")
    scalp_bybit_api_secret: str = Field(default="")
    flowzone_bybit_api_key: str = Field(default="")
    flowzone_bybit_api_secret: str = Field(default="")
    bybit_demo: bool = Field(default=True)
    bybit_category: str = Field(default="linear")
    # Сверять net по Bybit closedPnl (full pagination). False — отчёт по
    # pnl_verified/pnl_usd из БД (БД = traceability, не ground truth).
    closed_pnl_enabled: bool = Field(default=True)
    # Включать post-exit MFE детектор exit_left_money (тянет klines).
    mfe_enabled: bool = Field(default=False)

    # ─── Telegram (раздельные конфиги ботов) ─────────────────────────────
    scalp_telegram_enabled: bool = Field(default=False)
    scalp_telegram_bot_token: str = Field(default="")
    scalp_telegram_chat_id: str = Field(default="")
    flowzone_telegram_enabled: bool = Field(default=False)
    flowzone_telegram_bot_token: str = Field(default="")
    flowzone_telegram_chat_id: str = Field(default="")

    # ─── Baseline анализа (точка отсчёта = последняя правка логики) ───────
    # Сделки ДО baseline не анализируются: до правки логики это «другая
    # стратегия», смешивать через границу нельзя (no-data-fitting + sample-size).
    # Пусто = без нижней границы. Дата обоснована артефактом (дата выката
    # коммита, сменившего логику — BUILDLOG_SCALP/FLOWZONE), а не подбором.
    #
    # Bot-wide точка отсчёта (UTC) — fallback для стратегий без своей даты.
    # Формат: "YYYY-MM-DD" (полночь) ИЛИ "YYYY-MM-DD HH:MM" (если логика выкатилась
    # в середине дня — отсекаем сделки ДО выката того же дня):
    scalp_baseline_date: str = Field(default="")
    flowzone_baseline_date: str = Field(default="")
    # Per-strategy даты (у страт scalp разные даты правок логики). Формат:
    # "strategy=YYYY-MM-DD,strategy2=YYYY-MM-DD" (напр.
    # "sweep_fade=2026-06-17,density_break=2026-06-15"). Приоритетнее bot-wide.
    scalp_baseline_dates: str = Field(default="")
    flowzone_baseline_dates: str = Field(default="")

    # ─── Пороги наблюдения (НЕ торговые; нейтральные/относительные) ───────
    # sample-size.mdc: «тема»/«победа» только при выборке ≥ этих порогов.
    min_trades_for_theme: int = Field(default=100)
    min_days_for_theme: int = Field(default=14)
    significance_p: float = Field(default=0.05)

    # Грейдинг §5: число score-бакетов (квантильный маппинг A+/A/B/C).
    grade_buckets: int = Field(default=4)
    # Монотонность грейда: минимальный ранговый коэффициент (Spearman) кривой
    # «грейд → EXP», ниже которого считаем грейд непредиктивным. Нейтральный
    # структурный порог (0.5 = «хотя бы умеренно монотонна»), не под P&L.
    grade_monotonic_min_rho: float = Field(default=0.5)

    # sl_cluster: связка флагается, если её доля sl_hit ≥ factor × baseline
    # (baseline = общая доля sl_hit по страте) при n ≥ min. Относительный порог.
    sl_cluster_factor: float = Field(default=1.5)
    sl_cluster_min_trades: int = Field(default=20)

    # regime_leak: срез страты системно убыточен (EXP < 0) при общем плюсе
    # страты, n ≥ min. Структурный знак, без magic-числа на P&L.
    regime_leak_min_trades: int = Field(default=20)

    # factor_noise: токен reasons — кандидат на шум, если |EXP_with − EXP_without|
    # ниже доли от |EXP| страты и WR-разница мала. Относительный порог.
    factor_noise_max_exp_frac: float = Field(default=0.1)
    factor_noise_min_trades: int = Field(default=30)

    # exit_left_money: medians MFE_after_exit ≥ factor × реализованного хода.
    exit_left_money_factor: float = Field(default=2.0)
    exit_left_money_min_trades: int = Field(default=20)

    # overtrading: «горячий» час = число сделок ≥ factor × медианы по активным
    # часам; сравниваем EXP горячих vs спокойных. Относительный структурный
    # порог (self-нормировка по медиане), не под P&L.
    overtrading_min_trades: int = Field(default=20)
    overtrading_spike_factor: float = Field(default=2.0)

    # big_game_hunting: top-грейд считается «редким», если его доля сделок ниже
    # этого порога. Структурный порог доли (канон §8 «A+ редки»), не под P&L.
    big_game_max_top_share: float = Field(default=0.15)
    big_game_min_trades: int = Field(default=30)

    # paper_live_divergence: связка валидна на paper (EXP>0), но системно
    # проигрывает на live (EXP<0); n ≥ min на каждой стороне.
    paper_live_min_trades: int = Field(default=20)

    @property
    def db_path(self) -> str:
        import os
        return os.path.join(self.data_dir, self.db_filename)

    def bot_db_path(self, bot: str) -> str:
        import os
        if bot == "scalp":
            return os.path.join(self.scalp_db_dir, "scalp_bot.sqlite")
        return os.path.join(self.flowzone_db_dir, "flowzone_bot.sqlite")

    @staticmethod
    def _parse_date(raw: str) -> float | None:
        """Baseline в UTC. Принимает дату (``YYYY-MM-DD`` = полночь) ИЛИ дату+время
        (``YYYY-MM-DD HH:MM`` / ``YYYY-MM-DDTHH:MM`` [:SS]) — время нужно, когда
        логика выкатилась в середине дня и сделки ДО выката надо отсечь."""
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

    def _baseline_map(self, bot: str) -> dict[str, float]:
        """Per-strategy baseline-даты бота: {strategy: epoch}."""
        raw = self.scalp_baseline_dates if bot == "scalp" else self.flowzone_baseline_dates
        out: dict[str, float] = {}
        for part in (raw or "").split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            strat, _, date = part.partition("=")
            ts = self._parse_date(date)
            if ts is not None:
                out[strat.strip()] = ts
        return out

    def baseline_ts(self, bot: str, strategy: str | None = None) -> float | None:
        """Epoch-нижняя граница анализа: per-strategy дата (если есть) →
        bot-wide дата → None. Per-strategy приоритетнее (у страт scalp разные
        даты правок логики)."""
        if strategy is not None:
            ps = self._baseline_map(bot).get(strategy)
            if ps is not None:
                return ps
        bot_wide = (self.scalp_baseline_date if bot == "scalp"
                    else self.flowzone_baseline_date)
        return self._parse_date(bot_wide)

    def min_baseline_ts(self, bot: str) -> float | None:
        """Наименьшая из всех baseline-дат бота (для нижней границы загрузки)."""
        candidates = list(self._baseline_map(bot).values())
        bw = self._parse_date(self.scalp_baseline_date if bot == "scalp"
                              else self.flowzone_baseline_date)
        if bw is not None:
            candidates.append(bw)
        return min(candidates) if candidates else None

    def bybit_keys(self, bot: str) -> tuple[str, str]:
        if bot == "scalp":
            return self.scalp_bybit_api_key, self.scalp_bybit_api_secret
        return self.flowzone_bybit_api_key, self.flowzone_bybit_api_secret

    def telegram_for(self, bot: str) -> tuple[bool, str, str, str]:
        """(enabled, token, chat_id, prefix) для бота."""
        if bot == "scalp":
            return (self.scalp_telegram_enabled, self.scalp_telegram_bot_token,
                    self.scalp_telegram_chat_id, "[tradecard-scalp]")
        return (self.flowzone_telegram_enabled, self.flowzone_telegram_bot_token,
                self.flowzone_telegram_chat_id, "[tradecard-flowzone]")


def load_settings() -> TradecardBybitSettings:
    return TradecardBybitSettings()
