"""Implied vol from the 1-hour KXBTCD ladder: flat term structure, zero skew.

Each two-sided `greater` rung with a usable book is inverted through the SAME pricer the quotes
use (the 60-second settlement average under the ladder series' return shape,
`digital_shape.shape_for(series)`), so the vol we read is the vol we price with. Rungs are combined by a |vega|-weighted median:

  * A digital's vega is ZERO at the money (d1 = 0) and peaks around 1 sigma away, so the
    at-the-money rung -- the one whose mid moves most with spot -- says the least about vol.
    Weighting by |dP/dsigma| lets the rungs that carry information decide.
  * A median, because a single stale or one-sided rung should not move the answer.

Rungs are excluded (not repaired) when: a side is missing, the spread exceeds IV_MAX_SPREAD, the
mid is outside IV_MID_BAND, or the inversion does not bracket.
"""
from kbtc import settings
from kbtc.pricing import digital, digital_shape

SIG_LO, SIG_HI = digital_shape.SIG_LO, digital_shape.SIG_HI


def invert(mid, S, K, tau_s, shape=None):
    """sigma with p_above_avg(S, K, sigma, tau) == mid under `shape` (None -> shape_for())."""
    shape = digital_shape.shape_for() if shape is None else shape
    return digital_shape.invert_sigma(mid, S, K, tau_s, shape)


def vega(S, K, sigma, tau_s, h=0.005, shape=None):
    shape = digital_shape.shape_for() if shape is None else shape
    up = digital.p_above_avg(S, K, sigma + h, tau_s, shape=shape)
    dn = digital.p_above_avg(S, K, sigma - h, tau_s, shape=shape)
    return None if up is None or dn is None else (up - dn) / (2 * h)


def weighted_median(pairs):
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= 0.5 * tot:
            return v
    return pairs[-1][0]


def pick_hourly(markets, now):
    """The hourly KXBTCD event to read: nearest close with tau in [MIN, MAX]. -> (event, [mk])."""
    by = {}
    for m in markets:
        tau = m["close_ts"] - now
        if settings.IV_HOURLY_MIN_TAU_S <= tau <= settings.IV_HOURLY_MAX_TAU_S:
            by.setdefault(m["event_ticker"], []).append(m)
    if not by:
        return None, []
    ev = min(by, key=lambda e: by[e][0]["close_ts"])
    return ev, by[ev]


def pick_daily(markets, now):
    """The 1-day KXBTCD ladder: the nearest event closing at 17:00 ET with tau in
    [IV_DAILY_MIN_TAU_S, IV_DAILY_MAX_TAU_S]. -> (event, [mk])."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    by = {}
    for m in markets:
        tau = m["close_ts"] - now
        c = datetime.fromtimestamp(m["close_ts"], et)
        if (c.hour, c.minute) == (17, 0) and \
                settings.IV_DAILY_MIN_TAU_S <= tau <= settings.IV_DAILY_MAX_TAU_S:
            by.setdefault(m["event_ticker"], []).append(m)
    if not by:
        return None, []
    ev = min(by, key=lambda e: by[e][0]["close_ts"])
    return ev, by[ev]


def implied_sigma(markets, S, now):
    """(sigma, detail) from one hourly event's markets. sigma is None with a reason if unusable."""
    d = {"n_rungs": 0, "rungs": [], "reason": None}
    if S is None:
        d["reason"] = "no_spot"
        return None, d
    pairs = []
    for m in markets:
        if m["strike_type"] not in ("greater", "greater_or_equal") or m["floor_strike"] is None:
            continue
        b, a = m["yes_bid"], m["yes_ask"]
        if not b or not a or a >= 1 or b <= 0 or a - b > settings.IV_MAX_SPREAD:
            continue
        mid = 0.5 * (a + b)
        if not settings.IV_MID_BAND[0] <= mid <= settings.IV_MID_BAND[1]:
            continue
        tau = m["close_ts"] - now
        shape = digital_shape.shape_for(m.get("series"))
        s = invert(mid, S, m["floor_strike"], tau, shape)
        if s is None:
            continue
        v = vega(S, m["floor_strike"], s, tau, shape=shape)
        if not v:
            continue
        pairs.append((s, abs(v)))
        d["rungs"].append({"K": m["floor_strike"], "mid": mid, "sigma": round(s, 4),
                           "vega": round(v, 4)})
    d["n_rungs"] = len(pairs)
    if len(pairs) < settings.IV_MIN_RUNGS:
        d["reason"] = "too_few_rungs"
        return None, d
    return weighted_median(pairs), d
