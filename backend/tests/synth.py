"""Deterministic synthetic XAU/USD market containing ONE complete textbook BUY setup, for tests only.
It is SIMULATED data: it says nothing about real-market behaviour or strategy performance."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

NOW = pd.Timestamp("2026-03-26 13:15:00", tz="UTC")   # the M1 entry-confirmation candle closes here


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(f"2026-03-26 {s}", tz="UTC")


def wave(end_open: pd.Timestamp, n: int, step_min: int, base: float, drift: float, amp: float, period: int,
         seed: int, end_price: float | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    times = [end_open - pd.Timedelta(minutes=step_min * (n - 1 - i)) for i in range(n)]
    rows, prev = [], base
    for i, t in enumerate(times):
        c = base + drift * i + amp * math.sin(2 * math.pi * i / period) + rng.normal(0, amp * 0.06)
        spread = amp * (0.25 + 0.25 * rng.random())
        rows.append((t, prev, max(prev, c) + spread * 0.5, min(prev, c) - spread * 0.5, c))
        prev = c
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    if end_price is not None:                       # shift so the series ends where we want it
        df[["open", "high", "low", "close"]] += end_price - df["close"].iloc[-1]
    return df


def crafted(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame([(ts(t), o, h, l, c) for t, o, h, l, c in rows], columns=["time", "open", "high", "low", "close"])


def build_buy_scenario(entry_close: float = 4103.3):
    """frames: completed candles per timeframe as of NOW. Levels are returned for assertions."""
    h1 = wave(ts("12:00"), 600, 60, 3000.0, 0.8, 10.0, 14, 1)

    m15_wave = wave(ts("12:30"), 600, 15, 4000.0, 0.18, 4.0, 20, 2)
    s_low = 4102.15                                       # last confirmed M15 swing low in this wave (asserted in tests)
    last_close = m15_wave["close"].iloc[-1]
    sweep = crafted([("12:45", last_close, last_close + 0.3, s_low - 1.0, s_low + 1.6)])   # wick below, close back above
    m15 = pd.concat([m15_wave, sweep], ignore_index=True)

    m5_hist = wave(ts("12:15"), 500, 5, 4090.0, 0.02, 1.2, 18, 3, end_price=4101.8)
    m5_tail = crafted([
        ("12:20", 4101.8, 4102.4, 4101.5, 4102.2), ("12:25", 4102.2, 4102.9, 4102.0, 4102.7),
        ("12:30", 4102.7, 4103.5, 4102.5, 4103.3), ("12:35", 4103.3, 4104.0, 4103.0, 4103.6),   # internal swing high 4104.0
        ("12:40", 4103.6, 4103.7, 4102.8, 4103.0), ("12:45", 4103.0, 4103.2, 4102.0, 4102.3),
        ("12:50", 4102.3, 4102.5, 4101.4, 4101.6), ("12:55", 4101.6, 4101.9, 4101.2, 4101.7),
        ("13:00", 4102.6, 4102.8, 4101.9, 4102.0),     # last bearish candle  -> entry zone [4101.9, 4102.8]
        ("13:05", 4102.0, 4104.9, 4101.95, 4104.6),    # displacement / MSS candle (body 88%)
    ])
    m5 = pd.concat([m5_hist, m5_tail], ignore_index=True)

    m1_hist = wave(ts("13:09"), 500, 1, 4095.0, 0.0, 0.4, 25, 4, end_price=4104.5)
    m1_tail = crafted([
        ("13:10", 4104.5, 4104.7, 4104.0, 4104.2), ("13:11", 4104.2, 4104.3, 4103.4, 4103.5),
        ("13:12", 4103.5, 4103.6, 4102.9, 4103.0), ("13:13", 4103.0, 4103.1, 4102.6, 4102.7),   # touches, no close above
        ("13:14", 4102.7, entry_close + 0.2, 4102.5, entry_close),                              # touches + closes above -> ENTRY
    ])
    m1 = pd.concat([m1_hist, m1_tail], ignore_index=True)
    return {"H1": h1, "M15": m15, "M5": m5, "M1": m1}


def to_payload(df: pd.DataFrame, symbol: str, interval: str) -> dict:
    return {"meta": {"symbol": symbol, "interval": interval}, "status": "ok",
            "values": [{"datetime": r.time.strftime("%Y-%m-%d %H:%M:%S"), "open": str(r.open), "high": str(r.high),
                        "low": str(r.low), "close": str(r.close)} for r in df.itertuples()]}
