"""Event-contract WebSocket: live YES books for the quoted BTC markets, plus our fills.

Frames verified live on KXBTC15M (hundreds of deltas per second on one market):
  orderbook_snapshot  msg.yes_dollars_fp / msg.no_dollars_fp = [[price, size]] (fractional sizes)
  orderbook_delta     msg.price_dollars, msg.delta_fp, msg.side in {yes, no}, msg.ts_ms
A NO bid at p is a YES offer at 1 - p (the Kalshi repo's strategy/quoting.top_of_book). Prices are held as
integer micro-dollars so the 0.1c tail levels and the 1c band levels share one exact grid.

One `orderbook_delta` subscription per market, so a roll is subscribe-new / unsubscribe-old and
a seq gap only invalidates the market whose subscription skipped. `fill` is account-wide.
"""
import json
import threading
import time
import urllib.parse

import websocket

from kbtc import settings
from kbtc.kalshi_root import auth
from kbtc.secrets import kalshi_credentials

MICROS = 1_000_000


def to_micros(s):
    return int(round(float(s) * MICROS))


class YesBook:
    def __init__(self):
        self.yes, self.no = {}, {}            # micros -> size
        self.valid = False
        self.ts_ms = None
        self.rx = None

    def snapshot(self, yes, no):
        self.yes = {to_micros(p): float(q) for p, q in yes if float(q) > 0}
        self.no = {to_micros(p): float(q) for p, q in no if float(q) > 0}
        self.valid = True

    def delta(self, side, price, dq):
        lv = self.yes if side == "yes" else self.no
        p = to_micros(price)
        q = lv.get(p, 0.0) + float(dq)
        if q > 1e-9:
            lv[p] = q
        else:
            if q < -1e-6:
                self.valid = False
            lv.pop(p, None)

    def top(self):
        """(best_bid, best_ask, bid_size, ask_size) in YES dollars; a missing side is None."""
        bb = max(self.yes) if self.yes else None
        bn = max(self.no) if self.no else None
        return ((bb / MICROS) if bb is not None else None,
                ((MICROS - bn) / MICROS) if bn is not None else None,
                self.yes.get(bb) if bb is not None else None,
                self.no.get(bn) if bn is not None else None)


class EventFeed:
    def __init__(self, url=settings.EVENT_WS_URL, on_book=None, on_fill=None,
                 on_level=None, on_trade=None):
        self.url = url
        self.on_book, self.on_fill = on_book, on_fill
        # Queue estimation hooks (strategy.queue_est): every level change and every public print
        # on a subscribed market. Called on the WS thread; must be quick.
        self.on_level, self.on_trade = on_level, on_trade
        self.trade_sid_of, self.pending_trade = {}, {}
        self.lock = threading.Lock()
        self.books = {}                        # ticker -> YesBook
        self.sid_of = {}                       # ticker -> sid
        self.ticker_of = {}                    # sid -> ticker
        self.seq = {}                          # sid -> last seq
        self.pending = {}                      # cmd id -> ticker
        self.fills = []
        self.gaps = 0
        self.reconnects = 0
        self._id = 10
        self._ws = None
        self._stop = threading.Event()
        self._want = set()
        self.fill_sid = None

    def start(self):
        threading.Thread(target=self._run, name="event-ws", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._ws and self._ws.close()
        except Exception:
            pass

    def _send(self, obj):
        self._id += 1
        obj["id"] = self._id
        self._ws.send(json.dumps(obj))
        return self._id

    def set_markets(self, tickers):
        """Subscribe to exactly `tickers` (adds new, drops the rest)."""
        tickers = set(tickers)
        with self.lock:
            add = tickers - self._want
            drop = self._want - tickers
            self._want = tickers
        if self._ws is None:
            return
        for t in add:
            self._subscribe(t)
        for t in drop:
            sids = [x for x in (self.sid_of.pop(t, None), self.trade_sid_of.pop(t, None)) if x is not None]
            for sid in sids:
                self.ticker_of.pop(sid, None)
            if sids:
                try:
                    self._send({"cmd": "unsubscribe", "params": {"sids": sids}})
                except Exception:
                    pass
            with self.lock:
                self.books.pop(t, None)

    def _subscribe(self, t):
        with self.lock:
            self.books[t] = YesBook()
        i = self._send({"cmd": "subscribe", "params": {"channels": ["orderbook_delta"],
                                                       "market_tickers": [t]}})
        self.pending[i] = t
        j = self._send({"cmd": "subscribe", "params": {"channels": ["trade"], "market_tickers": [t]}})
        self.pending_trade[j] = t

    def _connect(self):
        key_id, key_path = kalshi_credentials()
        path = urllib.parse.urlparse(self.url).path
        hdrs = auth.auth_headers(key_id, key_path, "GET", path)
        ws = websocket.create_connection(self.url, header=[f"{h}: {v}" for h, v in hdrs.items()],
                                         suppress_origin=True, timeout=10)
        self._ws = ws
        self.sid_of.clear()
        self.ticker_of.clear()
        self.seq.clear()
        self._send({"cmd": "subscribe", "params": {"channels": ["fill"]}})
        for t in list(self._want):
            self._subscribe(t)

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._connect()
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
                    for b in self.books.values():
                        b.valid = False
                time.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    def handle(self, msg, rx):
        typ = msg.get("type")
        body = msg.get("msg") or {}
        if typ == "subscribed":
            ch, sid = body.get("channel"), body.get("sid")
            if ch == "orderbook_delta":
                t = self.pending.pop(msg.get("id"), None)
                if t is not None:
                    self.sid_of[t], self.ticker_of[sid] = sid, t
            elif ch == "fill":
                self.fill_sid = sid
            elif ch == "trade":
                t = self.pending_trade.pop(msg.get("id"), None)
                if t is not None:
                    self.trade_sid_of[t] = sid
            return
        if typ == "trade":
            if self.on_trade:
                self.on_trade(body.get("market_ticker"), body.get("taker_side"),
                              body.get("yes_price_dollars"), body.get("count_fp"))
            return
        if typ in ("orderbook_snapshot", "orderbook_delta"):
            sid, seq = msg.get("sid"), msg.get("seq")
            t = body.get("market_ticker") or self.ticker_of.get(sid)
            with self.lock:
                b = self.books.get(t)
                if b is None:
                    return
                before = b.top() if b.valid else None
                if typ == "orderbook_snapshot":
                    b.snapshot(body.get("yes_dollars_fp") or [], body.get("no_dollars_fp") or [])
                else:
                    last = self.seq.get(sid)
                    if last is not None and seq is not None and seq != last + 1:
                        self.gaps += 1
                        b.valid = False
                    lv = b.yes if body["side"] == "yes" else b.no
                    pm = to_micros(body["price_dollars"])
                    level_before = lv.get(pm, 0.0)
                    b.delta(body["side"], body["price_dollars"], body["delta_fp"])
                    b.ts_ms = body.get("ts_ms")
                    lvl_evt = (body["side"], pm, float(body["delta_fp"]), level_before)
                self.seq[sid] = seq
                b.rx = rx
                valid = b.valid
                changed = valid and b.top() != before
            if typ == "orderbook_delta" and self.on_level is not None:
                self.on_level(t, *lvl_evt)
            if not valid:
                self._resnapshot(t, sid)
            elif changed and self.on_book:
                # Only a TOP change wakes the quoter: ~850 deltas/s arrive on one 15m market and
                # nearly all of them are deep levels the quoting rules never read.
                self.on_book(t, rx, body.get("ts_ms"))
            return
        if typ == "fill":
            body = dict(body)
            body["_rx"] = rx
            with self.lock:
                self.fills.append(body)
            if self.on_fill:
                self.on_fill(body)

    _last_snap = {}

    def _resnapshot(self, t, sid):
        now = time.time()
        if now - self._last_snap.get(t, 0) < 2.0 or sid is None:
            return
        self._last_snap[t] = now
        try:
            self._send({"cmd": "update_subscription",
                        "params": {"sids": [sid], "market_tickers": [t], "action": "get_snapshot"}})
        except Exception:
            pass

    def level_size(self, t, book_side, price_m):
        with self.lock:
            b = self.books.get(t)
            if b is None:
                return None
            return (b.yes if book_side == "yes" else b.no).get(price_m, 0.0)

    def top(self, t):
        """(bid, ask, bid_size, ask_size, ts_ms, rx) or None if the book is not usable."""
        with self.lock:
            b = self.books.get(t)
            if b is None or not b.valid:
                return None
            return (*b.top(), b.ts_ms, b.rx)
