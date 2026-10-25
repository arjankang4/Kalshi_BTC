"""Rate budget: weighted split, blocking, 429 backoff, usage report. Fake clock, no sleeping."""
from kbtc.kalshi_root import harness as H
from kbtc.venue.ratebudget import Bucket, RateBudget, from_limits


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.t += d


c = Clock()
rb = RateBudget("t", 300, 900, 0.10, {"a": 3, "b": 1}, clock=c, sleep=c.sleep)
H.approx("a gets 3/4 of 30 tok/s", rb.alloc["a"], 22.5)
H.approx("b gets 1/4", rb.alloc["b"], 7.5)

print("sustained rate converges to the allotment")
for _ in range(200):
    rb.acquire("b")
rate = rb.used["b"] / c.t
H.ok("b tokens/s", rate, 7.5, 0.5)

print("categories do not starve each other")
t0 = c.t
rb.acquire("a")
H.ok("a not delayed by b's backlog", c.t - t0, 0.0, 1.0)

print("429 backoff doubles and is served once")
w1 = rb.on_429("a")
w2 = rb.on_429("a")
H.check("doubles", w2, 2 * w1)
t0 = c.t
rb.acquire("a")
H.assert_true("acquire waited at least the backoff", c.t - t0 >= w2 - 1e-9, f"{c.t - t0}")

print("capacity guard")
try:
    Bucket(1, 5).wait_time(10)
    H.assert_true("cost above capacity raises", False)
except ValueError:
    H.assert_true("cost above capacity raises", True)

print("from_limits reads the account body, else falls back")
rb2 = from_limits("p", {"read": {"refill_rate": 400, "bucket_capacity": 400}}, (1, 1), 0.5, {"x": 1})
H.check("limits body", rb2.account, (400.0, 400.0))
rb3 = from_limits("p", None, (300, 900), 0.5, {"x": 1})
H.check("fallback", rb3.account, (300.0, 900.0))
H.finish()
