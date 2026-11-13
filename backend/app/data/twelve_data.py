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

    def fetch(self, timeframe: str, outputsize: int, now: datetime):
        import httpx
        try:
            label, key = self.creds.acquire()
        except NoEligibleCredential:
            return None, "NO_CREDENTIAL"
        try:
            r = httpx.get(BASE, params={"symbol": self.symbol, "interval": TD_INTERVAL[timeframe],
                                        "outputsize": outputsize, "timezone": "UTC", "order": "ASC",
                                        "apikey": key}, timeout=self.timeout)
            body = r.json()
        except Exception as e:  # network/JSON failure: never leak the key
            self.creds.release_error(label, None, self.creds.redact(str(e)))
            return None, "NETWORK_ERROR"
        code = body.get("code") if isinstance(body, dict) else None
        if r.status_code != 200 or (isinstance(body, dict) and body.get("status") == "error"):
            self.creds.release_error(label, int(code or r.status_code), str(body.get("message", "")))
            return None, f"PROVIDER_{code or r.status_code}"
        hdr = r.headers
        used = _int(hdr.get("api-credits-used"))
        left = _int(hdr.get("api-credits-left"))
        self.creds.release_ok(label, used, left)
        return body, None


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
