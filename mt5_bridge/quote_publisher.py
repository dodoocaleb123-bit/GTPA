"""POSTs quotes to the GPTA backend. On any failure it just retries next poll; the backend treats missing/stale
quotes as spread-check UNAVAILABLE/FAIL, so a dead bridge can never produce a passing signal."""
from __future__ import annotations


class QuotePublisher:
    def __init__(self, base_url: str, token: str, post=None):
        self.url, self.token, self._post = base_url + "/api/broker/quote", token, post

    def publish(self, quote: dict) -> bool:
        post = self._post
        if post is None:
            import requests
            post = requests.post
        try:
            r = post(self.url, json=quote, headers={"Authorization": f"Bearer {self.token}"}, timeout=3)
            return r.status_code == 200
        except Exception:
            return False
