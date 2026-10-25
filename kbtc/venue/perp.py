"""KXBTCPERP: REST parsers/client and the WebSocket feed that is the fast spot read.

Verified against docs.kalshi.com (margin-rest, margin-ws) and live frames:
  REST  GET /margin/markets/{t}                 bid, ask, price, reference_price{price,ts_ms},
                                                settlement_mark_price, liquidation_mark_price,
                                                tick_size, contract_size
        GET /margin/markets/{t}/orderbook       {"orderbook": {"bids": [[px, qty]], "asks": ...}}
        GET /margin/funding_rates/estimate      funding_rate, mark_price, next_funding_time
        GET /margin/markets/{t}/candlesticks    period_interval in {1, 60, 1440} minutes
  WS    orderbook_delta  snapshot {bid, ask}, then {price, delta, side, ts_ms}; seq per sid
        ticker           coalesced to <= 1/s; reference_price is BRTI stamped on the whole second
                         and arrives ~630 ms after that stamp
Prices are dollars PER CONTRACT; a contract is 0.0001 BTC, so BTC/USD = price / 0.0001 and one
0.0001 tick is $1. Everything below converts to BTC/USD floats at the boundary.

Premium-index and mark-price formulas are not published; funding is carried for sanity checks
only and never feeds the spot estimate.
"""
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass

import websocket

from kbtc import settings
from kbtc.kalshi_root import auth
from kbtc.secrets import kalshi_credentials
from kbtc.venue.book import L2Book

TICK_USD = 1.0      # one 0.0001 price tick on a 0.0001 BTC contract


def usd(price_str):
    """Per-contract dollar price string -> BTC/USD float."""
    return float(price_str) / settings.PERP_CONTRACT_BTC


def ticks_to_usd(ticks):
    return ticks * TICK_USD


@dataclass
class Stamped:
    value: float        # BTC/USD (or a rate, for funding)
    ts_ms: int          # exchange timestamp
    rx: float           # local receive time, epoch seconds


def parse_stamped(obj, rx):
    if not obj or obj.get("price") is None:
        return None
    return Stamped(usd(obj["price"]), int(obj["ts_ms"]), rx)


def parse_market(body):
    m = body["market"]
    if abs(float(m["contract_size"]) - settings.PERP_CONTRACT_BTC) > 1e-12:
        raise ValueError(f"contract_size {m['contract_size']} != settings.PERP_CONTRACT_BTC")
    rx = body.get("_rx", {}).get("t_recv", time.time())
    return {"ticker": m["ticker"], "status": m["status"],
            "bid": usd(m["bid"]) if m.get("bid") else None,
            "ask": usd(m["ask"]) if m.get("ask") else None,
            "reference": parse_stamped(m.get("reference_price"), rx),
            "settlement_mark": parse_stamped(m.get("settlement_mark_price"), rx),
            "liquidation_mark": parse_stamped(m.get("liquidation_mark_price"), rx),
            "tick_size": m["tick_size"], "contract_size": m["contract_size"]}


def parse_orderbook(body):
    ob = body["orderbook"]
    book = L2Book()
    book.apply_snapshot(ob.get("bids") or [], ob.get("asks") or [])
    return book


def parse_funding_estimate(body):
    return {"rate": float(body["funding_rate"]), "mark": usd(body["mark_price"]),
            "next_funding_time": body["next_funding_time"],
            "computed_time": body["computed_time"]}


# A candle whose close spread is wider than this is a one-sided or empty book, not a price.
# Seen in the first days after listing: asks at the int64 sentinel 4.6e18 and mids alternating
# by thousands of dollars minute to minute.
MAX_CANDLE_SPREAD_BPS = 10.0


def parse_candles(body):
    """-> list of (end_ts, mid_close) using bid/ask closes; price close is last trade.

    Candles with a missing side or a close spread over MAX_CANDLE_SPREAD_BPS are DROPPED (a gap),
    never repaired.
    """
    out = []
    for c in body.get("candlesticks", []):
        b, a = (c.get("bid") or {}).get("close"), (c.get("ask") or {}).get("close")
        if b is None or a is None:
            continue
        bu, au = usd(b), usd(a)
        mid = 0.5 * (bu + au)
        if not (0 < bu < au) or 1e4 * (au - bu) / mid > MAX_CANDLE_SPREAD_BPS:
            continue
        out.append((int(c["end_period_ts"]), mid))
    return out


class PerpRest:
    """REST reads for one perp market, each charged to its own rate-budget category."""

    def __init__(self, client, ticker=settings.PERP_TICKER):
        self.c, self.t = client, ticker

    def market(self):
        return parse_market(self.c.get(f"/margin/markets/{self.t}", category="market"))

    def orderbook(self, depth=0):
        return parse_orderbook(self.c.get(f"/margin/markets/{self.t}/orderbook",
                                          {"depth": depth}, category="book"))

    def funding_estimate(self):
        return parse_funding_estimate(self.c.get("/margin/funding_rates/estimate",
                                                 {"ticker": self.t}, category="funding"))

    def candles_1m(self, start_ts, end_ts):
        """1-minute candles, paged in <= 1000-minute chunks (the range limit is undocumented)."""
        out, s, end = [], int(start_ts), int(end_ts)
        while s < end:                       # ints on both sides: a float end never terminates
            e = min(end, s + 1000 * 60)
            out += parse_candles(self.c.get(f"/margin/markets/{self.t}/candlesticks",
                                            {"start_ts": s, "end_ts": e, "period_interval": 1},
                                            category="candles"))
            s = e
        seen, dedup = set(), []
        for ts, m in sorted(out):
            if ts not in seen:
                seen.add(ts)
                dedup.append((ts, m))
        return dedup


class PerpFeed:
    """Background WS thread: live L2 book + latest ticker fields for one perp market.

    A seq gap on the book subscription invalidates the book and requests a fresh snapshot;
    the socket is reconnected (with a new snapshot) on any error. `on_book` / `on_ticker`
    callbacks run on the WS thread and must be quick.
    """

    def __init__(self, ticker=settings.PERP_TICKER, url=settings.MARGIN_WS_URL,
                 on_book=None, on_ticker=None):
        self.ticker, self.url = ticker, url
        self.on_book, self.on_ticker = on_book, on_ticker
        self.book = L2Book()
        self.lock = threading.Lock()
        self.last_book_rx = None
        self.last_ticker = None          # dict of parsed ticker fields
        self.seq = None
        self.book_sid = None
        self.gaps = 0
        self.reconnects = 0
        self.frames = 0
        self._stop = threading.Event()
        self._snap_asked = 0.0           # one get_snapshot per SNAPSHOT_RETRY_S while invalid
        self._ws = None
        self._thread = None

    # -- lifecycle -------------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="perp-ws", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def wait_ready(self, timeout=15.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self.lock:
                if self.book.valid and self.book.top() is not None and self.last_ticker:
                    return True
            time.sleep(0.05)
        return False

    # -- socket ----------------------------------------------------------------------------
    def _connect(self):
        key_id, key_path = kalshi_credentials()
        path = urllib.parse.urlparse(self.url).path
        hdrs = auth.auth_headers(key_id, key_path, "GET", path)
        # suppress_origin: Kalshi's WS edge 403s a handshake carrying Origin (seen on the
        # event-contract WS; the Kalshi repo's kalshi/ws.py). Same client library, same rule.
        ws = websocket.create_connection(self.url, header=[f"{h}: {v}" for h, v in hdrs.items()],
                                         suppress_origin=True, timeout=10)
        ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                            "params": {"channels": ["orderbook_delta", "ticker"],
                                       "market_tickers": [self.ticker]}}))
        return ws

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._ws = self._connect()
                with self.lock:
                    self.book.valid = False
                    self.seq = None
                    self.book_sid = None
                backoff = 1.0
                while not self._stop.is_set():
                    raw = self._ws.recv()
                    if not raw:
                        raise ConnectionError("empty frame")
                    self.handle(json.loads(raw), time.time())
            except Exception:
                if self._stop.is_set():
                    break
                self.reconnects += 1
                with self.lock:
                    self.book.valid = False
                time.sleep(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                try:
                    if self._ws is not None:
                        self._ws.close()
                except Exception:
                    pass

    # -- frames (pure given the frame; pure functions, testable on captured frames) -----------------
    def handle(self, msg, rx):
        self.frames += 1
        typ = msg.get("type")
        body = msg.get("msg") or {}
        if typ == "subscribed" and body.get("channel") == "orderbook_delta":
            self.book_sid = body.get("sid")
            return
        if typ in ("orderbook_snapshot", "orderbook_delta"):
            seq = msg.get("seq")
            with self.lock:
                if typ == "orderbook_snapshot":
                    self.book.apply_snapshot(body.get("bid") or [], body.get("ask") or [])
                    self.book_sid = msg.get("sid", self.book_sid)
                else:
                    if self.seq is not None and seq is not None and seq != self.seq + 1:
                        self.gaps += 1
                        self.book.valid = False
                    self.book.apply_delta(body["side"], body["price"], body["delta"],
                                          body.get("ts_ms"))
                self.seq = seq
                self.last_book_rx = rx
                valid = self.book.valid
            if not valid:
                self._request_snapshot()
            elif self.on_book:
                self.on_book(self, rx)
            return
        if typ == "ticker":
            t = {"rx": rx, "ts_ms": body.get("ts_ms"),
                 "bid": usd(body["bid"]) if body.get("bid") else None,
                 "ask": usd(body["ask"]) if body.get("ask") else None,
                 "reference": parse_stamped(body.get("reference_price"), rx),
                 "settlement_mark": parse_stamped(body.get("settlement_mark_price"), rx),
                 "funding_rate": (body.get("funding_rate") or {}).get("rate"),
                 "next_funding_time_ms": (body.get("funding_rate") or {}).get("next_funding_time_ms")}
            with self.lock:
                self.last_ticker = t
            if self.on_ticker:
                self.on_ticker(self, t)
            return
        if typ == "error":
            self.last_error = body

    SNAPSHOT_RETRY_S = 2.0

    def _request_snapshot(self):
        ws = self._ws
        now = time.time()
        if ws is None or self.book_sid is None or now - self._snap_asked < self.SNAPSHOT_RETRY_S:
            return
        self._snap_asked = now
        try:
            ws.send(json.dumps({"id": 2, "cmd": "update_subscription",
                                "params": {"sids": [self.book_sid],
                                           "market_tickers": [self.ticker],
                                           "action": "get_snapshot"}}))
        except Exception:
            pass

    def top_usd(self):
        """(bid, bid_size, ask, ask_size, exchange_ts_ms, rx) in BTC/USD, or None."""
        with self.lock:
            if not self.book.valid:
                return None
            t = self.book.top()
            if t is None:
                return None
            return (ticks_to_usd(t[0]), t[1], ticks_to_usd(t[2]), t[3],
                    self.book.ts_ms, self.last_book_rx)
