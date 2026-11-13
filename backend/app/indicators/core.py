"""EMA / ATR. Deterministic; values before the warm-up period are NaN (never 'fully initialised')."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(close: pd.Series, period: int) -> pd.Series:
    """EMA seeded with the SMA of the first `period` closes; NaN until then."""
    v = close.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    if len(v) < period:
        return pd.Series(out, index=close.index)
    k = 2.0 / (period + 1)
    out[period - 1] = v[:period].mean()
    for i in range(period, len(v)):
        out[i] = v[i] * k + out[i - 1] * (1 - k)
    return pd.Series(out, index=close.index)


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR seeded with the mean of the first `period` true ranges."""
    tr = true_range(df).to_numpy(dtype=float)
    out = np.full(len(tr), np.nan)
    if len(tr) < period:
        return pd.Series(out, index=df.index)
    out[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return pd.Series(out, index=df.index)


def warmup_ok(n_candles: int, period: int) -> bool:
    # EMA needs `period` seeds; require a further period to let the seed's influence decay.
    return n_candles >= period * 2
