"""Per-market position limit: stable at the boundary (no place/cancel flip), blocks real breaches."""
from kbtc import settings
from kbtc.kalshi_root import harness as H
from kbtc.strategy.quoter import side_within_limit as ok

LIM = settings.POSITION_LIMIT_CONTRACTS
LOT = float(settings.QUOTE_COUNT)
assert LIM > LOT + 3

# The flip case: short a little under the limit, one lot ask resting. Keeping it must be allowed
# (|pos| + LOT <= LIM)...
p = LIM - LOT - 3
H.check(f"resting ask at -{p:g} is kept", ok(-p, "ask", LOT, LOT, LIM), True)
# ...and with nothing resting, posting it must be allowed too -- same answer, so no flip.
H.check(f"posting that ask at -{p:g} is allowed", ok(-p, "ask", 0, LOT, LIM), True)
q = LIM - LOT + 1
H.check(f"at -{q:g} a new {LOT:g}-lot ask would breach {LIM}", ok(-q, "ask", 0, LOT, LIM), False)
H.check(f"short {LIM + 2} (over the limit): ask blocked", ok(-(LIM + 2), "ask", 0, LOT, LIM), False)
H.check(f"short {LIM + 2}: bid (reducing) allowed", ok(-(LIM + 2), "bid", 0, LOT, LIM), True)
H.check(f"long {LIM - LOT:g} + {LOT:g} resting bid = {LIM}: kept", ok(LIM - LOT, "bid", LOT, LOT, LIM), True)
H.check(f"long {LIM - LOT:g} + {2 * LOT:g} resting: blocked", ok(LIM - LOT, "bid", 2 * LOT, LOT, LIM), False)
H.finish()
