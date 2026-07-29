"""Сетап «hook» / failed auction (C5, канон Fabervaale «The Only Orderflow
Guide» 26:17 и 27:20).

Канон описывает его как отдельный, самый надёжный по win rate сетап:

    *«when we go back to the value area low and down, we do what we call the
    hook. So they do a failed auction, they try to break, they get rejected.
    So all this it's a rejection area. And when you go back inside, you have
    your continuation trade»* (26:17)

    *«it also hook the value area high from the downside. And this is what we
    like to call a fake out, a failed auction trap traders… usually the price
    slice through the value area to go to seek orders on the value area low.
    This is one really profitable setup with high win rate»* (27:20)

Механика: цена выходит за границу value area ПРОТИВ направления аукциона, НЕ
принимается там (failed auction — объёма за границей мало), возвращается внутрь
VA — и это вход в сторону аукциона. Для лонга неудачная вылазка происходит ниже
VAL, для шорта — выше VAH.

Отличие от основного сетапа §4 (reload-зона + absorption): там вход по
подтверждению потоком в зоне конфлюэнса, здесь — по ОТВЕРЖЕНИЮ границы value
area. Стоп естественно ставится за экстремум неудачной вылазки: если рынок
всё-таки примет цену за границей, тезис «failed auction» мёртв.

Порог «не приняли» выведен из канон-константы Value Area 68%, а не подобран:
доля объёма за границей должна быть меньше нейтральной вне-VA массы
``1 − value_area_pct`` (та же логика, что у ``context.classify``). Новых
magic-number-ов не вводим (no-data-fitting.mdc).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from flowzone_bot.data.aggregates import TradePrint


@dataclass
class Hook:
    """Результат проверки failed auction у границы value area."""
    confirmed: bool
    side: str                    # сторона сделки: long | short
    boundary: float = 0.0        # тестируемая граница VA (VAL для long, VAH для short)
    extreme: float = 0.0         # экстремум неудачной вылазки за границу
    beyond_frac: float = 0.0     # доля объёма, наторгованного за границей
    reasons: list[str] = field(default_factory=list)


def detect_hook(prints: list[TradePrint], side: str, *, vah: float | None,
                val: float | None, last_price: float | None,
                value_area_pct: float = 0.68) -> Hook:
    """Failed auction у границы value area с возвратом внутрь.

    ``side`` — сторона сделки по направлению аукциона (continuation): для
    ``"long"`` ищем отвергнутую вылазку НИЖЕ ``val``, для ``"short"`` — ВЫШЕ
    ``vah``. ``prints`` — исполненный поток за окно наблюдения (persisted-тики,
    не 5-минутный снапшот: hook разворачивается дольше одной M5-свечи).

    Условия (все обязательны):
    1. цена реально выходила за границу (есть принты за ней);
    2. объёма за границей мало — ``beyond_frac < 1 − value_area_pct``: рынок
       НЕ принял цену снаружи (канон «failed auction», а не пробой);
    3. цена вернулась ВНУТРЬ value area (``val ≤ last_price ≤ vah``) — канон
       «when you go back inside, you have your continuation trade».
    """
    if side not in ("long", "short") or not prints:
        return Hook(False, side, reasons=["no_data"])
    if vah is None or val is None or last_price is None or vah <= val:
        return Hook(False, side, reasons=["no_value_area"])
    if not (val <= last_price <= vah):
        return Hook(False, side, reasons=["not_back_inside"])

    if side == "long":
        boundary = val
        beyond = [t for t in prints if t.price < val]
        extreme = min((t.price for t in beyond), default=val)
    else:
        boundary = vah
        beyond = [t for t in prints if t.price > vah]
        extreme = max((t.price for t in beyond), default=vah)
    if not beyond:
        return Hook(False, side, boundary=boundary, reasons=["no_excursion"])

    total = sum(t.size for t in prints)
    if total <= 0:
        return Hook(False, side, boundary=boundary, reasons=["no_volume"])
    beyond_frac = sum(t.size for t in beyond) / total
    max_beyond = 1.0 - value_area_pct
    reasons = [f"hook_beyond={beyond_frac:.0%}"]
    if beyond_frac >= max_beyond:
        # за границей наторговали столько же, сколько держит нейтральный хвост —
        # это принятие (реальный пробой), а не отвергнутая вылазка
        reasons.append("accepted_outside")
        return Hook(False, side, boundary=boundary, extreme=extreme,
                    beyond_frac=beyond_frac, reasons=reasons)
    reasons.append("back_inside_va")
    return Hook(True, side, boundary=boundary, extreme=extreme,
                beyond_frac=beyond_frac, reasons=reasons)
