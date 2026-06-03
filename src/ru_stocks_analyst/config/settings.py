"""Настройки ru_stocks_analyst (env-prefix ``RU_STOCKS_*``)."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuStocksSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="RU_STOCKS_",
        extra="ignore",
    )

    # ─── Tinkoff Invest API (REST) ───────────────────────────────────────
    # Токен: https://www.tbank.ru/invest/settings/ → «Токен для API»
    # Документация: https://developer.tbank.ru/invest/intro/intro/
    tinkoff_token: str = Field(default="")
    # Пусто = авто-выбор первого брокерского (ACCOUNT_TYPE_TINKOFF, не ИИС)
    account_id: str = Field(default="")
    api_base_url: str = Field(
        default="https://invest-public-api.tinkoff.ru/rest",
    )
    use_sandbox: bool = Field(default=False)

    # ─── Скринер ─────────────────────────────────────────────────────────
    universe_top_n: int = Field(default=50)
    min_price_rub: float = Field(default=50.0)
    candle_days: int = Field(default=60)
    max_ideas_per_digest: int = Field(default=5)
    risk_per_trade_pct: float = Field(default=10.0)

    # ─── Расписание ──────────────────────────────────────────────────────
    poll_interval_sec: int = Field(default=3600)
    morning_digest_hour_msk: int = Field(default=9)
    morning_digest_minute_msk: int = Field(default=5)

    # ─── Новости (RSS) ───────────────────────────────────────────────────
    news_enabled: bool = Field(default=True)
    news_max_age_hours: int = Field(default=36)
    news_cache_ttl_sec: int = Field(default=600)
    rss_feeds_raw: str = Field(
        default="",
        description="CSV name|url; пусто = встроенные ленты",
    )

    # ─── LLM (опционально) ───────────────────────────────────────────────
    llm_enabled: bool = Field(default=True)
    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field(default="deepseek-chat")
    deepseek_base_url: str = Field(default="https://api.deepseek.com")

    # ─── Telegram ────────────────────────────────────────────────────────
    telegram_enabled: bool = Field(default=True)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # ─── Инфра ───────────────────────────────────────────────────────────
    data_dir: str = Field(default="./data/ru_stocks")
    log_level: str = Field(default="INFO")
    dry_run: bool = Field(default=False)

    @property
    def effective_api_base(self) -> str:
        if self.use_sandbox:
            return "https://sandbox-invest-public-api.tinkoff.ru/rest"
        return self.api_base_url.rstrip("/")

    def parse_rss_feeds(self) -> tuple | None:
        """Пустая строка → None (дефолтные ленты в RuNewsAggregator)."""
        raw = (self.rss_feeds_raw or "").strip()
        if not raw:
            return None
        from ru_stocks_analyst.news.rss import FeedSource

        feeds = []
        for part in raw.split(","):
            part = part.strip()
            if "|" in part:
                name, url = part.split("|", 1)
                feeds.append(FeedSource(name.strip(), url.strip()))
            elif part.startswith("http"):
                feeds.append(FeedSource(part[:24], part))
        return tuple(feeds) if feeds else None


def load_settings() -> RuStocksSettings:
    return RuStocksSettings()
