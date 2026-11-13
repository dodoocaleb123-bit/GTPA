"""Entry retest engine (blueprint 12).

FORMAL DISPLACEMENT DEFINITION (the blueprint leaves this open; this is my proposal, configurable):
  * The displacement is the run of consecutive M5 candles of the MSS direction ending at the MSS candle.
  * The entry zone is the full high-low range of the nearest opposite-colour M5 candle immediately
    preceding that run (searched back at most `lookback` candles). No such candle => no setup.
Entry: after the MSS candle closes, the first completed M1 candle that trades into the zone and closes
beyond its far boundary (above upper for BUY, below lower for SELL). The M1 close is the proposed entry.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .mss_engine import MSS


@dataclass
class EntryZone:
    upper: float
    lower: float
    source_time: pd.Timestamp
    displacement_start_time: pd.Timestamp

    def as_dict(self) -> dict:
        return {"upper": self.upper, "lower": self.lower, "source_time": self.source_time.isoformat(),
                "displacement_start_time": self.displacement_start_time.isoformat()}


@dataclass
class EntryResult:
    status: str     # ENTRY | WAITING | EXPIRED | INVALIDATED | CANCELLED_TP_FIRST | NO_ENTRY_ZONE
    zone: EntryZone | None = None
    entry_price: float | None = None
    entry_time: pd.Timestamp | None = None     # close time of the M1 confirmation candle
    candle_time: pd.Timestamp | None = None    # open time of the M1 confirmation candle
    reason: str = ""

    def as_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "entry_price": self.entry_price,
                "entry_time": self.entry_time.isoformat() if self.entry_time is not None else None,
                "zone": self.zone.as_dict() if self.zone else None}


def entry_zone(m5: pd.DataFrame, mss: MSS, lookback: int = 10) -> EntryZone | None:
    buy = mss.direction == "BUY"
    o, c = m5["open"].to_numpy(), m5["close"].to_numpy()
    k = mss.candle_idx - 1
    start = mss.candle_idx
    while k >= 0 and mss.candle_idx - k <= lookback:
        opposite = c[k] < o[k] if buy else c[k] > o[k]
        same = c[k] > o[k] if buy else c[k] < o[k]
        if opposite:
            return EntryZone(float(m5["high"].iloc[k]), float(m5["low"].iloc[k]), m5["time"].iloc[k],
                             m5["time"].iloc[start])
        if same:
            start = k
        k -= 1
    return None


def find_entry(m1: pd.DataFrame, m5: pd.DataFrame, mss: MSS, sweep_extreme: float, now: pd.Timestamp,
               expiry_minutes: int, lookback: int = 10, tp_level_fn=None) -> EntryResult:
    buy = mss.direction == "BUY"
    zone = entry_zone(m5, mss, lookback)
    if zone is None:
        return EntryResult("NO_ENTRY_ZONE", reason="NO_OPPOSITE_CANDLE_BEFORE_DISPLACEMENT")
    deadline = mss.close_time + pd.Timedelta(minutes=expiry_minutes)
    tp = tp_level_fn(zone) if tp_level_fn else None
    for i in range(len(m1)):
        t0 = m1["time"].iloc[i]
        t1 = t0 + pd.Timedelta(seconds=60)
        if t0 < mss.close_time:
            continue
        if t1 > deadline:
            return EntryResult("EXPIRED", zone, reason="EXPIRY_AFTER_MSS")
        hi, lo, cl = float(m1["high"].iloc[i]), float(m1["low"].iloc[i]), float(m1["close"].iloc[i])
        if (buy and cl < sweep_extreme) or (not buy and cl > sweep_extreme):
            return EntryResult("INVALIDATED", zone, reason="CLOSE_BEYOND_SWEEP_EXTREME")
        if tp is not None and ((buy and hi >= tp) or (not buy and lo <= tp)):
            return EntryResult("CANCELLED_TP_FIRST", zone, reason="TARGET_REACHED_BEFORE_RETEST")
        touches = lo <= zone.upper and hi >= zone.lower
        confirmed = cl > zone.upper if buy else cl < zone.lower
        if touches and confirmed:
            return EntryResult("ENTRY", zone, cl, t1, t0)
    if now >= deadline:
        return EntryResult("EXPIRED", zone, reason="EXPIRY_AFTER_MSS")
    return EntryResult("WAITING", zone, reason="AWAITING_RETEST")
