"""Конфигурация yorsh_bot (env-namespace ``YORSH_*``).

Пороги-эвристики (density kratnosti, persistence, spurt amplitude) —
**стартовые точки**, финальные значения только из калибровки на собранной
ленте с записью в BUILDLOG_YORSH.md (no-data-fitting.mdc). Канонических
порогов для нашего сетапа «регулярный прострел от genuine density» в
литературе нет — их нужно вывести из данных (см. аудит, раздел
«Качество источников»).
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class YorshSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YORSH_", extra="ignore")

    # ─── Инфраструктура ──────────────────────────────────────────────────
    data_dir: str = Field(default="/data")
    log_level: str = Field(default="INFO")

    # ─── Биржи / вселенная ───────────────────────────────────────────────
    exchanges: str = Field(default="mexc,bitget")
    # Лимит подписок на биржу (сверяться с лимитами WS в офиц. доке — M1/M2).
    max_symbols_per_exchange: int = Field(default=50)
    universe_refresh_hours: float = Field(default=6.0)
    # Фильтр оборота: живой, но низколиквидный (не топ-30 CMC).
    min_24h_volume_usd: float = Field(default=10_000.0)
    max_24h_volume_usd: float = Field(default=2_000_000.0)

    # ─── Сырая лента ─────────────────────────────────────────────────────
    raw_retention_days: int = Field(default=30)
    raw_max_gb: float = Field(default=20.0)

    # ─── Density-tracker (M4) ────────────────────────────────────────────
    # Кратность размера плотности к соседним уровням. Стартовая точка,
    # калибруется на собранной ленте.
    density_kratnosti: float = Field(default=5.0)
    # Порог genuine (из аудита п.2; калибруется на ленте).
    density_min_persistence_sec: float = Field(default=60.0)
    # Окно reappear для refill (iceberg) — стартовая точка, калибровать.
    density_refill_window_sec: float = Field(default=30.0)
    # Окно reappear на ДРУГОЙ цене для признака «прыгает» (spoof) — стартовая.
    density_move_window_sec: float = Field(default=10.0)
    # Скольких тиков best-price достаточно для «цена подошла» (spoof-pull) — стартовая.
    density_approach_ticks: float = Field(default=5.0)
    # Отношение cumulative traded / visible size для iceberg-mismatch — стартовая.
    density_mismatch_ratio: float = Field(default=3.0)
    # Сколько секунд плотность может отсутствовать (size=0) перед close — стартовая.
    density_gap_close_sec: float = Field(default=30.0)

    # ─── Сканер прострелов (M5) ──────────────────────────────────────────
    # Стартовый порог прострела (RisingWave engineering-эвристика; калибровать,
    # см. no-data-fitting.mdc). НЕ канон.
    spurt_min_amplitude_pct: float = Field(default=2.0)

    # ─── Временное (M1/M2 → удалится в M3) ───────────────────────────────
    # Список символов для коллектора до готовности universe-менеджера.
    # Пусто = нет подписок (старт M0). В M3 убирается.
    symbols_static: str = Field(default="")

    @property
    def exchange_list(self) -> list[str]:
        return [e.strip().lower() for e in self.exchanges.split(",")
                if e.strip()]

    @property
    def static_symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols_static.split(",")
                if s.strip()]


def load_settings() -> YorshSettings:
    return YorshSettings()
