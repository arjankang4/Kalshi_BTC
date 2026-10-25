"""Summarise one quoter session journal: speed, staleness, fills and adverse selection.

    python -m kbtc.tools.session_report var/log/quoter-<start>.jsonl

Latency legs (ms, p50/p90/p99):
  perp exch->rx      KXBTCPERP book delta exchange ts_ms to our receive
  perp rx->wake      our receive to the fast loop picking it up (queueing inside our process)
  wake->theo         re-price (forward shift, digital, rv shadow, Greeks)
  theo->decide       decide_ladder + lean for both sides
  decide->ack        order send to exchange ack, per create/cancel (live only)
  spot->ack          perp exchange ts to order ack: the whole chain
  event exch->rx     the quoted market's own book delta to our receive
Staleness: resting orders whose edge went <= 0 against the current theo, count and time-to-gone.
Fills: count, taker/maker, markouts vs mid and theo at +1/+5/+30/+60 s (positive = good for us).
"""
import collections
import json
import statistics as st
import sys


def q(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 5:
        return {"n": len(xs), "values": [round(x, 2) for x in xs]}
    p = st.quantiles(xs, n=100)
    return {"n": len(xs), "p50": round(p[49], 2), "p90": round(p[89], 2), "p99": round(p[98], 2),
            "max": round(max(xs), 2)}


def report(path):
    R = [json.loads(l) for l in open(path)]
    T = [r for r in R if r["kind"] == "tick"]
    acts = [(r, a) for r in T for a in r["actions"]]
    out = {"journal": path, "kinds": dict(collections.Counter(r["kind"] for r in R))}
    start = next((r for r in R if r["kind"] == "start"), {})
    end = next((r for r in R if r["kind"] == "end"), {})
    out["live"] = start.get("live")
    out["end"] = {k: end.get(k) for k in ("why", "positions", "cash", "mark", "stats")}
    out["latency_ms"] = {
        "perp exch->rx": q([r["perp_rx"] * 1e3 - r["perp_exch_ms"] for r in T if r.get("perp_exch_ms")]),
        "perp rx->wake (perp-triggered ticks)": q([1e3 * (r["t_wake"] - r["perp_rx"]) for r in T
                                                   if r.get("perp_rx") and r.get("trigger") == "perp"]),
        "wake->theo": q([1e3 * (r["t_theo"] - r["t_wake"]) for r in T]),
        "theo->decide": q([1e3 * (r["t_decide"] - r["t_theo"]) for r in T]),
        "decide->ack (create)": q([1e3 * (a["t_ack"] - r["t_decide"]) for r, a in acts if a["op"] == "place"]),
        "order rtt (create)": q([a["rtt_ms"] for r, a in acts if a["op"] == "place" and a.get("rtt_ms")]),
        "order rtt (cancel)": q([a["rtt_ms"] for r, a in acts if a["op"] == "cancel" and a.get("rtt_ms")]),
        "spot exch->ack (perp-triggered)": q([a["t_ack"] * 1e3 - r["perp_exch_ms"] for r, a in acts
                                              if r.get("perp_exch_ms") and r.get("trigger") == "perp"]),
        "event exch->rx": q([r["ev_book_rx"] * 1e3 - r["ev_book_exch_ms"] for r in T if r.get("ev_book_exch_ms")]),
    }
    out["ticks"] = {"n": len(T), "by_trigger": dict(collections.Counter(r.get("trigger") for r in T)), "refusals": dict(collections.Counter(
        "|".join(r["reasons"]) or "quoted" for r in T).most_common(8))}
    out["actions"] = dict(collections.Counter(f"{a['op']}:{a.get('reason')}" for _, a in acts))
    out["action_errors"] = [a for _, a in acts if not a.get("ok")][:10]
    stale = [r for r in R if r["kind"] == "stale"]
    out["stale"] = {"events": len(stale), "time_to_gone_s": q([r["stale_s"] for r in stale])}
    fills = [r for r in R if r["kind"] == "fill"]
    out["fills"] = {"n": len(fills), "contracts": sum(f["count"] for f in fills),
                    "taker": sum(1 for f in fills if f.get("is_taker")),
                    "edge_vs_theo_at_fill_c": q([100 * f["yes_dir"] * (f["theo"] - f["price"])
                                                 for f in fills if f.get("theo") is not None])}
    mk = [r for r in R if r["kind"] == "markout"]
    out["markouts_c"] = {f"+{h}s": {"vs_mid": q([100 * m["markout_mid"] for m in mk
                                               if m["horizon_s"] == h and m["markout_mid"] is not None]),
                                    "vs_theo": q([100 * m["markout_theo"] for m in mk
                                                  if m["horizon_s"] == h and m["markout_theo"] is not None])}
                         for h in (1, 5, 30, 60)}
    # theo - mid by moneyness (a check on whether the theo is over- or under-confident against
    # the book, i.e. sigma too high or too low). Positive = our theo above the book mid, in cents.
    buckets = ((0.25, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75))
    tm = {}
    for lo, hi in buckets:
        xs = [100 * (r["theo"] - 0.5 * (r["bid"] + r["ask"])) for r in T
              if r.get("theo") is not None and r.get("bid") is not None and r.get("ask") is not None
              and lo <= r["theo"] < hi]
        tm[f"{lo:.2f}-{hi:.2f}"] = q(xs)
    out["theo_minus_mid_c_by_theo"] = tm
    cy = [r for r in R if r["kind"] == "cycle"]
    out["vol"] = {"sigma_impl": q([r["sigma_impl"] for r in cy]),
                  "sigma_rv_15m": q([(r["rv"] or {}).get("sigma_ann_15m") for r in cy]),
                  "sigma_rv_1h": q([(r["rv"] or {}).get("sigma_ann_1h") for r in cy]),
                  "rv_threshold_removed_last": (cy[-1]["rv"] or {}).get("threshold_removed") if cy else None,
                  "rv_low_sample_cycles": sum(1 for r in cy if (r["rv"] or {}).get("low_samples"))}
    return out


if __name__ == "__main__":
    print(json.dumps(report(sys.argv[1]), indent=1, default=str))
