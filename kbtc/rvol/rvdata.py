"""Shared daily realized-measure panel for the BTC realized-vol model comparison (#1-#4).

    python -m kbtc.rvol.rvdata            # 5m Coinbase cache -> var/cache/rv_daily.parquet

DAY = the 24 hours ending 17:00 America/New_York (the KXBTCD daily close), labelled by the ET date
on which it ends. A return belongs to the day in which it ENDS (bucket start + width). DST is
handled by zoneinfo, so a day is 23h or 25h twice a year; `span_h` records it.

Returns are close-to-close log returns between CONSECUTIVE cached 5-minute candles. Coinbase omits
buckets with no trades, so a return may span several intervals; it is kept as one return (the sum
of squares over the day is still the day's quadratic variation estimate). `n_gap` counts them.

Columns (all per day, variances NOT annualised):
    rv       sum r^2
    rq       n/3 * sum r^4                (realized quarticity; n = n_ret, the returns actually formed)
    rs_pos   sum r^2 over r > 0            (rs_pos + rs_neg == rv exactly)
    rs_neg   sum r^2 over r < 0
    bv       (pi/2) * sum |r_i||r_{i-1}|  (bipower variation, adjacent returns within the day)
    r_day    sum r                        (close-to-close log return, for GARCH)
    n_ret, n_gap, span_h, coverage       (coverage = seconds spanned by the day's returns / day length)
    complete  coverage >= MIN_COVERAGE and not the first/last (partial) day in the cache
"""
import math
import os
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from kbtc.data import coinbase

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(coinbase.ROOT)
DAILY_PATH = os.path.join(ROOT, "rv_daily.parquet")
MIN_COVERAGE = 0.95
GRAN_S = 300


def returns(candles, gran=GRAN_S):
    """Consecutive-candle log returns. end_s = bucket end of the later candle; gap = spans > 1 bucket."""
    ts = candles["ts_s"].to_numpy(dtype="int64")
    c = candles["close_usd"].to_numpy(dtype="float64")
    r = np.diff(np.log(c))
    return pd.DataFrame({"start_s": ts[:-1] + gran, "end_s": ts[1:] + gran, "r": r,
                         "gap": (np.diff(ts) > gran)})


def day_label(end_s):
    """ET date of the 17:00 close that ends the day containing instant end_s (end-inclusive)."""
    t = pd.to_datetime(end_s, unit="s", utc=True).tz_convert(ET)
    return (t + pd.Timedelta(hours=7) - pd.Timedelta(seconds=1)).date


def day_bounds(d):
    """(start_s, end_s) of the day labelled d."""
    import datetime as dt
    hi = dt.datetime.combine(d, dt.time(17), tzinfo=ET)
    lo = dt.datetime.combine(d - dt.timedelta(days=1), dt.time(17), tzinfo=ET)
    return int(lo.timestamp()), int(hi.timestamp())


def daily_panel(rets):
    rets = rets.copy()
    rets["day"] = day_label(rets["end_s"].to_numpy())
    out = []
    for d, g in rets.groupby("day", sort=True):
        r = g["r"].to_numpy()
        n = len(r)
        a = np.abs(r)
        lo, hi = day_bounds(d)
        cov_s = np.minimum(g["end_s"].to_numpy(), hi) - np.maximum(g["start_s"].to_numpy(), lo)
        out.append({
            "day": d,
            "rv": float(np.sum(r * r)),
            "rq": float(n / 3.0 * np.sum(r ** 4)),
            "rs_pos": float(np.sum(r[r > 0] ** 2)),
            "rs_neg": float(np.sum(r[r < 0] ** 2)),
            "bv": float(math.pi / 2.0 * np.sum(a[1:] * a[:-1])) if n > 1 else float("nan"),
            "r_day": float(np.sum(r)),
            "n_ret": n,
            "n_gap": int(g["gap"].sum()),
            "span_h": (hi - lo) / 3600.0,
            "coverage": float(np.clip(cov_s, 0, None).sum() / (hi - lo)),
        })
    p = pd.DataFrame(out)
    p["complete"] = p["coverage"] >= MIN_COVERAGE
    p.loc[p.index[[0, -1]], "complete"] = False        # first/last day in the cache are partial
    p["day"] = pd.to_datetime(p["day"])
    return p


def build(write=True):
    rets = returns(coinbase.load(GRAN_S))
    p = daily_panel(rets)
    if write:
        p.to_parquet(DAILY_PATH, index=False)
    return p


def load_daily(complete_only=True):
    p = pd.read_parquet(DAILY_PATH)
    p = p[p["complete"]].reset_index(drop=True) if complete_only else p
    return add_har_features(p)


def add_har_features(p, windows=(("d", 1), ("w", 7), ("m", 30))):
    """rv_d / rv_w / rv_m = mean daily rv over the last 1 / 7 / 30 days INCLUDING day t.

    NaN unless all of those days are present and contiguous, so every model sees the same rows.
    The 24/7 windows (7, 30) replace the equity 5/22 (Corsi 2009) because BTC trades every day.
    """
    p = p.sort_values("day").reset_index(drop=True)
    s = p.set_index("day")["rv"].asfreq("D")          # missing days become NaN
    for name, n in windows:
        m = s.rolling(n, min_periods=n).mean()
        p["rv_" + name] = m.reindex(p["day"]).to_numpy()
    return p


if __name__ == "__main__":
    p = build()
    print(len(p), "days;", int(p["complete"].sum()), "complete;", p["day"].min().date(), "->",
          p["day"].max().date())


# ---- intraday panel (1-minute returns, one row per 15-minute origin) ----------------------------

INTRADAY_PATH = os.path.join(ROOT, "rv_intraday.parquet")
FEATURE_WINDOWS_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440, "7d": 10080}
TARGET_WINDOWS_MIN = {"15m": 15, "1h": 60}
ORIGIN_STEP_MIN = 15
MIN_WINDOW_COVERAGE = 0.90


def intraday_panel(candles_1m=None, write=False):
    """One row per 15-minute UTC origin t (:00/:15/:30/:45) from Coinbase 1-minute candles.

    rv_<w>  = sum r^2 over (t - w, t]            for w in FEATURE_WINDOWS_MIN
    rq_1h, rs_pos_1h, rs_neg_1h, bv_1h           same definitions as the daily panel, over (t-1h, t]
    y_15m, y_1h = sum r^2 over (t, t + h]         (targets; evalkit reads these)
    cov_<w>, ycov_<h>  window coverage >= MIN_WINDOW_COVERAGE (seconds spanned by returns / window)
    tow     15-minute bucket of the week in America/New_York, 0..671 (Mon 00:00 = 0)
    A return is assigned to the minute its later candle's bucket ENDS; one spanning a gap counts
    once, at its end.
    """
    if candles_1m is None:
        candles_1m = coinbase.load(60)
    rt = returns(candles_1m, gran=60)
    t0 = int(rt["end_s"].min() // 60 * 60)
    t1 = int(rt["end_s"].max() // 60 * 60)
    n = (t1 - t0) // 60 + 1
    pos = ((rt["end_s"].to_numpy() - t0) // 60).astype(np.int64)
    r = rt["r"].to_numpy()
    span = (rt["end_s"] - rt["start_s"]).to_numpy(dtype="float64")
    a = np.abs(r)
    adj = np.r_[0.0, a[1:] * a[:-1]]

    def cum(v):
        x = np.zeros(n + 1)
        np.add.at(x, pos + 1, v)
        return np.cumsum(x)                     # C[k] = sum of values ending at minute index < k

    C = {"r2": cum(r * r), "r4": cum(r ** 4), "pos": cum(np.where(r > 0, r * r, 0.0)),
         "neg": cum(np.where(r < 0, r * r, 0.0)), "adj": cum(adj),
         "cnt": cum(np.ones_like(r)), "span": cum(span)}
    first = ((t0 + ORIGIN_STEP_MIN * 60 - 1) // (ORIGIN_STEP_MIN * 60)) * ORIGIN_STEP_MIN * 60
    origins = np.arange(first, t1 + 1, ORIGIN_STEP_MIN * 60, dtype=np.int64)
    k = (origins - t0) // 60 + 1                # C[k] covers returns ending at or before origin

    def win(key, lo_min, hi_min):
        lo = np.clip(k + lo_min, 0, n)
        hi = np.clip(k + hi_min, 0, n)
        ok = (k + lo_min >= 0) & (k + hi_min <= n)
        v = C[key][hi] - C[key][lo]
        return np.where(ok, v, np.nan)

    out = {"origin": pd.to_datetime(origins, unit="s", utc=True)}
    for name, w in FEATURE_WINDOWS_MIN.items():
        out["rv_" + name] = win("r2", -w, 0)
        out["cov_" + name] = win("span", -w, 0) / (60.0 * w) >= MIN_WINDOW_COVERAGE
    cnt = win("cnt", -60, 0)
    out["rq_1h"] = cnt / 3.0 * win("r4", -60, 0)
    out["rs_pos_1h"] = win("pos", -60, 0)
    out["rs_neg_1h"] = win("neg", -60, 0)
    out["bv_1h"] = math.pi / 2.0 * win("adj", -60, 0)
    out["n_ret_1h"] = cnt
    for name, w in TARGET_WINDOWS_MIN.items():
        out["y_" + name] = win("r2", 0, w)
        out["ycov_" + name] = win("span", 0, w) / (60.0 * w) >= MIN_WINDOW_COVERAGE
    # 15-MINUTE-RETURN targets: the variance a 15-minute (or 1-hour) digital actually settles on,
    # free of the 1-minute microstructure excess that 1-minute RV shows against BRTI settlement
    # values. y_15m_r15 = (ln P(t+15m) - ln P(t))^2 -- ONE return, so an
    # unbiased but noisy target; y_1h_r15 = the sum of the four 15-minute squared returns in the hour.
    lp = np.full(n + 1, np.nan)
    lp[pos + 1] = np.log(candles_1m["close_usd"].to_numpy()[1:])     # log price at each return's end
    lp = pd.Series(lp).ffill().to_numpy()

    def lp_at(off_min):
        i = k + off_min
        okk = (i >= 1) & (i <= n)
        return np.where(okk, lp[np.clip(i, 0, n)], np.nan)

    l0, l15, l30, l45, l60 = (lp_at(m) for m in (0, 15, 30, 45, 60))
    out["y_15m_r15"] = (l15 - l0) ** 2
    out["y_1h_r15"] = (l15 - l0) ** 2 + (l30 - l15) ** 2 + (l45 - l30) ** 2 + (l60 - l45) ** 2
    out["ycov_15m_r15"] = out["ycov_15m"]
    out["ycov_1h_r15"] = out["ycov_1h"]
    p = pd.DataFrame(out)
    et = p["origin"].dt.tz_convert(ET)
    p["tow"] = (et.dt.dayofweek * 96 + et.dt.hour * 4 + et.dt.minute // 15).astype("int16")
    if write:
        p.to_parquet(INTRADAY_PATH, index=False)
    return p


def load_intraday():
    return pd.read_parquet(INTRADAY_PATH)
