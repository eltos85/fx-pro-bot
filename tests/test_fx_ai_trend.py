"""Smoke-тесты для fx_ai_trend (Trend-follower LLM).

Структура пакета скопирована с fx_ai_trader, поэтому большинство
core-логики (pip-value table, RSS gas keywords, KillSwitch shape,
paper-reconcile) уже тестируется в test_fx_ai_trader.py. Здесь —
тесты на:

1. Корректное переименование пакетов (импорты не сломаны).
2. settings env-prefix AI_FX_TREND_* (изоляция от AI_FX_TRADER_*).
3. Trend-follower-specific конфигурация (label, БД, killswitch caps).
4. Prompts модуль импортируется без ошибок.
5. Pip-value таблица одинаковая (та же логика как у Discretionary —
   на тех же инструментах должны быть те же $/pip).
"""
from __future__ import annotations

import pytest


class TestImports:
    """Ensure все модули нового пакета load'ятся без ошибок."""

    def test_package_root(self):
        import fx_ai_trend  # noqa: F401

    def test_settings_module(self):
        from fx_ai_trend.config.settings import AiFxTrendSettings

        s = AiFxTrendSettings()
        assert s is not None

    def test_executor_module(self):
        from fx_ai_trend.trading import executor  # noqa: F401

    def test_prompts_module(self):
        from fx_ai_trend.llm.prompts import (
            SYSTEM_PROMPT,
            SYSTEM_PROMPT_REVIEW,
            build_system_prompt_review,
            build_user_prompt,
            build_user_prompt_review,
        )

        assert "TREND-FOLLOWER" in SYSTEM_PROMPT
        assert "Donchian" in SYSTEM_PROMPT
        assert "Turtle" in SYSTEM_PROMPT
        assert "NAT.GAS" in SYSTEM_PROMPT
        assert "XAUUSD" in SYSTEM_PROMPT
        assert "BRENT" in SYSTEM_PROMPT
        assert "Faith" in SYSTEM_PROMPT  # canonical source citation
        assert "Clenow" in SYSTEM_PROMPT  # canonical source citation
        assert "AQR" in SYSTEM_PROMPT  # canonical source citation
        # Review prompt должен excluder "open" action.
        assert "open" in SYSTEM_PROMPT_REVIEW.lower()
        assert "FORBIDDEN" in SYSTEM_PROMPT_REVIEW
        assert callable(build_system_prompt_review)
        assert callable(build_user_prompt)
        assert callable(build_user_prompt_review)

    def test_app_main_module_file_present(self):
        """``app.main`` импортирует ``anthropic`` SDK (DeepSeek через
        Anthropic-compat API), который ставится только в Docker-образ.
        Здесь просто убеждаемся что файл существует и synтаксически
        парсится — runtime-импорт проверяется при docker build.
        """
        import ast
        from pathlib import Path

        path = Path(__file__).parent.parent / "src/fx_ai_trend/app/main.py"
        assert path.exists()
        src = path.read_text(encoding="utf-8")
        ast.parse(src)  # AssertionError на синтакс — fails test

    def test_main_module_entrypoint_file_present(self):
        import ast
        from pathlib import Path

        path = Path(__file__).parent.parent / "src/fx_ai_trend/__main__.py"
        assert path.exists()
        ast.parse(path.read_text(encoding="utf-8"))


class TestTrendFollowerSettings:
    """Изоляция от fx_ai_trader + trend-follower-specific defaults."""

    def test_default_symbols_three_commodities(self):
        from fx_ai_trend.config.settings import AiFxTrendSettings

        s = AiFxTrendSettings()
        # Тот же 3-asset универс что и у Discretionary — это
        # ОБЯЗАТЕЛЬНО для A/B-эксперимента.
        assert s.symbols == ("XAUUSD", "BZ=F", "NG=F")

    def test_order_label_isolated_from_discretionary(self):
        """ai-fx-trend != ai-fx-trader — broker-side изоляция."""
        from fx_ai_trader.config.settings import AiFxTraderSettings
        from fx_ai_trend.config.settings import AiFxTrendSettings

        trend = AiFxTrendSettings()
        discr = AiFxTraderSettings()
        assert trend.order_label == "ai-fx-trend"
        assert discr.order_label == "ai-fx-trader"
        assert trend.order_label != discr.order_label

    def test_db_filename_isolated(self):
        from fx_ai_trader.config.settings import AiFxTraderSettings
        from fx_ai_trend.config.settings import AiFxTrendSettings

        trend = AiFxTrendSettings()
        discr = AiFxTraderSettings()
        assert trend.db_filename == "fx_ai_trend.sqlite"
        assert discr.db_filename == "fx_ai_trader.sqlite"
        assert trend.db_filename != discr.db_filename

    def test_trading_disabled_by_default(self):
        """Phase 1 = paper-mode."""
        from fx_ai_trend.config.settings import AiFxTrendSettings

        s = AiFxTrendSettings()
        assert s.trading_enabled is False

    def test_killswitch_wider_total_loss_than_discretionary(self):
        """Trend-follower имеет натурально больший drawdown
        (Clenow: 20-30% DD норма для CTA), KillSwitch caps должны
        это учитывать.
        """
        from fx_ai_trader.config.settings import AiFxTraderSettings
        from fx_ai_trend.config.settings import AiFxTrendSettings

        trend = AiFxTrendSettings()
        discr = AiFxTraderSettings()
        # Daily loss limit — тот же (защита от runaway).
        assert trend.max_daily_loss_usd == discr.max_daily_loss_usd == 150.0
        # Total loss — шире у trend-follower.
        assert trend.max_total_loss_usd > discr.max_total_loss_usd
        assert trend.max_total_loss_usd == 500.0
        assert discr.max_total_loss_usd == 300.0

    def test_pyramiding_limits(self):
        """Trend-follower с pyramiding нужны больше слотов чем
        Discretionary one-shot."""
        from fx_ai_trader.config.settings import AiFxTraderSettings
        from fx_ai_trend.config.settings import AiFxTrendSettings

        trend = AiFxTrendSettings()
        discr = AiFxTraderSettings()
        # Trend имеет до 4 unit на инструмент (Turtle canonical) ×
        # 3 instruments — общий cap 6 (margin-safe).
        assert trend.max_positions_per_symbol == 4
        assert trend.max_open_positions == 6
        # Discretionary: 3/3.
        assert discr.max_positions_per_symbol == 3
        assert discr.max_open_positions == 3


class TestPipValueTableSameAsDiscretionary:
    """Pip-value на тех же инструментах должен быть identical между
    fx_ai_trend и fx_ai_trader — это broker-spec property, не
    стратегическая константа.
    """

    def test_xauusd_pip_value(self):
        from fx_ai_trader.trading.executor import (
            _pip_value_per_std_lot as discr_pv,
        )
        from fx_ai_trend.trading.executor import (
            _pip_value_per_std_lot as trend_pv,
        )

        assert trend_pv("XAUUSD") == discr_pv("XAUUSD") == 1.0

    def test_brent_pip_value(self):
        from fx_ai_trader.trading.executor import (
            _pip_value_per_std_lot as discr_pv,
        )
        from fx_ai_trend.trading.executor import (
            _pip_value_per_std_lot as trend_pv,
        )

        assert trend_pv("BZ=F") == discr_pv("BZ=F") == 10.0

    def test_ng_pip_value(self):
        from fx_ai_trader.trading.executor import (
            _pip_value_per_std_lot as discr_pv,
        )
        from fx_ai_trend.trading.executor import (
            _pip_value_per_std_lot as trend_pv,
        )

        assert trend_pv("NG=F") == discr_pv("NG=F") == 10.0

    def test_ng_pip_size(self):
        from fx_ai_trader.trading.executor import _pip_size_for as discr_ps
        from fx_ai_trend.trading.executor import _pip_size_for as trend_ps

        assert trend_ps("NG=F") == discr_ps("NG=F") == 0.001


class TestRssKeywordsShared:
    """RSS keywords identical — обе философии торгуют тот же универс,
    нет смысла дублировать."""

    def test_gas_keywords_same(self):
        from fx_ai_trader.news.rss import GAS_KEYWORDS as discr_gas
        from fx_ai_trend.news.rss import GAS_KEYWORDS as trend_gas

        assert trend_gas == discr_gas

    def test_classify_ng_storage(self):
        from fx_ai_trend.news.rss import SYMBOL_KEYWORDS, _classify_symbols

        text = "EIA Weekly Natural Gas Storage shows 95 Bcf build"
        symbols = _classify_symbols(text, list(SYMBOL_KEYWORDS.keys()))
        assert "NG=F" in symbols
