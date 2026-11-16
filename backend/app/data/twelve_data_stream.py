"""Real-time price stream for the dashboard headline price.

Strategy analysis continues to use completed OHLC candles from the REST time-series API.
This stream is a separate, in-memory latest-tick read model; it does not build candles.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote

WS_BASE = "wss://ws.twelvedata.com/v1/quotes/price"


def _normal_symbol(value: object) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    price: float
    tick_time: datetime
    received_at: datetime
    bid: float | None = None
    ask: float | None = None


class StreamSubscriptionError(RuntimeError):
    pass


class TwelveDataPriceStream:
    """One long-lived Twelve Data /quotes/price subscription for one instrument."""

    def __init__(self, symbol: str, api_key: Callable[[], str | None], *, max_age_seconds: float = 30.0,
                 connector: Callable | None = None, clock: Callable[[], datetime] | None = None,
                 heartbeat_seconds: float = 10.0):
        self.symbol = symbol
        self._api_key = api_key
        self.max_age_seconds = max(1.0, float(max_age_seconds))
        self._connector = connector or self._default_connector
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._tick: PriceTick | None = None
        self._state = "STOPPED"
        self._error: str | None = None

    @staticmethod
    def _default_connector(api_key: str):
        from websockets.sync.client import connect
        return connect(
            f"{WS_BASE}?apikey={quote(api_key, safe='')}",
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=1_048_576,
        )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._state = "STOPPING"

    def run_forever(self) -> None:
        """Reconnect with bounded backoff; intended to run in one daemon thread."""
        delay = 1.0
        while not self._stop.is_set():
            key = self._api_key()
            if not key:
                self._set_state("UNAVAILABLE", "No Twelve Data API key is configured")
                self._stop.wait(30.0)
                continue
            self._set_state("CONNECTING", None)
            try:
                with self._connector(key) as ws:
                    self._set_state("SUBSCRIBING", None)
                    ws.send(json.dumps({"action": "subscribe", "params": {"symbols": self.symbol}}))
                    delay = 1.0
                    self._consume(ws)
                if not self._stop.is_set():
                    raise ConnectionError("stream closed")
            except StreamSubscriptionError as exc:
                self._set_state("UNAVAILABLE", str(exc))
                self._stop.wait(min(max(delay, 30.0), 300.0))
                delay = min(max(delay * 2, 30.0), 300.0)
            except Exception as exc:
                if self._stop.is_set():
                    break
                # Do not expose exception text: WebSocket errors can include the API-key URL.
                self._set_state("DISCONNECTED", f"WebSocket error: {type(exc).__name__}")
                self._stop.wait(delay)
                delay = min(delay * 2, 60.0)
        self._set_state("STOPPED", None)

    def _consume(self, ws) -> None:
        last_heartbeat = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_heartbeat >= self.heartbeat_seconds:
                ws.send(json.dumps({"action": "heartbeat"}))
                last_heartbeat = now
            try:
                raw = ws.recv(timeout=1.0)
            except TimeoutError:
                continue
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            self.handle_event(message)

    def handle_event(self, message: dict) -> None:
        """Apply one provider event. Public to keep parsing testable without network access."""
        event = message.get("event")
        if event == "subscribe-status":
            success = message.get("success") or []
            subscribed = any(_normal_symbol(item.get("symbol")) == _normal_symbol(self.symbol)
                             for item in success if isinstance(item, dict))
            if message.get("status") != "ok" or not subscribed:
                # Avoid forwarding raw provider payloads into dashboard/API responses.
                raise StreamSubscriptionError(
                    f"Twelve Data did not authorize the {self.symbol} stream; check API key, symbol and WebSocket plan access"
                )
            self._set_state("SUBSCRIBED", None)
            return
        if event == "error":
            raise StreamSubscriptionError(
                f"Twelve Data rejected the {self.symbol} stream; check API key, symbol and WebSocket plan access"
            )
        if event != "price" or _normal_symbol(message.get("symbol")) != _normal_symbol(self.symbol):
            return
        try:
            price = float(message["price"])
            timestamp = datetime.fromtimestamp(float(message["timestamp"]), timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return
        now = self._clock()
        if not math.isfinite(price) or price <= 0 or timestamp > now + timedelta(seconds=5):
            return
        bid = _optional_price(message.get("bid"))
        ask = _optional_price(message.get("ask"))
        if bid is not None and ask is not None and ask < bid:
            bid = ask = None
        tick = PriceTick(self.symbol, price, timestamp, now, bid, ask)
        with self._lock:
            self._tick = tick
            self._state = "SUBSCRIBED"
            self._error = None

    def snapshot(self, now: datetime | None = None) -> dict:
        now = now or self._clock()
        with self._lock:
            tick, state, error = self._tick, self._state, self._error
        if tick is None:
            return {
                "instrument": self.symbol,
                "price": None,
                "bid": None,
                "ask": None,
                "tick_time": None,
                "age_seconds": None,
                "label": "UNAVAILABLE",
                "source": "twelve_data_websocket",
                "stream_status": state,
                "error": error,
            }
        age = (now - tick.tick_time).total_seconds()
        label = "LIVE" if -5 <= age <= self.max_age_seconds else "DELAYED"
        return {
            "instrument": tick.symbol,
            "price": tick.price,
            "bid": tick.bid,
            "ask": tick.ask,
            "tick_time": tick.tick_time.isoformat(),
            "received_at": tick.received_at.isoformat(),
            "age_seconds": round(max(0.0, age), 3),
            "label": label,
            "source": "twelve_data_websocket",
            "stream_status": state,
            "error": error,
        }

    def _set_state(self, state: str, error: str | None) -> None:
        with self._lock:
            self._state, self._error = state, error


def _optional_price(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None
