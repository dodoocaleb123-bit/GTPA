"""Telegram formatting, delivery tracking and bot commands (blueprint 16).
This module never analyses markets: it only renders events produced by the signal engine."""
from __future__ import annotations

import time
from typing import Callable

from ..database.store import Store, _now

DISCLAIMER = "Prices are indicative. ENTRY OBSERVED does not confirm broker execution."


def _p(x: float) -> str:
    return f"{x:,.2f}"


def format_signal(s: dict) -> str:
    buy = s["direction"] == "BUY"
    zone = "Demand zone" if buy else "Supply zone"
    mss = "Bullish MSS" if buy else "Bearish MSS"
    return "\n".join([
        "GPTA | GOLD DESK", "QUALIFIED SETUP", f"{s['direction']} XAU/USD", "",
        "ENTRY ZONE", f"{_p(s['entry_low'])} - {_p(s['entry_high'])}", "",
        "STOP LOSS", _p(s["sl"]), "", "TAKE PROFIT", _p(s["tp"]), "", "RISK/REWARD", f"1:{s['rr']:.1f}", "",
        "TECHNICAL CONFLUENCE", f"✓ H1/M15 {'bullish' if buy else 'bearish'} alignment", f"✓ {zone} confirmed",
        "✓ Liquidity sweep detected", f"✓ {mss} confirmed", "✓ Entry retest validated", "✓ Spread filter passed",
        "✓ Economic news filter passed", "", "STATUS", "WAITING FOR ENTRY", "EXPIRY", str(s["expiry"])[:16].replace("T", " ") + " UTC",
        "SIGNAL ID", s["signal_id"], "", DISCLAIMER])


def format_event(kind: str, s: dict, extra: str = "") -> str:
    titles = {"ENTRY_OBSERVED": "ENTRY OBSERVED", "INVALIDATED": "SETUP INVALIDATED", "EXPIRED": "SIGNAL EXPIRED",
              "TP_HIT": "TAKE PROFIT HIT", "SL_HIT": "STOP LOSS HIT", "CLOSED_OTHER": "CLOSED - AMBIGUOUS OUTCOME"}
    return f"GPTA | {titles.get(kind, kind)}\n{s['direction']} XAU/USD  {s['signal_id']}\n{extra}\n\n{DISCLAIMER}".strip()


class Notifier:
    """`sender(chat_id, text) -> message_id` is injected (real one calls the Bot API via httpx)."""

    def __init__(self, store: Store, sender: Callable[[str, str], str], chat_id: str, max_attempts: int = 3,
                 sleep=time.sleep):
        self.store, self.sender, self.chat_id, self.max_attempts, self.sleep = store, sender, chat_id, max_attempts, sleep

    def send(self, dedupe_key: str, kind: str, text: str) -> bool:
        row = self.store.q("SELECT status FROM telegram_messages WHERE dedupe_key=?", (dedupe_key,))
        if row and row[0]["status"] == "SENT":
            return True                       # duplicate prevented
        if not row:
            self.store.x("INSERT INTO telegram_messages(dedupe_key,chat_id,kind,status,attempts,ts) VALUES(?,?,?,?,?,?)",
                         (dedupe_key, self.chat_id, kind, "PENDING", 0, _now()))
        err = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                mid = self.sender(self.chat_id, text)
                self.store.x("UPDATE telegram_messages SET status='SENT',attempts=attempts+?,message_id=?,error=NULL WHERE dedupe_key=?",
                             (attempt, str(mid), dedupe_key))
                return True
            except Exception as e:           # transient failure: retry, then leave PENDING for the next cycle
                err = type(e).__name__
                if attempt < self.max_attempts:
                    self.sleep(min(2 ** attempt, 8))
        self.store.x("UPDATE telegram_messages SET attempts=attempts+?,error=? WHERE dedupe_key=?",
                     (self.max_attempts, err, dedupe_key))
        return False

    def retry_pending(self, texts: dict[str, str]) -> int:
        n = 0
        for r in self.store.q("SELECT dedupe_key,kind FROM telegram_messages WHERE status='PENDING'"):
            if r["dedupe_key"] in texts and self.send(r["dedupe_key"], r["kind"], texts[r["dedupe_key"]]):
                n += 1
        return n

    def delivery_ok(self) -> bool:
        return not self.store.q("SELECT 1 FROM telegram_messages WHERE status='PENDING' AND attempts>=? LIMIT 1", (self.max_attempts,))


def make_http_sender(token: str):
    def send(chat_id: str, text: str) -> str:
        import httpx
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat_id, "text": text}, timeout=10)
        r.raise_for_status()
        return str(r.json()["result"]["message_id"])
    return send


def handle_command(cmd: str, ctx: dict) -> str:
    """Pure command router. `ctx` supplies already-computed data; no decisions are made here."""
    c = cmd.strip().split()[0].lower().split("@")[0]
    if c in ("/start", "/help"):
        return "GPTA commands: /status /signals /market /performance /history /help"
    if c == "/status":
        return "GPTA status\n" + "\n".join(f"{k}: {v}" for k, v in ctx.get("health", {}).items())
    if c == "/signals":
        a = ctx.get("active", [])
        return "No active signals." if not a else "\n".join(
            f"{s['signal_id']} {s['direction']} entry {_p(s['entry'])} SL {_p(s['sl'])} TP {_p(s['tp'])} [{s['state']}]" for s in a)
    if c == "/market":
        m = ctx.get("market", {})
        return "Market data unavailable." if not m else "\n".join(f"{k}: {v}" for k, v in m.items())
    if c == "/performance":
        m = ctx.get("performance", {})
        return "No completed paper trades yet." if not m.get("completed") else \
            f"{m['label']}\ntrades {m['completed']}  win rate {m['win_rate']}  net R {m['net_r']}  PF {m['profit_factor']}"
    if c == "/history":
        h = ctx.get("history", [])
        return "No signals yet." if not h else "\n".join(f"{s['signal_id']} {s['direction']} {s['state']}" for s in h[:10])
    return "Unknown command. Try /help"
