"""Signal -> alpha -> trading lean: one all-in lean from several signals, each weighted by its alpha.

PRINCIPLE. A signal may move our quotes only in proportion to the alpha it has shown, where alpha is
measured on what matters to a resting quote: the markout of the flow that actually trades against us
(the weights are fitted offline on historical KXBTC15M maker fill markouts; that fitting code is
not published). A signal with little
alpha barely moves us; one with none is not used at all.

PIPELINE, per market per tick:
    1. SIGNALS, in probability points; positive means YES is underpriced by our quote price p_q:
         s_rv    = p_theo - p_q            (the RV digital's disagreement left after the mid blend)
         s_move  = p(sigma e^{b|z|}) - p_theo   (the move-conditioned vol lean; z = BRTI's move over
                                                 l_move_s in forecast sigmas)
    2. ORTHOGONALISE in the fitted order (Gram-Schmidt with the fitted loadings), so a signal that
       merely repeats an earlier one adds nothing.
    3. WEIGHT: lean = sum_k w_k s~_k, with w_k the shrunk markout regression coefficients.
    4. TRADE: the favoured side (bid when lean > 0) has its required edge reduced by |lean|, the
       other side's raised by |lean|. The quoter keeps its max(0, ...) floor, so the favoured side
       can at most quote at zero edge to the un-leaned theo, never through it; orders stay post-only.
    5. ATTRIBUTION: contribution_k = w_k s~_k and share_k = contribution_k / lean, journalled per tick,
       so each signal's strength can be read back from how much of the lean it produced.

The weight file (var/model/lean_weights.json) is used only if it exists, "pass" is true (a held-out
markout test) and its "digital_shape" (absent = "gaussian") is the shape the live pricer now uses for its
series (kbtc.pricing.digital_shape.shape_for): weights fitted on Gaussian-digital signals are not applied
to t-digital signals. Otherwise `load` returns None and the quoter applies no lean.
"""
import json
import math
import os

from kbtc.pricing import digital_shape

ART = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "var", "model", "lean_weights.json")
MICROS = 1_000_000


def art_path(shape):
    """One weight file per digital shape: lean_weights.json (gaussian), lean_weights_<shape>.json
    otherwise. Switching DIGITAL_SHAPE (either way) therefore picks up the weights fitted for it, and
    switching back restores the old ones, with no window of no lean."""
    return ART if shape == "gaussian" else ART.replace(".json", f"_{shape}.json")


def load(path=None, series="KXBTC15M"):
    """The fitted pipeline, or None if there is no weight file, it did not pass its held-out test, or
    it was fitted under a different digital shape than the live one for its series."""
    if path is None:
        path = art_path(digital_shape.shape_for(series))
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        art = json.load(fh)
    if not art.get("pass") or not any(art.get("weights", {}).values()):
        return None
    if art.get("digital_shape", "gaussian") != digital_shape.shape_for(art.get("series", "KXBTC15M")):
        return None
    return art


def signals(p_theo, p_q, p_move, p_drift=None, p_iv=None):
    """Raw signals (probability points). Missing inputs give None for that signal.

    s_drift (candidate, used only if the loaded artifact lists it): p_drift - p_theo, with p_drift the
    theo at S * exp(m_300), i.e. if BRTI's last 300 s log move continues one more window."""
    out = {"s_rv": None, "s_move": None, "s_drift": None, "s_iv": None}
    if p_theo is not None and p_q is not None:
        out["s_rv"] = p_theo - p_q
    if p_move is not None and p_theo is not None:
        out["s_move"] = p_move - p_theo
    if p_drift is not None and p_theo is not None:
        out["s_drift"] = p_drift - p_theo
    # s_iv (candidate): the theo at the CURRENT-HOUR KXBTCD ladder's implied sigma minus the RV theo.
    # A missing hourly IV (about a third of the time) contributes nothing.
    if p_iv is not None and p_theo is not None:
        out["s_iv"] = p_iv - p_theo
    return out


def combine(sig, art):
    """(lean, detail). A signal that is None has orthogonalised value 0: it contributes nothing.

    (Treating it as a raw 0 instead would turn "unknown" into "below its projection on the earlier
    signals" and move the lean.)"""
    order = art["order"]
    load_ = art["loadings"]
    w = art["weights"]
    orth = {}
    for k, s in enumerate(order):
        v = sig.get(s)
        if v is None:
            orth[s] = 0.0
            continue
        v = float(v)
        for j in order[:k]:
            v -= load_.get(f"{s}|{j}", 0.0) * orth[j]
        orth[s] = v
    contrib = {s: w.get(s, 0.0) * orth[s] for s in order}
    lean = sum(contrib.values())
    share = {s: (c / lean if lean else 0.0) for s, c in contrib.items()}
    return lean, {"raw": {s: sig.get(s) for s in order}, "orth": orth, "contrib": contrib,
                  "share": share, "lean_c": 100.0 * lean}


def side_adjust_micros(lean, side):
    """Required-edge change for one side, in micro-dollars: negative (tighter) on the favoured side."""
    if not lean:
        return 0
    favoured = "bid" if lean > 0 else "ask"
    mag = int(round(abs(lean) * MICROS))
    return -mag if side == favoured else mag


def p_move(p_above, S, K, sigma_ann, tau_s, z, b):
    """The move-conditioned digital: sigma scaled by exp(b |z|). `p_above(S, K, sigma, tau)`."""
    if z is None or sigma_ann is None:
        return None
    return p_above(S, K, sigma_ann * math.exp(b * abs(z)), tau_s)


def move_m(brti, now_s, l_move_s):
    """BRTI's raw log move over the last l_move_s seconds (None under the same staleness rules as move_z)."""
    z = move_z(brti, now_s, l_move_s, 1.0)
    return None if z is None else z * math.sqrt(l_move_s / (365.0 * 86400.0))


def move_z(brti, now_s, l_move_s, sigma_ann):
    """BRTI's log move over the last l_move_s seconds in forecast sigmas, from (ts_s, price) prints.
    None unless there is a print within 5 s of both ends."""
    if not brti or sigma_ann is None or sigma_ann <= 0:
        return None
    last = brti[-1]
    if now_s - last[0] > 5:
        return None
    target = now_s - l_move_s
    past = None
    for t, p in reversed(brti):
        if t <= target:
            past = (t, p)
            break
    if past is None or target - past[0] > 5:
        return None
    return math.log(last[1] / past[1]) / (sigma_ann * math.sqrt(l_move_s / (365.0 * 86400.0)))
