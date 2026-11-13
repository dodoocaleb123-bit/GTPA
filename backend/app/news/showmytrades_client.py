"""ShowMyTrades calendar client. The endpoint, auth scheme and response schema are UNVERIFIED (the blueprint
says "verify during integration"). Set SHOWMYTRADES_URL / SHOWMYTRADES_KEY and adjust `fetch` after
reading their API documentation. Failure raises, so the NewsGate goes stale and blocks signals (strict)."""
from __future__ import annotations

import os

from .calendar_service import Event, normalize_event


class ShowMyTradesClient:
    def __init__(self, url: str | None = None, key: str | None = None, source_tz: str = "UTC"):
        self.url = url or os.environ.get("SHOWMYTRADES_URL", "")
        self.key = key or os.environ.get("SHOWMYTRADES_KEY", "")
        self.source_tz = source_tz

    def fetch(self) -> list[Event]:
        import httpx
        if not self.url:
            raise RuntimeError("SHOWMYTRADES_URL not configured")
        r = httpx.get(self.url, headers={"Authorization": f"Bearer {self.key}"} if self.key else {}, timeout=15)
        r.raise_for_status()
        body = r.json()
        rows = body.get("events", body) if isinstance(body, dict) else body
        events = [e for e in (normalize_event(x, self.source_tz) for x in rows) if e]
        if rows and not events:
            raise ValueError("calendar payload could not be parsed")
        return events
