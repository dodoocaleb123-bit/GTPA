"""GPTA backend API (blueprint section 21).

Built on Starlette (the layer FastAPI is built on) because FastAPI could not be installed in the build
sandbox; route handlers are plain functions and port 1:1 to FastAPI routers. Every /api route except
/api/health requires `Authorization: Bearer <GPTA_API_TOKEN>`. The API only *reads* decisions made by the
strategy/qualification engines (rule: the dashboard never decides) -- the sole writes are settings (versioned,
audited, guard-railed), broker quote ingestion, and backtest runs.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .backtesting.engine import run_backtest
from .broker.spread_engine import Quote
from .config import Cfg, TF_SECONDS, load_config
from .paper_trading.engine import PaperTrade, metrics, session_of

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

# Settings that must never be loosened through the API (blueprint section 26 engineering rules).
LOCKED = {
    ("execution", "mode"): lambda v: v == "paper",
    ("execution", "broker_execution_enabled"): lambda v: v is False,
    ("risk", "minimum_reward_risk"): lambda v: isinstance(v, (int, float)) and v >= 2.0,
    ("spread", "block_on_failure"): lambda v: v is True,
    ("news", "block_on_failure"): lambda v: v is True,
    ("spread", "enabled"): lambda v: v is True,
    ("news", "enabled"): lambda v: v is True,
    ("trend", "swing_left"): lambda v: isinstance(v, int) and v >= 1,
    ("trend", "swing_right"): lambda v: isinstance(v, int) and v >= 1,
    ("collection", "independent_provider_requests"): lambda v: v is True,
}


def _default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def _clean(o):
    """Strict JSON: NaN/inf become null (browsers' JSON.parse rejects NaN)."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)) and not np.isfinite(o):
        return None
    if isinstance(o, np.generic):
        return _clean(o.item())
    return o


def jr(obj, status: int = 200) -> Response:
    return Response(json.dumps(_clean(obj), default=_default), status_code=status, media_type="application/json")


def err(code: str, message: str, status: int) -> Response:
    return jr({"error": code, "message": message}, status)


class RateLimiter:
    def __init__(self, limit: int, per_seconds: float):
        self.limit, self.per, self.hits = limit, per_seconds, defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.per:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def create_app(rt, *, token: str | None, scheduler=None, allow_no_auth: bool = False) -> Starlette:
    if not token and not allow_no_auth:
        raise RuntimeError("GPTA_API_TOKEN is required (set GPTA_DEV_NO_AUTH=1 for local development only)")
    sensitive = RateLimiter(30, 60)
    app_state = {"rt": rt, "scheduler": scheduler}

    def authed(request: Request) -> bool:
        if allow_no_auth and not token:
            return True
        h = request.headers.get("authorization", "")
        given = h[7:] if h.lower().startswith("bearer ") else ""
        return bool(given) and hmac.compare_digest(given.encode(), (token or "").encode())

    def guard(fn, *, limited: bool = False):
        async def wrapped(request: Request):
            if not authed(request):
                return err("UNAUTHORIZED", "missing or invalid bearer token", 401)
            if limited and not sensitive.allow(request.client.host if request.client else "?"):
                return err("RATE_LIMITED", "too many requests", 429)
            try:
                res = fn(request)
                return await res if asyncio.iscoroutine(res) else res
            except ValueError as e:
                return err("BAD_REQUEST", str(e), 400)
            except Exception as e:  # never leak internals/secrets in an API response
                rt.store.log("ERROR", "api", f"{request.url.path}: {type(e).__name__}")
                return err("INTERNAL", "internal error", 500)
        return wrapped

    def qint(request: Request, name: str, default: int, lo: int = 1, hi: int = 5000) -> int:
        try:
            v = int(request.query_params.get(name, default))
        except ValueError:
            raise ValueError(f"{name} must be an integer")
        if not lo <= v <= hi:
            raise ValueError(f"{name} must be between {lo} and {hi}")
        return v

    def label() -> str:
        return "LIVE" if all(h["healthy"] for h in rt.health.values()) else "UNAVAILABLE"

    # ---- system ---------------------------------------------------------------------------------
    async def health(request):
        return jr({"status": "ok", "database": rt.store.health(), "time": datetime.now(timezone.utc).isoformat()})

    def system_status(request):
        st = rt.system_status()
        st["scheduler"] = app_state["scheduler"].status() if app_state["scheduler"] else {"running": False}
        st["strategy_version"] = rt.cfg.version_hash()
        return jr(st)

    def system_logs(request):
        return jr(rt.store.q("SELECT ts,level,source,message FROM system_logs ORDER BY id DESC LIMIT ?", (qint(request, "limit", 100, 1, 1000),)))

    # ---- market data ----------------------------------------------------------------------------
    def candles(request):
        tf = request.query_params.get("timeframe", request.query_params.get("tf", "M15")).upper()
        if tf not in TF_SECONDS:
            raise ValueError("timeframe must be one of M1,M5,M15,H1")
        df = rt.store.candles(rt.instrument, tf, qint(request, "limit", 300, 1, 2000))
        return jr({"timeframe": tf, "label": label() if rt.health[tf]["healthy"] else "UNAVAILABLE", "provider": "twelvedata",
                   "candles": [{"time": int(r.time.timestamp()), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                               for r in df.itertuples()]})

    def price(request):
        df = rt.store.candles(rt.instrument, "M1", 1)
        if df.empty:
            return jr({"instrument": rt.instrument, "price": None, "label": "UNAVAILABLE"})
        last = df.iloc[-1]
        age = (datetime.now(timezone.utc) - last["time"].to_pydatetime()).total_seconds() - 60
        return jr({"instrument": rt.instrument, "price": float(last["close"]), "candle_time": last["time"],
                   "age_seconds": max(age, 0), "label": "LIVE" if rt.health["M1"]["healthy"] and age < 180 else "DELAYED"})

    def timeframes(request):
        out = {}
        for tf in TF_SECONDS:
            r = rt.store.q("SELECT COUNT(*) n, MAX(ts) last FROM market_candles WHERE instrument=? AND timeframe=? AND completed=1", (rt.instrument, tf))[0]
            out[tf] = {"stored": r["n"], "last_completed": r["last"], **rt.health[tf]}
        return jr(out)

    def data_health(request):
        reqs = rt.store.q("SELECT timeframe, SUM(ok) ok, COUNT(*) n FROM data_requests WHERE ts > ? GROUP BY timeframe",
                          ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
        return jr({"label": label(), "timeframes": rt.health, "requests_last_hour": reqs,
                   "credentials": rt.creds.status() if rt.creds else [],
                   "baseline_requests_per_day": 4 * 60 * 24})

    # ---- broker / spread --------------------------------------------------------------------------
    def broker_quote(request):
        q = rt.spread.last
        return jr(q.as_dict(datetime.now(timezone.utc)) if q else {"connected": False, "label": "UNAVAILABLE"})

    def broker_status(request):
        q, now = rt.spread.last, datetime.now(timezone.utc)
        age = (now - q.tick_time).total_seconds() if q else None
        return jr({"connected": bool(q and q.connected and age is not None and age <= rt.cfg.spread.maximum_quote_age_seconds * 6),
                   "quote_age_seconds": age, "last_tick": q.tick_time if q else None, "server": q.server if q else None,
                   "symbol": q.symbol if q else None, "fresh": bool(age is not None and age <= rt.cfg.spread.maximum_quote_age_seconds)})

    async def ingest(request: Request):
        b = await request.json()
        try:
            tick = datetime.fromtimestamp(float(b["tick_time"]), timezone.utc) if isinstance(b["tick_time"], (int, float)) \
                else datetime.fromisoformat(b["tick_time"])
            q = Quote(str(b["symbol"]), float(b["bid"]), float(b["ask"]), tick, datetime.now(timezone.utc),
                      str(b.get("server", "")), bool(b.get("connected", True)))
        except (KeyError, ValueError, TypeError):
            raise ValueError("body needs symbol, bid, ask, tick_time")
        rt.ingest_quote(q)
        return jr({"accepted": True, "valid": q.valid})

    def spread_current(request):
        q = rt.spread.last
        return jr({"spread": q.spread if q and q.valid else None, **(q.as_dict(datetime.now(timezone.utc)) if q else {})})

    def spread_history(request):
        return jr(rt.store.q("SELECT ts,spread FROM spread_history ORDER BY id DESC LIMIT ?", (qint(request, "limit", 200, 1, 5000),))[::-1])

    # ---- news -------------------------------------------------------------------------------------
    def news_events(request):
        return jr([e.as_dict() for e in rt.news.events])

    def news_upcoming(request):
        now = datetime.now(timezone.utc)
        return jr([e.as_dict() for e in rt.news.events if e.ts_utc >= now][:20])

    def news_status(request):
        now = datetime.now(timezone.utc)
        c = rt.news.check(now)
        return jr({**rt.news.status(now), "check": c.as_dict() if hasattr(c, "as_dict") else str(c)})

    def news_blackouts(request):
        return jr(rt.news.blackouts())

    # ---- strategy ---------------------------------------------------------------------------------
    def strategy_status(request):
        return jr({"mode": rt.cfg.execution.mode, "strategy_version": rt.cfg.version_hash(), "last_cycle": rt.last_cycle,
                   "last_error": rt.last_error, "warmup": rt.last_trace.get("stages", {}).get("warmup"), "label": label()})

    def strategy_analysis(request):
        recent = rt.store.q("SELECT setup_id,ts,result,checks FROM qualification_results ORDER BY id DESC LIMIT 20")
        for r in recent:
            _flatten(r)
        return jr({"trace": rt.last_trace, "recent_decisions": recent, "label": label()})

    def strategy_zones(request):
        return jr(rt.last_trace.get("stages", {}).get("zones", []))

    def strategy_structure(request):
        st = rt.last_trace.get("stages", {})
        return jr({"trend": st.get("trend"), "alignment": st.get("alignment"), "sweeps": st.get("sweeps", []),
                   "setups": st.get("setups", [])})

    def strategy_versions(request):
        return jr(rt.store.q("SELECT version,created_at,note FROM strategy_versions ORDER BY created_at DESC"))

    # ---- signals ----------------------------------------------------------------------------------
    def _flatten(r: dict) -> None:
        payload = json.loads(r["checks"])
        r["checks"], r["rejection_codes"] = payload["checks"], payload["rejection_codes"]

    def _signal_rows(where: str = "", args=()):
        rows = rt.store.q(f"SELECT * FROM signals {where} ORDER BY created_at DESC", args)
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r.get("detail") else {}
        return rows

    def signals_list(request):
        return jr(_signal_rows())

    def signals_active(request):
        return jr(_signal_rows("WHERE state NOT IN ('REJECTED','INVALIDATED','EXPIRED','TP_HIT','SL_HIT','CLOSED_OTHER')"))

    def signals_history(request):
        q = request.query_params
        sql, args = [], []
        if q.get("direction"):
            sql.append("direction=?"); args.append(q["direction"].upper())
        if q.get("outcome"):
            sql.append("state=?"); args.append(q["outcome"].upper())
        if q.get("since"):
            sql.append("created_at>=?"); args.append(q["since"])
        signals = _signal_rows("WHERE " + " AND ".join(sql) if sql else "", tuple(args))
        setups = rt.store.q("SELECT s.setup_id,s.direction,s.created_at,s.status,q.checks FROM setups s "
                            "LEFT JOIN qualification_results q ON q.setup_id=s.setup_id ORDER BY s.created_at DESC LIMIT 500")
        reason = q.get("reason")
        out = []
        for s in setups:
            codes = json.loads(s["checks"])["rejection_codes"] if s["checks"] else []
            if s["status"] == "QUALIFIED" or (q.get("direction") and s["direction"] != q["direction"].upper()):
                continue
            if reason and not any(reason in c for c in codes):
                continue
            out.append({"setup_id": s["setup_id"], "direction": s["direction"], "created_at": s["created_at"],
                        "result": s["status"], "rejection_codes": codes})
        return jr({"signals": signals, "non_qualified_setups": out})

    def signal_one(request):
        sid = request.path_params["signal_id"]
        rows = _signal_rows("WHERE signal_id=?", (sid,))
        if not rows:
            return err("NOT_FOUND", "unknown signal", 404)
        ev = rt.store.q("SELECT ts,event,detail FROM signal_events WHERE signal_id=? ORDER BY id", (sid,))
        qual = rt.store.q("SELECT ts,result,checks FROM qualification_results WHERE setup_id=?", (rows[0]["setup_id"],))
        for r in qual:
            _flatten(r)
        trade = rt.store.q("SELECT * FROM paper_trades WHERE signal_id=?", (sid,))
        return jr({**rows[0], "events": ev, "qualification": qual, "paper_trade": trade[0] if trade else None})

    # ---- performance -------------------------------------------------------------------------------
    def _trades() -> list[PaperTrade]:
        out = []
        for r in rt.store.q("SELECT * FROM paper_trades ORDER BY entry_ts"):
            out.append(PaperTrade(r["signal_id"], r["direction"], r["proposed_entry"], r["sim_entry"], r["sl"], r["tp"], r["entry_ts"],
                                  r["exit_ts"], r["exit_price"], r["outcome"], r["r_multiple"], r["spread"], r["slippage"],
                                  session_of(pd.Timestamp(r["entry_ts"]))))
        return out

    def perf_summary(request):
        sig = rt.store.q("SELECT COUNT(*) n FROM signals")[0]["n"]
        entries = rt.store.q("SELECT COUNT(*) n FROM signal_events WHERE event='ENTRY_OBSERVED'")[0]["n"]
        return jr({**metrics(_trades()), "total_signals": sig, "entries_observed": entries})

    def perf_equity(request):
        eq, cum = [], 0.0
        for t in _trades():
            if t.outcome in ("WIN", "LOSS") and t.r_multiple is not None:
                cum += t.r_multiple
                eq.append({"time": t.exit_ts, "r": round(cum, 4)})
        return jr({"label": "PAPER/SIMULATED", "points": eq})

    def perf_trades(request):
        return jr({"label": "PAPER/SIMULATED", "trades": [t.as_dict() for t in _trades()]})

    def perf_sessions(request):
        return jr({"label": "PAPER/SIMULATED", "by_session": metrics(_trades()).get("by_session", {})})

    # ---- settings (versioned + audited) ---------------------------------------------------------------
    def settings_get(request):
        return jr({"version": rt.cfg.version_hash(), "config": rt.cfg.raw()})

    async def settings_put(request: Request):
        body = await request.json()
        changes, note = body.get("changes"), str(body.get("note", ""))[:500]
        if not isinstance(changes, dict) or not changes:
            raise ValueError("body must be {changes: {section: {key: value}}, note?: string}")
        raw = rt.cfg.raw()
        applied = []

        def walk(base, upd, path=()):
            for k, v in upd.items():
                if k not in base:
                    raise ValueError(f"unknown setting {'.'.join(path + (k,))}")
                if isinstance(base[k], dict):
                    if not isinstance(v, dict):
                        raise ValueError(f"{'.'.join(path + (k,))} must be an object")
                    walk(base[k], v, path + (k,))
                else:
                    old = base[k]
                    num = (int, float)
                    if isinstance(old, bool) != isinstance(v, bool) or (isinstance(old, num) and not isinstance(old, bool) and not isinstance(v, num)) \
                            or (isinstance(old, str) and not isinstance(v, str)) or (isinstance(old, list) and not isinstance(v, list)):
                        raise ValueError(f"wrong type for {'.'.join(path + (k,))}")
                    rule = LOCKED.get(path + (k,))
                    if rule and not rule(v):
                        raise ValueError(f"{'.'.join(path + (k,))} is locked by GPTA engineering rules")
                    base[k] = v
                    applied.append({"path": ".".join(path + (k,)), "old": old, "new": v})
        walk(raw, changes)
        new = Cfg(raw)
        ver = new.version_hash()
        rt.store.save_strategy_version(ver, raw, note)
        rt.store.audit("api", "settings_change", {"from": rt.cfg.version_hash(), "to": ver, "changes": applied, "note": note})
        rt.apply_config(new)
        return jr({"version": ver, "applied": applied})

    # ---- backtesting -------------------------------------------------------------------------------------
    async def backtest_run(request: Request):
        body = await request.json() if await request.body() else {}
        bars = {tf: qint_val(body.get("bars", {}).get(tf, 5000), 100, 20000) for tf in TF_SECONDS}
        frames = {tf: rt.store.candles(rt.instrument, tf, bars[tf]) for tf in TF_SECONDS}
        if any(len(f) < 50 for f in frames.values()):
            raise ValueError("not enough stored candles for all four timeframes; load history first")
        res = run_backtest(frames, rt.cfg, assumed_spread=float(body.get("assumed_spread", 0.30)),
                           assume_no_news=bool(body.get("assume_no_news", False)))
        rt.store.x("INSERT OR REPLACE INTO backtest_runs VALUES(?,?,?,?)", (res["run_id"], datetime.now(timezone.utc).isoformat(),
                                                                          json.dumps(res["config"]), json.dumps({k: res[k] for k in ("metrics", "decision_counts", "trades")}, default=_default)))
        return jr({"run_id": res["run_id"], "label": "SIMULATED", "config": res["config"], "decision_counts": res["decision_counts"], "metrics": res["metrics"]})

    def qint_val(v, lo, hi):
        v = int(v)
        if not lo <= v <= hi:
            raise ValueError("bars out of range")
        return v

    def backtest_runs(request):
        return jr(rt.store.q("SELECT run_id,created_at,config FROM backtest_runs ORDER BY created_at DESC"))

    def backtest_one(request):
        r = rt.store.q("SELECT * FROM backtest_runs WHERE run_id=?", (request.path_params["run_id"],))
        if not r:
            return err("NOT_FOUND", "unknown run", 404)
        return jr({"run_id": r[0]["run_id"], "label": "SIMULATED", "config": json.loads(r[0]["config"]), **json.loads(r[0]["metrics"])})

    # ---- websocket (push of system status) -------------------------------------------------------------------
    async def ws(websocket: WebSocket):
        tok = websocket.query_params.get("token", "")
        if not (allow_no_auth and not token) and not hmac.compare_digest(tok.encode(), (token or "").encode()):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_text(json.dumps({"type": "status", "label": label(), "last_cycle": rt.last_cycle,
                                                      "active_signals": len(rt.signals.active())}, default=_default))
                await asyncio.sleep(5)
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def index(request):
        return FileResponse(FRONTEND / "index.html")

    G = guard
    routes = [
        Route("/", index),
        Route("/api/health", health),
        Route("/api/system/status", G(system_status)), Route("/api/system/logs", G(system_logs)),
        Route("/api/market/price", G(price)), Route("/api/market/candles", G(candles)),
        Route("/api/market/timeframes", G(timeframes)), Route("/api/market/data-health", G(data_health)),
        Route("/api/broker/status", G(broker_status)), Route("/api/broker/quote", G(broker_quote)),
        Route("/api/broker/quote", G(ingest, limited=False), methods=["POST"]),
        Route("/api/spread/current", G(spread_current)), Route("/api/spread/history", G(spread_history)),
        Route("/api/news/events", G(news_events)), Route("/api/news/upcoming", G(news_upcoming)),
        Route("/api/news/status", G(news_status)), Route("/api/news/blackouts", G(news_blackouts)),
        Route("/api/strategy/status", G(strategy_status)), Route("/api/strategy/analysis", G(strategy_analysis)),
        Route("/api/strategy/zones", G(strategy_zones)), Route("/api/strategy/structure", G(strategy_structure)),
        Route("/api/strategy/versions", G(strategy_versions)),
        Route("/api/signals", G(signals_list)), Route("/api/signals/active", G(signals_active)),
        Route("/api/signals/history", G(signals_history)), Route("/api/signals/{signal_id}", G(signal_one)),
        Route("/api/performance/summary", G(perf_summary)), Route("/api/performance/equity", G(perf_equity)),
        Route("/api/performance/trades", G(perf_trades)), Route("/api/performance/sessions", G(perf_sessions)),
        Route("/api/settings", G(settings_get)), Route("/api/settings", G(settings_put, limited=True), methods=["PUT"]),
        Route("/api/backtest/run", G(backtest_run, limited=True), methods=["POST"]),
        Route("/api/backtest/runs", G(backtest_runs)), Route("/api/backtest/{run_id}", G(backtest_one)),
        WebSocketRoute("/ws", ws),
    ]
    app = Starlette(routes=routes)
    origins = [o for o in os.environ.get("GPTA_CORS_ORIGINS", "").split(",") if o]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])
    app.state.rt = rt
    return app


# ---- production wiring ----------------------------------------------------------------------------------
def build_from_env():
    """Assemble the real runtime from environment variables (see .env.example)."""
    from .broker.spread_engine import SpreadEngine
    from .data.credential_manager import CredentialManager
    from .data.data_scheduler import Scheduler
    from .data.twelve_data import TwelveDataProvider
    from .database.store import Store
    from .news.calendar_service import NewsGate
    from .news.showmytrades_client import ShowMyTradesClient
    from .runtime import GPTARuntime
    from .signals.signal_service import SignalService
    from .telegram.bot import TelegramBot
    from .telegram.notifier import Notifier, make_http_sender

    store = Store(os.environ.get("GPTA_DB", "gpta.db"))
    cfg = load_config()
    latest = store.q("SELECT version,config FROM strategy_versions ORDER BY created_at DESC LIMIT 1")
    if latest:                                   # the newest saved version is the active one (survives restarts)
        cfg = Cfg(json.loads(latest[0]["config"]))
    else:
        store.save_strategy_version(cfg.version_hash(), cfg.raw(), "initial from strategy.yaml")
    creds = CredentialManager.from_env()
    notifier = None
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if tok and chat:
        notifier = Notifier(store, make_http_sender(tok), chat)
    signals = SignalService(store, notifier, cfg, cfg.version_hash())
    rt = GPTARuntime(cfg, store, TwelveDataProvider(creds, cfg.instrument), SpreadEngine(cfg.spread),
                     NewsGate(cfg.news), signals, creds)
    bot = None
    if tok and chat:
        from .paper_trading.engine import metrics  # noqa: F401  (command ctx uses rt read-models)
        bot = TelegramBot(tok, chat, lambda: _bot_ctx(rt))
    news_client = ShowMyTradesClient() if os.environ.get("SHOWMYTRADES_URL") else None
    sched = Scheduler(rt, news_client, bot=bot)
    return rt, sched


def _bot_ctx(rt) -> dict:
    st = rt.system_status()
    q = rt.spread.last
    px = rt.store.candles(rt.instrument, "M1", 1)
    return {"health": {"data": st["data_label"], "database": st["database"], "last_cycle": st["last_cycle"], "mode": st["mode"]},
            "active": [s.as_dict() | {"signal_id": s.signal_id} for s in rt.signals.active()],
            "market": {"price": None if px.empty else float(px["close"].iloc[-1]), "bid": q.bid if q else None,
                       "ask": q.ask if q else None, "label": st["data_label"]} if not px.empty else {},
            "performance": rt.performance(),
            "history": rt.store.q("SELECT signal_id,direction,state FROM signals ORDER BY created_at DESC LIMIT 10")}


def app_factory() -> Starlette:                    # uvicorn app.main:app_factory --factory
    rt, sched = build_from_env()
    token = os.environ.get("GPTA_API_TOKEN")
    app = create_app(rt, token=token, scheduler=sched, allow_no_auth=os.environ.get("GPTA_DEV_NO_AUTH") == "1")
    if os.environ.get("GPTA_AUTORUN", "1") == "1":
        sched.start()
    return app
