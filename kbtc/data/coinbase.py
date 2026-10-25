"""Coinbase BTC-USD public candles, cached by month. External history for the realized-vol studies.

    python -m kbtc.data.coinbase --gran 300 --start 2016-01-01      # 5-minute
    python -m kbtc.data.coinbase --gran 60  --start 2022-01-01      # 1-minute
    python -m kbtc.data.coinbase --gran 60  --start 2022-01-01 --product ETH-USD  # -> var/cache/coinbase_eth

WHY COINBASE. The perp (KXBTCPERP) only listed 2026-06-03, far too short for 7/30-day HAR windows
or any regime check. Coinbase is a BRTI constituent with history to 2015, and its candle endpoint
is public (no key). It is ONE venue: BRTI averages several, so levels can differ slightly.

Endpoint: GET /products/BTC-USD/candles?granularity=G&start=ISO&end=ISO -> at most 300 rows of
[time, low, high, open, close, volume], newest first. A bucket with no trades is OMITTED, not
zero-filled -- the cache keeps those gaps; a return across one spans several intervals.

Cache: var/cache/coinbase/{5m,1m}/ym=YYYY-MM.parquet with columns
    ts_s (bucket start, UTC seconds), open_usd, high_usd, low_usd, close_usd, volume_btc
A month is written only when complete (all its requests succeeded) and is skipped on re-run,
except the current month, which is always refetched.
"""
import argparse
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

import pandas as pd

BASE_FMT = "https://api.exchange.coinbase.com/products/{product}/candles"
PRODUCT = "BTC-USD"
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     "var", "cache")
ROOT = os.path.join(CACHE, "coinbase")          # BTC-USD (the default product)


def root_for(product=PRODUCT):
    """BTC-USD keeps var/cache/coinbase; other products get var/cache/coinbase_<base>, e.g. coinbase_eth."""
    return ROOT if product == PRODUCT else os.path.join(CACHE, "coinbase_" + product.split("-")[0].lower())
LABEL = {300: "5m", 60: "1m"}
MAX_ROWS = 300
MIN_INTERVAL_S = 0.34                   # public limit is ~10 req/s; stay at ~3
COLS = ["ts_s", "low_usd", "high_usd", "open_usd", "close_usd", "volume_btc"]

_last = [0.0]


def _get(params, tries=6, product=PRODUCT):
    url = BASE_FMT.format(product=product) + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    delay = 1.0
    for _ in range(tries):
        wait = MIN_INTERVAL_S - (time.monotonic() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kbtc-research"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError(f"gave up on {url}")


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_range(gran, lo, hi, product=PRODUCT):
    """All candles with lo <= ts < hi, oldest first."""
    rows = {}
    step = gran * MAX_ROWS
    t = lo
    while t < hi:
        e = min(t + step, hi)
        # Coinbase's `end` is inclusive; ask for [t, e - gran] so windows do not overlap.
        body = _get({"granularity": gran, "start": _iso(t), "end": _iso(e - gran)}, product=product)
        if isinstance(body, dict):
            raise RuntimeError(f"coinbase error: {body}")
        for r in body:
            if lo <= r[0] < hi:
                rows[int(r[0])] = r
        t = e
    df = pd.DataFrame([rows[k] for k in sorted(rows)], columns=COLS)
    return df[["ts_s", "open_usd", "high_usd", "low_usd", "close_usd", "volume_btc"]]


def month_bounds(y, m):
    lo = dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
    hi = dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=dt.timezone.utc)
    return int(lo.timestamp()), int(hi.timestamp())


def path(gran, y, m, product=PRODUCT):
    return os.path.join(root_for(product), LABEL[gran], f"ym={y:04d}-{m:02d}.parquet")


def sync(gran, start, end=None, log=print, product=PRODUCT):
    end = end or dt.date.today()
    y, m = start.year, start.month
    now = dt.datetime.now(dt.timezone.utc)
    while (y, m) <= (end.year, end.month):
        p = path(gran, y, m, product)
        current = (y, m) == (now.year, now.month)
        if not os.path.exists(p) or current:
            lo, hi = month_bounds(y, m)
            hi = min(hi, int(now.timestamp()) // gran * gran)
            df = fetch_range(gran, lo, hi, product)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            df.to_parquet(p + ".tmp", index=False)
            os.replace(p + ".tmp", p)
            expect = (hi - lo) // gran
            log(f"{LABEL[gran]} {y}-{m:02d}: {len(df)} rows ({len(df) / max(expect, 1):.1%} of buckets)")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def load(gran, start=None, end=None, product=PRODUCT):
    d = os.path.join(root_for(product), LABEL[gran])
    ps = sorted(os.listdir(d))
    if start:
        ps = [p for p in ps if p[3:10] >= f"{start.year:04d}-{start.month:02d}"]
    if end:
        ps = [p for p in ps if p[3:10] <= f"{end.year:04d}-{end.month:02d}"]
    parts = [pd.read_parquet(os.path.join(d, p)) for p in ps]
    df = pd.concat([x for x in parts if len(x)] or parts[:1], ignore_index=True)   # months before listing are empty
    for c in df.columns:
        df[c] = pd.to_numeric(df[c]) if df[c].dtype == object else df[c]
    return df.sort_values("ts_s").drop_duplicates("ts_s").reset_index(drop=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--gran", type=int, choices=sorted(LABEL), required=True)
    ap.add_argument("--start", type=dt.date.fromisoformat, required=True)
    ap.add_argument("--end", type=dt.date.fromisoformat)
    ap.add_argument("--product", default=PRODUCT)
    a = ap.parse_args(argv)
    sync(a.gran, a.start, a.end, product=a.product)


if __name__ == "__main__":
    main()
