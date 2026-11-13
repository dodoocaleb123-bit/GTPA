"""End-to-end strategy flow on the synthetic BUY scenario + lifecycle, signals, Telegram, backtest, runtime."""
import pandas as pd
import pytest
from datetime import datetime, timezone

from app.config import load_config
from app.qualification.types import ok, fail, unavailable
from app.strategy.strategy_orchestrator import StrategyOrchestrator
from app.signals.lifecycle import Signal, advance, IllegalTransition, DuplicateGuard
from app.signals.signal_service import SignalService
from app.telegram.notifier import Notifier, format_signal, handle_command
from app.database.store import Store
from app.paper_trading.engine import from_signal, metrics
from app.backtesting.engine import run_backtest, split_out_of_sample
from synth import NOW, build_buy_scenario, to_payload, ts

# the textbook sweep candle spikes ATR; relax the proposed 10/90 band only for the happy-path fixture
WIDE = {"volatility": {"lower_percentile": 1, "upper_percentile": 99},
        "collection": {"history_bars": {"M1": 600, "M5": 600, "M15": 700, "H1": 700}}}   # keep the whole fixture in view
SP = lambda sd: ok("valid_spread")
NEWS = ok("valid_news")


def run(frames, cfg=None, now=NOW, spread=SP, news=NEWS, **kw):
    cfg = cfg or load_config(overrides=WIDE)
    return cfg, StrategyOrchestrator(cfg).evaluate(frames, now, spread_check=spread, news_check=news, spread_value=0.3, **kw)


def test_full_buy_setup_qualifies_with_complete_audit_trail():
    cfg, r = run(build_buy_scenario())
    d, = r.decisions
    assert d.result == "QUALIFIED" and d.qualification.rejection_codes == []
    s = d.signal
    assert s["direction"] == "BUY" and s["rr"] >= 2.0
    assert s["sl"] < s["entry"] < s["tp"]
    assert (s["entry_low"], s["entry_high"]) == (4101.9, 4102.8)          # last bearish M5 candle before displacement
    assert s["tp"] == pytest.approx(4109.9497, abs=1e-3)                  # nearest untouched M15 swing high, not stretched
    assert len(d.qualification.checks) == 16
    assert r.trace["stages"]["trend"]["H1"]["state"] == "BULLISH"


def test_default_volatility_band_rejects_sweep_spike():
    _, r = run(build_buy_scenario(), load_config())
    assert r.decisions[0].result == "REJECTED"
    assert "valid_volatility:VOLATILITY_TOO_HIGH" in r.decisions[0].qualification.rejection_codes


def test_news_or_spread_unavailable_blocks_signal():
    _, r = run(build_buy_scenario(), news=unavailable("valid_news", "CALENDAR_UNRELIABLE"))
    assert r.decisions[0].result == "DATA_UNAVAILABLE" and r.decisions[0].signal is None
    _, r = run(build_buy_scenario(), spread=lambda sd: unavailable("valid_spread", "MT5_UNAVAILABLE"))
    assert r.decisions[0].signal is None
    _, r = run(build_buy_scenario(), news=fail("valid_news", "NEWS_BLACKOUT"))
    assert r.decisions[0].result == "REJECTED"


def test_rr_below_two_is_rejected_not_adjusted():
    frames = build_buy_scenario(entry_close=4105.0)       # entry too far up -> R < 2 against the fixed TP
    cfg, r = run(frames)
    d = r.decisions[0]
    assert d.result == "REJECTED" and "minimum_reward_risk:RR_BELOW_MINIMUM" in d.qualification.rejection_codes
    assert d.detail["tp"] == pytest.approx(4109.9497, abs=1e-3)          # TP untouched by the failure


def test_unhealthy_timeframe_blocks():
    health = {"M1": True, "M5": False, "M15": True, "H1": True}
    _, r = run(build_buy_scenario(), data_health=health)
    assert r.decisions[0].result == "DATA_UNAVAILABLE" and r.decisions[0].signal is None


def test_stale_entry_is_not_published():
    _, r = run(build_buy_scenario(), now=NOW + pd.Timedelta(minutes=10))
    assert all(d.signal is None for d in r.decisions)


def test_expired_after_60_minutes_and_duplicate_not_reevaluated():
    cfg, r = run(build_buy_scenario())
    sid = r.decisions[0].setup_id
    _, r2 = run(build_buy_scenario(), seen={sid})
    assert r2.decisions == []                              # same setup is never evaluated twice


def test_target_reached_before_retest_cancels():
    f = build_buy_scenario()
    f["M1"] = f["M1"].copy()
    f["M1"].loc[f["M1"]["time"] == ts("13:11"), "high"] = 4110.5      # TP touched before the retest
    _, r = run(f)
    d = r.decisions[0]
    assert d.result == "INVALIDATED" and d.signal is None and d.detail["reason"] == "TARGET_REACHED_BEFORE_RETEST"


def test_close_below_sweep_low_invalidates():
    f = build_buy_scenario()
    f["M1"] = f["M1"].copy()
    f["M1"].loc[f["M1"]["time"] == ts("13:11"), ["low", "close"]] = [4100.0, 4100.5]
    _, r = run(f)
    assert r.decisions[0].result == "INVALIDATED" and r.decisions[0].signal is None


def test_ema_disagreement_blocks():
    f = build_buy_scenario()
    f["H1"] = f["H1"].copy()
    cfg = load_config(overrides={**WIDE, "trend": {"ema_fast": 400, "ema_slow": 50}})   # forces EMA 'bearish'
    _, r = run(f, cfg)
    assert r.decisions and r.decisions[0].signal is None


def test_no_future_candles_used_for_a_decision():
    """Appending candles after NOW must not change what was decided at NOW once they are cut off."""
    f = build_buy_scenario()
    extra = {tf: pd.concat([df, df.tail(1).assign(time=df["time"].iloc[-1] + (df["time"].iloc[-1] - df["time"].iloc[-2]) * 3)])
             for tf, df in f.items()}
    cut = {tf: df[df["time"] <= f[tf]["time"].iloc[-1]] for tf, df in extra.items()}
    _, a = run(f)
    _, b = run(cut)
    assert a.decisions[0].signal == b.decisions[0].signal


# ---- lifecycle ------------------------------------------------------------------------------
def mk_signal(direction="BUY"):
    return Signal("T-1", "S-1", direction, 100.0, 99.5, 100.5, 98.0, 106.0, 3.0, pd.Timestamp("2026-03-26 14:15", tz="UTC"),
                  pd.Timestamp("2026-03-26 13:15", tz="UTC"), state="WAITING_FOR_ENTRY")


def c(minute, o, h, l, cl):
    return pd.Series({"time": pd.Timestamp(f"2026-03-26 13:{minute:02d}", tz="UTC"), "open": o, "high": h, "low": l, "close": cl})


def test_lifecycle_entry_then_tp_and_no_reactivation():
    s = mk_signal()
    assert advance(s, c(15, 100.2, 100.6, 99.9, 100.4)) == "ENTRY_OBSERVED" and s.state == "ENTRY_OBSERVED"
    assert advance(s, c(16, 100.4, 106.2, 100.3, 106.0)) == "TP_HIT" and s.exit_price == 106.0
    with pytest.raises(IllegalTransition):
        s.move("WAITING_FOR_ENTRY", pd.Timestamp.now(tz="UTC"))
    assert advance(s, c(17, 1, 2, 0, 1)) is None


def test_ambiguous_candle_never_assumed_profitable():
    s = mk_signal()
    advance(s, c(15, 100.2, 100.6, 99.9, 100.4))
    assert advance(s, c(16, 100.4, 107.0, 97.0, 100.0)) == "CLOSED_OTHER"          # touches both TP and SL
    assert s.outcome_note == "AMBIGUOUS_TP_SL_SAME_CANDLE" and s.exit_price is None
    assert from_signal(s, 0.3, 0.05).outcome == "AMBIGUOUS"


def test_tp_before_entry_expires_and_expiry_by_time():
    s = mk_signal()
    assert advance(s, c(15, 103.0, 106.5, 102.9, 106.2)) == "EXPIRED"
    s2 = mk_signal()
    s2.expiry = pd.Timestamp("2026-03-26 13:20", tz="UTC")
    assert advance(s2, c(20, 103, 103.5, 102.5, 103.0)) == "EXPIRED"                 # entry not allowed after expiry


def test_sl_close_invalidates_waiting_signal():
    s = mk_signal()
    assert advance(s, c(15, 99.0, 99.2, 97.0, 97.5)) == "INVALIDATED"


def test_duplicate_guard():
    g = DuplicateGuard()
    assert g.new_setup("a") and not g.new_setup("a")
    assert g.new_event("s", "TP_HIT") and not g.new_event("s", "TP_HIT")


# ---- paper trading / metrics ------------------------------------------------------------------
def test_paper_trade_costs_and_metrics():
    s = mk_signal()
    advance(s, c(15, 100.2, 100.6, 99.9, 100.4))
    advance(s, c(16, 100.4, 106.2, 100.3, 106.0))
    t = from_signal(s, 0.3, 0.05)
    assert t.outcome == "WIN" and 0 < t.r_multiple < 3.0                 # costs reduce the nominal 3R
    sl = mk_signal(); advance(sl, c(15, 100.2, 100.6, 99.9, 100.4)); advance(sl, c(16, 100.4, 100.5, 97.0, 97.5))
    tl = from_signal(sl, 0.3, 0.05)
    m = metrics([t, tl])
    assert tl.outcome == "LOSS" and tl.r_multiple < -1.0 and m["completed"] == 2 and "PAPER" in m["label"]
    assert m["max_consecutive_losses"] == 1 and m["win_rate"] == 0.5


# ---- Telegram ---------------------------------------------------------------------------------
def test_notifier_dedupes_retries_and_tracks_failures():
    store, sent, boom = Store(), [], [2]
    def sender(chat, text):
        if boom[0] > 0:
            boom[0] -= 1
            raise ConnectionError("x")
        sent.append(text); return 77
    n = Notifier(store, sender, "chat", max_attempts=3, sleep=lambda s: None)
    assert n.send("k1", "TRADE SIGNAL", "hello") and n.send("k1", "TRADE SIGNAL", "hello")
    assert len(sent) == 1                                                  # retried twice, delivered once, never duplicated
    row = store.q("SELECT status,message_id,attempts FROM telegram_messages")[0]
    assert row["status"] == "SENT" and row["message_id"] == "77" and row["attempts"] == 3
    dead = Notifier(store, lambda c, t: (_ for _ in ()).throw(ConnectionError()), "chat", 2, lambda s: None)
    assert not dead.send("k2", "X", "t") and not dead.delivery_ok()


def test_signal_format_and_commands():
    _, r = run(build_buy_scenario())
    msg = format_signal({**r.decisions[0].signal, "signal_id": "GPTA-XAU-0001"})
    assert "BUY XAU/USD" in msg and "4,101.90 - 4,102.80" in msg and "does not confirm broker execution" in msg.replace("\n", " ")
    assert "/signals" in handle_command("/help", {}) and handle_command("/signals", {}) == "No active signals."


# ---- signal service: no publication unless qualified; failed Telegram preserved --------------------
def test_signal_service_publishes_once_and_preserves_on_telegram_failure():
    cfg = load_config(overrides=WIDE)
    _, r = run(build_buy_scenario(), cfg)
    d = r.decisions[0]
    store, ok_send = Store(), [False]
    def sender(chat, text):
        if not ok_send[0]:
            raise ConnectionError()
        return 1
    svc = SignalService(store, Notifier(store, sender, "c", 1, lambda s: None), cfg, cfg.version_hash())
    sig = svc.record_decision(d, NOW)
    assert sig.state == "QUALIFIED"                                        # preserved, not PUBLISHED
    assert svc.record_decision(d, NOW) is None                              # duplicate blocked
    ok_send[0] = True
    svc.retry_unpublished(NOW)
    assert sig.state == "WAITING_FOR_ENTRY"
    assert store.q("SELECT COUNT(*) c FROM signals")[0]["c"] == 1
    # a REJECTED decision is stored for audit but never becomes a signal
    svc2 = SignalService(Store(), None, cfg, "v")
    _, r2 = run(build_buy_scenario(), load_config())
    assert svc2.record_decision(r2.decisions[0], NOW) is None
    assert svc2.store.q("SELECT COUNT(*) c FROM signals")[0]["c"] == 0
    assert svc2.store.q("SELECT result FROM qualification_results")[0]["result"] == "REJECTED"


def test_signal_service_blocks_sub_minimum_rr():
    cfg = load_config(overrides=WIDE)
    _, r = run(build_buy_scenario(), cfg)
    d = r.decisions[0]
    d.signal["rr"] = 1.99
    svc = SignalService(Store(), None, cfg, "v")
    assert svc.record_decision(d, NOW) is None


# ---- runtime with fakes -------------------------------------------------------------------------
class FakeProvider:
    def __init__(self, frames, fail=()):
        self.frames, self.fail = frames, set(fail)
    def fetch(self, tf, outputsize, now):
        if tf in self.fail:
            return None, "PROVIDER_500"
        iv = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "1h"}[tf]
        df = self.frames[tf]
        step = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}[tf]
        nxt = df["time"].iloc[-1] + pd.Timedelta(minutes=step)
        # A provider only returns a currently-forming candle if that slot has not closed yet at `now`;
        # padding a slot that is already in the past would fabricate a "completed" candle.
        if nxt + pd.Timedelta(minutes=step) > pd.Timestamp(now):
            df = pd.concat([df, df.tail(1).assign(time=nxt)])
        return to_payload(df.tail(outputsize), "XAU/USD", iv), None


def build_runtime(frames, fail=(), cfg=None, news_ok=True):
    from app.runtime import GPTARuntime
    from app.broker.spread_engine import SpreadEngine, Quote
    from app.news.calendar_service import NewsGate
    cfg = cfg or load_config(overrides=WIDE)
    store = Store()
    now = NOW.to_pydatetime()
    news = NewsGate(cfg.news, clock=lambda: now)
    if news_ok:
        news.update([], now)
    spread = SpreadEngine(cfg.spread)
    from datetime import timedelta
    for i in range(60):
        spread.observe(Quote("XAUUSD", 4103.0, 4103.25, now - timedelta(seconds=100 - i), now, "demo"))
    spread.observe(Quote("XAUUSD", 4103.0, 4103.25, now - timedelta(seconds=1), now, "demo"))
    sent = []
    svc = SignalService(store, Notifier(store, lambda c_, t: sent.append(t) or len(sent), "c"), cfg, cfg.version_hash())
    return GPTARuntime(cfg, store, FakeProvider(frames, fail), spread, news, svc), sent, now


def test_runtime_tick_publishes_one_qualified_signal():
    rt, sent, now = build_runtime(build_buy_scenario())
    out = rt.tick(now)
    assert out["decisions"][0]["result"] == "QUALIFIED"
    assert len(sent) == 1 and "QUALIFIED SETUP" in sent[0]
    assert rt.tick(now)["decisions"] == [] and len(sent) == 1               # next minute: no duplicate
    # forming candles were not stored as completed
    assert rt.store.q("SELECT COUNT(*) c FROM market_candles WHERE completed=0 AND timeframe='M1'")[0]["c"] == 1
    assert rt.system_status(now)["data_label"] == "LIVE"
    from datetime import timedelta
    assert rt.system_status(now + timedelta(minutes=10))["data_label"] == "DELAYED"      # healthy but old is never LIVE


def test_runtime_blocks_when_news_calendar_never_loaded_or_a_timeframe_fails():
    rt, sent, now = build_runtime(build_buy_scenario(), news_ok=False)
    assert rt.tick(now)["decisions"][0]["result"] == "DATA_UNAVAILABLE" and sent == []
    rt, sent, now = build_runtime(build_buy_scenario(), fail=["M5"])
    out = rt.tick(now)
    assert sent == [] and rt.health["M5"]["healthy"] is False
    assert rt.system_status()["data_label"] == "UNAVAILABLE"                # never labelled LIVE with a dead feed


# ---- backtest --------------------------------------------------------------------------------------
def test_backtest_runs_same_engine_and_is_reproducible():
    f = build_buy_scenario()
    cfg = load_config(overrides=WIDE)
    # extend M1 with a post-entry run to TP so the paper trade completes
    m1 = f["M1"]
    extra = pd.DataFrame([(ts("13:15"), 4103.3, 4104.0, 4103.2, 4103.9), (ts("13:16"), 4103.9, 4110.2, 4103.8, 4110.0)],
                         columns=m1.columns)
    f2 = {**f, "M1": pd.concat([m1, extra], ignore_index=True)}
    a = run_backtest(f2, cfg, assume_no_news=True, assumed_spread=0.25, start_index=len(m1) - 8)
    b = run_backtest(f2, cfg, assume_no_news=True, assumed_spread=0.25, start_index=len(m1) - 8)
    assert a["decision_counts"].get("QUALIFIED") == 1 and a["metrics"]["wins"] == 1
    assert a["trades"] == b["trades"] and a["run_id"] == b["run_id"] and a["config"]["label"] == "SIMULATED"
    # without an explicit news assumption nothing can qualify (strict)
    c0 = run_backtest(f2, cfg, assume_no_news=False, assumed_spread=0.25, start_index=len(m1) - 8)
    assert c0["decision_counts"].get("QUALIFIED", 0) == 0


def test_oos_split_is_chronological():
    f = build_buy_scenario()
    a, b, cut = split_out_of_sample(f, 0.7)
    assert a["M1"]["time"].max() < cut <= b["M1"]["time"].min()
