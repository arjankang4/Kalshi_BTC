"""Queue-position estimate for our resting orders (standard L2 approximation).

Purpose: read "we were alone / last to pull" directly off a fill.

  at our place ack      ahead = displayed size at our price on our book side (we join behind it)
  public print at px    ahead -= print size          (prints consume the queue FROM THE FRONT)
  level drop, no print  ahead -= drop * ahead / level_before   (cancels: equally likely anywhere)

Book-side mapping on Kalshi event markets: our YES bid at p rests on the YES ladder at p; our YES
offer at p rests as a NO bid at 1 - p. A print with taker_side "yes" (taker BUYS yes) consumes
resting YES offers at yes_price; taker_side "no" consumes YES bids.

Separating a print's own level decrease from a cancel: each print adds its size to a per-level
"print budget", and later level decreases are absorbed by that budget first; only the excess is
treated as cancels. Our OWN cancels also feed the budget, so they are not mistaken for others'.

Known bias: `ahead` at ack may or may not already include our own order, depending on whether
the book delta for our add arrived before the ack (both orders are seen). It is journaled as-is
with `level_at_place`, so a reader can subtract our count when judging "alone".
"""
from collections import defaultdict

MICROS = 1_000_000


class QueueTracker:
    def __init__(self):
        self.orders = {}                          # oid -> dict
        self.budget = defaultdict(float)          # (ticker, side, price_m) -> unmatched print size

    @staticmethod
    def book_key(side, price_m):
        """Our (side, yes price) -> the book ladder and price it rests at."""
        return ("yes", price_m) if side == "bid" else ("no", MICROS - price_m)

    def on_place(self, oid, ticker, side, price, level_size):
        pm = int(round(price * MICROS))
        self.orders[oid] = {"ticker": ticker, "side": side, "price_m": pm,
                            "ahead": float(level_size or 0.0),
                            "level_at_place": float(level_size or 0.0),
                            "prints": 0.0, "cancels_ahead": 0.0}

    def on_trade(self, ticker, taker_side, yes_price, count):
        side_hit = "ask" if taker_side == "yes" else "bid"
        pm = int(round(float(yes_price) * MICROS))
        n = float(count)
        self.budget[(ticker, side_hit, pm)] += n
        for o in self.orders.values():
            if o["ticker"] == ticker and o["side"] == side_hit and o["price_m"] == pm:
                eat = min(o["ahead"], n)
                o["ahead"] -= eat
                o["prints"] += n

    def on_level(self, ticker, book_side, book_price_m, delta, level_before):
        """A book delta on the YES or NO ladder (book_side 'yes'/'no'), sizes in contracts."""
        if delta >= 0:
            return
        side = "bid" if book_side == "yes" else "ask"
        pm = book_price_m if book_side == "yes" else MICROS - book_price_m
        key = (ticker, side, pm)
        dec = -float(delta)
        absorbed = min(dec, self.budget[key])
        self.budget[key] -= absorbed
        cancel = dec - absorbed
        if cancel <= 0 or level_before <= 0:
            return
        for o in self.orders.values():
            if o["ticker"] == ticker and o["side"] == side and o["price_m"] == pm and o["ahead"] > 0:
                cut = cancel * o["ahead"] / level_before
                o["ahead"] = max(0.0, o["ahead"] - cut)
                o["cancels_ahead"] += cut

    def on_own_cancel(self, oid, count):
        o = self.orders.pop(oid, None)
        if o is not None:
            self.budget[(o["ticker"], o["side"], o["price_m"])] += float(count or 0)

    def snapshot(self, oid):
        o = self.orders.get(oid)
        if o is None:
            return None
        return {"ahead_est": round(o["ahead"], 2), "level_at_place": o["level_at_place"],
                "prints_at_px": o["prints"], "cancels_ahead_est": round(o["cancels_ahead"], 2)}

    def done(self, oid):
        self.orders.pop(oid, None)
