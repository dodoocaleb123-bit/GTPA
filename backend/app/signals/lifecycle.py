"""Signal state machine, duplicate guard and TP/SL monitoring (blueprint 15)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

TRANSITIONS = {
    "CANDIDATE": {"QUALIFIED", "REJECTED"},
    "QUALIFIED": {"PUBLISHED"},
    "PUBLISHED": {"WAITING_FOR_ENTRY"},
    "WAITING_FOR_ENTRY": {"ENTRY_OBSERVED", "INVALIDATED", "EXPIRED"},
    "ENTRY_OBSERVED": {"TP_HIT", "SL_HIT", "CLOSED_OTHER"},
}
TERMINAL = {"REJECTED", "INVALIDATED", "EXPIRED", "TP_HIT", "SL_HIT", "CLOSED_OTHER"}


class IllegalTransition(Exception):
    pass


@dataclass
class Signal:
    signal_id: str
    setup_id: str
    direction: str
    entry: float
    entry_low: float
    entry_high: float
    sl: float
    tp: float
    rr: float
    expiry: pd.Timestamp
    created_at: pd.Timestamp
    state: str = "QUALIFIED"
    history: list = field(default_factory=list)       # (ts, state, note)
    entry_time: pd.Timestamp | None = None
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    outcome_note: str = ""

    def move(self, new: str, ts: pd.Timestamp, note: str = "") -> None:
        if self.state in TERMINAL:
            raise IllegalTransition(f"{self.signal_id} is terminal ({self.state}); cannot reactivate")
        if new not in TRANSITIONS.get(self.state, set()):
            raise IllegalTransition(f"{self.state} -> {new}")
        self.state = new
        self.history.append((ts.isoformat(), new, note))

    def as_dict(self) -> dict:
        d = {k: (v.isoformat() if isinstance(v, pd.Timestamp) else v) for k, v in self.__dict__.items()}
        return d


class DuplicateGuard:
    """Permanent registry of setup ids and one-shot notification keys."""

    def __init__(self, seen: set[str] | None = None):
        self.setups: set[str] = set(seen or ())
        self.events: set[tuple[str, str]] = set()

    def new_setup(self, setup_id: str) -> bool:
        if setup_id in self.setups:
            return False
        self.setups.add(setup_id)
        return True

    def new_event(self, signal_id: str, event: str) -> bool:
        k = (signal_id, event)
        if k in self.events:
            return False
        self.events.add(k)
        return True


def resolve_ambiguous(sig: Signal, lower: pd.DataFrame | None) -> str:
    """A candle touched both TP and SL: use lower-timeframe candles inside it. Returns TP / SL / AMBIGUOUS.
    Never assumes the profitable outcome."""
    if lower is None or lower.empty:
        return "AMBIGUOUS"
    buy = sig.direction == "BUY"
    for _, r in lower.iterrows():
        hit_tp = r["high"] >= sig.tp if buy else r["low"] <= sig.tp
        hit_sl = r["low"] <= sig.sl if buy else r["high"] >= sig.sl
        if hit_tp and hit_sl:
            return "AMBIGUOUS"
        if hit_tp:
            return "TP"
        if hit_sl:
            return "SL"
    return "AMBIGUOUS"


def advance(sig: Signal, candle: pd.Series, lower_lookup=None, *, htf_invalid: bool = False,
            safety_failed: bool = False) -> str | None:
    """Feed ONE completed M1 candle (in time order). Returns the new state if one was reached.
    The earliest applicable condition wins: expiry -> invalidation -> entry -> exit."""
    buy = sig.direction == "BUY"
    t0 = candle["time"]
    t1 = t0 + pd.Timedelta(seconds=60)
    if sig.state in TERMINAL or t1 <= sig.created_at:
        return None
    hi, lo, cl = candle["high"], candle["low"], candle["close"]

    if sig.state == "WAITING_FOR_ENTRY":
        if t1 > sig.expiry:
            sig.move("EXPIRED", t1, "EXPIRY_TIME_REACHED")
            return "EXPIRED"
        tp_first = hi >= sig.tp if buy else lo <= sig.tp
        sl_through = cl < sig.sl if buy else cl > sig.sl
        if htf_invalid or safety_failed or sl_through or tp_first:
            reason = ("TARGET_REACHED_BEFORE_ENTRY" if tp_first else "STRUCTURAL_INVALIDATION" if sl_through
                      else "DIRECTION_CHANGED" if htf_invalid else "SAFETY_CONDITION_FAILED")
            sig.move("INVALIDATED" if reason != "TARGET_REACHED_BEFORE_ENTRY" else "EXPIRED", t1, reason)
            return sig.state
        if lo <= sig.entry <= hi:
            sig.entry_time = t1
            sig.move("ENTRY_OBSERVED", t1, "PRICE_TRADED_AT_ENTRY")   # NOT a broker fill
            just_entered = True
            # the same candle may also reach SL/TP: fall through to exit check below
        else:
            return None
    else:
        just_entered = False

    if sig.state == "ENTRY_OBSERVED":
        hit_tp = hi >= sig.tp if buy else lo <= sig.tp
        hit_sl = lo <= sig.sl if buy else hi >= sig.sl
        if hit_tp and hit_sl:
            which = resolve_ambiguous(sig, lower_lookup(t0) if lower_lookup else None)
        else:
            which = "TP" if hit_tp else "SL" if hit_sl else None
        if which == "TP":
            sig.exit_time, sig.exit_price = t1, sig.tp
            sig.move("TP_HIT", t1)
        elif which == "SL":
            sig.exit_time, sig.exit_price = t1, sig.sl
            sig.move("SL_HIT", t1)
        elif which == "AMBIGUOUS":
            sig.exit_time, sig.outcome_note = t1, "AMBIGUOUS_TP_SL_SAME_CANDLE"
            sig.move("CLOSED_OTHER", t1, "AMBIGUOUS_TP_SL_SAME_CANDLE")
        else:
            return "ENTRY_OBSERVED" if just_entered else None
        return sig.state
    return None
