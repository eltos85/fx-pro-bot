"""flowzone_bot — order-flow бот для Bybit perpetual futures.

Реализует стратегию из STRATEGY_FLOWZONE.md (Auction Market Theory +
Volume Profile + Order Flow, continuation-вход из зон высокой вероятности).
Канон — ролик Fabervaale «How To Find The BEST Entry Zones»
(https://youtu.be/06R-ebyOhDI). Любой числовой порог обоснован каноном или
канонической литературой Market Profile (Steidlmayer / Jim Dalton «Mind Over
Markets»).

ИЗОЛЯЦИЯ (strategy-guard.mdc): пакет НЕ импортирует ничего из ``fx_pro_bot.*``,
``ai_trader.*``, ``bybit_bot.*``, ``fx_ai_trader.*``, ``scalp_bot.*`` и наоборот.
Самостоятельная экосистема со своим env-namespace (``FLOWZONE_*``), своей БД
(volume ``flowzone_data``) и своим BUILDLOG_FLOWZONE.md.

Решения принимаются ДЕТЕРМИНИРОВАННЫМИ правилами по микроструктуре в реальном
времени, без LLM-вызова на сделку.
"""

__version__ = "0.3.0"
