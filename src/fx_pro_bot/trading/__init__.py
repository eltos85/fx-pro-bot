"""Автоторговля через cTrader Open API."""

from fx_pro_bot.trading.executor import TradeExecutor
from fx_pro_bot.trading.killswitch import KillSwitch

__all__ = ["TradeExecutor", "KillSwitch"]
