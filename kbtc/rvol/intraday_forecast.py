"""Live-callable intraday realized-variance forecast: the promoted intraday model.

    python -m kbtc.rvol.intraday_forecast --fit      # sync Coinbase 1m, refit, write the artifact
    from kbtc.rvol.intraday_forecast import load, forecast
    art = load();  forecast(art, now_s, closes)       # -> {"15m": var, "1h": var, ...}

MODEL (har_intraday.har_tow_log_smear, rolling 365-day training window, one fit per horizon):
    ln(y/S_y) = b0 + sum_w b_w ln(rv_w / S_w),   w in 15m, 1h, 4h, 1d, 7d
    forecast  = S_y * exp(xb) * smear,           smear = mean(exp(training residuals))  (Duan)
S_* is the time-of-week variance profile (672 fifteen-minute buckets, America/New_York, mean 1)
summed over the window, fitted on the same training rows. Variances are totals over the horizon,
NOT annualised; `sigma_ann_<h>` converts at 365*24*60 minutes per year.

WHY THIS ONE. Chosen by out-of-sample comparison through kbtc.rvol.evalkit (QLIKE, Diebold-Mariano,
Model Confidence Set) against HARQ, HAR-X and EWMA baselines on Coinbase 1m. The rolling 365-day
window is preferred over an expanding one because it tracks the vol LEVEL more closely -- level is
what a theo prices.

LIVE-VS-FIT VENUE. Fitted on Coinbase trade-price 1m closes. Fed another venue's closes (perp mids,
BRTI) the forecast inherits that venue's noise level: the log model is close to unit-elastic in
the feature level, so a venue with ~x% lower 1m RV forecasts ~x% lower. Track realised/forecast
live and set `venue_scale` from it; it defaults to 1 and is never estimated silently.

CALENDAR VERSION (`--fit --calendar` -> var/model/rv_intraday_cal.json): identical model, but every
S_* comes from the pooled BTC+ETH crypto calendar (kbtc/rvol/cryptocal.py; FOMC/CPI/NFP windows
excluded) instead of a per-fit BTC-only profile, refitted per fold with no look-ahead
(kbtc/rvol/cryptocal_refit.py). Its 1-minute shape also drives `variance_to_close`, the pricing
clock.

`forecast` is valid at ANY instant, not only on the 15-minute grid: every window is measured in
minutes back from `now_s`, and S_* weights each minute by its own bucket. At grid instants it
reproduces the walk-forward forecasts exactly.
"""
import argparse
import datetime as dt
import json
import math
import os
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from kbtc.rvol import har_intraday as HI
from kbtc.rvol import rvdata as R

ET = ZoneInfo("America/New_York")
ART_PATH = os.path.join(os.path.dirname(R.ROOT), "model", "rv_intraday.json")
# The calendar version (pooled BTC+ETH crypto calendar, kbtc/rvol/cryptocal.py) is a SEPARATE
# artifact so a running quoter -- which loads ART_PATH once at start -- never switches silently.
ART_CAL_PATH = os.path.join(os.path.dirname(R.ROOT), "model", "rv_intraday_cal.json")
# Calendar model with its LEVEL refitted on 15-minute returns (kbtc/rvol/r15_refit.py).
ART_CAL15_PATH = os.path.join(os.path.dirname(R.ROOT), "model", "rv_intraday_cal15.json")
CAL_PATH = os.path.join(os.path.dirname(R.ROOT), "model", "crypto_calendar.json")
WINDOW_DAYS = 365
WIN_MIN = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "7d": 10080}
H_MIN = {"15m": 15, "1h": 60}
MIN_PER_YEAR = 365.0 * 24 * 60
MIN_COVERAGE = 0.90


def fit_artifact(panel, end=None, window_days=WINDOW_DAYS, calendar=None, level="1m"):
    """Fit both horizons on usable rows whose target ended by `end` (default: all) within the window.

    calendar: None -> each horizon fits its own BTC-only time-of-week profile on its training rows
    (the original promotion). A path to crypto_calendar.json -> the pooled calendar is used for
    every S_* instead, and its 1-minute shape (`tow_1m`) is embedded so `forecast` and
    `variance_to_close` weight minutes inside a bucket by the calendar, not uniformly.
    level: "1m" -> forecasts 1-minute RV (the fitted target). "r15" -> the shape is still fitted
    on 1-minute RV, then scaled by kappa_h = mean(y_r15 / in-sample forecast) on the same rows:
    the forecast becomes the variance of 15-minute returns, what a digital settles on.
    """
    from kbtc.rvol import evalkit as E
    p = HI.usable(panel)
    end = p["origin"].max() + pd.Timedelta(hours=1) if end is None else end
    cal = None
    if calendar is not None:
        with open(calendar) as fh:
            cal = json.load(fh)
    art = {"model": "har_tow_log_smear" + ("_cal" if cal else "") + ("_r15" if level == "r15" else ""),
           "level": level, "window_days": window_days,
           "windows": list(WIN_MIN),
           "fitted_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "source": "Coinbase BTC-USD 1m closes", "horizons": {}}
    for h in H_MIN:
        t = E.with_targets(p, h)
        t = t[(t["target_end"] <= end) & (t["origin"] >= end - pd.Timedelta(days=window_days))]
        prof = HI.tow_profile(t, calendar=cal["tow_15m"] if cal else None)
        X, Sy = HI._design(t, prof, h, True)
        y = np.log(t["y"].to_numpy() / Sy)
        ok = np.isfinite(X).all(axis=1) & np.isfinite(y)
        b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        adj = HI.retransform(y[ok] - X[ok] @ b, X.shape[1], smear=True)
        kappa = 1.0
        if level == "r15":
            f_in = Sy * np.exp(X @ b) * adj
            y15 = t["y_" + h + "_r15"].to_numpy(dtype="float64")
            m = np.isfinite(f_in) & (f_in > 0) & np.isfinite(y15)
            kappa = float(np.mean(y15[m] / f_in[m]))
        art["horizons"][h] = {"beta": b.tolist(), "smear": adj * kappa, "kappa_r15": kappa,
                              "smear_1m": adj, "profile": prof.tolist(),
                              "n_train": int(ok.sum()), "train_from": str(t["origin"].min()),
                              "train_to": str(t["target_end"].max())}
    if cal:
        art["calendar_version"] = cal["calendar_version"]
        t1 = np.asarray(cal["tow_1m"], dtype="float64")
        t15 = np.asarray(cal["tow_15m"], dtype="float64")
        if np.max(np.abs(t1.reshape(672, 15).mean(axis=1) / t1.mean() - t15 / t15.mean())) > 1e-9:
            raise ValueError("calendar tow_1m does not aggregate to tow_15m")
        art["tow_1m"] = (t1 / t1.mean()).tolist()
    return art


def save(art, path=ART_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        json.dump(art, fh)
    os.replace(path + ".tmp", path)


def load(path=ART_PATH):
    with open(path) as fh:
        return json.load(fh)


def _minute_profile(prof, first_minute_s, n, tow_1m=None):
    """Per-minute weight, in units of 'one mean 15-minute bucket', for n minutes from first_minute_s.

    Without a 1-minute calendar every minute of a bucket gets bucket/15. With `tow_1m` (mean-1 per
    minute of the week, whose 15-minute means equal the 15-minute profile) the weight is
    tow_1m/15: identical sums over whole buckets -- so grid-origin forecasts are unchanged -- but
    the right shape inside a bucket (the 08:30 minute, the top of the hour).
    """
    t = pd.to_datetime(first_minute_s + 60 * np.arange(n), unit="s", utc=True).tz_convert(ET)
    if tow_1m is not None:
        mow = (t.dayofweek * 1440 + t.hour * 60 + t.minute).to_numpy()
        return np.asarray(tow_1m)[mow] / 15.0
    tow = (t.dayofweek * 96 + t.hour * 4 + t.minute // 15).to_numpy()
    return np.asarray(prof)[tow] / 15.0


def forecast(art, now_s, closes, venue_scale=1.0):
    """closes: iterable of (bucket_start_s, price) 1-minute candles, ascending, covering >= 7 days.

    A candle's close is the price at bucket_start + 60, exactly as in the fit. Returns
    {"15m": var, "1h": var, "sigma_ann_15m": ..., "sigma_ann_1h": ..., "ok": bool, "reason": str}.
    """
    now_s = int(now_s) // 60 * 60
    c = np.asarray(closes, dtype="float64")
    if c.ndim != 2 or len(c) < 2:
        return {"ok": False, "reason": "no_closes"}
    ts, px = c[:, 0].astype(np.int64), c[:, 1]
    keep = ts + 60 <= now_s
    ts, px = ts[keep], px[keep]
    end_s = ts[1:] + 60
    start_s = ts[:-1] + 60
    r2 = np.diff(np.log(px)) ** 2
    out = {"ok": True, "reason": ""}
    feats = {}
    for w, m in WIN_MIN.items():
        lo = now_s - 60 * m
        sel = end_s > lo
        cov = np.clip(np.minimum(end_s[sel], now_s) - np.maximum(start_s[sel], lo), 0, None).sum() / (60 * m)
        if cov < MIN_COVERAGE:
            return {"ok": False, "reason": f"coverage_{w}={cov:.2f}"}
        feats[w] = float(r2[sel].sum())
        if feats[w] <= 0:
            return {"ok": False, "reason": f"zero_rv_{w}"}
    for h, hm in H_MIN.items():
        a = art["horizons"][h]
        prof = a["profile"]
        t1 = art.get("tow_1m")
        mp_back = _minute_profile(prof, now_s - 60 * 10080, 10080, t1)      # minutes before now
        Sw = {w: mp_back[-m:].sum() for w, m in WIN_MIN.items()}
        Sy = _minute_profile(prof, now_s, hm, t1).sum()
        x = np.r_[1.0, [math.log(feats[w] / Sw[w]) for w in WIN_MIN]]
        v = Sy * math.exp(float(x @ np.asarray(a["beta"]))) * a["smear"] * venue_scale
        out[h] = v
        out["level_" + h] = v / Sy                     # deseasonalised: variance per mean bucket
        out["sigma_ann_" + h] = math.sqrt(v * MIN_PER_YEAR / hm)
    out["features"] = feats
    return out


def variance_to_close(art, fc, now_s, close_s):
    """Total variance of log price over (now_s, close_s], on the calendar clock, for pricing.

    The model forecasts the next 15 minutes and the next hour. A market closing in tau minutes
    needs variance over exactly (now, close]: that is the model's deseasonalised LEVEL times the
    calendar weight of those minutes -- not sigma_ann * tau, which spreads variance evenly and so
    mis-states it whenever the window straddles a busy minute (08:30, 09:30, the top of the hour)
    or a quiet one. The level comes from the 15m model at tau <= 15, the 1h model at tau >= 60,
    and a log-linear blend in between (continuity, as the SPX blend).

    Returns {"var", "sigma_eff_ann", "tau_min", "level", "weight"} or None if fc is not ok.
    sigma_eff_ann = sqrt(var * minutes/year / tau): the flat-clock sigma that reproduces `var`,
    which is what kbtc.pricing.digital.market_theo takes.
    """
    if not fc or not fc.get("ok"):
        return None
    tau_s = close_s - now_s
    if tau_s <= 0:
        return None
    tau_min = tau_s / 60.0
    l15, l60 = fc["level_15m"], fc["level_1h"]
    if tau_min <= 15:
        level = l15
    elif tau_min >= 60:
        level = l60
    else:
        w = (tau_min - 15) / 45.0
        level = math.exp((1 - w) * math.log(l15) + w * math.log(l60))
    a = art["horizons"]["15m"]
    first = int(now_s) // 60 * 60
    n = int(math.ceil((close_s - first) / 60.0))
    wts = _minute_profile(a["profile"], first, n, art.get("tow_1m"))
    # fractional first and last minutes
    wts = wts.copy()
    wts[0] *= (first + 60 - now_s) / 60.0
    tail = close_s - (first + 60 * (n - 1))
    if n > 1:
        wts[-1] *= tail / 60.0
    weight = float(wts.sum())
    var = level * weight
    return {"var": var, "sigma_eff_ann": math.sqrt(var * MIN_PER_YEAR / tau_min), "tau_min": tau_min,
            "level": level, "weight": weight}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--calendar", action="store_true",
                    help="fit on var/model/crypto_calendar.json and write rv_intraday_cal.json")
    ap.add_argument("--r15", action="store_true",
                    help="with --calendar: level refitted on 15-minute returns -> rv_intraday_cal15.json")
    a = ap.parse_args(argv)
    if a.fit:
        from kbtc.data import coinbase
        if not a.no_sync:
            coinbase.sync(60, dt.date.today().replace(day=1))
        c = coinbase.load(60)
        art = fit_artifact(HI.add_ewma(R.intraday_panel(c), c),
                           calendar=CAL_PATH if a.calendar else None, level="r15" if a.r15 else "1m")
        path = (ART_CAL15_PATH if a.r15 else ART_CAL_PATH) if a.calendar else ART_PATH
        save(art, path)
        print("wrote", path, {h: v["n_train"] for h, v in art["horizons"].items()},
              art.get("calendar_version"))


if __name__ == "__main__":
    main()
