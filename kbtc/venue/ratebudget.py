"""Request-rate budget: one token bucket per Kalshi exchange instance, split into categories.

Kalshi meters the event-contract and perps exchanges in separate read/write buckets, per account
(docs: getting_started/rate_limits). A process gets a SHARE of the account's read refill rate;
that share is split across request categories by weight, each with its own sub-bucket. This
keeps a burst of one category (book resync after a gap) from starving another (funding).

Usage is counted per category and written as a JSONL line every `settings.USAGE_LOG_EVERY_S`,
against both this process's allotment and the account limit. 429s carry no Retry-After header
and no penalty (docs), so the response is exponential backoff on the category that hit it.
"""
import json
import os
import threading
import time

from kbtc import settings


class Bucket:
    """Classic token bucket. `clock` is injectable for tests."""

    def __init__(self, refill_per_s, capacity, clock=time.monotonic):
        self.refill = float(refill_per_s)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.clock = clock
        self.t = clock()

    def _top_up(self):
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.refill)
        self.t = now

    def wait_time(self, cost):
        """Seconds until `cost` tokens are available (0 if now). Does not take them."""
        self._top_up()
        if cost > self.capacity:
            raise ValueError(f"cost {cost} exceeds bucket capacity {self.capacity}")
        short = cost - self.tokens
        # Tolerance, not `<= 0`: after sleeping exactly short/refill, float rounding can leave a
        # 1e-15 shortfall whose wait no longer advances a clock at t ~ 1e2..1e9 -- a livelock.
        return 0.0 if short <= 1e-9 else short / self.refill

    def take(self, cost):
        self._top_up()
        self.tokens -= cost


class RateBudget:
    """Weighted per-category sub-buckets over a share of one account bucket."""

    def __init__(self, name, account_refill, account_capacity, share, weights,
                 clock=time.monotonic, sleep=time.sleep, log_path=None):
        if not 0 < share <= 1:
            raise ValueError("share must be in (0, 1]")
        total_w = float(sum(weights.values()))
        self.name = name
        self.account = (float(account_refill), float(account_capacity))
        self.share = share
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        cost = settings.REQUEST_COST
        self.buckets, self.alloc = {}, {}
        for cat, w in weights.items():
            refill = account_refill * share * w / total_w
            # Capacity: the same seconds-of-burst as the account bucket, and never below one
            # request -- a sub-bucket that cannot hold one request can never serve one.
            cap = max(cost, account_capacity * share * w / total_w)
            self.buckets[cat] = Bucket(refill, cap, clock)
            self.alloc[cat] = refill
        self.used = {c: 0 for c in weights}
        self.n429 = {c: 0 for c in weights}
        self.backoff = {c: 0.0 for c in weights}
        self.log_path = log_path
        self._last_log = clock()
        self._window_used = {c: 0 for c in weights}

    def acquire(self, category, cost=settings.REQUEST_COST):
        """Block until `category` may spend `cost` tokens, then spend them."""
        b = self.buckets[category]
        while True:
            with self.lock:
                w = max(b.wait_time(cost), self.backoff[category])
                if w <= 0:
                    b.take(cost)
                    self.used[category] += cost
                    self._window_used[category] += cost
                    self._maybe_log()
                    return
                self.backoff[category] = 0.0      # a backoff is served once, then re-measured
            self.sleep(w)

    def on_429(self, category):
        with self.lock:
            self.n429[category] += 1
            prev = self.backoff[category]
            self.backoff[category] = min(settings.BACKOFF_429_MAX_S,
                                         max(settings.BACKOFF_429_S, prev * 2))
            # Drain the sub-bucket too: the exchange just told us our local view was optimistic.
            self.buckets[category].tokens = min(self.buckets[category].tokens, 0.0)
            return self.backoff[category]

    def report(self, elapsed_s):
        per_s = {c: u / elapsed_s for c, u in self._window_used.items()} if elapsed_s > 0 else {}
        tot = sum(per_s.values())
        return {"budget": self.name, "window_s": round(elapsed_s, 2),
                "tokens_per_s": {c: round(v, 2) for c, v in per_s.items()},
                "total_tokens_per_s": round(tot, 2),
                "alloc_tokens_per_s": {c: round(v, 2) for c, v in self.alloc.items()},
                "share_used": round(tot / (self.account[0] * self.share), 4),
                "account_used": round(tot / self.account[0], 4),
                "n429": dict(self.n429)}

    def _maybe_log(self):
        now = self.clock()
        el = now - self._last_log
        if el < settings.USAGE_LOG_EVERY_S:
            return
        rec = self.report(el)
        rec["ts"] = time.time()
        self._last_log = now
        self._window_used = {c: 0 for c in self._window_used}
        if self.log_path:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")


def from_limits(name, limits, fallback, share, weights, **kw):
    """Build a RateBudget from a GET /account/limits[/perps] body, else the fallback pair."""
    try:
        refill = float(limits["read"]["refill_rate"])
        cap = float(limits["read"]["bucket_capacity"])
    except (TypeError, KeyError, ValueError):
        refill, cap = fallback
    return RateBudget(name, refill, cap, share, weights, **kw)
