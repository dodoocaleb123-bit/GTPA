"""Twelve Data provider. One independent request per timeframe, never derived from another.

NOTE: request shape (/time_series, interval names, `api-credits-*` headers) follows Twelve Data's public
docs as I know them and must be verified against the live account before deployment (blueprint sec. 27).
httpx is imported lazily so the rest of the system is importable without it.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Protocol

from ..config import TD_INTERVAL
from .candle_validator import ValidationReport, validate_response
from .credential_manager import CredentialManager, NoEligibleCredential

BASE = "https://api.twelvedata.com/time_series"


class CandleProvider(Protocol):
    def fetch(self, timeframe: str, outputsize: int, now: datetime) -> tuple[dict | None, str | None]:
        """Return (payload, error_code)."""


class TwelveDataProvider:
    def __init__(self, creds: CredentialManager, symbol: str = "XAU/USD", timeout: float = 10.0):
        self.creds, self.symbol, self.timeout = creds, symbol, timeout
        self.last_detail: dict[str, str] = {}      # latest provider failure text per timeframe (redacted)

    KEY_FAILURES = (401, 403, 429)    # failures that belong to the key, not the request: try the next key at once

    def fetch(self, timeframe: str, outputsize: int, now: datetime):
        import httpx
        last: tuple[None, str] = (None, "NO_CREDENTIAL")
        for _ in range(max(1, len(self.creds.creds))):
            try:
                label, key = self.creds.acquire()
            except NoEligibleCredential:
                return last
            try:
                r = httpx.get(BASE, params={"symbol": self.symbol, "interval": TD_INTERVAL[timeframe],
                                            "outputsize": outputsize, "timezone": "UTC", "order": "ASC",
                                            "apikey": key}, timeout=self.timeout)
                body = r.json()
            except Exception as e:  # network/JSON failure: never leak the key
                self.creds.release_error(label, None, self.creds.redact(str(e)))
                self.last_detail[timeframe] = f"{label}: network error"
                return None, "NETWORK_ERROR"
            code = body.get("code") if isinstance(body, dict) else None
            if r.status_code != 200 or (isinstance(body, dict) and body.get("status") == "error"):
                status = int(code or r.status_code)
                msg = str(body.get("message", "")) if isinstance(body, dict) else ""
                self.creds.release_error(label, status, msg)
                self.last_detail[timeframe] = f"{label}: HTTP {status} {self.creds.redact(msg)[:160]}"
                last = (None, f"PROVIDER_{code or r.status_code}")
                if status in self.KEY_FAILURES:
                    continue                      # this key is now parked; retry this same request on the next key
                return last
            hdr = r.headers
            self.creds.release_ok(label, _int(hdr.get("api-credits-used")), _int(hdr.get("api-credits-left")))
            self.last_detail.pop(timeframe, None)
            return body, None
        return last


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def collect_all(provider: CandleProvider, instrument: str, sizes: dict[str, int], now: datetime
                ) -> dict[str, tuple[ValidationReport | None, str | None]]:
    """Four independent concurrent requests; each result is processed on its own."""
    def one(tf: str):
        payload, err = provider.fetch(tf, sizes[tf], now)
        if payload is None:
            return tf, (None, err)
        return tf, (validate_response(payload, tf, instrument, now), None)

    with ThreadPoolExecutor(max_workers=4) as ex:
        return dict(ex.map(one, list(sizes)))
