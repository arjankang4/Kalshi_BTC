from kbtc.kalshi_root import harness as H
import datetime as dt
import numpy as np
import pandas as pd
from kbtc.rvol import rvdata as R

rng = np.random.default_rng(1)
# 20 days of synthetic 5-minute candles with gaps, starting 2024-03-05 (spans the 2024-03-10 DST jump)
t0 = int(dt.datetime(2024, 3, 5, 21, 0, tzinfo=dt.timezone.utc).timestamp())
ts = t0 + 300 * np.arange(20 * 288)
ts = ts[rng.random(len(ts)) > 0.03]                       # 3% missing buckets
c = 60000 * np.exp(np.cumsum(rng.normal(0, 0.002, len(ts))))
cand = pd.DataFrame({"ts_s": ts, "close_usd": c})
rt = R.returns(cand)
p = R.daily_panel(rt)

H.ok("rv summed over days equals total", p["rv"].sum(), float((rt["r"] ** 2).sum()), 1e-12)
H.ok("rs_pos + rs_neg == rv", float(np.max(np.abs(p["rs_pos"] + p["rs_neg"] - p["rv"]))), 0.0, 1e-15)
H.check("gap returns counted", int(p["n_gap"].sum()), int(rt["gap"].sum()))
# 17:00 ET boundary: a return ending exactly 17:00 ET belongs to that day; one second later, the next
b = int(dt.datetime(2024, 3, 12, 17, 0, tzinfo=R.ET).timestamp())
H.check("end-inclusive at 17:00 ET", str(R.day_label(np.array([b]))[0]), "2024-03-12")
H.check("just after 17:00 ET -> next day", str(R.day_label(np.array([b + 1]))[0]), "2024-03-13")
dst = p[p["day"] == pd.Timestamp("2024-03-10")]
H.check("DST day is 23h", float(dst["span_h"].iloc[0]), 23.0)
H.check("first/last day flagged partial", (bool(p["complete"].iloc[0]), bool(p["complete"].iloc[-1])),
        (False, False))

# intraday: y_15m(t) == rv_15m(t + 15m), and the identity holds
cm = pd.DataFrame({"ts_s": t0 + 60 * np.arange(3 * 1440)})
cm["close_usd"] = 60000 * np.exp(np.cumsum(rng.normal(0, 0.0008, len(cm))))
ip = R.intraday_panel(cm)
H.ok("target is the next window's feature", float(np.nanmax(np.abs(ip["y_15m"].values[:-1]
                                                                   - ip["rv_15m"].values[1:]))), 0.0, 1e-18)
H.ok("intraday rs identity", float(np.nanmax(np.abs(ip["rs_pos_1h"] + ip["rs_neg_1h"] - ip["rv_1h"]))), 0.0, 1e-15)
H.check("7d window NaN before 7d of history", bool(ip["rv_7d"].isna().all()), True)
H.finish()
