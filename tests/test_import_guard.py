"""kbtc never loads the SPX/NDX session machinery; only kbtc.strategy may load the equity config.

Pricing, venue and vol modules must not import the Kalshi repo's `config` at all. The quoter reuses the SPX
quoting rules through kbtc.strategy.spx_logic, which does load config for generic Kalshi values
(MIN/MAX_PRICE, QUOTE_OFFSET, QUOTE_TIGHTEN_FRACTION) -- but no calendar, vol model or board.
"""
import subprocess
import sys

from kbtc.kalshi_root import harness as H

BASE = ("kbtc.pricing.digital", "kbtc.pricing.spot", "kbtc.pricing.implied", "kbtc.pricing.greeks",
        "kbtc.rvol.ewma", "kbtc.venue.events",
        "kbtc.venue.perp", "kbtc.venue.ratebudget", "kbtc.venue.rest", "kbtc.venue.eventfeed",
        "kbtc.venue.orders", "kbtc.tools.theo_vs_book", "kbtc.tools.record_perp_top")
FORBIDDEN = ("pricing.varcal", "pricing.gapcal", "pricing.eventcal", "pricing.blendcal",
             "pricing.hourly", "pricing.atmvol", "pricing.greeks", "rvol", "rvol.model",
             "strategy.run_04_quote_loop", "strategy.edgemgr", "strategy.leanchain", "edgecal")


def loaded_after(mods):
    code = ("import sys\n" + "".join(f"import {m}\n" for m in mods) +
            "print('\\n'.join(sorted(sys.modules)))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return set(out.stdout.split())


base = loaded_after(BASE)
H.assert_true("pricing/venue/rvol/tools: equity config NOT loaded", "config" not in base)
H.assert_true("pricing/venue/rvol/tools: kalshi.client NOT loaded", "kalshi.client" not in base)
quoter = loaded_after(["kbtc.strategy.quoter"])
bad = sorted(m for m in FORBIDDEN if m in quoter)
H.assert_true("quoter: no SPX calendar / vol model / board modules", not bad, f"{bad}")
H.finish()
