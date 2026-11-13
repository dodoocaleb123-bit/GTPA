"""Unit tests: indicators, swings, zones, sweeps, risk maths, spread, news, credentials, validator."""
import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timedelta, timezone

from app.config import load_config
from app.indicators.core import atr, ema, warmup_ok
from app.strategy.swing_engine import find_swings
from app.strategy.zone_engine import build_zones, cluster, Zone
from app.strategy.liquidity_engine import detect_sweeps
from app.qualification import risk
from app.qualification.types import PASS, FAIL, UNAVAILABLE
from app.qualification.volatility import check_volatility
from app.qualification.qualification_engine import qualify, REQUIRED
from app.broker.spread_engine import Quote, SpreadEngine
from app.news.calendar_service import NewsGate, normalize_event, is_qualifying
from app.data.credential_manager import CredentialManager, KeyStatus, NoEligibleCredential
from app.data.candle_validator import validate_response

cfg = load_config()
UTC = timezone.utc


def frame(highs, lows, closes=None, start="2026-03-02 00:00", step=15):
    n = len(highs)
    t = pd.date_range(start, periods=n, freq=f"{step}min", tz="UTC")
    closes = closes or [(h + l) / 2 for h, l in zip(highs, lows)]
    return pd.DataFrame({"time": t, "open": closes, "high": highs, "low": lows, "close": closes}).astype({"open": float, "high": float, "low": float, "close": float})


# ---- indicators -----------------------------------------------------------------------------
def test_ema_seed_and_warmup():
    s = pd.Series(np.arange(1, 301, dtype=float))
    e = ema(s, 50)
    assert e.iloc[:49].isna().all() and e.iloc[49] == pytest.approx(25.5)
    assert e.iloc[-1] < s.iloc[-1]                      # lags a rising series
    assert not warmup_ok(250, 200) and warmup_ok(400, 200)


def test_atr_wilder():
    df = frame([11] * 30, [9] * 30, [10] * 30)
    a = atr(df, 14)
    assert a.iloc[:13].isna().all() and a.iloc[13] == pytest.approx(2.0) and a.iloc[-1] == pytest.approx(2.0)


# ---- swings ---------------------------------------------------------------------------------
def test_swing_strict_and_confirmation():
    h = [1, 2, 3, 9, 3, 2, 1, 2, 3]
    l = [x - 1 for x in h]
    sw = find_swings(frame(h, l), 2, 2)
    assert [(s.kind, s.idx, s.confirmed_idx) for s in sw if s.kind == "H"] == [("H", 3, 5)]
    # not confirmed until both right candles exist
    assert not [s for s in find_swings(frame(h[:5], l[:5]), 2, 2) if s.kind == "H"]


def test_equal_highs_do_not_qualify():
    h = [1, 2, 5, 5, 2, 1, 1]
    assert not [s for s in find_swings(frame(h, [x - 1 for x in h]), 2, 2) if s.kind == "H"]


def test_no_lookahead_swings_stable_under_truncation():
    rng = np.random.default_rng(0)
    c = np.cumsum(rng.normal(0, 1, 400)) + 100
    df = frame(list(c + 0.5), list(c - 0.5), list(c))
    full = {(s.idx, s.kind) for s in find_swings(df, 2, 2)}
    for cut in (120, 250, 399):
        part = {(s.idx, s.kind) for s in find_swings(df.iloc[:cut], 2, 2)}
        assert part <= full                              # nothing seen early that later disappears / uses the future
        assert all(i + 2 < cut for i, _ in part)         # only confirmed pivots


# ---- zones / sweeps ---------------------------------------------------------------------------
def _demand_setup():
    #            0   1   2   3   4   5   6   7   8
    h = [110, 108, 106, 105, 107, 109, 111, 112, 111]
    l = [108, 105, 102, 100, 103, 106, 108, 109, 99]    # idx3 swing low 100; last candle sweeps below it
    c = [109, 106, 103, 102, 106, 108, 110, 111, 106]   # last closes back above 100
    return frame(h, l, c)


def test_zone_invalidation_and_expiry():
    df = _demand_setup()
    sw = find_swings(df, 2, 2)
    z = build_zones(df, sw, "M15", 96)
    d = [x for x in z if x.type == "demand"][0]
    assert (d.upper, d.lower) == (105, 100) and d.status in ("ACTIVE", "TESTED")
    # close beyond distal boundary invalidates
    df2 = df.copy()
    df2.loc[8, "close"] = 99
    d2 = [x for x in build_zones(df2, find_swings(df2, 2, 2), "M15", 96) if x.type == "demand"][0]
    assert d2.status == "INVALIDATED"
    # expiry
    d3 = [x for x in build_zones(df, sw, "M15", 2) if x.type == "demand"][0]
    assert d3.status == "EXPIRED"


def test_sweep_requires_close_reclaim_not_just_wick():
    df = _demand_setup()
    sw = find_swings(df, 2, 2)
    zones = build_zones(df, sw, "M15", 96)
    ok_ = detect_sweeps(df, sw, zones, "BUY", window=1, lookback=8)
    assert len(ok_) == 1 and ok_[0].level == 100 and ok_[0].extreme == 99
    wick_only = df.copy()
    wick_only.loc[8, "close"] = 99.5                      # wick below AND closes below: no reclaim
    sw2 = find_swings(wick_only, 2, 2)
    assert detect_sweeps(wick_only, sw2, build_zones(wick_only, sw2, "M15", 96), "BUY", window=1, lookback=8) == []


def test_zone_cluster_keeps_identities():
    mk = lambda i, u, lo: Zone(f"z{i}", "demand", u, lo, i, pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-01", tz="UTC"), "M15")
    groups = cluster([mk(1, 10, 8), mk(2, 9.5, 8.5), mk(3, 20, 18)], 0.5)
    assert sorted(map(sorted, groups)) == [["z1", "z2"], ["z3"]]


# ---- risk maths -----------------------------------------------------------------------------
@pytest.mark.parametrize("tp,expect", [(1.99, False), (2.0, True), (2.5, True), (3.0, True)])
def test_rr_boundary(tp, expect):
    raw, adj = risk.reward_risk("BUY", 100.0, 99.0, 100.0 + tp, 0.0)
    assert (risk.check_rr(raw, adj, 2.0).status == PASS) is expect


def test_rr_accounts_for_spread_and_sell():
    raw, adj = risk.reward_risk("SELL", 100.0, 101.0, 98.0, 0.5)
    assert raw == pytest.approx(2.0) and adj == pytest.approx((2 - .5) / (1 + .5)) and adj < 2.0
    assert risk.check_rr(raw, adj, 2.0).status == FAIL


def test_stop_validation():
    assert risk.stop_loss("BUY", 100.0, 2.0, .25) == 99.5 and risk.stop_loss("SELL", 100.0, 2.0, .25) == 100.5
    assert risk.check_stop("BUY", 100, 101, 2, 5).code == "STOP_ON_WRONG_SIDE"
    assert risk.check_stop("BUY", 100, 80, 2, 5).code == "STOP_TOO_WIDE"


def test_take_profit_nearest_untouched_never_extended():
    h = [1, 2, 12, 2, 1, 3, 9, 3, 2, 4, 3, 2]
    df = frame(h, [x - 1 for x in h])
    sw = find_swings(df, 2, 2)
    assert risk.take_profit(df, sw, 5.0, "BUY") == 9      # nearest above entry, not the farther 12
    assert risk.take_profit(df, sw, 50.0, "BUY") is None  # nothing above: no TP is invented


def test_volatility_excludes_current_and_bands():
    a = pd.Series(list(np.linspace(1, 2, 100)) + [1.5])
    assert check_volatility(a, 100, 10, 90).status == PASS
    assert check_volatility(pd.Series(list(np.linspace(1, 2, 100)) + [9.0]), 100, 10, 90).code == "VOLATILITY_TOO_HIGH"
    assert check_volatility(pd.Series(list(np.linspace(1, 2, 100)) + [0.1]), 100, 10, 90).code == "VOLATILITY_TOO_LOW"
    assert check_volatility(pd.Series([1.0] * 50), 100, 10, 90).status == UNAVAILABLE


# ---- qualification --------------------------------------------------------------------------
def test_unavailable_never_qualifies():
    from app.qualification.types import ok, unavailable, QUALIFIED
    checks = [ok(n) for n in REQUIRED]
    assert qualify(checks).result == QUALIFIED
    checks[REQUIRED.index("valid_news")] = unavailable("valid_news", "CALENDAR_UNRELIABLE")
    assert qualify(checks).result == "DATA_UNAVAILABLE"
    assert qualify(checks[:-3]).result != QUALIFIED         # missing checks count as unavailable


# ---- spread ---------------------------------------------------------------------------------
def _q(now, bid=4200.10, ask=4200.35, age=1.0, connected=True):
    return Quote("XAUUSD", bid, ask, now - timedelta(seconds=age), now, "srv", connected)


def test_spread_rules():
    now = datetime(2026, 3, 26, 13, 0, tzinfo=UTC)
    eng = SpreadEngine(cfg.spread)
    assert eng.check(now, 5.0, _q(now)).code == "INSUFFICIENT_SPREAD_HISTORY"        # fails safe, not pass
    for i in range(60):
        eng.observe(_q(now - timedelta(seconds=100 - i), age=0))
    assert eng.check(now, 5.0, _q(now)).status == PASS
    assert eng.check(now, 20.0, _q(now, ask=4200.10 + 0.9)).code == "SPREAD_EXCEEDS_MEDIAN_MULTIPLE"
    assert eng.check(now, 2.0, _q(now)).code == "SPREAD_EXCEEDS_STOP_RATIO"           # 0.25 > 10% of 2.0
    assert eng.check(now, 5.0, _q(now, age=9)).code == "QUOTE_STALE"
    assert eng.check(now, 5.0, _q(now, bid=4201, ask=4200)).code == "INVALID_QUOTE"
    assert eng.check(now, 5.0, _q(now, connected=False)).status == UNAVAILABLE
    assert SpreadEngine(cfg.spread).check(now, 5.0, None).status == UNAVAILABLE


# ---- news -----------------------------------------------------------------------------------
def test_news_gate_strict_and_blackout():
    now = datetime(2026, 3, 26, 12, 0, tzinfo=UTC)
    g = NewsGate(cfg.news, clock=lambda: now)
    assert g.check(now).status == UNAVAILABLE                          # never refreshed => not "no news"
    ev = normalize_event({"name": "Core CPI m/m", "currency": "usd", "impact": "High", "datetime": "2026-03-26T08:30:00-04:00"})
    assert ev.ts_utc == datetime(2026, 3, 26, 12, 30, tzinfo=UTC) and is_qualifying(ev)   # converted to UTC
    g.update([ev], now)
    r = g.check(now)
    assert r.code == "NEWS_BLACKOUT"                                   # 30 min before
    assert g.check(now - timedelta(minutes=31)).status == PASS
    assert g.check(now + timedelta(minutes=61)).status == PASS         # >30 min after
    assert g.check(now + timedelta(hours=7)).status == UNAVAILABLE     # cache too old
    assert normalize_event({"name": "x"}) is None
    eur = normalize_event({"name": "CPI", "currency": "EUR", "impact": "High", "datetime": "2026-03-26T12:00:00Z"})
    assert not is_qualifying(eur)


# ---- credentials ------------------------------------------------------------------------------
def test_credential_rotation_and_secret_hygiene():
    now = [datetime(2026, 3, 26, 12, 0, 30, tzinfo=UTC)]
    m = CredentialManager({"a": "SECRET_A", "b": "SECRET_B"}, clock=lambda: now[0])
    la, ka = m.acquire()
    m.release_error(la, 429, "You have run out of API credits for the current minute SECRET_A")
    lb, kb = m.acquire()
    assert lb != la and kb != ka
    m.release_ok(lb, credits_used=1, credits_left=799)
    st = m.status()
    assert "SECRET_A" not in str(st) and "SECRET_B" not in str(st) and "SECRET_A" not in st[0]["last_error"]
    assert {s["label"]: s["status"] for s in st}[la] == "RATE_LIMITED"
    now[0] += timedelta(minutes=1)
    assert {s["label"]: s["status"] for s in m.status()}[la] == "AVAILABLE"        # recovered when limit resets
    m.release_error(*m.acquire()[:1], 401, "invalid apikey")
    m.release_error(*m.acquire()[:1], 401, "invalid apikey")
    with pytest.raises(NoEligibleCredential):
        m.acquire()
    assert all(s["status"] == "INVALID" for s in m.status())


def test_daily_quota_exhaustion_waits_until_midnight():
    now = [datetime(2026, 3, 26, 12, 0, tzinfo=UTC)]
    m = CredentialManager({"a": "K"}, clock=lambda: now[0])
    m.release_error(m.acquire()[0], 429, "daily limit reached")
    assert m.status()[0]["status"] == "QUOTA_EXHAUSTED"
    now[0] = datetime(2026, 3, 27, 0, 0, 1, tzinfo=UTC)
    assert m.status()[0]["status"] == "AVAILABLE"


# ---- candle validation ----------------------------------------------------------------------
def _payload(rows, symbol="XAU/USD", interval="15min"):
    return {"meta": {"symbol": symbol, "interval": interval}, "values": [
        dict(datetime=t, open=o, high=h, low=l, close=c) for t, o, h, l, c in rows]}


def test_validator_flags_bad_data_and_marks_forming():
    now = datetime(2026, 3, 26, 12, 20, tzinfo=UTC)
    rows = [("2026-03-26 11:30:00", 1, 2, 0.5, 1.5), ("2026-03-26 11:45:00", 1, 2, 0.5, 1.5),
            ("2026-03-26 11:45:00", 1, 2, 0.5, 1.5),                 # duplicate
            ("2026-03-26 12:00:00", 1, 0.5, 2, 1.5),                 # high < low
            ("2026-03-26 12:15:00", 1, 2, 0.5, 1.5)]                 # forming at 12:20
    r = validate_response(_payload(rows), "M15", "XAU/USD", now)
    codes = [i["code"] for i in r.issues]
    assert "DUPLICATE_TIMESTAMP" in codes and "INVALID_OHLC" in codes and "MISSING_INTERVAL" in codes
    assert [c["completed"] for c in r.accepted] == [True, True, False]
    assert validate_response(_payload(rows, symbol="EUR/USD"), "M15", "XAU/USD", now).issues[0]["code"] == "WRONG_INSTRUMENT"
    assert validate_response(_payload(rows, interval="1h"), "M15", "XAU/USD", now).issues[0]["code"] == "WRONG_TIMEFRAME"


def test_validator_stale_only_when_market_open():
    rows = [("2026-03-26 10:00:00", 1, 2, 0.5, 1.5)]
    assert validate_response(_payload(rows), "M15", "XAU/USD", datetime(2026, 3, 26, 12, 20, tzinfo=UTC)).stale
    sat = datetime(2026, 3, 28, 12, 20, tzinfo=UTC)
    assert not validate_response(_payload([("2026-03-27 20:00:00", 1, 2, 0.5, 1.5)]), "M15", "XAU/USD", sat).stale
