"""GPTA runtime: one `tick()` per minute wires collection -> storage -> strategy -> qualification -> signals.
Every external dependency is injected, so the whole loop is testable with fakes (tests/test_runtime.py)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pandas as pd

from .broker.spread_engine import Quote, SpreadEngine
from .config import TF_SECONDS, Cfg
from .data.candle_validator import to_frame
from .data.twelve_data import CandleProvider, collect_all
from .database.store import Store
from .news.calendar_service import NewsGate
from .paper_trading.engine import PaperTrade, metrics, session_of
from .signals.signal_service import SignalService
from .strategy.strategy_orchestrator import StrategyOrchestrator


class GPTARuntime:
    def __init__(self, cfg: Cfg, store: Store, provider: CandleProvider, spread: SpreadEngine, news: NewsGate,
                 signals: SignalService, creds=None):
        self.cfg, self.store, self.provider, self.spread, self.news, self.signals, self.creds = \
            cfg, store, provider, spread, news, signals, creds
        self.orch = StrategyOrchestrator(cfg)
        self.instrument = cfg.instrument
        self.health = {tf: {"healthy": False, "last_ok": None, "issues": [], "error": None} for tf in TF_SECONDS}
        self.last_trace: dict = {}
        self.last_cycle: str | None = None
        self.last_error: str | None = None
        self._last_m1: pd.Timestamp | None = None
        self._lock = threading.Lock()

    # -- collection ----------------------------------------------------------------------------
    def collect(self, now: datetime, sizes: dict[str, int] | None = None) -> None:
        sizes = sizes or {tf: self.cfg.collection.history_bars[tf] if self._need_history(tf) else 5 for tf in TF_SECONDS}
        results = collect_all(self.provider, self.instrument, sizes, now)
        for tf, (rep, err) in results.items():
            h = self.health[tf]
            if rep is None:
                h.update(healthy=False, error=err)
                self.store.x("INSERT INTO data_requests(ts,timeframe,ok,error,issues) VALUES(?,?,?,?,?)",
                             (now.isoformat(), tf, 0, err, "[]"))
                continue
            corr = self.store.upsert_candles(self.instrument, tf, rep.accepted)
            for c in corr:
                self.store.log("WARN", "data", f"historical correction ignored {c}")
            h.update(healthy=rep.healthy and not corr, error=None, issues=rep.issues[-5:])
            if rep.healthy:
                h["last_ok"] = now.isoformat()
            self.store.x("INSERT INTO data_requests(ts,timeframe,ok,error,issues) VALUES(?,?,?,?,?)",
                         (now.isoformat(), tf, int(rep.healthy), None, str(rep.issues[-5:])))

    def _need_history(self, tf: str) -> bool:
        have = self.store.q("SELECT COUNT(*) c FROM market_candles WHERE instrument=? AND timeframe=?", (self.instrument, tf))[0]["c"]
        return have < self.cfg.collection.history_bars[tf] * 0.9

    # -- one strategy cycle -----------------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            try:
                self.collect(now)
                return self._analyse(pd.Timestamp(now))
            except Exception as e:  # strategy exception: reject the cycle, log, keep running
                self.last_error = f"{type(e).__name__}: {e}"
                self.store.log("ERROR", "runtime", self.last_error)
                return {"error": self.last_error}

    def _analyse(self, now: pd.Timestamp) -> dict:
        bars = self.cfg.collection.history_bars
        data = {tf: self.store.candles(self.instrument, tf, bars[tf]) for tf in TF_SECONDS}
        healthy = {tf: self.health[tf]["healthy"] for tf in TF_SECONDS}

        # lifecycle first: feed new completed M1 candles to live signals
        m1 = data["M1"]
        new = m1 if self._last_m1 is None else m1[m1["time"] > self._last_m1]
        for _, c in new.iterrows():
            self.signals.on_m1_candle(c, lambda t0: m1[(m1["time"] >= t0) & (m1["time"] < t0 + pd.Timedelta(minutes=1))],
                                      spread=self.spread.last.spread if self.spread.last else 0.0)
        if not m1.empty:
            self._last_m1 = m1["time"].iloc[-1]
        self.signals.retry_unpublished(now)

        q = self.spread.last
        res = self.orch.evaluate(
            data, now,
            spread_check=lambda sd: self.spread.check(now.to_pydatetime(), sd),
            news_check=self.news.check(now.to_pydatetime()), data_health=healthy,
            seen=self.signals.guard.setups,
            active_signals=[{"direction": s.direction} for s in self.signals.active()],
            spread_value=q.spread if q and q.valid else 0.0)
        for d in res.decisions:
            self.signals.record_decision(d, now)
        self.last_trace, self.last_cycle = res.trace, now.isoformat()
        return {"decisions": [d.as_dict() for d in res.decisions], "trace": res.trace}

    # -- broker quotes (pushed by the Windows MT5 bridge) ------------------------------------------
    def ingest_quote(self, q: Quote) -> None:
        self.spread.observe(q)
        self.store.x("INSERT INTO broker_quotes(symbol,bid,ask,tick_ts,server,retrieved_at,valid) VALUES(?,?,?,?,?,?,?)",
                     (q.symbol, q.bid, q.ask, q.tick_time.isoformat(), q.server, q.retrieved_at.isoformat(), int(q.valid)))
        if q.valid and q.connected:
            self.store.x("INSERT INTO spread_history(ts,spread) VALUES(?,?)", (q.tick_time.isoformat(), q.spread))

    # -- versioned configuration changes ------------------------------------------------------------
    def apply_config(self, cfg: Cfg) -> None:
        with self._lock:
            self.cfg = cfg
            self.orch = StrategyOrchestrator(cfg)
            self.signals.cfg = cfg
            self.signals.version = cfg.version_hash()
            self.spread.cfg = cfg.spread
            self.news.cfg = cfg.news

    # -- read models for the API ------------------------------------------------------------------
    def system_status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        q = self.spread.last
        return {"time": now.isoformat(), "database": self.store.health(), "last_cycle": self.last_cycle,
                "last_error": self.last_error, "data_health": self.health,
                "broker": q.as_dict(now) if q else {"connected": False},
                "news": self.news.status(now), "credentials": self.creds.status() if self.creds else [],
                "telegram_ok": self.signals.notifier.delivery_ok() if self.signals.notifier else None,
                "mode": self.cfg.execution.mode, "broker_execution_enabled": self.cfg.execution.broker_execution_enabled,
                "data_label": self.data_label(now)}

    def data_label(self, now: datetime | None = None) -> str:
        """LIVE only if every feed is healthy AND the last strategy cycle is recent; healthy-but-old is DELAYED."""
        now = now or datetime.now(timezone.utc)
        if not all(h["healthy"] for h in self.health.values()):
            return "UNAVAILABLE"
        if not self.last_cycle or (now - datetime.fromisoformat(self.last_cycle)).total_seconds() > 180:
            return "DELAYED"
        return "LIVE"

    def performance(self) -> dict:
        rows = self.store.q("SELECT * FROM paper_trades")
        trades = [PaperTrade(r["signal_id"], r["direction"], r["proposed_entry"], r["sim_entry"], r["sl"], r["tp"], r["entry_ts"],
                             r["exit_ts"], r["exit_price"], r["outcome"], r["r_multiple"], r["spread"], r["slippage"],
                             session_of(pd.Timestamp(r["entry_ts"]))) for r in rows]
        return metrics(trades)
