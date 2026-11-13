"""Backtester that replays history through the SAME StrategyOrchestrator and lifecycle used live
(blueprint 18). At each M1 close the strategy only sees candles whose close time <= that instant, so
lookahead is structurally impossible. Spread is an explicit assumption; news is an explicit flag."""
from __future__ import annotations

import hashlib
import json

import pandas as pd

from ..config import TF_SECONDS, Cfg
from ..paper_trading.engine import PaperTrade, from_signal, metrics
from ..qualification.types import ok, unavailable
from ..signals.lifecycle import TERMINAL, Signal, advance
from ..strategy.strategy_orchestrator import StrategyOrchestrator


def run_backtest(frames: dict[str, pd.DataFrame], cfg: Cfg, *, assumed_spread: float = 0.30,
                 assume_no_news: bool = False, start_index: int | None = None) -> dict:
    """frames: completed candles per timeframe ('time' = open time, UTC). Returns metrics + trades + decisions.
    With assume_no_news=False the news check is UNAVAILABLE, so (correctly) nothing can qualify;
    pass True only for strategy-mechanics testing and the report will say so."""
    orch = StrategyOrchestrator(cfg)
    bars = cfg.collection.history_bars
    closes = {tf: (frames[tf]["time"] + pd.Timedelta(seconds=TF_SECONDS[tf])).dt.tz_convert("UTC").dt.tz_localize(None).to_numpy() for tf in TF_SECONDS}
    m1 = frames["M1"]
    seen: set[str] = set()
    sigs: list[Signal] = []
    trades: list[PaperTrade] = []
    decisions: list[dict] = []
    counts: dict[str, int] = {}
    news = ok("valid_news", simulated=True) if assume_no_news else unavailable("valid_news", "NO_CALENDAR_IN_BACKTEST")
    start = start_index if start_index is not None else min(len(m1) - 1, 600)

    for i in range(start, len(m1)):
        t = m1["time"].iloc[i] + pd.Timedelta(seconds=60)           # this M1 candle just completed
        for s in sigs:                                              # lifecycle first (uses only candle i)
            if s.state not in TERMINAL:
                advance(s, m1.iloc[i], lambda t0: _m1_inside(m1, t0))
                if s.state in ("TP_HIT", "SL_HIT", "CLOSED_OTHER"):
                    pt = from_signal(s, assumed_spread, cfg.paper.slippage)
                    if pt:
                        trades.append(pt)
        view = {}
        for tf in TF_SECONDS:
            n = int(closes[tf].searchsorted(t.tz_convert("UTC").tz_localize(None).to_datetime64(), side="right"))
            view[tf] = frames[tf].iloc[max(0, n - bars[tf]):n]
        res = orch.evaluate(view, t, spread_check=lambda sd: _spread(cfg, assumed_spread, sd), news_check=news, seen=seen,
                            active_signals=[{"direction": s.direction} for s in sigs if s.state not in TERMINAL],
                            spread_value=assumed_spread)
        for d in res.decisions:
            if not d.terminal:
                continue
            seen.add(d.setup_id)
            counts[d.result] = counts.get(d.result, 0) + 1
            decisions.append({"time": t.isoformat(), **d.as_dict()})
            if d.result == "QUALIFIED" and d.signal:
                p = d.signal
                s = Signal(f"BT-{len(sigs)+1:04d}", d.setup_id, p["direction"], p["entry"], p["entry_low"], p["entry_high"],
                           p["sl"], p["tp"], p["rr"], pd.Timestamp(p["expiry"]), t)
                s.move("PUBLISHED", t)
                s.move("WAITING_FOR_ENTRY", t)
                sigs.append(s)

    run_cfg = {"strategy_version": cfg.version_hash(), "assumed_spread": assumed_spread, "slippage": cfg.paper.slippage,
               "assume_no_news": assume_no_news, "label": "SIMULATED"}
    return {"run_id": hashlib.sha1(json.dumps(run_cfg, sort_keys=True).encode()).hexdigest()[:10], "config": run_cfg,
            "decision_counts": counts, "metrics": metrics(trades), "trades": [t.as_dict() for t in trades],
            "decisions": decisions}


def _m1_inside(m1: pd.DataFrame, t0: pd.Timestamp):
    return m1[(m1["time"] >= t0) & (m1["time"] < t0 + pd.Timedelta(minutes=1))]


def _spread(cfg, spread: float, stop_dist: float):
    from ..qualification.types import fail
    if spread > cfg.spread.maximum_stop_distance_ratio * stop_dist:
        return fail("valid_spread", "SPREAD_EXCEEDS_STOP_RATIO", spread=spread, simulated=True)
    return ok("valid_spread", spread=spread, simulated=True)


def split_out_of_sample(frames: dict[str, pd.DataFrame], fraction: float = 0.7):
    """Chronological split on M1 time; every timeframe is cut at the same instant."""
    m1 = frames["M1"]
    cut = m1["time"].iloc[int(len(m1) * fraction)]
    a = {tf: df[df["time"] < cut].reset_index(drop=True) for tf, df in frames.items()}
    b = {tf: df[df["time"] >= cut].reset_index(drop=True) for tf, df in frames.items()}
    return a, b, cut
