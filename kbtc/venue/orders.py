"""Signed order writes for the event-contract exchange: create (post-only) and cancel.

Same bodies as the Kalshi repo's kalshi/client.create_order / cancel_order: side 'bid' = buy YES,
'ask' = sell YES; price and count are fixed-point strings; post_only=True so we can never take;
self_trade_prevention_type='maker'. V2 returns `order_id` flat.

WRITES NEVER RETRY. A 429 or timeout on a create may or may not have placed the order, and
retrying could double it. A failed create is reported
and the next cycle re-reads what is resting. Every call is timed: t_sign, t_sent, t_ack.

Nothing in this module sends unless `Writer(live=True)` AND the environment has KBTC_ALLOW_LIVE=1,
a two-key lock.
"""
import json
import os
import time
import uuid

import requests

from kbtc import settings
from kbtc.kalshi_root import auth
from kbtc.secrets import kalshi_credentials


class LiveLocked(RuntimeError):
    pass


class Writer:
    def __init__(self, budget=None, live=False, base=settings.REST_BASE):
        if live and os.environ.get("KBTC_ALLOW_LIVE") != "1":
            raise LiveLocked("live=True needs KBTC_ALLOW_LIVE=1 in the environment")
        self.live = live
        self.base = base.rstrip("/")
        self.prefix = "/trade-api/v2"
        self.budget = budget
        self.s = requests.Session()
        self.key_id, self.key_path = kalshi_credentials()

    def _call(self, method, path, body=None, cost=settings.ORDER_TOKEN_COST, category="write",
              params=None):
        if self.budget is not None:
            self.budget.acquire(category, cost)
        t0 = time.time()
        h = auth.auth_headers(self.key_id, self.key_path, method, self.prefix + path)
        t1 = time.time()
        r = self.s.request(method, self.base + path, headers=h, params=params,
                           data=None if body is None else json.dumps(body), timeout=5.0)
        t2 = time.time()
        timing = {"t_start": t0, "sign_ms": 1e3 * (t1 - t0), "rtt_ms": 1e3 * (t2 - t1), "t_ack": t2}
        try:
            payload = r.json()
        except ValueError:
            payload = {"raw": r.text[:200]}
        if r.status_code == 429 and self.budget is not None:
            self.budget.on_429(category)
        return r.status_code, payload, timing

    def create(self, ticker, side, price, count=settings.QUOTE_COUNT):
        coid = str(uuid.uuid4())
        body = {"ticker": ticker, "side": side, "count": count, "price": f"{price:.4f}",
                "time_in_force": "good_till_canceled", "self_trade_prevention_type": "maker",
                "post_only": True, "client_order_id": coid}
        if not self.live:
            return {"ok": True, "dry": True, "order_id": f"dry-{coid[:8]}", "body": body,
                    "timing": {"t_start": time.time(), "sign_ms": 0.0, "rtt_ms": 0.0,
                               "t_ack": time.time()}}
        st, p, tm = self._call("POST", "/portfolio/events/orders", body)
        oid = p.get("order_id") or (p.get("order") or {}).get("order_id")
        return {"ok": st in (200, 201) and bool(oid), "status": st, "order_id": oid,
                "body": body, "resp": p if st not in (200, 201) else None, "timing": tm}

    def cancel(self, order_id, exchange_index):
        """Cancel one order ON ITS SHARD. `exchange_index` is required, not defaulted.

        A cancel sent without `exchange_index` defaults to shard 0; if the market lives on another
        shard every cancel comes back 404. Treated as "already gone", the quoter would believe its
        orders were cancelled, keep placing replacements, and let resting bids pile up and fill past
        the position limit. A 404 is therefore NOT success here: the order is only
        known gone on a 2xx, and a failed cancel stays in the caller's book to be retried.
        """
        if not self.live or str(order_id).startswith("dry-"):
            return {"ok": True, "dry": True, "timing": {"t_start": time.time(), "sign_ms": 0.0,
                                                       "rtt_ms": 0.0, "t_ack": time.time()}}
        st, p, tm = self._call("DELETE", f"/portfolio/events/orders/{order_id}",
                               cost=settings.CANCEL_TOKEN_COST,
                               params={"exchange_index": int(exchange_index)})
        return {"ok": st in (200, 201, 204), "status": st, "timing": tm,
                "resp": p if st not in (200, 201, 204) else None}

    def resting(self, series=settings.QUOTE_SERIES):
        """Our resting orders on `series`. A signed GET, read rarely (start, exit, kill switch)."""
        path = "/portfolio/orders"
        h = auth.auth_headers(self.key_id, self.key_path, "GET", self.prefix + path)
        r = self.s.get(self.base + path, headers=h, params={"status": "resting", "limit": 1000},
                       timeout=5.0)
        r.raise_for_status()
        return [o for o in r.json().get("orders", []) if o.get("ticker", "").startswith(series)]

    def cancel_all(self, series=settings.QUOTE_SERIES):
        """Kill-switch path: cancel every resting order on `series`. Returns (n_ok, n_fail)."""
        ok = fail = 0
        for o in self.resting(series):
            r = self.cancel(o["order_id"], o.get("exchange_index", 0)) if self.live else {"ok": True}
            ok += bool(r["ok"])
            fail += not r["ok"]
        return ok, fail
