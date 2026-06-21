# Research Log

Phase-by-phase chronology of the Polymarket research project. Coarse dates by phase.
Each phase: what was tested · verdict · lesson learned.

---

## Phase 0 — Signal discovery and first live trades (April–May 2026)

**What was tested:** Snapshot-filter signal engine over 5-minute and 15-minute binary
crypto price-interval markets. Signals driven by orderbook imbalance, Binance momentum,
and price-difference from session open. First live trading confirmed (first fill: May 2026).

**Signal quality:** Early presets achieved backtest win rates of 58–83%. Peak result:
`no_momentum_peak_hours` at 83.2% WR over 535 sessions. Several presets showed 78%+
backtest WR against a published Polymarket breakeven bar of 71.8%.

**Verdict:** Edge findable in backtest.

**Lesson:** Backtest-discovered win rates are not live win rates. The bottleneck
shifted from finding signals to understanding why backtest results do not transfer.

---

## Phase 1 — Execution realism: forensic analysis (May 2026)

**What was tested:** Why is live PnL negative when backtest WR is well above breakeven?
Forensic analysis of 774 live trades across multiple preset families.

**Key findings:**

- **Adverse selection (confirmed):** Filled trades WR = 73.7% vs hypothetical-reject WR = 77.3%.
  The venue preferentially accepts the lower-quality signals. z-test p = 0.098.
- **FAK kill rate ~40%:** About 40% of submitted Fill-or-Kill orders are rejected
  outright. Backtest assumed 100% fill.
- **Spread explosion before reject:** Spread significantly wider at the snapshot preceding
  a rejected order vs non-rejected (t-test p = 0.002), suggesting book-driven selection.
- **Real breakeven 78–82%, not 71.8%:** The published 71.8% bar uses no fee, 100% fill,
  zero slippage. Adding the calibrated taker fee + adverse selection + tail slippage raises
  the bar by 6–10 percentage points.

**Verdict:** The 71.8% bar is systematically optimistic. Any preset below 80%+ backtest
WR (cost-inclusive, with realistic fill model) cannot be expected to be profitable live.

**Lesson:** Never size capital on pre-calibration backtest figures. The execution cost
model matters as much as the signal.

---

## Wave 4.5 — Reality Calibration (May 2026)

**What was tested:** A full execution-cost calibration pipeline inserted ahead of all
remaining strategy work:

- **C3 fee model:** Calibrated `fee = FEE_RATE × p × (1−p)` against 109 matched
  on-chain fill/chain pairs. Calibrated `FEE_RATE = 0.071754`. RMSE = 0.000786.
- **C4 slippage model:** Added 1-tick slippage as the default assumption (`--slippage-ticks 1`).
  `--zero-slippage` flag for comparison.
- **C5 / D3 fill calibration:** Logistic fill-rate model per spread bucket, fitted
  per-side (NO-only: `b₀ = 0.824, b₁ = −15.7`; YES borrows NO). Pooled model
  was found to dilute the NO-side slope 17× by averaging with the opposite-signed
  YES-side slope. Per-side fitting revealed 16.3pp discrimination (pooled: < 1pp).
- **Train/holdout chronological split:** `--holdout-start` flag for forward-only
  out-of-sample evaluation.

**Verdict:** Calibration infrastructure complete. 0 of the previous preset families
survived calibrated backtesting above the real breakeven.

**Lesson:** Aggregate calibration gates (±2pp) are necessary but not sufficient.
They can pass a model that discriminates < 1pp within-strata. Always verify per-group.

---

## Wave 7 — Discovery Engine (June 2026)

**What was tested:** Systematic predictor screen across 77,559 sessions of live market data
(53 features: open/mid/pre orderbook, Binance momentum, lead-lag gap, imbalance).

Four fronts evaluated:

### Entry edge screen (Phase 2.5)
Method: `market_error = outcome_binary − open_mid` residual screen (controls for the
market's own probability estimate). Within-`open_mid`-decile conditional correlation.

Result: mean(market_error) = −0.002 (calibrated). Max |corr(feature, market_error)| = 0.013.
Max within-decile conditional corr = 0.003. Both far below 0.03 threshold.

**Verdict: MARKET EFFICIENT.** Orderbook incorporates Binance within ~5 seconds.

### Exit-timing screen (Phase 6)
Method: among sessions winning at mid, predict STAYS WIN vs FLIPS.
Economic test: Policy A (hold) vs B (signal exit) vs C (naive exit-all), with C3 fee + slippage.

Result: `mid_mid_dist_from_even` AUC = 0.756 (real statistical signal). But Policy A
(hold) mean PnL = −0.024 vs best Policy B = −0.025. Exiting early is worse than holding.

**Verdict: TAUTOLOGY.** High AUC ≠ economic edge. Exiting costs more than the variance reduction saves.

### Late-window screen (Phase 7)
Method: forward-only simulation at t ≈ 40s using `late_binance_dir != 0` (no outcome
information). Note: an earlier contaminated version used outcome-dependent filtering —
self-identified and explicitly excluded; only the forward-only result counts.

Result: n = 40,170 fillable, WR = 45.6%, mean PnL = −$0.015, total = −$612.

**Verdict: UNFILLABLE.** Theoretical corr = 0.032 (marginal), but late-window spread
widening and taker fee make cost-inclusive fills unprofitable.

### Maker feasibility (Phase 8)
Method: assess whether a maker (limit-order) strategy could invert the cost structure.
Reviewed historical data for depth/flow fields; reviewed execution infrastructure.

Result: depth/flow fields absent (0/293,786 snapshots). Maker fee: unknown (zero fills).
Execution lifecycle: POSTED status discarded; MATCHED channel not consumed.

**Verdict: GATED.** Single blocking unknown: is the maker fee materially below 7.2% taker?
One P1 probe (1 live resting order) resolves this.

**Net verdict: CRYPTO FROZEN.** Four consecutive NO verdicts. Track closed; pivot to weather.

---

## Weather Track — W0 to W2.7 (June 2026)

The weather track investigated forecast-arbitrage in Polymarket daily-temperature markets:
do ensemble weather models identify mispriced temperature buckets?

### W0 — D-1 feasibility study

**What was tested:** Can a D-1 ensemble forecast (ECMWF + GFS) identify Polymarket
temperature buckets where the model and market disagree materially?

**Finding:** The two models disagreed by 2.25°C on a 1°C-wide bucket ladder for London
June 10 (ECMWF μ = 15.90°C vs GFS μ = 18.15°C). A naive single-model analysis reported
+0.18 to +0.33 edge on multiple buckets — half in opposite directions. The market sat
between the models, which is the *correct* response to genuine model uncertainty.

**Verdict: NEEDS FORWARD DATA.** At D-1 the market appears well-calibrated relative to
multi-model consensus. "Edge" from a single model is an inter-model disagreement artifact.

**Lesson:** Multi-model blending with disagreement-widens-σ safety property is mandatory.

### W1 — Pure model core (math-seam unit tests)

**What was built:** `bucket_model.py`, `blend.py`, `edge.py`, `cities.py`. 130 unit tests.
W0 London worked example reproduced numerically. Both rounding regimes (WU, HKO) verified.

**Key property verified:** When ECMWF and GFS disagree, `blend_models()` produces
σ_blend = 1.398 > max(0.92, 0.73) — the anti-overfit guarantee holds.

### W2 — Market I/O and intraday smell-test

**What was tested:** Live intraday screen across 52 temperature markets.

**Finding:** 51/52 markets showed |edge| ≥ 8% on the largest bucket.
Modal outcome agreement between model and market: only 11/52 markets (21%).
Mean blended σ = 0.97°C; intraday gap visible.

**Verdict: INTRADAY GAP VISIBLE — attribution unclear.** Could be genuine model edge
or stale market pricing or measurement artifact. Gate W3 on independent validation.

### W2.5 — Edge validation (RETRACTED)

**What was tested:** 5-city case study: is the W2 gap genuine edge or artifact?
Tokyo, Seoul, Shanghai classified as "EDGE IS REAL"; London, Paris as "LIKELY ARTIFACT."

**Initial verdict:** EDGE IS REAL (3/5 cities). **This verdict was retracted.**

**Error discovered:** Realized temperatures sourced from Open-Meteo reanalysis
(`forecast+past_days=2`). This product underestimated station temperatures by 0.9–4.4°C.
After re-running with METAR/ASOS from the resolution station: all 3 "REAL" cases
reclassified as OUR-ERROR. The market was correct in every case.

### W2.6 — Edge hardening and METAR re-verification

**What was tested:** Independent re-verification of W2.5 "REAL" cases using METAR/ASOS
from aviationweather.gov and IEM ASOS archive.

**Verdict: WAS OUR ERROR.** All three East Asian cases fail METAR verification.
Permanent rule: *never use Open-Meteo as realized temperature; only METAR/ASOS.*

**Decision: do NOT build W3 recorder.** No confirmed edge to justify it.

### W2.7 / H3 — Intraday price-path feasibility

**What was tested:** Can a basket of model-confident positions be executed profitably
intraday, before the day's temperature high is confirmed?

**Finding 1:** Settled-market CLOB `prices-history` endpoint returns HTTP 400
(clobTokenIds are cleared once the market settles). Zero intraday price paths available
for settled markets. The hypothesis cannot be tested on historical data.

**Finding 2 (informational, look-ahead):** Analysis restricted to winning markets shows
positive intraday price travel. Explicitly labeled: *"⚠ LOOK-AHEAD — you cannot know the
winner in the morning."* Excluded from the verdict.

**Finding 3 (forward-only):** Basket P&L using only model-driven selection (no outcome
information) is ≤ 0 after costs in all scenarios tested.

**Verdict: UNTESTABLE ON HISTORY.** Circular dependency: cannot prove H3 without a forward
recorder; cannot justify building the recorder without proving H3.

**Weather track status: human-decision gate.** No hypothesis survived to a testable YES.
Further investment requires a forward-recording decision and is subject to human approval.

---

## Current status

All research fronts are at a decision gate:

| Track | Status |
|-------|--------|
| Crypto taker | FROZEN — four NO verdicts |
| Crypto maker | GATED — P1 fee probe required |
| Weather D-1 | GATED — forward recorder required |
| Weather intraday | UNTESTABLE — forward recorder required |
