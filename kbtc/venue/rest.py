"""Kalshi REST GET with a rate budget, 429 backoff and optional signing.

Market data on both exchanges is public; signing is only for account endpoints (limits).
GETs are idempotent, so they retry on 429 and 5xx. There is no write path in kbtc.
"""
import time
import urllib.parse

import requests

from kbtc import settings
from kbtc.kalshi_root import auth
from kbtc.secrets import kalshi_credentials


class KalshiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


class RestClient:
    def __init__(self, budget=None, base=settings.REST_BASE, timeout=5.0, session=None):
        self.base = base.rstrip("/")
        self.path_prefix = urllib.parse.urlparse(self.base).path      # /trade-api/v2
        self.budget = budget
        self.timeout = timeout
        self.s = session or requests.Session()

    def get(self, path, params=None, category=None, signed=False):
        url = self.base + path
        for attempt in range(settings.GET_RETRIES + 1):
            if self.budget is not None and category is not None:
                self.budget.acquire(category)
            headers = {}
            if signed:
                key_id, key_path = kalshi_credentials()
                headers = auth.auth_headers(key_id, key_path, "GET", self.path_prefix + path)
            t0 = time.time()
            r = self.s.get(url, params=params, headers=headers, timeout=self.timeout)
            if r.status_code == 200:
                body = r.json()
                body["_rx"] = {"t_send": t0, "t_recv": time.time()}
                return body
            if r.status_code == 429 and self.budget is not None and category is not None:
                self.budget.on_429(category)          # acquire() serves the backoff
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(settings.BACKOFF_429_MAX_S, settings.BACKOFF_429_S * 2 ** attempt))
                continue
            raise KalshiError(r.status_code, r.text)
        raise KalshiError(r.status_code, r.text)

    def account_limits(self, perps=False):
        return self.get("/account/limits/perps" if perps else "/account/limits", signed=True)
