from kbtc.kalshi_root import harness as H
import numpy as np
import pandas as pd
from kbtc.rvol import evalkit as E

rng = np.random.default_rng(7)
# ---- leakage guard: a fit_predict that records the latest target_end it was trained on ----------
days = pd.date_range("2018-01-01", "2021-06-30", freq="D")
panel = pd.DataFrame({"day": days, "rv": np.exp(rng.normal(-8, 0.5, len(days)))})
seen = []


def fp(train, test, h):
    seen.append((train["target_end"].max(), test["origin"].min()))
    return np.full(len(test), train["y"].mean())


E.walk_forward(panel, "7d", fp, oos_start=pd.Timestamp("2020-01-01"), train_start=pd.Timestamp("2018-01-01"))
H.check("no training target ends after the first test origin",
        all(te <= to for te, to in seen), True)

# ---- DM recovers a planted difference; roughly calibrated under the null ------------------------
n = 3000
y = np.exp(rng.normal(-8, 0.6, n))
truth = y * np.exp(rng.normal(0, 0.3, n))
origin = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")


def frame(name, f):
    return pd.DataFrame({"origin": origin, "horizon": "15m", "forecast": f, "realized": y, "model": name})


good, bad = truth, truth * 1.6
df = pd.concat([frame("good", good), frame("bad", bad)])
d, t, p, _ = E.dm(df, "good", "bad", "15m")
H.check("DM: good beats inflated", (d < 0, p < 0.01), (True, True))
rej = 0
for s in range(200):
    r = np.random.default_rng(100 + s)
    a = y * np.exp(r.normal(0, 0.3, n))
    b = y * np.exp(r.normal(0, 0.3, n))
    _, _, p0, _ = E.dm(pd.concat([frame("a", a), frame("b", b)]), "a", "b", "15m")
    rej += p0 < 0.05
H.ok("DM size ~5% under the null", rej / 200, 0.05, 0.04)

# ---- MCS keeps the best model and drops a clearly worse one --------------------------------------
df3 = pd.concat([frame("best", truth), frame("close", truth * np.exp(rng.normal(0, 0.02, n))),
                 frame("awful", truth * 2.5)])
m = E.mcs(df3, "15m", B=300).set_index("model")
H.check("MCS: best in, awful out", (bool(m.loc["best", "in_mcs"]), bool(m.loc["awful", "in_mcs"])),
        (True, False))
H.ok("QLIKE is zero at a perfect forecast", float(E.qlike([2.0], [2.0])[0]), 0.0, 1e-15)
H.finish()
