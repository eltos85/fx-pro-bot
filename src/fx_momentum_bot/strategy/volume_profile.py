"""Volume Profile (Market/Auction) стратегия — gold-only, механическая.

─── Research basis ───
- Peter Steidlmayer (CBOT), «Steidlmayer on Markets: Trading with Market
  Profile» (2003): концепция аукциона, point of control, value area.
- James Dalton, «Mind Over Markets» (2007): value area = ~70% объёма
  (≈1σ), failed auction (rejection за краем value area) и failed
  breakout / acceptance outside value как канонические сетапы.
- Value-area 70% — каноничный параметр (1 std dev of a normal-ish
  volume distribution), НЕ подгонка (.cursor/rules/no-data-fitting.mdc).

Идея популяризована роликом Faiz SMC «The Easiest Gold Volume Profile
Trading Strategy» (5-min, окно 03:00–07:00 NY) — здесь механизирована
объективными правилами (без дискреции «BE here or here»).

Два сетапа на 5-минутках, профиль строится по сессионному окну:
1. FAILED AUCTION (fade-to-value): 5m close за VAL/VAH, затем 5m close
   обратно внутрь value area (reclaim) → вход в сторону возврата, цель —
   POC / противоположный край VA. Инвалид если POC уже задет.
2. BREAKOUT (acceptance outside): цена закрепилась (consolidation) за
   краем VA несколько баров, затем пробивает экстремум консолидации →
   вход по направлению пробоя, SL за консолидацией.

Риск: SL за хвостом свипа/консолидации, RR ≥ vp_min_rr, лимит сделок в
сторону за день — на стороне main.py. Сам модуль чистый (pure), без I/O.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True, slots=True)
class Profile:
    poc: float          # point of control (цена макс. объёма)
    vah: float          # value area high
    val: float          # value area low
    shape: str          # "P" | "B" | "D"
    session_low: float
    session_high: float
    total_volume: float


@dataclass(frozen=True, slots=True)
class VolumeProfileSignal:
    direction: str      # "long" | "short" | "flat"
    setup: str          # "failed_auction" | "breakout" | "none"
    last_close: float
    atr: float
    entry: float
    sl_price: float
    tp_price: float
    profile: Profile | None
    reason: str


def _compute_atr(df: pd.DataFrame, period: int) -> float:
    if len(df) < period + 1:
        return 0.0
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=period, min_periods=period).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else 0.0


def split_session_live(
    df: pd.DataFrame, *, tz: str, session_start: str, session_end: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Разбить 5m-бары на (session, live) для ТЕКУЩЕГО дня в tz.

    session — бары в окне [session_start, session_end) последней
    локальной даты, по которым строится профиль.
    live — бары ПОСЛЕ session_end той же даты (где ищем сетапы).
    Если профиль ещё формируется (сейчас < session_end) — live пустой.
    """
    if df.empty:
        return df, df
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        return df.iloc[0:0], df.iloc[0:0]
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
        df = df.copy()
        df.index = idx
    local = df.tz_convert(tz)
    last_date = local.index[-1].date()
    sh, sm = (int(x) for x in session_start.split(":"))
    eh, em = (int(x) for x in session_end.split(":"))
    times = local.index.time
    import datetime as _dt

    start_t = _dt.time(sh, sm)
    end_t = _dt.time(eh, em)
    same_day = local.index.date == last_date
    in_session = same_day & (times >= start_t) & (times < end_t)
    after_session = same_day & (times >= end_t)
    session = local[in_session]
    live = local[after_session]
    return session, live


def compute_profile(
    session_bars: pd.DataFrame, *, value_area_pct: float, num_bins: int
) -> Profile | None:
    """Построить volume profile из 5m-баров сессии.

    Объём каждого бара равномерно распределяется по бинам, перекрытым его
    диапазоном [Low, High]. POC = бин макс. объёма. Value area расширяется
    от POC к соседу с большим объёмом, пока не накопит value_area_pct.
    """
    if session_bars.empty or num_bins < 2:
        return None
    lo = float(session_bars["Low"].min())
    hi = float(session_bars["High"].max())
    if not (hi > lo):
        return None
    total_vol = float(session_bars["Volume"].sum())
    if total_vol <= 0:
        return None

    bin_w = (hi - lo) / num_bins
    vols = [0.0] * num_bins
    centers = [lo + (i + 0.5) * bin_w for i in range(num_bins)]

    for _, bar in session_bars.iterrows():
        b_lo = float(bar["Low"])
        b_hi = float(bar["High"])
        b_vol = float(bar["Volume"])
        if b_vol <= 0 or b_hi < b_lo:
            continue
        i_lo = max(0, int((b_lo - lo) / bin_w))
        i_hi = min(num_bins - 1, int((b_hi - lo) / bin_w))
        span = i_hi - i_lo + 1
        share = b_vol / span
        for i in range(i_lo, i_hi + 1):
            vols[i] += share

    poc_idx = max(range(num_bins), key=lambda i: vols[i])
    cum = vols[poc_idx]
    target = value_area_pct * total_vol
    low_i = high_i = poc_idx
    while cum < target and (low_i > 0 or high_i < num_bins - 1):
        up = vols[high_i + 1] if high_i + 1 < num_bins else -1.0
        down = vols[low_i - 1] if low_i - 1 >= 0 else -1.0
        if up >= down:
            high_i += 1
            cum += vols[high_i]
        else:
            low_i -= 1
            cum += vols[low_i]

    val = lo + low_i * bin_w
    vah = lo + (high_i + 1) * bin_w
    poc = centers[poc_idx]
    shape = _classify_shape(vols)
    return Profile(
        poc=poc, vah=vah, val=val, shape=shape,
        session_low=lo, session_high=hi, total_volume=total_vol,
    )


def _classify_shape(vols: list[float]) -> str:
    """P (объём вверху=тренд up), B (внизу=тренд down), D (середина=баланс)."""
    n = len(vols)
    third = max(1, n // 3)
    bottom = sum(vols[:third])
    middle = sum(vols[third: n - third])
    top = sum(vols[n - third:])
    if middle >= top and middle >= bottom:
        return "D"
    return "P" if top > bottom else "B"


def _detect_failed_auction(
    live: pd.DataFrame, profile: Profile, *, breach_lookback: int, atr: float
) -> tuple[str, float, float] | None:
    """Reclaim обратно в value area. Возврат (direction, entry, sl) или None.

    Триггер на баре пересечения: предыдущий close снаружи, последний —
    внутри. До этого в пределах breach_lookback был хотя бы один close
    за краем (настоящий свип). POC не должен быть уже задет на reclaim.
    """
    if len(live) < 2:
        return None
    closes = live["Close"].to_numpy(dtype=float)
    lows = live["Low"].to_numpy(dtype=float)
    highs = live["High"].to_numpy(dtype=float)
    last = closes[-1]
    prev = closes[-2]
    buf = 0.1 * atr if atr > 0 else 0.0
    win = min(breach_lookback, len(live))

    # long: свип ниже VAL, reclaim назад выше VAL
    if prev <= profile.val < last:
        breached = any(closes[-win:][k] < profile.val for k in range(win))
        if breached and last < profile.poc:  # ещё не дошли до POC
            sl = float(lows[-win:].min()) - buf
            if sl < last:
                return "long", last, sl
    # short: свип выше VAH, reclaim назад ниже VAH
    if prev >= profile.vah > last:
        breached = any(closes[-win:][k] > profile.vah for k in range(win))
        if breached and last > profile.poc:
            sl = float(highs[-win:].max()) + buf
            if sl > last:
                return "short", last, sl
    return None


def _detect_breakout(
    live: pd.DataFrame, profile: Profile, *, consolidation_bars: int, atr: float
) -> tuple[str, float, float] | None:
    """Acceptance outside value → пробой консолидации. (direction, entry, sl)."""
    if len(live) < consolidation_bars + 1:
        return None
    closes = live["Close"].to_numpy(dtype=float)
    lows = live["Low"].to_numpy(dtype=float)
    highs = live["High"].to_numpy(dtype=float)
    last = closes[-1]
    buf = 0.1 * atr if atr > 0 else 0.0
    cons_close = closes[-1 - consolidation_bars:-1]
    cons_high = highs[-1 - consolidation_bars:-1]
    cons_low = lows[-1 - consolidation_bars:-1]

    # up-breakout: консолидация полностью выше VAH, пробой её максимума
    if all(c > profile.vah for c in cons_close):
        cons_top = float(cons_high.max())
        if last > cons_top:
            sl = float(cons_low.min()) - buf
            if sl < last:
                return "long", last, sl
    # down-breakout: консолидация ниже VAL, пробой её минимума
    if all(c < profile.val for c in cons_close):
        cons_bot = float(cons_low.min())
        if last < cons_bot:
            sl = float(cons_high.max()) + buf
            if sl > last:
                return "short", last, sl
    return None


def _target(
    direction: str, entry: float, sl: float, profile: Profile, *, min_rr: float
) -> float | None:
    """TP: POC/противоположный край VA, но не ближе min_rr. Иначе 2*min_rr нет → None."""
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    floor_tp = min_rr * risk
    if direction == "long":
        # fade-цель: противоположный край VA / POC выше входа (Dalton).
        candidates = [p for p in (profile.poc, profile.vah) if p > entry]
        far = max(candidates) if candidates else entry + floor_tp
        tp = max(far, entry + floor_tp)  # breakout: нет VA-цели → measured 1.5R
        if (tp - entry) / risk < min_rr:
            return None
        return tp
    candidates = [p for p in (profile.poc, profile.val) if p < entry]
    far = min(candidates) if candidates else entry - floor_tp
    tp = min(far, entry - floor_tp)
    if (entry - tp) / risk < min_rr:
        return None
    return tp


def build_signal(
    candles_5m: pd.DataFrame,
    *,
    tz: str,
    session_start: str,
    session_end: str,
    value_area_pct: float,
    num_bins: int,
    atr_period: int,
    min_rr: float,
    breach_lookback: int,
    consolidation_bars: int,
) -> VolumeProfileSignal | None:
    """Главная точка входа: из 5m-баров вернуть сигнал или None (мало данных)."""
    if candles_5m is None or candles_5m.empty:
        return None
    needed = {"Open", "High", "Low", "Close", "Volume"}
    if not needed.issubset(set(candles_5m.columns)):
        return None

    session, live = split_session_live(
        candles_5m, tz=tz, session_start=session_start, session_end=session_end
    )
    profile = compute_profile(
        session, value_area_pct=value_area_pct, num_bins=num_bins
    )
    atr = _compute_atr(candles_5m, atr_period)
    last_close = float(candles_5m["Close"].iloc[-1])

    if profile is None or live.empty or atr <= 0:
        return VolumeProfileSignal(
            direction="flat", setup="none", last_close=last_close, atr=atr,
            entry=last_close, sl_price=0.0, tp_price=0.0, profile=profile,
            reason="no_profile_or_live" if profile is None else "no_setup",
        )

    fa = _detect_failed_auction(
        live, profile, breach_lookback=breach_lookback, atr=atr
    )
    detected = ("failed_auction", fa) if fa else None
    if detected is None:
        bo = _detect_breakout(
            live, profile, consolidation_bars=consolidation_bars, atr=atr
        )
        detected = ("breakout", bo) if bo else None

    if detected is None or detected[1] is None:
        return VolumeProfileSignal(
            direction="flat", setup="none", last_close=last_close, atr=atr,
            entry=last_close, sl_price=0.0, tp_price=0.0, profile=profile,
            reason=f"no_setup shape={profile.shape}",
        )

    setup, (direction, entry, sl) = detected
    tp = _target(direction, entry, sl, profile, min_rr=min_rr)
    if tp is None:
        return VolumeProfileSignal(
            direction="flat", setup="none", last_close=last_close, atr=atr,
            entry=last_close, sl_price=0.0, tp_price=0.0, profile=profile,
            reason=f"{setup}_{direction}_rr_too_low",
        )

    reason = (
        f"{setup} {direction} shape={profile.shape} "
        f"VAL={profile.val:.2f} POC={profile.poc:.2f} VAH={profile.vah:.2f} "
        f"entry={entry:.2f} sl={sl:.2f} tp={tp:.2f}"
    )
    return VolumeProfileSignal(
        direction=direction, setup=setup, last_close=last_close, atr=atr,
        entry=entry, sl_price=sl, tp_price=tp, profile=profile, reason=reason,
    )
