"""API tests against a real uvicorn server (Starlette's TestClient needs httpx, unavailable in the sandbox)."""
import socket
import threading
import time
from datetime import datetime, timezone

import pytest
import requests
import uvicorn

from app.main import create_app
from test_flow import build_runtime, build_buy_scenario

TOKEN = "t" * 32
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def server():
    rt, sent, now = build_runtime(build_buy_scenario())
    rt.tick(now)                                   # produces one QUALIFIED + published signal
    app = create_app(rt, token=TOKEN)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", rt, sent
    srv.should_exit = True; th.join(5)


def get(base, path, **kw):
    return requests.get(base + path, headers=H, timeout=5, **kw)


def test_health_open_everything_else_needs_token(server):
    base, *_ = server
    assert requests.get(base + "/api/health", timeout=5).json()["status"] == "ok"
    assert requests.get(base + "/api/signals", timeout=5).status_code == 401
    assert requests.get(base + "/api/signals", headers={"Authorization": "Bearer wrong"}, timeout=5).status_code == 401
    assert get(base, "/api/signals").status_code == 200


def test_create_app_refuses_to_start_without_auth_config():
    rt, *_ = build_runtime(build_buy_scenario())
    with pytest.raises(RuntimeError):
        create_app(rt, token=None)


def test_signal_endpoints_expose_the_engines_decision_and_audit(server):
    base, rt, sent = server
    act = get(base, "/api/signals/active").json()
    assert len(act) == 1 and act[0]["direction"] == "BUY" and act[0]["state"] == "WAITING_FOR_ENTRY" and act[0]["rr"] >= 2
    one = get(base, f"/api/signals/{act[0]['signal_id']}").json()
    assert len(one["qualification"][0]["checks"]) == 16
    assert get(base, "/api/signals/NOPE").status_code == 404
    assert get(base, "/api/signals/history?direction=SELL").json()["signals"] == []


def test_market_endpoints_label_data_and_reject_bad_input(server):
    base, *_ = server
    c = get(base, "/api/market/candles?timeframe=M5&limit=5").json()
    assert len(c["candles"]) == 5 and c["label"] in ("LIVE", "UNAVAILABLE")
    assert get(base, "/api/market/candles?timeframe=M3").status_code == 400
    assert get(base, "/api/market/candles?limit=abc").status_code == 400
    assert get(base, "/api/market/timeframes").json()["M1"]["stored"] > 0
    assert get(base, "/api/market/data-health").json()["baseline_requests_per_day"] == 5760


def test_broker_quote_ingest_and_spread(server):
    base, rt, _ = server
    now = datetime.now(timezone.utc).timestamp()
    r = requests.post(base + "/api/broker/quote", headers=H, timeout=5,
                      json={"symbol": "XAUUSD", "bid": 4200.10, "ask": 4200.35, "tick_time": now, "server": "Demo"})
    assert r.json() == {"accepted": True, "valid": True}
    s = get(base, "/api/spread/current").json()
    assert s["spread"] == pytest.approx(0.25) and get(base, "/api/broker/status").json()["fresh"] is True
    assert requests.post(base + "/api/broker/quote", headers=H, json={"bid": 1}, timeout=5).status_code == 400
    assert requests.post(base + "/api/broker/quote", json={}, timeout=5).status_code == 401
    inverted = requests.post(base + "/api/broker/quote", headers=H, timeout=5,
                             json={"symbol": "XAUUSD", "bid": 4200.5, "ask": 4200.1, "tick_time": now}).json()
    assert inverted["valid"] is False                     # stored for audit, never usable for qualification


def test_settings_are_validated_versioned_audited_and_locked(server):
    base, rt, _ = server
    v0 = get(base, "/api/settings").json()["version"]
    put = lambda body: requests.put(base + "/api/settings", headers=H, json=body, timeout=5)
    assert put({"changes": {"risk": {"minimum_reward_risk": 1.5}}}).status_code == 400      # rule: never below 1:2
    assert put({"changes": {"execution": {"broker_execution_enabled": True}}}).status_code == 400
    assert put({"changes": {"spread": {"block_on_failure": False}}}).status_code == 400
    assert put({"changes": {"nope": {"x": 1}}}).status_code == 400
    assert put({"changes": {"risk": {"stop_buffer_atr": "wide"}}}).status_code == 400       # wrong type
    assert get(base, "/api/settings").json()["version"] == v0                                  # rejects changed nothing
    ok = put({"changes": {"risk": {"stop_buffer_atr": 0.3}}, "note": "wider stop test"}).json()
    assert ok["version"] != v0 and ok["applied"][0] == {"path": "risk.stop_buffer_atr", "old": 0.25, "new": 0.3}
    assert rt.cfg.risk.stop_buffer_atr == 0.3 and rt.signals.version == ok["version"]
    assert any(v["version"] == ok["version"] for v in get(base, "/api/strategy/versions").json())
    assert rt.store.q("SELECT COUNT(*) c FROM audit_logs WHERE action='settings_change'")[0]["c"] == 1


def test_performance_and_news_and_status_shapes(server):
    base, rt, _ = server
    p = get(base, "/api/performance/summary").json()
    assert p["total_signals"] == 1 and "PAPER" in p["label"]
    assert get(base, "/api/performance/equity").json()["label"] == "PAPER/SIMULATED"
    rt.news.update([], datetime.now(timezone.utc))        # fixture calendar is from March; refresh against the real clock
    assert get(base, "/api/news/status").json()["reliable"] is True
    st = get(base, "/api/system/status").json()
    assert st["mode"] == "paper" and st["broker_execution_enabled"] is False
    assert "apikey" not in str(get(base, "/api/market/data-health").json()).lower()


def test_backtest_endpoint_runs_and_labels_simulated(server):
    base, *_ = server
    r = requests.post(base + "/api/backtest/run", headers=H, json={"assume_no_news": True, "assumed_spread": 0.25}, timeout=60)
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert r.json()["label"] == "SIMULATED"
        assert get(base, f"/api/backtest/{r.json()['run_id']}").json()["label"] == "SIMULATED"
