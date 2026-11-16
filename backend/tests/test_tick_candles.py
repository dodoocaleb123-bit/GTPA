from datetime import datetime, timedelta, timezone

from app.data.tick_candles import PROVIDER, TickCandleAggregator
from app.data.twelve_data_stream import PriceTick
from app.database.store import Store


BASE = datetime(2026, 11, 16, 0, 0, 0, tzinfo=timezone.utc)


def tick(seconds: int, price: float) -> PriceTick:
    at = BASE + timedelta(seconds=seconds)
    return PriceTick("XAU/USD", price, at, at)


def test_ticks_form_all_four_timeframes_without_persisting_open_bars():
    store = Store(":memory:")
    aggregator = TickCandleAggregator(store, "XAU/USD")
    aggregator.on_connected(BASE)
    assert aggregator.ingest(tick(1, 100.0))
    assert aggregator.ingest(tick(45, 103.0))

    active = aggregator.current_bars()
    assert set(active) == {"M1", "M5", "M15", "H1"}
    for bar in active.values():
        assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (100.0, 103.0, 100.0, 103.0)
        assert bar["completed"] is False and bar["coverage"] == "FULL"
    assert store.q("SELECT COUNT(*) AS n FROM market_candles")[0]["n"] == 0


def test_interval_rollover_persists_completed_ohlc_only():
    store = Store(":memory:")
    aggregator = TickCandleAggregator(store, "XAU/USD")
    aggregator.on_connected(BASE)
    aggregator.ingest(tick(1, 100.0))
    aggregator.ingest(tick(30, 98.0))
    aggregator.ingest(tick(70, 102.0))   # closes M1 00:00; M5 remains forming
    aggregator.ingest(tick(301, 105.0))  # closes M1 00:01 and M5 00:00

    m1 = store.candles("XAU/USD", "M1")
    assert [t.to_pydatetime() for t in m1["time"]] == [BASE, BASE + timedelta(minutes=1)]
    first = m1.iloc[0]
    assert (first["open"], first["high"], first["low"], first["close"]) == (100.0, 100.0, 98.0, 98.0)
    m5 = store.candles("XAU/USD", "M5")
    assert len(m5) == 1 and m5.iloc[0]["time"].to_pydatetime() == BASE
    assert (m5.iloc[0]["open"], m5.iloc[0]["high"], m5.iloc[0]["low"], m5.iloc[0]["close"]) == (100.0, 102.0, 98.0, 102.0)
    assert store.candles("XAU/USD", "M15").empty


def test_idle_finalization_closes_periods_without_a_boundary_tick():
    store = Store(":memory:")
    aggregator = TickCandleAggregator(store, "XAU/USD", finalize_grace_seconds=2)
    aggregator.on_connected(BASE)
    aggregator.ingest(tick(1, 100.0))

    closed = aggregator.finalize_due(BASE + timedelta(minutes=1, seconds=2))
    assert closed == 1
    assert len(store.candles("XAU/USD", "M1")) == 1
    assert "M1" not in aggregator.current_bars()
    assert "M5" in aggregator.current_bars()


def test_late_tick_cannot_reopen_a_bucket_after_idle_finalization():
    store = Store(":memory:")
    aggregator = TickCandleAggregator(store, "XAU/USD", finalize_grace_seconds=2)
    aggregator.on_connected(BASE)
    aggregator.ingest(tick(1, 100.0))
    aggregator.finalize_due(BASE + timedelta(minutes=1, seconds=2))
    late = PriceTick("XAU/USD", 99.0, BASE + timedelta(seconds=59), BASE + timedelta(minutes=1, seconds=3))
    assert aggregator.ingest(late)

    saved = store.candles("XAU/USD", "M1").iloc[0]
    assert (saved["open"], saved["high"], saved["low"], saved["close"]) == (100.0, 100.0, 100.0, 100.0)
    assert "M1" not in aggregator.current_bars()


def test_m15_and_h1_bars_close_on_their_own_boundaries():
    store = Store(":memory:")
    aggregator = TickCandleAggregator(store, "XAU/USD")
    aggregator.on_connected(BASE)
    aggregator.ingest(tick(1, 100.0))
    aggregator.ingest(tick(901, 102.0))  # M15 00:00 closes; H1 remains open
    m15 = store.candles("XAU/USD", "M15")
    assert len(m15) == 1 and m15.iloc[0]["time"].to_pydatetime() == BASE
    assert store.candles("XAU/USD", "H1").empty
    aggregator.ingest(tick(3601, 101.0))  # M15 00:15 and H1 00:00 close
    m15 = store.candles("XAU/USD", "M15")
    h1 = store.candles("XAU/USD", "H1")
    assert [t.to_pydatetime() for t in m15["time"]] == [BASE, BASE + timedelta(minutes=15)]
    assert len(h1) == 1 and h1.iloc[0]["time"].to_pydatetime() == BASE
    assert (h1.iloc[0]["open"], h1.iloc[0]["high"], h1.iloc[0]["low"], h1.iloc[0]["close"]) == (100.0, 102.0, 100.0, 102.0)


def test_first_partial_bucket_and_bars_spanning_disconnect_are_not_marked_complete():
    store = Store(":memory:")
    aggregator = TickCandleAggregator(store, "XAU/USD", finalize_grace_seconds=2)
    aggregator.on_connected(BASE + timedelta(seconds=30))
    aggregator.ingest(tick(31, 100.0))
    aggregator.ingest(tick(65, 101.0))  # drops partial M1 00:00; M1 00:01 is fully observed
    aggregator.on_disconnected()
    aggregator.on_connected(BASE + timedelta(seconds=80))
    aggregator.ingest(tick(90, 102.0))  # current M1 still spans the disconnect, so it remains partial
    aggregator.ingest(tick(125, 103.0)) # M1 00:01 is not persisted; M1 00:02 starts clean
    aggregator.finalize_due(BASE + timedelta(minutes=3, seconds=2))

    m1 = store.candles("XAU/USD", "M1")
    assert [t.to_pydatetime() for t in m1["time"]] == [BASE + timedelta(minutes=2)]
    assert aggregator.status(BASE + timedelta(minutes=3))["dropped_partial_bars"]["M1"] >= 2


def test_rest_polling_cannot_replace_tick_aggregated_completed_bars():
    store = Store(":memory:")
    row = {"time": BASE, "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0,
           "volume": None, "completed": True}
    store.upsert_candles("XAU/USD", "M1", [row], provider=PROVIDER)
    rest_revision = {**row, "open": 200.0, "high": 205.0, "low": 199.0, "close": 204.0}
    store.upsert_candles("XAU/USD", "M1", [rest_revision], provider="twelvedata")

    saved = store.candles("XAU/USD", "M1").iloc[0]
    assert (saved["open"], saved["high"], saved["low"], saved["close"]) == (100.0, 105.0, 99.0, 104.0)
    assert store.q("SELECT provider FROM market_candles")[0]["provider"] == PROVIDER
