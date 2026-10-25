"""Model #1b: intraday HAR-RV for the horizons the BTC quoter trades (next 15 minutes, next hour).

    python -m kbtc.rvol.har_intraday      # walk-forward -> var/research/rv_models/intraday_<name>/

Rows are evalkit's intraday panel (rvdata.intraday_panel: 15-minute UTC origins, Coinbase 1m).

    har_tow_log   ln(y/S_y) = b0 + sum_w b_w ln(rv_w/S_w),  w in 15m, 1h, 4h, 1d, 7d
                  forecast S_y * exp(xb + s^2/2). S_* = the time-of-week variance profile summed over
                  the window's 15-minute buckets, fitted on the TRAINING rows only (y_15m by tow,
                  normalised to mean 1). The BTC analogue of the SPX variance calendar.
    har_log       the same without the time-of-week profile (isolates what the profile buys)
    *_smear       Duan smearing mean(exp(resid)) instead of exp(s^2/2): fat-tailed log residuals
                  make exp(s^2/2) overshoot the level
    har_tow_log_sm2  profile smoothed over +-2 buckets (75 min) plus smearing
    har15_lin     the 2-second-RV HAR form (kbtc/rvol/rv2s.py, var/model/har_15m.json):
                  y = b0 + b1*15*rv_1m + b5*3*rv_5m + b15*rv_15m, refit per horizon on variances
    ewma60        EWMA of 1-minute r^2, 60-minute half-life, times h (baseline)
    ewma60_tow    ewma60 rescaled by S_y / S_trailing-1h (the profile applied to the baseline too)
    persist       rv_15m * h/15

A zero trailing window cannot be logged; such rows are dropped from training and their forecast
falls back to the training median ratio. Both counts are reported (`FALLBACK`).
"""
import json

import numpy as np
import pandas as pd

from kbtc.data import coinbase
from kbtc.rvol import evalkit as E
from kbtc.rvol import rvdata as R

WINDOWS = {"15m": 1, "1h": 4, "4h": 16, "1d": 96, "7d": 672}      # in 15-minute buckets
H_BUCKETS = {"15m": 1, "1h": 4}
H_MIN = {"15m": 15, "1h": 60}
NB = 672
FALLBACK = {}


def tow_profile(train, smooth=0, calendar=None):
    """Mean y_15m by time-of-week bucket, normalised to mean 1 over the week.

    smooth=k averages each bucket with its k neighbours either side (circular), trading shape
    resolution for noise; k=2 is a 75-minute window.
    calendar: an EXTERNAL 672-bucket profile (array, or path to a JSON with key "tow_15m") used
    instead of fitting one on `train` -- e.g. the pooled BTC+ETH var/model/crypto_calendar.json.
    It is renormalised to mean 1. A calendar fitted on rows that overlap the test window is a
    lookahead; the caller owns that (fit it per fold or on pre-test data only).
    """
    if calendar is not None:
        if isinstance(calendar, str):
            with open(calendar) as fh:
                calendar = json.load(fh)["tow_15m"]
        c = np.asarray(calendar, dtype="float64")
        if c.shape != (NB,):
            raise ValueError(f"calendar must have {NB} buckets, got {c.shape}")
        return c / c.mean()
    g = train.groupby("tow")["y_15m"].mean().reindex(range(NB))
    g = g.fillna(g.mean()).to_numpy()
    if smooth:
        k = np.ones(2 * smooth + 1) / (2 * smooth + 1)
        g = np.convolve(np.r_[g[-smooth:], g, g[:smooth]], k, mode="valid")
    return g / g.mean()


def _sum_buckets(prof, start, n):
    """Sum of prof over n consecutive buckets starting at `start` (cyclic, vectorised)."""
    c = np.r_[0.0, np.cumsum(np.r_[prof, prof, prof])]
    s = (np.asarray(start) % NB) + NB
    return c[s + n] - c[s]


def seasonal(df, prof, h):
    """S_y for the target and S_w for each trailing window, in units of 'mean 15-minute bucket'."""
    tow = df["tow"].to_numpy().astype(np.int64)
    Sy = _sum_buckets(prof, tow, H_BUCKETS[h])
    Sw = {w: _sum_buckets(prof, tow - n, n) for w, n in WINDOWS.items()}
    return Sy, Sw


def _design(df, prof, h, use_tow):
    if use_tow:
        Sy, Sw = seasonal(df, prof, h)
    else:
        Sy = np.full(len(df), float(H_BUCKETS[h]))
        Sw = {w: np.full(len(df), float(n)) for w, n in WINDOWS.items()}
    cols = [np.log(df["rv_" + w].to_numpy(dtype="float64") / Sw[w]) for w in WINDOWS]
    X = np.column_stack([np.ones(len(df))] + cols)
    return X, Sy


def retransform(resid, n_params, smear=False):
    """Log-scale forecast -> variance-scale multiplier. SHARED: HAR-X and HARQ call this so a
    covariate's gain is never partly a retransform difference.

    smear=False: lognormal exp(s^2/2), s^2 with n_params degrees of freedom removed.
    smear=True:  Duan (1983) smearing mean(exp(resid)) -- no normality assumption.
    """
    resid = np.asarray(resid, dtype="float64")
    if smear:
        return float(np.mean(np.exp(resid)))
    return float(np.exp(0.5 * np.sum(resid ** 2) / (len(resid) - n_params)))


def _har_log(train, test, h, use_tow, name, smooth=0, smear=False, calendar=None):
    prof = tow_profile(train, smooth, calendar) if use_tow else None
    X, Sy = _design(train, prof, h, use_tow)
    y = np.log(train["y"].to_numpy() / Sy)
    ok = np.isfinite(X).all(axis=1) & np.isfinite(y)
    b, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
    adj = retransform(y[ok] - X[ok] @ b, X.shape[1], smear)
    Xt, Syt = _design(test, prof, h, use_tow)
    f = Syt * np.exp(Xt @ b) * adj
    bad = ~np.isfinite(f)
    k = (name, h)
    FALLBACK[k] = FALLBACK.get(k, [0, 0])
    FALLBACK[k][0] += int((~ok).sum())
    FALLBACK[k][1] += int(bad.sum())
    if bad.any():
        f[bad] = Syt[bad] * np.median(train["y"].to_numpy() / Sy)
    return f


def har_tow_log(train, test, h):
    return _har_log(train, test, h, True, "har_tow_log")


def har_log(train, test, h):
    return _har_log(train, test, h, False, "har_log")


def har_tow_log_smear(train, test, h):
    return _har_log(train, test, h, True, "har_tow_log_smear", smear=True)


def har_tow_log_sm2(train, test, h):
    return _har_log(train, test, h, True, "har_tow_log_sm2", smooth=2, smear=True)


def har_tow_log_smear_cal(calendar):
    """har_tow_log_smear on an external calendar (see tow_profile). Returns an evalkit fit_predict."""
    def fp(train, test, h):
        return _har_log(train, test, h, True, "har_tow_log_smear_cal", smear=True, calendar=calendar)
    return fp


def har_log_smear(train, test, h):
    return _har_log(train, test, h, False, "har_log_smear", smear=True)


def har15_lin(train, test, h):
    def X(d):
        return np.column_stack([np.ones(len(d)), 15 * d["rv_1m"].to_numpy(), 3 * d["rv_5m"].to_numpy(),
                                d["rv_15m"].to_numpy()])
    Xtr = X(train)
    ok = np.isfinite(Xtr).all(axis=1)
    b, *_ = np.linalg.lstsq(Xtr[ok], train["y"].to_numpy()[ok], rcond=None)
    f = X(test) @ b
    bad = ~(f > 0)
    FALLBACK[("har15_lin", h)] = [0, FALLBACK.get(("har15_lin", h), [0, 0])[1] + int(bad.sum())]
    f[bad] = np.nanmin(train["y"])
    return f


def persist(train, test, h):
    return test["rv_15m"].to_numpy(dtype="float64") * H_MIN[h] / 15.0


def ewma60(train, test, h):
    return test["ewma60_per_min"].to_numpy(dtype="float64") * H_MIN[h]


def ewma60_tow(train, test, h):
    prof = tow_profile(train)
    tow = test["tow"].to_numpy().astype(np.int64)
    Sy = _sum_buckets(prof, tow, H_BUCKETS[h])
    St = _sum_buckets(prof, tow - 4, 4)                 # the trailing hour the EWMA mostly reflects
    return test["ewma60_per_min"].to_numpy(dtype="float64") * H_MIN[h] * (Sy / H_BUCKETS[h]) / (St / 4)


def add_ewma(panel, candles, half_life_min=60):
    """EWMA of per-minute r^2 (returns placed at their end minute), sampled at each origin."""
    rt = R.returns(candles, gran=60)
    t0 = int(rt["end_s"].min())
    idx = pd.date_range(pd.to_datetime(t0, unit="s", utc=True),
                        pd.to_datetime(int(rt["end_s"].max()), unit="s", utc=True), freq="1min")
    s = pd.Series(0.0, index=idx)
    s.loc[pd.to_datetime(rt["end_s"].to_numpy(), unit="s", utc=True)] = rt["r"].to_numpy() ** 2
    e = s.ewm(halflife=half_life_min, adjust=False).mean()
    p = panel.copy()
    p["ewma60_per_min"] = e.reindex(p["origin"]).to_numpy()
    return p


MODELS = {"har_tow_log": har_tow_log, "har_log": har_log, "har_tow_log_smear": har_tow_log_smear,
          "har_tow_log_sm2": har_tow_log_sm2, "har_log_smear": har_log_smear, "har15_lin": har15_lin,
          "ewma60": ewma60, "ewma60_tow": ewma60_tow, "persist": persist}


def build_panel():
    c = coinbase.load(60)
    p = R.intraday_panel(c, write=True)
    return add_ewma(p, c)


def usable(p):
    """Rows every intraday model can forecast: full 7d history and coverage on the feature windows."""
    ok = np.ones(len(p), bool)
    for w in ["1m", "5m", "15m", "1h", "4h", "1d", "7d"]:
        ok &= p["rv_" + w].notna().to_numpy() & p["cov_" + w].to_numpy(dtype=bool)
    return p[ok & p["ewma60_per_min"].notna().to_numpy()].reset_index(drop=True)


def run(panel=None, save=True, **kw):
    panel = usable(build_panel() if panel is None else panel)
    return {n: E.evaluate("intraday_" + n, fn, ("15m", "1h"), panel, save=save, **kw)
            for n, fn in MODELS.items()}


if __name__ == "__main__":
    out = run()
    s = E.score(pd.concat(out.values()))
    print(s[s.regime.isin(["all", "post_etf"])].to_string())
    print("fallback:", FALLBACK)
