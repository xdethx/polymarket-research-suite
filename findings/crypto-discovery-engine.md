# Finding: Polymarket Crypto Price-Interval Markets Are Efficient for Retail Takers

A systematic multi-front investigation across 77,559 sessions of 5-minute and 15-minute
Polymarket binary crypto price-interval markets found no exploitable retail taker edge.
Four hypotheses were tested independently; all produced NO verdicts.

---

## Dataset

**77,559 market sessions** collected from live Polymarket data across BTC, ETH, SOL, XRP,
and other assets. Session-level features extracted: open/mid/pre-expiry orderbook snapshots,
Binance spot prices, momentum, imbalance, lead-lag gap features. Each session has a binary
outcome (UP/DOWN) determined by the close-vs-open Binance price comparison.

---

## Hypothesis 1 — Entry edge: orderbook lead-lag mispricing at session open

**Hypothesis:** The Polymarket orderbook mid lags Binance by several seconds at session open.
A retail taker observing a directional Binance move can enter before the book reprices,
capturing a lead-lag edge.

**Method:**

Define `market_error = outcome_binary − open_mid`. A positive `market_error` means the market
under-priced the UP outcome at open. Screen all open-tick features (raw orderbook, Binance
price, lead-lag gap, imbalance, momentum) against `market_error` using:

1. Pearson correlation with `market_error` (threshold |corr| ≥ 0.03 for "meaningful")
2. Within-`open_mid`-decile conditional correlation (removes the confound that `open_mid`
   trivially predicts UP-rate — the conditional test asks: *given that we hold the market's
   own probability estimate fixed, does the feature carry additional information?*)

**Results:**

- `open_mid` AUC = **0.6587** (sanity pass — the market's price strongly predicts the outcome)
- `mean(market_error)` = **−0.00197** (market is well-calibrated; no systematic bias)
- Max |corr(feature, market_error)| = **0.0128** (`open_price_diff_pct`), **0.0114** (`open_binance_dir`)
- Max within-decile conditional corr = **0.0031** (`open_lead_lag_gap`)
- All values far below the 0.03 meaningful-signal threshold

**Verdict: MARKET EFFICIENT.** The Polymarket orderbook incorporates Binance price changes
within approximately 5 seconds. No open-tick feature predicts where the market is wrong
after controlling for the market's own probability estimate.

---

## Hypothesis 2 — Exit timing: early exit from a winning position

**Hypothesis:** A position that is winning at mid-session but sitting near 50¢ (close to
even) is at risk of flipping to a loss. Exiting early on this signal would reduce variance
and improve overall PnL.

**Method:**

Among 50,390 sessions where the position was winning at mid-session, predict
STAYS WIN vs FLIPS. Screen mid-session and pre-expiry orderbook/momentum features
against the flip label.

Economic test: compare three policies:
- **Policy A (hold):** hold all positions to expiry
- **Policy B (signal exit):** exit when a feature exceeds a threshold
- **Policy C (naive exit-all):** exit all winning mid-session positions

Including calibrated taker fee (7.2% of `p×(1−p)`) and 1-tick slippage throughout.

**Results:**

- `mid_mid_dist_from_even` AUC = **0.7567** (strong statistical signal)
- `pre_mid_dist_from_even` AUC = **0.7406**
- Policy A mean_pnl = **−0.0242** (baseline)
- Best Policy B (threshold = 0.02): mean_pnl = **−0.0252** (worse than hold; t = −3.04)
- Policy C: mean_pnl = **−0.0494** (t = −17.55 vs A)

**Verdict: TAUTOLOGY.** The AUC is real — a near-0.5 position mid-session is genuinely
unstable — but exiting costs spread plus fee, and that cost exceeds the variance reduction.
The ordering Policy A > B_best > C is consistent across 5-minute and 15-minute markets and
across chronological halves of the dataset.

This finding illustrates a critical distinction: **high AUC does not imply economic edge.**
Statistical separability and economic profitability are independent properties.

---

## Hypothesis 3 — Late-window entry: Binance residual signal near expiry

**Hypothesis:** At t ≈ 40 seconds before expiry, the market is nearly settled (late_mid AUC ≈
0.95). However, a small residual Binance price signal might still be tradeable in the
remaining window.

**Method:**

62,554 sessions with a valid late-window tick (t ∈ [30, 50] seconds). Measure
`corr(price_diff_pct, market_error_late)` as a potential signal.

Economic test: forward-only simulation using `late_binance_dir != 0` as the entry criterion
(no outcome information; observable at t ≈ 40s). Fill gated on fillable fraction.

**Results:**

- `late_mid` AUC = **0.9524** (market prices the outcome almost perfectly at t ≈ 40s)
- `corr(price_diff_pct, market_error_late)` = **0.03178** (marginally above threshold)
- Fillable fraction: **61.6%**
- Forward-only sim: n = 40,170 fillable, WR = **45.63%**, mean_pnl = **−$0.015**, total = **−$612**

**Verdict: THEORETICAL-ONLY, UNFILLABLE.** The signal exists but is consumed by
late-window spread widening and taker fee. The late-window book drains rapidly as expiry
approaches, making cost-inclusive fills unprofitable.

*Note: an earlier analysis that appeared profitable (+$3,035 total) was self-identified as
look-ahead contaminated — it filtered on the actual outcome to define the trade set.
The forward-only corrected simulation (Step 4A) is the definitive result.*

---

## Hypothesis 4 — Maker strategy: earn the spread instead of paying it

**Hypothesis:** Inverting the cost structure by posting resting limit orders eliminates the
taker fee and earns the spread, potentially making the same signals profitable.

**Method:**

Analyzed 2,000 sessions / 293,786 snapshots for depth/flow fields needed to estimate fill
probability. Assessed execution infrastructure capability for resting-order lifecycle.

**Results:**

- Depth/flow fields (needed for fill-probability estimation): **0 / 293,786** (absent)
- Touch-based fill proxy at −0.01 offset: **58.3% fill rate**, but post-fill mid drift is
  100% adverse by construction (when `best_ask ≤ P`, mid has already crossed below the
  limit by at least spread/2)
- Maker fee: **unknown** — no maker fills ever recorded, so the fee rate cannot be measured
- Resting-order lifecycle gap: `POSTED` status discarded; user-WS MATCHED channel not consumed

**Verdict: GATED — pursue only after P1 fee verification.** The entire maker thesis depends
on whether the maker fee is materially below the calibrated taker fee (7.2%). A 1–2 hour
probe to record one live maker fill would resolve this. Building the full maker infrastructure
before measuring the fee would be premature.

---

## Key methodological lessons

**Calibrated fee model is necessary.** A naive breakeven analysis using a publicly stated
fee rate produced a breakeven bar 6–10 percentage points below the empirically estimated
true breakeven. The on-chain-calibrated fee model (fitted to 109 matched fill/chain pairs,
RMSE 0.000786 on `p×(1−p)`) closed this gap.

**Adverse selection is real and measurable.** Forensic analysis of live trades found that
filled positions had a 3.6 pp lower win rate than hypothetical-reject positions (73.7% vs
77.3%). The venue systematically accepts the worse signals — a structural headwind that
backtest simulations assuming 100% fill do not capture.

**Per-side calibration is mandatory.** A pooled fill-rate logistic model appeared well-
calibrated in aggregate (±2pp gate) but discriminated less than 1pp across the realistic
spread range. The NO-only slope (b₁ = −15.7) was diluted 17× by pooling with the opposite-
signed YES-only slope (b₁ = +5.7). Fitting per-side first revealed discrimination of 16.3pp;
the aggregate gate was blind to this collapse.
