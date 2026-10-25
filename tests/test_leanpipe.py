"""leanpipe: orthogonalisation, alpha-proportional weighting, per-side trading lean, attribution."""
import json
import math
import os
import tempfile

from kbtc.kalshi_root import harness as H
from kbtc.strategy import leanpipe as LP

ART = {"order": ["s_move", "s_rv"], "loadings": {"s_rv|s_move": 0.5},
       "weights": {"s_move": 0.8, "s_rv": 0.2}, "pass": True}

# --- orthogonalisation and weighting --------------------------------------------------------------
lean, d = LP.combine({"s_rv": 0.02, "s_move": 0.01}, ART)
H.ok("s_rv is orthogonalised against s_move: 0.02 - 0.5*0.01", d["orth"]["s_rv"], 0.015, 1e-12)
H.ok("lean = sum of weight x orthogonalised signal", lean, 0.8 * 0.01 + 0.2 * 0.015, 1e-12)
H.ok("attribution shares sum to 1", sum(d["share"].values()), 1.0, 1e-12)
H.ok("a signal's share is its contribution over the lean", d["share"]["s_move"], 0.008 / 0.011, 1e-9)

# alpha-proportional: with uncorrelated signals (zero loading), a signal with a quarter of the weight
# moves the lean a quarter as much for the same raw size
UNCORR = dict(ART, loadings={"s_rv|s_move": 0.0})
l1, _ = LP.combine({"s_rv": 0.0, "s_move": 0.01}, UNCORR)
l2, _ = LP.combine({"s_rv": 0.01, "s_move": 0.0}, UNCORR)
H.ok("equal raw signals move the lean in proportion to their weights", l2 / l1, 0.2 / 0.8, 1e-12)
lz, _ = LP.combine({"s_rv": 0.05, "s_move": 0.0}, dict(ART, weights={"s_move": 0.8, "s_rv": 0.0}))
H.check("a zero-alpha signal does not lean at all", lz, 0.0)
lm, dm = LP.combine({"s_rv": None, "s_move": 0.01}, ART)
H.ok("a missing signal contributes nothing (not a raw 0 residualised to -0.005)", lm, 0.008, 1e-12)
lo, _ = LP.combine({"s_rv": 0.0, "s_move": 0.01}, ART)
H.ok("but a KNOWN zero below its projection does count", lo, 0.008 + 0.2 * (0.0 - 0.5 * 0.01), 1e-12)
H.check("... and is recorded as missing", dm["raw"]["s_rv"], None)

# --- trading lean ---------------------------------------------------------------------------------
H.check("lean > 0 favours the bid: its required edge falls", LP.side_adjust_micros(0.004, "bid"), -4000)
H.check("... and the ask's rises by the same", LP.side_adjust_micros(0.004, "ask"), 4000)
H.check("lean < 0 favours the ask", LP.side_adjust_micros(-0.004, "ask"), -4000)
H.check("no lean, no adjustment", LP.side_adjust_micros(0.0, "bid"), 0)
base = 10_000
H.check("the quoter's floor: a lean larger than the base edge stops at zero edge",
        max(0, base + LP.side_adjust_micros(0.03, "bid")), 0)

# --- inputs ---------------------------------------------------------------------------------------
H.check("signals: s_rv = p_theo - p_q", round(LP.signals(0.55, 0.53, 0.54)["s_rv"], 10), 0.02)
H.check("signals: s_move = p_move - p_theo", round(LP.signals(0.55, 0.53, 0.54)["s_move"], 10), -0.01)
brti = [(1000.0 + i, 80000.0) for i in range(301)] + [(1301.0, 80080.0)]
z = LP.move_z(brti, 1301.0, 300, 0.5)
H.ok("move_z: a 10 bp move over 300 s in forecast sigmas",
     z, math.log(80080 / 80000) / (0.5 * math.sqrt(300 / (365 * 86400))), 1e-9)
H.check("move_z: None without a print 300 s back", LP.move_z(brti[-50:], 1301.0, 300, 0.5), None)
H.check("move_z: None when the latest print is stale", LP.move_z(brti, 1320.0, 300, 0.5), None)

# --- the weight file gate -------------------------------------------------------------------------
d_ = tempfile.mkdtemp()
p = os.path.join(d_, "w.json")
H.check("no file -> no lean", LP.load(p), None)
json.dump(dict(ART, **{"pass": False}), open(p, "w"))
H.check("a file that failed its held-out test -> no lean", LP.load(p), None)
json.dump(ART, open(p, "w"))
from kbtc import settings
from kbtc.pricing import digital_shape as DS
_old_shape = getattr(settings, "DIGITAL_SHAPE", "gaussian")
try:
    settings.DIGITAL_SHAPE = "gaussian"
    H.check("a passing file loads", LP.load(p)["order"], ["s_move", "s_rv"])
    settings.DIGITAL_SHAPE = "t"
    H.check("weights fitted under the Gaussian (no digital_shape key) are refused under 't'", LP.load(p), None)
    json.dump(dict(ART, series="KXBTC15M", digital_shape="t"), open(p, "w"))
    H.check("weights fitted under 't' load under 't'", LP.load(p)["order"], ["s_move", "s_rv"])
    settings.DIGITAL_SHAPE = {"KXBTCD": "t", "KXBTC15M": "gaussian"}
    H.check("...and are refused when KXBTC15M is Gaussian again (per-series dict)", LP.load(p), None)
finally:
    settings.DIGITAL_SHAPE = _old_shape

# --- candidate s_drift: behaviour-neutral unless the artifact lists it ---------------------------
sd = LP.signals(0.55, 0.53, 0.54, p_drift=0.58)
H.check("s_drift = p_drift - p_theo", round(sd["s_drift"], 10), 0.03)
l_without, _ = LP.combine({k: v for k, v in sd.items() if k != "s_drift"}, ART)
l_with, _ = LP.combine(sd, ART)
H.check("an artifact without s_drift ignores it (shipped behaviour unchanged)", l_with, l_without)
ART3 = dict(ART, order=["s_move", "s_rv", "s_drift"], weights={"s_move": 0.8, "s_rv": 0.2, "s_drift": 0.05},
            loadings={"s_rv|s_move": 0.5, "s_drift|s_move": 0.0, "s_drift|s_rv": 0.0})
l3, _ = LP.combine(sd, ART3)
H.ok("with s_drift in the artifact it adds w * s_drift~", l3 - l_without, 0.05 * 0.03, 1e-12)
H.ok("move_m: the raw log move", LP.move_m(brti, 1301.0, 300), math.log(80080 / 80000), 1e-12)

# --- candidate s_iv: neutral unless listed; missing IV contributes nothing -----------------------------
si = LP.signals(0.55, 0.53, 0.54, p_drift=0.58, p_iv=0.52)
H.check("s_iv = p_iv - p_theo", round(si["s_iv"], 10), -0.03)
l_no, _ = LP.combine({k: v for k, v in si.items() if k != "s_iv"}, ART)
l_yes, _ = LP.combine(si, ART)
H.check("an artifact without s_iv ignores it", l_yes, l_no)
ART4 = dict(ART3, order=["s_move", "s_rv", "s_drift", "s_iv"],
            weights=dict(ART3["weights"], s_iv=0.05),
            loadings=dict(ART3["loadings"], **{"s_iv|s_move": 0.0, "s_iv|s_rv": 0.0, "s_iv|s_drift": 0.0}))
lm_, dm_ = LP.combine(dict(si, s_iv=None), ART4)
H.check("missing hourly IV: s_iv~ = 0, no contribution", dm_["contrib"]["s_iv"], 0.0)

# --- one weight file per digital shape ---------------------------------------------------------------
H.check("gaussian weights live at ART", LP.art_path("gaussian"), LP.ART)
H.check("t weights live beside them", LP.art_path("t").endswith("lean_weights_t.json"), True)
H.finish()
