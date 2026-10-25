"""KXBTC15M quoter: SPX/NDX quoting logic on a BTC theo, instrumented for a speed test.

    python -m kbtc.strategy.quoter --minutes 15                       # DRY: decides, sends nothing
    KBTC_ALLOW_LIVE=1 python -m kbtc.strategy.quoter --minutes 15 --live

TWO LOOPS, the SPX split (the companion Kalshi repo's strategy/fastcancel):
  slow         every VOL_UPDATE_INTERVAL_S. vol: the RV forecast (sigma_rv); implied vol from the 1-hour
               KXBTCD ladder (sigma_impl, the theo vol when THEO_VOL_SOURCE is implied); market selection; the spot ANCHOR
               (S_hat and the perp microprice it was read against); risk checks; markouts.
  fast         wakes on EVERY perp top-of-book change (and on our market's book changing).
               Delta-adjust, the SPX way: the forward is the anchor shifted by the perp move,
               F = S_anchor + (micro_now - micro_anchor), and the market is RE-PRICED exactly at
               F (fastcancel.reprice_shifted: "the move is applied as a delta, not a level").
               Then decide_ladder -> plan_side -> cancel/place. Every step is timestamped.

THEO: the 60-second-average digital at F with the promoted RV sigma (THEO_VOL_SOURCE = rv), zero
skew, then BLENDED with the book mid in logit space (BOOK_BLEND_W); a market whose blended price
is more than MAX_QUOTE_MID_GAP_OFF_C from mid is refused (hysteresis: back below ON for REENABLE_S). The fixed-shape RV lean is off (LEAN_ENABLED).
REQUIRED EDGE to post = max(0, floor(tau) + bonus(R) + lean) + backoff (pricing.edgecurve);
to stay, edge > 0 (plan_side).
REFUSED: theo outside [THEO_BAND_MIN, THEO_BAND_MAX]; R > R_MAX[tenor] or tau outside the width
buckets (pricing.edgecurve); a SIDE whose required edge exceeds MAX_HALF_WIDTH; spot unreliable; no fresh
implied vol; tau < QUOTE_CUTOFF_S; position at the limit on that side; HALT file present.

LOG (var/log/quoter-<start>.jsonl), one record per event:
  tick    perp exch ts, our receive time, theo-done time, decision, send/ack times per order
  stale   a resting order whose edge went <= 0, and how long until it was gone
  fill    fill + theo/mid at fill; markouts at +1/+5/+30/+60 s
  cycle   the slow loop's state, once per slow-loop pass
"""
import argparse
import json
import math
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from kbtc import settings
from kbtc.pricing import digital, edgecurve, greeks, implied
from kbtc.pricing.spot import SpotEstimator, microprice
from kbtc.rvol.live_intraday import LiveIntraday
from kbtc.strategy import leanpipe
from kbtc.strategy import spx_logic as L
from kbtc.strategy.account_risk import DailyLoss
from kbtc.strategy.queue_est import QueueTracker
from kbtc.venue import ratebudget
from kbtc.venue.eventfeed import EventFeed
from kbtc.venue.events import EventRest, priceable
from kbtc.venue.orders import Writer
from kbtc.venue.perp import PerpFeed
from kbtc.venue.rest import RestClient

MARKOUTS_S = (1, 5, 30, 60)


def defensive_gate(rungs, need_micros):
    """Keep only rungs whose edge clears `need` -- for the side the lean is AGAINST.

    The SPX join rule (strategy.quoting.decide) posts a JOIN at any positive edge; `need` only gates
    PENNY and the width cap. So the lean's defensive side (need = base + |lean|) never stopped joins,
    and a join on the side the lean says is wrong is exactly the fill it is meant to avoid. The
    favoured side keeps the permissive join.
    """
    return [r for r in rungs if r.edge is not None and int(round(r.edge * 1_000_000)) >= need_micros]


def side_within_limit(pos, side, resting_n, lot, limit):
    """May this side keep/post orders? Exposure = position + what already rests on the side, or
    position + one new lot when nothing rests -- never both (that double count made the limit flip
    every tick: place -> "too big" -> cancel -> place)."""
    add = max(resting_n, lot)
    worst = (pos + add) if side == "bid" else (-pos + add)
    return worst <= limit + 1e-9


class Journal:
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "a", buffering=1 << 16)
        self.lock = threading.Lock()

    def w(self, kind, **rec):
        rec["kind"] = kind
        rec.setdefault("t", time.time())
        with self.lock:
            self.fh.write(json.dumps(rec, default=str) + "\n")

    def close(self):
        with self.lock:
            self.fh.flush()
            self.fh.close()


class Quoter:
    def __init__(self, live=False, minutes=15.0):
        self.live = live
        self.t_end = time.time() + 60 * minutes
        start = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        os.makedirs(settings.LOG_DIR, exist_ok=True)
        self.j = Journal(os.path.join(settings.LOG_DIR, f"quoter-{start}{'' if live else '-dry'}.jsonl"))
        self.stop = threading.Event()
        self.lock = threading.Lock()

        probe = RestClient()
        lim = probe.account_limits(perps=False)
        self.read_budget = ratebudget.from_limits(
            "event_read", lim, settings.EVENT_READ_FALLBACK, settings.QUOTE_EVENT_READ_SHARE,
            {"KXBTCD": 0.5, "KXBTC15M": 0.5},
            log_path=os.path.join(settings.LOG_DIR, f"rate_usage-{start}.jsonl"))
        wr = lim.get("write") or {}
        self.write_budget = ratebudget.RateBudget(
            "event_write", float(wr.get("refill_rate", settings.EVENT_WRITE_FALLBACK[0])),
            float(wr.get("bucket_capacity", settings.EVENT_WRITE_FALLBACK[1])),
            settings.QUOTE_EVENT_WRITE_SHARE, {"write": 1.0},
            log_path=os.path.join(settings.LOG_DIR, f"rate_usage-{start}.jsonl"))
        self.events = EventRest(RestClient(self.read_budget))
        self.writer = Writer(self.write_budget, live=live)
        self.daily = DailyLoss(RestClient())            # account-equity loss limit, persistent
        self._acct_t = 0.0

        self.spot = SpotEstimator()
        from kbtc.rvol import intraday_forecast as IF
        self.IF = IF
        art_path = {"calendar": IF.ART_CAL_PATH, "calendar15": IF.ART_CAL15_PATH}.get(settings.RV_CLOCK)
        self.rvf = LiveIntraday(art=IF.load(art_path) if art_path else None)
        self.rv_fc = {"ok": False, "reason": "init"}
        self.sig_close = {}                      # ticker -> calendar sigma to close (RV_CLOCK=calendar)
        self.perp = PerpFeed(on_book=self._on_perp, on_ticker=self._on_ticker)
        self.qt = QueueTracker()                 # L2 queue-position estimate per resting order
        self.feed = EventFeed(on_book=self._on_event_book, on_fill=self._on_fill,
                              on_level=lambda t, sd, pm, d, before: self.qt.on_level(t, sd, pm, d, before),
                              on_trade=lambda t, ts, px, n: self.qt.on_trade(t, ts, px, n)
                              if t and ts and px and n else None)
        self.brti = []                         # (ts_s, price): settlement prints + the lean's 300 s move
        # Signal -> alpha -> trading lean (kbtc/strategy/leanpipe.py). Loaded ONCE here, so a new weight
        # file takes effect only at the next start. Active only when the file
        # exists AND passed its held-out markout test; otherwise None and no lean is applied.
        self.leanpipe = leanpipe.load() if settings.LEANPIPE_ENABLED else None
        self.wake = threading.Event()
        self.last_perp = None                  # (exch_ms, rx, micro, mid)
        self.perp_seq = 0                      # bumps on every perp microprice change
        self.seen_perp_seq = 0

        # slow-loop state, read by the fast path under self.lock
        self.anchor = None                     # (S_hat, micro_at_anchor, t)
        self.sigma_impl = None
        self.sigma_impl_t = 0.0
        self.iv_reads = []
        self.iv_detail = {}
        self.markets = {}                      # ticker -> parsed market (ACTIVE set, fast path)
        self.candidates = {}                   # every quotable market (refreshed every 10 s)
        self.spot_state = None

        # orders/positions
        self.resting = defaultdict(list)       # (ticker, side) -> [order dict]
        self.position = defaultdict(float)
        self.cash = 0.0
        self.last_fill_t = {}                  # (ticker, side) -> monotonic
        self.stale_since = {}                  # order_id -> (t, edge_micros)
        self.zombie = {}                       # (ticker, side) -> (monotonic armed, touch then)
        self._last_reasons = {}                # ticker -> refusal reasons last journaled
        self.gap_state = {}                    # ticker -> quote-mid gap hysteresis state
        self.reject_run, self.reject_until, self.reject_times = {}, {}, []
        self.pending_verify = {}               # order_id -> 404'd cancel awaiting verification
        self.pending_markouts = []
        self.halted = None
        self.stats = defaultdict(float)

    # ------------------------------------------------------------------ feed callbacks
    def _on_perp(self, feed, rx):
        top = feed.top_usd()
        if top is None:
            return
        bid, bsz, ask, asz, ts_ms, _ = top
        self.spot.on_book(bid, bsz, ask, asz, ts_ms, rx)
        m = microprice(bid, bsz, ask, asz)
        prev = self.last_perp
        if prev is None or abs(m - prev[2]) > 1e-9:
            self.perp_seq += 1
        self.last_perp = (ts_ms, rx, m, 0.5 * (bid + ask))
        if prev is None or abs(m - prev[2]) > 1e-9:
            self.stats["perp_changes"] += 1
            self.wake.set()

    def _on_ticker(self, feed, t):
        r = t.get("reference")
        if r is not None and self.spot.on_reference(r.value, r.ts_ms):
            self.brti.append((r.ts_ms / 1000.0, r.value))
            # >= 400 prints kept (1 Hz): the lean's move signal looks 300 s back. move_z scans this
            # list per market per tick -- do not raise the cap without moving it to the slow loop.
            if len(self.brti) > 500:
                del self.brti[:100]
        if t.get("funding_rate") is not None:
            self.spot.on_funding(t["funding_rate"])

    def _on_event_book(self, ticker, rx, ts_ms):
        if ticker in self.markets:
            self.stats["event_book_changes"] += 1
            self.wake.set()

    def _on_fill(self, f):
        oid = f.get("order_id")
        if oid in self.pending_verify:
            pv = self.pending_verify.pop(oid)
            self.j.w("cancel_404_resolved", order_id=oid, by="fill", ticker=pv["ticker"],
                     lag_s=round(time.time() - pv["t"], 4))
        tk = f.get("market_ticker")
        n = float(f.get("count_fp") or 0)
        px = float(f.get("yes_price_dollars") or 0)
        action, yes_no = f.get("action"), f.get("side")
        # YES-terms direction: buy yes / sell no = long YES
        sign = +1 if (action == "buy") == (yes_no == "yes") else -1
        with self.lock:
            side = None
            for (t, s), lst in self.resting.items():
                for o in lst:
                    if o.get("order_id") == oid:
                        side = s
                        o["count"] = f"{max(0.0, float(o['count']) - n):.2f}"
                self.resting[(t, s)] = [o for o in lst if float(o["count"]) > 1e-9]
            if f.get("post_position_fp") is not None:
                self.position[tk] = float(f["post_position_fp"])
            else:
                self.position[tk] += sign * n
            self.cash -= sign * n * px
            self.cash -= float(f.get("fee_cost") or 0)
            if side:
                self.last_fill_t[(tk, side)] = time.monotonic()
        theo = self._theo_now(tk)
        top = self.feed.top(tk)
        mid = None if not top or top[0] is None or top[1] is None else 0.5 * (top[0] + top[1])
        self.stats["fills"] += 1
        # Queue context at the fill: displayed size at our price and the size
        # resting AHEAD of ours cannot be known exactly from an L2 book; journal the level size
        # and the exchange's own trade id so the matched public print can be joined exactly.
        lvl = None
        try:
            with self.feed.lock:
                b = self.feed.books.get(tk)
                if b is not None:
                    from kbtc.venue.eventfeed import to_micros
                    pm = to_micros(px) if px else None
                    lvl = {"yes_at_px": b.yes.get(pm), "no_at_1mpx": b.no.get(1_000_000 - pm) if pm else None}
        except Exception:                                                 # noqa: BLE001
            lvl = None
        self.j.w("fill", ticker=tk, order_id=oid, side=side, yes_dir=sign, count=n, price=px,
                 trade_id=f.get("trade_id"), level_size_after=lvl, ts_ms=f.get("ts_ms"),
                 queue=self.qt.snapshot(oid),
                 is_taker=f.get("is_taker"), fee=f.get("fee_cost"), theo=theo, mid=mid,
                 rx=f.get("_rx"), ts=f.get("ts"), position=self.position[tk])
        if not any(o.get("order_id") == oid for lst in self.resting.values() for o in lst):
            self.qt.done(oid)                        # fully filled: stop tracking its queue
        now = time.time()
        for h in MARKOUTS_S:
            self.pending_markouts.append((now + h, h, tk, sign, px, oid))

    # ------------------------------------------------------------------ pricing
    def _forward(self):
        """Delta-adjusted forward: anchor S_hat shifted by the perp microprice move since."""
        with self.lock:
            a = self.anchor
        lp = self.last_perp
        if a is None or lp is None:
            return None, None
        move = lp[2] - a[1]
        return a[0] + move, move

    def _observed(self, close_ts):
        lo = close_ts - settings.SETTLE_AVG_WINDOW_S
        return [p for t, p in list(self.brti) if lo <= t < close_ts]

    TENOR = {"KXBTC15M": "15m", "KXBTCD": "1h"}

    def _sigmas(self, series, mk=None):
        """(theo sigma, lean sigma) for a market series; either None when unavailable.

        implied_*: theo = the ladder's implied sigma if read within IV_MAX_AGE_S; lean = the
        promoted RV forecast at the market's own tenor. rv: theo = that forecast; no lean.
        """
        fc = self.rv_fc
        rv = fc.get("sigma_ann_" + self.TENOR[series]) if fc.get("ok") else None
        if settings.RV_CLOCK in ("calendar", "calendar15") and mk is not None:
            # sigma over exactly (now, close], computed once per pass of the slow loop
            # (variance_to_close is ~0.7 ms/call -- too slow for the per-tick fast path).
            rv = self.sig_close.get(mk["ticker"]) if fc.get("ok") else None
        if settings.THEO_VOL_SOURCE == "rv":
            return rv, None
        fresh = self.sigma_impl is not None and time.time() - self.sigma_impl_t < settings.IV_MAX_AGE_S
        return (self.sigma_impl if fresh else None), rv

    def _theo_now(self, tk):
        mk = self.markets.get(tk)
        F, _ = self._forward()
        sig_t, _ = self._sigmas(mk["series"], mk) if mk else (None, None)
        if mk is None or F is None or sig_t is None:
            return None
        now = time.time()
        obs = self._observed(mk["close_ts"]) if mk["close_ts"] - now < settings.SETTLE_AVG_WINDOW_S else None
        v, _ = digital.market_theo(mk, F, sig_t, now, obs)
        return v

    # ------------------------------------------------------------------ fast path
    def fast_loop(self):
        while not self.stop.is_set():
            self.wake.wait(0.05)
            self.wake.clear()
            if self.stop.is_set():
                break
            try:
                self.fast_once()
            except Exception as e:                                    # noqa: BLE001
                self.stats["fast_errors"] += 1
                self.j.w("error", where="fast", err=f"{type(e).__name__}: {str(e)[:200]}")

    def fast_once(self):
        t_wake = time.time()
        lp = self.last_perp
        seq = self.perp_seq
        trigger = "perp" if seq != self.seen_perp_seq else "event_or_timer"
        self.seen_perp_seq = seq
        F, move = self._forward()
        with self.lock:
            spot_ok = self.spot_state is not None and self.spot_state.reliable
            markets = dict(self.markets)
        now = time.time()
        for tk, mk in markets.items():
            top = self.feed.top(tk)
            sig_i, sig_rv = self._sigmas(mk["series"], mk)   # theo sigma, lean sigma (None = none)
            iv_fresh = sig_i is not None
            reasons = []
            if self.halted:
                reasons.append(f"halt:{self.halted}")
            if not spot_ok:
                reasons.append("spot_unreliable")
            if not iv_fresh:
                reasons.append("no_fresh_iv")
            if F is None:
                reasons.append("no_forward")
            if top is None:
                reasons.append("no_event_book")
            tau = mk["close_ts"] - now
            if tau < settings.QUOTE_CUTOFF_S:
                reasons.append("cutoff")
            if self.reject_until.get(tk, 0.0) > now:
                reasons.append("reject_backoff")
            theo = p_rv = g = None
            p_theo = p_mid = None
            shadow = None
            lean_all, lean_attr = 0.0, None
            if not reasons:
                obs = self._observed(mk["close_ts"]) if tau < settings.SETTLE_AVG_WINDOW_S else None
                theo, _ = digital.market_theo(mk, F, sig_i, now, obs)
                if sig_rv is not None and settings.LEAN_ENABLED:
                    p_rv, _ = digital.market_theo(mk, F, sig_rv, now, obs)
                g = greeks.market_greeks(mk, F, sig_i, now, obs)
                p_theo = theo                            # the RV digital, before the blend
                shadow = self._shadow_v(mk, F, now, obs, theo)
                if theo is None:
                    reasons.append("no_theo")
                elif not settings.THEO_BAND_MIN <= theo <= settings.THEO_BAND_MAX:
                    reasons.append("theo_out_of_band")
                else:
                    # Blend with the book mid (settings.BOOK_BLEND_W); refuse a one-sided gap.
                    bb0, ba0 = (top[0], top[1]) if top else (None, None)
                    if bb0 is None or ba0 is None or not 0 < bb0 < ba0 < 1:
                        reasons.append("no_two_sided_book")
                    else:
                        p_mid = 0.5 * (bb0 + ba0)
                        w = settings.BOOK_BLEND_W[mk["series"]]
                        lg = lambda p: math.log(p / (1 - p))                 # noqa: E731
                        z = w * lg(p_mid) + (1 - w) * lg(theo)
                        theo = 1.0 / (1.0 + math.exp(-z))
                        if self._gap_blocked(tk, abs(theo - p_mid) * 100, now):
                            reasons.append("quote_mid_gap")
                # The all-in lean (leanpipe): alpha-weighted, orthogonalised signals -> one lean in
                # probability points, applied per side below. Only on the series it was fitted for.
                lp_art = self.leanpipe
                if lp_art is not None and not reasons and p_theo is not None and \
                        mk["series"] == lp_art.get("series"):
                    zm = leanpipe.move_z(list(self.brti), now, lp_art["l_move_s"], sig_i)
                    pmv = (digital.market_theo(mk, F, sig_i * math.exp(lp_art["b_move"] * abs(zm)),
                                               now, obs)[0] if zm is not None else None)
                    lean_all, lean_attr = leanpipe.combine(leanpipe.signals(p_theo, theo, pmv), lp_art)
                    lean_attr["z_move"] = zm
                # Width caps + R curve: floor(tau) + bonus(R); refuse past R_MAX.
                base_m, max_m, edet = edgecurve.post_requirement(g["R"], tau, mk["series"])
                if base_m is None:
                    reasons.append(edet["reason"])
            t_theo = time.time()
            bb, ba = (top[0], top[1]) if top else (None, None)
            desired, notes = {}, {}
            if not reasons:
                pos = self.position[tk]
                for side in ("bid", "ask"):
                    # Count RESTING size too: position + everything resting on this side + one
                    # more order must fit: resting orders can fill before the filled-position
                    # check sees them, overshooting the limit.
                    rest_n = sum(float(o["count"]) for o in self.resting[(tk, side)])
                    pend_n = sum(float(pv["order"].get("count") or 0)
                                 for pv in self.pending_verify.values()
                                 if pv["ticker"] == tk and pv["side"] == side)
                    # The side's exposure is position + what already rests, OR position + one new
                    # lot when nothing rests -- not both. Counting a new lot on top of a resting one
                    # made the limit flip every tick (place -> "too big" -> cancel -> place ...),
                    # burning the write budget into 429s.
                    if not side_within_limit(pos, side, rest_n + pend_n, float(settings.QUOTE_COUNT),
                                             settings.POSITION_LIMIT_CONTRACTS):
                        notes[side] = "position_limit"
                        continue
                    if lean_attr is not None:
                        # favoured side: need falls by |lean| (floored at 0 below); other side rises
                        adj = leanpipe.side_adjust_micros(lean_all, side)
                        ld = {"lean_cents": round(adj / 10_000, 4), "lean_reason": "leanpipe"}
                    else:
                        adj, ld = L.edge_adjust(theo, p_rv, side,
                                                clamp=settings.LEAN_DISAGREEMENT_CLAMP,
                                                defensive=settings.LEAN_DEFENSIVE_CENTS,
                                                aggressive=settings.LEAN_AGGRESSIVE_CENTS)
                    ft = self.last_fill_t.get((tk, side))
                    back = L.backoff_micros(None if ft is None else time.monotonic() - ft)
                    need = max(0, base_m + adj) + back
                    if need > max_m:
                        # The edge this side needs exceeds the width cap for this tau: do not POST. A resting order still stays while its edge is > 0.
                        rungs = []
                        rung_reason = "width_cap"
                    else:
                        rungs = L.decide_ladder(side, theo, bb, ba, [settings.QUOTE_COUNT],
                                                min_edge_micros=need,
                                                tiers_enabled=settings.TIERS_ENABLED)
                        rung_reason = rungs[0].reason if rungs else "none"
                    gated = (settings.LEAN_DEFENSIVE_JOIN_GATE and lean_attr is not None and adj > 0)
                    if gated:
                        # LEAN-DEFENSIVE side: joins must clear `need` too (LEAN_DEFENSIVE_JOIN_GATE).
                        rungs = defensive_gate(rungs, need)
                        if not rungs:
                            rung_reason = "defensive_gate"
                    if gated and not rungs:
                        pass        # side left out of `desired` -> any resting order is CANCELLED
                    else:
                        desired[side] = rungs
                    notes[side] = {"need_micros": need, "base_micros": base_m, "max_micros": max_m,
                                   "floor_c": edet.get("floor_c"), "bonus_c": edet.get("bonus_c"),
                                   "lean_cents": ld.get("lean_cents"),
                                   "lean": ld.get("lean_reason"), "backoff": back,
                                   "rung": rung_reason}
            t_decide = time.time()
            actions = self._reconcile(tk, theo, desired, refused=bool(reasons))
            self._track_stale(tk, theo, now)
            self.stats["ticks"] += 1
            # Journal a tick only when it carries information: an order action, a quoting
            # decision, or a change in why the market is refused (2.5 GB / 5 min otherwise).
            key = tuple(reasons)
            if not actions and reasons and self._last_reasons.get(tk) == key:
                continue
            self._last_reasons[tk] = key
            self.j.w("tick", ticker=tk, trigger=trigger,
                     perp_exch_ms=lp[0] if lp else None, perp_rx=lp[1] if lp else None,
                     t_wake=t_wake, t_theo=t_theo, t_decide=t_decide, t_done=time.time(),
                     F=None if F is None else round(F, 2), move=None if move is None else round(move, 2),
                     sigma_impl=sig_i, sigma_rv=sig_rv, theo=theo, p_rv=p_rv,
                     p_theo=p_theo, p_mid=p_mid, shadow_v=shadow, lean_attr=lean_attr,
                     R=None if not g else g["R"], bid=bb, ask=ba, tau=round(tau, 1),
                     reasons=reasons, notes=notes, actions=actions,
                     ev_book_exch_ms=top[4] if top else None, ev_book_rx=top[5] if top else None)

    def _shadow_v(self, mk, F, now, obs, p_rv_theo):
        """SHADOW ONLY (never acted on): an implied-vol lean, journaled for offline evaluation.

            V = P(S, sigma_hourly_iv) - P(S, sigma_rv)      (the VOL part of the cross-market gap)

        plus the per-side lean it WOULD apply (pricing.lean.edge_adjust with the LEAN_* settings).
        Journaled to forward-test against our fills and the live mid. No fresh hourly IV -> None,
        i.e. no-IV -> no-lean.
        """
        fresh = self.sigma_impl is not None and now - self.sigma_impl_t < settings.IV_MAX_AGE_S
        if not fresh or p_rv_theo is None or settings.THEO_VOL_SOURCE != "rv":
            return None
        p_iv, _ = digital.market_theo(mk, F, self.sigma_impl, now, obs)
        if p_iv is None:
            return None
        out = {"sigma_hourly_iv": round(self.sigma_impl, 4), "p_iv": round(p_iv, 5),
               "V": round(p_iv - p_rv_theo, 5)}
        for side in ("bid", "ask"):
            adj, _ = L.edge_adjust(p_rv_theo, p_iv, side, clamp=settings.LEAN_DISAGREEMENT_CLAMP,
                                   defensive=settings.LEAN_DEFENSIVE_CENTS,
                                   aggressive=settings.LEAN_AGGRESSIVE_CENTS)
            out[f"lean_{side}_c"] = round(adj / 10_000, 4)
        return out

    def _shard(self, tk):
        mk = self.markets.get(tk) or self.candidates.get(tk)
        if mk is None:
            raise RuntimeError(f"no exchange shard known for {tk}; refusing to guess")
        return mk["exchange_index"]

    PENDING_VERIFY_S = 3.0          # > the resting-list lag after an order event (~1-2 s)

    def _verify_pending(self, now):
        """Resolve 404'd cancels: gone from the resting list after the lag -> resolved (filled or
        cancelled). STILL resting -> the cancel really failed: cancel again, count it toward HALT."""
        due = [oid for oid, pv in self.pending_verify.items() if now - pv["t"] >= self.PENDING_VERIFY_S]
        if not due or not self.live:
            for oid in due:
                self.pending_verify.pop(oid, None)
            return
        try:
            live_ids = {o["order_id"]: o for o in self.writer.resting()}
        except Exception as e:                                        # noqa: BLE001
            self.j.w("error", where="verify_pending", err=f"{type(e).__name__}: {str(e)[:160]}")
            return
        for oid in due:
            pv = self.pending_verify.pop(oid)
            if oid not in live_ids:
                self.j.w("cancel_404_resolved", order_id=oid, by="not_resting", ticker=pv["ticker"])
                continue
            o = live_ids[oid]
            r = self.writer.cancel(oid, o.get("exchange_index", pv["order"].get("exchange_index")))
            self.j.w("cancel_404_still_resting", order_id=oid, ticker=pv["ticker"],
                     retry_status=r.get("status"))
            if not r["ok"]:
                self._on_cancel_fail(pv["ticker"], r)

    def _on_cancel_fail(self, tk, r):
        """A cancel that did not return 2xx: the order may still be live. Count it; HALT on a run."""
        self.cancel_fail_run = getattr(self, "cancel_fail_run", 0) + 1
        self.j.w("cancel_fail", ticker=tk, status=r.get("status"), resp=r.get("resp"))
        if self.cancel_fail_run >= settings.CANCEL_FAIL_HALT and not self.halted:
            self.halted = "cancel_failures"
            self.j.w("halt", why=self.halted, last_resp=r.get("resp"))
            self.daily.halt(f"cancel_failures: {self.cancel_fail_run} consecutive, last {r.get('resp')}")
            self.stop.set()
            self.wake.set()

    def _on_reject(self, tk, r):
        """A rejected create: back this market off after a run of them, HALT on a burst."""
        now = time.time()
        self.reject_run[tk] = self.reject_run.get(tk, 0) + 1
        self.reject_times.append(now)
        self.reject_times = [t for t in self.reject_times if now - t <= settings.REJECT_HALT_WINDOW_S]
        if self.reject_run[tk] >= settings.REJECT_BACKOFF_AFTER:
            self.reject_until[tk] = now + settings.REJECT_BACKOFF_S
            self.reject_run[tk] = 0
            self.j.w("reject_backoff", ticker=tk, until=self.reject_until[tk],
                     status=r.get("status"), resp=r.get("resp"))
        code = ((r.get("resp") or {}).get("error") or {}).get("code")
        systemic = code in settings.SYSTEMIC_REJECT_CODES or r.get("status") in settings.SYSTEMIC_REJECT_STATUS
        if (systemic or len(self.reject_times) >= settings.REJECT_HALT_COUNT) and not self.halted:
            self.halted = f"order_reject_systemic:{code}" if systemic else "order_reject_burst"
            self.j.w("halt", why=self.halted, last_resp=r.get("resp"))
            self.daily.halt(f"{self.halted}: {len(self.reject_times)} rejects in "
                            f"{settings.REJECT_HALT_WINDOW_S:.0f}s, last {r.get('resp')}")
            self.stop.set()
            self.wake.set()

    def _gap_blocked(self, tk, gap_c, now):
        """Hysteresis on |p_q - mid|: block above OFF; unblock only after < ON for REENABLE_S."""
        st = self.gap_state.setdefault(tk, {"blocked": False, "below_since": None, "toggles": 0})
        if not st["blocked"]:
            if gap_c > settings.MAX_QUOTE_MID_GAP_OFF_C:
                st.update(blocked=True, below_since=None, toggles=st["toggles"] + 1)
        else:
            if gap_c < settings.MAX_QUOTE_MID_GAP_ON_C:
                if st["below_since"] is None:
                    st["below_since"] = now
                elif now - st["below_since"] >= settings.QUOTE_MID_GAP_REENABLE_S:
                    st.update(blocked=False, below_since=None, toggles=st["toggles"] + 1)
            else:
                st["below_since"] = None
        return st["blocked"]

    def _reconcile(self, tk, theo, desired, refused):
        """plan_side per side, then send: cancels first (they reduce risk), then places.

        A REFUSED market (any reason in fast_once) pulls everything. Otherwise plan_side rules,
        exactly as SPX: a resting order stays while its edge is > 0, even when the current
        ladder would not post it; it is replaced only for a >= 1-tick improvement.
        """
        out = []
        for side in ("bid", "ask"):
            live = [dict(o) for o in self.resting[(tk, side)]]
            rungs = desired.get(side, [])
            if refused or theo is None or (side not in desired and live):
                cancels, places = live, []            # refused (or side position-blocked)
                for c in cancels:
                    c["reason"] = "refused"
            else:
                top = self.feed.top(tk)
                touch = None if not top else (top[0] if side == "bid" else top[1])
                z_at, z_touch = self.zombie.get((tk, side), (None, None))
                cancels, places, _keeps = L.plan_side(tk, side, theo, rungs, live,
                                                      [settings.QUOTE_COUNT], touch=touch,
                                                      zombie_at=z_at, zombie_touch=z_touch,
                                                      now=time.monotonic())
            for c in cancels:
                r = self.writer.cancel(c["order_id"], c.get("exchange_index", self._shard(tk)))
                self.stats["cancels"] += 1
                if r["ok"]:
                    with self.lock:
                        self.resting[(tk, side)] = [o for o in self.resting[(tk, side)]
                                                    if o["order_id"] != c["order_id"]]
                    self.qt.on_own_cancel(c["order_id"], c.get("count"))
                    if c.get("reason") == "edge_negative":
                        # Arm the SPX zombie hold (orders.MarketState.arm_zombie): no improve on
                        # this side for QUOTE_ZOMBIE_HOLD_S unless the touch moves in.
                        top = self.feed.top(tk)
                        self.zombie[(tk, side)] = (time.monotonic(), None if not top else
                                                   (top[0] if side == "bid" else top[1]))
                    st = self.stale_since.pop(c["order_id"], None)
                    if st:
                        self.j.w("stale", ticker=tk, side=side, order_id=c["order_id"],
                                 price=c["price"], stale_s=time.time() - st[0],
                                 edge_micros_at_detect=st[1])
                if r["ok"]:
                    self.cancel_fail_run = 0
                elif r.get("status") == 404:
                    # Usually a fill won the race (the fill notice arrives just after the 404). Take the order OUT of planning -- never re-cancel
                    # it blindly -- and verify it later (_verify_pending) against fills and the
                    # resting list, whose ~1-2 s lag is why this is not decided here.
                    with self.lock:
                        self.resting[(tk, side)] = [o for o in self.resting[(tk, side)]
                                                    if o["order_id"] != c["order_id"]]
                    self.pending_verify[c["order_id"]] = {"ticker": tk, "side": side,
                                                          "t": time.time(), "order": c}
                    self.j.w("cancel_404_pending", ticker=tk, side=side, order_id=c["order_id"],
                             price=c["price"])
                else:
                    self._on_cancel_fail(tk, r)
                out.append({"op": "cancel", "side": side, "price": c["price"],
                            "reason": c.get("reason"), "ok": r["ok"], **r["timing"],
                            "status": r.get("status")})
            if len(self.resting[(tk, side)]) + len(places) > 1:
                places = places[:max(0, 1 - len(self.resting[(tk, side)]))]
            for p in places:
                if sum(len(v) for v in self.resting.values()) >= settings.MAX_RESTING_ORDERS:
                    break
                r = self.writer.create(tk, side, p["price"], p["count"])
                self.stats["places"] += 1
                if r["ok"]:
                    with self.lock:
                        self.resting[(tk, side)].append(
                            {"ticker": tk, "side": side, "price": p["price"],
                             "count": p["count"], "order_id": r["order_id"],
                             "exchange_index": self._shard(tk)})
                    bs, bpm = QueueTracker.book_key(side, int(round(p["price"] * 1_000_000)))
                    self.qt.on_place(r["order_id"], tk, side, p["price"],
                                     self.feed.level_size(tk, bs, bpm))
                    self.reject_run[tk] = 0
                else:
                    self.stats["place_errors"] += 1
                    self._on_reject(tk, r)
                out.append({"op": "place", "side": side, "price": p["price"],
                            "reason": p.get("reason"), "ok": r["ok"], **r["timing"],
                            "status": r.get("status"), "resp": r.get("resp")})
        return out

    def _track_stale(self, tk, theo, now):
        if theo is None:
            return
        for side in ("bid", "ask"):
            for o in self.resting[(tk, side)]:
                e = L.edge_micros(side, theo, o["price"])
                if e <= 0 and o["order_id"] not in self.stale_since:
                    self.stale_since[o["order_id"]] = (now, e)
                    self.stats["stale_events"] += 1

    # ------------------------------------------------------------------ slow loop
    def slow_loop(self):
        next_iv = next_mk = 0.0
        while not self.stop.is_set() and time.time() < self.t_end:
            t0 = time.time()
            try:
                self.slow_once(t0, refresh_iv=t0 >= next_iv, refresh_markets=t0 >= next_mk)
                if t0 >= next_iv:
                    next_iv = t0 + settings.IV_REFRESH_S
                if t0 >= next_mk:
                    next_mk = t0 + 10.0
            except Exception as e:                                    # noqa: BLE001
                self.stats["slow_errors"] += 1
                self.j.w("error", where="slow", err=f"{type(e).__name__}: {str(e)[:200]}")
            self.stop.wait(max(0.0, settings.VOL_UPDATE_INTERVAL_S - (time.time() - t0)))
        self.stop.set()
        self.wake.set()

    def slow_once(self, now, refresh_iv, refresh_markets):
        st = self.spot.state(now)
        lp = self.last_perp
        rvs = self.rv_fc = self.rvf.forecast(now)
        if settings.RV_CLOCK in ("calendar", "calendar15") and rvs.get("ok"):
            sc = {}
            for tk, mk in list(self.candidates.items()):
                if tk in self.markets or mk["close_ts"] - now <= settings.QUOTE_MAX_TAU_S[mk["series"]]:
                    v = self.IF.variance_to_close(self.rvf.art, rvs, now, mk["close_ts"])
                    sc[tk] = v.get("sigma_eff_ann")
            self.sig_close = sc
        with self.lock:
            self.spot_state = st
            if st.s_hat is not None and lp is not None:
                self.anchor = (st.s_hat, lp[2], now)
        if refresh_iv:
            ms = self.events.open_markets("KXBTCD")
            pick = implied.pick_daily if settings.THEO_VOL_SOURCE == "implied_1d" else implied.pick_hourly
            ev, ladder = pick(ms, now)
            sig, d = implied.implied_sigma(ladder, st.s_hat, now) if ladder else (None, {"reason": "no_ladder"})
            d["event"] = ev
            with self.lock:
                self.iv_detail = d
                if sig is not None:
                    self.iv_reads = (self.iv_reads + [sig])[-settings.IV_SMOOTH_READS:]
                    self.sigma_impl = sorted(self.iv_reads)[len(self.iv_reads) // 2]
                    self.sigma_impl_t = now
                    d["sigma_raw"] = sig
        if refresh_markets:
            cands = {}
            for series in settings.QUOTE_SERIES:
                for m in self.events.open_markets(series):
                    ok, _ = priceable(m, now)
                    tau = m["close_ts"] - now
                    if ok and settings.QUOTE_CUTOFF_S < tau <= settings.QUOTE_MAX_TAU_S[series]:
                        cands[m["ticker"]] = m
            self.candidates = cands
        self._select_active(now)
        self._risk(now)
        self._markouts(now)
        self._verify_pending(now)
        self.j.w("cycle", s_hat=st.s_hat, ref=st.ref, basis=st.basis, dev_bps=st.dev_bps,
                 spot_reliable=st.reliable, spot_reasons=st.reasons,
                 sigma_impl=self.sigma_impl, iv_age_s=now - self.sigma_impl_t if self.sigma_impl_t else None,
                 iv_event=self.iv_detail.get("event"), iv_rungs=self.iv_detail.get("n_rungs"),
                 iv_reason=self.iv_detail.get("reason"), iv_raw=self.iv_detail.get("sigma_raw"),
                 rv=rvs, positions=dict(self.position), cash=self.cash,
                 resting={f"{k[0]}|{k[1]}": [o["price"] for o in v] for k, v in self.resting.items() if v},
                 halted=self.halted, perp_gaps=self.perp.gaps, ev_gaps=self.feed.gaps,
                 perp_reconnects=self.perp.reconnects, ev_reconnects=self.feed.reconnects,
                 gap_toggles={t: g["toggles"] for t, g in self.gap_state.items()},
                 stats=dict(self.stats))

    def _select_active(self, now):
        """Once per slow-loop pass: the markets the FAST path prices, streams and quotes.

        Only candidates whose theo is within ACTIVE_THEO_MARGIN of the quoting band, nearest the
        money first, at most MAX_ACTIVE_MARKETS. Pricing and streaming every hourly strike on
        every perp tick multiplies spot->order latency and starves the event WebSocket. A market with a resting order stays
        active until it is pulled.
        """
        F, _ = self._forward()
        scored = []
        for tk, mk in self.candidates.items():
            sig, _ = self._sigmas(mk["series"], mk)
            th = None
            if F is not None and sig is not None:
                th, _ = digital.market_theo(mk, F, sig, now)
            keep = th is not None and (settings.THEO_BAND_MIN - settings.ACTIVE_THEO_MARGIN <= th
                                       <= settings.THEO_BAND_MAX + settings.ACTIVE_THEO_MARGIN)
            if keep or self._has_resting(tk):
                scored.append((abs((th if th is not None else 0.5) - 0.5), tk))
        active, per = {}, {}
        for _, tk in sorted(scored):
            ser = self.candidates[tk]["series"]
            if len(active) >= settings.MAX_ACTIVE_MARKETS or \
                    per.get(ser, 0) >= settings.MAX_ACTIVE_PER_SERIES.get(ser, settings.MAX_ACTIVE_MARKETS):
                continue
            active[tk] = self.candidates[tk]
            per[ser] = per.get(ser, 0) + 1
        for tk in list(self.markets):
            if tk not in active and self._has_resting(tk):
                active[tk] = self.markets[tk]            # keep until its orders are gone
        with self.lock:
            gone = set(self.markets) - set(active)
            self.markets = active
        if set(active) != getattr(self, "_subscribed", set()):
            self.feed.set_markets(list(active))
            self._subscribed = set(active)
        for t in gone:
            self._pull_market(t, "deactivated")

    def _has_resting(self, t):
        return any(self.resting[(t, s)] for s in ("bid", "ask"))

    def _pull_market(self, t, why):
        for side in ("bid", "ask"):
            for o in list(self.resting[(t, side)]):
                r = self.writer.cancel(o["order_id"], o.get("exchange_index", self._shard(t)))
                if r.get("status") == 404:
                    with self.lock:
                        self.resting[(t, side)] = [x for x in self.resting[(t, side)]
                                                   if x["order_id"] != o["order_id"]]
                    self.pending_verify[o["order_id"]] = {"ticker": t, "side": side,
                                                          "t": time.time(), "order": o}
                elif not r["ok"]:
                    self._on_cancel_fail(t, r)
                if r["ok"]:
                    with self.lock:
                        self.resting[(t, side)] = [x for x in self.resting[(t, side)]
                                                   if x["order_id"] != o["order_id"]]
                self.j.w("pull", ticker=t, side=side, price=o["price"], why=why, ok=r["ok"])

    def _mark(self):
        pnl = self.cash
        for tk, pos in self.position.items():
            if abs(pos) < 1e-9:
                continue
            top = self.feed.top(tk)
            if top and top[0] is not None and top[1] is not None:
                pnl += pos * 0.5 * (top[0] + top[1])
            else:
                th = self._theo_now(tk)
                pnl += pos * (th if th is not None else 0.5)
        return pnl

    def _risk(self, now):
        """HALT file (operator kill switch) every cycle; ACCOUNT equity loss every ACCOUNT_CHECK_S.

        The loss limit is account-based and persistent (strategy/account_risk.DailyLoss): it
        survives restarts, sees settlements, and writes HALT on breach so no later start runs.
        """
        if self.halted:
            return
        why = None
        if os.path.exists(settings.HALT_FILE):
            why = "halt_file"
        elif now - self._acct_t >= settings.ACCOUNT_CHECK_S:
            self._acct_t = now
            try:
                ok, d = self.daily.check()
                self.acct_err_run = 0                  # consecutive, not cumulative (see below)
                self.j.w("account", **d, baseline_cents=self.daily.baseline_cents)
                if not ok:
                    why = "daily_loss_limit"
            except Exception as e:                                    # noqa: BLE001
                self.stats["account_errors"] += 1
                self.acct_err_run = getattr(self, "acct_err_run", 0) + 1
                self.j.w("error", where="account", err=f"{type(e).__name__}: {str(e)[:160]}")
                if self.acct_err_run >= 6:     # six CONSECUTIVE failed reads = a minute blind on the limit
                    why = "account_mark_unavailable"
        if why:
            self.halted = why
            self.j.w("halt", why=why, account=self.daily.last)
            for t in list(self.markets):
                self._pull_market(t, why)
            if self.live:
                self.writer.cancel_all()
            self.stop.set()
            self.wake.set()

    def _markouts(self, now):
        due = [m for m in self.pending_markouts if m[0] <= now]
        self.pending_markouts = [m for m in self.pending_markouts if m[0] > now]
        for _, h, tk, sign, px, oid in due:
            top = self.feed.top(tk)
            mid = None if not top or top[0] is None or top[1] is None else 0.5 * (top[0] + top[1])
            theo = self._theo_now(tk)
            self.j.w("markout", ticker=tk, order_id=oid, horizon_s=h, yes_dir=sign, price=px,
                     mid=mid, theo=theo,
                     markout_mid=None if mid is None else sign * (mid - px),
                     markout_theo=None if theo is None else sign * (theo - px))

    # ------------------------------------------------------------------ run
    def run(self):
        self.j.w("start", live=self.live, rv_model=self.rvf.art.get("model"),
                 rv_fitted=self.rvf.art.get("fitted_utc"), settings={k: getattr(settings, k) for k in dir(settings)
                                                    if k.isupper() and not k.endswith("PATH")},
                 )
        # Seed positions from the EXCHANGE: the in-session map is otherwise built only from fills,
        # so a restart would forget inventory carried over and the per-market limit would let it
        # double.
        try:
            body = RestClient().get("/portfolio/positions", {"limit": 1000, "count_filter": "position"},
                                    signed=True)
            for x in body.get("market_positions", []):
                if x.get("ticker", "").startswith(settings.QUOTE_SERIES):
                    self.position[x["ticker"]] = float(x.get("position_fp") or 0)
            self.j.w("positions_seeded", positions=dict(self.position))
        except Exception as e:                                           # noqa: BLE001
            raise SystemExit(f"refusing to start: cannot read exchange positions ({e})")
        ok, why = self.daily.preflight()
        self.j.w("preflight", ok=ok, detail=why, baseline_cents=self.daily.baseline_cents)
        if not ok:
            raise SystemExit(f"refusing to start: {why}")
        if self.live:
            pre = self.writer.resting()
            if pre:
                raise SystemExit(f"refusing to start: {len(pre)} resting {settings.QUOTE_SERIES} orders")
        self.perp.start()
        self.feed.start()
        self.rvf.start()
        if not self.perp.wait_ready(20):
            raise SystemExit("perp WS not ready")
        # warm the basis, then the implied vol, before any quote
        while self.spot.n_basis < settings.MIN_BASIS_SAMPLES and not self.stop.is_set():
            time.sleep(0.5)
        fast = threading.Thread(target=self.fast_loop, name="fast", daemon=True)
        fast.start()
        self.slow_loop()
        fast.join(timeout=5)
        self.shutdown("session_end")

    def shutdown(self, why):
        self.stop.set()
        n_ok = n_fail = 0
        for t in list(self.markets) + [k[0] for k, v in self.resting.items() if v]:
            self._pull_market(t, why)
        left = []
        if self.live:
            n_ok, n_fail = self.writer.cancel_all()
            # The resting list lags a fresh create by ~1-2 s: wait, then sweep
            # once more, then verify -- an order placed just before exit must not be missed.
            time.sleep(3.0)
            n2_ok, n2_fail = self.writer.cancel_all()
            n_ok, n_fail = n_ok + n2_ok, n_fail + n2_fail
            try:
                left = self.writer.resting()
            except Exception as e:                                    # noqa: BLE001
                left = [{"error": f"{type(e).__name__}: {e}"}]
            if left:
                print(f"[end] !!! {len(left)} {settings.QUOTE_SERIES} ORDERS STILL RESTING after "
                      f"cancel_all -- cancel by hand (exchange_index!)", flush=True)
        self.j.w("end", why=why, cancel_all_ok=n_ok, cancel_all_fail=n_fail, still_resting=len(left),
                 positions=dict(self.position), cash=self.cash, mark=self._mark(),
                 stats=dict(self.stats))
        self.perp.stop()
        self.feed.stop()
        self.rvf.stop()
        self.j.close()
        print(f"[end] {why}: stats {dict(self.stats)} positions {dict(self.position)} "
              f"mark {self._mark():.2f} -> {self.j.path}")


LOCK_PATH = os.path.join(os.path.dirname(settings.LOG_DIR), "quoter.lock")
PID_PATH = os.path.join(os.path.dirname(settings.LOG_DIR), "quoter.pid")


def single_instance():
    """Exclusive, process-lifetime lock: a second quoter refuses to start.

    If a launcher records the pid of a wrapper, a restart's SIGTERM can miss the real process and
    a new quoter starts beside it. The
    quoter now owns its lock and writes its OWN pid (os.getpid()) for anything that must signal it.
    """
    import fcntl
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"refusing to start: another quoter holds {LOCK_PATH} "
                         f"(pid {open(PID_PATH).read().strip() if os.path.exists(PID_PATH) else '?'})")
    with open(PID_PATH, "w") as pf:
        pf.write(str(os.getpid()))
    return fh                                    # keep the handle open for the process lifetime


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args(argv)
    _lock = single_instance()                    # noqa: F841  held until exit
    q = Quoter(live=a.live, minutes=a.minutes)

    def _sig(*_):
        q.stop.set()
        q.wake.set()
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    try:
        q.run()
    except SystemExit as e:
        print(f"[abort] {e}", file=sys.stderr)
        if not q.stop.is_set():
            q.shutdown(f"abort: {e}")
        return 1
    except Exception as e:                                                  # noqa: BLE001
        print(f"[crash] {type(e).__name__}: {e}", file=sys.stderr)
        q.shutdown(f"crash: {type(e).__name__}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
