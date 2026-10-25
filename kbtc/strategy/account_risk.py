"""Daily loss limit on ACCOUNT equity, persistent across restarts and settlements.

Why not the quoter's own mark: a process-local mark (cash from this run's fills + positions at
mid) resets on restart and cannot see settlements -- a settled 15m market's book vanishes and a
position would be marked at 0.5 forever. Inherited positions plus settlements can make such a
mark report a profit on a losing day.

So the mark is what the EXCHANGE says the account is worth:

    equity = balance + portfolio_value            (GET /portfolio/balance, both in cents)

and the day's loss is baseline - equity, where the baseline is recorded the FIRST time any
quoter starts on that ET date and persisted to var/state/pnl-<ET date>.json. A restart re-reads
it; it is never overwritten. On breach the HALT file is written with the reason, so every later
start refuses until a human removes it.

Deposits/withdrawals during the day would move equity without trading; the operator is expected
not to transfer mid-session. A transfer is visible in the journal as a step in `balance` with no
fills, and the baseline file can be edited by hand if one happens.
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from kbtc import settings

ET = ZoneInfo("America/New_York")
STATE_DIR = os.path.join(os.path.dirname(settings.LOG_DIR), "state")


def et_date(now=None):
    return datetime.fromtimestamp(now or time.time(), ET).date().isoformat()


def equity_cents(rest):
    b = rest.get("/portfolio/balance", signed=True)
    return int(b["balance"]) + int(b.get("portfolio_value") or 0), b


class DailyLoss:
    def __init__(self, rest, limit_usd=None, state_dir=STATE_DIR, halt_file=None, now=None):
        self.rest = rest
        self.limit_c = int(round(100 * (settings.DAILY_LOSS_LIMIT_USD if limit_usd is None else limit_usd)))
        self.halt_file = halt_file or settings.HALT_FILE
        self.date = et_date(now)
        self.path = os.path.join(state_dir, f"pnl-{self.date}.json")
        os.makedirs(state_dir, exist_ok=True)
        eq, raw = equity_cents(rest)
        if os.path.exists(self.path):
            with open(self.path) as fh:
                self.state = json.load(fh)
        else:
            self.state = {"date": self.date, "baseline_cents": eq, "created": time.time(),
                          "balance_at_baseline": raw.get("balance"),
                          "portfolio_value_at_baseline": raw.get("portfolio_value")}
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.path)
        self.last = {"equity_cents": eq, "t": time.time()}

    @property
    def baseline_cents(self):
        return int(self.state["baseline_cents"])

    def check(self):
        """-> (ok, detail). Writes HALT and returns ok=False on breach."""
        eq, raw = equity_cents(self.rest)
        loss = self.baseline_cents - eq
        self.last = {"equity_cents": eq, "loss_cents": loss, "t": time.time(),
                     "balance": raw.get("balance"), "portfolio_value": raw.get("portfolio_value")}
        if loss >= self.limit_c:
            self.halt(f"daily_loss {loss / 100:.2f} >= {self.limit_c / 100:.2f} USD "
                      f"(baseline {self.baseline_cents / 100:.2f}, equity {eq / 100:.2f})")
            return False, self.last
        return True, self.last

    def halt(self, why):
        os.makedirs(os.path.dirname(self.halt_file), exist_ok=True)
        with open(self.halt_file, "w") as fh:
            fh.write(f"{datetime.now(ET).isoformat()} {why}\n")

    def preflight(self):
        """Refuse to start if HALT exists or today's loss is already at the limit."""
        if os.path.exists(self.halt_file):
            with open(self.halt_file) as fh:
                return False, f"HALT present: {fh.read().strip()}"
        ok, d = self.check()
        return ok, d
