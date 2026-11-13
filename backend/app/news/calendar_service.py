"""Economic calendar normalisation, USD event filtering, blackout windows and the strict news gate
(blueprint 6). The ShowMyTrades payload format is NOT verified: `normalize_event` accepts several common
key spellings and rejects anything it cannot parse. Verify against the real API during integration."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..qualification.types import CheckResult, fail, ok, unavailable

KEYWORDS = ("cpi", "consumer price", "non-farm", "nonfarm", "nfp", "federal funds", "interest rate decision",
            "fomc", "fed chair", "press conference", "gdp", "fed ")
IMPACT_MAP = {"high": "HIGH", "3": "HIGH", "red": "HIGH", "high impact": "HIGH",
              "medium": "MEDIUM", "2": "MEDIUM", "orange": "MEDIUM", "low": "LOW", "1": "LOW", "yellow": "LOW"}


@dataclass(frozen=True)
class Event:
    event_id: str
    name: str
    currency: str
    impact: str
    ts_utc: datetime
    previous: str | None = None
    forecast: str | None = None
    actual: str | None = None

    def as_dict(self) -> dict:
        return {**self.__dict__, "ts_utc": self.ts_utc.isoformat()}


def normalize_event(raw: dict, source_tz: str = "UTC") -> Event | None:
    g = lambda *ks: next((raw[k] for k in ks if raw.get(k) not in (None, "")), None)
    name, cur, ts = g("name", "event", "title"), g("currency", "country_currency", "ccy"), g("datetime", "time", "date_utc", "timestamp")
    if not (name and cur and ts):
        return None
    try:
        if isinstance(ts, (int, float)):
            t = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            t = t.replace(tzinfo=ZoneInfo(source_tz)) if t.tzinfo is None else t
            t = t.astimezone(timezone.utc)
    except (ValueError, OSError):
        return None
    impact = IMPACT_MAP.get(str(g("impact", "importance", "volatility") or "").strip().lower(), "UNKNOWN")
    return Event(str(g("id", "event_id") or f"{cur}-{name}-{t.isoformat()}"), str(name), str(cur).upper(),
                 impact, t, g("previous"), g("forecast"), g("actual"))


def is_qualifying(e: Event, currencies=("USD",)) -> bool:
    if e.currency not in currencies:
        return False
    return e.impact == "HIGH" or any(k in e.name.lower() for k in KEYWORDS)


class NewsGate:
    def __init__(self, cfg, clock=lambda: datetime.now(timezone.utc)):
        self.cfg, self._clock = cfg, clock
        self.events: list[Event] = []
        self.last_refresh: datetime | None = None
        self._lock = threading.Lock()

    def update(self, events: list[Event], refreshed_at: datetime | None = None) -> None:
        with self._lock:
            self.events = sorted(events, key=lambda e: e.ts_utc)
            self.last_refresh = refreshed_at or self._clock()

    def blackouts(self) -> list[dict]:
        pre, post = timedelta(minutes=self.cfg.pre_event_minutes), timedelta(minutes=self.cfg.post_event_minutes)
        return [{"event": e.as_dict(), "start": (e.ts_utc - pre).isoformat(), "end": (e.ts_utc + post).isoformat()}
                for e in self.events if is_qualifying(e, self.cfg.currencies)]

    def status(self, now: datetime | None = None) -> dict:
        now = now or self._clock()
        age = (now - self.last_refresh).total_seconds() / 60 if self.last_refresh else None
        return {"reliable": age is not None and age <= self.cfg.maximum_calendar_age_minutes,
                "age_minutes": age, "events": len(self.events)}

    def check(self, now: datetime | None = None) -> CheckResult:
        now = now or self._clock()
        if not self.cfg.enabled:
            return unavailable("valid_news", "NEWS_CHECK_DISABLED")
        st = self.status(now)
        if not st["reliable"]:           # strict mode: stale/missing calendar is NOT "no news"
            return unavailable("valid_news", "CALENDAR_UNRELIABLE", age_minutes=st["age_minutes"])
        pre, post = timedelta(minutes=self.cfg.pre_event_minutes), timedelta(minutes=self.cfg.post_event_minutes)
        for e in self.events:
            if is_qualifying(e, self.cfg.currencies) and e.ts_utc - pre <= now <= e.ts_utc + post:
                return fail("valid_news", "NEWS_BLACKOUT", event=e.name, event_time=e.ts_utc.isoformat())
        return ok("valid_news")
