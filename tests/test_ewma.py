from kbtc.kalshi_root import harness as H
import math
import random
from kbtc.rvol.ewma import ewma_sigma

rng = random.Random(3)
sig = 0.5
m, mids = 80000.0, []
for i in range(2000):
    mids.append((60 * i, m))
    m *= math.exp(rng.gauss(0, sig / math.sqrt(525600)))
H.ok("recovers simulated sigma", ewma_sigma(mids, half_life_min=400), sig, 0.05)
gappy = [x for i, x in enumerate(mids) if i % 5]          # drop every 5th candle
H.ok("gaps do not bias it", ewma_sigma(gappy, half_life_min=400), sig, 0.06)
H.check("too short -> None", ewma_sigma(mids[:10]), None)
H.finish()
