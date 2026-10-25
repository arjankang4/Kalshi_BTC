"""The SPX/NDX quoting rules, imported -- not copied -- from the companion Kalshi repo.

    quoting.decide_ladder   improve / penny / join, never cross, one-sided books as ONE_SIDED_WIDTH-wide
    orders.plan_side        keep / cancel / replace against what is resting (with a requote deadband)
    lean.edge_adjust        realized-vol lean on the REQUIRED EDGE (defensive side up, aggressive side down)
    lean.backoff_micros     decaying entry tax on a side we were just filled on

These modules import the equity `config`, but only for generic Kalshi values (MIN_PRICE,
MAX_PRICE, QUOTE_OFFSET, QUOTE_TIGHTEN_FRACTION); none of the SPX calendars, vol
models or boards load (tests/test_import_guard pins that list). This is the one kbtc module
allowed to pull them in, so every other kbtc module stays config-free.
"""
from kbtc import kalshi_root  # noqa: F401  (puts the Kalshi repo on sys.path)

from pricing import lean                      # noqa: E402
from strategy import orders, quoting          # noqa: E402

decide_ladder = quoting.decide_ladder
TICK = quoting.TICK
MICROS = quoting.MICROS
edge_micros = quoting._edge_micros
plan_side = orders.plan_side
edge_adjust = lean.edge_adjust
backoff_micros = lean.backoff_micros
