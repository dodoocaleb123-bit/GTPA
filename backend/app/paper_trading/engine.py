"""Paper trades + performance metrics (blueprint 19, 18.5). Paper trades are simulations, never fills."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..signals.lifecycle import Signal


@dataclass
class PaperTrade:
    signal_id: str
    direction: str
    proposed_entry: float
    sim_entry: float
    sl: float
    tp: float
    entry_ts: str
    exit_ts: str | None
    exit_price: float | None
    outcome: str                  # WIN / LOSS / AMBIGUOUS / OPEN
    r_multiple: float | None
    spread: float
    slippage: float
    session: str = ""
    atr_regime: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def session_of(ts: pd.Timestamp) -> str:
    h = ts.hour
    return "ASIA" if h < 7 else "LONDON" if h < 13 else "NEW_YORK" if h < 21 else "OFF_HOURS"


def from_signal(sig: Signal, spread: float, slippage: float, atr_regime: str = "") -> PaperTrade | None:
    """Simulated fill: worse than proposed by spread + slippage on entry; exits charged slippage too.
    R is measured against the ORIGINAL risk (proposed entry to SL) so costs show up as lost R."""
    if sig.entry_time is None:
        return None
    buy = sig.direction == "BUY"
    sim_entry = sig.entry + (spread + slippage if buy else slippage)
    risk = abs(sig.entry - sig.sl)
    if sig.state == "TP_HIT":
        exit_p = sig.tp - slippage
        r = ((exit_p - sim_entry) if buy else (sim_entry - exit_p - spread)) / risk
        out = "WIN"
    elif sig.state == "SL_HIT":
        exit_p = sig.sl - slippage if buy else sig.sl + slippage
        r = ((exit_p - sim_entry) if buy else (sim_entry - exit_p - spread)) / risk
        out = "LOSS"
    elif sig.state == "CLOSED_OTHER":
        exit_p, r, out = None, None, "AMBIGUOUS"
    else:
        exit_p, r, out = None, None, "OPEN"
    return PaperTrade(sig.signal_id, sig.direction, sig.entry, sim_entry, sig.sl, sig.tp, sig.entry_time.isoformat(),
                      sig.exit_time.isoformat() if sig.exit_time is not None else None, exit_p, out,
                      None if r is None else round(float(r), 4), spread, slippage, session_of(sig.entry_time), atr_regime)


def metrics(trades: list[PaperTrade]) -> dict:
    done = [t for t in trades if t.outcome in ("WIN", "LOSS")]
    rs = [t.r_multiple for t in done]
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    best = worst = cur_w = cur_l = 0
    for r in rs:
        cur_w, cur_l = (cur_w + 1, 0) if r > 0 else (0, cur_l + 1)
        best, worst = max(best, cur_w), max(worst, cur_l)
    dur = [(pd.Timestamp(t.exit_ts) - pd.Timestamp(t.entry_ts)).total_seconds() / 60 for t in done if t.exit_ts]
    by_session: dict[str, dict] = {}
    for t in done:
        s = by_session.setdefault(t.session, {"trades": 0, "net_r": 0.0})
        s["trades"] += 1
        s["net_r"] = round(s["net_r"] + t.r_multiple, 4)
    gl = abs(sum(losses))
    return {"label": "PAPER/SIMULATED - not broker-executed results",
            "total_trades": len(trades), "completed": len(done), "ambiguous": sum(t.outcome == "AMBIGUOUS" for t in trades),
            "open": sum(t.outcome == "OPEN" for t in trades),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(done), 4) if done else None,
            "profit_factor": round(sum(wins) / gl, 4) if gl > 0 else (None if not wins else float("inf")),
            "net_r": round(sum(rs), 4), "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
            "max_drawdown_r": round(dd, 4), "max_consecutive_wins": best, "max_consecutive_losses": worst,
            "avg_duration_min": round(sum(dur) / len(dur), 2) if dur else None, "by_session": by_session}
