"""Queue estimate: prints eat from the front, cancels pro-rata, own cancels not double counted."""
from kbtc.kalshi_root import harness as H
from kbtc.strategy.queue_est import QueueTracker, MICROS

q = QueueTracker()
q.on_place("A", "M", "bid", 0.45, 20)                       # 20 ahead of our YES bid at 0.45
q.on_trade("M", "no", 0.45, 5)                              # taker sells yes: hits bids at 0.45
q.on_level("M", "yes", 450_000, -5, 25)                     # the print's own decrease: absorbed
H.approx("print eats 5 from the front", q.snapshot("A")["ahead_est"], 15.0)
q.on_level("M", "yes", 450_000, -4, 20)                     # a cancel (no print): pro-rata
H.approx("cancel pro-rata: 4 * 15/20 = 3 removed", q.snapshot("A")["ahead_est"], 12.0)
q.on_trade("M", "yes", 0.45, 50)                            # taker BUYS yes: hits offers, not bids
H.approx("opposite-side print leaves our queue alone", q.snapshot("A")["ahead_est"], 12.0)

q.on_place("B", "M", "ask", 0.55, 8)                        # our YES offer at .55 rests as NO bid .45
q.on_level("M", "no", MICROS - 550_000, -2, 10)             # NO-ladder cancel at 0.45 -> our ask
H.approx("ask side maps to the NO ladder at 1-p", q.snapshot("B")["ahead_est"], 8 - 2 * 8 / 10)
q.on_own_cancel("B", 5)
q.on_level("M", "no", MICROS - 550_000, -5, 8)              # our own removal: absorbed by budget
H.check("own cancel does not shrink others (B gone)", q.snapshot("B"), None)
H.finish()
