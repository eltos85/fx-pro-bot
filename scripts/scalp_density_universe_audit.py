"""C-05 prerequisite: замер частоты «стен» в стакане по монетам рынка.

Цель (см. STRATEGY_CONTRADICTIONS_SCALP.md C-05): density_bounce зависит от
ГЛУБИНЫ стакана (resting walls), а авто-вселенная отбирает по ВОЛАТИЛЬНОСТИ
(range 6–20% + RVOL + turnover + spread) и range-floor 6% режет глубокие мейджоры.
Гипотеза: квалифицирующие стены чаще появляются на мейджорах (глубокие книги),
чем на волатильных альтах текущей вселенной.

Метод (НЕ переинтерпретация — РОВНО логика бота, no-data-fitting.mdc):
- кандидаты: текущая vol-вселенная (filter_tickers с боевыми порогами) ∪ мейджоры
  ∪ топ-оборот рынка;
- для каждой монеты K снимков стакана через публичный REST get_orderbook (limit=50),
  берём ТОП-25 уровней каждой стороны (бот видит ob_levels=25), и прогоняем НАШ
  detect_wall(wall_mult=5, baseline=_baseline_avg) + near_round(0.003) — стена
  квалифицируется ровно как у density_bounce;
- wall_rate = доля снимков с квалифицирующей стеной (bid или ask).

Ограничение: один прогон — точечный во времени (стены меняются интрадей). Для
вердикта C-05 прогнать в разные сессии. Это РАЗВЕДКА, не основание для правки порогов.

Запуск (локально, из корня репо):
    PYTHONPATH=src python3 scripts/scalp_density_universe_audit.py
Артефакт: data/scalp_density_universe_audit.txt
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request

from scalp_bot.analysis.strategies import _baseline_avg, detect_wall, near_round
from scalp_bot.data.universe import filter_tickers, score_ticker

# Боевые пороги (settings.py дефолты) — синхрон с продом.
MIN_TURNOVER = 100_000_000.0
MIN_RANGE_PCT = 6.0
MAX_RANGE_PCT = 20.0
MAX_SPREAD_BPS = 5.0
WALL_MULT = 5.0          # density_wall_mult
ROUND_FRAC = 0.003       # density_round_frac
OB_LEVELS = 25           # бот обрезает orderbook.50 до топ-25 (aggregates.py)
OB_FETCH_LIMIT = 50      # подписка orderbook.50

# Глубокие мейджоры — часто режутся range-floor 6% (universe.py docstring).
MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT",
          "ADAUSDT", "LTCUSDT", "AVAXUSDT", "LINKUSDT"]

K_SNAPSHOTS = 6          # снимков на монету
SNAPSHOT_GAP_SEC = 12.0  # пауза между раундами
MAX_CANDIDATES = 60      # safety-кап (rate-limit)

BASE = "https://api.bybit.com"


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_tickers() -> list[dict]:
    d = _get(f"{BASE}/v5/market/tickers?category=linear")
    return d["result"]["list"]


def fetch_orderbook(symbol: str) -> tuple[list, list] | None:
    """(bids, asks) как list[(price,size)], топ-OB_LEVELS, мирроринг бота."""
    try:
        d = _get(f"{BASE}/v5/market/orderbook?category=linear&symbol={symbol}"
                 f"&limit={OB_FETCH_LIMIT}")
        res = d.get("result") or {}
        b = [(float(p), float(s)) for p, s in res.get("b", [])]
        a = [(float(p), float(s)) for p, s in res.get("a", [])]
        # уже отсортированы биржей (b desc, a asc); обрезаем до топ-25 как бот
        return b[:OB_LEVELS], a[:OB_LEVELS]
    except Exception as e:  # noqa: BLE001
        print(f"  ! orderbook {symbol}: {e}", file=sys.stderr)
        return None


def wall_in_snapshot(bids: list, asks: list) -> tuple[bool, float]:
    """Есть ли квалифицирующая стена (как у density_bounce) в этом снимке.
    Возвращает (has_wall, max_concentration_ratio = max_size/baseline по сторонам)."""
    best_ratio = 0.0
    has = False
    for levels in (bids, asks):
        if len(levels) < 5:
            continue
        base = _baseline_avg([sz for _, sz in levels])
        if base <= 0:
            continue
        price, size = max(levels, key=lambda ps: ps[1])
        best_ratio = max(best_ratio, size / base)
        wall = detect_wall(levels, WALL_MULT, baseline=base)
        if wall is not None and near_round(wall[0], ROUND_FRAC):
            has = True
    return has, best_ratio


def main() -> None:
    print("Тяну tickers…", file=sys.stderr)
    tickers = fetch_tickers()
    by_sym = {t["symbol"]: t for t in tickers}

    vol_rows = filter_tickers(
        tickers, min_turnover=MIN_TURNOVER, min_range_pct=MIN_RANGE_PCT,
        max_range_pct=MAX_RANGE_PCT, max_spread_bps=MAX_SPREAD_BPS)
    vol_syms = {m["symbol"] for m in vol_rows}

    # топ-оборот рынка (широкий референс)
    usdt = [t for t in tickers if t["symbol"].endswith("USDT")
            and not t.get("curPreListingPhase")]
    top_turnover = sorted(
        usdt, key=lambda t: float(t.get("turnover24h") or 0), reverse=True)[:25]
    top_syms = {t["symbol"] for t in top_turnover}

    majors = [s for s in MAJORS if s in by_sym]
    candidates = list(dict.fromkeys(list(vol_syms) + majors + list(top_syms)))
    candidates = candidates[:MAX_CANDIDATES]
    print(f"Кандидатов: {len(candidates)} "
          f"(vol={len(vol_syms)}, majors={len(majors)}, top={len(top_syms)})",
          file=sys.stderr)

    # K раундов round-robin (стена должна стоять, не мелькать → несколько снимков)
    hits: dict[str, int] = {s: 0 for s in candidates}
    snaps: dict[str, int] = {s: 0 for s in candidates}
    ratios: dict[str, list[float]] = {s: [] for s in candidates}
    for r in range(K_SNAPSHOTS):
        print(f"раунд {r + 1}/{K_SNAPSHOTS}…", file=sys.stderr)
        for s in candidates:
            ob = fetch_orderbook(s)
            if ob is None:
                continue
            has, ratio = wall_in_snapshot(ob[0], ob[1])
            snaps[s] += 1
            if has:
                hits[s] += 1
            if ratio > 0:
                ratios[s].append(ratio)
            time.sleep(0.05)
        if r < K_SNAPSHOTS - 1:
            time.sleep(SNAPSHOT_GAP_SEC)

    def meta(s: str) -> dict:
        m = score_ticker(by_sym.get(s, {})) or {}
        return {"range": m.get("range_pct", 0.0),
                "turn": m.get("turnover", 0.0),
                "spread": m.get("spread_bps", 0.0)}

    rows = []
    for s in candidates:
        n = snaps[s]
        if n == 0:
            continue
        rate = hits[s] / n
        med_ratio = statistics.median(ratios[s]) if ratios[s] else 0.0
        mt = meta(s)
        rows.append({
            "sym": s, "rate": rate, "med_ratio": med_ratio,
            "range": mt["range"], "turn": mt["turn"], "spread": mt["spread"],
            "in_vol": s in vol_syms,
            "is_major": s in majors,
        })
    rows.sort(key=lambda x: (x["rate"], x["med_ratio"]), reverse=True)

    def grp_summary(name: str, sel: list[dict]) -> str:
        if not sel:
            return f"{name}: (пусто)"
        rs = [x["rate"] for x in sel]
        cs = [x["med_ratio"] for x in sel]
        return (f"{name}: n={len(sel)} | wall_rate mean={statistics.mean(rs):.2f} "
                f"median={statistics.median(rs):.2f} | concentr median="
                f"{statistics.median(cs):.1f}× | монет с rate>0: "
                f"{sum(1 for x in rs if x > 0)}")

    in_vol = [x for x in rows if x["in_vol"]]
    majors_excl = [x for x in rows if x["is_major"] and not x["in_vol"]]
    majors_all = [x for x in rows if x["is_major"]]

    lines = []
    lines.append("# C-05 замер: частота квалифицирующих стен в стакане")
    lines.append(f"# {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} | "
                 f"K={K_SNAPSHOTS} снимков, gap={SNAPSHOT_GAP_SEC}с, "
                 f"top-{OB_LEVELS} уровней, wall≥{WALL_MULT}×base & near_round")
    lines.append("# ОГРАНИЧЕНИЕ: точечный прогон во времени; для вердикта прогнать "
                 "в разные сессии. Разведка, не основание для тюнинга порогов.")
    lines.append("")
    lines.append("=== СВОДКА ПО ГРУППАМ (ответ на гипотезу C-05) ===")
    lines.append(grp_summary("vol-вселенная (текущая, sweep_fade)", in_vol))
    lines.append(grp_summary("мейджоры ИСКЛЮЧЁННЫЕ (не в vol)", majors_excl))
    lines.append(grp_summary("мейджоры все", majors_all))
    lines.append("")
    lines.append("Гипотеза C-05 подтверждается, если у исключённых мейджоров "
                 "wall_rate заметно выше, чем у vol-вселенной.")
    lines.append("")
    lines.append("=== ПО МОНЕТАМ (sorted by wall_rate) ===")
    lines.append(f"{'symbol':<14}{'wall_rate':>10}{'concentr':>10}"
                 f"{'range%':>9}{'turn$M':>9}{'spr_bps':>9}  flags")
    for x in rows:
        flags = []
        if x["in_vol"]:
            flags.append("VOL")
        if x["is_major"]:
            flags.append("MAJOR")
        lines.append(
            f"{x['sym']:<14}{x['rate']:>10.2f}{x['med_ratio']:>10.1f}"
            f"{x['range']:>9.1f}{x['turn'] / 1e6:>9.0f}{x['spread']:>9.2f}"
            f"  {','.join(flags)}")

    out = "\n".join(lines) + "\n"
    path = "data/scalp_density_universe_audit.txt"
    with open(path, "w") as f:
        f.write(out)
    print(out)
    print(f"\n→ артефакт: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
