"""Стандартный тариф Bybit и проверка «символ дороже стандарта».

Зачем отдельный модуль. Вся наша модель издержек построена на стандартной
сетке Bybit: taker 0.055%, maker 0.02%
(<https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate>). Из неё
выведена константа ``cfg.round_trip_fee_frac`` и заявленный в settings.py
инвариант «комиссия ≤ 1/min_risk_fee_mult доля R». Но тариф — свойство
КОНТРАКТА, и он не универсален: замер 2026-08-06 (BUILDLOG `cae61f4`) показал,
что BANKUSDT и ESPORTSUSDT берут ровно вдвое больше стандарта. Заранее, до
сделки, узнать это на demo нельзя: эндпоинта ``/v5/account/fee-rate`` в
demo-списке API нет — поэтому ставку учим из филлов (``symbol_fees``).

Комиссия в R равна ставке, делённой на ширину стопа, поэтому двойной тариф
удваивает и издержки в R: замер по density_break — 0.269R в среднем, но
BANKUSDT отдельно −0.851R чистыми на сделку при валовом −0.464R. Это не вывод
по P&L на 10 сделках (что запрещено sample-size.mdc), а арифметика: цена входа
в такой контракт вдвое выше той, из которой считают наши гейты.
"""

from __future__ import annotations

# Стандартная сетка Bybit для linear perpetual, non-VIP.
# https://bybit-exchange.github.io/docs/v5/enum#tradingfeerate
STANDARD_MAKER_FEE = 0.0002
STANDARD_TAKER_FEE = 0.00055

# Численный допуск на сравнение со сеткой: ставка приходит округлённой, и
# промо/VIP-скидки могут дать небольшое отклонение ВНИЗ. Это не подбираемый
# порог: наблюдаемые тарифы дискретны — либо ровно стандарт (×1.0), либо ровно
# двойной (×2.0), и 5% отделяет их с огромным запасом.
FEE_TARIFF_TOLERANCE = 0.05


def is_above_standard(rate: float | None, standard: float,
                      tolerance: float = FEE_TARIFF_TOLERANCE) -> bool:
    """Ставка выше стандартной сетки (за пределами численного допуска)?

    ``None`` — ставка ещё не выучена, это НЕ повод блокировать: до первого
    филла тариф на demo не узнать, и fail-closed здесь остановил бы торговлю
    на любом новом символе.
    """
    if rate is None or standard <= 0:
        return False
    return rate > standard * (1.0 + tolerance)


def nonstandard_tariff(maker_rate: float | None, taker_rate: float | None,
                       tolerance: float = FEE_TARIFF_TOLERANCE) -> bool:
    """Хоть одна из ног дороже стандарта → весь символ в другом режиме издержек.

    Проверяем обе ноги, потому что вход и выход могут быть разного типа
    (maker-лимитка на входе, taker-выход по SL/TP), и достаточно одной дорогой
    ноги, чтобы round-trip перестал соответствовать нашей константе.
    """
    return (is_above_standard(maker_rate, STANDARD_MAKER_FEE, tolerance)
            or is_above_standard(taker_rate, STANDARD_TAKER_FEE, tolerance))
