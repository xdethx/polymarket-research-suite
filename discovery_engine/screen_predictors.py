"""
discovery_engine/screen_predictors.py — Wave 7 Phase 2.5: entry edge + exit screen.

ILLUSTRATIVE — run against your own session feature CSV.
See discovery_engine/README.md for the required schema.

Reads sessions_leadlag.csv (77,559 sessions, 53 columns) and performs
two independent screens:

STEP 2 — ENTRY edge screen (open tick ONLY — no look-ahead).
  Section A1: Single-feature rank-AUC for predicting outcome==UP.
    open_mid is shown as reference but does NOT drive the verdict —
    it is the market's own probability estimate and will trivially dominate.

  Section A2: Market-error residual screen (drives the ENTRY verdict).
    market_error = (1 if outcome==UP else 0) - open_mid
    A2a: Mean market_error — sanity: should be ≈ 0 if market is calibrated.
    A2b: Pearson correlation of each open-tick feature with market_error.
    A2c: open_mid-decile conditional check — within-decile actual UP rate vs feature mean.
         NOTE: cross-decile corr(feature_mean, UP_rate) is confounded by open_mid;
         this within-decile version is the correct conditional test.

  Section B: Lead-lag specific findings.
    Does open_lead_lag_gap or open_binance_dir carry residual signal above mid?

  ENTRY verdict (from A2): MARKET EFFICIENT or SIGNAL FOUND.

STEP 3 — EXIT-timing screen (mid + pre ticks — look-ahead OK).
  "Winning at mid" = position side at open is still implied by mid_mid.
  Among winning-at-mid sessions, predict whether position STAYS WIN vs FLIPS.
  Benchmark = mid_mid AUC on the flip target.

  EXIT verdict: NO EARLY-EXIT SIGNAL or SIGNAL FOUND.

SANITY GATE:
  open_mid AUC (vs outcome==UP) must be clearly > 0.5 by construction.
  If it is ≈ 0.5, outcome labeling is broken — script STOPS before emitting verdicts.

Run:
    python discovery_engine/screen_predictors.py
        → runs on the synthetic sample bundled alongside this script
          (discovery_engine/sample_sessions_leadlag.csv); no arguments needed.

    python discovery_engine/screen_predictors.py path/to/your/sessions.csv
        → runs on your own session-feature CSV in the documented schema.

Output: printed report with verdicts.
"""

import argparse
import csv
import math
import os
import statistics
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
IN_PATH     = os.path.join("discovery_engine", "sessions_leadlag.csv")
SAMPLE_PATH = os.path.join("discovery_engine", "sample_sessions_leadlag.csv")

# ENTRY verdict thresholds
CORR_MEANINGFUL = 0.03   # |corr with market_error| threshold for "meaningful"
AUC_BEAT_MARGIN = 0.02   # |AUC-0.5| margin above open_mid to flag a candidate (Section A1)
N_DECILES       = 10     # open_mid decile count for A2c

# EXIT verdict threshold (same |AUC-0.5| margin over benchmark)
EXIT_BEAT_MARGIN = 0.02

# Open-tick feature names to screen (raw + derived lead-lag)
OPEN_RAW_FEATURES = [
    "open_mid",
    "open_spread",
    "open_imbalance",
    "open_bid_volume",
    "open_ask_volume",
    "open_momentum_5m",
    "open_price_diff_pct",
]
OPEN_DERIVED_FEATURES = [
    "open_mid_dist_from_even",
    "open_binance_dir",
    "open_orderbook_dir",
    "open_lead_lag_gap",
    "open_binance_orderbook_align",
    "open_momentum_imbalance_agree",
]
OPEN_FEATURES = OPEN_RAW_FEATURES + OPEN_DERIVED_FEATURES

# Mid/pre features for exit screen
EXIT_FEATURES = [
    "mid_mid",
    "mid_imbalance",
    "mid_momentum_5m",
    "mid_lead_lag_gap",
    "mid_mid_dist_from_even",
    "pre_mid",
    "pre_imbalance",
    "pre_momentum_5m",
    "pre_lead_lag_gap",
    "pre_mid_dist_from_even",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _f(s) -> object:
    """Parse CSV string to float; return None on blank/error."""
    if s == "" or s is None:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def rank_auc(feature_vals, binary_labels):
    """Rank-based AUC for predicting label==1.

    Uses average-rank for ties (standard Mann-Whitney U / Wilcoxon).
    Returns (auc, n_valid) where n_valid = len(pairs with non-None feature).
    """
    pairs = [(fv, lv) for fv, lv in zip(feature_vals, binary_labels) if fv is not None]
    n = len(pairs)
    n_pos = sum(1 for _, lv in pairs if lv == 1)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0 or n == 0:
        return None, n

    # Sort by feature value; compute average ranks for ties
    pairs_sorted = sorted(pairs, key=lambda x: x[0])

    # Build list of (rank_sum_for_value) with tie averaging
    # We need rank of each element (1-indexed average rank for ties)
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and pairs_sorted[j][0] == pairs_sorted[i][0]:
            j += 1
        # elements i..j-1 share the same value; average rank = (i+1 + j) / 2
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[k] = avg_rank
        i = j

    # Sum of ranks for positive class
    rank_sum_pos = sum(ranks[k] for k in range(n) if pairs_sorted[k][1] == 1)
    auc = (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return auc, n


def pearson_corr(xs, ys):
    """Pearson correlation for paired (x, y) where neither is None.

    Returns (corr, n_valid).
    """
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None, n
    xv = [p[0] for p in pairs]
    yv = [p[1] for p in pairs]
    mx = sum(xv) / n
    my = sum(yv) / n
    cov   = sum((x - mx) * (y - my) for x, y in zip(xv, yv))
    var_x = sum((x - mx) ** 2 for x in xv)
    var_y = sum((y - my) ** 2 for y in yv)
    denom = math.sqrt(var_x * var_y)
    if denom == 0:
        return 0.0, n
    return cov / denom, n


# ---------------------------------------------------------------------------
# Load sessions
# ---------------------------------------------------------------------------

def load_data(path):
    print(f"Loading {path} ...", flush=True)
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    print(f"  {len(rows)} rows loaded.", flush=True)
    return rows


# ---------------------------------------------------------------------------
# STEP 2 — ENTRY edge screen
# ---------------------------------------------------------------------------

def entry_screen(rows):
    # Filter to valid outcome rows
    valid = [r for r in rows if r.get("outcome", "") in ("UP", "DOWN")]
    n_total = len(valid)
    print(f"\nEntry screen: {n_total} sessions with valid outcome.", flush=True)
    if n_total == 0:
        print("ERROR: no valid outcome rows!", flush=True)
        return None

    binary = [1 if r["outcome"] == "UP" else 0 for r in valid]
    open_mid_vals = [_f(r.get("open_mid", "")) for r in valid]

    # ---------------------------------------------------------------------------
    # SANITY CHECK: open_mid AUC must be clearly > 0.5
    # ---------------------------------------------------------------------------
    sanity_auc, sanity_n = rank_auc(open_mid_vals, binary)
    print(f"\n--- SANITY CHECK ---", flush=True)
    print(f"open_mid AUC = {sanity_auc:.4f}  (n={sanity_n})", flush=True)
    if sanity_auc is None or sanity_auc < 0.52:
        print("FATAL: open_mid AUC ≈ 0.5 — outcome labeling is broken or reversed!", flush=True)
        print("       Stopping. Do not trust any further results.", flush=True)
        return None
    print("Sanity PASS: open_mid AUC clearly > 0.5 (market price predicts UP by construction).",
          flush=True)

    # ---------------------------------------------------------------------------
    # Section A1 — Single-feature AUC (descriptive)
    # ---------------------------------------------------------------------------
    print(f"\n--- Section A1: Single-feature AUC (descriptive, open_mid is reference) ---",
          flush=True)
    a1_rows = []
    for feat in OPEN_FEATURES:
        fvals = [_f(r.get(feat, "")) for r in valid]
        auc, n_valid = rank_auc(fvals, binary)
        if auc is None:
            a1_rows.append((feat, None, n_valid))
        else:
            a1_rows.append((feat, auc, n_valid))

    a1_rows.sort(key=lambda x: abs(x[1] - 0.5) if x[1] is not None else 0, reverse=True)

    open_mid_auc = sanity_auc
    print(f"  {'Feature':<38}  {'AUC':>7}  {'|AUC-0.5|':>9}  {'n':>8}  Note")
    print(f"  {'-'*38}  {'-'*7}  {'-'*9}  {'-'*8}  ----")
    for feat, auc, n_valid in a1_rows:
        if auc is None:
            print(f"  {feat:<38}  {'N/A':>7}  {'N/A':>9}  {n_valid:>8}")
            continue
        margin = abs(auc - 0.5) - abs(open_mid_auc - 0.5)
        note = ""
        if feat == "open_mid":
            note = "<-- reference (market price)"
        elif margin > AUC_BEAT_MARGIN:
            note = f"<-- beats open_mid by {margin:.4f} (A1 only; see A2 for verdict)"
        print(f"  {feat:<38}  {auc:>7.4f}  {abs(auc-0.5):>9.4f}  {n_valid:>8}  {note}")

    # ---------------------------------------------------------------------------
    # Section A2a — market_error target + calibration sanity
    # ---------------------------------------------------------------------------
    print(f"\n--- Section A2a: Market-error calibration sanity ---", flush=True)
    market_errors = []
    for r, om, lv in zip(valid, open_mid_vals, binary):
        if om is not None:
            market_errors.append(lv - om)
        else:
            market_errors.append(None)

    valid_me = [v for v in market_errors if v is not None]
    mean_me  = sum(valid_me) / len(valid_me) if valid_me else None
    print(f"  mean(market_error) = {mean_me:.6f}  (n={len(valid_me)})")
    if mean_me is not None and abs(mean_me) > 0.05:
        print(f"  WARNING: |mean| > 0.05 suggests systematic mispricing or labeling issue.")
    else:
        print(f"  PASS: |mean| ≤ 0.05 — market is roughly calibrated.")

    # ---------------------------------------------------------------------------
    # Section A2b — Pearson correlation with market_error
    # ---------------------------------------------------------------------------
    print(f"\n--- Section A2b: Pearson correlation(feature, market_error) ---", flush=True)
    print(f"  {'Feature':<38}  {'corr':>8}  {'|corr|':>7}  {'n':>8}  Meaningful?")
    print(f"  {'-'*38}  {'-'*8}  {'-'*7}  {'-'*8}  -----------")

    a2b_rows = []
    for feat in OPEN_FEATURES:
        fvals = [_f(r.get(feat, "")) for r in valid]
        corr, n_valid = pearson_corr(fvals, market_errors)
        a2b_rows.append((feat, corr, n_valid))

    a2b_rows.sort(key=lambda x: abs(x[1]) if x[1] is not None else 0, reverse=True)

    a2b_candidates = []
    for feat, corr, n_valid in a2b_rows:
        if corr is None:
            print(f"  {feat:<38}  {'N/A':>8}  {'N/A':>7}  {n_valid:>8}  N/A")
            continue
        meaningful = abs(corr) >= CORR_MEANINGFUL
        tag = "YES <--" if meaningful else ""
        if meaningful:
            a2b_candidates.append((feat, corr))
        print(f"  {feat:<38}  {corr:>8.5f}  {abs(corr):>7.5f}  {n_valid:>8}  {tag}")

    # ---------------------------------------------------------------------------
    # Section A2c — open_mid-decile conditional check
    # ---------------------------------------------------------------------------
    print(f"\n--- Section A2c: open_mid-decile conditional check ---", flush=True)

    # Build decile bins from valid (non-None) open_mid values
    om_valid_sorted = sorted(v for v in open_mid_vals if v is not None)
    n_om = len(om_valid_sorted)
    decile_boundaries = []
    for d in range(1, N_DECILES):
        idx = int(n_om * d / N_DECILES)
        decile_boundaries.append(om_valid_sorted[min(idx, n_om - 1)])

    def _decile(v):
        if v is None:
            return None
        for i, b in enumerate(decile_boundaries):
            if v <= b:
                return i
        return N_DECILES - 1

    # Group by decile
    decile_groups = {i: {"up": 0, "n": 0, "features": {f: [] for f in OPEN_FEATURES}}
                     for i in range(N_DECILES)}
    for r, om, lv in zip(valid, open_mid_vals, binary):
        d = _decile(om)
        if d is None:
            continue
        decile_groups[d]["n"] += 1
        decile_groups[d]["up"] += lv
        for feat in OPEN_FEATURES:
            fv = _f(r.get(feat, ""))
            if fv is not None:
                decile_groups[d]["features"][feat].append(fv)

    # For each feature, compute weighted mean within-decile correlation with market_error.
    # This is the correct conditional check: does the feature predict WHERE the
    # market is wrong, after holding open_mid roughly constant?
    a2c_rows = []
    for feat in OPEN_FEATURES:
        weighted_corr_sum = 0.0
        weight_total      = 0
        n_deciles_used    = 0
        for d in range(N_DECILES):
            pairs_feat = []
            pairs_me   = []
            for r, om, me in zip(valid, open_mid_vals, market_errors):
                if _decile(om) != d:
                    continue
                fv = _f(r.get(feat, ""))
                if fv is not None and me is not None:
                    pairs_feat.append(fv)
                    pairs_me.append(me)
            if len(pairs_feat) < 3:
                continue
            c, _ = pearson_corr(pairs_feat, pairs_me)
            if c is not None:
                weighted_corr_sum += c * len(pairs_feat)
                weight_total      += len(pairs_feat)
                n_deciles_used    += 1

        if weight_total == 0:
            a2c_rows.append((feat, None, 0))
        else:
            mean_within_corr = weighted_corr_sum / weight_total
            a2c_rows.append((feat, mean_within_corr, n_deciles_used))

    a2c_rows.sort(key=lambda x: abs(x[1]) if x[1] is not None else 0, reverse=True)

    print(f"  (Mean within-decile corr(feature, market_error) — controls for open_mid)")
    print(f"  NOTE: cross-decile corr(feature_mean, UP_rate) is confounded by open_mid;")
    print(f"  this within-decile version is the correct conditional test.")
    print(f"  {'Feature':<38}  {'mean_w_corr':>11}  {'|corr|':>7}  {'n_deciles':>9}")
    print(f"  {'-'*38}  {'-'*11}  {'-'*7}  {'-'*9}")

    a2c_candidates = []
    for feat, corr, n_dec in a2c_rows[:15]:
        if corr is None:
            print(f"  {feat:<38}  {'N/A':>11}  {'N/A':>7}  {n_dec:>9}")
            continue
        tag = " <--" if abs(corr) >= CORR_MEANINGFUL else ""
        if abs(corr) >= CORR_MEANINGFUL:
            a2c_candidates.append((feat, corr))
        print(f"  {feat:<38}  {corr:>11.6f}  {abs(corr):>7.6f}  {n_dec:>9}{tag}")

    # Detailed decile table for top-3 by |mean within-decile corr|
    top3 = [r for r in a2c_rows if r[1] is not None][:3]
    if top3:
        print(f"\n  Decile detail (top-3 features; UP%, expected UP%=open_mid, market_error):")
        print(f"  {'Decile':>6}  {'om_mean':>7}  {'n':>6}  {'UP%':>5}  {'xpUP%':>6}  {'ME':>7}",
              end="")
        for feat, _, _ in top3:
            print(f"  {feat[:16]:>16}", end="")
        print()

        for d in range(N_DECILES):
            g = decile_groups[d]
            if g["n"] == 0:
                continue
            up_pct     = 100 * g["up"] / g["n"]
            om_mean    = sum(g["features"]["open_mid"]) / len(g["features"]["open_mid"]) \
                         if g["features"]["open_mid"] else float("nan")
            exp_up_pct = 100 * om_mean
            me_mean    = up_pct/100 - om_mean
            print(f"  {d:>6}  {om_mean:>7.4f}  {g['n']:>6}  {up_pct:>5.1f}%  "
                  f"{exp_up_pct:>5.1f}%  {me_mean:>+7.4f}",
                  end="")
            for feat, _, _ in top3:
                flist = g["features"][feat]
                fmean = sum(flist) / len(flist) if flist else float("nan")
                print(f"  {fmean:>16.4f}", end="")
            print()

    # ---------------------------------------------------------------------------
    # Section B — Lead-lag specific findings
    # ---------------------------------------------------------------------------
    print(f"\n--- Section B: Lead-lag hypothesis (open_lead_lag_gap, open_binance_dir) ---",
          flush=True)

    lag_features = [
        "open_price_diff_pct", "open_binance_dir", "open_orderbook_dir",
        "open_lead_lag_gap", "open_binance_orderbook_align",
    ]

    corr_map = {feat: corr for feat, corr, _ in a2b_rows}

    print(f"  Feature correlation with market_error (lead-lag subset):")
    for feat in lag_features:
        corr = corr_map.get(feat)
        if corr is None:
            print(f"    {feat:<40}  N/A")
        else:
            tag = " <-- SIGNAL" if abs(corr) >= CORR_MEANINGFUL else ""
            print(f"    {feat:<40}  corr={corr:.5f}{tag}")

    # ---------------------------------------------------------------------------
    # ENTRY VERDICT (from A2)
    # ---------------------------------------------------------------------------
    print(f"\n{'='*70}", flush=True)
    has_signal = bool(a2b_candidates) or bool(a2c_candidates)
    if has_signal:
        signal_feats = list({f for f, _ in a2b_candidates + a2c_candidates})
        print(f"ENTRY: SIGNAL FOUND: {', '.join(signal_feats)}")
    else:
        print(f"ENTRY: MARKET EFFICIENT")
        print(f"  No open-tick feature shows |corr(market_error)| >= {CORR_MEANINGFUL}")
        print(f"  and no within-decile conditional separation found.")
        print(f"  Recommendation: pivot to exit-timing angle (see EXIT verdict below).")
    print(f"{'='*70}", flush=True)

    return {
        "a2b_candidates": a2b_candidates,
        "a2c_candidates": a2c_candidates,
        "has_signal": has_signal,
        "open_mid_auc": open_mid_auc,
        "mean_me": mean_me,
    }


# ---------------------------------------------------------------------------
# STEP 3 — EXIT-timing screen
# ---------------------------------------------------------------------------

def exit_screen(rows):
    print(f"\n\n{'='*70}", flush=True)
    print(f"STEP 3 — EXIT-TIMING SCREEN", flush=True)
    print(f"{'='*70}", flush=True)

    valid = [r for r in rows if r.get("outcome", "") in ("UP", "DOWN")]

    # Identify UP/DOWN position side at open (based on open_mid)
    winning_at_mid = []
    for r in valid:
        outcome  = r["outcome"]
        open_mid = _f(r.get("open_mid", ""))
        mid_mid  = _f(r.get("mid_mid",  ""))
        if open_mid is None or mid_mid is None:
            continue

        if open_mid > 0.5:
            pos_side = "UP"
        elif open_mid < 0.5:
            pos_side = "DOWN"
        else:
            continue  # indifferent, skip

        if pos_side == "UP" and mid_mid > 0.5:
            winning_at_mid.append(r)
        elif pos_side == "DOWN" and mid_mid < 0.5:
            winning_at_mid.append(r)

    n_winning = len(winning_at_mid)
    print(f"\nSessions winning at mid (mid_mid still on open position side): {n_winning}")
    if n_winning < 100:
        print("  WARNING: too few sessions for reliable exit screen.")

    # Label: STAYS WIN (1) vs FLIPS (0)
    flip_labels = []
    for r in winning_at_mid:
        outcome  = r["outcome"]
        open_mid = _f(r.get("open_mid", ""))
        pos_side = "UP" if open_mid > 0.5 else "DOWN"
        stays = 1 if outcome == pos_side else 0
        flip_labels.append(stays)

    n_stays = sum(flip_labels)
    n_flips = n_winning - n_stays
    pct_stays = 100 * n_stays / n_winning if n_winning > 0 else 0
    print(f"  Stays win: {n_stays} ({pct_stays:.1f}%)  |  Flips to loss: {n_flips} ({100-pct_stays:.1f}%)")

    if n_stays == 0 or n_flips == 0:
        print("  Cannot compute AUC: all sessions have the same label.")
        print("\n" + "="*70)
        print("EXIT: NO EARLY-EXIT SIGNAL (insufficient variance in flip label)")
        print("="*70)
        return

    # Benchmark: mid_mid AUC on flip target
    mid_mid_vals  = [_f(r.get("mid_mid", "")) for r in winning_at_mid]
    bench_auc, bench_n = rank_auc(mid_mid_vals, flip_labels)
    print(f"\n  Benchmark: mid_mid AUC on stays-win = {bench_auc:.4f}  (n={bench_n})")

    print(f"\n  {'Feature':<38}  {'AUC':>7}  {'|AUC-0.5|':>9}  {'Δbench':>8}  {'n':>8}  Signal?")
    print(f"  {'-'*38}  {'-'*7}  {'-'*9}  {'-'*8}  {'-'*8}  -------")

    exit_candidates = []
    for feat in EXIT_FEATURES:
        fvals = [_f(r.get(feat, "")) for r in winning_at_mid]
        auc, n_valid = rank_auc(fvals, flip_labels)
        if auc is None:
            print(f"  {feat:<38}  {'N/A':>7}  {'N/A':>9}  {'N/A':>8}  {n_valid:>8}")
            continue
        delta = abs(auc - 0.5) - (abs(bench_auc - 0.5) if bench_auc else 0)
        is_sig = delta > EXIT_BEAT_MARGIN
        tag = " <--" if is_sig else ""
        if is_sig:
            exit_candidates.append((feat, auc, delta))
        print(f"  {feat:<38}  {auc:>7.4f}  {abs(auc-0.5):>9.4f}  {delta:>+8.4f}  {n_valid:>8}{tag}")

    print(f"\n{'='*70}")
    if exit_candidates:
        cand_names = [f for f, _, _ in exit_candidates]
        print(f"EXIT: SIGNAL FOUND: {', '.join(cand_names)}")
        print(f"  These mid/pre features predict whether a winning position stays a win.")
    else:
        print(f"EXIT: NO EARLY-EXIT SIGNAL")
        print(f"  No mid/pre feature beats mid_mid benchmark by > {EXIT_BEAT_MARGIN} on |AUC-0.5|.")
        print(f"  Holding to expiry is not inferior to any available early-exit rule.")
    print(f"{'='*70}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Market-error predictor screen (entry + exit). "
                    "With no arguments, runs on the bundled synthetic sample."
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        default=None,
        help="Path to your session-feature CSV. "
             "Omit to use the bundled synthetic sample (illustrative only).",
    )
    args = parser.parse_args()

    if args.csv_path is None:
        # Default: synthetic sample bundled with the script
        path = SAMPLE_PATH
        print("=" * 70)
        print("SYNTHETIC SAMPLE MODE")
        print("Running on discovery_engine/sample_sessions_leadlag.csv")
        print("This is illustrative scaffolding — NOT a research result.")
        print("The verdicts below reflect 45 synthetic rows, not real market data.")
        print("To screen your own data:")
        print("  python discovery_engine/screen_predictors.py path/to/sessions.csv")
        print("Required schema: discovery_engine/README.md")
        print("=" * 70)
        if not os.path.exists(path):
            print(f"\nERROR: sample file not found at {path}")
            print("The repository may be incomplete. Check discovery_engine/README.md.")
            sys.exit(1)
    else:
        path = args.csv_path
        if not os.path.exists(path):
            print(f"ERROR: {path} not found.")
            print("This script expects a session feature CSV.")
            print("See discovery_engine/README.md for the required schema.")
            sys.exit(1)

    rows = load_data(path)
    result = entry_screen(rows)
    if result is None:
        print("\nABORT: sanity check failed — do not trust any verdict.")
        sys.exit(1)

    exit_screen(rows)

    print("\nscreen_predictors.py done.")


if __name__ == "__main__":
    main()
