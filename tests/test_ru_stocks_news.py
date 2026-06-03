"""Тесты новостного слоя."""
from __future__ import annotations

from ru_stocks_analyst.news.ticker_map import (
    build_ticker_index,
    is_market_wide,
    match_tickers,
)


def test_match_sber_lukoil():
    idx = build_ticker_index(["SBER", "LKOH"])
    found = match_tickers("Сбербанк повысил прогноз по прибыли", idx)
    assert "SBER" in found
    found2 = match_tickers("Лукойл объявил дивиденды", idx)
    assert "LKOH" in found2


def test_market_wide():
    assert is_market_wide("ЦБ РФ сохранил ключевую ставку")
    assert not is_market_wide("Погода в Москве")
