"""Shared out-of-sample evaluation for the BTC realized-vol model comparison (#1 HAR-RV, #2 HARQ,
#3 SHAR, #4 HAR-X). Every model is scored on the SAME rows with the SAME losses, from files.

ONE ENTRY POINT
    evaluate(name, fit_predict, horizons, panel)
        fit_predict(train_df, test_df, horizon) -> np.ndarray, one forecast per test_df row
    writes var/research/rv_models/<name>/forecasts.parquet:
        origin (UTC Timestamp), horizon (str), forecast, realized, model

TARGETS (built here, never by a model, so they cannot differ between models)
    daily panel (rvdata.load_daily; one row per 17:00-ET day, `day` = the close that ENDS it):
        "1d" / "7d" / "30d": y = MEAN daily rv over days t+1 .. t+h           (per-day variance)
    intraday panel (rvdata.intraday_panel; one row per 15-min origin):
        "15m" / "1h": y = y_15m / y_1h, total rv over (t, t+h]                (not annualised)

SPLIT. Train rows start at TRAIN_START (2021-01-01), test rows at OOS_START (2025-01-01), expanding
window, refit every 30 days (daily) / 7 days (intraday).

LEAKAGE GUARD. At refit instant R, train_df holds only rows whose target window ENDED at or
before R (daily: day + h days <= R; intraday: origin + h <= R). test_df holds the rows with
R <= origin < next refit. A model sees features at its own row only; any profile (time-of-week,
EWMA lambda, intercept) must be fitted on train_df.

LOSSES on variance: QLIKE = y/f - ln(y/f) - 1 (Patton 2011, robust to noise in the proxy), MSE.
A forecast <= 0 or non-finite is an error, not a skip -- it is reported.

COMPARISON: dm() Diebold-Mariano with a Newey-West variance at lag = overlap (h-1 in origin
steps); mcs() Hansen-Lunde-Nason Model Confidence Set with a stationary block bootstrap (T_max).
Both are numpy-only (scipy is not installed); the normal CDF uses math.erf.
"""
import math
import os

import numpy as np
import pandas as pd

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "var", "research", "rv_models")

DAILY_H = {"1d": 1, "7d": 7, "30d": 30}
INTRADAY_H = {"15m": pd.Timedelta(minutes=15), "1h": pd.Timedelta(hours=1),
              # the same windows scored on 15-minute-return targets (rvdata: y_15m_r15, y_1h_r15)
              "15m_r15": pd.Timedelta(minutes=15), "1h_r15": pd.Timedelta(hours=1)}
# One split for every model: train from 2021-01-01, test from 2025-01-01, because BTC vol may
# have trended down since 2017 and older data could bias every fit. Intraday data starts 2022-01, so TRAIN_START is a no-op there.
OOS_START = {"daily": pd.Timestamp("2025-01-01"), "intraday": pd.Timestamp("2025-01-01", tz="UTC")}
TRAIN_START = {"daily": pd.Timestamp("2021-01-01"), "intraday": pd.Timestamp("2021-01-01", tz="UTC")}
REFIT = {"daily": pd.Timedelta(days=30), "intraday": pd.Timedelta(days=7)}
# Origin steps spanned by one target, for the DM/HAC lag.
STEP = {"daily": pd.Timedelta(days=1), "intraday": pd.Timedelta(minutes=15)}

REGIMES = {
    "covid_2020_03": ("2020-03-01", "2020-04-01"),
    "y2021": ("2021-01-01", "2022-01-01"),
    "may_2021": ("2021-05-01", "2021-06-01"),
    "y2022": ("2022-01-01", "2023-01-01"),
    "ftx_2022_11": ("2022-11-01", "2022-12-01"),
    "pre_etf": ("2000-01-01", "2024-01-11"),
    "post_etf": ("2024-01-11", "2100-01-01"),
}


def kind_of(h):
    if h in DAILY_H:
        return "daily"
    if h in INTRADAY_H:
        return "intraday"
    raise KeyError(h)


# ---- targets ------------------------------------------------------------------------------------

def with_targets(panel, h):
    """panel + columns origin, y, target_end for horizon h (rows with an incomplete target dropped)."""
    k = kind_of(h)
    p = panel.copy()
    if k == "daily":
        n = DAILY_H[h]
        p = p.sort_values("day").reset_index(drop=True)
        p["origin"] = pd.to_datetime(p["day"])
        # contiguous days only: a missing day inside the window invalidates the target
        rv = p["rv"].to_numpy()
        days = p["origin"].to_numpy()
        y = np.full(len(p), np.nan)
        for i in range(len(p) - n):
            if (days[i + n] - days[i]) == np.timedelta64(n, "D"):
                y[i] = rv[i + 1:i + 1 + n].mean()
        p["y"] = y
        p["target_end"] = p["origin"] + pd.Timedelta(days=n)
    else:
        p["y"] = p["y_" + h]
        p["target_end"] = p["origin"] + INTRADAY_H[h]
        if ("ycov_" + h) in p:
            p.loc[~p["ycov_" + h].astype(bool), "y"] = np.nan
    return p[np.isfinite(p["y"]) & (p["y"] > 0)].reset_index(drop=True)


# ---- the walk-forward ---------------------------------------------------------------------------

def walk_forward(panel, h, fit_predict, oos_start=None, refit=None, train_start=None):
    k = kind_of(h)
    p = with_targets(panel, h)
    start = OOS_START[k] if oos_start is None else oos_start
    t_start = TRAIN_START[k] if train_start is None else train_start
    step = REFIT[k] if refit is None else refit
    end = p["origin"].max()
    out = []
    r = start
    while r <= end:
        train = p[(p["target_end"] <= r) & (p["origin"] >= t_start)]
        test = p[(p["origin"] >= r) & (p["origin"] < r + step)]
        if len(test) and len(train):
            f = np.asarray(fit_predict(train.reset_index(drop=True), test.reset_index(drop=True), h),
                           dtype="float64")
            if f.shape != (len(test),):
                raise ValueError(f"fit_predict returned {f.shape}, expected ({len(test)},)")
            out.append(pd.DataFrame({"origin": test["origin"].to_numpy(), "horizon": h,
                                     "forecast": f, "realized": test["y"].to_numpy()}))
        r = r + step
    return pd.concat(out, ignore_index=True)


def evaluate(name, fit_predict, horizons, panel, save=True, **kw):
    df = pd.concat([walk_forward(panel, h, fit_predict, **kw) for h in horizons], ignore_index=True)
    df["model"] = name
    if save:
        d = os.path.join(ROOT, name)
        os.makedirs(d, exist_ok=True)
        df.to_parquet(os.path.join(d, "forecasts.parquet"), index=False)
    return df


def load(names=None):
    names = names or sorted(n for n in os.listdir(ROOT)
                            if os.path.exists(os.path.join(ROOT, n, "forecasts.parquet")))
    return pd.concat([pd.read_parquet(os.path.join(ROOT, n, "forecasts.parquet")) for n in names],
                     ignore_index=True)


# ---- losses and scoring -------------------------------------------------------------------------

def qlike(f, y):
    f = np.asarray(f, dtype="float64")
    y = np.asarray(y, dtype="float64")
    x = y / f
    return x - np.log(x) - 1.0


def mse(f, y):
    return (np.asarray(f, dtype="float64") - np.asarray(y, dtype="float64")) ** 2


LOSSES = {"qlike": qlike, "mse": mse}


def common_rows(df):
    """Keep only (origin, horizon) rows every model forecast validly, so scores are comparable."""
    ok = df[np.isfinite(df["forecast"]) & (df["forecast"] > 0)]
    n_models = df["model"].nunique()
    cnt = ok.groupby(["origin", "horizon"])["model"].transform("nunique")
    return ok[cnt == n_models].copy()


def score(df, by_regime=True):
    """Mean loss per (model, horizon[, regime]) on the common rows, plus bad-forecast counts."""
    bad = df[~(np.isfinite(df["forecast"]) & (df["forecast"] > 0))].groupby("model").size()
    c = common_rows(df)
    for k, fn in LOSSES.items():
        c[k] = fn(c["forecast"], c["realized"])
    c["ratio"] = c["forecast"] / c["realized"]
    rows = []
    groups = [("all", c)]
    if by_regime:
        o = c["origin"].dt.tz_localize(None) if c["origin"].dt.tz is not None else c["origin"]
        for rg, (a, b) in REGIMES.items():
            groups.append((rg, c[(o >= pd.Timestamp(a)) & (o < pd.Timestamp(b))]))
    for rg, g in groups:
        if not len(g):
            continue
        s = g.groupby(["model", "horizon"]).agg(qlike=("qlike", "mean"), mse=("mse", "mean"),
                                                 n=("qlike", "size"))
        s["mean_fc_over_mean_rv"] = (g.groupby(["model", "horizon"])["forecast"].mean()
                                     / g.groupby(["model", "horizon"])["realized"].mean())
        s["regime"] = rg
        rows.append(s.reset_index())
    out = pd.concat(rows, ignore_index=True)
    out.attrs["bad_forecasts"] = bad.to_dict()
    return out


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def nw_var(d, lag):
    """Newey-West long-run variance of series d (Bartlett weights)."""
    d = np.asarray(d, dtype="float64") - np.mean(d)
    n = len(d)
    v = d @ d / n
    for j in range(1, min(lag, n - 1) + 1):
        v += 2.0 * (1.0 - j / (lag + 1.0)) * (d[j:] @ d[:-j]) / n
    return v


def hac_lag(h):
    k = kind_of(h)
    span = DAILY_H[h] * pd.Timedelta(days=1) if k == "daily" else INTRADAY_H[h]
    return max(int(span / STEP[k]) - 1, 0)


def dm(df, a, b, h, loss="qlike"):
    """DM test of model a vs b at horizon h. Returns (mean loss diff a-b, t-stat, two-sided p, n).

    Negative diff = a better. Lag = target overlap in origin steps (at least 1 for intraday
    because consecutive 15-min windows share no returns only at h=15m).
    """
    c = common_rows(df[df["model"].isin([a, b]) & (df["horizon"] == h)])
    w = c.pivot_table(index="origin", columns="model", values="forecast")
    y = c.drop_duplicates("origin").set_index("origin")["realized"].reindex(w.index)
    fn = LOSSES[loss]
    d = fn(w[a].to_numpy(), y.to_numpy()) - fn(w[b].to_numpy(), y.to_numpy())
    lag = max(hac_lag(h), 1)
    se = math.sqrt(nw_var(d, lag) / len(d))
    t = d.mean() / se if se > 0 else float("nan")
    return float(d.mean()), float(t), float(2.0 * (1.0 - _ncdf(abs(t)))), len(d)


def _stationary_bootstrap_idx(n, B, mean_block, rng):
    p = 1.0 / mean_block
    idx = np.empty((B, n), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, B)
    new = rng.random((B, n)) < p
    starts = rng.integers(0, n, (B, n))
    for t in range(1, n):
        idx[:, t] = np.where(new[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return idx


def mcs(df, h, loss="qlike", alpha=0.10, B=1000, mean_block=None, seed=0, models=None):
    """Hansen-Lunde-Nason MCS, T_max statistic, stationary bootstrap.

    Returns a DataFrame: model, mean_loss, mcs_p (the MCS p-value), in_mcs (mcs_p >= alpha), n.
    """
    d = df[df["horizon"] == h]
    if models is not None:
        d = d[d["model"].isin(models)]
    c = common_rows(d)
    w = c.pivot_table(index="origin", columns="model", values="forecast").sort_index()
    y = c.drop_duplicates("origin").set_index("origin")["realized"].reindex(w.index).to_numpy()
    names = list(w.columns)
    L = np.column_stack([LOSSES[loss](w[m].to_numpy(), y) for m in names])
    n = L.shape[0]
    mean_block = mean_block or max(hac_lag(h) + 1, int(round(n ** (1 / 3))))
    rng = np.random.default_rng(seed)
    idx = _stationary_bootstrap_idx(n, B, mean_block, rng)
    Lb = L[idx].mean(axis=1)                       # (B, M) bootstrap mean losses
    # Eliminate down to one model so every model gets an MCS p-value: the p-value of the model
    # removed at step j is max(p_1..p_j); the last survivor gets 1. in_mcs = mcs_p >= alpha.
    alive = list(range(len(names)))
    pvals, p_run = {}, 0.0
    while len(alive) > 1:
        Lm = L[:, alive].mean(axis=0)
        dbar = Lm - Lm.mean()
        Lbm = Lb[:, alive]
        db = (Lbm - Lbm.mean(axis=1, keepdims=True)) - dbar
        se = np.sqrt((db ** 2).mean(axis=0))
        se = np.where(se > 0, se, np.inf)
        t = dbar / se
        p = float(((db / se).max(axis=1) >= t.max()).mean())
        p_run = max(p_run, p)
        worst = alive[int(np.argmax(t))]
        pvals[names[worst]] = p_run
        alive.remove(worst)
    pvals[names[alive[0]]] = 1.0
    res = pd.DataFrame({"model": names, "mean_loss": L.mean(axis=0),
                        "mcs_p": [pvals[m] for m in names]})
    res["in_mcs"] = res["mcs_p"] >= alpha
    res["n"] = n
    return res.sort_values("mean_loss").reset_index(drop=True)
