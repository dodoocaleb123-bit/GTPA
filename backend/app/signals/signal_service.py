"""Turns QUALIFIED decisions into published signals and drives their lifecycle. All persistence and
notification side effects live here; the strategy engine stays pure."""
from __future__ import annotations

import json
from itertools import count

import pandas as pd

from ..database.store import Store, _now
from ..paper_trading.engine import PaperTrade, from_signal
from ..strategy.strategy_orchestrator import Decision
from ..telegram.notifier import Notifier, format_event, format_signal
from .lifecycle import TERMINAL, DuplicateGuard, Signal, advance


class SignalService:
    def __init__(self, store: Store, notifier: Notifier | None, cfg, strategy_version: str):
        self.store, self.notifier, self.cfg, self.version = store, notifier, cfg, strategy_version
        self.guard = DuplicateGuard({r["setup_id"] for r in store.q("SELECT setup_id FROM setups")})
        self.signals: dict[str, Signal] = {}
        self._n = count(store.q("SELECT COUNT(*) c FROM signals")[0]["c"] + 1)
        self.spread_at_entry: dict[str, float] = {}
        self._load_open()

    def _load_open(self):
        for r in self.store.q("SELECT * FROM signals WHERE state NOT IN ('REJECTED','INVALIDATED','EXPIRED','TP_HIT','SL_HIT','CLOSED_OTHER')"):
            self.signals[r["signal_id"]] = Signal(r["signal_id"], r["setup_id"], r["direction"], r["entry"], r["entry_low"],
                                                  r["entry_high"], r["sl"], r["tp"], r["rr"], pd.Timestamp(r["expiry"]),
                                                  pd.Timestamp(r["created_at"]), r["state"])

    def active(self) -> list[Signal]:
        return [s for s in self.signals.values() if s.state not in TERMINAL]

    def record_decision(self, d: Decision, now: pd.Timestamp) -> Signal | None:
        """Persist every terminal decision (qualified or not) for audit; publish if QUALIFIED."""
        if d.result == "WAITING" or not self.guard.new_setup(d.setup_id):
            return None
        self.store.x("INSERT OR IGNORE INTO setups VALUES(?,?,?,?,?,?)", (d.setup_id, d.direction, now.isoformat(), self.version,
                                                                           d.result, json.dumps(d.detail, default=str)))
        if d.qualification:
            self.store.x("INSERT INTO qualification_results(setup_id,ts,result,checks) VALUES(?,?,?,?)",
                         (d.setup_id, now.isoformat(), d.result, json.dumps(d.qualification.as_dict(), default=str)))
        if d.result != "QUALIFIED" or not d.signal:
            return None
        p = d.signal
        if p["rr"] < self.cfg.risk.minimum_reward_risk - 1e-9:      # last line of defence (rule #4)
            self.store.log("ERROR", "signals", f"blocked sub-minimum R signal {d.setup_id}")
            return None
        sid = f"GPTA-XAU-{next(self._n):04d}"
        sig = Signal(sid, d.setup_id, p["direction"], p["entry"], p["entry_low"], p["entry_high"], p["sl"], p["tp"],
                     p["rr"], pd.Timestamp(p["expiry"]), now)
        self.store.x("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, d.setup_id, sig.direction, sig.entry, sig.entry_low, sig.entry_high, sig.sl, sig.tp, sig.rr,
                      "QUALIFIED", now.isoformat(), p["expiry"], json.dumps(p, default=str)))
        self.signals[sid] = sig
        # PUBLISHED only once Telegram delivery is confirmed; a qualified signal is preserved if delivery fails
        sent = self.notifier.send(f"{sid}:SIGNAL", "TRADE SIGNAL", format_signal({**p, "signal_id": sid})) if self.notifier else True
        if sent:
            sig.move("PUBLISHED", now, "TELEGRAM_CONFIRMED")
            sig.move("WAITING_FOR_ENTRY", now)
        self._persist(sig, now)
        return sig

    def retry_unpublished(self, now: pd.Timestamp):
        for s in self.signals.values():
            if s.state == "QUALIFIED" and self.notifier:
                p = {"direction": s.direction, "entry_low": s.entry_low, "entry_high": s.entry_high, "sl": s.sl,
                     "tp": s.tp, "rr": s.rr, "expiry": s.expiry.isoformat(), "signal_id": s.signal_id}
                if self.notifier.send(f"{s.signal_id}:SIGNAL", "TRADE SIGNAL", format_signal(p)):
                    s.move("PUBLISHED", now, "TELEGRAM_CONFIRMED_ON_RETRY")
                    s.move("WAITING_FOR_ENTRY", now)
                    self._persist(s, now)

    def on_m1_candle(self, candle: pd.Series, lower_lookup=None, htf_invalid=False, safety_failed=False,
                     spread: float = 0.0) -> list[PaperTrade]:
        trades = []
        for sig in list(self.signals.values()):
            if sig.state not in ("WAITING_FOR_ENTRY", "ENTRY_OBSERVED"):
                continue
            new = advance(sig, candle, lower_lookup, htf_invalid=htf_invalid, safety_failed=safety_failed)
            if new is None:
                continue
            ts = candle["time"] + pd.Timedelta(seconds=60)
            for state in [h[1] for h in sig.history if h[1] in ("ENTRY_OBSERVED", "INVALIDATED", "EXPIRED", "TP_HIT", "SL_HIT", "CLOSED_OTHER")]:
                self._emit(sig, state, ts)
            if sig.state == "ENTRY_OBSERVED":
                self.spread_at_entry[sig.signal_id] = spread
            if sig.state in ("TP_HIT", "SL_HIT", "CLOSED_OTHER"):
                pt = from_signal(sig, self.spread_at_entry.get(sig.signal_id, spread), self.cfg.paper.slippage)
                if pt:
                    self.store.x("INSERT OR REPLACE INTO paper_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (pt.signal_id, pt.proposed_entry, pt.sim_entry, pt.sl, pt.tp, pt.entry_ts, pt.exit_ts,
                                  pt.exit_price, pt.outcome, pt.r_multiple, pt.spread, pt.slippage, pt.direction))
                    trades.append(pt)
            self._persist(sig, ts)
        return trades

    def _emit(self, sig: Signal, event: str, ts: pd.Timestamp):
        """Each lifecycle event is stored and notified at most once."""
        if not self.guard.new_event(sig.signal_id, event):
            return
        self.store.x("INSERT OR IGNORE INTO signal_events(signal_id,ts,event,detail) VALUES(?,?,?,?)",
                     (sig.signal_id, ts.isoformat(), event, sig.outcome_note))
        if self.notifier:
            self.notifier.send(f"{sig.signal_id}:{event}", event, format_event(event, sig.as_dict()))

    def _persist(self, sig: Signal, ts: pd.Timestamp):
        self.store.x("UPDATE signals SET state=? WHERE signal_id=?", (sig.state, sig.signal_id))
