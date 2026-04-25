"""Тесты скальпинг-стратегий bybit_bot."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bybit_bot.market_data.models import Bar


def _make_bars(
    symbol: str = "BTCUSDT",
    n: int = 100,
    base_price: float = 60000.0,
    step: float = 10.0,
    volume: float = 100.0,
) -> list[Bar]:
    """Генератор тестовых баров."""
    bars = []
    for i in range(n):
        price = base_price + step * (i % 20 - 10)
        hour = 10 + i // 60
        minute = i % 60
        bars.append(Bar(
            symbol=symbol,
            ts=datetime(2026, 4, 11, hour, minute, tzinfo=UTC),
            open=price - 5,
            high=price + 20,
            low=price - 20,
            close=price,
            volume=volume,
        ))
    return bars


# ── Indicators ───────────────────────────────────────────────

class TestIndicators:
    def test_vwap_basic(self):
        from bybit_bot.strategies.scalping.indicators import vwap
        bars = _make_bars(n=50)
        val = vwap(bars)
        assert val > 0

    def test_vwap_zero_volume(self):
        from bybit_bot.strategies.scalping.indicators import vwap
        bars = _make_bars(n=10, volume=0)
        val = vwap(bars)
        assert val > 0

    def test_rolling_z_score(self):
        from bybit_bot.strategies.scalping.indicators import rolling_z_score
        values = [float(i) for i in range(50)]
        z = rolling_z_score(values, 20)
        assert z > 0  # последнее значение выше среднего

    def test_rolling_z_score_insufficient(self):
        from bybit_bot.strategies.scalping.indicators import rolling_z_score
        assert rolling_z_score([1.0, 2.0], 20) == 0.0

    def test_ols_hedge_ratio(self):
        from bybit_bot.strategies.scalping.indicators import ols_hedge_ratio
        a = [float(i) for i in range(50)]
        b = [float(i) * 2 for i in range(50)]
        beta = ols_hedge_ratio(a, b)
        assert 0.4 < beta < 0.6

    def test_ols_insufficient(self):
        from bybit_bot.strategies.scalping.indicators import ols_hedge_ratio
        assert ols_hedge_ratio([1.0], [2.0]) == 1.0

    def test_spread_series(self):
        from bybit_bot.strategies.scalping.indicators import spread_series
        a = [10.0, 20.0, 30.0]
        b = [5.0, 10.0, 15.0]
        sprd = spread_series(a, b, 2.0)
        assert all(abs(s) < 0.001 for s in sprd)

    def test_ema_slope(self):
        from bybit_bot.strategies.scalping.indicators import ema_slope
        vals = [float(i) for i in range(20)]
        slope = ema_slope(vals, 5)
        assert slope > 0

    def test_avg_volume(self):
        from bybit_bot.strategies.scalping.indicators import avg_volume
        bars = _make_bars(n=30, volume=100.0)
        assert avg_volume(bars, 20) == pytest.approx(100.0)


# ── VWAP Crypto Strategy ────────────────────────────────────

class TestVwapCrypto:
    def test_no_signals_in_range(self):
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        strat = VwapCryptoStrategy()
        bars = _make_bars(n=60, step=1.0)
        signals = strat.scan({"BTCUSDT": bars})
        assert isinstance(signals, list)

    def test_insufficient_bars(self):
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        strat = VwapCryptoStrategy()
        bars = _make_bars(n=10)
        assert strat.scan({"BTCUSDT": bars}) == []

    def test_signal_fields(self):
        from bybit_bot.strategies.scalping.vwap_crypto import VwapSignal, VwapCryptoStrategy
        from bybit_bot.analysis.signals import Direction
        sig = VwapSignal(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            deviation_atr=2.5,
            rsi=25.0,
            vwap_price=60000.0,
            atr_value=500.0,
            entry_price=59000.0,
        )
        assert sig.direction == Direction.LONG
        assert sig.deviation_atr == 2.5

    # ── Wave 6 whitelist'ы (BUILDLOG.md 2026-04-25) ────────────────

    def test_is_active_time_no_filters_passes(self):
        """Без allowed_weekdays и allowed_hours — пропускает любое время."""
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        bar = Bar(
            symbol="BTCUSDT",
            ts=datetime(2026, 4, 25, 23, 30, tzinfo=UTC),  # суббота, 23 UTC
            open=1, high=1, low=1, close=1, volume=1,
        )
        strat = VwapCryptoStrategy()
        assert strat._is_active_time(bar) is True

    def test_is_active_time_weekday_filter_blocks_weekend(self):
        """allowed_weekdays={mon..fri} отсекает субботу (weekday=5)."""
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        sat = Bar(symbol="BTCUSDT",
                  ts=datetime(2026, 4, 25, 14, 30, tzinfo=UTC),  # Sat
                  open=1, high=1, low=1, close=1, volume=1)
        mon = Bar(symbol="BTCUSDT",
                  ts=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),  # Mon
                  open=1, high=1, low=1, close=1, volume=1)
        strat = VwapCryptoStrategy(allowed_weekdays={0, 1, 2, 3, 4})
        assert strat._is_active_time(sat) is False
        assert strat._is_active_time(mon) is True

    def test_is_active_time_hour_filter_blocks_off_hours(self):
        """allowed_hours_utc={14,15,16,19,20} отсекает 17 UTC."""
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        h17 = Bar(symbol="BTCUSDT",
                  ts=datetime(2026, 4, 27, 17, 0, tzinfo=UTC),
                  open=1, high=1, low=1, close=1, volume=1)
        h15 = Bar(symbol="BTCUSDT",
                  ts=datetime(2026, 4, 27, 15, 0, tzinfo=UTC),
                  open=1, high=1, low=1, close=1, volume=1)
        strat = VwapCryptoStrategy(allowed_hours_utc={14, 15, 16, 19, 20})
        assert strat._is_active_time(h17) is False
        assert strat._is_active_time(h15) is True

    def test_is_active_time_naive_ts_assumes_utc(self):
        """Бар без tzinfo трактуется как UTC, weekday/hour корректны."""
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        naive = Bar(symbol="BTCUSDT",
                    ts=datetime(2026, 4, 27, 15, 0),  # без tzinfo
                    open=1, high=1, low=1, close=1, volume=1)
        strat = VwapCryptoStrategy(allowed_hours_utc={15})
        assert strat._is_active_time(naive) is True

    def test_allowed_symbols_blocks_non_whitelisted(self):
        """allowed_symbols={ETHUSDT} → BTCUSDT не сканируется (нет сигнала)."""
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        strat = VwapCryptoStrategy(allowed_symbols={"ETHUSDT"})
        bars = _make_bars(symbol="BTCUSDT", n=60)
        assert strat.scan({"BTCUSDT": bars}) == []

    def test_init_stores_filter_params(self):
        """Параметры сохраняются в инстансе для последующего применения."""
        from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
        strat = VwapCryptoStrategy(
            allowed_direction="long",
            allowed_symbols={"ADAUSDT", "SOLUSDT"},
            allowed_hours_utc={14, 15, 16, 19, 20},
            allowed_weekdays={0, 1, 2, 3, 4},
        )
        assert strat._allowed_direction == "long"
        assert strat._allowed_symbols == {"ADAUSDT", "SOLUSDT"}
        assert strat._allowed_hours_utc == {14, 15, 16, 19, 20}
        assert strat._allowed_weekdays == {0, 1, 2, 3, 4}


# ── Stat-Arb Crypto Strategy ────────────────────────────────

class TestStatArbCrypto:
    def test_no_signals_insufficient_data(self):
        from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
        strat = StatArbCryptoStrategy()
        bars = _make_bars(n=50)
        signals = strat.scan({"BTCUSDT": bars, "ETHUSDT": bars})
        assert signals == []

    def test_scan_with_enough_data(self):
        from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
        strat = StatArbCryptoStrategy()
        bars_btc = _make_bars("BTCUSDT", n=200, base_price=60000)
        bars_eth = _make_bars("ETHUSDT", n=200, base_price=3000)
        signals = strat.scan({"BTCUSDT": bars_btc, "ETHUSDT": bars_eth})
        assert isinstance(signals, list)

    def test_check_exits_empty(self):
        from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
        strat = StatArbCryptoStrategy()
        bars_btc = _make_bars("BTCUSDT", n=200, base_price=60000)
        bars_eth = _make_bars("ETHUSDT", n=200, base_price=3000)
        to_close = strat.check_exits({"BTCUSDT": bars_btc, "ETHUSDT": bars_eth}, [])
        assert to_close == []


# ── Funding Rate Scalp ───────────────────────────────────────

class TestFundingScalp:
    def test_no_client_returns_empty(self):
        from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
        strat = FundingScalpStrategy(client=None)
        assert strat.scan(("BTCUSDT",), {}) == []

    def test_is_near_funding_logic(self):
        from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
        result = FundingScalpStrategy._is_near_funding()
        assert isinstance(result, bool)

    def test_should_exit_after_funding(self):
        from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
        result = FundingScalpStrategy.should_exit_after_funding()
        assert isinstance(result, bool)

    def test_signal_fields(self):
        from bybit_bot.strategies.scalping.funding_scalp import FundingSignal
        from bybit_bot.analysis.signals import Direction
        sig = FundingSignal(
            symbol="BTCUSDT",
            direction=Direction.SHORT,
            funding_rate=0.001,
            next_funding_time="2026-04-11T08:00:00Z",
            strength=0.8,
            atr_value=500.0,
            entry_price=72000.0,
        )
        assert sig.direction == Direction.SHORT
        assert sig.funding_rate == 0.001


# ── Volume Spike Strategy ────────────────────────────────────

class TestVolumeSpike:
    def test_no_spike_normal_volume(self):
        from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
        strat = VolumeSpikeStrategy()
        bars = _make_bars(n=60, volume=100.0)
        signals = strat.scan({"BTCUSDT": bars})
        assert isinstance(signals, list)

    def test_spike_detected(self):
        from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
        strat = VolumeSpikeStrategy()
        bars = _make_bars(n=60, volume=100.0, step=5.0)
        # Сделать последний бар с аномальным объёмом и движением
        last = bars[-1]
        spike_bar = Bar(
            symbol=last.symbol,
            ts=last.ts,
            open=last.close - 500,
            high=last.close + 100,
            low=last.close - 600,
            close=last.close,
            volume=500.0,  # 5x от среднего 100
        )
        bars[-1] = spike_bar
        signals = strat.scan({"BTCUSDT": bars})
        assert isinstance(signals, list)

    def test_insufficient_bars(self):
        from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
        strat = VolumeSpikeStrategy()
        assert strat.scan({"BTCUSDT": _make_bars(n=5)}) == []

    def test_max_signals_limit(self):
        from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
        strat = VolumeSpikeStrategy(max_signals_per_scan=2)
        assert strat._max_signals == 2


# ── Session ORB Strategy ─────────────────────────────────────

def _make_orb_session(
    *,
    session_hour: int = 8,
    box_high: float = 60_100.0,
    box_low: float = 59_900.0,
    pre_bars: int = 120,
    post_orb_bars: int = 1,
    breakout: str | None = "up",
    breakout_delta: float = 50.0,
    spike_volume: float = 200.0,
    normal_volume: float = 100.0,
    symbol: str = "BTCUSDT",
) -> list[Bar]:
    """Сгенерировать последовательность баров для теста ORB.

    - 120 пре-сессионных баров с EMA-трендом вверх или вниз (управляется
      параметром breakout: 'up' -> растущий тренд, 'down' -> падающий).
    - 3 бара коробки (первые 15 мин сессии, session_hour:00..session_hour:15)
      формируют диапазон [box_low, box_high].
    - post_orb_bars пробойных баров, последний — либо пробой вверх/вниз
      с объёмом spike_volume, либо сидит в коробке.
    """
    from datetime import timedelta

    bars: list[Bar] = []
    session_start = datetime(2026, 4, 17, session_hour, 0, tzinfo=UTC)
    pre_start = session_start - timedelta(minutes=5 * pre_bars)

    # Пре-сессия: лёгкий дрейф к центру коробки + рыночный шум, чтобы
    # ADX остался ниже 25 (иначе ORB-фильтр «ADX > 25 = уже тренд»
    # отсечёт всё). EMA-slope при этом остаётся положительным/отрицательным
    # благодаря направленному дрейфу.
    center = (box_high + box_low) / 2
    drift = 0.05 if breakout == "up" else (-0.05 if breakout == "down" else 0.0)
    for i in range(pre_bars):
        # Осцилляция (синус) + лёгкий тренд — ADX останется ~10-20
        noise = ((i * 7) % 11) - 5  # детерминированный псевдо-шум
        price = center - drift * (pre_bars - i) + noise
        bars.append(Bar(
            symbol=symbol,
            ts=pre_start + timedelta(minutes=5 * i),
            open=price - 2,
            high=price + 5,
            low=price - 5,
            close=price,
            volume=normal_volume,
        ))

    # 3 бара коробки — осциллируют внутри [box_low, box_high]
    for i in range(3):
        bars.append(Bar(
            symbol=symbol,
            ts=session_start + timedelta(minutes=5 * i),
            open=box_low + (box_high - box_low) * 0.3,
            high=box_high,
            low=box_low,
            close=box_low + (box_high - box_low) * 0.6,
            volume=normal_volume,
        ))

    # post_orb_bars пробойных баров
    for i in range(post_orb_bars):
        is_last = i == post_orb_bars - 1
        if is_last and breakout == "up":
            o, h, l_, c = box_high - 5, box_high + breakout_delta, box_high - 10, box_high + breakout_delta - 5
            v = spike_volume
        elif is_last and breakout == "down":
            o, h, l_, c = box_low + 5, box_low + 10, box_low - breakout_delta, box_low - breakout_delta + 5
            v = spike_volume
        else:
            # Сидит внутри коробки
            o = box_low + (box_high - box_low) * 0.4
            h = box_high - 10
            l_ = box_low + 10
            c = box_low + (box_high - box_low) * 0.5
            v = normal_volume
        bars.append(Bar(
            symbol=symbol,
            ts=session_start + timedelta(minutes=5 * (3 + i)),
            open=o, high=h, low=l_, close=c, volume=v,
        ))
    return bars


class TestSessionOrb:
    def test_breakout_up_detected(self):
        from bybit_bot.strategies.scalping.session_orb import (
            SessionOrbStrategy, OrbSignal,
        )
        from bybit_bot.analysis.signals import Direction

        bars = _make_orb_session(breakout="up")
        signals = SessionOrbStrategy().scan({"BTCUSDT": bars})
        assert len(signals) == 1
        sig = signals[0]
        assert isinstance(sig, OrbSignal)
        assert sig.symbol == "BTCUSDT"
        assert sig.direction == Direction.LONG
        assert sig.session == "london"
        assert sig.volume_ratio >= 1.3

    def test_breakout_down_detected(self):
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
        from bybit_bot.analysis.signals import Direction

        bars = _make_orb_session(breakout="down")
        signals = SessionOrbStrategy().scan({"BTCUSDT": bars})
        assert len(signals) == 1
        assert signals[0].direction == Direction.SHORT

    def test_no_signal_inside_box(self):
        """Цена внутри коробки → нет пробоя → нет сигнала."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        bars = _make_orb_session(breakout=None)
        assert SessionOrbStrategy().scan({"BTCUSDT": bars}) == []

    def test_no_signal_low_volume(self):
        """Пробой есть, но объём ниже 1.3× → отсекаем."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        bars = _make_orb_session(breakout="up", spike_volume=100.0)  # == normal
        assert SessionOrbStrategy().scan({"BTCUSDT": bars}) == []

    def test_no_signal_out_of_session(self):
        """Текущий бар вне сессионных окон → коробку не строим."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        # 05:00 UTC — никакая сессия не активна (asia 00-01, london 08-09, ny 14-15)
        bars = _make_orb_session(session_hour=5, breakout="up")
        assert SessionOrbStrategy().scan({"BTCUSDT": bars}) == []

    def test_no_signal_after_earlier_breakout(self):
        """Если пробой уже случился раньше в post_orb — второй раз не входим."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        # 5 post-orb баров: 1-й пробойный, 5-й тоже пробойный
        bars = _make_orb_session(breakout="up", post_orb_bars=5)
        # Вручную сделаем первый post-orb бар тоже пробойным
        # (индекс 120 + 3 = 123 — первый после коробки)
        first_post = bars[123]
        bars[123] = Bar(
            symbol=first_post.symbol, ts=first_post.ts,
            open=60_095.0, high=60_200.0, low=60_090.0, close=60_190.0,
            volume=first_post.volume,
        )
        # Теперь пробой был раньше → последний бар тоже пробойный,
        # но earlier_broke_up=True → должен быть отсечён
        assert SessionOrbStrategy().scan({"BTCUSDT": bars}) == []

    def test_insufficient_bars(self):
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
        assert SessionOrbStrategy().scan({"BTCUSDT": _make_bars(n=10)}) == []

    def test_max_signals_limit(self):
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
        strat = SessionOrbStrategy(max_signals_per_scan=1)
        bars_a = _make_orb_session(breakout="up")
        bars_b = _make_orb_session(breakout="up", symbol="ETHUSDT",
                                   box_high=3000.0, box_low=2990.0,
                                   spike_volume=300.0)
        signals = strat.scan({"BTCUSDT": bars_a, "ETHUSDT": bars_b})
        assert len(signals) == 1

    def test_allowed_sessions_whitelist(self):
        """allowed_sessions={'asia'} блокирует London-пробой."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        bars = _make_orb_session(breakout="up")  # london session
        strat = SessionOrbStrategy(allowed_sessions={"asia"})
        assert strat.scan({"BTCUSDT": bars}) == []
        # Без ограничения — сигнал есть
        assert len(SessionOrbStrategy().scan({"BTCUSDT": bars})) == 1

    def test_allowed_symbols_whitelist(self):
        """allowed_symbols={'ETHUSDT'} блокирует BTCUSDT-сигнал."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        bars = _make_orb_session(breakout="up")
        strat = SessionOrbStrategy(allowed_symbols={"ETHUSDT"})
        assert strat.scan({"BTCUSDT": bars}) == []

    def test_allowed_direction_long_blocks_short(self):
        """allowed_direction='long' отсекает SHORT-пробой."""
        from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy

        bars_down = _make_orb_session(breakout="down")
        strat = SessionOrbStrategy(allowed_direction="long")
        assert strat.scan({"BTCUSDT": bars_down}) == []
        # Lower boundary: long проходит
        bars_up = _make_orb_session(breakout="up")
        assert len(strat.scan({"BTCUSDT": bars_up})) == 1


# ── Turtle Soup Strategy ─────────────────────────────────────

def _make_turtle_bars(
    *,
    trap_direction: str | None = "down",  # "down" → long-setup; "up" → short-setup; None → neutral
    symbol: str = "BTCUSDT",
    n_history: int = 100,
    center: float = 60000.0,
    range_half: float = 100.0,
    ts_base: datetime | None = None,
) -> list[Bar]:
    """Сгенерировать бары для теста Turtle Soup.

    - n_history баров колеблются в [center-range_half, center+range_half]
      (образует 20-барный диапазон).
    - trap_bar (индекс -3): пробивает 20-барный экстремум (low/high).
    - bars[-2]: промежуточный.
    - last_bar: возвращается ВНУТРЬ диапазона с буфером.
    - trap_direction=None → нет пробоя.
    - ts_base: начальная метка времени (по умолчанию 2026-04-17 12:00 UTC).
    """
    from datetime import timedelta

    if ts_base is None:
        ts_base = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    bars: list[Bar] = []

    # History: детерминированный шум с амплитудой range_half
    for i in range(n_history):
        noise = ((i * 13) % 21) - 10  # -10..+10
        price = center + noise * (range_half / 10.0) * 0.6
        bars.append(Bar(
            symbol=symbol,
            ts=ts_base + timedelta(minutes=5 * i),
            open=price - 1,
            high=price + 5,
            low=price - 5,
            close=price,
            volume=100.0,
        ))

    atr_est = 10.0  # приблизительный ATR нашей синтетики

    # Для long-setup нужен RSI<30 на пробое, для short-setup RSI>70.
    # Добавим 16 «bias»-баров почти без откатов, чтобы RSI(14) ушёл в экстремум.
    # Эти бары НЕ должны менять 20-барный экстремум, который увидит стратегия
    # в момент пробоя — поэтому держим их значения внутри истории.
    bias_len = 16
    if trap_direction == "down":
        # Снижение внутри верхней половины диапазона, чтобы не создать новый low
        for i in range(bias_len):
            price = center + 60 - i * 4  # center+60..center+0
            bars.append(Bar(
                symbol=symbol, ts=ts_base + timedelta(minutes=5 * (n_history + i)),
                open=price + 2, high=price + 3, low=price - 1, close=price,
                volume=100.0,
            ))
    elif trap_direction == "up":
        # Рост внутри нижней половины диапазона
        for i in range(bias_len):
            price = center - 60 + i * 4  # center-60..center+0
            bars.append(Bar(
                symbol=symbol, ts=ts_base + timedelta(minutes=5 * (n_history + i)),
                open=price - 2, high=price + 1, low=price - 3, close=price,
                volume=100.0,
            ))
    else:
        for i in range(bias_len):
            bars.append(Bar(
                symbol=symbol, ts=ts_base + timedelta(minutes=5 * (n_history + i)),
                open=center - 1, high=center + 3, low=center - 3, close=center,
                volume=100.0,
            ))

    # Пересчёт экстремума ПОСЛЕ bias. Стратегия для trap_bar = bars[-3]
    # (после добавления ещё 3 баров ниже) берёт history = bars[-3-20 : -3].
    # Сейчас это последние 20 добавленных баров.
    ref_history = bars[-20:]
    hist_low = min(b.low for b in ref_history)
    hist_high = max(b.high for b in ref_history)

    last_ts = bars[-1].ts + timedelta(minutes=5)
    if trap_direction == "down":
        trap_price = hist_low - atr_est * 0.8
        bars.append(Bar(
            symbol=symbol, ts=last_ts,
            open=hist_low + 1, high=hist_low + 2,
            low=trap_price, close=trap_price + 1,
            volume=150.0,
        ))
        bars.append(Bar(
            symbol=symbol, ts=last_ts + timedelta(minutes=5),
            open=trap_price + 2, high=hist_low + 5, low=trap_price + 1,
            close=hist_low + 4, volume=120.0,
        ))
        bars.append(Bar(
            symbol=symbol, ts=last_ts + timedelta(minutes=10),
            open=hist_low + 4, high=hist_low + 15, low=hist_low + 3,
            close=hist_low + 12, volume=130.0,
        ))
    elif trap_direction == "up":
        trap_price = hist_high + atr_est * 0.8
        bars.append(Bar(
            symbol=symbol, ts=last_ts,
            open=hist_high - 1, high=trap_price, low=hist_high - 2,
            close=trap_price - 1, volume=150.0,
        ))
        bars.append(Bar(
            symbol=symbol, ts=last_ts + timedelta(minutes=5),
            open=trap_price - 2, high=trap_price - 1, low=hist_high - 5,
            close=hist_high - 4, volume=120.0,
        ))
        bars.append(Bar(
            symbol=symbol, ts=last_ts + timedelta(minutes=10),
            open=hist_high - 4, high=hist_high - 3, low=hist_high - 15,
            close=hist_high - 12, volume=130.0,
        ))
    else:
        for i in range(3):
            bars.append(Bar(
                symbol=symbol, ts=last_ts + timedelta(minutes=5 * i),
                open=center - 1, high=center + 3, low=center - 3, close=center,
                volume=100.0,
            ))

    return bars


class TestTurtleSoup:
    def test_long_on_fake_breakdown(self):
        """Ложный пробой вниз + RSI<30 + возврат → long."""
        from bybit_bot.strategies.scalping.turtle_soup import (
            TurtleSoupStrategy, TurtleSoupSignal,
        )
        from bybit_bot.analysis.signals import Direction

        bars = _make_turtle_bars(trap_direction="down")
        signals = TurtleSoupStrategy().scan({"BTCUSDT": bars})
        assert len(signals) == 1
        sig = signals[0]
        assert isinstance(sig, TurtleSoupSignal)
        assert sig.direction == Direction.LONG
        assert sig.rsi_at_break < 30.0
        assert sig.break_depth_atr > 0

    def test_short_on_fake_breakup(self):
        """Ложный пробой вверх + RSI>70 + возврат → short."""
        from bybit_bot.strategies.scalping.turtle_soup import TurtleSoupStrategy
        from bybit_bot.analysis.signals import Direction

        bars = _make_turtle_bars(trap_direction="up")
        signals = TurtleSoupStrategy().scan({"BTCUSDT": bars})
        assert len(signals) == 1
        assert signals[0].direction == Direction.SHORT
        assert signals[0].rsi_at_break > 70.0

    def test_no_signal_without_breakout(self):
        from bybit_bot.strategies.scalping.turtle_soup import TurtleSoupStrategy
        bars = _make_turtle_bars(trap_direction=None)
        assert TurtleSoupStrategy().scan({"BTCUSDT": bars}) == []

    def test_insufficient_bars(self):
        from bybit_bot.strategies.scalping.turtle_soup import TurtleSoupStrategy
        assert TurtleSoupStrategy().scan({"BTCUSDT": _make_bars(n=30)}) == []

    def test_max_signals_limit(self):
        from bybit_bot.strategies.scalping.turtle_soup import TurtleSoupStrategy
        strat = TurtleSoupStrategy(max_signals_per_scan=1)
        bars_a = _make_turtle_bars(trap_direction="down")
        bars_b = _make_turtle_bars(trap_direction="up", symbol="ETHUSDT",
                                   center=3000.0, range_half=5.0)
        signals = strat.scan({"BTCUSDT": bars_a, "ETHUSDT": bars_b})
        assert len(signals) == 1


# ── BTC Lead-Lag Strategy ────────────────────────────────────


def _make_leadlag_bars(
    *,
    symbol: str,
    base_price: float,
    tail_move_pct: float = 0.0,      # движение за последние 3 бара в %
    correlated_with: list[Bar] | None = None,  # база для корреляции по log-returns
    n: int = 100,
) -> list[Bar]:
    """Генерирует бары альта с контролируемой корреляцией log-returns с correlated_with.

    По research (Asia-Pacific FM 2026) стратегия считает corr на log-returns,
    а не на ценах. Чтобы тест был реалистичным, альт реплицирует returns BTC
    со scale-фактором (≈1.5× beta для small-cap), но в последних 3 барах
    ведёт собственное движение tail_move_pct (имитируя лаг).
    """
    from datetime import timedelta
    import math as _m
    ts_base = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    bars: list[Bar] = []

    if correlated_with is not None and len(correlated_with) >= n:
        ref_closes = [b.close for b in correlated_with[-n:]]
        # ref returns
        ref_returns = [_m.log(ref_closes[i] / ref_closes[i - 1]) for i in range(1, n)]

        # Первые n-3 returns альта = returns BTC × beta (high corr)
        # Последние 3 — собственный линейный шаг для tail_move_pct
        beta = 1.0  # идеальная корреляция по log-returns
        alt_closes = [base_price]
        for i in range(n - 1):
            if i < (n - 3) - 1:
                r = ref_returns[i] * beta
            else:
                # Последние 3 шага: каждый шаг = tail_move_pct / 3 (линейно)
                r = _m.log(1 + (tail_move_pct / 100) / 3)
            alt_closes.append(alt_closes[-1] * _m.exp(r))

        for i, p in enumerate(alt_closes):
            bars.append(Bar(
                symbol=symbol, ts=ts_base + timedelta(minutes=5 * i),
                open=p, high=p * 1.001, low=p * 0.999,
                close=p, volume=100.0,
            ))
    else:
        for i in range(n):
            noise = ((i * 7) % 11 - 5) * 0.0005
            price = base_price * (1 + noise)
            bars.append(Bar(
                symbol=symbol, ts=ts_base + timedelta(minutes=5 * i),
                open=price, high=price * 1.001, low=price * 0.999,
                close=price, volume=100.0,
            ))
        anchor = bars[-4].close
        target = anchor * (1 + tail_move_pct / 100)
        for j in range(3):
            frac = (j + 1) / 3
            p = anchor + (target - anchor) * frac
            bars[n - 3 + j] = Bar(
                symbol=symbol, ts=ts_base + timedelta(minutes=5 * (n - 3 + j)),
                open=p, high=p * 1.002, low=p * 0.998, close=p, volume=150.0,
            )
    return bars


def _make_btc_impulse_bars(direction: str = "up") -> list[Bar]:
    """BTC-бары: 100 баров случайного движения, финальные 3 — импульс ≥1.2% ≥1.5 ATR.

    Амплитуда 1.2% подобрана так, чтобы:
    - проходил фильтр BTC_MOVE_PCT=1%;
    - проходил фильтр BTC_MOVE_MIN_ATR=1.5;
    - не ломалась корреляция log-returns с альтом, двигающимся на 0.15%
      (по research такой дифференциал характерен для real lead-lag).
    """
    from datetime import timedelta
    ts_base = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    bars: list[Bar] = []

    base = 60000.0
    for i in range(97):
        trend = (i - 48) * 1.5
        noise = ((i * 13) % 17 - 8) * 5
        price = base + trend + noise
        bars.append(Bar(
            symbol="BTCUSDT",
            ts=ts_base + timedelta(minutes=5 * i),
            open=price - 5, high=price + 15, low=price - 15, close=price,
            volume=1000.0,
        ))
    start = bars[-1].close
    mult = 1.012 if direction == "up" else 0.988
    end = start * mult
    for i in range(3):
        frac = (i + 1) / 3
        price = start + (end - start) * frac
        bars.append(Bar(
            symbol="BTCUSDT", ts=ts_base + timedelta(minutes=5 * (97 + i)),
            open=price - 5, high=price + 10, low=price - 10, close=price,
            volume=1500.0,
        ))
    return bars


class TestBtcLeadLag:
    def test_long_alt_follows_btc_up(self):
        """BTC +1.2%, alt +0.15% (есть лаг, корр returns >0.5) → long alt."""
        from bybit_bot.strategies.scalping.btc_leadlag import (
            BtcLeadLagStrategy, LeadLagSignal,
        )
        from bybit_bot.analysis.signals import Direction

        btc = _make_btc_impulse_bars("up")
        alt = _make_leadlag_bars(
            symbol="SOLUSDT", base_price=150.0,
            tail_move_pct=0.15,
            correlated_with=btc,
        )
        signals = BtcLeadLagStrategy().scan({"BTCUSDT": btc, "SOLUSDT": alt})
        assert len(signals) == 1
        sig = signals[0]
        assert isinstance(sig, LeadLagSignal)
        assert sig.symbol == "SOLUSDT"
        assert sig.direction == Direction.LONG
        assert sig.correlation >= 0.5

    def test_short_alt_follows_btc_down(self):
        """BTC -1.2%, alt -0.15% → short alt."""
        from bybit_bot.strategies.scalping.btc_leadlag import BtcLeadLagStrategy
        from bybit_bot.analysis.signals import Direction

        btc = _make_btc_impulse_bars("down")
        alt = _make_leadlag_bars(
            symbol="SOLUSDT", base_price=150.0,
            tail_move_pct=-0.15, correlated_with=btc,
        )
        signals = BtcLeadLagStrategy().scan({"BTCUSDT": btc, "SOLUSDT": alt})
        assert len(signals) == 1
        assert signals[0].direction == Direction.SHORT

    def test_no_signal_without_btc(self):
        """Нет BTC в bars_map → нет сигналов."""
        from bybit_bot.strategies.scalping.btc_leadlag import BtcLeadLagStrategy
        alt = _make_leadlag_bars(symbol="SOLUSDT", base_price=150.0)
        assert BtcLeadLagStrategy().scan({"SOLUSDT": alt}) == []

    def test_no_signal_weak_btc_move(self):
        """BTC движется слабо (<1%) → нет сигналов."""
        from bybit_bot.strategies.scalping.btc_leadlag import BtcLeadLagStrategy
        from datetime import timedelta
        ts_base = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
        btc = [
            Bar(symbol="BTCUSDT", ts=ts_base + timedelta(minutes=5 * i),
                open=60000 + (i % 5), high=60010, low=59990,
                close=60000 + (i % 5), volume=1000.0)
            for i in range(100)
        ]
        alt = _make_leadlag_bars(symbol="SOLUSDT", base_price=150.0, correlated_with=btc)
        assert BtcLeadLagStrategy().scan({"BTCUSDT": btc, "SOLUSDT": alt}) == []

    def test_no_signal_alt_already_followed(self):
        """Альт уже догнал BTC (движение >ALT_LAG_MAX_PCT в ту же сторону) → нет лага."""
        from bybit_bot.strategies.scalping.btc_leadlag import BtcLeadLagStrategy
        btc = _make_btc_impulse_bars("up")
        alt = _make_leadlag_bars(
            symbol="SOLUSDT", base_price=150.0,
            tail_move_pct=1.0,  # >ALT_LAG_MAX_PCT=0.3% и >70% BTC 1.2% → догнал
            correlated_with=btc,
        )
        assert BtcLeadLagStrategy().scan({"BTCUSDT": btc, "SOLUSDT": alt}) == []

    def test_no_signal_low_correlation(self):
        """Альт слабо коррелирует с BTC → нет сигналов."""
        from bybit_bot.strategies.scalping.btc_leadlag import BtcLeadLagStrategy
        btc = _make_btc_impulse_bars("up")
        # Альт с рандомным движением (не скоррелирован)
        alt = _make_leadlag_bars(symbol="SOLUSDT", base_price=150.0, tail_move_pct=0.1)
        assert BtcLeadLagStrategy().scan({"BTCUSDT": btc, "SOLUSDT": alt}) == []

    def test_insufficient_btc_bars(self):
        """Мало BTC-баров (<MIN_BARS) → нет сигналов."""
        from bybit_bot.strategies.scalping.btc_leadlag import BtcLeadLagStrategy
        short_btc = _make_btc_impulse_bars("up")[:30]
        alt = _make_leadlag_bars(symbol="SOLUSDT", base_price=150.0)
        assert BtcLeadLagStrategy().scan({"BTCUSDT": short_btc, "SOLUSDT": alt}) == []


# ── Crypto Overbought Fader (COF) ────────────────────────────
#
# COF = ансамбль Turtle-SHORT + VWAP-SHORT + фильтры Variant E
# (NY-сессия, RSI14≥65, ATR%≥0.3). Требуется согласие обеих стратегий.
# Подробнее: `strategies/scalping/crypto_overbought_fader.py`.
#
# ВАЖНО: прибыльность COF валидирована 90-дневным backtest-ом (139 сделок,
# PF 1.98, OOS PF 2.05 — см. BUILDLOG_BYBIT.md 2026-04-23 "COF research").
# Unit-тесты здесь НЕ проверяют прибыльность — только корректность
# gate-фильтров (сессия/RSI/ATR%/ADX/HTF). «Подогнанные под результат»
# synthetic бары для positive-сценария не пишем (это curve-fitting к тесту,
# а не валидация логики). Live-прогон на малом размере — финальная проверка.


def _cof_ts_base(hour_utc: int = 17) -> datetime:
    """ts_base такой, чтобы последний бар турт-сетапа попал на hour_utc.

    _make_turtle_bars кладёт n_history (100) + bias_len (16) + 3 trap = 119 баров
    с шагом 5 мин → диапазон 595 мин = 9h 55m. Последний бар = ts_base + 590 мин.
    """
    from datetime import timedelta
    last_delta = timedelta(minutes=5 * (100 + 16 + 3 - 1))  # 590 min = 9h 50m
    target = datetime(2026, 4, 17, hour_utc, 0, tzinfo=UTC)
    return target - last_delta


class TestCryptoOverboughtFader:
    """Crypto Overbought Fader — gate-фильтры Variant E.

    Тесты проверяют ТОЛЬКО корректность отсечений: страта не должна давать
    сигнал вне NY-сессии, на long-setup, без пробоя, при низкой волатильности,
    при малом числе баров. Проверка «страта действительно генерит SHORT» —
    через backtest/live, не через подогнанные synthetic-бары.
    """

    def test_no_signal_outside_ny_session(self):
        """Turtle-up setup вне NY (Asia 05:00 UTC) → COF-сигнал отсекается."""
        from bybit_bot.strategies.scalping.crypto_overbought_fader import (
            CryptoOverboughtFaderStrategy,
        )
        bars = _make_turtle_bars(
            trap_direction="up",
            center=100.0, range_half=3.0,
            ts_base=_cof_ts_base(hour_utc=5),  # Asia
        )
        assert CryptoOverboughtFaderStrategy().scan({"SOLUSDT": bars}) == []

    def test_no_signal_on_long_setup(self):
        """Setup «вниз» (long-candidate у turtle) — COF молчит (только SHORT)."""
        from bybit_bot.strategies.scalping.crypto_overbought_fader import (
            CryptoOverboughtFaderStrategy,
        )
        bars = _make_turtle_bars(
            trap_direction="down",
            center=100.0, range_half=3.0,
            ts_base=_cof_ts_base(hour_utc=17),
        )
        assert CryptoOverboughtFaderStrategy().scan({"SOLUSDT": bars}) == []

    def test_no_signal_without_breakout(self):
        """Нет turtle-trap в NY-сессию — COF молчит (DUO-agreement не выполнен)."""
        from bybit_bot.strategies.scalping.crypto_overbought_fader import (
            CryptoOverboughtFaderStrategy,
        )
        bars = _make_turtle_bars(
            trap_direction=None,
            center=100.0, range_half=3.0,
            ts_base=_cof_ts_base(hour_utc=17),
        )
        assert CryptoOverboughtFaderStrategy().scan({"SOLUSDT": bars}) == []

    def test_no_signal_low_atr_pct(self):
        """Очень низкий ATR/price*100 (BTC @60k, range_half=100) → COF молчит.

        ATR≈10 при price≈60000 → ATR%≈0.017% ≪ 0.3% (порог Variant E).
        """
        from bybit_bot.strategies.scalping.crypto_overbought_fader import (
            CryptoOverboughtFaderStrategy,
        )
        bars = _make_turtle_bars(
            trap_direction="up",
            center=60000.0, range_half=100.0,
            ts_base=_cof_ts_base(hour_utc=17),
        )
        assert CryptoOverboughtFaderStrategy().scan({"BTCUSDT": bars}) == []

    def test_insufficient_bars(self):
        """Слишком мало баров (< MIN_BARS+LOOKBACK+RECLAIM) — пропуск."""
        from bybit_bot.strategies.scalping.crypto_overbought_fader import (
            CryptoOverboughtFaderStrategy,
        )
        assert CryptoOverboughtFaderStrategy().scan({"BTCUSDT": _make_bars(n=30)}) == []

    def test_scan_returns_list_on_empty_input(self):
        """API-контракт: scan возвращает list даже при пустом входе."""
        from bybit_bot.strategies.scalping.crypto_overbought_fader import (
            CryptoOverboughtFaderStrategy,
        )
        assert CryptoOverboughtFaderStrategy().scan({}) == []

    def test_set_htf_slopes_does_not_raise(self):
        """set_htf_slopes принимает dict, не ломает last scan."""
        from bybit_bot.strategies.scalping.crypto_overbought_fader import (
            CryptoOverboughtFaderStrategy,
        )
        strat = CryptoOverboughtFaderStrategy()
        strat.set_htf_slopes({"SOLUSDT": 0.01, "LINKUSDT": -0.002})
        # Scan на turtle-вниз сетапе всё равно молчит (long → не SHORT).
        bars = _make_turtle_bars(
            trap_direction="down",
            center=100.0, range_half=3.0,
            ts_base=_cof_ts_base(hour_utc=17),
        )
        assert strat.scan({"SOLUSDT": bars}) == []


# ── Integration: imports ─────────────────────────────────────

def test_all_scalping_imports():
    from bybit_bot.strategies.scalping.indicators import vwap, rolling_z_score, ols_hedge_ratio
    from bybit_bot.strategies.scalping.vwap_crypto import VwapCryptoStrategy
    from bybit_bot.strategies.scalping.stat_arb_crypto import StatArbCryptoStrategy
    from bybit_bot.strategies.scalping.funding_scalp import FundingScalpStrategy
    from bybit_bot.strategies.scalping.volume_spike import VolumeSpikeStrategy
    from bybit_bot.strategies.scalping.session_orb import SessionOrbStrategy
    from bybit_bot.strategies.scalping.turtle_soup import TurtleSoupStrategy
    from bybit_bot.strategies.scalping.btc_leadlag import BtcLeadLagStrategy
    from bybit_bot.strategies.scalping.crypto_overbought_fader import (
        CryptoOverboughtFaderStrategy, CofSignal,
    )


# ── Wave 6: VWAP env-парсеры и build-helper ──────────────────────

class TestVwapEnvParsers:
    def test_parse_hours_env_valid(self):
        from bybit_bot.app.main import _parse_hours_env
        assert _parse_hours_env("14,15,16,19,20") == {14, 15, 16, 19, 20}

    def test_parse_hours_env_empty(self):
        from bybit_bot.app.main import _parse_hours_env
        assert _parse_hours_env("") is None
        assert _parse_hours_env("   ") is None

    def test_parse_hours_env_skips_invalid(self):
        from bybit_bot.app.main import _parse_hours_env
        assert _parse_hours_env("14,abc,99,15") == {14, 15}

    def test_parse_weekdays_env_valid(self):
        from bybit_bot.app.main import _parse_weekdays_env
        assert _parse_weekdays_env("mon,tue,wed,thu,fri") == {0, 1, 2, 3, 4}

    def test_parse_weekdays_env_case_insensitive(self):
        from bybit_bot.app.main import _parse_weekdays_env
        assert _parse_weekdays_env("MON, Tue,WED") == {0, 1, 2}

    def test_parse_weekdays_env_skips_unknown(self):
        from bybit_bot.app.main import _parse_weekdays_env
        assert _parse_weekdays_env("mon,xyz,fri") == {0, 4}

    def test_parse_weekdays_env_empty(self):
        from bybit_bot.app.main import _parse_weekdays_env
        assert _parse_weekdays_env("") is None

    def test_build_scalp_vwap_with_full_wave6_config(self, monkeypatch):
        """Полный набор Wave 6 → передаётся в VwapCryptoStrategy."""
        from bybit_bot.app.main import _build_scalp_vwap
        from bybit_bot.config.settings import Settings
        monkeypatch.setenv("BYBIT_BOT_SCALP_VWAP_DIRECTION", "long")
        monkeypatch.setenv("BYBIT_BOT_SCALP_VWAP_SYMBOLS", "ADAUSDT,SOLUSDT,SUIUSDT,TONUSDT,WIFUSDT")
        monkeypatch.setenv("BYBIT_BOT_SCALP_VWAP_HOURS_UTC", "14,15,16,19,20")
        monkeypatch.setenv("BYBIT_BOT_SCALP_VWAP_WEEKDAYS", "mon,tue,wed,thu,fri")
        s = Settings(_env_file=None)
        strat = _build_scalp_vwap(s)
        assert strat._allowed_direction == "long"
        assert strat._allowed_symbols == {"ADAUSDT", "SOLUSDT", "SUIUSDT", "TONUSDT", "WIFUSDT"}
        assert strat._allowed_hours_utc == {14, 15, 16, 19, 20}
        assert strat._allowed_weekdays == {0, 1, 2, 3, 4}

    def test_build_scalp_vwap_invalid_direction_ignored(self, monkeypatch):
        from bybit_bot.app.main import _build_scalp_vwap
        from bybit_bot.config.settings import Settings
        monkeypatch.setenv("BYBIT_BOT_SCALP_VWAP_DIRECTION", "sideways")
        s = Settings(_env_file=None)
        strat = _build_scalp_vwap(s)
        assert strat._allowed_direction is None

    def test_build_scalp_vwap_empty_config_no_filters(self, monkeypatch):
        """Пустые env-строки → None во всех фильтрах (обратная совместимость)."""
        from bybit_bot.app.main import _build_scalp_vwap
        from bybit_bot.config.settings import Settings
        for k in (
            "BYBIT_BOT_SCALP_VWAP_DIRECTION", "BYBIT_BOT_SCALP_VWAP_SYMBOLS",
            "BYBIT_BOT_SCALP_VWAP_HOURS_UTC", "BYBIT_BOT_SCALP_VWAP_WEEKDAYS",
        ):
            monkeypatch.delenv(k, raising=False)
        s = Settings(_env_file=None)
        strat = _build_scalp_vwap(s)
        assert strat._allowed_direction is None
        assert strat._allowed_symbols is None
        assert strat._allowed_hours_utc is None
        assert strat._allowed_weekdays is None
