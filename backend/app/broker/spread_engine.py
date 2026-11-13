"""Broker quote handling and spread qualification (blueprint 5). Unavailable data never passes."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from ..qualification.types import CheckResult, fail, ok, unavailable


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    tick_time: datetime
    retrieved_at: datetime
    server: str = ""
    connected: bool = True

    @property
    def valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def as_dict(self, now: datetime | None = None) -> dict:
        d = {"symbol": self.symbol, "bid": self.bid, "ask": self.ask, "spread": round(self.spread, 5),
             "tick_time": self.tick_time.isoformat(), "server": self.server, "connected": self.connected,
             "valid": self.valid}
        if now:
            d["age_seconds"] = (now - self.tick_time).total_seconds()
        return d


class SpreadEngine:
    def __init__(self, cfg):
        self.cfg = cfg                      # the `spread` config section
        self._hist: deque[tuple[datetime, float]] = deque(maxlen=max(cfg.rolling_observations * 4, 500))
        self.last: Quote | None = None
        self._lock = threading.Lock()

    def observe(self, q: Quote) -> None:
        with self._lock:
            self.last = q
            if q.valid and q.connected and (not self._hist or q.tick_time > self._hist[-1][0]):
                self._hist.append((q.tick_time, q.spread))

    def check(self, now: datetime, stop_distance: float, quote: Quote | None = None,
              simulated_spread: float | None = None) -> CheckResult:
        """`simulated_spread` is for backtests only: staleness / median rules are skipped and the result
        is explicitly labelled SIMULATED in its detail."""
        c = self.cfg
        if not c.enabled:
            return unavailable("valid_spread", "SPREAD_CHECK_DISABLED")  # disabled != passed (fail safe)
        if simulated_spread is not None:
            if simulated_spread > c.maximum_stop_distance_ratio * stop_distance:
                return fail("valid_spread", "SPREAD_EXCEEDS_STOP_RATIO", spread=simulated_spread, simulated=True)
            return ok("valid_spread", spread=simulated_spread, simulated=True)
        q = quote or self.last
        if q is None or not q.connected:
            return unavailable("valid_spread", "MT5_UNAVAILABLE")
        if not q.valid:
            return fail("valid_spread", "INVALID_QUOTE", bid=q.bid, ask=q.ask)
        age = (now - q.tick_time).total_seconds()
        if age > c.maximum_quote_age_seconds or age < -2:
            return fail("valid_spread", "QUOTE_STALE", age_seconds=age)
        sp = q.spread
        if sp > c.maximum_stop_distance_ratio * stop_distance:
            return fail("valid_spread", "SPREAD_EXCEEDS_STOP_RATIO", spread=sp, stop_distance=stop_distance)
        with self._lock:
            prior = [s for t, s in self._hist if t < q.tick_time][-c.rolling_observations:]
        if len(prior) < c.minimum_observations:
            return unavailable("valid_spread", "INSUFFICIENT_SPREAD_HISTORY", have=len(prior))
        med = median(prior)
        if sp > c.rolling_median_multiplier * med:
            return fail("valid_spread", "SPREAD_EXCEEDS_MEDIAN_MULTIPLE", spread=sp, median=med)
        return ok("valid_spread", spread=sp, median=med)
