"""Daily loss limit: baseline persists across restarts; breach writes HALT; preflight refuses."""
import os
import tempfile

from kbtc.kalshi_root import harness as H
from kbtc.strategy.account_risk import DailyLoss


class FakeRest:
    def __init__(self):
        self.balance, self.pv = 295531, 0

    def get(self, path, params=None, category=None, signed=False):
        assert path == "/portfolio/balance" and signed
        return {"balance": self.balance, "portfolio_value": self.pv}


d = tempfile.mkdtemp()
halt = os.path.join(d, "HALT")
r = FakeRest()
a = DailyLoss(r, limit_usd=1000, state_dir=d, halt_file=halt)
H.check("baseline = balance + portfolio value", a.baseline_cents, 295531)

print("restart keeps the day's baseline even after equity moved")
r.balance, r.pv = 280000, 5000
b = DailyLoss(r, limit_usd=1000, state_dir=d, halt_file=halt)
H.check("baseline not reset by a restart", b.baseline_cents, 295531)
ok, det = b.check()
H.check("loss counted from the day baseline", det["loss_cents"], 295531 - 285000)
H.check("under the limit -> ok", ok, True)

print("settlement is seen: position value converts to cash, equity unchanged")
r.balance, r.pv = 285000, 0
H.check("no phantom loss on settlement", b.check()[1]["loss_cents"], 295531 - 285000)

print("breach")
r.balance, r.pv = 295531 - 99_999, 0
H.check("$999.99 is not a breach", b.check()[0], True)
r.balance = 295531 - 100_000
ok, _ = b.check()
H.check("$1000.00 breaches", ok, False)
H.assert_true("HALT written with reason", os.path.exists(halt) and "daily_loss" in open(halt).read())
c = DailyLoss(r, limit_usd=1000, state_dir=d, halt_file=halt)
ok, why = c.preflight()
H.assert_true("a new start refuses while HALT exists", not ok and "HALT present" in why, why)
H.finish()
