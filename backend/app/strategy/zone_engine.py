"""Supply / demand zones from confirmed M15 swing candles (blueprint 9)."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .swing_engine import Swing

ACTIVE, TESTED, CONSUMED, INVALIDATED, EXPIRED = "ACTIVE", "TESTED", "CONSUMED", "INVALIDATED", "EXPIRED"
VALID = (ACTIVE, TESTED)


@dataclass
class Zone:
    id: str
    type: str                 # "demand" | "supply"
    upper: float
    lower: float
    created_idx: int          # confirmation index (zone is unknown before this candle closes)
    created_time: pd.Timestamp
    source_time: pd.Timestamp
    timeframe: str
    touches: int = 0
    status: str = ACTIVE
    invalidation_reason: str | None = None
    end_idx: int | None = None
    expiry_idx: int = 0

    def as_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "upper": self.upper, "lower": self.lower,
                "created_time": self.created_time.isoformat(), "source_time": self.source_time.isoformat(),
                "timeframe": self.timeframe, "touches": self.touches, "status": self.status,
                "invalidation_reason": self.invalidation_reason}


def build_zones(df: pd.DataFrame, swings: list[Swing], timeframe: str = "M15", expiry_candles: int = 96) -> list[Zone]:
    zones: list[Zone] = []
    hi, lo, cl, t = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), df["time"]
    n = len(df)
    for s in swings:
        typ = "demand" if s.kind == "L" else "supply"
        z = Zone(id=f"Z-{timeframe}-{typ}-{s.time.strftime('%Y%m%dT%H%M')}", type=typ,
                 upper=float(hi[s.idx]), lower=float(lo[s.idx]), created_idx=s.confirmed_idx,
                 created_time=t.iloc[s.confirmed_idx], source_time=s.time, timeframe=timeframe,
                 expiry_idx=s.confirmed_idx + expiry_candles)
        for k in range(s.confirmed_idx + 1, n):
            beyond = cl[k] < z.lower if typ == "demand" else cl[k] > z.upper
            if beyond:
                z.status, z.invalidation_reason, z.end_idx = INVALIDATED, "CLOSE_BEYOND_DISTAL", k
                break
            if k >= z.expiry_idx:
                z.status, z.invalidation_reason, z.end_idx = EXPIRED, "EXPIRY", k
                break
            if lo[k] <= z.upper and hi[k] >= z.lower:
                z.touches += 1
                z.status = TESTED
        zones.append(z)
    return zones


def valid_zones(zones: list[Zone], typ: str) -> list[Zone]:
    """Most recent first."""
    return sorted((z for z in zones if z.type == typ and z.status in VALID), key=lambda z: -z.created_idx)


def cluster(zones: list[Zone], threshold: float = 0.5) -> list[list[str]]:
    """Group same-type zones whose overlap >= threshold of the narrower zone. Identities are retained."""
    groups: list[list[Zone]] = []
    for z in sorted(zones, key=lambda z: z.created_idx):
        for g in groups:
            if g[0].type == z.type and any(_overlap(z, m) >= threshold for m in g):
                g.append(z)
                break
        else:
            groups.append([z])
    return [[m.id for m in g] for g in groups]


def _overlap(a: Zone, b: Zone) -> float:
    inter = min(a.upper, b.upper) - max(a.lower, b.lower)
    narrow = min(a.upper - a.lower, b.upper - b.lower)
    return 0.0 if inter <= 0 or narrow <= 0 else inter / narrow
