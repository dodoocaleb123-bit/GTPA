"""Market structure shift on M5 after a qualifying M15 sweep (blueprint 11)."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..indicators.core import atr as atr_fn
from .liquidity_engine import Sweep
from .swing_engine import find_swings


@dataclass
class MSS:
    id: str
    sweep_id: str
    direction: str
    level: float              # internal swing broken
    level_time: pd.Timestamp
    candle_idx: int           # M5 index of the displacement/MSS candle
    candle_time: pd.Timestamp
    close_time: pd.Timestamp  # when it completed (expiry clock starts here)

    def as_dict(self) -> dict:
        return {k: (v.isoformat() if isinstance(v, pd.Timestamp) else v) for k, v in self.__dict__.items()}


def detect_mss(m5: pd.DataFrame, sweep: Sweep, left: int, right: int, buffer: float, min_body: float,
               atr_period: int = 14, tf_seconds: int = 300) -> tuple[MSS | None, str]:
    """Returns (mss, status) with status in FOUND / WAITING / INVALIDATED / NO_INTERNAL_STRUCTURE."""
    buy = sweep.direction == "BUY"
    a = atr_fn(m5, atr_period).to_numpy()
    sw = [s for s in find_swings(m5, left, right)
          if s.kind == ("H" if buy else "L") and s.time < sweep.breach_time]
    if not sw:
        return None, "NO_INTERNAL_STRUCTURE"
    lvl = sw[-1]
    o, h, l, c, t = (m5[k].to_numpy() for k in ("open", "high", "low", "close", "time"))
    for k in range(len(m5)):
        close_time = m5["time"].iloc[k] + pd.Timedelta(seconds=tf_seconds)
        if close_time <= sweep.confirm_close_time or a[k] != a[k]:
            continue
        # structure fails if price closes beyond the sweep extreme before the MSS
        if (buy and c[k] < sweep.extreme) or (not buy and c[k] > sweep.extreme):
            return None, "INVALIDATED"
        rng = h[k] - l[k]
        body = abs(c[k] - o[k]) / rng if rng > 0 else 0.0
        broke = c[k] > lvl.price + buffer * a[k] if buy else c[k] < lvl.price - buffer * a[k]
        right_colour = c[k] > o[k] if buy else c[k] < o[k]
        if broke and right_colour and body >= min_body:
            return MSS(id=f"MSS-{sweep.id}", sweep_id=sweep.id, direction=sweep.direction, level=lvl.price,
                       level_time=lvl.time, candle_idx=k, candle_time=m5["time"].iloc[k],
                       close_time=close_time), "FOUND"
    return None, "WAITING"
