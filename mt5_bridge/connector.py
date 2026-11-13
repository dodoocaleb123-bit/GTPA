"""Thin wrapper around the MetaTrader5 Python package. Market data ONLY: this module never imports or calls any
order function (order_send etc.), by design (blueprint 5.7). `mt5` is injected so the logic is testable off Windows."""
from __future__ import annotations

import time

GOLD_HINTS = ("XAUUSD", "GOLD")


def discover_symbol(mt5, hint: str = "") -> str | None:
    """Find the broker's actual gold symbol (XAUUSD, XAUUSD.m, GOLD, ...). Prefers an exact XAUUSD match."""
    if hint and mt5.symbol_info(hint) is not None:
        return hint
    names = [s.name for s in (mt5.symbols_get() or [])]
    def rank(n: str):
        u = n.upper()
        return (0 if u == "XAUUSD" else 1 if u.startswith("XAUUSD") else 2 if u.startswith("GOLD") else 9, len(n))
    cands = sorted((n for n in names if any(h in n.upper() for h in GOLD_HINTS)), key=rank)
    return cands[0] if cands and rank(cands[0])[0] < 9 else None


def estimate_utc_offset(tick_epoch: float, now_epoch: float | None = None, step: int = 900) -> int:
    """Broker 'epoch' is server-local. Round the difference to a 15-minute multiple (real zones are)."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    return int(round((tick_epoch - now_epoch) / step) * step)


class MT5Connector:
    def __init__(self, mt5, symbol_hint: str = "", offset_seconds: int | None = None):
        self.mt5, self.hint, self.offset, self.symbol = mt5, symbol_hint, offset_seconds, None

    def connect(self, login=None, password=None, server=None) -> bool:
        ok = self.mt5.initialize(login=int(login), password=password, server=server) if login else self.mt5.initialize()
        if not ok:
            return False
        self.symbol = discover_symbol(self.mt5, self.hint)
        if not self.symbol or not self.mt5.symbol_select(self.symbol, True):
            return False
        return True

    def read(self, now_epoch: float | None = None) -> dict | None:
        """Return a quote dict (tick_time in UTC epoch seconds) or None if no usable tick."""
        t = self.mt5.symbol_info_tick(self.symbol) if self.symbol else None
        if t is None or not t.bid or not t.ask:
            return None
        raw = getattr(t, "time_msc", 0) / 1000.0 or float(t.time)
        if self.offset is None:
            self.offset = estimate_utc_offset(raw, now_epoch)
        info = self.mt5.account_info()
        return {"symbol": self.symbol, "bid": float(t.bid), "ask": float(t.ask), "tick_time": raw - self.offset,
                "server": getattr(info, "server", "") or "", "connected": True}

    def shutdown(self):
        self.mt5.shutdown()
