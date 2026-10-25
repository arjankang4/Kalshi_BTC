"""Baseline BTC vol: EWMA of 1-minute KXBTCPERP mid log returns, annualised on a 24/7 clock.

A placeholder for the RV model (HAR + time-of-week profile). It exists so that tomorrow's theo
vs book comparison has a sigma that is (a) real-time, (b) from the same venue, and (c) simple
enough that a theo miss can be attributed. Its known blind spot is the intraday/weekly shape:
a flat sigma over-prices quiet hours and under-prices US-open hours.

Candle mids use bid/ask closes, not last trade, so bid-ask bounce does not enter the returns.
"""
import math

from kbtc import settings


def ewma_sigma(mids, half_life_min=settings.VOL_EWMA_HALF_LIFE_MIN):
    """mids: [(end_ts, mid)] sorted, 1-minute spacing (gaps allowed). -> annualised sigma or None.

    A gap of g minutes contributes one return scaled to per-minute variance (r^2 / g), so a
    missing candle neither vanishes nor counts as g quiet minutes.
    """
    if len(mids) < 30:
        return None
    lam = math.exp(-math.log(2) / half_life_min)
    v, wsum = 0.0, 0.0
    for (t0, m0), (t1, m1) in zip(mids, mids[1:]):
        g = max(1.0, (t1 - t0) / 60.0)
        if m0 <= 0 or m1 <= 0:
            continue
        r2 = math.log(m1 / m0) ** 2 / g
        v = lam * v + (1 - lam) * r2
        wsum = lam * wsum + (1 - lam)
    if wsum <= 0:
        return None
    per_min = v / wsum
    return math.sqrt(per_min * 525600.0)
