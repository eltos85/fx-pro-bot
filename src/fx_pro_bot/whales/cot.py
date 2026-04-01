"""COT (Commitments of Traders) — позиции крупных спекулянтов по данным CFTC.

CFTC публикует отчёт еженедельно (пятница, данные за вторник).
Non-Commercial = хедж-фонды, банки, крупные спекулянты — «киты».
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from fx_pro_bot.analysis.signals import TrendDirection

log = logging.getLogger(__name__)

FUTURES_TO_SYMBOL: dict[str, str] = {
    "EURO FX": "EURUSD=X",
    "BRITISH POUND": "GBPUSD=X",
    "JAPANESE YEN": "USDJPY=X",
    "AUSTRALIAN DOLLAR": "AUDUSD=X",
    "CANADIAN DOLLAR": "USDCAD=X",
    "GOLD": "GC=F",
    "SILVER": "SI=F",
    "CRUDE OIL, LIGHT SWEET": "CL=F",
    "BRENT LAST DAY": "BZ=F",
}

INVERTED_PAIRS = frozenset({"USDJPY=X", "USDCAD=X"})

_COL_CANDIDATES: dict[str, list[str]] = {
    "name": [
        "Market and Exchange Names",
        "Market_and_Exchange_Names",
    ],
    "date": [
        "As of Date in Form YYMMDD",
        "As_of_Date_In_Form_YYMMDD",
        "Report_Date_as_YYYY-MM-DD",
    ],
    "nc_long": [
        "Noncommercial Positions-Long (All)",
        "NonComm_Positions_Long_All",
        "Noncommercial Long",
    ],
    "nc_short": [
        "Noncommercial Positions-Short (All)",
        "NonComm_Positions_Short_All",
        "Noncommercial Short",
    ],
    "chg_long": [
        "Change in Noncommercial-Long (All)",
        "Change_in_NonComm_Long_All",
    ],
    "chg_short": [
        "Change in Noncommercial-Short (All)",
        "Change_in_NonComm_Short_All",
    ],
}


@dataclass(frozen=True, slots=True)
class CotSignal:
    symbol: str
    direction: TrendDirection
    net_position: int
    net_change: int
    long_pct: float
    report_date: str


def fetch_cot_signals() -> list[CotSignal]:
    """Загрузить последний COT-отчёт и сгенерировать сигналы."""
    try:
        import cot_reports as cot  # type: ignore[import-untyped]
    except ImportError:
        log.warning("cot_reports не установлен — COT-сигналы недоступны")
        return []

    try:
        df = cot.cot_year(datetime.now().year, cot_report_type="legacy_fut")
    except Exception:
        log.exception("Не удалось загрузить COT-отчёт")
        return []

    if df is None or df.empty:
        log.warning("COT DataFrame пуст")
        return []

    df.columns = [c.strip() for c in df.columns]

    cols = {key: _find_col(df.columns.tolist(), cands) for key, cands in _COL_CANDIDATES.items()}

    if not all(cols[k] for k in ("name", "date", "nc_long", "nc_short")):
        log.warning("Не найдены нужные колонки COT: %s", {k: v for k, v in cols.items()})
        return []

    latest_date = df[cols["date"]].max()
    latest = df[df[cols["date"]] == latest_date]

    signals: list[CotSignal] = []
    for _, row in latest.iterrows():
        sig = _row_to_signal(row, cols, str(latest_date))
        if sig is not None:
            signals.append(sig)

    log.info("COT: %d сигналов (отчёт %s)", len(signals), str(latest_date)[:10])
    return signals


def _row_to_signal(
    row: object, cols: dict[str, str | None], report_date: str
) -> CotSignal | None:
    market_name = str(getattr(row, "get", lambda k: row[k])(cols["name"])).strip().upper()  # type: ignore[index]

    matched_symbol: str | None = None
    for futures_name, symbol in FUTURES_TO_SYMBOL.items():
        if futures_name.upper() in market_name:
            matched_symbol = symbol
            break
    if matched_symbol is None:
        return None

    try:
        nc_long = int(row[cols["nc_long"]])  # type: ignore[index]
        nc_short = int(row[cols["nc_short"]])  # type: ignore[index]
    except (ValueError, TypeError):
        return None

    net = nc_long - nc_short
    total = nc_long + nc_short
    long_pct = (nc_long / total * 100) if total > 0 else 50.0

    net_change = 0
    if cols.get("chg_long") and cols.get("chg_short"):
        try:
            net_change = int(row[cols["chg_long"]]) - int(row[cols["chg_short"]])  # type: ignore[index]
        except (ValueError, TypeError):
            pass

    if net > 0 and long_pct > 55:
        direction = TrendDirection.LONG
    elif net < 0 and long_pct < 45:
        direction = TrendDirection.SHORT
    else:
        direction = TrendDirection.FLAT

    if matched_symbol in INVERTED_PAIRS and direction != TrendDirection.FLAT:
        direction = (
            TrendDirection.SHORT if direction == TrendDirection.LONG else TrendDirection.LONG
        )

    return CotSignal(
        symbol=matched_symbol,
        direction=direction,
        net_position=net,
        net_change=net_change,
        long_pct=round(long_pct, 1),
        report_date=report_date,
    )


def _find_col(columns: list[str], candidates: list[str]) -> str | None:
    norm = {c.strip().lower().replace(" ", "_").replace("-", "_"): c for c in columns}
    for cand in candidates:
        if cand in columns:
            return cand
        key = cand.strip().lower().replace(" ", "_").replace("-", "_")
        if key in norm:
            return norm[key]
    return None
