"""Four-key Twelve Data credential manager (blueprint 4.4).

Keys are held only as opaque secrets; they are never returned by status()/repr and are
redacted from error text. Rotation moves to another eligible key only when a documented
limit is hit; whether separate accounts may be pooled is governed by Twelve Data's terms —
that must be verified by the account owner (the manager does not assume it).
"""
from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from cryptography.fernet import Fernet


class KeyStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    INVALID = "INVALID"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"


@dataclass
class Credential:
    label: str
    _secret: bytes = field(repr=False)          # Fernet-encrypted at rest in memory
    status: KeyStatus = KeyStatus.AVAILABLE
    available_at: datetime | None = None
    credits_used: int = 0
    credits_left: int | None = None
    requests: int = 0
    failures: int = 0
    last_error: str | None = None
    last_used: datetime | None = None
    fp: str = ''                                 # sha256 fingerprint prefix; lets saved state be matched to the same key


class NoEligibleCredential(Exception):
    pass


MAX_KEYS = 8   # TWELVE_DATA_KEY_1 .. TWELVE_DATA_KEY_8; unset/empty slots are ignored


class CredentialManager:
    def __init__(self, keys: dict[str, str], clock=lambda: datetime.now(timezone.utc),
                 fernet_key: str | None = None, sticky: bool = False):
        self.sticky = sticky
        self._f = Fernet((fernet_key or os.environ.get("GPTA_SECRET_KEY") or Fernet.generate_key()))
        self._clock = clock
        self._lock = threading.Lock()
        self._rr = 0
        self.creds = {lbl: Credential(lbl, self._f.encrypt(k.encode()), fp=hashlib.sha256(k.encode()).hexdigest()[:12])
                      for lbl, k in keys.items() if k}
        self.on_change = None        # optional callback(list[dict]) to persist key state (set by the app)

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.snapshot())
            except Exception:
                pass                 # persistence is best-effort; never break a data request

    def snapshot(self) -> list[dict]:
        return [{"label": c.label, "fp": c.fp, "status": c.status.value,
                 "available_at": c.available_at.isoformat() if c.available_at else None,
                 "requests": c.requests, "failures": c.failures, "last_error": c.last_error} for c in self.creds.values()]

    def restore(self, saved: list[dict]) -> int:
        """Re-apply saved blocking states after a restart (only if it is the same key and the block has not expired)."""
        n, now = 0, self._clock()
        for r in saved:
            c = self.creds.get(r["label"])
            if not c or c.fp != r["fp"] or r["status"] not in ("QUOTA_EXHAUSTED", "INVALID", "RATE_LIMITED", "COOLDOWN"):
                continue
            until = datetime.fromisoformat(r["available_at"]) if r["available_at"] else None
            if r["status"] != "INVALID" and (until is None or until <= now):
                continue
            c.status, c.available_at, c.last_error = KeyStatus(r["status"]), until, r.get("last_error")
            c.requests, c.failures = r.get("requests", 0), r.get("failures", 0)
            n += 1
        return n

    @classmethod
    def from_env(cls, **kw) -> "CredentialManager":
        keys = {f"key{i}": os.environ.get(f"TWELVE_DATA_KEY_{i}", "") for i in range(1, MAX_KEYS + 1)}
        # GPTA_KEY_MODE=sticky (default): every request uses the first working key; the next key takes over only
        # when it is exhausted/rate-limited/invalid. GPTA_KEY_MODE=rotate: round-robin across keys.
        kw.setdefault("sticky", os.environ.get("GPTA_KEY_MODE", "sticky").lower() != "rotate")
        return cls(keys, **kw)

    # -- selection ---------------------------------------------------------------------------
    def _recover(self, now: datetime) -> None:
        for c in self.creds.values():
            if c.status in (KeyStatus.RATE_LIMITED, KeyStatus.COOLDOWN, KeyStatus.QUOTA_EXHAUSTED) \
                    and c.available_at and now >= c.available_at:
                c.status, c.available_at = KeyStatus.AVAILABLE, None

    def acquire(self) -> tuple[str, str]:
        """Returns (label, secret). Raises NoEligibleCredential when nothing is usable."""
        with self._lock:
            now = self._clock()
            self._recover(now)
            labels = list(self.creds)
            if self.sticky:
                # first usable key in order; a key already IN_USE by a concurrent request of the same cycle is shared
                for lbl in labels:
                    c = self.creds[lbl]
                    if c.status in (KeyStatus.AVAILABLE, KeyStatus.IN_USE):
                        c.status, c.last_used = KeyStatus.IN_USE, now
                        c.requests += 1
                        return lbl, self._f.decrypt(c._secret).decode()
                raise NoEligibleCredential("all credentials unavailable")
            for off in range(len(labels)):
                lbl = labels[(self._rr + off) % len(labels)]
                c = self.creds[lbl]
                if c.status == KeyStatus.AVAILABLE:
                    self._rr = (self._rr + off + 1) % len(labels)
                    c.status, c.last_used = KeyStatus.IN_USE, now
                    c.requests += 1
                    return lbl, self._f.decrypt(c._secret).decode()
            raise NoEligibleCredential("all credentials unavailable")

    # -- outcome recording -------------------------------------------------------------------
    def release_ok(self, label: str, credits_used: int | None = None, credits_left: int | None = None):
        with self._lock:
            c = self.creds[label]
            c.status = KeyStatus.AVAILABLE
            if credits_used is not None:
                c.credits_used += credits_used           # provider-reported, not assumed
            if credits_left is not None:
                c.credits_left = credits_left

    def release_error(self, label: str, http_status: int | None, message: str = ""):
        """Classify by provider response. 401/403 -> INVALID, 429 -> per-minute limit (or daily quota
        when the message says so), anything else -> short COOLDOWN."""
        with self._lock:
            now = self._clock()
            c = self.creds[label]
            c.failures += 1
            c.last_error = self._redact(message, label)[:200]
            m = message.lower()
            if http_status in (401, 403) or "apikey" in m and "invalid" in m:
                c.status = KeyStatus.INVALID
            elif http_status == 429 and ("day" in m or "daily" in m):
                c.status = KeyStatus.QUOTA_EXHAUSTED
                c.available_at = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            elif http_status == 429:
                c.status = KeyStatus.RATE_LIMITED
                c.available_at = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            else:
                c.status = KeyStatus.COOLDOWN
                c.available_at = now + timedelta(seconds=30)
        self._changed()

    def set_disabled(self, label: str, disabled: bool):
        with self._lock:
            self.creds[label].status = KeyStatus.DISABLED if disabled else KeyStatus.AVAILABLE

    def _redact(self, text: str, label: str) -> str:
        secret = self._f.decrypt(self.creds[label]._secret).decode()
        return text.replace(secret, "***") if secret else text

    def redact(self, text: str) -> str:
        for lbl in self.creds:
            text = self._redact(text, lbl)
        return text

    def status(self) -> list[dict]:
        """Safe for API responses: no secrets."""
        with self._lock:
            self._recover(self._clock())
            return [{"label": c.label, "status": c.status.value, "requests": c.requests,
                     "failures": c.failures, "credits_used": c.credits_used,
                     "credits_left": c.credits_left, "available_at": c.available_at.isoformat() if c.available_at else None,
                     "last_error": c.last_error} for c in self.creds.values()]

    def stream_key(self) -> str | None:
        """Return one usable key for the separate WebSocket credit pool without reserving an API request.

        REST rate/quota cooldowns do not consume WebSocket credits; only invalid or explicitly disabled
        credentials are skipped here. The returned secret is for the streaming client and is never logged.
        """
        with self._lock:
            for c in self.creds.values():
                if c.status not in (KeyStatus.INVALID, KeyStatus.DISABLED):
                    return self._f.decrypt(c._secret).decode()
        return None
