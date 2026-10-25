"""Fat-tailed digital: P(A > K) under a unit-variance return SHAPE on the same total vol w.

    P(A > K) = 1 - F( (ln(K/S) + w^2/2) / w )

with w the pricer's total vol (kbtc.pricing.digital.effective_w: sigma, tau and the 60-s-average
adjustment), and F the CDF of the forecast-standardised log return z = r / sqrt(forecast var):

    "gaussian"   F = N(0,1)          -> exactly the Kalshi repo's pricing.bs.p_above (N(d2)); same code path
    "t"          unit-variance Student-t, one nu per tenor (fitted offline; fitting code not published)
    "empirical"  pooled BTC+ETH TRAIN z, Gaussian-kernel smoothed, standardised to mean 0 / rms 1

THE SHAPE CARRIES NO SCALE. Every F here has variance 1, so the level stays with the vol forecast;
only the distribution of a 1-sigma move changes. A fat-tailed F with the same variance puts more
mass near 0 AND in the tails, so near-strike digitals move away from 0.5 faster than N(d2), while far wings carry more
probability.

DRIFT CONVENTION. The Gaussian's -w^2/2 log-median shift is kept for every shape: the log return is
-w^2/2 + w Z. For the Gaussian that makes E[S_T] = S exactly. A Student-t has NO moment generating
function, so E[e^{wZ}] is infinite on the full real line; on the table's support (|z| <= Z_MAX)
the mean error is E[S_T]/S - 1 = O(w^2 (kurtosis - 3) / 12 ... ) and is checked numerically, not assumed
(< 1e-6 at every w a BTC digital sees). Changing the shift to the
t's own truncated cumulant would move P by the same ~1e-6: it is left at the Gaussian's.

TABLES. var/model/digital_shape.json holds, per (shape, tenor), F and its density f on a uniform z
grid (step H, |z| <= Z_MAX). Lookup is a direct index plus a cubic Hermite step (value and slope at
both knots), so the interpolation error is O(H^4 f''') -- ~1e-10 -- and a call costs ~1 us of pure
Python. Beyond the grid F is clamped to its end values (tail mass there < 1e-6 for the fitted nu).

TENOR. p_above_tau blends by time to close: the 15-minute table for tau <= 15 min, the 1-hour
table for tau >= 60 min, and between them a mixture 1 - [(1-l) F_15m + l F_1h] with
l = ln(tau/15min) / ln(4). A mixture of two unit-variance zero-mean laws is still unit variance and
monotone, and it removes the price jump a hard switch between tables would put into the
hourly quotes. (tenor_for / TENOR_SPLIT_S remain for callers that want one table.) The final minute (inside the settlement window) is not priced here: kbtc.pricing.digital
keeps it Gaussian on the partially observed average.

SHAPE PER SERIES. settings.DIGITAL_SHAPE is a shape name, or a dict {series: shape} with an optional
"default" key. shape_for(series) resolves it; anything unknown or missing is "gaussian". The live
pricer (kbtc.pricing.digital.market_theo) resolves by mk["series"]; every fit-time caller resolves by
the series it fits, so fit and live read the same shape from the same place.

ONE IMPLEMENTATION, TWO SPEEDS.
    p_above_tau        scalar, pure Python + math (the live path; no numpy import)
    p_above_tau_vec    numpy, the same arithmetic element-wise (fit-time panels), <= 1e-12 of the scalar
    p_above_avg / p_above_avg_vec     the same on (sigma, tau): the 60-s-average w of
                                      kbtc.pricing.digital.effective_w, tau >= the window only
    invert_sigma / invert_sigma_vec   sigma from a price under a shape, by bisection
The Gaussian vec path evaluates math.erf element-wise (exact; no polynomial erf), so it agrees with
pricing.bs.p_above to the last ulp of the log. numpy is imported inside the vec functions only.
"""
import json
import math
import os

from kbtc import settings

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ART = os.path.join(ROOT, "var", "model", "digital_shape.json")
SHAPES = ("gaussian", "t", "empirical")
TENOR_SPLIT_S = 1200.0
SQRT2 = math.sqrt(2.0)

_TABLES = None          # {(shape, tenor): (z0, h, n, F, f)}


# ------------------------------------------------------------------ the t distribution (exact)
def _betacf(a, b, x, itmax=400, eps=3e-16):
    """Continued fraction for the incomplete beta (modified Lentz; Numerical Recipes 6.4)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def betainc(a, b, x):
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
           + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1.0 - x) / b


def t_unit_cdf(z, nu):
    """CDF of the UNIT-VARIANCE Student-t (nu > 2): Z = X sqrt((nu-2)/nu), X ~ t_nu."""
    x = z * math.sqrt(nu / (nu - 2.0))
    tail = 0.5 * betainc(0.5 * nu, 0.5, nu / (nu + x * x))
    return 1.0 - tail if x > 0 else tail


# ------------------------------------------------------------------ tables
def load(path=ART):
    """Read the artifact into the lookup cache (idempotent). Returns the parsed JSON header."""
    global _TABLES
    with open(path) as fh:
        a = json.load(fh)
    tabs = {}
    for key, t in a["tables"].items():
        shape, tenor = key.split("/")
        tabs[(shape, tenor)] = (float(t["z0"]), float(t["h"]), len(t["F"]), t["F"], t["f"])
    _TABLES = tabs
    return {k: v for k, v in a.items() if k != "tables"}


def tenor_for(tau_s):
    return "15m" if tau_s <= TENOR_SPLIT_S else "1h"


TAU_15M, TAU_1H = 900.0, 3600.0
_LN4 = math.log(TAU_1H / TAU_15M)


def tenor_weight(tau_s):
    """Weight on the 1-hour table: 0 at tau <= 15 min, 1 at tau >= 60 min, log-linear between."""
    if tau_s <= TAU_15M:
        return 0.0
    if tau_s >= TAU_1H:
        return 1.0
    return math.log(tau_s / TAU_15M) / _LN4


def cdf(z, shape, tenor):
    """F(z) for a tabled shape (cubic Hermite on the uniform grid)."""
    if shape == "gaussian":
        return 0.5 * (1.0 + math.erf(z / SQRT2))
    if _TABLES is None:
        load()
    z0, h, n, F, f = _TABLES[(shape, tenor)]
    u = (z - z0) / h
    if u <= 0.0:
        return F[0]
    i = int(u)
    if i >= n - 1:
        return F[n - 1]
    s = u - i
    s2 = s * s
    s3 = s2 * s
    return ((2 * s3 - 3 * s2 + 1) * F[i] + (s3 - 2 * s2 + s) * h * f[i]
            + (-2 * s3 + 3 * s2) * F[i + 1] + (s3 - s2) * h * f[i + 1])


def p_above_shape(S, K, w_total, shape="t", tenor="15m"):
    """P(S_T > K) with log return -w^2/2 + w Z, Z ~ F_shape,tenor (unit variance).

    shape "gaussian" returns the Kalshi repo's pricing.bs.p_above(S, K, w) itself (bit-identical)."""
    if shape == "gaussian":
        from kbtc.kalshi_root import bs
        return bs.p_above(S, K, w_total)
    if S <= 0.0 or K <= 0.0:
        return None
    if w_total <= 0.0:
        return 1.0 if S > K else 0.0
    z = (math.log(K / S) + 0.5 * w_total * w_total) / w_total
    return 1.0 - cdf(z, shape, tenor)


def p_above_tau(S, K, w_total, shape, tau_s):
    """p_above_shape with the tenor blended by time to close (see TENOR). This is what the proposed
    kbtc.pricing.digital hook calls; "gaussian" is bs.p_above bit for bit."""
    if shape == "gaussian":
        from kbtc.kalshi_root import bs
        return bs.p_above(S, K, w_total)
    lam = tenor_weight(tau_s)
    if lam == 0.0:
        return p_above_shape(S, K, w_total, shape, "15m")
    if lam == 1.0:
        return p_above_shape(S, K, w_total, shape, "1h")
    if S <= 0.0 or K <= 0.0:
        return None
    if w_total <= 0.0:
        return 1.0 if S > K else 0.0
    z = (math.log(K / S) + 0.5 * w_total * w_total) / w_total
    return 1.0 - ((1.0 - lam) * cdf(z, shape, "15m") + lam * cdf(z, shape, "1h"))


# ------------------------------------------------------------------ shape per series
def shape_for(series=None):
    """The shape name for `series` from settings.DIGITAL_SHAPE (str, or {series: shape, "default": s}).
    Unknown names and missing entries resolve to "gaussian"."""
    v = getattr(settings, "DIGITAL_SHAPE", "gaussian")
    if isinstance(v, dict):
        v = v.get(series, v.get("default", "gaussian")) if series is not None else v.get("default", "gaussian")
    return v if v in SHAPES else "gaussian"


# ------------------------------------------------------------------ scalar on (sigma, tau)
def w_avg(sigma_ann, tau_s, window_s=None):
    """kbtc.pricing.digital.effective_w, same arithmetic (bit-identical)."""
    window_s = settings.SETTLE_AVG_WINDOW_S if window_s is None else window_s
    return math.sqrt(sigma_ann * sigma_ann / settings.SECONDS_PER_YEAR * max(tau_s - 2.0 * window_s / 3.0, 0.0))


def p_above_avg(S, K, sigma_ann, tau_s, shape, window_s=None):
    """kbtc.pricing.digital.p_above_avg for tau >= the settlement window under an explicit shape
    (None inside the window, where the live pricer needs the observed prints)."""
    window_s = settings.SETTLE_AVG_WINDOW_S if window_s is None else window_s
    if S is None or K is None or S <= 0 or K <= 0 or tau_s < window_s:
        return None
    w = w_avg(sigma_ann, tau_s, window_s)
    if shape == "gaussian":
        from kbtc.kalshi_root import bs
        return bs.p_above(S, K, w)
    return p_above_tau(S, K, w, shape, tau_s)


SIG_LO, SIG_HI, BISECT_ITERS = 0.02, 5.0, 50


def invert_sigma(p, S, K, tau_s, shape, window_s=None, lo=SIG_LO, hi=SIG_HI, iters=BISECT_ITERS):
    """Annualised sigma with p_above_avg(S, K, sigma, tau, shape) == p, by bisection on [lo, hi]
    (kbtc.pricing.implied.invert's bracket and step count). None if the price does not bracket."""
    def f(s):
        v = p_above_avg(S, K, s, tau_s, shape, window_s)
        return None if v is None else v - p
    flo, fhi = f(lo), f(hi)
    if flo is None or fhi is None or flo * fhi > 0:
        return None
    for _ in range(iters):
        m = 0.5 * (lo + hi)
        fm = f(m)
        if (fm < 0) == (flo < 0):
            lo, flo = m, fm
        else:
            hi = m
    return 0.5 * (lo + hi)


# ------------------------------------------------------------------ vectorised (numpy, fit-time)
_NP_TABLES = {}
_ERF_VEC = None


def _np_table(shape, tenor):
    import numpy as np
    key = (shape, tenor)
    if key not in _NP_TABLES:
        if _TABLES is None:
            load()
        z0, h, n, F, f = _TABLES[key]
        _NP_TABLES[key] = (z0, h, n, np.asarray(F, dtype=float), np.asarray(f, dtype=float))
    return _NP_TABLES[key]


def cdf_vec(z, shape, tenor):
    """cdf() element-wise: the same Hermite step, the same end clamps."""
    import numpy as np
    global _ERF_VEC
    z = np.asarray(z, dtype=float)
    if shape == "gaussian":
        if _ERF_VEC is None:
            _ERF_VEC = np.frompyfunc(math.erf, 1, 1)
        return 0.5 * (1.0 + np.asarray(_ERF_VEC(z / SQRT2), dtype=float))
    z0, h, n, F, f = _np_table(shape, tenor)
    u = (z - z0) / h
    fin = np.isfinite(u)
    i = np.where(fin, np.clip(np.floor(np.where(fin, u, 0.0)), 0, n - 2), 0).astype(np.int64)
    s = u - i
    s2 = s * s
    s3 = s2 * s
    out = ((2 * s3 - 3 * s2 + 1) * F[i] + (s3 - 2 * s2 + s) * h * f[i]
           + (-2 * s3 + 3 * s2) * F[i + 1] + (s3 - s2) * h * f[i + 1])
    out = np.where(u <= 0.0, F[0], out)
    out = np.where(u >= n - 1, F[n - 1], out)
    return np.where(np.isnan(u), np.nan, out)


def _p_one_shape(S, K, w, shape, tau):
    import numpy as np
    with np.errstate(divide="ignore", invalid="ignore"):
        if shape == "gaussian":            # bs.p_above: d1 = (ln(F/K) + w^2/2)/w, d2 = d1 - w
            d1 = (np.log(S / K) + 0.5 * w * w) / w
            p = cdf_vec(d1 - w, "gaussian", None)
        else:
            z = (np.log(K / S) + 0.5 * w * w) / w
            lam = np.where(tau <= TAU_15M, 0.0, np.where(tau >= TAU_1H, 1.0, np.log(tau / TAU_15M) / _LN4))
            p = 1.0 - ((1.0 - lam) * cdf_vec(z, shape, "15m") + lam * cdf_vec(z, shape, "1h"))
    p = np.where(w <= 0.0, np.where(S > K, 1.0, 0.0), p)
    return np.where((S <= 0.0) | (K <= 0.0), np.nan, p)


def p_above_tau_vec(S, K, w_total, shape, tau_s):
    """p_above_tau element-wise (numpy). `shape` is one name or an array of names (one per element,
    e.g. shape_for per series). None -> NaN (S or K <= 0). Returns an ndarray (0-d for scalars)."""
    import numpy as np
    S, K, w, tau = np.broadcast_arrays(*(np.asarray(v, dtype=float) for v in (S, K, w_total, tau_s)))
    if isinstance(shape, str):
        return _p_one_shape(S, K, w, shape, tau)
    shp = np.broadcast_to(np.asarray(shape, dtype=object), S.shape)
    out = np.full(S.shape, np.nan)
    for name in set(shp.ravel().tolist()):
        m = shp == name
        out[m] = _p_one_shape(S[m], K[m], w[m], name, tau[m])
    return out


def w_avg_vec(sigma_ann, tau_s, window_s=None):
    """w_avg element-wise."""
    import numpy as np
    window_s = settings.SETTLE_AVG_WINDOW_S if window_s is None else window_s
    sigma_ann = np.asarray(sigma_ann, dtype=float)
    return np.sqrt(sigma_ann * sigma_ann / settings.SECONDS_PER_YEAR
                   * np.maximum(np.asarray(tau_s, dtype=float) - 2.0 * window_s / 3.0, 0.0))


def p_above_avg_vec(S, K, sigma_ann, tau_s, shape, window_s=None):
    """p_above_avg element-wise: NaN where the scalar returns None (tau < window, S or K <= 0)."""
    import numpy as np
    window_s = settings.SETTLE_AVG_WINDOW_S if window_s is None else window_s
    tau = np.asarray(tau_s, dtype=float)
    p = p_above_tau_vec(S, K, w_avg_vec(sigma_ann, tau, window_s), shape, tau)
    return np.where(np.broadcast_to(tau, p.shape) >= window_s, p, np.nan)


def invert_sigma_vec(p, S, K, tau_s, shape, window_s=None, lo=SIG_LO, hi=SIG_HI, iters=BISECT_ITERS):
    """invert_sigma element-wise (the same bracket, steps and midpoint rule); NaN where it does not
    bracket."""
    import numpy as np
    p = np.asarray(p, dtype=float)
    S, K, tau, p = np.broadcast_arrays(*(np.asarray(v, dtype=float) for v in (S, K, tau_s, p)))
    lo = np.full(p.shape, float(lo))
    hi = np.full(p.shape, float(hi))
    flo = p_above_avg_vec(S, K, lo, tau, shape, window_s) - p
    fhi = p_above_avg_vec(S, K, hi, tau, shape, window_s) - p
    ok = np.isfinite(flo) & np.isfinite(fhi) & (flo * fhi <= 0)
    for _ in range(iters):
        m = 0.5 * (lo + hi)
        fm = p_above_avg_vec(S, K, m, tau, shape, window_s) - p
        same = (fm < 0) == (flo < 0)
        lo = np.where(same, m, lo)
        flo = np.where(same, fm, flo)
        hi = np.where(same, hi, m)
    return np.where(ok, 0.5 * (lo + hi), np.nan)
