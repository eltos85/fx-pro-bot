"""Тесты арифметики замера scripts/hybrid_poll_fidelity.py (без сети).

Проверяем ровно то, чем рука «как у бота» отличается от модели §17.6: сетку
опроса, цену исполнения по факту опроса, одну фиксацию за цикл и слиппедж на
двух ногах.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import hybrid_poll_fidelity as pf  # noqa: E402

MIN = pf.MINUTE_MS


def bar(minute: int, op: float, hi: float, lo: float, cl: float):
    """Минутная свеча с временем, кратным минуте от нуля эпохи."""
    return (minute * MIN, op, hi, lo, cl)


def long_from(minute: int) -> dict[int, int]:
    """Тренд «покупать» с указанной минуты и до конца отрезка."""
    return {minute * MIN: 1}


def test_level_arm_fires_on_a_wick_the_poll_arm_never_sees():
    """Прокол уровня между опросами: заявка исполнилась бы, бот не увидел.

    Опрос раз в 3 минуты — минуты 0, 3, 6. Всплеск на минуте 4 успевает уйти,
    и на минуте 6 цена снова ниже уровня.
    """
    minutes = [
        bar(0, 100.0, 100.0, 100.0, 100.0),
        bar(1, 100.0, 100.2, 100.0, 100.1),
        bar(2, 100.1, 100.3, 100.0, 100.2),
        bar(3, 100.2, 100.4, 100.1, 100.3),
        bar(4, 100.3, 102.0, 100.2, 100.4),   # прокол внутри интервала
        bar(5, 100.4, 100.6, 100.2, 100.3),
        bar(6, 100.3, 100.5, 100.2, 100.4),   # опрос: уже ниже уровня
    ]
    states = long_from(0)

    lvl = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
                 mode="level")
    poll = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
                  mode="poll", poll_min=3)

    assert len(lvl["events"]) == 1
    assert lvl["events"][0]["price"] == pytest.approx(101.0)
    assert poll["events"] == []


def test_poll_arm_fills_above_the_level_when_price_overshoots():
    """Бот закрывает по цене опроса, а она выше уровня — денег больше порога."""
    minutes = [
        bar(0, 100.0, 100.0, 100.0, 100.0),
        bar(1, 100.0, 100.5, 100.0, 100.4),
        bar(2, 100.4, 101.0, 100.4, 100.9),
        bar(3, 100.9, 102.0, 100.9, 101.8),   # опрос: цена уже +1.8%
    ]
    states = long_from(0)

    poll = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
                  mode="poll", poll_min=3)

    assert len(poll["events"]) == 1
    ev = poll["events"][0]
    assert ev["price"] == pytest.approx(101.8)
    # 2 ETH куплено по 100, закрыто по 101.8 → $3.60, а порог обещал $2.00.
    assert ev["gross"] == pytest.approx(3.6)


def test_poll_arm_makes_one_fixation_per_wake_up():
    """За один опрос бот делает не больше одной фиксации, даже если цена ушла.

    Заявка на уровне при том же скачке сработала бы несколько раз: после
    обратного входа следующий уровень тоже оказывается под максимумом.
    """
    minutes = [
        bar(0, 100.0, 100.0, 100.0, 100.0),
        bar(1, 100.0, 100.1, 100.0, 100.1),
        bar(2, 100.1, 100.2, 100.0, 100.1),
        bar(3, 100.1, 105.0, 100.1, 105.0),   # +5% одним движением
    ]
    states = long_from(0)

    lvl = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
                 mode="level")
    poll = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
                  mode="poll", poll_min=3)

    assert len(lvl["events"]) > 1
    assert len(poll["events"]) == 1


def test_slippage_is_paid_on_both_legs():
    """Слиппедж бьёт дважды: закрытие дешевле, обратный вход дороже."""
    minutes = [
        bar(0, 100.0, 100.0, 100.0, 100.0),
        bar(1, 100.0, 100.5, 100.0, 100.4),
        bar(2, 100.4, 101.0, 100.4, 100.9),
        bar(3, 100.9, 101.2, 100.9, 101.2),
        bar(6, 101.2, 101.3, 101.0, 101.1),
    ]
    states = long_from(0)

    clean = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
                   mode="poll", poll_min=3, slip_bp=0.0)
    dirty = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
                   mode="poll", poll_min=3, slip_bp=10.0)

    assert len(clean["events"]) == len(dirty["events"]) == 1
    # Закрытие: 101.2 → 101.2 × (1 − 0.001) = 101.0988.
    assert dirty["events"][0]["price"] == pytest.approx(101.2 * 0.999)
    assert dirty["events"][0]["gross"] < clean["events"][0]["gross"]
    # Обратный вход дороже, поэтому и остаток позиции в конце хуже.
    assert dirty["net"] < clean["net"]


def test_slippage_is_not_applied_to_the_level_arm():
    """Лимитная заявка исполняется на своём уровне — рынок за неё не платит."""
    minutes = [
        bar(0, 100.0, 100.0, 100.0, 100.0),
        bar(1, 100.0, 102.0, 100.0, 101.5),
    ]
    states = long_from(0)

    r = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
               mode="level", slip_bp=50.0)

    assert r["events"][0]["price"] == pytest.approx(101.0)


def test_trend_flip_closes_at_the_bar_open_in_both_arms():
    """Разворот тренда закрывает позицию по открытию минуты в обеих руках."""
    minutes = [
        bar(0, 100.0, 100.2, 99.9, 100.1),
        bar(1, 100.1, 100.3, 100.0, 100.2),
        bar(2, 99.0, 99.1, 98.5, 98.7),
    ]
    states = {0: 1, 2 * MIN: 0}

    for mode in ("level", "poll"):
        r = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
                   mode=mode, poll_min=3)
        assert [e["reason"] for e in r["rule_exits"]] == ["правило"]
        assert r["rule_exits"][0]["price"] == pytest.approx(99.0)
        assert r["events"] == []


def test_fees_are_charged_on_every_leg_of_the_circle():
    """Комиссия — с каждой ноги: вход, закрытие, обратный вход, финал."""
    minutes = [
        bar(0, 100.0, 100.0, 100.0, 100.0),
        bar(3, 100.0, 101.5, 100.0, 101.5),
    ]
    states = long_from(0)

    r = pf.run(minutes, states, threshold_pct=1.0, notional=200.0,
               mode="poll", poll_min=3)

    # Вход $200, закрытие ~$200×1.015, обратный вход $200, закрытие в конце
    # истории по 101.5 — четыре ноги примерно по $200.
    assert len(r["events"]) == 1
    assert r["fees"] == pytest.approx(4 * 200 * pf.TAKER, rel=0.02)


class StubSession:
    """Биржа-заглушка: отдаёт страницы свечей, может «падать» N первых раз."""

    def __init__(self, minutes: list[tuple], fails: int = 0) -> None:
        self.minutes = sorted(minutes, key=lambda r: r[0])
        self.fails = fails
        self.calls = 0

    def get_kline(self, *, start, end, limit, **_):
        self.calls += 1
        if self.fails > 0:
            self.fails -= 1
            raise RuntimeError("Too many visits. (ErrCode: 10006)")
        rows = [r for r in self.minutes if start <= r[0] <= end]
        rows = rows[-limit:]
        return {"result": {"list": [[str(r[0]), *(str(x) for x in r[1:])]
                                   for r in reversed(rows)]}}


def test_fetch_minutes_retries_and_returns_the_full_range(monkeypatch):
    """Ошибка лимита биржи — повод подождать, а не молча обрезать историю."""
    monkeypatch.setattr(pf.time, "sleep", lambda _s: None)
    data = [bar(i, 100.0 + i, 100.5 + i, 99.5 + i, 100.2 + i)
            for i in range(2500)]
    sess = StubSession(data, fails=2)

    got = pf.fetch_minutes(sess, "ETHUSDT", 0)

    assert len(got) == 2500
    assert got[0][0] == 0 and got[-1][0] == 2499 * MIN
    assert sess.calls > 3   # две неудачи плюс страницы


def test_fetch_minutes_gives_up_loudly_instead_of_truncating(monkeypatch):
    """Если страница так и не пришла — падаем, а не отдаём огрызок ряда."""
    monkeypatch.setattr(pf.time, "sleep", lambda _s: None)
    sess = StubSession([bar(0, 100.0, 100.0, 100.0, 100.0)],
                       fails=pf.PAGE_RETRIES)

    with pytest.raises(SystemExit):
        pf.fetch_minutes(sess, "ETHUSDT", 0)


def test_no_trades_when_the_trend_never_turns_long():
    minutes = [bar(i, 100.0, 100.5, 99.5, 100.0) for i in range(10)]
    r = pf.run(minutes, {0: 0}, threshold_pct=1.0, notional=200.0, mode="poll",
               poll_min=3)
    assert r["events"] == [] and r["rule_exits"] == []
    assert r["fees"] == 0.0
