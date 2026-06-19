"""5 Why движок (канон §5, метод Toyota) над агрегатами паттерна — read-only.

На главную повторяющуюся тему периода задаём «почему?» 5 раз через DeepSeek.
Канон-нюанс для детерминированных ботов: решение часто **не про систему, а про
рынок** → 5 Why должен искать **режим/условие**, в котором сетап валиден (новый
фильтр/playbook), а НЕ «сделай стоп туже» (анти-канон §10). Выход — цепочка +
**гипотеза-решение** (≤200 симв.) как кандидат на ручную проверку, не disable.

LLM ничего не меняет; гипотеза пишется в собственную БД tradecard (advisory §8).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from tradecard_bybit.analysis.trade import Trade
from tradecard_bybit.llm.client import DeepSeekClient

# Краткий канон страт (источник правды — STRATEGY_*/STRATEGY_FLOWZONE.md). Даём
# LLM понять, что правило задумано как research-based, а не «баг» (TASKSPEC §6).
_STRATEGY_CANON = {
    "sweep_fade": "CAP order-flow: свип ликвидности + CVD-дивергенция + reclaim "
                  "(CHoCH) + разворот ленты; mean-reversion fade ТОЛЬКО по HTF-тренду "
                  "(EMA200 15m) и не в сильный тренд (ADX≥30, Connors/Raschke).",
    "sweep_fade_canon": "Канон Turtle Soup (Connors/Raschke 1995): свип ЗНАЧИМОГО "
                        "уровня (PDH/PDL) + full reclaim + taker-вход на возврате; "
                        "вселенная ликвидных мейджоров.",
    "density_break": "Momentum-пробой выстоявшей плотности: вход на ЗАКРЫТИИ за "
                     "уровнем + CVD follow-through (анти-grab), taker; не фейд.",
    "density_bounce": "Фейд от resting-плотности (стена выстояла 20–30+ мин, "
                      "Bookmap absorption); вход maker.",
    "flowzone": "Auction/Volume-Profile continuation (Steidlmayer/Dalton): "
                "acceptance за value area → зона confluence ≥2 VP-факторов → "
                "absorption контр-стороны → вход по тренду аукциона.",
}

_PATTERN_HINT = {
    "grade_not_predictive": "score не отделяет винов — грейдинг/веса факторов "
                            "могут не отражать реальный edge в текущем режиме.",
    "strategy_regime_leak": "страта теряет в конкретном срезе — возможно нет "
                            "фильтра режима/сессии/символа, где сетап валиден.",
    "sl_cluster": "повтор стопов на связке — уровень/режим, где сетап не работает.",
    "exit_left_money": "правило выхода фиксирует до значимого продолжения.",
    "factor_noise": "фактор не улучшает EXP — кандидат на удаление (как scalp v0.9.0).",
    "overtrading": "перегретые часы хуже — возможно вход на шуме/нулевом edge.",
    "big_game_hunting": "дрейф к редкому A+ при рабочем baseline (канон §8).",
    "paper_live_divergence": "валидно на paper, проигрывает на live — slippage/"
                             "fees/исполнение/режим.",
}

_SYSTEM = (
    "Ты — аналитик торгового деска SMB (Momentum Model, 5-Step Process). Перед "
    "тобой РЕТРОСПЕКТИВА детерминированного rule-based бота (без дискреции и "
    "психологии). 'Ошибка' = повторяющийся убыточный ПАТТЕРН правил, не эмоции. "
    "Задай 'почему?' 5 раз (метод Toyota): каждое 'почему' опирается на данные. "
    "Настоящая причина обычно на 4-5-м why и часто 'про рынок, а не про систему' "
    "— ищи РЕЖИМ/УСЛОВИЕ, в котором сетап валиден (новый фильтр/playbook), а НЕ "
    "'сделай стоп туже' (это анти-канон). Не предлагай отключать инструмент по "
    "малой выборке. Ответ — кандидат-ГИПОТЕЗА для ручной проверки человеком, НЕ "
    "готовое изменение конфига. Пиши по-русски, кратко."
)

_OUTPUT_SPEC = (
    "\n\nФОРМАТ ОТВЕТА (строго):\n"
    "WHY1: ...\nWHY2: ...\nWHY3: ...\nWHY4: ...\nWHY5: ...\n"
    "ГИПОТЕЗА: <одно предложение ≤200 символов: фильтр режима / session-гейт / "
    "перекалибровка score / удаление factor-noise / новый playbook>"
)


@dataclass
class FiveWhyResult:
    chain: list[str]
    hypothesis: str
    raw: str
    error: str | None = None


def build_prompt(*, code: str, strategy: str | None, scope: dict, n: int,
                 wr: float, exp_r: float | None, net: float,
                 samples: list[Trade]) -> str:
    canon = _STRATEGY_CANON.get(strategy or "", "")
    hint = _PATTERN_HINT.get(code, "")
    lines = [
        f"ПАТТЕРН (тема №1 периода): {code}",
        f"Подсказка по паттерну: {hint}" if hint else "",
        f"Страта (playbook): {strategy or '—'}",
        f"Канон страты (research-based, НЕ баг): {canon}" if canon else "",
        f"Срез: {scope}",
        f"Агрегаты: сделок={n}, WR={wr:.0%}, "
        f"EXP(avgR)={exp_r:.2f}" if exp_r is not None else
        f"Агрегаты: сделок={n}, WR={wr:.0%}, EXP=n/a",
        f"net P&L среза = ${net:.2f}",
        "",
        "Репрезентативные сделки (score / reasons / close_reason / mode / netR):",
    ]
    for t in samples[:5]:
        r = t.r_multiple
        lines.append(
            f"  #{t.id} {t.symbol} {t.side} score={t.score} "
            f"reasons=[{','.join(t.reasons)}] close={t.close_reason} "
            f"mode={t.mode} net=${(t.pnl_usd or 0.0):.2f} "
            f"R={r:.2f}" if r is not None else
            f"  #{t.id} {t.symbol} {t.side} score={t.score} "
            f"reasons=[{','.join(t.reasons)}] close={t.close_reason} "
            f"mode={t.mode} net=${(t.pnl_usd or 0.0):.2f}")
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


def run_five_why(client: DeepSeekClient, *, code: str, strategy: str | None,
                 scope: dict, n: int, wr: float, exp_r: float | None,
                 net: float, samples: list[Trade]) -> FiveWhyResult:
    prompt = build_prompt(code=code, strategy=strategy, scope=scope, n=n, wr=wr,
                          exp_r=exp_r, net=net, samples=samples)
    resp = client.ask(_SYSTEM, prompt)
    if resp.error:
        return FiveWhyResult(chain=[], hypothesis="", raw="", error=resp.error)
    chain, hyp = parse_response(resp.text)
    return FiveWhyResult(chain=chain, hypothesis=hyp, raw=resp.text)
