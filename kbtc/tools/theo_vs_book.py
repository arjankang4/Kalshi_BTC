"""Where do our BTC theos land against the Kalshi book?  Read-only; sends no orders.

    python -m kbtc.tools.theo_vs_book --minutes 30
    python -m kbtc.tools.theo_vs_book --minutes 30 --sigma 0.40      # flat-vol override

Streams KXBTCPERP over the margin WebSocket into the fast-spot estimator, polls the open
KXBTC15M / KXBTCD / KXBTC markets over REST inside this process's share of the event-contract
read budget, prices each with the 60-s-average digital, and logs one row per market per poll:

    where = below_bid | inside | above_ask | one_sided | no_book

Rows go to var/log/theo_vs_book-<UTC date>.jsonl; a per-event summary prints every
--summary-s. Rows priced while the spot is flagged unreliable carry `reliable: false` and are
left out of the summary.
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from kbtc import settings
from kbtc.pricing import digital
from kbtc.pricing.spot import SpotEstimator
from kbtc.rvol.ewma import ewma_sigma
from kbtc.venue import ratebudget
from kbtc.venue.events import EventRest, priceable
from kbtc.venue.perp import PerpFeed, PerpRest
from kbtc.venue.rest import RestClient

NEAR = (0.03, 0.97)          # summary is restricted to theos in this band; the tails say little


class Monitor:
    def __init__(self, sigma_override=None):
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        os.makedirs(settings.LOG_DIR, exist_ok=True)
        self.rows_path = os.path.join(settings.LOG_DIR, f"theo_vs_book-{day}.jsonl")
        usage = os.path.join(settings.LOG_DIR, f"rate_usage-{day}.jsonl")

        probe = RestClient()
        try:
            ev_lim = probe.account_limits(perps=False)
            pp_lim = probe.account_limits(perps=True)
        except Exception as e:                                    # noqa: BLE001
            print(f"[limits] could not read account limits ({e}); using fallbacks")
            ev_lim = pp_lim = None
        self.event_budget = ratebudget.from_limits(
            "event_read", ev_lim, settings.EVENT_READ_FALLBACK, settings.EVENT_READ_SHARE,
            settings.EVENT_CATEGORY_WEIGHTS, log_path=usage)
        self.perp_budget = ratebudget.from_limits(
            "perp_read", pp_lim, settings.PERP_READ_FALLBACK, settings.PERP_READ_SHARE,
            settings.PERP_CATEGORY_WEIGHTS, log_path=usage)
        print(f"[limits] event read {self.event_budget.account} share {settings.EVENT_READ_SHARE}"
              f" | perp read {self.perp_budget.account} share {settings.PERP_READ_SHARE}")

        self.perp = PerpRest(RestClient(self.perp_budget))
        self.events = EventRest(RestClient(self.event_budget))
        mk = self.perp.market()                     # also asserts contract_size
        print(f"[perp] {mk['ticker']} {mk['status']} bid {mk['bid']:.0f} ask {mk['ask']:.0f} "
              f"BRTI {mk['reference'].value:.2f}")

        self.spot = SpotEstimator()
        self.spot_lock = threading.Lock()
        self.brti = deque(maxlen=600)               # (ts_s, price) BRTI prints, for the window
        self.sigma_override = sigma_override
        self.sigma, self.sigma_t = None, 0.0
        self.feed = PerpFeed(on_book=self._on_book, on_ticker=self._on_ticker)
        self.stats = defaultdict(lambda: defaultdict(float))
        self.n_rows = 0

    # -- perp callbacks (WS thread) -----------------------------------------------------------
    def _on_book(self, feed, rx):
        top = feed.top_usd()
        if top is not None:
            with self.spot_lock:
                self.spot.on_book(*top)

    def _on_ticker(self, feed, t):
        ref = t.get("reference")
        with self.spot_lock:
            if ref is not None and self.spot.on_reference(ref.value, ref.ts_ms):
                self.brti.append((ref.ts_ms / 1000.0, ref.value))
            if t.get("funding_rate") is not None:
                self.spot.on_funding(t["funding_rate"])

    # -- sigma ----------------------------------------------------------------------------------
    def refresh_sigma(self, now):
        if self.sigma_override is not None:
            self.sigma = self.sigma_override
            return
        if self.sigma is not None and now - self.sigma_t < settings.VOL_REFRESH_S:
            return
        self.sigma_t = now                   # a failed fetch waits a full refresh, not one loop
        mids = self.perp.candles_1m(now - settings.VOL_LOOKBACK_MIN * 60, now)
        s = ewma_sigma(mids)
        if s is not None:
            self.sigma = s
            print(f"[vol] EWMA sigma {s:.3f} from {len(mids)} 1-min perp candles")
        else:
            print(f"[vol] no sigma from {len(mids)} candles; keeping {self.sigma}")

    def observed_in_window(self, close_ts):
        lo = close_ts - settings.SETTLE_AVG_WINDOW_S
        with self.spot_lock:
            return [p for t, p in self.brti if lo <= t < close_ts]

    # -- one poll of one series -------------------------------------------------------------------
    def poll(self, series):
        markets = self.events.open_markets(series)
        now = time.time()
        with self.spot_lock:
            st = self.spot.state(now)
        out = []
        for mk in markets:
            ok, why = priceable(mk, now)
            tau = mk["close_ts"] - now
            if not ok or tau < settings.MIN_TAU_S or st.s_hat is None or self.sigma is None:
                continue
            obs = self.observed_in_window(mk["close_ts"]) if tau < settings.SETTLE_AVG_WINDOW_S else None
            fair, det = digital.market_theo(mk, st.s_hat, self.sigma, now, obs)
            if fair is None:
                continue
            b, a = mk["yes_bid"], mk["yes_ask"]
            has_b, has_a = b is not None and b > 0, a is not None and 0 < a < 1
            if has_b and has_a:
                where = "below_bid" if fair < b else ("above_ask" if fair > a else "inside")
                mid = 0.5 * (a + b)
            else:
                where, mid = ("one_sided" if (has_b or has_a) else "no_book"), None
            row = {"ts": round(now, 3), "ticker": mk["ticker"], "event": mk["event_ticker"],
                   "series": series, "strike_type": mk["strike_type"],
                   "floor": mk["floor_strike"], "cap": mk["cap_strike"],
                   "tau_s": round(tau, 1), "s_hat": round(st.s_hat, 2), "ref": st.ref,
                   "basis": None if st.basis is None else round(st.basis, 2),
                   "reliable": st.reliable, "reasons": st.reasons, "sigma": round(self.sigma, 4),
                   "theo": round(fair, 4), "yes_bid": b, "yes_ask": a,
                   "bid_size": mk["yes_bid_size"], "ask_size": mk["yes_ask_size"],
                   "where": where,
                   "theo_minus_mid_c": None if mid is None else round(100 * (fair - mid), 2),
                   "n_window_prints": None if obs is None else len(obs)}
            out.append(row)
            if st.reliable and NEAR[0] <= fair <= NEAR[1]:
                s = self.stats[mk["event_ticker"]]
                s["n"] += 1
                s[where] += 1
                if mid is not None:
                    s["sum_dmid_c"] += row["theo_minus_mid_c"]
                    s["sum_absdmid_c"] += abs(row["theo_minus_mid_c"])
                    s["n_mid"] += 1
                    s["sum_halfspread_c"] += 50 * (a - b)
        with open(self.rows_path, "a") as fh:
            for r in out:
                fh.write(json.dumps(r) + "\n")
        self.n_rows += len(out)
        return st

    def summary(self):
        with self.spot_lock:
            st = self.spot.state(time.time())
        print(f"\n[{datetime.now(timezone.utc):%H:%M:%S}Z] S_hat {st.s_hat and round(st.s_hat, 1)} "
              f"BRTI {st.ref} basis {st.basis and round(st.basis, 2)} dev {st.dev_bps and round(st.dev_bps, 2)}bps "
              f"reliable {st.reliable} {st.reasons} sigma {self.sigma and round(self.sigma, 3)} "
              f"ws gaps {self.feed.gaps} reconnects {self.feed.reconnects} rows {self.n_rows}")
        print(f"  {'event':28s} {'n':>5s} {'below':>6s} {'inside':>6s} {'above':>6s} "
              f"{'1side':>6s} {'theo-mid c':>10s} {'|theo-mid| c':>12s} {'half-spr c':>10s}")
        for ev in sorted(self.stats):
            s = self.stats[ev]
            nm = s["n_mid"] or 1
            print(f"  {ev:28s} {int(s['n']):5d} {int(s['below_bid']):6d} {int(s['inside']):6d} "
                  f"{int(s['above_ask']):6d} {int(s['one_sided']):6d} {s['sum_dmid_c'] / nm:10.2f} "
                  f"{s['sum_absdmid_c'] / nm:12.2f} {s['sum_halfspread_c'] / nm:10.2f}")
        for b in (self.event_budget, self.perp_budget):
            r = b.report(max(1e-9, time.monotonic() - b._last_log))
            print(f"  [{r['budget']}] {r['total_tokens_per_s']} tok/s = {100 * r['account_used']:.1f}% "
                  f"of account, 429s {sum(r['n429'].values())}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--sigma", type=float, default=settings.VOL_OVERRIDE_ANN)
    ap.add_argument("--series", default=",".join(settings.EVENT_SERIES))
    ap.add_argument("--summary-s", type=float, default=60)
    a = ap.parse_args(argv)

    m = Monitor(sigma_override=a.sigma)
    m.feed.start()
    if not m.feed.wait_ready(20):
        print("[perp] WS not ready after 20 s", file=sys.stderr)
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    series = [s for s in a.series.split(",") if s]
    next_poll = {s: 0.0 for s in series}
    t_end = time.time() + 60 * a.minutes
    next_summary = time.time() + a.summary_s
    # Let the basis warm up before the first poll counts.
    while not stop.is_set() and m.spot.n_basis < settings.MIN_BASIS_SAMPLES and time.time() < t_end:
        time.sleep(0.5)
    while not stop.is_set() and time.time() < t_end:
        now = time.time()
        try:
            m.refresh_sigma(now)
            for s in series:
                if now >= next_poll[s]:
                    m.poll(s)
                    next_poll[s] = now + settings.POLL_S.get(s, 5.0)
        except Exception as e:                                    # noqa: BLE001
            print(f"[poll] {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
            time.sleep(1.0)
        if now >= next_summary:
            m.summary()
            next_summary = now + a.summary_s
        time.sleep(0.1)
    m.summary()
    m.feed.stop()
    print(f"\nrows -> {m.rows_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
