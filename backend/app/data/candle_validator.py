"""Candle response validation (blueprint 4.5). Never invents or substitutes data."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..config import TF_SECONDS


def is_market_open(t: datetime) -> bool:
    """Approximate spot-gold hours (UTC): closed Fri 22:00 -> Sun 23:00, and 22:00-23:00 daily.
    Configure/replace with the broker's real session table before production."""
    wd, h = t.weekday(), t.hour
    if wd == 5:
        return False
    if wd == 4 and h >= 22:
        return False
    if wd == 6 and h < 23:
        return False
    if h == 22:
        return False
    return True


@dataclass
class ValidationReport:
    accepted: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    stale: bool = False
    healthy: bool = True

    def add(self, code: str, **kw):
        self.issues.append({"code": code, **kw})


def parse_time(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def validate_response(payload: dict, timeframe: str, instrument: str, now: datetime,
                      stale_factor: float = 2.0) -> ValidationReport:
    """payload = Twelve Data style {'meta': {...}, 'values': [{datetime, open, high, low, close, volume?}]}."""
    rep = ValidationReport()
    tf = TF_SECONDS[timeframe]
    meta = payload.get("meta", {})
    sym = str(meta.get("symbol", "")).replace("/", "").upper()
    if sym != instrument.replace("/", "").upper():
        rep.add("WRONG_INSTRUMENT", got=meta.get("symbol"))
        rep.healthy = False
        return rep
    iv = str(meta.get("interval", ""))
    from ..config import TD_INTERVAL
    if iv != TD_INTERVAL[timeframe]:
        rep.add("WRONG_TIMEFRAME", got=iv)
        rep.healthy = False
        return rep

    seen: set[datetime] = set()
    for row in payload.get("values", []):
        try:
            t = parse_time(row["datetime"])
            o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, ValueError, TypeError):
            rep.add("MALFORMED_ROW", row=row)
            continue
        if min(o, h, l, c) <= 0 or h < l or h < max(o, c) or l > min(o, c):
            rep.add("INVALID_OHLC", time=t.isoformat())
            continue
        if t in seen:
            rep.add("DUPLICATE_TIMESTAMP", time=t.isoformat())
            continue
        if t.timestamp() % tf != 0:
            rep.add("MISALIGNED_TIMESTAMP", time=t.isoformat())
            continue
        seen.add(t)
        vol = row.get("volume")
        rep.accepted.append({
            "time": t, "open": o, "high": h, "low": l, "close": c,
            "volume": float(vol) if vol not in (None, "") else None,
            # completed only when the interval has fully elapsed at retrieval time
            "completed": t + timedelta(seconds=tf) <= now,
        })
    rep.accepted.sort(key=lambda r: r["time"])
    _check_gaps(rep, timeframe)
    done = [r for r in rep.accepted if r["completed"]]
    if not done:
        rep.stale = True
    else:
        age = (now - (done[-1]["time"] + timedelta(seconds=tf))).total_seconds()
        # stale only while the market should be open
        if age > tf * stale_factor + 60 and is_market_open(now):
            rep.stale = True
            rep.add("STALE_RESPONSE", age_seconds=age)
    rep.healthy = not rep.stale and not any(i["code"] in ("MISSING_INTERVAL",) for i in rep.issues[-5:])
    return rep


def _check_gaps(rep: ValidationReport, timeframe: str) -> None:
    tf = timedelta(seconds=TF_SECONDS[timeframe])
    ts = [r["time"] for r in rep.accepted]
    for a, b in zip(ts, ts[1:]):
        t = a + tf
        while t < b:
            if is_market_open(t):
                rep.add("MISSING_INTERVAL", time=t.isoformat())
                break  # one issue per gap is enough
            t += tf


def to_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.reset_index(drop=True)
