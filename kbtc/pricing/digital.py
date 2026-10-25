"""Kalshi BTC digitals on a 60-second settlement average.

Every BTC event market settles on A = the simple average of the 60 BRTI prints before close.
With log-price X a driftless Brownian motion of variance rate s2 (per second):

  tau >= W (window not yet started):
      A - S_t has variance  s2 * (tau - W) + s2 * W / 3  =  s2 * (tau - 2W/3)
      i.e. the average is worth 40 s less variance than the close, for W = 60.
  0 < tau < W (inside the window): e = W - tau seconds already printed with mean m_obs.
      A = (e * m_obs + integral of the remaining tau seconds) / W
        mean  (e * m_obs + tau * S_t) / W,  sd  S_t * sqrt(s2 * tau^3 / 3) / W
      priced as a normal in price -- over <= 60 s the lognormal correction is ~1e-6.

Then P(A > K) feeds `pricing.brackets.bracket_fair` (from the Kalshi repo), so greater / less /
between follow Kalshi's rules table exactly as the equity boards do.

The forward is S_hat itself: no drift. Funding carry (~1e-4 per 8 h) is under a dollar per day.

RETURN SHAPE (settings.DIGITAL_SHAPE: a name, or {series: name, "default": name}; default "gaussian").
For tau >= W the digital is 1 - F((ln(K/S) + w^2/2) / w) on the same effective w: "gaussian" is N(d2)
through pricing.bs; "t" / "empirical" read the unit-variance shape tables in var/model/digital_shape.json
(kbtc.pricing.digital_shape.p_above_tau; fitted offline, fitting code not published): the 15-minute table
for tau <= 15 min, the 1-hour table for tau >= 60 min, a log-tau mixture between. The final minute
stays Gaussian on the observed average. The shape is resolved PER SERIES by digital_shape.shape_for:
market_theo passes shape_for(mk["series"]), so Greeks (pricing/greeks.py, via market_theo) follow it;
the IV inversion (pricing/implied.py) passes shape_for of the ladder's series; offline fitting code
calls the vectorised digital_shape.p_above_avg_vec with shape_for of the series it fits.
"""
import math

from kbtc import settings
from kbtc.kalshi_root import bs, brackets
from kbtc.pricing import digital_shape


def var_rate_per_s(sigma_ann):
    return sigma_ann * sigma_ann / settings.SECONDS_PER_YEAR


def effective_w(sigma_ann, tau_s, window_s=settings.SETTLE_AVG_WINDOW_S):
    """Total vol w = sqrt(variance) of the settlement average's log, for tau >= window.

    `w` in the Kalshi repo's `pricing.bs` convention: the STANDARD DEVIATION of the log return over
    the window (sigma * sqrt(T)), not the variance.
    """
    return math.sqrt(var_rate_per_s(sigma_ann) * max(tau_s - 2.0 * window_s / 3.0, 0.0))


def p_above_avg(S, K, sigma_ann, tau_s, observed=None, window_s=settings.SETTLE_AVG_WINDOW_S, shape=None):
    """P(settlement average > K).

    `observed`: BRTI prints already inside the window (list of prices), used when tau < window.
    `shape`: the return shape for tau >= window; None -> digital_shape.shape_for() (the default entry).
    Returns None when inside the window without enough prints to price honestly.
    """
    if S is None or K is None or S <= 0 or K <= 0:
        return None
    if tau_s >= window_s:
        if shape is None:
            shape = digital_shape.shape_for()
        if shape == "gaussian":
            return bs.p_above(S, K, effective_w(sigma_ann, tau_s, window_s))
        return digital_shape.p_above_tau(S, K, effective_w(sigma_ann, tau_s, window_s), shape, tau_s)
    if tau_s <= 0:
        return None
    e = window_s - tau_s
    if not observed or len(observed) < 0.8 * e:       # need ~all prints already made
        return None
    m_obs = sum(observed) / len(observed)
    mean = (e * m_obs + tau_s * S) / window_s
    sd = S * math.sqrt(var_rate_per_s(sigma_ann) * tau_s ** 3 / 3.0) / window_s
    if sd <= 0:
        return 1.0 if mean > K else 0.0
    return bs.norm_cdf((mean - K) / sd)


def market_theo(mk, S, sigma_ann, now, observed=None):
    """(fair, detail) for one parsed event market (kbtc.venue.events.parse_market), priced under
    the shape of its series."""
    tau = mk["close_ts"] - now
    shape = digital_shape.shape_for(mk.get("series"))
    return brackets.bracket_fair(
        mk["strike_type"], mk["floor_strike"], mk["cap_strike"],
        lambda K: p_above_avg(S, K, sigma_ann, tau, observed, shape=shape))
