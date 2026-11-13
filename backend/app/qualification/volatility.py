"""Volatility gate on M15 ATR percentile band (blueprint 13). Current ATR is excluded from the sample."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .types import CheckResult, fail, ok, unavailable


def check_volatility(atr_series: pd.Series, lookback: int, lo_pct: float, hi_pct: float) -> CheckResult:
    s = atr_series.dropna()
    if len(s) < lookback + 1:
        return unavailable("valid_volatility", "INSUFFICIENT_ATR_HISTORY", have=len(s), need=lookback + 1)
    cur = float(s.iloc[-1])
    prior = s.iloc[-(lookback + 1):-1].to_numpy()
    lo, hi = np.percentile(prior, lo_pct), np.percentile(prior, hi_pct)
    d = {"atr": cur, "p_low": float(lo), "p_high": float(hi)}
    if cur < lo:
        return fail("valid_volatility", "VOLATILITY_TOO_LOW", **d)
    if cur > hi:
        return fail("valid_volatility", "VOLATILITY_TOO_HIGH", **d)
    return ok("valid_volatility", **d)
