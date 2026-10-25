"""Record every KXBTCPERP top-of-book change and every BRTI reference print to local CSV.

    python -m kbtc.tools.record_perp_top --hours 24

Local only (var/cache/perp_top/), no S3. This is the input to the 2-second RV study: no history
of perp mids finer than 1 minute exists anywhere, so it has to be recorded.

Rows:  kind,exch_ms,rx_ms,a,b,c,d
  top  exch_ms rx_ms bid bid_size ask ask_size      (BTC/USD, contracts)
  ref  ts_ms   rx_ms brti                           (reference_price)
"""
import argparse
import os
import signal
import threading
import time
from datetime import datetime, timezone

from kbtc import settings
from kbtc.venue.perp import PerpFeed

OUT_DIR = os.path.join(os.path.dirname(settings.LOG_DIR), "cache", "perp_top")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    a = ap.parse_args(argv)
    os.makedirs(OUT_DIR, exist_ok=True)
    lock = threading.Lock()
    state = {"fh": None, "day": None, "last": None, "last_ref": None, "n": 0}

    def write(line):
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        with lock:
            if day != state["day"]:
                if state["fh"]:
                    state["fh"].close()
                state["fh"] = open(os.path.join(OUT_DIR, f"perp_top-{day}.csv"), "a", buffering=1 << 16)
                state["day"] = day
            state["fh"].write(line)
            state["n"] += 1

    def on_book(feed, rx):
        t = feed.top_usd()
        if t is None:
            return
        key = t[:4]
        if key == state["last"]:
            return
        state["last"] = key
        write(f"top,{t[4]},{int(rx * 1000)},{t[0]:.0f},{t[1]:g},{t[2]:.0f},{t[3]:g}\n")

    def on_ticker(feed, tk):
        r = tk.get("reference")
        if r is None or r.ts_ms == state["last_ref"]:
            return
        state["last_ref"] = r.ts_ms
        write(f"ref,{r.ts_ms},{int(tk['rx'] * 1000)},{r.value:.2f},,,\n")

    feed = PerpFeed(on_book=on_book, on_ticker=on_ticker).start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    t_end = time.time() + 3600 * a.hours
    while not stop.is_set() and time.time() < t_end:
        stop.wait(60)
        with lock:
            if state["fh"]:
                state["fh"].flush()
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] rows {state['n']} gaps {feed.gaps} "
              f"reconnects {feed.reconnects}", flush=True)
    feed.stop()
    with lock:
        if state["fh"]:
            state["fh"].close()


if __name__ == "__main__":
    main()
