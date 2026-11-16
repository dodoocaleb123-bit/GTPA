"""Build UTC-aligned OHLC candles from a real-time price-tick stream.

Only bars observed for their entire interval are persisted as completed candles. The currently
forming bar is kept in memory for chart display. Completed bars are stored separately by provider
name so REST history/backfill cannot overwrite the live-stream aggregation.
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Mapping

from ..config import TF_SECONDS


PROVIDER = "twelvedata_ws_ticks"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TickCandleAggregator:
    def __init__(self, store, instrument: str, *, max_tick_age_seconds: float = 30.0,
                 finalize_grace_seconds: float = 2.0):
        self.store = store
        self.instrument = instrument
        self.max_tick_age_seconds = max(1.0, float(max_tick_age_seconds))
        self.finalize_grace_seconds = max(0.0, float(finalize_grace_seconds))
        self._lock = threading.RLock()
        self._connected_since: datetime | None = None
        self._connected = False
        self._last_tick_time: datetime | None = None
        self._last_tick_received: datetime | None = None
        self._current: dict[str, dict | None] = {tf: None for tf in TF_SECONDS}
        self._last_closed: dict[str, datetime | None] = {tf: None for tf in TF_SECONDS}
        self._dropped_partial = {tf: 0 for tf in TF_SECONDS}

    def on_connected(self, at: datetime | None = None) -> None:
        with self._lock:
            self._connected_since = _utc(at or datetime.now(timezone.utc))
            self._connected = True

    def on_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._connected_since = None
            # Any open bar that spans a connection gap is not safe to persist as a full OHLC bar.
            for bar in self._current.values():
                if bar is not None:
                    bar["eligible"] = False

    def ingest(self, tick) -> bool:
        """Apply one validated PriceTick. Returns False when it is stale, out of order or disconnected."""
        tick_time = _utc(tick.tick_time)
        received_at = _utc(tick.received_at)
        try:
            price = float(tick.price)
        except (TypeError, ValueError):
            return False
        lag = (received_at - tick_time).total_seconds()
        if not math.isfinite(price) or price <= 0 or lag < -5 or lag > self.max_tick_age_seconds:
            return False

        completed: list[tuple[str, dict]] = []
        with self._lock:
            if not self._connected or self._connected_since is None:
                return False
            if self._last_tick_time is not None and tick_time < self._last_tick_time:
                return False
            self._last_tick_time, self._last_tick_received = tick_time, received_at
            for tf, seconds in TF_SECONDS.items():
                bucket_epoch = int(tick_time.timestamp()) // seconds * seconds
                bucket_time = datetime.fromtimestamp(bucket_epoch, timezone.utc)
                closed = self._last_closed[tf]
                if closed is not None and bucket_time <= closed:
                    continue
                current = self._current[tf]
                if current is not None:
                    if bucket_time < current["time"]:
                        continue
                    if bucket_time > current["time"]:
                        self._complete_locked(tf, current, completed)
                        self._current[tf] = None
                        current = None
                if current is None:
                    current = {
                        "time": bucket_time,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "eligible": self._connected_since <= bucket_time,
                    }
                    self._current[tf] = current
                else:
                    current["high"] = max(current["high"], price)
                    current["low"] = min(current["low"], price)
                    current["close"] = price
        self._persist(completed)
        return True

    def finalize_due(self, now: datetime | None = None) -> int:
        """Close elapsed bars even when no new tick arrives exactly at the interval boundary."""
        now = _utc(now or datetime.now(timezone.utc))
        completed: list[tuple[str, dict]] = []
        with self._lock:
            for tf, seconds in TF_SECONDS.items():
                current = self._current[tf]
                if current is None:
                    continue
                end = current["time"].timestamp() + seconds + self.finalize_grace_seconds
                if now.timestamp() >= end:
                    self._complete_locked(tf, current, completed)
                    self._current[tf] = None
        self._persist(completed)
        return len(completed)

    def current_bars(self) -> dict[str, dict]:
        """Return copies of forming bars; partial coverage is explicitly identified."""
        with self._lock:
            return {
                tf: {
                    "time": bar["time"],
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "completed": False,
                    "coverage": "FULL" if bar["eligible"] else "PARTIAL",
                    "provider": PROVIDER,
                }
                for tf, bar in self._current.items() if bar is not None
            }

    def is_live(self, now: datetime | None = None) -> bool:
        now = _utc(now or datetime.now(timezone.utc))
        with self._lock:
            if not self._connected or self._last_tick_time is None:
                return False
            age = (now - self._last_tick_time).total_seconds()
            return -5 <= age <= self.max_tick_age_seconds

    def status(self, now: datetime | None = None) -> dict:
        now = _utc(now or datetime.now(timezone.utc))
        with self._lock:
            age = ((now - self._last_tick_time).total_seconds() if self._last_tick_time else None)
            return {
                "provider": PROVIDER,
                "connected": self._connected,
                "last_tick_time": self._last_tick_time.isoformat() if self._last_tick_time else None,
                "last_tick_age_seconds": round(max(0.0, age), 3) if age is not None else None,
                "dropped_partial_bars": dict(self._dropped_partial),
                "forming_timeframes": sorted(tf for tf, bar in self._current.items() if bar is not None),
            }

    def _complete_locked(self, tf: str, bar: Mapping, out: list[tuple[str, dict]]) -> None:
        self._last_closed[tf] = bar["time"]
        if not bar["eligible"]:
            self._dropped_partial[tf] += 1
            return
        out.append((tf, {
            "time": bar["time"],
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "close": bar["close"],
            "volume": None,
            "completed": True,
        }))

    def _persist(self, completed: list[tuple[str, dict]]) -> None:
        for tf, row in completed:
            self.store.upsert_candles(self.instrument, tf, [row], provider=PROVIDER)
