"""Trend classification: HH+HL with a confirmed bullish BOS (or mirrored) else RANGING (blueprint 8.3-8.5)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..indicators.core import atr as atr_fn
from .swing_engine import Swing, find_swings

BULLISH, BEARISH, RANGING = "BULLISH", "BEARISH", "RANGING"


@dataclass
class TrendResult:
    state: str
    reason: str
    swings: list[Swing] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"state": self.state, "reason": self.reason}


def classify(df: pd.DataFrame, left: int, right: int, buffer: float, atr_period: int = 14,
             swings: list[Swing] | None = None) -> TrendResult:
    swings = swings if swings is not None else find_swings(df, left, right)
    a = atr_fn(df, atr_period)
    highs = [s for s in swings if s.kind == "H"]
    lows = [s for s in swings if s.kind == "L"]
    if len(highs) < 2 or len(lows) < 2 or a.isna().all():
        return TrendResult(RANGING, "INSUFFICIENT_STRUCTURE", swings)
    h1, h2, l1, l2 = highs[-2], highs[-1], lows[-2], lows[-1]
    close = df["close"].to_numpy()
    av = a.to_numpy()

    def first_break(start: int, level: float, up: bool) -> int | None:
        for k in range(start + 1, len(df)):
            if av[k] != av[k]:  # NaN
                continue
            if (up and close[k] > level + buffer * av[k]) or (not up and close[k] < level - buffer * av[k]):
                return k
        return None

    if h2.price > h1.price and l2.price > l1.price:
        bos = first_break(h1.confirmed_idx, h1.price, True)
        if bos is not None and first_break(l2.confirmed_idx, l2.price, False) is None:
            return TrendResult(BULLISH, "HH_HL_BOS", swings)
        return TrendResult(RANGING, "BULLISH_STRUCTURE_WITHOUT_BOS", swings)
    if h2.price < h1.price and l2.price < l1.price:
        bos = first_break(l1.confirmed_idx, l1.price, False)
        if bos is not None and first_break(h2.confirmed_idx, h2.price, True) is None:
            return TrendResult(BEARISH, "LH_LL_BOS", swings)
        return TrendResult(RANGING, "BEARISH_STRUCTURE_WITHOUT_BOS", swings)
    return TrendResult(RANGING, "CONFLICTING_STRUCTURE", swings)


def alignment(h1: str, m15: str) -> str | None:
    """'BUY' / 'SELL' / None (any disagreement or ranging => no trade)."""
    if h1 == BULLISH and m15 == BULLISH:
        return "BUY"
    if h1 == BEARISH and m15 == BEARISH:
        return "SELL"
    return None
