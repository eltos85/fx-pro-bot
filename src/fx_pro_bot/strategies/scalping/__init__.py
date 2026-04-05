"""Скальпинг-стратегии: VWAP Reversion, Stat-Arb Pairs, Session ORB."""

from fx_pro_bot.strategies.scalping.session_orb import SessionOrbStrategy
from fx_pro_bot.strategies.scalping.stat_arb import StatArbStrategy
from fx_pro_bot.strategies.scalping.vwap_reversion import VwapReversionStrategy

__all__ = ["VwapReversionStrategy", "StatArbStrategy", "SessionOrbStrategy"]
