"""Тесты ru_stocks_analyst (без живого API)."""
from __future__ import annotations

from ru_stocks_analyst.analysis.screener import evaluate_share
from ru_stocks_analyst.data.universe import ShareInstrument
from ru_stocks_analyst.tinkoff.accounts import pick_brokerage_account
from ru_stocks_analyst.tinkoff.rest_client import quotation_to_float


def test_quotation_to_float():
    assert quotation_to_float({"units": "100", "nano": 500000000}) == 100.5
    assert quotation_to_float(None) == 0.0


def test_pick_brokerage_prefers_tinkoff_not_iis():
    accounts = [
        {"id": "iis1", "type": 2, "status": "ACCOUNT_STATUS_OPEN", "name": "ИИС"},
        {"id": "br1", "type": 1, "status": "ACCOUNT_STATUS_OPEN", "name": "Брокер"},
    ]
    a = pick_brokerage_account(accounts)
    assert a["id"] == "br1"


def test_pick_brokerage_preferred_id():
    accounts = [
        {"id": "x", "type": 1, "status": "ACCOUNT_STATUS_OPEN", "name": "X"},
    ]
    a = pick_brokerage_account(accounts, preferred_id="x")
    assert a["id"] == "x"


def _synthetic_candles_trend_up(n: int = 60) -> list[dict]:
    """Умеренный тренд вверх (RSI не уходит в перекупленность >68)."""
    candles = []
    price = 200.0
    for i in range(n):
        # чередуем рост и откат — RSI остаётся в «продолжении»
        if i % 5 == 4:
            price -= 0.8
        else:
            price += 0.35
        u = int(price)
        nano = int(round((price - u) * 1e9))
        vol = 90_000 if i % 5 == 4 else 110_000
        # последний бар — повышенный объём (фильтр 1.1× среднего)
        if i == n - 1:
            vol = 150_000
        candles.append(
            {
                "open": {"units": str(u), "nano": nano},
                "high": {"units": str(u + 2), "nano": 0},
                "low": {"units": str(u - 2), "nano": 0},
                "close": {"units": str(u), "nano": nano},
                "volume": vol,
            }
        )
    return candles


def test_evaluate_share_long_trend():
    inst = ShareInstrument(
        figi="BBG001",
        ticker="TEST",
        name="Test",
        uid="uid1",
        currency="rub",
        exchange="MOEX",
        liquidity_flag=True,
    )
    idea = evaluate_share(inst, _synthetic_candles_trend_up())
    assert idea is not None
    assert idea.direction == "long"
    assert idea.ticker == "TEST"
