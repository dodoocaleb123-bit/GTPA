"""Stop loss, take profit, reward-to-risk (blueprint 14.2-14.4). TP is never extended to force R."""
from __future__ import annotations

import pandas as pd

from ..strategy.swing_engine import Swing
from .types import CheckResult, fail, ok, unavailable


def stop_loss(direction: str, sweep_extreme: float, atr_m15: float, buffer: float) -> float:
    return sweep_extreme - buffer * atr_m15 if direction == "BUY" else sweep_extreme + buffer * atr_m15


def check_stop(direction: str, entry: float, sl: float, atr_m15: float, max_atr_multiple: float) -> CheckResult:
    risk = entry - sl if direction == "BUY" else sl - entry
    if risk <= 0:
        return fail("valid_stop_loss", "STOP_ON_WRONG_SIDE", entry=entry, sl=sl)
    if risk > max_atr_multiple * atr_m15:
        return fail("valid_stop_loss", "STOP_TOO_WIDE", risk=risk, limit=max_atr_multiple * atr_m15)
    return ok("valid_stop_loss", sl=sl, risk=risk)


def take_profit(m15: pd.DataFrame, swings: list[Swing], entry: float, direction: str) -> float | None:
    """Nearest valid opposing M15 swing: untouched since confirmation, beyond entry."""
    buy = direction == "BUY"
    hi, lo = m15["high"].to_numpy(), m15["low"].to_numpy()
    cands = []
    for s in swings:
        if s.kind != ("H" if buy else "L"):
            continue
        if buy and s.price <= entry or (not buy and s.price >= entry):
            continue
        after = slice(s.confirmed_idx + 1, None)
        if len(hi[after]) and ((hi[after].max() >= s.price) if buy else (lo[after].min() <= s.price)):
            continue   # already traded through since it formed: no longer a valid target
        cands.append(s.price)
    if not cands:
        return None
    return min(cands) if buy else max(cands)


def reward_risk(direction: str, entry: float, sl: float, tp: float, spread: float) -> tuple[float, float]:
    """Returns (raw_R, cost_adjusted_R). Cost-adjusted charges one full spread against the trade:
    it widens risk and shrinks reward (conservative for both sides). Qualification uses cost-adjusted R."""
    if direction == "BUY":
        risk, reward = entry - sl, tp - entry
    else:
        risk, reward = sl - entry, entry - tp
    raw = reward / risk if risk > 0 else 0.0
    r_c, w_c = risk + spread, reward - spread
    adj = w_c / r_c if r_c > 0 and w_c > 0 else 0.0
    return raw, adj


def check_rr(raw: float, adj: float, minimum: float) -> CheckResult:
    if round(adj, 9) >= minimum:
        return ok("minimum_reward_risk", r=round(adj, 4), raw_r=round(raw, 4))
    return fail("minimum_reward_risk", "RR_BELOW_MINIMUM", r=round(adj, 4), raw_r=round(raw, 4), minimum=minimum)


def check_target(tp: float | None) -> CheckResult:
    return ok("valid_target", tp=tp) if tp is not None else fail("valid_target", "NO_VALID_OPPOSING_SWING")
