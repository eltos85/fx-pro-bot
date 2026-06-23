"""5 Why движок (канон §5, метод Toyota) над агрегатами паттерна — read-only.

На главную повторяющуюся тему периода задаём «почему?» 5 раз через DeepSeek.
Канон-нюанс для детерминированного momentum-бота: решение часто **не про
систему, а про рынок** → 5 Why должен искать **режим/условие**, в котором
TSMOM-сетап валиден (новый фильтр/playbook), а НЕ «сделай стоп туже» (анти-канон).
Выход — цепочка + **гипотеза-решение** (≤200 симв.) как кандидат на ручную
проверку, не disable.

LLM ничего не меняет; гипотеза пишется в собственную БД tradecard (advisory §8).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from tradecard_momentum.analysis.trade import MomentumTrade
from tradecard_momentum.llm.client import DeepSeekClient

# Канон стратегии momentum (источник правды — research-блоки fx_momentum_bot).
# Даём LLM понять, что правило задумано как research-based, а не «баг».
_STRATEGY_CANON = (
    "fx_momentum_bot: time-series momentum (Moskowitz/Ooi/Pedersen 2012). Вход — "
    "edge-trigger на СМЕНЕ направления при |momentum| > порога; ATR-стоп "
    "(sl_dist = ATR×mult). Выход — TSMOM sign-rule (закрытие при пересечении "
    "momentum нуля против позиции) + BE@1R (Van Tharp) + partial@1.5R (Raschke) "
    "+ ATR-trailing (Turtle/LeBeau Chandelier). Event-guard блокирует входы ±60м "
    "вокруг HIGH-impact релизов; spread-guard режет вход при широком спреде."
)

_PATTERN_HINT = {
    "signal_not_predictive": "сила сигнала (|momentum|) не отделяет винов — порог "
                             "входа/величина импульса могут не отражать edge в "
                             "текущем режиме (тренд vs флет).",
    "symbol_session_leak": "бот теряет в конкретном символе/сессии/стороне — "
                           "возможно нет фильтра режима, где TSMOM валиден.",
    "loss_cluster": "повтор убытков на связке symbol×side — уровень/режим, где "
                    "тренд-следование не работает (пила/range).",
    "overtrading": "перегретые часы хуже — дребезг edge-trigger вокруг порога / "
                   "вход на шуме.",
    "swap_drag": "overnight financing съедает прибыль удерживаемых позиций — "
                 "вопрос hold-time / размера / выбора инструмента, не сигнала.",
}

_SYSTEM = (
    "Ты — аналитик торгового деска SMB (Momentum Model, 5-Step Process). Перед "
    "тобой РЕТРОСПЕКТИВА детерминированного rule-based бота (time-series momentum, "
    "без дискреции и психологии). 'Ошибка' = повторяющийся убыточный ПАТТЕРН "
    "правил, не эмоции. Задай 'почему?' 5 раз (метод Toyota): каждое 'почему' "
    "опирается на данные. Настоящая причина обычно на 4-5-м why и часто 'про "
    "рынок, а не про систему' — ищи РЕЖИМ/УСЛОВИЕ, в котором TSMOM-сетап валиден "
    "(новый фильтр/playbook), а НЕ 'сделай стоп туже' (это анти-канон). Не "
    "предлагай отключать инструмент по малой выборке. Ответ — кандидат-ГИПОТЕЗА "
    "для ручной проверки человеком, НЕ готовое изменение конфига. Пиши по-русски, "
    "кратко."
)

_OUTPUT_SPEC = (
    "\n\nФОРМАТ ОТВЕТА (строго):\n"
    "WHY1: ...\nWHY2: ...\nWHY3: ...\nWHY4: ...\nWHY5: ...\n"
    "ГИПОТЕЗА: <одно предложение ≤200 символов: фильтр режима / session-гейт / "
    "перекалибровка порога входа / hold-time / новый playbook>"
)


@dataclass
class FiveWhyResult:
    chain: list[str]
    hypothesis: str
    raw: str
    error: str | None = None


def build_prompt(*, code: str, scope: dict, n: int, wr: float,
                 exp_r: float | None, net: float,
                 samples: list[MomentumTrade]) -> str:
    hint = _PATTERN_HINT.get(code, "")
    lines = [
        f"ПАТТЕРН (тема №1 периода): {code}",
        f"Подсказка по паттерну: {hint}" if hint else "",
        f"Канон стратегии (research-based, НЕ баг): {_STRATEGY_CANON}",
        f"Срез: {scope}",
        (f"Агрегаты: сделок={n}, WR={wr:.0%}, EXP(avgR)={exp_r:.2f}"
         if exp_r is not None else
         f"Агрегаты: сделок={n}, WR={wr:.0%}, EXP=n/a"),
        f"net P&L среза = ${net:.2f}",
        "",
        "Репрезентативные сделки (symbol/side/|momentum|вх/netR/net$):",
    ]
    for t in samples[:5]:
        r = t.r_multiple
        mom = (f"{t.signal_momentum:.4f}" if t.signal_momentum is not None
               else "n/a")
        rstr = f"{r:.2f}" if r is not None else "n/a"
        lines.append(
            f"  pid={t.position_id} {t.symbol} {t.side} |mom|вх={mom} "
            f"R={rstr} net=${t.net_usd:+.2f}")
    prompt = "\n".join(x for x in lines if x != "") + _OUTPUT_SPEC
    return prompt


def parse_response(text: str) -> tuple[list[str], str]:
    """Извлечь цепочку WHY1..5 и ГИПОТЕЗУ из ответа LLM."""
    chain: list[str] = []
    for i in range(1, 6):
        m = re.search(rf"WHY\s*{i}\s*[:.)]\s*(.+)", text, re.IGNORECASE)
        if m:
            chain.append(m.group(1).strip())
    hm = re.search(r"ГИПОТЕЗА\s*[:.)]\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    hypothesis = ""
    if hm:
        hypothesis = hm.group(1).strip().splitlines()[0].strip()
    hypothesis = hypothesis[:200]
    return chain, hypothesis


def run_five_why(client: DeepSeekClient, *, code: str, scope: dict, n: int,
                 wr: float, exp_r: float | None, net: float,
                 samples: list[MomentumTrade]) -> FiveWhyResult:
    prompt = build_prompt(code=code, scope=scope, n=n, wr=wr, exp_r=exp_r,
                          net=net, samples=samples)
    resp = client.ask(_SYSTEM, prompt)
    if resp.error:
        return FiveWhyResult(chain=[], hypothesis="", raw="", error=resp.error)
    chain, hyp = parse_response(resp.text)
    return FiveWhyResult(chain=chain, hypothesis=hyp, raw=resp.text)
