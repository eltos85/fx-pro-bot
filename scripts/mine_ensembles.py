"""Ансамбль-анализ: моменты где 2+ страты выдали сигнал на одном символе
в одном направлении внутри окна ±15 минут от entry_ts.

Для каждой сделки `T` считаем `peer_count` = сколько ДРУГИХ сделок других страт
имеют: тот же symbol, то же direction, |entry_ts - T.entry_ts| <= WINDOW_MIN
минут.

Группировка:
  - solo (peer_count == 0)
  - duo (peer_count == 1)  — одна другая страта согласна
  - trio+ (peer_count >= 2) — две или более других согласны

Гипотеза: чем больше страт согласны одновременно, тем выше качество сигнала.
Если в `trio+` WR/PF заметно выше base rate — это подтверждение, что ансамбль
несёт edge.

Дополнительно:
  - `ensemble × session` — где ансамбль работает лучше всего
  - `ensemble × direction` — long vs short для ансамблей
  - `ensemble × symbol`

Вход: data/backtest_trades_enriched.csv
Выход: stdout + data/mine_ensembles_out.txt (через tee)
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

IN = Path("data/backtest_trades_enriched.csv")
WINDOW_MIN = 15
MIN_N = 100
MIN_WR = 0.55
MIN_PF = 1.3


@dataclass
class Trade:
    strategy: str
    symbol: str
    direction: str
    entry_ts: datetime
    net_pct: float
    session: str
    peers: int = 0
    peer_strats: tuple[str, ...] = ()


def _load() -> list[Trade]:
    out: list[Trade] = []
    with IN.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            out.append(Trade(
                strategy=row["strategy"],
                symbol=row["symbol"],
                direction=row["direction"],
                entry_ts=datetime.fromisoformat(row["entry_ts"]),
                net_pct=float(row["net_pct"]),
                session=row["session"],
            ))
    return out


def _metrics(trs: list[Trade]) -> dict:
    n = len(trs)
    if n == 0:
        return {"n": 0}
    wins = [t for t in trs if t.net_pct > 0]
    losses = [t for t in trs if t.net_pct <= 0]
    wr = len(wins) / n
    exp_pct = sum(t.net_pct for t in trs) / n * 100
    sum_wins = sum(t.net_pct for t in wins)
    sum_losses = abs(sum(t.net_pct for t in losses))
    pf = (sum_wins / sum_losses) if sum_losses > 0 else float("inf")
    return {
        "n": n,
        "wr": wr,
        "exp_pct": exp_pct,
        "pf": pf,
        "sum_pct": sum(t.net_pct for t in trs) * 100,
    }


def _compute_peers(trs: list[Trade]) -> None:
    """Для каждой T считаем сколько ДРУГИХ сделок других страт согласны с ней
    (same symbol, same direction, |Δt| <= WINDOW_MIN, different strategy)."""
    window = timedelta(minutes=WINDOW_MIN)
    # Группировка по (symbol, direction) для ускорения: пиры ищем только внутри.
    grp: dict[tuple[str, str], list[tuple[datetime, int, str]]] = defaultdict(list)
    for i, t in enumerate(trs):
        grp[(t.symbol, t.direction)].append((t.entry_ts, i, t.strategy))
    # Сортируем по ts чтобы использовать двусторонний указатель (но у нас данные
    # уже почти отсортированы по времени глобально; отсортируем на всякий).
    for key in grp:
        grp[key].sort()

    for (sym, dirn), items in grp.items():
        n_local = len(items)
        for a in range(n_local):
            ts_a, idx_a, strat_a = items[a]
            peers: list[str] = []
            # окно ±15 мин ⇒ сначала идём назад
            b = a - 1
            while b >= 0 and (ts_a - items[b][0]) <= window:
                if items[b][2] != strat_a:
                    peers.append(items[b][2])
                b -= 1
            # потом вперёд
            b = a + 1
            while b < n_local and (items[b][0] - ts_a) <= window:
                if items[b][2] != strat_a:
                    peers.append(items[b][2])
                b += 1
            trs[idx_a].peers = len(peers)
            trs[idx_a].peer_strats = tuple(sorted(set(peers)))


def _bucket_by_peer(trs: list[Trade]) -> dict[str, list[Trade]]:
    out: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        if t.peers == 0:
            key = "solo (0 peers)"
        elif t.peers == 1:
            key = "duo (1 peer)"
        elif t.peers == 2:
            key = "trio (2 peers)"
        elif t.peers >= 3:
            key = f"quartet+ (3+ peers)"
        out[key].append(t)
    return out


def _print_slices(title: str, buckets: dict[str, list[Trade]], top_n: int = 25) -> None:
    rows = []
    for key, trs in buckets.items():
        m = _metrics(trs)
        if m["n"] < MIN_N:
            continue
        rows.append((key, m))
    rows.sort(key=lambda x: x[1]["exp_pct"], reverse=True)
    if not rows:
        print(f"\n{title}\n  (нет срезов с n >= {MIN_N})")
        return
    print(f"\n{title}")
    print(f"  {'КЛЮЧ':<50} {'N':>5} {'WR%':>6} {'EXP%':>7} {'PF':>5} {'Σ%':>8}")
    for key, m in rows[:top_n]:
        mark = " ✓" if (m["wr"] >= MIN_WR and m["pf"] >= MIN_PF and m["exp_pct"] > 0) else ""
        pf_s = f"{m['pf']:.2f}" if m["pf"] != float("inf") else " inf"
        print(f"  {key:<50} {m['n']:>5} {m['wr']*100:>5.1f} {m['exp_pct']:>+7.3f} {pf_s:>5} {m['sum_pct']:>+8.1f}{mark}")


def main() -> None:
    trs = _load()
    print(f"Загружено {len(trs)} сделок")
    print(f"Поиск ансамблей в окне ±{WINDOW_MIN} мин…")
    _compute_peers(trs)

    # Распределение peer_count
    dist: dict[int, int] = defaultdict(int)
    for t in trs:
        dist[t.peers] += 1
    print("\nРаспределение peer_count:")
    for k in sorted(dist.keys()):
        print(f"  peers={k}: {dist[k]} сделок")

    # 1) Основная разбивка по peer-бакетам
    print("\n=" * 50)
    print("### 1. ПО КОЛИЧЕСТВУ СОГЛАСНЫХ СТРАТ (peers)")
    _print_slices("Peer count:", _bucket_by_peer(trs))

    # 2) Ансамбль × сессия
    print("\n### 2. ПО ПАРЕ (peers, session)")
    def _peer_sess(t: Trade) -> str:
        pc = "solo" if t.peers == 0 else ("duo" if t.peers == 1 else f"trio+({t.peers})")
        return f"{pc}/{t.session}"
    _print_slices("Peer×Session:", {f"{k}": v for k, v in _group(trs, _peer_sess).items()})

    # 3) Ансамбль × направление
    print("\n### 3. ПО ПАРЕ (peers, direction)")
    def _peer_dir(t: Trade) -> str:
        pc = "solo" if t.peers == 0 else ("duo" if t.peers == 1 else "trio+")
        return f"{pc}/{t.direction}"
    _print_slices("Peer×Direction:", _group(trs, _peer_dir))

    # 4) Ансамбль × символ (топ-30 по Σ)
    print("\n### 4. ПО ПАРЕ (peers, symbol) — только trio+")
    def _peer_sym(t: Trade) -> str:
        pc = "solo" if t.peers == 0 else ("duo" if t.peers == 1 else "trio+")
        return f"{pc}/{t.symbol}"
    _print_slices("Peer×Symbol:", _group(trs, _peer_sym), top_n=40)

    # 5) Конкретные комбинации страт (peer_strats кортеж)
    print("\n### 5. ТОП-КОМБИНАЦИИ СТРАТ (для duo/trio+)")
    def _combo(t: Trade) -> str:
        if t.peers == 0:
            return "_solo"
        sig = tuple(sorted(set((t.strategy,) + t.peer_strats)))
        return "+".join(sig)
    buckets = _group(trs, _combo)
    # убираем _solo из вывода (он большой и неинтересен)
    buckets.pop("_solo", None)
    _print_slices("Strategy combos:", buckets, top_n=25)


def _group(trs: list[Trade], key_fn) -> dict[str, list[Trade]]:
    out: dict[str, list[Trade]] = defaultdict(list)
    for t in trs:
        out[key_fn(t)].append(t)
    return out


if __name__ == "__main__":
    main()
