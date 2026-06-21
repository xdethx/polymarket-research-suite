# Finding: Maker Strategy Is Gated on an Unknown Fee Rate

The investigation into a maker (passive limit-order) strategy for Polymarket crypto
price-interval markets was inconclusive due to a single unresolvable unknown: the maker
fee rate. All further analysis is gated on a P1 fee probe that requires live execution.

---

## Motivation

The four-front taker-edge investigation closed with no viable path for retail takers
(entry EFFICIENT, exit TAUTOLOGY, late-window UNFILLABLE). The natural follow-on question
is: does the unfavorability stem from the *taker* fee structure? If so, posting resting
limit orders instead of crossing the spread might invert the cost structure.

A calibrated taker fee rate of **7.2% of p·(1−p)** was derived from on-chain data
(109 matched fill/chain pairs, RMSE 0.000786). For a mid-market trade at p = 0.5,
this fee is approximately 1.8 cents per dollar of notional. If the maker fee is
materially lower — or zero — the same signals that are unprofitable as a taker might
become profitable as a maker.

---

## What the data shows

**Dataset:** 2,000 sessions, 293,786 snapshots

**Depth and flow fields:** The fields needed to estimate maker fill probability
(order-level depth, trade flow, cancel rate) are absent from the available data.
All 293,786 snapshots carry `None` for these fields. There is no path to estimating
fill probability from historical data.

**Touch-based fill proxy:** As a rough substitute, a position is considered "fillable"
when `best_ask ≤ P` (the limit price is at or above the best ask). This gives a
nominal fill rate of **58.3%** at a −0.01 offset from mid. However, this estimate
has a fatal flaw: whenever the condition `best_ask ≤ P` is true, the mid has already
crossed below P by at least spread/2. Post-fill adverse drift is therefore 100% by
construction. The touch proxy overestimates fill quality.

**Maker fee:** **Unknown.** Zero maker fills have ever been recorded. The fee rate
cannot be measured from existing data.

---

## Infrastructure gap

The execution infrastructure supports GTC limit order posting (`order_type="GTC"`),
but not the resting-order lifecycle:

- `POSTED` status (order resting in the book) is discarded by the live engine
- The user WebSocket MATCHED channel (which delivers resting-order fills) is not consumed
- There is no mechanism to cancel resting orders after position expiry or signal flip

Building the maker lifecycle would require a non-trivial extension to the execution
infrastructure: tracking open resting orders per market, handling the MATCHED event,
and canceling stale orders at session close.

---

## The single largest unknown

The entire maker thesis rests on one assumption: that the maker fee is materially below
the calibrated taker fee of 7.2% of p·(1−p). If the maker fee is similar to the taker
fee, the structural advantage of posting disappears and the thesis collapses.

This is resolvable with a **P1 fee probe**: post one resting limit order, let it fill,
and read the maker fee from the on-chain settlement. Total time: 1–2 hours. Total cost:
the size of one minimum fill.

---

## Recommendation

**Do not build the maker lifecycle infrastructure (P2–P5) before the P1 fee probe.**

If the P1 probe confirms a maker fee materially below 7.2%, the following would be
needed before evaluating the strategy:
- Forward-recorded depth and flow data (depth fields are currently absent)
- A fill-probability model that does not use the flawed touch proxy
- A resting-order lifecycle in the execution layer

If the P1 probe shows comparable maker and taker fees, the thesis is closed.

---

## Summary

| Item | Status |
|------|--------|
| Taker fee (calibrated) | 7.2% of p·(1−p) |
| Maker fee | Unknown (zero fills recorded) |
| Historical fill data | Absent (293,786 snapshots, 0 depth fields) |
| Fill probability model | No viable approach on historical data |
| Execution lifecycle | Partial (GTC post exists; MATCHED channel not consumed) |
| **Verdict** | **GATED — P1 fee probe required before any further investment** |
