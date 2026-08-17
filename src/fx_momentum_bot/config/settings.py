from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MomentumBotSettings(BaseSettings):
    """Isolated settings for momentum bot.

    Uses only MOMENTUM_BOT_* variables to avoid overlap with legacy bots.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = Field(default="INFO", validation_alias="MOMENTUM_BOT_LOG_LEVEL")
    trading_enabled: bool = Field(
        default=False, validation_alias="MOMENTUM_BOT_TRADING_ENABLED"
    )
    poll_interval_sec: int = Field(
        default=300, validation_alias="MOMENTUM_BOT_POLL_INTERVAL_SEC"
    )
    symbols_raw: str = Field(
        default="EURUSD=X,GBPUSD=X,USDJPY=X,AUDUSD=X",
        validation_alias="MOMENTUM_BOT_SYMBOLS",
    )
    yfinance_interval: str = Field(
        default="1h", validation_alias="MOMENTUM_BOT_YFINANCE_INTERVAL"
    )
    yfinance_period: str = Field(
        default="3mo", validation_alias="MOMENTUM_BOT_YFINANCE_PERIOD"
    )

    momentum_lookback_bars: int = Field(
        default=24, validation_alias="MOMENTUM_BOT_LOOKBACK_BARS"
    )
    atr_period: int = Field(default=14, validation_alias="MOMENTUM_BOT_ATR_PERIOD")
    atr_stop_mult: float = Field(
        default=2.5, validation_alias="MOMENTUM_BOT_ATR_STOP_MULT"
    )
    # НЕ используется с 2026-06-10: брокерский TP у momentum-входов убран
    # (стоял на 1.4R и закрывал позицию до активации partial/trailing@1.5R —
    # runner был мёртвым кодом). Выход ведёт сопровождение: BE@1R +
    # partial@1.5R + ATR-trailing (Raschke partial+runner, LeBeau Chandelier).
    # Поле оставлено для совместимости env, согласовано с пользователем.
    atr_take_mult: float = Field(
        default=3.5, validation_alias="MOMENTUM_BOT_ATR_TAKE_MULT"
    )
    signal_threshold: float = Field(
        default=0.0015, validation_alias="MOMENTUM_BOT_SIGNAL_THRESHOLD"
    )

    # Fallback-лот, если ATR-сайзинг выключен (risk_per_trade_usd=0).
    lot_size: float = Field(default=0.01, validation_alias="MOMENTUM_BOT_LOT_SIZE")
    # ─── ATR-scaled sizing (Tharp ch.11, Vince 1992) ───
    # Фиксированный риск $ на сделку, лот пересчитывается из SL-дистанции:
    # выравнивает риск между инструментами с разной ценой пункта. Канон
    # fixed-fractional risk уже реализован у advisor (calc_lot_size, риск
    # $15 = 1% от $1500) — переиспользуем ту же функцию. lot = risk /
    # (sl_pips × pip_value); cap MAX_LOT_SIZE=0.05 (инцидент 23.04).
    # 0 = выключить (фикс-лот).
    risk_per_trade_usd: float = Field(
        default=15.0, validation_alias="MOMENTUM_BOT_RISK_PER_TRADE_USD"
    )
    max_lot_size: float = Field(
        default=0.05, validation_alias="MOMENTUM_BOT_MAX_LOT_SIZE"
    )
    # ─── Spread-guard на входе ───
    # Скип входа, если live bid/ask спред > доли SL-дистанции: спред —
    # прямой вычет из R (вход по ask, SL/выход по bid). При 10% спреда
    # к риску система 2:1 теряет ~0.1R на сделку до начала торговли
    # (cost-to-risk контроль, Harris «Trading and Exchanges» 2003 ch.21).
    # Естественно блокирует ночь/роллувер 17:00 ET/пост-релизные минуты —
    # меряем фактический спред, не хардкодим часы. 0 = выключить.
    max_spread_risk_fraction: float = Field(
        default=0.10, validation_alias="MOMENTUM_BOT_MAX_SPREAD_RISK_FRACTION"
    )
    # Лимит одновременно открытых momentum-позиций (по всем символам).
    max_open_positions: int = Field(
        default=3, validation_alias="MOMENTUM_BOT_MAX_OPEN_POSITIONS"
    )
    order_label: str = Field(
        default="momentum-bot", validation_alias="MOMENTUM_BOT_ORDER_LABEL"
    )
    # Broker-side label (cTrader ProtoOAPosition.label) для ИЗОЛЯЦИИ позиций
    # на общем счёте: и fx_momentum_bot, и fx_ai_trader сидят на одном
    # cTrader-аккаунте. fx_ai_trader фильтрует свои позиции по label
    # "ai-fx-trader"; momentum обязан помечать свои отдельным label и
    # управлять/считать ТОЛЬКО их (иначе подхватит XAUUSD-сделки AI на
    # общем счёте). См. BUILDLOG.md 2026-06-09.
    position_label: str = Field(
        default="momentum-bot", validation_alias="MOMENTUM_BOT_POSITION_LABEL"
    )
    # Legacy-label: до введения position_label momentum открывал ордера через
    # общий executor с дефолтным label="fx-pro-bot". Чтобы бот продолжал вести
    # СВОИ позиции, открытые до миграции (BE/трейлинг), управляем и этим label.
    # Изоляция от fx_ai_trader сохраняется (у него label="ai-fx-trader").
    # Advisor тоже "fx-pro-bot", но по deploy-правилу profile=disabled (не
    # торгует). Пусто = вести только position_label.
    manage_legacy_label: str = Field(
        default="fx-pro-bot", validation_alias="MOMENTUM_BOT_MANAGE_LEGACY_LABEL"
    )

    @property
    def managed_labels(self) -> frozenset[str]:
        """Набор broker-label, которые бот считает своими (управление + счёт)."""
        return frozenset(
            lbl for lbl in (self.position_label, self.manage_legacy_label) if lbl
        )
    # ─── Event-guard: блок входов вокруг HIGH-impact релизов ───
    # Окно ±60 мин по Andersen et al. 2003 (пик реакции и волатильности):
    # вход momentum в момент релиза ловит шип/фейкаут. Блокируются ТОЛЬКО
    # входы; сопровождение и выходы работают. Per-symbol scoping: US-релизы
    # (CPI/FOMC/NFP) блокируют все пары, ECB — только EUR, BoJ — только JPY.
    # См. src/fx_momentum_bot/strategy/event_guard.py.
    news_block_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_NEWS_BLOCK_ENABLED"
    )
    news_block_before_min: int = Field(
        default=60, validation_alias="MOMENTUM_BOT_NEWS_BLOCK_BEFORE_MIN"
    )
    news_block_after_min: int = Field(
        default=60, validation_alias="MOMENTUM_BOT_NEWS_BLOCK_AFTER_MIN"
    )

    # ─── Gap-защита: закрытие открытых позиций перед high-impact новостями ──
    # (BUILDLOG 2026-07-24). Event-guard блокирует только ВХОДЫ; открытая
    # позиция ловит шип релиза → SL не исполняется в точке → gap за SL (beyond_sl).
    # Loss-audit 13.07-24.07: 2 beyond_sl (07-14 12:30 UTC) = −$51.6 = 36% убытка.
    # Если HIGH-релиз в следующие news_close_before_min минут → закрыть открытые
    # позиции scoped-символов (US — все; ECB — EUR-пары; BoJ — JPY-пары), как
    # friday_flat. Сопровождение (BE/partial/trailing) отрабатывает до закрытия.
    # Research: Andersen et al. 2003 (news overreaction + gap); FX Foundations
    # (slippage on fill). Обратимо: enabled=False. before_min=0 → выключено.
    news_close_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_NEWS_CLOSE_ENABLED"
    )
    news_close_before_min: int = Field(
        default=5, validation_alias="MOMENTUM_BOT_NEWS_CLOSE_BEFORE_MIN"
    )

    # ─── Session-фильтр входов (liquid sessions only) ───────────────────
    # Блок НОВЫХ входов вне ликвидных FX-сессий (London 07–12 UTC / NY
    # 12–21 UTC). Asian session (00–07 UTC) и Late (21–24) — тонкая
    # ликвидность, momentum-сигналы ложные: эмпирически (cTrader deal-list,
    # 2026-06-01..26, 77 сделок) Asia = 0% WR по GBPUSD/AUDUSD, −$109 net;
    # NY session AUDUSD = 60% WR +$45. Канон — STRATEGIES.md стр.173
    # (Liquid session filter для FX mean-reversion: Asian session = ловля
    # падающего ножа). Блокируются ТОЛЬКО входы; сопровождение (BE/partial/
    # trailing), sign-decay выход и SL работают (риск-менеджмент важнее
    # канона входа — тот же принцип что event_guard). 0 0 24 = выключено.
    session_filter_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_SESSION_FILTER_ENABLED"
    )
    session_filter_start_hour_utc: int = Field(
        default=7, validation_alias="MOMENTUM_BOT_SESSION_FILTER_START_HOUR_UTC"
    )
    session_filter_end_hour_utc: int = Field(
        default=21, validation_alias="MOMENTUM_BOT_SESSION_FILTER_END_HOUR_UTC"
    )

    # ─── NY-open entry block (BUILDLOG 2026-07-24) ───────────────────────
    # Блок НОВЫХ входов в конкретные часы UTC внутри ликвидной сессии —
    # эмпирически враждебные momentum-окна (NY-open liquidity trap / stop-hunt).
    # Loss-audit 13.07-24.07 (34 сделки): 14-16h UTC WR 0-20%, net −$109 vs
    # London-open 08h WR 62%. МАЛАЯ ВЫБОРКА — переоценить на ≥100 сделках.
    # Список часов через запятую. Пустая строка / enabled=False → выключено.
    # Research: TheTradersLegacy (first 90 min NY = liquidity trap);
    # Andersen et al. 2003 (NY volatility peak). См. session_filter.hour_blocklist_skip_reason.
    ny_open_block_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_NY_OPEN_BLOCK_ENABLED"
    )
    ny_open_block_hours_raw: str = Field(
        default="14,15,16", validation_alias="MOMENTUM_BOT_NY_OPEN_BLOCK_HOURS"
    )

    # ─── ADX-фильтр входа (BUILDLOG 2026-07-24) ──────────────────────────
    # Блок НОВЫХ входов в рейндже: ADX(14) < adx_min → нет трендовости →
    # momentum не работает. Loss-audit 13.07-24.07 (34 сделки): ADX<20 —
    # 19/34 сделок, PF 0.24, net −$119; ADX 20-30 — ~ноль. ctx.adx считается
    # compute_entry_context (раньше observability-only, теперь блокирующий
    # фильтр — инвариант «never blocks» снят). ctx=None (мало данных / холодный
    # старт) → НЕ блокировать (не ломать старт и не подгонять).
    # Research: Wilder 1978 (ADX<20 = range); Chan/AQR (momentum needs trend).
    # Обратимо: enabled=False.
    adx_filter_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_ADX_FILTER_ENABLED"
    )
    adx_min: float = Field(
        default=20.0, validation_alias="MOMENTUM_BOT_ADX_MIN"
    )

    # ─── Friday-flat: закрытие momentum-позиций перед выходными ─────────
    # Сделки, переживающие выходные, эмпирически avgR −0.79 (vs −0.08
    # intra-week), WR 11%, net −$51 (cTrader deal-list, 77 сделок,
    # 2026-06-01..26). Убыток даёт гэп понедельника (SL вне 1R), не своп.
    # FX spot разрывается Сб/Вс → понедельничный гэп = информационный
    # разрыв без price discovery (отличается от continuous-market TSMOM
    # канона). Dalton 2007 + Lyons 2001 + Andersen 2003 — см.
    # strategy/friday_flat.py. BUILDLOG 2026-06-11: ранее friday-flat был
    # только для VP; теперь (2026-06-26) применён к FX-only momentum.
    # Окно в пятницу UTC, до FX weekly close (~21:00 UTC летом). Retry в
    # цикле при неудаче. Обратимо: enabled=False или start==end.
    friday_flat_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_FRIDAY_FLAT_ENABLED"
    )
    friday_flat_start: str = Field(
        default="20:00", validation_alias="MOMENTUM_BOT_FRIDAY_FLAT_START"
    )
    # Верхняя граница попыток: после FX close close-ордера отвергаются
    # (MARKET_CLOSED) — не спамим до полуночи.
    friday_flat_end: str = Field(
        default="20:45", validation_alias="MOMENTUM_BOT_FRIDAY_FLAT_END"
    )

    # Position management (trader-backed):
    # - Van Tharp: R-multiple discipline + break-even transfer.
    # - Linda Raschke discretionary practice: partial profit + runner.
    # - Turtle/LeBeau family: ATR-style trailing for trend persistence.
    position_management_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_POSITION_MANAGEMENT_ENABLED"
    )
    break_even_r: float = Field(
        default=1.0, validation_alias="MOMENTUM_BOT_BREAK_EVEN_R"
    )
    partial_take_r: float = Field(
        default=1.5, validation_alias="MOMENTUM_BOT_PARTIAL_TAKE_R"
    )
    partial_take_fraction: float = Field(
        default=0.5, validation_alias="MOMENTUM_BOT_PARTIAL_TAKE_FRACTION"
    )
    trailing_activate_r: float = Field(
        default=1.5, validation_alias="MOMENTUM_BOT_TRAILING_ACTIVATE_R"
    )
    trailing_atr_mult: float = Field(
        default=1.5, validation_alias="MOMENTUM_BOT_TRAILING_ATR_MULT"
    )

    # ─── Sign-decay exit hysteresis (BUILDLOG 2026-07-24) ─────────────────
    # Порог выхода sign-decay как доля от signal_threshold. 0.0 = чистый
    # TSMOM sign-rule (выход на пересечении нуля — старое поведение). 1.0 =
    # полный гистерезис: вход на +threshold, выход на -threshold. На H1
    # Hurst≈0.535 (тонкий trending edge, /tmp/hurst_h1.py), zero-cut закрывает
    # победителей на шумовых колебаниях вокруг нуля досрочно (avg win +0.48R,
    # не доживая до BE@1R/partial@1.5R/trailing — loss-audit 13.07-24.07,
    # 34 сделки). Гистерезис даёт победителям room до реального разворота.
    # Research: Moskowitz/Ooi/Pedersen 2012 (sign-rule база) + Chan (momentum
    # требует persistence, не noise-exit). Обратимо: 0.0 = старое поведение.
    decay_exit_threshold_mult: float = Field(
        default=1.0, validation_alias="MOMENTUM_BOT_DECAY_EXIT_THRESHOLD_MULT"
    )

    # Dedicated cTrader credentials for this bot only.
    ctrader_host_type: str = Field(
        default="demo", validation_alias="MOMENTUM_BOT_CTRADER_HOST_TYPE"
    )
    ctrader_client_id: str = Field(
        default="", validation_alias="MOMENTUM_BOT_CTRADER_CLIENT_ID"
    )
    ctrader_client_secret: str = Field(
        default="", validation_alias="MOMENTUM_BOT_CTRADER_CLIENT_SECRET"
    )
    ctrader_account_id: int = Field(
        default=0, validation_alias="MOMENTUM_BOT_CTRADER_ACCOUNT_ID"
    )
    ctrader_redirect_uri: str = Field(
        default="https://openapi.ctrader.com/apps/token",
        validation_alias="MOMENTUM_BOT_CTRADER_REDIRECT_URI",
    )
    token_service_url: str = Field(
        default="", validation_alias="MOMENTUM_BOT_TOKEN_SERVICE_URL"
    )
    token_service_secret: str = Field(
        default="", validation_alias="MOMENTUM_BOT_TOKEN_SERVICE_SECRET"
    )
    token_service_label: str = Field(
        default="momentum_bot", validation_alias="MOMENTUM_BOT_TOKEN_SERVICE_LABEL"
    )
    require_token_service: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_REQUIRE_TOKEN_SERVICE"
    )

    # Ground-truth P&L: deal-list на ТОМ ЖЕ live-коннекте (не второй Open API
    # слот). ProtoOADealListReq — https://help.ctrader.com/open-api/messages/
    # Historical ≤5 req/s (help.ctrader.com/open-api). Один запрос / цикл
    # (poll 300s) в лимит не упирается. baseline = дата последней правки
    # логики (совпадает с TRADECARD_MOMENTUM_BASELINE_DATE).
    pnl_sync_enabled: bool = Field(
        default=True, validation_alias="MOMENTUM_BOT_PNL_SYNC_ENABLED"
    )
    pnl_baseline_raw: str = Field(
        default="2026-07-24 08:27",
        validation_alias="MOMENTUM_BOT_PNL_BASELINE",
    )

    data_dir: str = Field(default="/data", validation_alias="MOMENTUM_BOT_DATA_DIR")
    db_filename: str = Field(
        default="momentum_bot.sqlite", validation_alias="MOMENTUM_BOT_DB_FILENAME"
    )
    token_filename: str = Field(
        default="momentum_bot_tokens.json",
        validation_alias="MOMENTUM_BOT_TOKEN_FILENAME",
    )

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.symbols_raw.split(",") if s.strip())

    @property
    def ny_open_block_hours(self) -> tuple[int, ...]:
        """Кортеж часов UTC для блокировки входов (NY-open block)."""
        hours: list[int] = []
        for tok in self.ny_open_block_hours_raw.split(","):
            tok = tok.strip()
            if tok:
                try:
                    h = int(tok)
                    if 0 <= h <= 23:
                        hours.append(h)
                except ValueError:
                    continue
        return tuple(hours)

    @property
    def pnl_baseline_ms(self) -> int:
        """Unix-ms начала учёта P&L (UTC). Битый формат → 2026-07-24 08:27."""
        from datetime import datetime, timezone

        raw = (self.pnl_baseline_raw or "").strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        return int(
            datetime(2026, 7, 24, 8, 27, tzinfo=timezone.utc).timestamp() * 1000
        )

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / self.db_filename

    @property
    def token_path(self) -> Path:
        return Path(self.data_dir) / self.token_filename

