"""C-05 step 2: где теряются стены density_bounce — wall_mult или near_round?

Первый замер (scalp_density_universe_audit.py) показал: стены ≥5×base ЕСТЬ у
многих монет (BTC concentr 51×, ETH 10×, NEAR 5.8×), но wall_rate≈0 — их режет
near_round(0.3%). Этот скрипт ИЗОЛИРУЕТ near_round-аттрицию: считает «сырые»
стены (size ≥ wall_mult×base) и какая доля из них проходит near_round при
0.3% / 0.5% / 1.0%, + распределение дистанции до ближайшего круглого уровня.

Метод (РОВНО логика бота, no-data-fitting): топ-25 уровней (ob_levels), наш
detect_wall/_baseline_avg/near_round из strategies.py. Шаг круглости в диагностике
дублирует формулу near_round (step=10^(floor(log10 price)−1)) и сверяется с
импортированным near_round при 0.3% (контроль дрейфа).

ЭТО ДИАГНОСТИКА. Любая правка density_round_frac/шага — только с research по
кластеризации лимиток на круглых уровнях (Osler 2003; Данилов) + одобрением.

Запуск:  PYTHONPATH=src python3 scripts/scalp_density_nearround_audit.py
Артефакт: data/scalp_density_nearround_audit.txt
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time
import urllib.request

from scalp_bot.analysis.strategies import _baseline_avg, near_round
from scalp_bot.data.universe import filter_tickers, score_ticker

MIN_TURNOVER = 100_000_000.0
MIN_RANGE_PCT = 6.0
MAX_RANGE_PCT = 20.0
MAX_SPREAD_BPS = 5.0
WALL_MULT = 5.0
ROUND_FRAC = 0.003
OB_LEVELS = 25
OB_FETCH_LIMIT = 50
RELAX_FRACS = [0.003, 0.005, 0.01]  # near_round при текущем и расслабленных порогах

MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT",
          "ADAUSDT", "LTCUSDT", "AVAXUSDT", "LINKUSDT"]
K_SNAPSHOTS = 10
SNAPSHOT_GAP_SEC = 8.0
MAX_CANDIDATES = 60
BASE = "https://api.bybit.com"


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_orderbook(symbol: str) -> tuple[list, list] | None:
    try:
        d = _get(f"{BASE}/v5/market/orderbook?category=linear&symbol={symbol}"
                 f"&limit={OB_FETCH_LIMIT}")
        res = d.get("result") or {}
        b = [(float(p), float(s)) for p, s in res.get("b", [])]
        a = [(float(p), float(s)) for p, s in res.get("a", [])]
        return b[:OB_LEVELS], a[:OB_LEVELS]
    except Exception as e:  # noqa: BLE001
        print(f"  ! orderbook {symbol}: {e}", file=sys.stderr)
        return None


def round_step(price: float) -> float:
    """Дубль шага из strategies.near_round (контроль дрейфа сверяется ниже)."""
    if price <= 0:
        return 0.0
    return 10.0 ** (math.floor(math.log10(price)) - 1)


def dist_to_round_frac(price: float) -> float:
    step = round_step(price)
    if step <= 0:
        return 1.0
    nearest = round(price / step) * step
    return abs(price - nearest) / price


def raw_walls(levels: list) -> list[tuple[float, float]]:
    """Все уровни-«сырые стены» (size ≥ WALL_MULT × baseline) на стороне книги."""
    if len(levels) < 5:
        return []
    base = _baseline_avg([sz for _, sz in levels])
    if base <= 0:
        return []
    return [(p, s / base) for p, s in levels if s >= WALL_MULT * base]


def main() -> None:
    print("Тяну tickers…", file=sys.stderr)
    tickers = _get(f"{BASE}/v5/market/tickers?category=linear")["result"]["list"]
    by_sym = {t["symbol"]: t for t in tickers}

    vol_rows = filter_tickers(
        tickers, min_turnover=MIN_TURNOVER, min_range_pct=MIN_RANGE_PCT,
        max_range_pct=MAX_RANGE_PCT, max_spread_bps=MAX_SPREAD_BPS)
    vol_syms = {m["symbol"] for m in vol_rows}
    usdt = [t for t in tickers if t["symbol"].endswith("USDT")
            and not t.get("curPreListingPhase")]
    top_syms = {t["symbol"] for t in sorted(
        usdt, key=lambda t: float(t.get("turnover24h") or 0), reverse=True)[:25]}
    majors = [s for s in MAJORS if s in by_sym]
    candidates = list(dict.fromkeys(list(vol_syms) + majors + list(top_syms)))[
        :MAX_CANDIDATES]
    print(f"Кандидатов: {len(candidates)}", file=sys.stderr)

    # сбор сырых стен по всем снимкам
    n_raw = 0
    pass_frac = {f: 0 for f in RELAX_FRACS}
    dists: list[float] = []
    drift_mismatch = 0
    examples: list[str] = []
    # пер-группно: сколько снимков с ≥1 near_round(0.3%)-стеной
    grp_snap = {"vol": [0, 0], "major_excl": [0, 0]}  # [hits, total]

    for r in range(K_SNAPSHOTS):
        print(f"раунд {r + 1}/{K_SNAPSHOTS}…", file=sys.stderr)
        for s in candidates:
            ob = fetch_orderbook(s)
            if ob is None:
                continue
            is_vol = s in vol_syms
            is_major_excl = (s in majors) and not is_vol
            snap_has_nr = False
            for levels in ob:
                for price, ratio in raw_walls(levels):
                    n_raw += 1
                    df = dist_to_round_frac(price)
                    dists.append(df)
                    nr03 = near_round(price, ROUND_FRAC)
                    if nr03 != (df <= ROUND_FRAC):
                        drift_mismatch += 1
                    for f in RELAX_FRACS:
                        if df <= f:
                            pass_frac[f] += 1
                    if nr03:
                        snap_has_nr = True
                    if len(examples) < 15 and ratio >= 8:
                        examples.append(
                            f"  {s:<12} px={price:<12.6f} ratio={ratio:>5.1f}× "
                            f"step={round_step(price):<10.6g} dist_round={df * 100:>5.2f}% "
                            f"{'NR✓' if nr03 else 'NR✗'}")
            if is_vol:
                grp_snap["vol"][1] += 1
                grp_snap["vol"][0] += int(snap_has_nr)
            elif is_major_excl:
                grp_snap["major_excl"][1] += 1
                grp_snap["major_excl"][0] += int(snap_has_nr)
            time.sleep(0.05)
        if r < K_SNAPSHOTS - 1:
            time.sleep(SNAPSHOT_GAP_SEC)

    lines = []
    lines.append("# C-05 step 2: near_round-аттриция сырых стен (≥5×base)")
    lines.append(f"# {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} | "
                 f"K={K_SNAPSHOTS}, top-{OB_LEVELS}, wall_mult={WALL_MULT}")
    lines.append("# ДИАГНОСТИКА: правка round_frac/шага — только с research + одобрением.")
    lines.append(f"# контроль дрейфа near_round vs дубль-формулы: mismatch={drift_mismatch}")
    lines.append("")
    lines.append(f"Сырых стен (≥{WALL_MULT}×base) всего: {n_raw}")
    if n_raw:
        for f in RELAX_FRACS:
            p = pass_frac[f]
            lines.append(f"  проходят near_round ≤{f * 100:.1f}%: {p} "
                         f"({p / n_raw * 100:.1f}%)")
        ds = sorted(dists)
        def pct(q: float) -> float:
            return ds[min(len(ds) - 1, int(q * len(ds)))]
        lines.append(f"  дистанция стены до круглого уровня: median="
                     f"{statistics.median(ds) * 100:.2f}% p25={pct(0.25) * 100:.2f}% "
                     f"p75={pct(0.75) * 100:.2f}%")
    lines.append("")
    lines.append("Снимков с ≥1 near_round(0.3%)-стеной (доля):")
    for g, (h, tot) in grp_snap.items():
        if tot:
            lines.append(f"  {g}: {h}/{tot} = {h / tot * 100:.1f}%")
    lines.append("")
    lines.append("Примеры крупных сырых стен (ratio≥8×):")
    lines.extend(examples or ["  (нет)"])
    lines.append("")
    lines.append("ЧТЕНИЕ: если сырых стен много, а near_round≤0.3% проходит малая "
                 "доля И median-дистанция ≫0.3% — бутылочное горло именно near_round/"
                 "шаг круглости, не подбор монет. Расслабление порога/шага — только "
                 "по research (Osler 2003 кластеризация ордеров на круглых; Данилов).")

    out = "\n".join(lines) + "\n"
    path = "data/scalp_density_nearround_audit.txt"
    with open(path, "w") as f:
        f.write(out)
    print(out)
    print(f"\n→ артефакт: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
