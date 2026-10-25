"""Kalshi BTC event markets (KXBTC15M / KXBTCD / KXBTC): discovery and top-of-book over REST.

Verified against live `GET /markets?series_ticker=&status=open` bodies and the
rules text:
  KXBTC15M  one `greater_or_equal` market per quarter-hour. Its strike is the 60-s BRTI
            average before the OPEN, so a listed-but-not-open market has no usable strike.
            price_level_structure `tapered_deci_cent`: sub-cent prices near the extremes.
  KXBTCD    `greater` ladders (hourly, 17:00 ET daily, Friday weekly).
  KXBTC     `greater` / `less` / `between` ladders on the same expiries.
All settle on the simple average of the 60 BRTI prints before close_time.
"""
from datetime import datetime

from kbtc.kalshi_root import brackets


def epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _f(x):
    return None if x in (None, "") else float(x)


def parse_market(m):
    return {"ticker": m["ticker"], "event_ticker": m["event_ticker"],
            "series": m["ticker"].split("-", 1)[0],
            "strike_type": m.get("strike_type"),
            "floor_strike": _f(m.get("floor_strike")), "cap_strike": _f(m.get("cap_strike")),
            "open_ts": epoch(m["open_time"]), "close_ts": epoch(m["close_time"]),
            "status": m.get("status"),
            "yes_bid": _f(m.get("yes_bid_dollars")), "yes_ask": _f(m.get("yes_ask_dollars")),
            "yes_bid_size": _f(m.get("yes_bid_size_fp")),
            "yes_ask_size": _f(m.get("yes_ask_size_fp")),
            "last": _f(m.get("last_price_dollars")),
            "volume_24h": _f(m.get("volume_24h_fp")),
            "price_level_structure": m.get("price_level_structure"),
            # Exchange SHARD the market lives on (BTC markets are not on shard 0); every order
            # write for them must name it, or Kalshi answers 404 as if the order did not exist.
            "exchange_index": int(m.get("exchange_index") or 0)}


def priceable(mk, now):
    """A market we can put a theo on right now, and why not if not."""
    if mk["status"] not in (None, "active", "open"):
        return False, "status"
    if now < mk["open_ts"]:
        return False, "not_open"
    if not brackets.is_priceable(mk["strike_type"]):
        return False, "strike_type"
    if mk["floor_strike"] is None and mk["cap_strike"] is None:
        return False, "no_strike"
    return True, None


class EventRest:
    def __init__(self, client):
        self.c = client

    def open_markets(self, series):
        """All open markets of a series (paged). Charged to the series' budget category."""
        out, cursor = [], None
        while True:
            params = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            body = self.c.get("/markets", params, category=series)
            rx = body.get("_rx", {}).get("t_recv")
            for m in body.get("markets", []):
                mk = parse_market(m)
                mk["rx"] = rx
                out.append(mk)
            cursor = body.get("cursor")
            if not cursor:
                return out
