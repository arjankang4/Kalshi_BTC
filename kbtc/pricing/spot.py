"""Fast BTC spot: KXBTCPERP top of book minus an EWMA basis to BRTI.

    S_hat(t) = micro(t) - b(t),   b <- EWMA over reference prints of (micro(ts_ref) - BRTI(ts_ref))

Two timing choices carry the estimate:
  * The basis is sampled once per NEW reference print (its `ts_ms` advances), never per book
    update -- re-using one stale BRTI against a moving book would pull b toward the move.
  * The book side of each basis sample is the microprice AT THE PRINT'S OWN TIMESTAMP, looked
    up in a short history, not the book at receipt. BRTI is stamped on the whole second and
    reaches us a large fraction of a second later; pairing it with the receipt-time book
    would put that much perp drift into b.

Funding is carried only as a sanity check on the sign of b (a positive rate means longs pay,
i.e. the perp trades rich); it never enters S_hat. It is a time-averaged premium, stale within
the 8-hour interval, and the basis is expected to move around funding times.
"""
import bisect
import math
from collections import deque
from dataclasses import dataclass, field

from kbtc import settings


def microprice(bid, bsz, ask, asz):
    """Quote-weighted mid, the SPX/NDX way: CUBE-ROOT size weights (the Kalshi repo's pricing/theo.micro).

        micro = (bid * cbrt(ask_size) + ask * cbrt(bid_size)) / (cbrt(bid_size) + cbrt(ask_size))

    Heavy offer -> fair sits near the bid. The cube root damps one huge resting order, which on
    the perp (levels of 18,100 contracts against 38) would otherwise pin the mid to one touch.
    """
    wb, wa = max(bsz, 0.0) ** (1.0 / 3.0), max(asz, 0.0) ** (1.0 / 3.0)
    if wb + wa <= 0:
        return 0.5 * (bid + ask)
    return (bid * wa + ask * wb) / (wb + wa)


@dataclass
class SpotState:
    s_hat: float | None
    reliable: bool
    reasons: list = field(default_factory=list)
    book_px: float | None = None       # microprice (or mid) now
    mid: float | None = None
    basis: float | None = None
    ref: float | None = None           # latest BRTI
    ref_ts_ms: int | None = None
    book_age_s: float | None = None
    ref_age_s: float | None = None
    dev_bps: float | None = None
    n_basis: int = 0
    funding_rate: float | None = None
    funding_sign_ok: bool | None = None


class SpotEstimator:
    def __init__(self, half_life_s=settings.BASIS_HALF_LIFE_S, use_micro=settings.MICROPRICE):
        self.tau = half_life_s / math.log(2)
        self.use_micro = use_micro
        self.hist_t, self.hist_px = deque(), deque()     # exchange ms, book px
        self.basis = None
        self.n_basis = 0
        self.last_ref = None          # (price, ts_ms)
        self.last_book = None         # (bid, bsz, ask, asz, ts_ms, rx)
        self.funding_rate = None

    # -- inputs ----------------------------------------------------------------------------
    def on_book(self, bid, bsz, ask, asz, ts_ms, rx):
        px = microprice(bid, bsz, ask, asz) if self.use_micro else 0.5 * (bid + ask)
        self.last_book = (bid, bsz, ask, asz, ts_ms, rx)
        t = ts_ms if ts_ms is not None else int(rx * 1000)
        if self.hist_t and t < self.hist_t[-1]:
            t = self.hist_t[-1]                       # keep the history monotone
        self.hist_t.append(t)
        self.hist_px.append(px)
        cutoff = t - settings.MID_HISTORY_S * 1000
        while self.hist_t and self.hist_t[0] < cutoff:
            self.hist_t.popleft()
            self.hist_px.popleft()

    def book_px_at(self, ts_ms):
        """Last book px at or before ts_ms, or None if the history does not reach back."""
        if not self.hist_t or ts_ms < self.hist_t[0]:
            return None
        i = bisect.bisect_right(self.hist_t, ts_ms) - 1
        return self.hist_px[i]

    def on_reference(self, price, ts_ms):
        """A BRTI print. Only a print with a NEWER ts_ms updates the basis."""
        if self.last_ref is not None and ts_ms <= self.last_ref[1]:
            return False
        prev = self.last_ref
        self.last_ref = (price, ts_ms)
        px = self.book_px_at(ts_ms)
        if px is None:
            return False
        x = px - price
        if self.basis is None:
            self.basis = x
        else:
            dt = (ts_ms - prev[1]) / 1000.0 if prev else 1.0
            a = 1.0 - math.exp(-max(dt, 0.0) / self.tau)
            self.basis += a * (x - self.basis)
        self.n_basis += 1
        return True

    def on_funding(self, rate):
        self.funding_rate = rate

    # -- output ----------------------------------------------------------------------------
    def state(self, now):
        st = SpotState(s_hat=None, reliable=False, n_basis=self.n_basis,
                       basis=self.basis, funding_rate=self.funding_rate)
        if self.last_book is None:
            st.reasons.append("no_book")
            return st
        bid, bsz, ask, asz, ts_ms, rx = self.last_book
        st.mid = 0.5 * (bid + ask)
        st.book_px = microprice(bid, bsz, ask, asz) if self.use_micro else st.mid
        st.book_age_s = now - rx
        if bid >= ask:
            st.reasons.append("crossed_book")
        if st.book_age_s > settings.MAX_BOOK_AGE_S:
            st.reasons.append("stale_book")
        if self.last_ref is not None:
            st.ref, st.ref_ts_ms = self.last_ref
            st.ref_age_s = now - st.ref_ts_ms / 1000.0
            if st.ref_age_s > settings.MAX_REF_AGE_S:
                st.reasons.append("stale_ref")
        else:
            st.reasons.append("no_ref")
        if self.basis is None:
            st.reasons.append("no_basis")
            return st
        if self.n_basis < settings.MIN_BASIS_SAMPLES:
            st.reasons.append("basis_warmup")
        st.s_hat = st.book_px - self.basis
        if st.ref is not None:
            st.dev_bps = 1e4 * (st.s_hat - st.ref) / st.ref
            if abs(st.dev_bps) > settings.MAX_SPOT_DEV_BPS:
                st.reasons.append("spot_vs_ref")
        if self.funding_rate is not None and self.basis != 0:
            # Informational: a positive rate should come with a rich perp. Not a guard.
            st.funding_sign_ok = (self.funding_rate >= 0) == (self.basis >= 0)
        st.reliable = not st.reasons
        return st
