"""Confirmed pivot detection (blueprint 8.2): strict N-left / N-right, no equal highs/lows.

A pivot at index i is only reported when i + right <= last index, i.e. both right-hand candles are
complete. Because the function only ever sees the frame it is given, running it on data truncated at
time t can never leak anything after t (see tests/test_no_lookahead.py).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Swing:
    idx: int
    time: pd.Timestamp
    price: float
    kind: str            # "H" or "L"
    confirmed_idx: int   # index of the candle whose close confirmed the pivot

    def as_dict(self) -> dict:
        return {"idx": self.idx, "time": self.time.isoformat(), "price": self.price, "kind": self.kind,
                "confirmed_idx": self.confirmed_idx}


def find_swings(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[Swing]:
    hi, lo, t = df["high"].to_numpy(), df["low"].to_numpy(), df["time"]
    out: list[Swing] = []
    for i in range(left, len(df) - right):
        h, l = hi[i], lo[i]
        if all(h > hi[i - k] for k in range(1, left + 1)) and all(h > hi[i + k] for k in range(1, right + 1)):
            out.append(Swing(i, t.iloc[i], float(h), "H", i + right))
        if all(l < lo[i - k] for k in range(1, left + 1)) and all(l < lo[i + k] for k in range(1, right + 1)):
            out.append(Swing(i, t.iloc[i], float(l), "L", i + right))
    return out
