# discovery_engine

A systematic predictor screen for identifying whether any observable signal at session
open predicts market pricing errors in Polymarket binary crypto-price markets.

## What this demonstrates

`screen_predictors.py` implements two independent screens over a session-level feature
table and prints structured verdicts. It is the core of the Wave 7 discovery-engine
analysis that closed the crypto research track with four consecutive NO verdicts.

**STEP 2 — Entry edge screen** (open-tick features only; no look-ahead)

The entry hypothesis: *does any observable feature at session open predict where the
Polymarket orderbook mid is wrong relative to the ultimate binary outcome?*

Method: define `market_error = outcome_binary − open_mid`, then screen features against
this residual rather than against the raw outcome. This separates "does the feature
contain information beyond what the market already priced?" from "does a high mid
predict an UP outcome?" (trivially yes, by construction).

Three checks:
- **A1** — single-feature rank-AUC (descriptive; `open_mid` is the reference baseline)
- **A2b** — Pearson correlation with `market_error` (threshold: |corr| ≥ 0.03)
- **A2c** — within-`open_mid`-decile conditional correlation (removes the confound
  that features correlating with `open_mid` trivially track the UP-rate trend)

**STEP 3 — Exit-timing screen** (mid/pre-expiry features; look-ahead is acceptable
because the position side is already determined at open)

The exit hypothesis: *among sessions where the position is winning at mid-session,
can any observable feature predict whether it stays a win or flips to a loss?*

Method: benchmark against `mid_mid` AUC on the flip label, then screen mid/pre features
for anything that beats the benchmark by > 0.02 on |AUC − 0.5|.

**Sanity gate:** `open_mid` AUC vs outcome must be > 0.52. If it falls near 0.5,
outcome labeling is broken and the script stops before emitting any verdict.

## Results on the 77,559-session dataset

| Screen | Key finding | Verdict |
|--------|-------------|---------|
| Entry A1 | `open_mid` AUC = 0.6587 (sanity pass). Max other |AUC−0.5| < 0.10 | Reference only |
| Entry A2b | Max \|corr(feature, market_error)\| = 0.0128 (`open_price_diff_pct`) | Below 0.03 threshold |
| Entry A2c | Max within-decile conditional corr = 0.0031 | Below 0.03 threshold |
| **ENTRY VERDICT** | | **MARKET EFFICIENT** |
| Exit Step 3 | `mid_mid_dist_from_even` AUC = 0.7567 (strong statistical signal) | |
| Exit economic test | Policy A (hold) mean_pnl = −0.024; best Policy B = −0.025 | Exit is worse than hold |
| **EXIT VERDICT** | | **TAUTOLOGY** (high AUC ≠ economic edge) |

The exit finding illustrates a key principle: **high AUC does not imply economic edge.**
The signal is real — a position near 0.5 at mid is genuinely unstable — but exiting
costs spread + fee, and that cost exceeds the variance reduction. Statistical significance
and economic significance are not the same thing.

## How to run

```bash
# Default — runs on the bundled synthetic sample, no arguments needed
python discovery_engine/screen_predictors.py

# Run on your own session-feature CSV
python discovery_engine/screen_predictors.py path/to/your/sessions_leadlag.csv
```

**Synthetic sample (`sample_sessions_leadlag.csv`):** A 45-row illustrative dataset
committed alongside the script so a reviewer can see the method run end-to-end and
inspect the output format. The values are procedurally generated (not market data);
the verdicts it prints are not research findings. The sample exists purely as
scaffolding — to exercise the code and demonstrate its output structure.

**Running on real data:** Supply a session-feature CSV with the columns listed in
`OPEN_FEATURES`, `OPEN_DERIVED_FEATURES`, and `EXIT_FEATURES` in `screen_predictors.py`,
plus `outcome` (`UP`/`DOWN`) and `interval` (`5`/`15`). The real results in the table
above come from the private 77,559-session dataset built from the data-ingestion recorder.

## Dependencies

```
stdlib only: csv, math, os, statistics, sys
```

No imports from the trading engine — this script is fully self-contained.
