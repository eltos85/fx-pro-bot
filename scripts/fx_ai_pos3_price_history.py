"""Динамика цены BRENT за время жизни позиции id=3 (15:20-16:00 UTC, 2026-05-13).

Тянет M1 свечи от cTrader, считает плавающий PnL по нашей формуле и
показывает максимум/минимум по сделке.
"""
from __future__ import annotations
import logging
import sys
import time

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

from fx_ai_trader.config.settings import AiFxTraderSettings
from fx_ai_trader.trading.client_adapter import CTraderFxAdapter
from fx_ai_trader.trading.executor import _calc_pnl_usd

ENTRY = 105.053  # broker fill price (deal entryPrice)
SL = 104.7
TP = 106.3
VOLUME_LOTS = 0.01
SYMBOL = "BZ=F"

# Окно: 2026-05-13 15:00 UTC → 16:30 UTC
FROM_UTC_MS = int(time.mktime((2026, 5, 13, 15, 0, 0, 0, 0, 0)) * 1000) - time.timezone * 1000
TO_UTC_MS = int(time.mktime((2026, 5, 13, 16, 30, 0, 0, 0, 0)) * 1000) - time.timezone * 1000


def main() -> int:
    s = AiFxTraderSettings()
    adapter = CTraderFxAdapter(s)
    print(f"=== Connecting cTrader (account {s.ctrader_account_id}) ===")
    adapter.start(timeout=30.0)
    if not adapter.is_ready:
        print("Adapter not ready")
        return 1

    info = adapter.get_symbol_info(SYMBOL)
    if not info:
        print(f"Symbol {SYMBOL} not found")
        return 1

    client = adapter._client  # noqa: SLF001
    from_ms = FROM_UTC_MS
    to_ms = TO_UTC_MS
    raw = client.get_trendbars(
        symbol_id=info.symbol_id, period_minutes=1,
        from_ts_ms=from_ms, to_ts_ms=to_ms,
    )
    SCALE = 100_000
    bars = []
    for tb in raw:
        low = tb.low
        bars.append({
            "ts": int(tb.utcTimestampInMinutes * 60),
            "open": (low + tb.deltaOpen) / SCALE,
            "high": (low + tb.deltaHigh) / SCALE,
            "low": low / SCALE,
            "close": (low + tb.deltaClose) / SCALE,
        })
    bars.sort(key=lambda b: b["ts"])
    print(f"Total M1 bars in window: {len(bars)}")
    print(f"Entry (broker fill): ${ENTRY}  SL: ${SL}  TP: ${TP}  Volume: {VOLUME_LOTS} lot")
    print()

    R = abs(ENTRY - SL) * 100 * VOLUME_LOTS * 10  # R в USD: 0.353 × 100 = 35.3 pips × $0.1 = $3.53
    print(f"R (risk per trade): ${R:.2f}")
    print()

    max_high = None
    min_low = None
    max_high_ts = None
    min_low_ts = None

    print(f"{'Time UTC':10s}  {'O':>9s} {'H':>9s} {'L':>9s} {'C':>9s}  "
          f"{'closePnL':>10s}  {'maxFloatPnL':>13s}  {'minFloatPnL':>13s}")
    for b in bars:
        from datetime import datetime, timezone
        t = datetime.fromtimestamp(b["ts"], tz=timezone.utc).strftime("%H:%M:%S")
        close_pnl = _calc_pnl_usd(
            side="BUY", entry=ENTRY, exit_price=b["close"],
            volume_lots=VOLUME_LOTS, symbol=SYMBOL,
        )
        max_pnl_in_bar = _calc_pnl_usd(
            side="BUY", entry=ENTRY, exit_price=b["high"],
            volume_lots=VOLUME_LOTS, symbol=SYMBOL,
        )
        min_pnl_in_bar = _calc_pnl_usd(
            side="BUY", entry=ENTRY, exit_price=b["low"],
            volume_lots=VOLUME_LOTS, symbol=SYMBOL,
        )
        if max_high is None or b["high"] > max_high:
            max_high = b["high"]
            max_high_ts = t
        if min_low is None or b["low"] < min_low:
            min_low = b["low"]
            min_low_ts = t

        print(f"{t}  {b['open']:>9.3f} {b['high']:>9.3f} {b['low']:>9.3f} {b['close']:>9.3f}  "
              f"${close_pnl:>+8.2f}  ${max_pnl_in_bar:>+11.2f}  ${min_pnl_in_bar:>+11.2f}")

    print()
    print("=" * 72)
    print(f"MAX HIGH:  ${max_high:.3f} @ {max_high_ts}  "
          f"→ peak unrealized PnL = ${(max_high - ENTRY) * 100 * VOLUME_LOTS * 10:+.2f}  "
          f"({(max_high - ENTRY) * 100 * VOLUME_LOTS * 10 / R:+.2f}R)")
    print(f"MIN LOW:   ${min_low:.3f} @ {min_low_ts}  "
          f"→ min unrealized PnL = ${(min_low - ENTRY) * 100 * VOLUME_LOTS * 10:+.2f}  "
          f"({(min_low - ENTRY) * 100 * VOLUME_LOTS * 10 / R:+.2f}R)")
    print(f"FINAL SL hit at ${SL} → realized PnL = $-3.32 (broker gross)")
    adapter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
