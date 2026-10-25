"""Required edge (quote half-width) for BTC digitals: the SPX curve shape on BTC anchors.

    to POST:  floor(tau) + bonus(R) + lean + backoff          (cents -> micro-dollars)
    refused:  R > R_MAX[tenor], tau outside every bucket, or the total > MAX_HALF_WIDTH(tau)
    to STAY:  edge > 0 (strategy.orders.plan_side, unchanged)

Shape (the companion Kalshi repo's pricing/edge.Curve.bonus_dollars): bonus is 0 at R_MIN,
BONUS_KNEE_C at R_P99 through an exponential leg with base BONUS_BASE, then linear to BONUS_CAP_C
at R_MAX, and None beyond.

Anchors (settings.R_ANCHORS) and width caps (settings.MIN/MAX_HALF_WIDTH_C) are per tenor and
per time-to-close bucket; they are set offline from historical spot and theo moves (that study
code is not published).

  floor(tau)           = MIN_HALF_WIDTH: the posting floor, at least one tick
  MAX_HALF_WIDTH(tau)  = the most edge a side may need and still be posted: needing more means the
                         market cannot be re-priced fast enough to be quoted at all.
The floor is never below settings.MIN_EDGE.
"""
from kbtc import settings

BONUS_KNEE_C = 2.0
BONUS_CAP_C = 8.0
BONUS_BASE = 4.0
MICROS_PER_CENT = 10_000


def bonus_cents(R, tenor):
    a = settings.R_ANCHORS[tenor]
    if R is None or R != R:
        return None
    if R > a["R_MAX"]:
        return None
    if R <= a["R_MIN"]:
        return 0.0
    if R <= a["R_P99"]:
        u = (R - a["R_MIN"]) / (a["R_P99"] - a["R_MIN"])
        return BONUS_KNEE_C * ((BONUS_BASE ** u) - 1.0) / (BONUS_BASE - 1.0)
    v = (R - a["R_P99"]) / (a["R_MAX"] - a["R_P99"])
    return BONUS_KNEE_C + (BONUS_CAP_C - BONUS_KNEE_C) * v


def width_bucket(tau, tenor):
    """(min_half_width_c, max_half_width_c) for this time to close, or None if outside every bucket."""
    for (lo, hi), mn, mx in zip(settings.WIDTH_TAU_BUCKETS[tenor], settings.MIN_HALF_WIDTH_C[tenor],
                                settings.MAX_HALF_WIDTH_C[tenor]):
        if lo <= tau <= hi:
            return mn, mx
    return None


def post_requirement(R, tau, tenor):
    """(base_micros, max_micros, detail) for one market; base_micros None = refuse the market.

    base = floor(tau) + bonus(R). The caller adds the per-side lean and backoff, then refuses a
    side whose total exceeds max_micros.
    """
    d = {"tenor": tenor, "R": R, "tau": tau}
    wb = width_bucket(tau, tenor)
    if wb is None:
        d["reason"] = "tau_outside_width_buckets"
        return None, None, d
    b = bonus_cents(R, tenor)
    if b is None:
        d["reason"] = "R_above_R_MAX"
        return None, None, d
    floor_c = max(settings.MIN_EDGE * 100.0, wb[0])
    d.update({"floor_c": floor_c, "bonus_c": round(b, 4), "max_c": wb[1]})
    return int(round((floor_c + b) * MICROS_PER_CENT)), int(round(wb[1] * MICROS_PER_CENT)), d
