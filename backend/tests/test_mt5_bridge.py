import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mt5_bridge.connector import MT5Connector, discover_symbol, estimate_utc_offset
from mt5_bridge.quote_publisher import QuotePublisher


class FakeMT5:
    def __init__(self, names, tick, ok=True):
        self.names, self._tick, self.ok = names, tick, ok
    def initialize(self, **kw): return self.ok
    def symbols_get(self): return [NS(name=n) for n in self.names]
    def symbol_info(self, n): return NS(name=n) if n in self.names else None
    def symbol_select(self, n, on): return n in self.names
    def symbol_info_tick(self, s): return self._tick
    def account_info(self): return NS(server="Demo-Server")
    def shutdown(self): pass


def test_symbol_discovery_prefers_exact_then_prefix_then_gold_and_ignores_others():
    assert discover_symbol(FakeMT5(["EURUSD", "XAUUSD.m", "XAUUSD", "GOLD"], None)) == "XAUUSD"
    assert discover_symbol(FakeMT5(["EURUSD", "GOLD#"], None)) == "GOLD#"
    assert discover_symbol(FakeMT5(["EURUSD"], None)) is None
    assert discover_symbol(FakeMT5(["XAUUSD", "XAUUSD.m"], None), hint="XAUUSD.m") == "XAUUSD.m"


def test_server_clock_offset_is_estimated_so_quotes_are_not_hours_stale():
    now = 1_800_000_000
    tick = NS(bid=4200.1, ask=4200.35, time=now + 3 * 3600, time_msc=(now + 3 * 3600) * 1000)   # broker on UTC+3
    assert estimate_utc_offset(now + 3 * 3600 + 0.4, now) == 3 * 3600
    c = MT5Connector(FakeMT5(["XAUUSD"], tick))
    assert c.connect()
    q = c.read(now_epoch=now + 0.4)
    assert abs(q["tick_time"] - now) < 1 and q["bid"] == 4200.1 and q["server"] == "Demo-Server" and q["symbol"] == "XAUUSD"


def test_no_tick_or_zero_prices_yield_nothing_and_connect_failure_reported():
    c = MT5Connector(FakeMT5(["XAUUSD"], None)); c.connect()
    assert c.read() is None
    c2 = MT5Connector(FakeMT5(["XAUUSD"], NS(bid=0, ask=0, time=1, time_msc=0))); c2.connect()
    assert c2.read() is None
    assert MT5Connector(FakeMT5(["XAUUSD"], None, ok=False)).connect() is False


def test_publisher_never_raises_and_sends_bearer_token():
    sent = {}
    def post(url, json, headers, timeout):
        sent.update(url=url, headers=headers); return NS(status_code=200)
    assert QuotePublisher("http://b", "tok", post).publish({"bid": 1}) is True and sent["headers"]["Authorization"] == "Bearer tok"
    def boom(*a, **k): raise ConnectionError()
    assert QuotePublisher("http://b", "tok", boom).publish({}) is False
