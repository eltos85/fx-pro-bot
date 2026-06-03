"""Сопоставление заголовков RSS с тикерами MOEX."""
from __future__ import annotations

import re
from typing import Iterable

# Базовые алиасы (дополняются именами из API Tinkoff)
STATIC_ALIASES: dict[str, tuple[str, ...]] = {
    "SBER": ("сбербанк", "сбер", "sberbank", "sber"),
    "LKOH": ("лукойл", "lukoil"),
    "GAZP": ("газпром", "gazprom"),
    "GMKN": ("норникель", "норильский никель", "nornickel"),
    "ROSN": ("роснефть", "rosneft"),
    "NVTK": ("новатэк", "novatek"),
    "YDEX": ("яндекс", "yandex"),
    "PLZL": ("полюс", "polyus"),
    "CHMF": ("северсталь", "severstal"),
    "MAGN": ("ммк", "магнитогорский металлургический"),
    "NLMK": ("нлмк", "nlmk"),
    "VTBR": ("втб", "vtb"),
    "TATN": ("татнефть", "tatneft"),
    "MTSS": ("мтс", "mts"),
    "AFKS": ("система", "afk sistema"),
    "MOEX": ("мосбиржа", "moex", "московская биржа"),
    "RUAL": ("русал", "rusal"),
    "PHOR": ("фосагро", "phosagro"),
    "ALRS": ("алроса", "alrosa"),
    "IRAO": ("интер рао", "inter rao"),
    "FEES": ("россети",),
    "HYDR": ("русгидро", "rushydro"),
    "PIKK": ("пик", "pik group"),
    "OZON": ("озон", "ozon"),
    "VKCO": ("вконтакте", "vk ", "vk."),
    "AFLT": ("аэрофлот", "aeroflot"),
    "MGNT": ("магнит", "magnit"),
    "HNFG": ("хэндерсон", "henderson", "hnfg"),
}

STATIC_TOP_TICKERS: tuple[str, ...] = tuple(STATIC_ALIASES.keys())

# Общий рынок РФ / MOEX (без привязки к тикеру)
MARKET_KEYWORDS: tuple[str, ...] = (
    "мосбирж",
    "moex",
    "рынок акци",
    "фондовый рынок",
    "индекс",
    "imoex",
    "индекс мосбирж",
    "цб рф",
    "центробанк",
    "ключев",
    "ставк",
    "рубл",
    "нефт",
    "brent",
    "санкц",
    "дивиденд",
    "отчетност",
    "выручк",
    "прибыл",
    "эмитент",
    "инвестор",
    "акци",
    "бирж",
)


def _name_tokens(name: str) -> tuple[str, ...]:
    words = re.findall(r"[а-яёa-z0-9]{3,}", name.lower())
    return tuple(words[:6])


def build_ticker_index(
    portfolio_tickers: Iterable[str],
    extra_tickers: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Индекс тикер → список подстрок для поиска в тексте."""
    tickers = {t.upper() for t in portfolio_tickers if t}
    tickers.update(t.upper() for t in extra_tickers if t)
    index: dict[str, list[str]] = {}
    for t in tickers:
        aliases: list[str] = [t.lower()]
        for a in STATIC_ALIASES.get(t, ()):
            if a not in aliases:
                aliases.append(a)
        index[t] = aliases
    return index


def match_tickers(text: str, index: dict[str, list[str]]) -> list[str]:
    t = text.lower()
    found: list[str] = []
    for ticker, aliases in index.items():
        for alias in aliases:
            if len(alias) <= 3:
                if re.search(rf"\b{re.escape(alias)}\b", t):
                    found.append(ticker)
                    break
            elif alias in t:
                found.append(ticker)
                break
    return found


def is_market_wide(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in MARKET_KEYWORDS)
