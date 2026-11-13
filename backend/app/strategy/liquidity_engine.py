"""Liquidity sweeps on M15 (blueprint 10). A wick alone is never enough: a completed candle must close
back beyond the swept level, within `reclaim_window` candles of the first breach, without the zone being
invalidated on or before the reclaim candle."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .swing_engine import Swing
from .zone_engine import Zone


@dataclass
class Sweep:
    id: str
    direction: str            # "BUY" (bullish sweep of a low) | "SELL"
    zone_id: str
    level: float              # swept swing price
    level_time: pd.Timestamp
    extreme: float            # sweep low (BUY) / sweep high (SELL)
    breach_time: pd.Timestamp
    confirm_time: pd.Timestamp        # open time of the reclaim candle
    confirm_close_time: pd.Timestamp  # when the reclaim candle completed
    confirm_idx: int
    breach_idx: int = 0
    status: str = "CONFIRMED"

    def as_dict(self) -> dict:
        return {k: (v.isoformat() if isinstance(v, pd.Timestamp) else v) for k, v in self.__dict__.items()}


def detect_sweeps(m15: pd.DataFrame, swings: list[Swing], zones: list[Zone], direction: str,
                  window: int = 3, lookback: int = 8, tf_seconds: int = 900) -> list[Sweep]:
    buy = direction == "BUY"
    hi, lo, cl, t = m15["high"].to_numpy(), m15["low"].to_numpy(), m15["close"].to_numpy(), m15["time"]
    n = len(m15)
    out: list[Sweep] = []
    for s in swings:
        if s.kind != ("L" if buy else "H"):
            continue
        breach = next((k for k in range(s.confirmed_idx + 1, n)
                       if (lo[k] < s.price if buy else hi[k] > s.price)), None)
        if breach is None:
            continue
        reclaim = next((r for r in range(breach, min(breach + window, n))
                        if (cl[r] > s.price if buy else cl[r] < s.price)), None)
        if reclaim is None or reclaim < n - lookback:
            continue
        zone = None
        for z in sorted((z for z in zones if z.type == ("demand" if buy else "supply")), key=lambda z: -z.created_idx):
            if z.created_idx >= breach:
                continue
            if z.end_idx is not None and z.end_idx <= reclaim:
                continue  # invalidated/expired on or before the reclaim candle
            if any(lo[k] <= z.upper and hi[k] >= z.lower for k in range(breach, reclaim + 1)):
                zone = z
                break
        if zone is None:
            continue
        seg = slice(breach, reclaim + 1)
        extreme = float(lo[seg].min() if buy else hi[seg].max())
        out.append(Sweep(
            id=f"SW-{direction}-{s.time.strftime('%Y%m%dT%H%M')}-{t.iloc[reclaim].strftime('%Y%m%dT%H%M')}",
            direction=direction, zone_id=zone.id, level=s.price, level_time=s.time, extreme=extreme,
            breach_time=t.iloc[breach], confirm_time=t.iloc[reclaim],
            confirm_close_time=t.iloc[reclaim] + pd.Timedelta(seconds=tf_seconds), confirm_idx=reclaim,
            breach_idx=breach))
    return sorted(out, key=lambda x: -x.confirm_idx)
