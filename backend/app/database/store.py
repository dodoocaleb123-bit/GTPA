"""SQLite repository (stdlib). Development store; the schema is plain SQL so it ports to PostgreSQL.
SQLAlchemy was requested in the blueprint but is not installable in the build sandbox; all DB access
is isolated in this class so it can be swapped for SQLAlchemy repositories later.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_candles(
  instrument TEXT, timeframe TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  provider TEXT, retrieved_at TEXT, completed INTEGER, validation TEXT,
  PRIMARY KEY(instrument,timeframe,ts));
CREATE TABLE IF NOT EXISTS data_requests(id INTEGER PRIMARY KEY, ts TEXT, timeframe TEXT, ok INTEGER, error TEXT, issues TEXT);
CREATE TABLE IF NOT EXISTS broker_quotes(id INTEGER PRIMARY KEY, symbol TEXT, bid REAL, ask REAL, tick_ts TEXT, server TEXT, retrieved_at TEXT, valid INTEGER);
CREATE TABLE IF NOT EXISTS spread_history(id INTEGER PRIMARY KEY, ts TEXT, spread REAL);
CREATE TABLE IF NOT EXISTS economic_events(event_id TEXT PRIMARY KEY, name TEXT, currency TEXT, impact TEXT, ts_utc TEXT, previous TEXT, forecast TEXT, actual TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS setups(setup_id TEXT PRIMARY KEY, direction TEXT, created_at TEXT, strategy_version TEXT, status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS qualification_results(id INTEGER PRIMARY KEY, setup_id TEXT, ts TEXT, result TEXT, checks TEXT);
CREATE TABLE IF NOT EXISTS signals(signal_id TEXT PRIMARY KEY, setup_id TEXT UNIQUE, direction TEXT, entry REAL, entry_low REAL, entry_high REAL, sl REAL, tp REAL, rr REAL, state TEXT, created_at TEXT, expiry TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS signal_events(id INTEGER PRIMARY KEY, signal_id TEXT, ts TEXT, event TEXT, detail TEXT, UNIQUE(signal_id,event));
CREATE TABLE IF NOT EXISTS paper_trades(signal_id TEXT PRIMARY KEY, proposed_entry REAL, sim_entry REAL, sl REAL, tp REAL, entry_ts TEXT, exit_ts TEXT, exit_price REAL, outcome TEXT, r_multiple REAL, spread REAL, slippage REAL, direction TEXT);
CREATE TABLE IF NOT EXISTS telegram_messages(id INTEGER PRIMARY KEY, dedupe_key TEXT UNIQUE, chat_id TEXT, kind TEXT, status TEXT, attempts INTEGER, message_id TEXT, error TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS strategy_versions(version TEXT PRIMARY KEY, created_at TEXT, config TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS system_logs(id INTEGER PRIMARY KEY, ts TEXT, level TEXT, source TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS backtest_runs(run_id TEXT PRIMARY KEY, created_at TEXT, config TEXT, metrics TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount

    # -- candles ---------------------------------------------------------------------------
    def upsert_candles(self, instrument: str, tf: str, rows: list[dict], provider="twelvedata") -> list[dict]:
        """Insert/update. Returns detected historical corrections (completed candle whose OHLC changed)."""
        corrections = []
        with self.lock:
            for r in rows:
                ts = r["time"].isoformat()
                old = self.db.execute("SELECT open,high,low,close,completed FROM market_candles WHERE instrument=? AND timeframe=? AND ts=?",
                                      (instrument, tf, ts)).fetchone()
                if old and old["completed"] and (old["open"], old["high"], old["low"], old["close"]) != \
                        (r["open"], r["high"], r["low"], r["close"]):
                    corrections.append({"ts": ts, "timeframe": tf, "old": tuple(old)[:4],
                                        "new": (r["open"], r["high"], r["low"], r["close"])})
                    continue  # keep the first completed value; a human must review the correction
                self.db.execute("INSERT OR REPLACE INTO market_candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (instrument, tf, ts, r["open"], r["high"], r["low"], r["close"], r.get("volume"),
                                 provider, _now(), int(r["completed"]), "OK"))
            self.db.commit()
        return corrections

    def candles(self, instrument: str, tf: str, limit: int = 1000, completed_only=True) -> pd.DataFrame:
        sql = "SELECT ts,open,high,low,close,volume FROM market_candles WHERE instrument=? AND timeframe=?"
        if completed_only:
            sql += " AND completed=1"
        rows = self.q(sql + " ORDER BY ts DESC LIMIT ?", (instrument, tf, limit))
        df = pd.DataFrame(rows[::-1], columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.rename(columns={"ts": "time"})
        df["time"] = pd.to_datetime(df["time"], utc=True)
        return df.reset_index(drop=True)

    # -- misc ------------------------------------------------------------------------------
    def log(self, level: str, source: str, message: str):
        self.x("INSERT INTO system_logs(ts,level,source,message) VALUES(?,?,?,?)", (_now(), level, source, message))

    def audit(self, actor: str, action: str, detail: dict):
        self.x("INSERT INTO audit_logs(ts,actor,action,detail) VALUES(?,?,?,?)", (_now(), actor, action, json.dumps(detail, default=str)))

    def save_strategy_version(self, version: str, config: dict, note: str = ""):
        self.x("INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?,?)", (version, _now(), json.dumps(config, default=str), note))

    def health(self) -> bool:
        try:
            self.q("SELECT 1")
            return True
        except Exception:
            return False
