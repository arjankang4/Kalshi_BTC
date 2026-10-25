"""Lean-defensive join gate: joins at edge below `need` are dropped; the favoured side is unaffected."""
from kbtc import settings
from kbtc.kalshi_root import harness as H
from kbtc.strategy import spx_logic as L
from kbtc.strategy.quoter import defensive_gate

# theo 0.50 on a 0.49/0.51 book: joining the bid at 0.49 earns 1.0c.
r = L.decide_ladder("bid", 0.50, 0.49, 0.51, [settings.QUOTE_COUNT], min_edge_micros=20_000, tiers_enabled=False)
H.check("SPX rule joins at 1c even though need is 2c", (r[0].reason, r[0].price), ("join_touch", 0.49))
H.check("gate drops that join when need = 2c", defensive_gate(r, 20_000), [])
H.check("gate keeps it when need = 1c (edge == need)", len(defensive_gate(r, 10_000)), 1)
H.check("gate keeps it when need = 0.5c", len(defensive_gate(r, 5_000)), 1)
H.check("empty ladder stays empty", defensive_gate([], 1), [])
H.finish()
