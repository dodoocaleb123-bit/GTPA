from datetime import datetime, timedelta, timezone
import threading

import pytest

from app.data.credential_manager import CredentialManager
from app.data.data_scheduler import Scheduler
from app.data.twelve_data_stream import StreamSubscriptionError, TwelveDataPriceStream


def test_price_event_exposes_provider_tick_and_optional_bid_ask():
    now = datetime(2026, 11, 16, 1, 0, 0, tzinfo=timezone.utc)
    stream = TwelveDataPriceStream("XAU/USD", lambda: "secret", clock=lambda: now)

    stream.handle_event({
        "event": "price", "symbol": "XAU/USD", "timestamp": now.timestamp(),
        "price": "4200.75", "bid": "4200.70", "ask": "4200.80",
    })

    result = stream.snapshot(now)
    assert result["price"] == 4200.75
    assert result["bid"] == 4200.70 and result["ask"] == 4200.80
    assert result["label"] == "LIVE"
    assert result["source"] == "twelve_data_websocket"
    assert result["stream_status"] == "SUBSCRIBED"


def test_old_tick_is_labeled_delayed_not_live():
    now = datetime(2026, 11, 16, 1, 0, 0, tzinfo=timezone.utc)
    stream = TwelveDataPriceStream("XAU/USD", lambda: None, max_age_seconds=30, clock=lambda: now)
    stream.handle_event({"event": "price", "symbol": "XAU/USD",
                         "timestamp": (now - timedelta(seconds=31)).timestamp(), "price": 4200.75})

    result = stream.snapshot(now)
    assert result["price"] == 4200.75
    assert result["label"] == "DELAYED"
    assert result["age_seconds"] == 31


def test_other_symbols_and_invalid_or_future_prices_are_ignored():
    now = datetime(2026, 11, 16, 1, 0, 0, tzinfo=timezone.utc)
    stream = TwelveDataPriceStream("XAU/USD", lambda: None, clock=lambda: now)
    events = [
        {"event": "price", "symbol": "EUR/USD", "timestamp": now.timestamp(), "price": 1.1},
        {"event": "price", "symbol": "XAU/USD", "timestamp": now.timestamp(), "price": -1},
        {"event": "price", "symbol": "XAU/USD", "timestamp": (now + timedelta(seconds=10)).timestamp(), "price": 4200},
        {"event": "price", "symbol": "XAU/USD", "timestamp": "bad", "price": 4200},
    ]
    for event in events:
        stream.handle_event(event)
    assert stream.snapshot(now)["price"] is None
    assert stream.snapshot(now)["label"] == "UNAVAILABLE"


def test_subscription_acknowledgement_and_failure_are_explicit():
    now = datetime(2026, 11, 16, 1, 0, 0, tzinfo=timezone.utc)
    stream = TwelveDataPriceStream("XAU/USD", lambda: None, clock=lambda: now)
    stream.handle_event({"event": "subscribe-status", "status": "ok",
                         "success": [{"symbol": "XAU/USD"}], "fails": []})
    assert stream.snapshot(now)["stream_status"] == "SUBSCRIBED"

    with pytest.raises(StreamSubscriptionError, match="WebSocket plan access"):
        stream.handle_event({"event": "subscribe-status", "status": "error",
                             "success": [], "fails": [{"symbol": "XAU/USD"}]})


def test_fake_connection_sends_subscription_and_stops_cleanly():
    now = datetime.now(timezone.utc)
    stream = None

    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.read = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send(self, value):
            self.sent.append(value)

        def recv(self, timeout=None):
            if not self.read:
                self.read = True
                return '{"event":"subscribe-status","status":"ok","success":[{"symbol":"XAU/USD"}],"fails":[]}'
            stream.stop()
            raise TimeoutError

    fake = FakeWebSocket()
    stream = TwelveDataPriceStream("XAU/USD", lambda: "test-key", connector=lambda _key: fake,
                                   clock=lambda: now)
    stream.run_forever()

    assert fake.sent[0] == '{"action": "subscribe", "params": {"symbols": "XAU/USD"}}'
    assert stream.snapshot(now)["stream_status"] == "STOPPED"


def test_stream_key_skips_invalid_keys_but_ignores_rest_quota_blocks():
    credentials = CredentialManager({"key1": "first-secret", "key2": "second-secret"})
    credentials.release_error("key1", 429, "daily quota exhausted")
    assert credentials.stream_key() == "first-secret"  # WebSocket credits are a separate pool.
    credentials.release_error("key1", 401, "invalid API key")
    assert credentials.stream_key() == "second-secret"


def test_scheduler_starts_and_stops_the_price_stream_worker():
    class FakeStream:
        def __init__(self):
            self.started = threading.Event()
            self.stopped = threading.Event()

        def run_forever(self):
            self.started.set()
            self.stopped.wait(5)

        def stop(self):
            self.stopped.set()

    stream = FakeStream()
    scheduler = Scheduler(object(), offset_seconds=60, price_stream=stream)
    scheduler.start()
    assert stream.started.wait(2)
    assert scheduler.status()["workers"]["gpta-price-stream"] is True
    scheduler.stop()
    for thread in scheduler._threads:
        thread.join(2)
    assert all(not thread.is_alive() for thread in scheduler._threads)


def test_stream_callbacks_feed_ticks_into_all_timeframe_aggregators():
    from app.data.tick_candles import TickCandleAggregator
    from app.database.store import Store

    now = datetime(2026, 11, 16, 1, 0, 1, tzinfo=timezone.utc)
    aggregator = TickCandleAggregator(Store(":memory:"), "XAU/USD")
    stream = TwelveDataPriceStream(
        "XAU/USD", lambda: "test-key", clock=lambda: now,
        on_tick=aggregator.ingest, on_connected=aggregator.on_connected,
        on_disconnected=aggregator.on_disconnected, on_idle=aggregator.finalize_due,
    )
    stream.handle_event({"event": "subscribe-status", "status": "ok",
                         "success": [{"symbol": "XAU/USD"}], "fails": []})
    stream.handle_event({"event": "price", "symbol": "XAU/USD", "timestamp": now.timestamp(), "price": 4200.75})

    assert set(aggregator.current_bars()) == {"M1", "M5", "M15", "H1"}
