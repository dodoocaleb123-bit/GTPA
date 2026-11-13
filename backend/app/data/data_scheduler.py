"""Central scheduler (blueprint 4.2). Plain threads instead of APScheduler (not installable in the build
sandbox); the jobs are tiny and the swap is mechanical. One strategy cycle per minute, a few seconds after the
minute boundary so providers have published the candle that just closed. News is refreshed on its own, slower
cadence and failures are left stale on purpose (strict mode blocks signals)."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone


class Scheduler:
    def __init__(self, rt, news_client=None, offset_seconds: float = 5.0, news_refresh_minutes: float = 30.0,
                 daily_report_hour_utc: int = 21, bot=None):
        self.rt, self.news_client, self.offset = rt, news_client, offset_seconds
        self.news_every = news_refresh_minutes * 60
        self.report_hour, self.bot = daily_report_hour_utc, bot
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.heartbeat: dict[str, str | None] = {"tick": None, "news": None}
        self.errors: list[str] = []
        self._last_report: str | None = None

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        jobs = [("gpta-tick", self._tick_loop)]
        if self.news_client:
            jobs.append(("gpta-news", self._news_loop))
        if self.bot:
            jobs.append(("gpta-telegram", self.bot.run_forever))
        for name, fn in jobs:
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        if self.bot:
            self.bot.stop()

    def status(self) -> dict:
        return {"running": not self._stop.is_set() and any(t.is_alive() for t in self._threads),
                "workers": {t.name: t.is_alive() for t in self._threads}, "heartbeat": self.heartbeat,
                "recent_errors": self.errors[-5:]}

    # -- jobs --------------------------------------------------------------------------------
    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=self.offset)
            if self._stop.wait(max(0.0, (nxt - now).total_seconds())):
                return
            self.run_once()

    def run_once(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        out = self.rt.tick(now)
        self.heartbeat["tick"] = now.isoformat()
        if "error" in out:
            self.errors.append(f"{now.isoformat()} {out['error']}")
        self._maybe_daily_report(now)
        return out

    def _news_loop(self) -> None:
        while not self._stop.is_set():
            self.refresh_news()
            if self._stop.wait(self.news_every):
                return

    def refresh_news(self) -> bool:
        try:
            events = self.news_client.fetch()
            self.rt.news.update(events)
            self.heartbeat["news"] = datetime.now(timezone.utc).isoformat()
            return True
        except Exception as e:                      # stale calendar => NewsGate reports UNAVAILABLE => no signals
            msg = f"news refresh failed: {type(e).__name__}"
            self.errors.append(msg)
            self.rt.store.log("WARN", "news", msg)
            return False

    def _maybe_daily_report(self, now: datetime) -> None:
        key = now.strftime("%Y-%m-%d")
        if now.hour != self.report_hour or self._last_report == key or not self.rt.signals.notifier:
            return
        m = self.rt.performance()
        text = (f"GPTA | DAILY PERFORMANCE REPORT {key}\n{m['label']}\ncompleted trades: {m['completed']}  "
                f"win rate: {m['win_rate']}  net R: {m['net_r']}  profit factor: {m['profit_factor']}")
        if self.rt.signals.notifier.send(f"DAILY:{key}", "DAILY PERFORMANCE REPORT", text):
            self._last_report = key
