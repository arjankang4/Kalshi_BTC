"""Per-contract dollar Greeks of a BTC digital, in the SPX/NDX units (the Kalshi repo's pricing/greeks):

    delta   dollars of value per $1 move in the underlying
    vega    dollars of value per 1 volatility point (1%) of annualised sigma
    theta   dollars of value per 1 minute of time passing
    R       sqrt(delta^2 + vega^2 + theta^2), the number the SPX edge curve and R_max read

Central finite differences on the SAME 60-second-average digital the theo uses, so the Greeks
describe the price we quote rather than a close-settling approximation of it.
"""
import math

from kbtc.pricing import digital


def market_greeks(mk, S, sigma, now, observed=None):
    def f(s, sig, t):
        v, _ = digital.market_theo(mk, s, sig, t, observed)
        return v
    base = f(S, sigma, now)
    out = {"delta": None, "vega": None, "theta": None, "R": None}
    if base is None:
        return out
    up, dn = f(S + 1.0, sigma, now), f(S - 1.0, sigma, now)
    vu, vd = f(S, sigma + 0.005, now), f(S, max(1e-4, sigma - 0.005), now)
    later = f(S, sigma, now + 60.0)
    if None in (up, dn, vu, vd):
        return out
    out["delta"] = (up - dn) / 2.0
    out["vega"] = (vu - vd) / 1.0            # 0.01 sigma span = one vol point
    out["theta"] = None if later is None else later - base
    t = out["theta"] or 0.0
    out["R"] = math.sqrt(out["delta"] ** 2 + out["vega"] ** 2 + t * t)
    return out
