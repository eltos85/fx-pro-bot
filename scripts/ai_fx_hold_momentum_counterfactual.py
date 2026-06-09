#!/usr/bin/env python3
"""Counterfactual: окупается ли momentum/breakout-вход в текущем режиме?

Контекст (вопрос пользователя 2026-06-09): fx_ai_trader ставит HOLD в ~98%
циклов, объясняя это «down-break is momentum, not pullback» / «first-spike
chase» — то есть СОЗНАТЕЛЬНО отказывается от пробойных входов, требуя чистый
pullback + confluence. Рынок при этом в high-vol momentum/breakdown режиме
(золото −8.6%/мес, нефть ±, Ормуз). Вопрос: сколько R бот оставляет на столе,
отказываясь от моментум-входов?

Метод (без подгонки, источник цен — снапшоты из самих decisions):
1. Из decisions.prompt_user парсим по каждому символу на каждый decision:
   price, 24h range low/high, ATR14(1H). Это ровно то, что видел бот.
2. Breakout-сигнал (canonical Donchian-style, согласован с собственным
   EntryBreakoutSensor бота: lookback≈24×1H, buffer 0.05 ATR):
     long  если price >= hi24 - buf*ATR
     short если price <= lo24 + buf*ATR
   Считаем ВХОД только на переходе в breakout-состояние (state machine),
   чтобы один пробой не считался 50 раз.
3. Forward-результат по реконструированному ряду цен:
   (a) fixed-horizon R: r = (P[t+H]-P)/ATR (для short со знаком), H=4h и 12h;
   (b) bracket first-touch: TP=2*ATR, SL=1*ATR, дискретный путь, макс 24h
       (консервативно — между точками возможны пропуски intrabar-касаний).
4. Агрегаты по символу и суммарно: mean R, % положительных, netR.

Запуск (на VPS, БД 125MB — гонять локально к ней):
  cat scripts/ai_fx_hold_momentum_counterfactual.py | \
    ssh root@VPS "python3 - /root/fx-pro-bot/data/fx_ai_trader.sqlite 2026-05-29"

Это НЕ меняет торговлю — чистый аудит (no-data-fitting.mdc: артефакт для
обоснования возможной regime-aware правки промпта ДО её внесения).
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime
from statistics import mean

PRICE_RE = re.compile(r"\[([A-Z0-9=]+)\] price=\$([0-9.]+)")
RANGE_RE = re.compile(r"24h range: low=\$([0-9.]+) high=\$([0-9.]+)")
ATR_RE = re.compile(r"ATR14=([0-9.]+)")

BUF_ATR = 0.05          # совпадает с entry_breakout_buffer_atr бота
TP_ATR = 2.0
SL_ATR = 1.0
HORIZONS_H = (4.0, 12.0)
HORIZON_TOL_H = 0.5     # допуск поиска снапшота t+H
MAX_BRACKET_H = 24.0


def parse_market_data(prompt: str) -> dict[str, dict]:
    """Из блока MARKET DATA вернуть {sym: {price, hi, lo, atr}}.

    Парсим ТОЛЬКО после '=== MARKET DATA ===' — раньше встречаются [SYM]
    в COT/macro блоках (без 'price='), их игнорируем по price-anchor.
    """
    idx = prompt.find("=== MARKET DATA ===")
    if idx == -1:
        return {}
    block = prompt[idx:]
    # граница — следующий крупный раздел
    end = block.find("=== OPEN POSITIONS ===")
    if end != -1:
        block = block[:end]

    out: dict[str, dict] = {}
    # split на под-блоки по строкам '[SYM] price='
    parts = re.split(r"(?=\[[A-Z0-9=]+\] price=\$)", block)
    for part in parts:
        m = PRICE_RE.search(part)
        if not m:
            continue
        sym = m.group(1)
        price = float(m.group(2))
        rng = RANGE_RE.search(part)
        atr = ATR_RE.search(part)  # первый ATR14 = 1H (4H идёт ниже)
        if not rng or not atr:
            continue
        lo = float(rng.group(1))
        hi = float(rng.group(2))
        a = float(atr.group(1))
        if a <= 0 or price <= 0:
            continue
        out[sym] = {"price": price, "hi": hi, "lo": lo, "atr": a}
    return out


def load_series(db: str, cutoff: str) -> dict[str, list]:
    """{sym: [(ts_epoch, price, hi, lo, atr), ...]} отсортировано по ts."""
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ts, prompt_user FROM decisions WHERE ts>=? ORDER BY ts",
        (cutoff,),
    ).fetchall()
    conn.close()
    series: dict[str, list] = {}
    for ts, prompt in rows:
        if not prompt:
            continue
        try:
            t = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            continue
        md = parse_market_data(prompt)
        for sym, d in md.items():
            series.setdefault(sym, []).append(
                (t, d["price"], d["hi"], d["lo"], d["atr"])
            )
    for sym in series:
        series[sym].sort(key=lambda x: x[0])
    return series


def detect_entries(pts: list) -> list[tuple]:
    """State-machine: вход на ПЕРЕХОДЕ в breakout. Возврат (idx, side)."""
    entries = []
    state = None  # None / 'long' / 'short'
    for i, (_, price, hi, lo, atr) in enumerate(pts):
        long_sig = price >= hi - BUF_ATR * atr
        short_sig = price <= lo + BUF_ATR * atr
        new = "long" if long_sig else ("short" if short_sig else None)
        if new is not None and new != state:
            entries.append((i, new))
        state = new
    return entries


def forward_r_fixed(pts: list, i: int, side: str, horizon_h: float):
    t0, p0, _, _, atr = pts[i]
    target = t0 + horizon_h * 3600
    best = None
    for j in range(i + 1, len(pts)):
        tj = pts[j][0]
        if abs(tj - target) <= HORIZON_TOL_H * 3600:
            if best is None or abs(tj - target) < abs(pts[best][0] - target):
                best = j
        if tj > target + HORIZON_TOL_H * 3600:
            break
    if best is None:
        return None
    pj = pts[best][1]
    move = (pj - p0) if side == "long" else (p0 - pj)
    return move / atr


def bracket_r(pts: list, i: int, side: str):
    """Дискретный first-touch TP=2ATR/SL=1ATR, макс 24h. None если не закрылось."""
    t0, p0, _, _, atr = pts[i]
    if side == "long":
        tp, sl = p0 + TP_ATR * atr, p0 - SL_ATR * atr
    else:
        tp, sl = p0 - TP_ATR * atr, p0 + SL_ATR * atr
    for j in range(i + 1, len(pts)):
        tj, pj = pts[j][0], pts[j][1]
        if tj - t0 > MAX_BRACKET_H * 3600:
            return None
        if side == "long":
            if pj <= sl:
                return -SL_ATR
            if pj >= tp:
                return TP_ATR
        else:
            if pj >= sl:
                return -SL_ATR
            if pj <= tp:
                return TP_ATR
    return None


def summarize(label: str, vals: list[float]) -> str:
    if not vals:
        return f"  {label}: n=0"
    pos = sum(1 for v in vals if v > 0)
    return (
        f"  {label}: n={len(vals)} netR={sum(vals):+.1f} "
        f"meanR={mean(vals):+.2f} win%={pos/len(vals)*100:.0f}"
    )


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else "fx_ai_trader.sqlite"
    cutoff = sys.argv[2] if len(sys.argv) > 2 else "2026-05-29"
    series = load_series(db, cutoff)

    print(f"=== Momentum/breakout counterfactual (cutoff {cutoff}) ===")
    print(f"buf={BUF_ATR}ATR  TP={TP_ATR}ATR SL={SL_ATR}ATR  horizons={HORIZONS_H}h\n")

    agg_fixed: dict[float, list] = {h: [] for h in HORIZONS_H}
    agg_bracket: list = []
    for sym in sorted(series):
        pts = series[sym]
        entries = detect_entries(pts)
        print(f"[{sym}] snapshots={len(pts)} breakout-entries={len(entries)}")
        for h in HORIZONS_H:
            vals = [
                r for (i, side) in entries
                if (r := forward_r_fixed(pts, i, side, h)) is not None
            ]
            agg_fixed[h].extend(vals)
            print(summarize(f"fixed +{h:.0f}h", vals))
        bvals = [
            r for (i, side) in entries
            if (r := bracket_r(pts, i, side)) is not None
        ]
        agg_bracket.extend(bvals)
        print(summarize("bracket 2:1", bvals))
        # split по направлению для bracket
        for s in ("long", "short"):
            sv = [
                r for (i, side) in entries if side == s
                and (r := bracket_r(pts, i, side)) is not None
            ]
            print(summarize(f"  bracket {s}", sv))
        print()

    print("=== TOTAL (all symbols) ===")
    for h in HORIZONS_H:
        print(summarize(f"fixed +{h:.0f}h", agg_fixed[h]))
    print(summarize("bracket 2:1", agg_bracket))


if __name__ == "__main__":
    main()
