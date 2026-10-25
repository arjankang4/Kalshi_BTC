"""Bridge to the config-free modules of the companion `Kalshi` checkout: `kalshi.auth`,
`pricing.bs`, `pricing.brackets`, `harness`.

Anything else there imports the equity `config` (secrets + SPX/NDX session). Import these names
from here, never `sys.path` hacks elsewhere, so there is one place that knows where it lives.
Set KBTC_KALSHI_ROOT to point elsewhere; the default is a sibling checkout named `Kalshi`.
"""
import sys

from kbtc.settings import KALSHI_ROOT     # KBTC_KALSHI_ROOT, else a sibling `Kalshi` checkout
if KALSHI_ROOT not in sys.path:
    sys.path.append(KALSHI_ROOT)      # appended, so kbtc's own names always win

from kalshi import auth               # noqa: E402,F401
from pricing import bs, brackets      # noqa: E402,F401
import harness                        # noqa: E402,F401
