"""Edge curve: SPX shape on BTC anchors; tau buckets; refusals."""
from kbtc import settings
from kbtc.kalshi_root import harness as H
from kbtc.pricing import edgecurve as E

a = settings.R_ANCHORS["KXBTC15M"]
H.approx("bonus 0 at R_MIN", E.bonus_cents(a["R_MIN"], "KXBTC15M"), 0.0)
H.approx("bonus = BONUS_KNEE_C at R_P99", E.bonus_cents(a["R_P99"], "KXBTC15M"), E.BONUS_KNEE_C, 1e-12)
H.approx("bonus = BONUS_CAP_C at R_MAX", E.bonus_cents(a["R_MAX"], "KXBTC15M"), E.BONUS_CAP_C, 1e-12)
H.check("refuse past R_MAX", E.bonus_cents(a["R_MAX"] * 1.01, "KXBTC15M"), None)
MN, MX = settings.MIN_HALF_WIDTH_C, settings.MAX_HALF_WIDTH_C
B15, B1H = settings.WIDTH_TAU_BUCKETS["KXBTC15M"], settings.WIDTH_TAU_BUCKETS["KXBTCD"]
assert B15[0][0] <= 700 <= B15[0][1] and B15[2][0] <= 200 <= B15[2][1]
assert B15[3][0] <= 90 <= B15[3][1] and 50 < B15[-1][0] and B1H[1][0] <= 1500 <= B1H[1][1]
H.check(f"15m, 700 s left -> first bucket ({MN['KXBTC15M'][0]}c, {MX['KXBTC15M'][0]}c)",
        E.width_bucket(700, "KXBTC15M"), (MN["KXBTC15M"][0], MX["KXBTC15M"][0]))
H.check(f"15m, 200 s left -> third bucket ({MN['KXBTC15M'][2]}c, {MX['KXBTC15M'][2]}c)",
        E.width_bucket(200, "KXBTC15M"), (MN["KXBTC15M"][2], MX["KXBTC15M"][2]))
H.check(f"15m, 90 s left -> last bucket ({MN['KXBTC15M'][3]}c, {MX['KXBTC15M'][3]}c)",
        E.width_bucket(90, "KXBTC15M"), (MN["KXBTC15M"][3], MX["KXBTC15M"][3]))
H.check("15m, 50 s left -> below the last bucket, outside every bucket", E.width_bucket(50, "KXBTC15M"), None)
H.check(f"1h, 1500 s -> second bucket ({MN['KXBTCD'][1]}c, {MX['KXBTCD'][1]}c)",
        E.width_bucket(1500, "KXBTCD"), (MN["KXBTCD"][1], MX["KXBTCD"][1]))
b, m, d = E.post_requirement(a["R_MIN"], 700, "KXBTC15M")
floor_c = max(settings.MIN_EDGE * 100.0, MN["KXBTC15M"][0])
H.check(f"calm 15m market: base = {floor_c:g}c floor + 0 bonus, max {MX['KXBTC15M'][0]}c", (b, m),
        (int(round(floor_c * E.MICROS_PER_CENT)), MX["KXBTC15M"][0] * E.MICROS_PER_CENT))
b, m, d = E.post_requirement(0.5, 700, "KXBTC15M")
H.check("R beyond R_MAX refuses the market", (b, d["reason"]), (None, "R_above_R_MAX"))
H.assert_true("floor never below MIN_EDGE", E.post_requirement(0.0, 2000, "KXBTCD")[0]
              >= settings.MIN_EDGE * 1e6)
H.finish()
