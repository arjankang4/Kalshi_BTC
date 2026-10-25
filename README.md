# Kalshi_BTC — quoting Kalshi's BTC event markets

A market maker for Kalshi's bitcoin contracts — `KXBTC15M` (15-minute up/down) and `KXBTCD`
(hourly / daily above-below) — built on the quoting rules of the SPX/NDX engine in
[**Kalshi**](https://github.com/arjankang4/Kalshi), with its own spot, volatility and venue layers.
A C++ port of the equity engine lives in [**Kalshi_Prod**](https://github.com/arjankang4/Kalshi_Prod).

> **What is not published.** This is a public copy of a private repository. The research (the
> microstructure, flow and lean studies, the RV model bake-offs), the fitted model artifacts
> (`var/model/`), the recorded market data, and the live tuned parameters are private. Tuned values
> in `kbtc/settings.py` are **illustrative**. The quoter is dry by default and needs Kalshi
> credentials and those artifacts to run.

| | |
|---|---|
| Markets | `KXBTC15M` (15-min up/down), `KXBTCD` (above/below: hourly, 17:00 ET daily, Friday weekly), `KXBTC` (ranges) |
| Settlement | simple average of the 60 CF Benchmarks BRTI prints before `close_time` |
| Fast spot | the `KXBTCPERP` margin WebSocket book, minus an EWMA basis to BRTI |
| Theo | a digital on the **60-second settlement average**, priced off realized vol, blended with the Kalshi book mid |
| Edge | the equity engine's dollar-Greek edge curve, re-anchored per series, with time-to-close width caps |

## How it works

```
perp WS book ──► microprice − EWMA basis to BRTI ──► S_hat           (venue/perp, pricing/spot)
Coinbase / perp returns ──► intraday RV forecast on a time-of-week calendar   (rvol/)
hourly KXBTCD ladder ──► implied vol cross-check                      (pricing/implied)
                │
                ▼
P(average of 60 BRTI prints > K) given S_hat, sigma, tau             (pricing/digital)
blend with the Kalshi book mid; dollar Greeks → R → required edge    (pricing/greeks, edgecurve)
                │
                ▼
slow 1-s loop (vol, IV, anchor, market selection) + fast path on every perp change
→ post-only ladders via the equity engine's decide_ladder / plan_side  (strategy/quoter)
→ per-category token-bucket rate budget, position and daily-loss limits, kill switch
```

## Layout

```
kbtc/settings.py          every threshold and parameter (units in the names)
kbtc/secrets.py           Kalshi key id/path, read at call time from KBTC_KALSHI_ENV
kbtc/kalshi_root.py       the ONLY bridge to the Kalshi repo: kalshi.auth, pricing.bs, pricing.brackets, harness
kbtc/venue/               ratebudget (per-category token buckets), rest, book, perp (REST+WS), eventfeed, orders
kbtc/pricing/             spot (S_hat), digital (60-s average settlement), implied, greeks, edgecurve
kbtc/rvol/                realized-vol forecast: EWMA baseline, intraday log-HAR with a time-of-week term, live feed
kbtc/strategy/            quoter (slow loop + fast path), the shared SPX ladder logic, account risk, lean pipeline
kbtc/tools/               theo-vs-book, perp recorder, session report
tests/                    python -m tests.test_*  (harness style, no pytest); tests/run_all.sh
```

`tests/test_import_guard` fails if anything pulls in the equity engine's `config` — kbtc borrows
only config-free modules from it.

## Running

Clone [Kalshi](https://github.com/arjankang4/Kalshi) beside this repo (or point `KBTC_KALSHI_ROOT`
at it) and its `requirements.txt` covers both.

```bash
export KBTC_KALSHI_ROOT=../Kalshi KBTC_KALSHI_ENV=../Kalshi/.env.kalshi
tests/run_all.sh                                                      # unit tests

python -m kbtc.strategy.quoter --minutes 15                           # dry: plans orders, sends none
KBTC_ALLOW_LIVE=1 python -m kbtc.strategy.quoter --minutes 15 --live  # live needs BOTH switches
touch var/HALT                                                        # kill switch: pulls within 1 s
```

## Venue notes

- Perp price is dollars per 0.0001-BTC contract: BTC/USD = price / 0.0001; one tick = $1.
- The margin WebSocket signs the path `/trade-api/ws/v2/margin` and handshakes without `Origin`.
  `orderbook_delta` is uncoalesced; `ticker` carries BRTI stamped on the whole second.
- Rate limits are per account: event-contract and perp reads are separate buckets, and the event
  bucket is shared with the equity engine, so kbtc takes a configured share of it.
- Premium-index and mark-price formulas are unpublished, so funding is a sanity check only.
