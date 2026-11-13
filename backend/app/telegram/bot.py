"""Telegram command listener (blueprint 16.4). Long-polls getUpdates and answers commands from the configured
chat only. It presents read-only data prepared by the runtime; it never analyses markets or decides trades."""
from __future__ import annotations

import threading
import time
from typing import Callable

from .notifier import handle_command


class TelegramBot:
    def __init__(self, token: str, chat_id: str, ctx_provider: Callable[[], dict], http_post=None, poll_seconds: int = 25):
        self.token, self.chat_id, self.ctx, self.poll = token, str(chat_id), ctx_provider, poll_seconds
        self._post = http_post or self._httpx_post
        self._offset = 0
        self._stop = threading.Event()

    def _httpx_post(self, method: str, payload: dict) -> dict:
        import httpx
        r = httpx.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=self.poll + 10)
        r.raise_for_status()
        return r.json()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> int:
        """Process one getUpdates batch; returns number of commands answered. Messages from other chats are ignored."""
        data = self._post("getUpdates", {"offset": self._offset, "timeout": self.poll, "allowed_updates": ["message"]})
        answered = 0
        for u in data.get("result", []):
            self._offset = max(self._offset, u["update_id"] + 1)
            m = u.get("message") or {}
            text = (m.get("text") or "").strip()
            if str(m.get("chat", {}).get("id")) != self.chat_id or not text.startswith("/"):
                continue
            self._post("sendMessage", {"chat_id": self.chat_id, "text": handle_command(text, self.ctx())})
            answered += 1
        return answered

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                if self._stop.wait(5):
                    return
