"""Live feed for the promoted intraday RV model (kbtc.rvol.intraday_forecast, har_tow_log_smear).

The model is fitted on Coinbase BTC-USD 1-minute trade closes, so it is FED Coinbase 1-minute
closes live -- the same venue it was fitted on, venue_scale = 1.0, explicitly (the fit's author:
another venue's closes shift the level by that venue's 1m RV ratio, and that must never be
estimated silently).

  * Seed: the last 8 days from the local Coinbase 1m cache, topped up to now over REST.
  * Top-up: a background thread fetches new closed minutes every TOPUP_S. Coinbase's public
    candle limit is ~10 req/s; one request a minute is nothing.
  * forecast(now) is cheap (numpy over ~11.5k minutes) and runs on the quoter's slow cycle;
    its value only moves when a new minute closes.
  * ok=False from the model (coverage < 90% on any window, zero RV) is surfaced as "no sigma",
    never as zero -- the quoter then refuses, it does not price at a stale or empty vol.

Realised-vs-forecast is journaled by the quoter (`rv_live` records), which is what a future
venue_scale would be set from, by hand.
"""
import datetime as dt
import threading
import time

import numpy as np

from kbtc.data import coinbase
from kbtc.rvol import intraday_forecast as IF

SEED_DAYS = 8
TOPUP_S = 30.0
VENUE_SCALE = 1.0


class LiveIntraday:
    def __init__(self, art=None, now=None):
        self.art = art or IF.load()
        self.lock = threading.Lock()
        self.closes = {}                       # bucket_start_s -> close
        self.errors = 0
        self.last_topup = None
        self._stop = threading.Event()
        now = int(now or time.time())
        start = now - SEED_DAYS * 86400
        d0 = dt.datetime.fromtimestamp(start, dt.timezone.utc).date()
        try:
            df = coinbase.load(60, start=d0)
            df = df[df["ts_s"] >= start]
            for t, c in zip(df["ts_s"].to_numpy(), df["close_usd"].to_numpy()):
                self.closes[int(t)] = float(c)
        except (FileNotFoundError, ValueError):
            pass
        self.topup(now)

    def topup(self, now=None):
        now = int(now or time.time()) // 60 * 60
        lo = (max(self.closes) + 60) if self.closes else now - SEED_DAYS * 86400
        if lo >= now:
            return 0
        try:
            df = coinbase.fetch_range(60, lo, now)
        except Exception:                                             # noqa: BLE001
            self.errors += 1
            return 0
        with self.lock:
            for t, c in zip(df["ts_s"].to_numpy(), df["close_usd"].to_numpy()):
                self.closes[int(t)] = float(c)
            cut = now - (SEED_DAYS + 1) * 86400
            for t in [t for t in self.closes if t < cut]:
                del self.closes[t]
        self.last_topup = time.time()
        return len(df)

    def start(self):
        def loop():
            while not self._stop.wait(TOPUP_S):
                self.topup()
        threading.Thread(target=loop, name="rv-topup", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def forecast(self, now=None):
        with self.lock:
            arr = np.array(sorted(self.closes.items()), dtype="float64")
        out = IF.forecast(self.art, now or time.time(), arr, venue_scale=VENUE_SCALE)
        out["n_closes"] = len(arr)
        out["last_close_age_s"] = None if not len(arr) else (now or time.time()) - (arr[-1, 0] + 60)
        return out
