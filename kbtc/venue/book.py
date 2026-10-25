"""Price-level order book for one market, keyed by integer ticks.

Kalshi margin books arrive as `[price_str, count_str]` levels. Level ORDER is not relied on
(observed: bids ascending, asks descending, best last on both), so best bid/ask are max/min of
the keys. Best levels are cached and recomputed only when a delta empties the cached level.
"""
from decimal import Decimal


def to_ticks(price_str, tick=Decimal("0.0001")):
    """'8.3461' -> 83461. Exact: goes through Decimal, never float."""
    q = Decimal(price_str) / tick
    if q != q.to_integral_value():
        raise ValueError(f"price {price_str} is not on the {tick} tick")
    return int(q)


class L2Book:
    def __init__(self):
        self.bids, self.asks = {}, {}
        self._bb = self._ba = None
        self.valid = False
        self.ts_ms = None          # exchange timestamp of the last applied delta

    def apply_snapshot(self, bid_levels, ask_levels):
        self.bids = {to_ticks(p): float(q) for p, q in bid_levels if float(q) > 0}
        self.asks = {to_ticks(p): float(q) for p, q in ask_levels if float(q) > 0}
        self._bb = max(self.bids) if self.bids else None
        self._ba = min(self.asks) if self.asks else None
        self.valid = True

    def apply_delta(self, side, price_str, delta_str, ts_ms=None):
        levels = self.bids if side == "bid" else self.asks
        p = to_ticks(price_str)
        q = levels.get(p, 0.0) + float(delta_str)
        if q > 1e-9:
            levels[p] = q
            if side == "bid":
                if self._bb is None or p > self._bb:
                    self._bb = p
            elif self._ba is None or p < self._ba:
                self._ba = p
        else:
            if q < -1e-9:
                self.valid = False     # negative size: we missed a frame
            levels.pop(p, None)
            if side == "bid" and p == self._bb:
                self._bb = max(self.bids) if self.bids else None
            elif side == "ask" and p == self._ba:
                self._ba = min(self.asks) if self.asks else None
        if ts_ms is not None:
            self.ts_ms = ts_ms

    def top(self):
        """(bid_ticks, bid_size, ask_ticks, ask_size) or None if either side is empty."""
        if self._bb is None or self._ba is None:
            return None
        return self._bb, self.bids[self._bb], self._ba, self.asks[self._ba]

    def crossed(self):
        t = self.top()
        return t is not None and t[0] >= t[2]
