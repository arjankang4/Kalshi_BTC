"""Fast spot: basis timing, staleness guards."""
from kbtc import settings
from kbtc.kalshi_root import harness as H
from kbtc.pricing.spot import SpotEstimator, microprice

H.approx("cube-root: bid 1 / ask 8 -> weights 1:2, heavy offer pulls toward the bid", microprice(100, 1, 103, 8), 101.0)
H.approx("microprice equal sizes = mid", microprice(100, 2, 102, 2), 101.0)
from pricing import theo                                      # noqa: E402  (Kalshi repo on sys.path via kbtc.kalshi_root; loads equity config; test only)
for args in ((83450, 12, 83455, 900), (83450, 700, 83455, 3), (100, 0, 102, 5)):
    b, bs_, a, as_ = args
    H.approx(f"matches the Kalshi repo's theo.micro {args}", microprice(b, bs_, a, as_), theo.micro(b, a, bs_, as_), 1e-9)

print("basis uses the book AT the print's timestamp, not at receipt")
s = SpotEstimator(half_life_s=60, use_micro=False)
# book: 100 until t=1000 ms, jumps to 110 at t=1600 ms
s.on_book(99, 1, 101, 1, 1000, 1.0)
s.on_book(109, 1, 111, 1, 1600, 1.6)
s.on_reference(98.0, 1000)               # print stamped 1000 ms, received later
H.approx("first basis = mid(1000) - ref", s.basis, 2.0)

print("stale reference print does not update")
H.check("same ts rejected", s.on_reference(50.0, 1000), False)
H.approx("basis unchanged", s.basis, 2.0)

print("EWMA over new prints")
s.on_book(109, 1, 111, 1, 2000, 2.0)
s.on_reference(106.0, 2000)              # x = 110 - 106 = 4
a = 1 - 2 ** (-1.0 / 60)
H.approx("one EWMA step", s.basis, 2.0 + a * (4.0 - 2.0), 1e-12)

print("guards")
s2 = SpotEstimator(use_micro=False)
for i in range(settings.MIN_BASIS_SAMPLES + 1):
    t = 10_000 + 1000 * i
    s2.on_book(99, 1, 101, 1, t, t / 1000)
    s2.on_reference(99.5, t)
st = s2.state(now=(10_000 + 1000 * settings.MIN_BASIS_SAMPLES) / 1000 + 0.1)
H.check("fresh -> reliable", (st.reliable, st.reasons), (True, []))
H.approx("S_hat = mid - basis", st.s_hat, 99.5, 1e-9)
st = s2.state(now=st.ref_ts_ms / 1000 + settings.MAX_REF_AGE_S + 1)
H.assert_true("old book and ref -> unreliable", not st.reliable and "stale_ref" in st.reasons
              and "stale_book" in st.reasons, f"{st.reasons}")
s3 = SpotEstimator(use_micro=False)
s3.on_book(99, 1, 101, 1, 1000, 1.0)
s3.on_reference(99.0, 1000)
H.assert_true("warmup flagged", "basis_warmup" in s3.state(1.1).reasons)
s4 = SpotEstimator(use_micro=False)
s4.on_reference(99.0, 1000)
H.assert_true("print before any book -> no basis", s4.basis is None)
H.finish()
