"""Every kbtc threshold and parameter. Nothing here is read from disk at import time.

Tuned values in this public copy are ILLUSTRATIVE: the live values are private. Structural
constants (venue URLs, contract sizes, rate-limit fallbacks, unit conversions) are real.

Units are in the name: _S seconds, _MS milliseconds, _BPS basis points, _USD dollars per BTC.
"""
import os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# ---- venue -----------------------------------------------------------------------------------
REST_BASE = "https://external-api.kalshi.com/trade-api/v2"
MARGIN_WS_URL = "wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin"
PERP_TICKER = "KXBTCPERP"
# Price per contract in dollars; contract is 0.0001 BTC, so BTC/USD = price / PERP_CONTRACT_BTC.
# Checked against GET /margin/markets/KXBTCPERP at start (perp.PerpRest.market) -- not assumed.
PERP_CONTRACT_BTC = 0.0001
EVENT_SERIES = ("KXBTC15M", "KXBTCD", "KXBTC")

# The companion `Kalshi` checkout (auth, pricing.bs, brackets, the SPX quoting rules, harness).
# Override with KBTC_KALSHI_ROOT; the default is a sibling checkout next to this repo.
KALSHI_ROOT = os.environ.get("KBTC_KALSHI_ROOT", str(_REPO.parent / "Kalshi"))
# Credentials: an .env with KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH, read at call time.
# Override with KBTC_KALSHI_ENV; the default is the .env inside KALSHI_ROOT.
KALSHI_ENV_PATH = os.environ.get("KBTC_KALSHI_ENV", os.path.join(KALSHI_ROOT, ".env.kalshi"))
# Scheduled macro releases (FOMC/CPI/NFP/...), read as plain JSON from the Kalshi checkout.
RELEASE_CALENDAR_PATH = os.path.join(KALSHI_ROOT, "var", "model", "release_calendar.json")

# ---- rate budget -------------------------------------------------------------------------------
# Kalshi meters perps and event contracts in SEPARATE buckets (docs: rate_limits), readable via
# /account/limits and /account/limits/perps. Both are re-read at start (ratebudget.from_limits);
# the tuples below (refill per second, cap) are only the fallback.
REQUEST_COST = 10
EVENT_READ_FALLBACK = (300, 900)
PERP_READ_FALLBACK = (400, 400)
# Fraction of each account-wide read bucket this process may use. The event bucket is shared
# with another process on the same account, which claims most of it.
EVENT_READ_SHARE = 0.10
# Fraction of the perp read bucket this process may use (the perp book itself is WebSocket,
# unmetered).
PERP_READ_SHARE = 0.4
# Split of this process's share across request categories. Weights, normalised at use.
PERP_CATEGORY_WEIGHTS = {"book": 0.5, "market": 0.3, "funding": 0.1, "candles": 0.1}
EVENT_CATEGORY_WEIGHTS = {"KXBTC15M": 0.4, "KXBTCD": 0.35, "KXBTC": 0.25}
BACKOFF_429_S = 0.25                 # first retry delay; doubles per consecutive 429
BACKOFF_429_MAX_S = 8.0
GET_RETRIES = 5
USAGE_LOG_EVERY_S = 10.0

# ---- fast spot -------------------------------------------------------------------------------
# b = EWMA(perp_mid - BRTI), updated once per NEW reference print (1 Hz). Half-life in seconds.
BASIS_HALF_LIFE_S = 90.0
# Book mid is looked up at the reference print's own timestamp, not at receipt.
MID_HISTORY_S = 10.0
MICROPRICE = True                    # size-weighted top of book; False = plain mid
MAX_BOOK_AGE_S = 2.0                 # no book update for this long -> unreliable
MAX_REF_AGE_S = 5.0                  # reference_price ts_ms older than this -> unreliable
MAX_SPOT_DEV_BPS = 20.0              # |S_hat - latest BRTI| beyond this -> unreliable
MIN_BASIS_SAMPLES = 10               # b is not trusted before this many reference prints

# ---- settlement ----------------------------------------------------------------------------
SETTLE_AVG_WINDOW_S = 60             # simple average of 60 BRTI prints before close (rules text)
# Return shape of the digital for tau >= the settlement window: "gaussian" (N(d2) through
# pricing.bs) | "t" | "empirical" (unit-variance tables in var/model/digital_shape.json, read by
# kbtc/pricing/digital_shape.py). Either ONE name for every series, or a dict per series with an
# optional "default"; unknown names and unlisted series resolve to "gaussian"
# (digital_shape.shape_for).
DIGITAL_SHAPE = "gaussian"

# ---- volatility (baseline; the RV model replaces this) --------------------------------------
SECONDS_PER_YEAR = 365.0 * 86400.0   # BTC trades 24/7
VOL_EWMA_HALF_LIFE_MIN = 45.0        # on 1-minute perp mid log returns
VOL_LOOKBACK_MIN = 4 * 60
VOL_REFRESH_S = 300.0
VOL_OVERRIDE_ANN = None              # e.g. 0.45 to price at a flat vol

# ---- theo vs book monitor ------------------------------------------------------------------
POLL_S = {"KXBTC15M": 2.0, "KXBTCD": 5.0, "KXBTC": 10.0}
MIN_TAU_S = 5.0                      # skip markets closing sooner than this
LOG_DIR = str(_REPO / "var" / "log")

# =============================================================================================
# LIVE QUOTING
# =============================================================================================
EVENT_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
QUOTE_SERIES = ("KXBTC15M", "KXBTCD")   # 15-minute up/down and the 1-hour above/below ladder
# Only markets closing within this many seconds are quoted: KXBTCD also lists the 17:00 daily and
# Friday weekly ladders, and only its CURRENT-HOUR event is in scope.
QUOTE_MAX_TAU_S = {"KXBTC15M": 900, "KXBTCD": 3600}
# The FAST path prices only the markets within this margin of the theo band, nearest the money,
# at most MAX_ACTIVE_MARKETS of them (selected on every pass of the slow loop).
ACTIVE_THEO_MARGIN = 0.15
MAX_ACTIVE_MARKETS = 3
# Per-series cap on the active set.
MAX_ACTIVE_PER_SERIES = {"KXBTC15M": 1, "KXBTCD": 1}

# ---- allocation of the account's event-contract buckets while quoting -----------------------
# Event-contract READ share for the quoter (replaces EVENT_READ_SHARE when quoting); the rest is
# left for ad hoc tools and recorders. WRITES: the whole event write bucket.
QUOTE_EVENT_READ_SHARE = 0.8
QUOTE_EVENT_WRITE_SHARE = 1.00
EVENT_WRITE_FALLBACK = (300, 900)
ORDER_TOKEN_COST = 10                 # create, per order (docs: default cost)
CANCEL_TOKEN_COST = 2                 # DELETE /portfolio/events/orders/{id}

# ---- realized vol ----------------------------------------------------------------------------
# PRODUCTION RV = kbtc.rvol.intraday_forecast (var/model/rv_intraday.json), fed live by
# kbtc.rvol.live_intraday.
VOL_UPDATE_INTERVAL_S = 2.0           # RV forecast, lean inputs and spot anchor refresh cadence

# ---- theo vol source ---------------------------------------------------------------------------
# "rv" = the RV forecast at the market's own tenor (kbtc.rvol.intraday_forecast);
# "implied_1d" = the 1-day KXBTCD 17:00-ET ladder; "implied_1h" = the current-hour KXBTCD ladder.
# With an implied theo the RV forecast is the LEAN; with "rv" as the theo there is no lean.
THEO_VOL_SOURCE = "rv"
# RV artifact + clock. "flat": rv_intraday.json, sigma_ann at the market's tenor.
# "calendar": rv_intraday_cal.json (pooled crypto intraday variance calendar) with sigma over
# EXACTLY (now, close] from the calendar (intraday_forecast.variance_to_close).
# "calendar15": rv_intraday_cal.json's model with each horizon's level refitted on 15-minute
# returns.
RV_CLOCK = "calendar15"
# QUOTED PRICE = a blend of the book mid and the RV theo, per series:
#     logit(p_q) = w * logit(p_mid) + (1 - w) * logit(p_rv)
BOOK_BLEND_W = {"KXBTC15M": 0.4, "KXBTCD": 0.5}
# Refuse a market when |p_q - mid| exceeds this (cents): a gap that large makes the quote
# one-sided purely because of theo - mid. With HYSTERESIS, so a market near a single threshold
# does not flip every tick: refuse above OFF, re-enable only after the gap has stayed below ON
# for REENABLE_S seconds.
MAX_QUOTE_MID_GAP_OFF_C = 2.0
MAX_QUOTE_MID_GAP_ON_C = 1.0
QUOTE_MID_GAP_REENABLE_S = 5.0
IV_DAILY_MIN_TAU_S = 2 * 3600          # a 1-day ladder closer than 2 h is too short to call "1-day"
IV_DAILY_MAX_TAU_S = 30 * 3600
SKEW = 0.0                            # skew: zero; flat Black-Scholes on the 60-s average
IV_REFRESH_S = 2.0                    # hourly ladder re-read cadence (REST)
IV_HOURLY_MIN_TAU_S = 600             # an hourly with < 10 min left is too close to invert
IV_HOURLY_MAX_TAU_S = 2 * 3600
IV_MAX_SPREAD = 0.05                  # rungs wider than this are not used
IV_MID_BAND = (0.10, 0.90)            # rungs whose mid is outside this carry no vol information
IV_MIN_RUNGS = 3
IV_MAX_AGE_S = 60.0                   # no fresh implied vol for this long -> no quote
IV_SMOOTH_READS = 3                   # rolling median over the last k ladder reads (1 = raw)

# ---- quoting (strategy.quoting.decide_ladder + strategy.orders.plan_side) ----------------------
THEO_BAND_MIN = 0.03                  # quote only theo in [min, max] per $1 payout
THEO_BAND_MAX = 0.97
MIN_EDGE = 0.005                      # minimum required edge, dollars per contract
QUOTE_COUNT = "2.00"                  # contracts per order
TIERS_ENABLED = False                 # one rung per side
QUOTE_CUTOFF_S = 90                   # stop quoting (and pull) a market this many seconds before close
FAST_MIN_MOVE_USD = 2.0               # a perp microprice move below this does not re-price

# ---- RV lean (pricing.lean.edge_adjust on the required edge, uncentred) ----------------------
LEAN_ENABLED = False

# ---- all-in lean: signal -> alpha -> trading lean (kbtc/strategy/leanpipe.py) ------------------
# Replaces the fixed-shape lean above whenever it is active. Signals (s_rv, s_move) are
# orthogonalised and weighted by their alpha on historical maker FILL MARKOUTS (weights fitted
# offline into var/model/lean_weights.json; the fitting code is not published); the favoured side's
# required edge falls by |lean| (floored at 0), the other side's rises by |lean|. Active only when
# the weight file exists and passed its held-out test, read at quoter start; otherwise no lean.
LEANPIPE_ENABLED = True
LEAN_DISAGREEMENT_CLAMP = 8.0         # lean clamp, probability points (p_rv vs theo)
LEAN_DEFENSIVE_CENTS = 1.5            # extra required edge on the defensive side at the clamp
LEAN_AGGRESSIVE_CENTS = 0.75          # required edge released on the aggressive side at the clamp
# While the lean is active, the side it is AGAINST may only JOIN at an edge that clears its raised
# requirement (quoter.defensive_gate); otherwise the SPX rule joins at any positive edge.
LEAN_DEFENSIVE_JOIN_GATE = True

# ---- quote width caps and the R curve (kbtc/pricing/edgecurve.py) ------------------------------
# R = sqrt(delta^2 + vega^2 + theta^2), dollars per contract (delta per $1, vega per vol point,
# theta per minute). Anchors per TENOR: R_MIN (bonus starts), R_P99 (the knee), R_MAX (refuse
# beyond). Set from the distribution of R on historical data.
R_ANCHORS = {"KXBTC15M": {"R_MIN": 0.002, "R_P99": 0.10, "R_MAX": 0.20},
             "KXBTCD":   {"R_MIN": 0.001, "R_P99": 0.03, "R_MAX": 0.05}}
# Half-width caps in cents by time-to-close bucket (seconds); outside every bucket = no quote.
# MIN is the posting floor (>= 1 tick). MAX: a side needing more edge than this is not quoted.
WIDTH_TAU_BUCKETS = {"KXBTC15M": ((600, 900), (300, 600), (120, 300), (60, 120)),
                     "KXBTCD":   ((2400, 3600), (1200, 2400), (300, 1200), (60, 300))}
MIN_HALF_WIDTH_C = {"KXBTC15M": (1, 2, 2, 3), "KXBTCD": (1, 1, 1, 2)}
MAX_HALF_WIDTH_C = {"KXBTC15M": (6, 8, 10, 12), "KXBTCD": (3, 4, 6, 7)}

# ---- risk ------------------------------------------------------------------------------------
POSITION_LIMIT_CONTRACTS = 50         # |net position| per market
MAX_RESTING_ORDERS = 30               # across all quoted markets (one bid + one offer each)
DAILY_LOSS_LIMIT_USD = 500.0          # ACCOUNT equity loss vs the ET-day baseline -> HALT, pull, stop
ACCOUNT_CHECK_S = 5.0                 # how often the account equity is re-read
# Order-reject circuit breaker: a failed place must not simply be retried on every tick (e.g. a
# balance held on the wrong exchange shard rejects every create).
REJECT_BACKOFF_AFTER = 3               # consecutive rejects on one market -> back that market off
REJECT_BACKOFF_S = 30.0
REJECT_HALT_COUNT = 20                 # this many rejects inside REJECT_HALT_WINDOW_S -> HALT the session
REJECT_HALT_WINDOW_S = 60.0
CANCEL_FAIL_HALT = 3                  # consecutive non-2xx cancels -> HALT (an order may be stuck live)
# A reject that says the ACCOUNT cannot trade here (not this price/market) halts at once: retrying it
# only hammers the order endpoint, which is how an API key gets throttled.
SYSTEMIC_REJECT_CODES = ("insufficient_shard_balance", "insufficient_balance", "insufficient_funds",
                         "unauthorized", "forbidden", "account_disabled")
SYSTEMIC_REJECT_STATUS = (401, 403)
HALT_FILE = str(_REPO / "var" / "HALT")

# ---- customer-flow vol lean + inventory leans ------------------------------------------------
# ALL OFF (every gain and cap is 0). Nothing here is read by the quoter until it is wired.
FLOW_WINDOW_S = 300                   # I_t = sum f / sum |f| over this trailing window, f = side x size x vega
FLOW_BASELINE_S = 3600                # EWMA time constant of the slow baseline (and of its sd)
FLOW_N0 = 10.0                        # shrinkage z~ = z n / (n + n0), n = kept taker orders in window
KAPPA_FLOW = 0.0                      # Delta sigma_flow = -KAPPA_FLOW z~ (annualised vol per unit z~); 0 = off
FLOW_HORIZON_S = 120                  # Delta sigma_flow decays linearly to 0 over this horizon
CAP_FLOW = 0.0                        # |s_flow| cap, probability points; <= 0 disables the component
FLOW_DROP_SWEEPS = True               # taker orders printing at > 1 price level are dropped
FLOW_LAG_S = 1                        # z~(t) counts taker orders stamped before floor(t) - FLOW_LAG_S
GAMMA_VEGA = 0.0                      # vega risk aversion in dsigma = -GAMMA_VEGA Var(dsigma_h) V; 0 = off
MU_SEL = 0.0                          # vol selection per unit of fresh vega (dsigma -= MU_SEL V_eff); 0 = off
TAU_SEL_S = 120.0                     # fresh-fill decay for V_eff
CAP_POS = 0.0                         # |vega inventory lean| cap per side, probability points; <= 0 disables
GAMMA_DELTA = 0.0                     # delta risk aversion (1/$), D = net $ delta over ALL BTC markets; 0 = off
CAP_DELTA_C = 0.0                     # |delta inventory lean| cap per side, cents; <= 0 disables the component
CAP_TOTAL = 0.0                       # |alpha lean + inventory leans| cap per side, probability points; <= 0 disables
INV_VAR_HORIZON_S = 120               # h for Var(dS_h) and Var(dsigma_h)
